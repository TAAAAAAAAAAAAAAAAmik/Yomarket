from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from storage import get_settings, save_settings

router = Router()

# ---------------------------------------------------------------------------
# Category emoji mapping (best-effort)
# ---------------------------------------------------------------------------
_CAT_EMOJI: dict[str, str] = {
    "игры": "🎮",
    "предложения": "💎",
    "подарки": "🎁",
}

_DEFAULT_CAT_EMOJI = "📦"


def _cat_emoji(name: str) -> str:
    return _CAT_EMOJI.get(name.lower(), _DEFAULT_CAT_EMOJI)


# ---------------------------------------------------------------------------
# FSM
# ---------------------------------------------------------------------------

class ResponderState(StatesGroup):
    waiting_message = State()    # user is typing the responder text
    confirming_save = State()    # user sees preview, can save or edit
    confirming_delete = State()  # user confirms deletion


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _trunc(s: str, n: int = 30) -> str:
    """Truncate to n chars for use in callback_data."""
    return s[:n]


def _cancel_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="resp:cats")
    return b.as_markup()


async def _load_all_ads(api) -> list[dict]:
    """Fetch all ad pages from the API."""
    ads: list[dict] = []
    if api is None:
        return ads
    cursor = None
    while True:
        try:
            data = await api.get_ads(cursor=cursor)
        except Exception:
            break
        items = data.get("data") or data.get("items") or []
        if not items:
            break
        ads.extend(items)
        cursor = data.get("cursor") or data.get("next_cursor")
        if not cursor:
            break
    return ads


def _ad_title(ad: dict) -> str:
    return (
        ad.get("title")
        or ad.get("name")
        or ad.get("product_name")
        or "—"
    )


def _ad_category(ad: dict) -> str:
    cat = (
        ad.get("category")
        or ad.get("category_name")
        or (ad.get("category_info") or {}).get("name")
        or ""
    )
    return str(cat).strip()


# ---------------------------------------------------------------------------
# Step 2: Category list
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "resp:cats")
async def show_categories(callback: CallbackQuery, api) -> None:
    ads = await _load_all_ads(api)

    # Collect unique non-empty categories
    seen: list[str] = []
    for ad in ads:
        cat = _ad_category(ad)
        if cat and cat not in seen:
            seen.append(cat)

    builder = InlineKeyboardBuilder()
    if seen:
        for cat in seen:
            emoji = _cat_emoji(cat)
            builder.button(
                text=f"{emoji} {cat}",
                callback_data=f"resp:cat:{_trunc(cat, 30)}",
            )
    builder.button(text="⬅️ Назад", callback_data="auto:menu")
    builder.adjust(1)

    if not seen:
        text = "📩 <b>Автоответчики</b>\n\nУ вас пока нет активных объявлений.\nДобавьте товары на YooMarket, чтобы настроить автоответчики."
    else:
        text = "📩 <b>Автоответчики</b>\n\nВыберите категорию для настройки автоответчика:"

    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()


# ---------------------------------------------------------------------------
# Step 3: Game list within a category
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("resp:cat:"))
async def show_games(callback: CallbackQuery, api) -> None:
    cat_key = callback.data[len("resp:cat:"):]

    ads = await _load_all_ads(api)

    # Filter ads whose category (truncated to 30) matches cat_key
    filtered = [
        ad for ad in ads
        if _trunc(_ad_category(ad), 30) == cat_key
    ]

    # Full category name from first match
    cat_full = _ad_category(filtered[0]) if filtered else cat_key

    # Group by title
    title_counts: dict[str, int] = {}
    for ad in filtered:
        t = _ad_title(ad)
        title_counts[t] = title_counts.get(t, 0) + 1

    def _count_label(n: int) -> str:
        if n == 1:
            return "1 товар"
        if 2 <= n <= 4:
            return f"{n} товара"
        return f"{n} товаров"

    builder = InlineKeyboardBuilder()
    for title, count in title_counts.items():
        builder.button(
            text=f"{title} — {_count_label(count)}",
            callback_data=f"resp:game:{_trunc(title, 30)}",
        )
    builder.button(text="⬅️ Назад", callback_data="resp:cats")
    builder.adjust(1)

    emoji = _cat_emoji(cat_full)
    text = (
        f"{emoji} <b>Категория: {cat_full}</b>\n\n"
        "Выберите игру:"
    )
    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()


# ---------------------------------------------------------------------------
# Step 4: Show current responder for a game
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("resp:game:"))
async def show_game_responder(callback: CallbackQuery, api) -> None:
    title_key = callback.data[len("resp:game:"):]

    # Resolve full title & category from ads
    ads_list = await _load_all_ads(api)
    full_title = title_key
    cat_full = ""
    for ad in ads_list:
        if _trunc(_ad_title(ad), 30) == title_key:
            full_title = _ad_title(ad)
            cat_full = _ad_category(ad)
            break

    s = get_settings(callback.from_user.id)
    responders = s.get("responders", {})
    existing = responders.get(full_title)

    emoji = _cat_emoji(cat_full)
    header = f"{emoji} <b>{full_title}</b>\nКатегория: {cat_full}\n\n"

    builder = InlineKeyboardBuilder()
    if existing:
        text = (
            header
            + "Текущий автоответчик:\n"
            + "——————————————\n"
            + f"{existing}\n"
            + "——————————————"
        )
        builder.button(text="✏️ Изменить", callback_data=f"resp:edit:{title_key}")
        builder.button(text="🗑 Удалить", callback_data=f"resp:del:{title_key}")
        builder.button(text="⬅️ Назад", callback_data=f"resp:cat:{_trunc(cat_full, 30)}")
        builder.adjust(2, 1)
    else:
        text = header + "Текущий автоответчик: —"
        builder.button(text="➕ Добавить автоответчик", callback_data=f"resp:add:{title_key}")
        builder.button(text="⬅️ Назад", callback_data=f"resp:cat:{_trunc(cat_full, 30)}")
        builder.adjust(1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()


# ---------------------------------------------------------------------------
# Step 5a: Add responder — prompt for message
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("resp:add:"))
async def add_responder_start(callback: CallbackQuery, state: FSMContext, api) -> None:
    title_key = callback.data[len("resp:add:"):]
    full_title, cat_full = await _resolve_title(title_key, api)

    await state.set_state(ResponderState.waiting_message)
    await state.update_data(game_name=full_title, category=cat_full, title_key=title_key)

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data=f"resp:game:{title_key}")
    await callback.message.edit_text(
        f"✏️ Напишите автоответчик для <b>{full_title}</b>:",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Step 5b: Edit responder — prompt for message
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("resp:edit:"))
async def edit_responder_start(callback: CallbackQuery, state: FSMContext, api) -> None:
    title_key = callback.data[len("resp:edit:"):]
    full_title, cat_full = await _resolve_title(title_key, api)

    await state.set_state(ResponderState.waiting_message)
    await state.update_data(game_name=full_title, category=cat_full, title_key=title_key)

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data=f"resp:game:{title_key}")
    await callback.message.edit_text(
        f"✏️ Напишите автоответчик для <b>{full_title}</b>:",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Step 6: User typed message → show preview
# ---------------------------------------------------------------------------

@router.message(ResponderState.waiting_message)
async def responder_preview(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    full_title = data.get("game_name", "")
    title_key = data.get("title_key", _trunc(full_title, 30))
    draft = message.text or ""

    await state.set_state(ResponderState.confirming_save)
    await state.update_data(draft_message=draft)

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Сохранить", callback_data="resp:save")
    builder.button(text="✏️ Изменить", callback_data=f"resp:reedit")
    builder.adjust(2)

    await message.answer(
        f"👀 <b>Предпросмотр автоответчика для {full_title}:</b>\n"
        "——————————————\n"
        f"{draft}\n"
        "——————————————",
        reply_markup=builder.as_markup(),
    )


# ---------------------------------------------------------------------------
# Step 7: Save
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "resp:save")
async def save_responder(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    full_title = data.get("game_name", "")
    draft = data.get("draft_message", "")

    await state.clear()

    s = get_settings(callback.from_user.id)
    responders = s.get("responders", {})
    responders[full_title] = draft
    s["responders"] = responders
    save_settings(callback.from_user.id, s)

    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ К автоответчикам", callback_data="resp:cats")

    await callback.message.edit_text(
        f"✅ <b>Автоответчик для {full_title} сохранён!</b>",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Re-edit: go back to typing
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "resp:reedit")
async def reedit_responder(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    full_title = data.get("game_name", "")
    title_key = data.get("title_key", _trunc(full_title, 30))

    await state.set_state(ResponderState.waiting_message)

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data=f"resp:game:{title_key}")
    await callback.message.edit_text(
        f"✏️ Напишите автоответчик для <b>{full_title}</b>:",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Delete flow
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("resp:del:") & ~F.data.startswith("resp:del_confirm:"))
async def delete_responder_confirm(callback: CallbackQuery, api) -> None:
    title_key = callback.data[len("resp:del:"):]
    full_title, _ = await _resolve_title(title_key, api)

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, удалить", callback_data=f"resp:del_confirm:{title_key}")
    builder.button(text="❌ Отмена", callback_data=f"resp:game:{title_key}")
    builder.adjust(2)

    await callback.message.edit_text(
        f"❓ Удалить автоответчик для <b>{full_title}</b>?",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("resp:del_confirm:"))
async def delete_responder_do(callback: CallbackQuery, api) -> None:
    title_key = callback.data[len("resp:del_confirm:"):]
    full_title, _ = await _resolve_title(title_key, api)

    s = get_settings(callback.from_user.id)
    responders = s.get("responders", {})
    responders.pop(full_title, None)
    s["responders"] = responders
    save_settings(callback.from_user.id, s)

    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ К автоответчикам", callback_data="resp:cats")

    await callback.message.edit_text(
        f"✅ Автоответчик для <b>{full_title}</b> удалён",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Internal helper: resolve full title + category from title_key via ads API
# ---------------------------------------------------------------------------

async def _resolve_title(title_key: str, api) -> tuple[str, str]:
    """Return (full_title, category) for a given truncated title_key."""
    ads_list = await _load_all_ads(api)
    for ad in ads_list:
        if _trunc(_ad_title(ad), 30) == title_key:
            return _ad_title(ad), _ad_category(ad)
    return title_key, ""
