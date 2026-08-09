from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from storage import get_settings, save_settings

import autoreply as ar
from datetime import datetime

router = Router()


def _esc(value) -> str:
    """Названия приходят с маркетплейса; «<» в них Telegram считает разметкой и
    отвергает всё сообщение целиком."""
    import html
    return html.escape(str(value), quote=False)


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

def _key(s: str) -> str:
    """Короткий устойчивый ключ названия для callback_data.

    Раньше здесь была обрезка до 30 символов, и два товара с одинаковым началом
    названия («Steam Gift 500 рублей регион…» и «Steam Gift 500 рублей Казах…»)
    давали один и тот же ключ: открывался и правился чужой автоответчик.
    Хеш от полного названия такого не допускает, а в callback_data влезает с
    запасом.
    """
    import hashlib
    if not s:
        return "0"
    return hashlib.sha1(str(s).encode("utf-8")).hexdigest()[:12]


def _cancel_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="resp:cats")
    return b.as_markup()


_LAST_ADS_ERROR: dict[int, str] = {}


async def _load_all_ads(api, uid: int = 0) -> list[dict]:
    """Все страницы объявлений из API.

    Ошибка запроса запоминается, а не проглатывается: пустой список выглядит
    точно так же, как «объявлений нет», и экран уверенно сообщал продавцу с
    сотней товаров, что товаров у него нет.
    """
    _LAST_ADS_ERROR.pop(uid, None)
    ads: list[dict] = []
    if api is None:
        _LAST_ADS_ERROR[uid] = "нет токена — отправьте /start"
        return ads
    cursor = None
    while True:
        try:
            data = await api.get_ads(cursor=cursor)
        except Exception as e:
            if not ads:
                _LAST_ADS_ERROR[uid] = str(e)[:150] or type(e).__name__
            break
        items = data.get("data") or data.get("items") or []
        if not items:
            break
        ads.extend(items)
        cursor = data.get("cursor") or data.get("next_cursor")
        if not cursor:
            break
    return ads


def _count_label(n: int) -> str:
    if n == 1:
        return "1 товар"
    if 2 <= n <= 4:
        return f"{n} товара"
    return f"{n} товаров"


def _title_buttons(builder: InlineKeyboardBuilder, ads: list[dict]) -> int:
    """Кнопки по названиям товаров. Возвращает, сколько получилось."""
    counts: dict[str, int] = {}
    for ad in ads:
        t = _ad_title(ad)
        counts[t] = counts.get(t, 0) + 1
    for title, count in counts.items():
        builder.button(text=f"{title[:30]} — {_count_label(count)}",
                       callback_data=f"resp:game:{_key(title)}")
    return len(counts)


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
    """Ответы, привязанные к конкретному товару.

    Экран строился вокруг категорий, и когда маркетплейс их не отдавал, он
    сообщал «у вас нет активных объявлений» — продавцу с полной витриной.
    Категория тут вспомогательная: если её нет, товары показываются сразу.
    """
    uid = callback.from_user.id
    ads = await _load_all_ads(api, uid)
    err = _LAST_ADS_ERROR.get(uid, "")

    seen: list[str] = []
    for ad in ads:
        cat = _ad_category(ad)
        if cat and cat not in seen:
            seen.append(cat)

    builder = InlineKeyboardBuilder()
    head = "🎮 <b>Ответы по товарам</b>\n\n"

    if err:
        text = (head + f"❌ Объявления не загрузились:\n<code>{_esc(err)}</code>\n\n"
                "<i>Это сбой связи с Юмаркетом, а не отсутствие товаров.</i>")
    elif not ads:
        text = (head + "Объявлений нет — привязывать ответ не к чему.\n\n"
                "<i>Автоответы по ключевым словам работают и без объявлений: "
                "они отвечают на сообщения покупателя.</i>")
        builder.button(text="📩 Автоответы", callback_data="ar:menu")
    elif not seen:
        # Категорий в ответе API нет — шаг с категориями просто пропускаем.
        n = _title_buttons(builder, ads)
        text = (head + f"Товаров: <b>{n}</b>. Выберите, для какого настроить "
                "ответ:")
    else:
        for cat in seen:
            builder.button(text=f"{_cat_emoji(cat)} {cat}",
                           callback_data=f"resp:cat:{_key(cat)}")
        text = (head + f"Объявлений: <b>{len(ads)}</b>. Выберите категорию:")

    builder.button(text="⬅️ Автоответы", callback_data="ar:menu")
    builder.adjust(1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()


# ---------------------------------------------------------------------------
# Step 3: Game list within a category
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("resp:cat:"))
async def show_games(callback: CallbackQuery, api) -> None:
    cat_key = callback.data[len("resp:cat:"):]

    ads = await _load_all_ads(api)

    # Ads whose category key matches the one from the button
    filtered = [
        ad for ad in ads
        if _key(_ad_category(ad)) == cat_key
    ]

    # Full category name from first match
    cat_full = _ad_category(filtered[0]) if filtered else ""

    builder = InlineKeyboardBuilder()
    n = _title_buttons(builder, filtered)
    builder.button(text="⬅️ Назад", callback_data="resp:cats")
    builder.adjust(1)

    if n:
        text = (f"{_cat_emoji(cat_full)} <b>{_esc(cat_full)}</b>\n\n"
                f"Товаров: <b>{n}</b>. Выберите, для какого настроить ответ:")
    else:
        # Категория пришла из старой кнопки, а витрина с тех пор изменилась.
        text = ("📭 <b>В этой категории товаров нет</b>\n\n"
                "Похоже, список изменился — вернитесь и выберите заново.")
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
        if _key(_ad_title(ad)) == title_key:
            full_title = _ad_title(ad)
            cat_full = _ad_category(ad)
            break

    s = get_settings(callback.from_user.id)
    responders = s.get("responders", {})
    existing = responders.get(full_title)

    emoji = _cat_emoji(cat_full)
    header = (f"{emoji} <b>{_esc(full_title)}</b>\n"
              + (f"Категория: {_esc(cat_full)}\n" if cat_full else "")
              + f"\n<i>Подстановки: {ar.HINT}</i>\n\n")

    # Без категории возвращаться некуда — её экран показал бы пустоту.
    back = f"resp:cat:{_key(cat_full)}" if cat_full else "resp:cats"
    builder = InlineKeyboardBuilder()
    if existing:
        text = (header + "Текущий ответ:\n"
                + f"<blockquote>{_esc(existing)}</blockquote>")
        builder.button(text="✏️ Изменить", callback_data=f"resp:edit:{title_key}")
        builder.button(text="🗑 Удалить", callback_data=f"resp:del:{title_key}")
        builder.button(text="⬅️ Назад", callback_data=back)
        builder.adjust(2, 1)
    else:
        text = header + "Ответа для этого товара пока нет."
        builder.button(text="➕ Добавить ответ", callback_data=f"resp:add:{title_key}")
        builder.button(text="⬅️ Назад", callback_data=back)
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
    title_key = data.get("title_key", _key(full_title))
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
    title_key = data.get("title_key", _key(full_title))

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
        if _key(_ad_title(ad)) == title_key:
            return _ad_title(ad), _ad_category(ad)
    return title_key, ""


# ═══════════════════════════════════════════════════════════════════════════
# Автоответы: ответы на сообщения покупателя
#
# Раньше «автоответчик» умел одно — написать что-то в чат при новом заказе.
# Покупатель, задавший вопрос, ответа не получал вовсе; продавцу приходило
# уведомление, и всё. Здесь всё общение собрано в один раздел: правила по
# ключевым словам, готовые наборы, журнал отправок с ошибками и проверка, на
# которой видно, что именно бот ответит на конкретное сообщение.
# ═══════════════════════════════════════════════════════════════════════════

class AutoReplyState(StatesGroup):
    kw = State()          # ключевые слова нового правила
    text = State()        # текст нового правила
    edit_kw = State()
    edit_text = State()
    fallback = State()
    cooldown = State()
    cap = State()
    hours = State()
    test = State()


def _conf(uid: int) -> tuple[dict, dict]:
    """(настройки, блок автоответов) — блок всегда с полями по умолчанию."""
    s = get_settings(uid)
    return s, ar.cfg(s)


def _save(uid: int, s: dict) -> None:
    save_settings(uid, s)


def _sw(on: bool) -> str:
    return "🟢" if on else "🔴"


def _ar_text(conf: dict) -> str:
    rules = conf.get("rules") or []
    live = [r for r in rules if r.get("on", True) and (r.get("text") or "").strip()]
    fb = conf.get("fallback") or {}
    on = conf.get("enabled", False)

    lines = ["📩 <b>Автоответы покупателю</b>", ""]
    lines.append(f"{_sw(on)} <b>{'Включены' if on else 'Выключены'}</b>")
    lines.append(f"📝 Правил: <b>{len(live)}</b>"
                 + (f" из {len(rules)}" if len(rules) != len(live) else ""))
    lines.append(f"💬 Запасной ответ: {_sw(bool(fb.get('on')))}")

    if on and not live and not fb.get("on"):
        lines += ["", "⚠️ <i>Отвечать нечем: нет ни одного правила. "
                      "Возьмите готовый набор — это одно нажатие.</i>"]

    when = ("только вне рабочего времени "
            f"({int(conf.get('from_hour', 22)):02d}:00–"
            f"{int(conf.get('to_hour', 9)):02d}:00)"
            if conf.get("quiet_only") else "круглосуточно")
    lines += ["", f"🕐 Когда: {when}",
              f"⏸ Пауза: <b>{int(conf.get('cooldown_min', 30))} мин</b> "
              f"· не больше <b>{int(conf.get('max_per_order', 3))}</b> на заказ"]

    sent = conf.get("log") or []
    if sent:
        failed = sum(1 for e in sent if not e.get("ok"))
        last = sent[0]
        when_s = datetime.fromtimestamp(float(last.get("ts", 0) or 0)).strftime("%d.%m %H:%M")
        lines += ["", f"📜 Последний: {'✅' if last.get('ok') else '❌'} {when_s}"]
        if failed:
            lines.append(f"   ❌ не доставлено за последнее время: <b>{failed}</b>")
    return "\n".join(lines)


def _ar_kb(conf: dict) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    on = conf.get("enabled", False)
    b.button(text=f"{'🔴 Выключить' if on else '🟢 Включить'} автоответы",
             callback_data="ar:t:on")
    b.button(text=f"📝 Правила ({len(conf.get('rules') or [])})", callback_data="ar:rules")
    b.button(text="✨ Готовые наборы", callback_data="ar:tpl")
    b.button(text="🧪 Проверка", callback_data="ar:test")
    b.button(text="🔍 Почему молчит", callback_data="ar:why")
    b.button(text="📜 Журнал", callback_data="ar:log")
    b.button(text="⚙️ Когда отвечать", callback_data="ar:opts")
    b.button(text="📦 Ответы на события заказа", callback_data="ar:events")
    b.button(text="🎮 Ответы по товарам", callback_data="resp:cats")
    b.button(text="⬅️ Чаты", callback_data="menu:chats")
    b.adjust(1, 2, 2, 2, 1, 1, 1)
    return b.as_markup()


@router.callback_query(F.data == "ar:menu")
async def ar_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    _, conf = _conf(callback.from_user.id)
    await callback.message.edit_text(_ar_text(conf), reply_markup=_ar_kb(conf))
    await callback.answer()


@router.callback_query(F.data == "ar:t:on")
async def ar_toggle(callback: CallbackQuery) -> None:
    s, conf = _conf(callback.from_user.id)
    conf["enabled"] = not conf.get("enabled", False)
    _save(callback.from_user.id, s)
    live = [r for r in (conf.get("rules") or []) if r.get("on", True)]
    if conf["enabled"] and not live and not (conf.get("fallback") or {}).get("on"):
        # Включить и промолчать — худший вариант: продавец уверен, что
        # покупателям отвечают. Сразу ведём туда, где это чинится.
        await callback.answer("Включено, но правил нет — выберите набор",
                              show_alert=True)
        return await ar_templates(callback)
    await callback.answer("🟢 Включено" if conf["enabled"] else "🔴 Выключено")
    await callback.message.edit_text(_ar_text(conf), reply_markup=_ar_kb(conf))


# ─────────────────────────────── правила ───────────────────────────────

def _rules_text(conf: dict) -> str:
    rules = conf.get("rules") or []
    lines = ["📝 <b>Правила автоответа</b>", ""]
    if not rules:
        lines.append("Пока пусто. Правило — это набор слов из сообщения "
                     "покупателя и ответ на них.")
        lines.append("")
        lines.append("Быстрее всего — взять готовый набор.")
    else:
        lines.append("Срабатывает то правило, чьё слово <b>длиннее</b>: "
                     "«ключ не подошёл» победит «ключ».")
    fb = conf.get("fallback") or {}
    if fb.get("on"):
        lines += ["", f"💬 <b>Запасной ответ</b> (когда ничего не совпало):",
                  f"<i>{_esc(str(fb.get('text', ''))[:120])}</i>"]
    return "\n".join(lines)


def _rules_kb(conf: dict) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for rule in (conf.get("rules") or [])[:20]:
        kws = ", ".join(str(k) for k in (rule.get("keywords") or []))[:28] or "без слов"
        hits = int(rule.get("hits", 0) or 0)
        b.button(text=f"{_sw(rule.get('on', True))} {kws}" + (f" · {hits}" if hits else ""),
                 callback_data=f"ar:r:{rule.get('id')}")
    b.button(text="➕ Добавить правило", callback_data="ar:radd")
    b.button(text="✨ Готовые наборы", callback_data="ar:tpl")
    fb_on = bool((conf.get("fallback") or {}).get("on"))
    b.button(text=f"{_sw(fb_on)} Запасной ответ", callback_data="ar:t:fb")
    b.button(text="✏️ Текст запасного", callback_data="ar:fbtx")
    b.button(text="⬅️ Автоответы", callback_data="ar:menu")
    b.adjust(1)
    return b.as_markup()


@router.callback_query(F.data == "ar:rules")
async def ar_rules(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    _, conf = _conf(callback.from_user.id)
    await callback.message.edit_text(_rules_text(conf), reply_markup=_rules_kb(conf))
    await callback.answer()


@router.callback_query(F.data.startswith("ar:r:"))
async def ar_rule(callback: CallbackQuery) -> None:
    rid = callback.data[len("ar:r:"):]
    _, conf = _conf(callback.from_user.id)
    rule = ar.find_rule(conf, rid)
    if not rule:
        # Правило удалили из другого окна — не оставляем экран мёртвым.
        await callback.answer("Правило не найдено", show_alert=True)
        await callback.message.edit_text(_rules_text(conf),
                                         reply_markup=_rules_kb(conf))
        return

    kws = ", ".join(str(k) for k in (rule.get("keywords") or [])) or "—"
    weak = bool(rule.get("weak"))
    text = (f"📝 <b>Правило</b>\n\n"
            f"🔑 Слова: <b>{_esc(kws)}</b>\n"
            f"{_sw(rule.get('on', True))} {'Включено' if rule.get('on', True) else 'Выключено'}"
            f" · сработало раз: <b>{int(rule.get('hits', 0) or 0)}</b>\n"
            f"{'🐢 Только если ничего другого не совпало' if weak else '⚡ Обычный приоритет'}\n\n"
            f"💬 Ответ:\n<blockquote>{_esc(rule.get('text', ''))}</blockquote>")

    b = InlineKeyboardBuilder()
    b.button(text=f"{'🔴 Выключить' if rule.get('on', True) else '🟢 Включить'}",
             callback_data=f"ar:ron:{rid}")
    b.button(text="🔑 Слова", callback_data=f"ar:rkw:{rid}")
    b.button(text="✏️ Текст", callback_data=f"ar:rtx:{rid}")
    b.button(text=("⚡ Сделать обычным" if weak else "🐢 Только как запасное"),
             callback_data=f"ar:rw:{rid}")
    b.button(text="🗑 Удалить", callback_data=f"ar:rdel:{rid}")
    b.button(text="⬅️ Правила", callback_data="ar:rules")
    b.adjust(1, 2, 1, 1, 1)
    await callback.message.edit_text(text, reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("ar:ron:"))
async def ar_rule_toggle(callback: CallbackQuery) -> None:
    rid = callback.data[len("ar:ron:"):]
    s, conf = _conf(callback.from_user.id)
    rule = ar.find_rule(conf, rid)
    if rule:
        rule["on"] = not rule.get("on", True)
        _save(callback.from_user.id, s)
    await ar_rule(callback)


@router.callback_query(F.data.startswith("ar:rw:"))
async def ar_rule_weak(callback: CallbackQuery) -> None:
    """Понизить правило до «запасного» — или вернуть обычный приоритет."""
    rid = callback.data[len("ar:rw:"):]
    s, conf = _conf(callback.from_user.id)
    rule = ar.find_rule(conf, rid)
    if rule:
        rule["weak"] = not rule.get("weak", False)
        _save(callback.from_user.id, s)
    await ar_rule(callback)


@router.callback_query(F.data.startswith("ar:rdel:") & ~F.data.startswith("ar:rdelok:"))
async def ar_rule_del(callback: CallbackQuery) -> None:
    rid = callback.data[len("ar:rdel:"):]
    _, conf = _conf(callback.from_user.id)
    rule = ar.find_rule(conf, rid) or {}
    kws = ", ".join(str(k) for k in (rule.get("keywords") or []))[:60]
    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, удалить", callback_data=f"ar:rdelok:{rid}")
    b.button(text="❌ Отмена", callback_data=f"ar:r:{rid}")
    b.adjust(2)
    await callback.message.edit_text(
        f"🗑 Удалить правило <b>{_esc(kws) or '—'}</b>?", reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("ar:rdelok:"))
async def ar_rule_del_ok(callback: CallbackQuery, state: FSMContext) -> None:
    rid = callback.data[len("ar:rdelok:"):]
    s, conf = _conf(callback.from_user.id)
    conf["rules"] = [r for r in (conf.get("rules") or []) if r.get("id") != rid]
    _save(callback.from_user.id, s)
    await callback.answer("🗑 Удалено")
    await ar_rules(callback, state)


# ─────────────────────────── добавление / правка ───────────────────────────

def _cancel(back: str = "ar:rules") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data=back)
    return b.as_markup()


_KW_HELP = ("Через запятую — слова или куски фразы, которые бот будет искать "
            "в сообщении покупателя.\n\n"
            "Пример: <code>где ключ, как получить, не вижу товар</code>\n\n"
            "Регистр, «ё» и знаки препинания не важны.")


@router.callback_query(F.data == "ar:radd")
async def ar_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AutoReplyState.kw)
    await callback.message.edit_text(
        f"➕ <b>Новое правило</b>\n\nШаг 1 из 2. {_KW_HELP}",
        reply_markup=_cancel())
    await callback.answer()


@router.message(AutoReplyState.kw)
async def ar_add_kw(message: Message, state: FSMContext) -> None:
    words = ar.parse_keywords(message.text or "")
    if not words:
        await message.answer("❌ Не разобрал слова. Пример: "
                             "<code>где ключ, как получить</code>")
        return
    await state.update_data(kw=words)
    await state.set_state(AutoReplyState.text)
    await message.answer(
        f"🔑 Слова: <b>{_esc(', '.join(words))}</b>\n\n"
        f"Шаг 2 из 2. Напишите ответ покупателю.\n\n"
        f"Можно подставлять: <code>{ar.HINT}</code>",
        reply_markup=_cancel())


@router.message(AutoReplyState.text)
async def ar_add_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    words = data.get("kw") or []
    body = (message.text or "").strip()
    if not body:
        await message.answer("❌ Ответ не может быть пустым.")
        return
    await state.clear()
    s, conf = _conf(message.from_user.id)
    conf.setdefault("rules", []).append(ar.new_rule(words, body))
    conf["enabled"] = True      # правило без включённых автоответов бесполезно
    _save(message.from_user.id, s)
    await message.answer(
        f"✅ Правило добавлено и автоответы включены.\n\n"
        f"🔑 <b>{_esc(', '.join(words))}</b>\n"
        f"<blockquote>{_esc(body)}</blockquote>")
    await message.answer(_ar_text(conf), reply_markup=_ar_kb(conf))


@router.callback_query(F.data.startswith("ar:rkw:"))
async def ar_edit_kw(callback: CallbackQuery, state: FSMContext) -> None:
    rid = callback.data[len("ar:rkw:"):]
    _, conf = _conf(callback.from_user.id)
    rule = ar.find_rule(conf, rid) or {}
    await state.set_state(AutoReplyState.edit_kw)
    await state.update_data(rid=rid)
    cur = ", ".join(str(k) for k in (rule.get("keywords") or [])) or "—"
    await callback.message.edit_text(
        f"🔑 <b>Слова правила</b>\n\nСейчас: <b>{_esc(cur)}</b>\n\n{_KW_HELP}",
        reply_markup=_cancel(f"ar:r:{rid}"))
    await callback.answer()


@router.message(AutoReplyState.edit_kw)
async def ar_edit_kw_save(message: Message, state: FSMContext) -> None:
    words = ar.parse_keywords(message.text or "")
    if not words:
        await message.answer("❌ Не разобрал слова. Пример: "
                             "<code>где ключ, как получить</code>")
        return
    data = await state.get_data()
    await state.clear()
    s, conf = _conf(message.from_user.id)
    rule = ar.find_rule(conf, data.get("rid", ""))
    if not rule:
        await message.answer("❌ Правило не найдено")
        return
    rule["keywords"] = words
    _save(message.from_user.id, s)
    await message.answer(f"✅ Слова: <b>{_esc(', '.join(words))}</b>")
    await message.answer(_rules_text(conf), reply_markup=_rules_kb(conf))


@router.callback_query(F.data.startswith("ar:rtx:"))
async def ar_edit_text(callback: CallbackQuery, state: FSMContext) -> None:
    rid = callback.data[len("ar:rtx:"):]
    _, conf = _conf(callback.from_user.id)
    rule = ar.find_rule(conf, rid) or {}
    await state.set_state(AutoReplyState.edit_text)
    await state.update_data(rid=rid)
    await callback.message.edit_text(
        f"✏️ <b>Текст ответа</b>\n\n"
        f"<blockquote>{_esc(rule.get('text', ''))}</blockquote>\n"
        f"Напишите новый. Подстановки: <code>{ar.HINT}</code>",
        reply_markup=_cancel(f"ar:r:{rid}"))
    await callback.answer()


@router.message(AutoReplyState.edit_text)
async def ar_edit_text_save(message: Message, state: FSMContext) -> None:
    body = (message.text or "").strip()
    if not body:
        await message.answer("❌ Ответ не может быть пустым.")
        return
    data = await state.get_data()
    await state.clear()
    s, conf = _conf(message.from_user.id)
    rule = ar.find_rule(conf, data.get("rid", ""))
    if not rule:
        await message.answer("❌ Правило не найдено")
        return
    rule["text"] = body
    _save(message.from_user.id, s)
    await message.answer("✅ Текст сохранён")
    await message.answer(_rules_text(conf), reply_markup=_rules_kb(conf))


# ─────────────────────────── запасной ответ ───────────────────────────

@router.callback_query(F.data == "ar:t:fb")
async def ar_fb_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    s, conf = _conf(callback.from_user.id)
    fb = conf.setdefault("fallback", {})
    fb["on"] = not fb.get("on", False)
    if fb["on"] and not (fb.get("text") or "").strip():
        fb["text"] = ar.DEFAULTS["fallback"]["text"]
    _save(callback.from_user.id, s)
    await callback.answer("🟢 Включён" if fb["on"] else "🔴 Выключен")
    await ar_rules(callback, state)


@router.callback_query(F.data == "ar:fbtx")
async def ar_fb_text(callback: CallbackQuery, state: FSMContext) -> None:
    _, conf = _conf(callback.from_user.id)
    await state.set_state(AutoReplyState.fallback)
    cur = (conf.get("fallback") or {}).get("text", "")
    await callback.message.edit_text(
        f"💬 <b>Запасной ответ</b>\n\nОтправляется, когда ни одно правило не "
        f"совпало.\n\nСейчас:\n<blockquote>{_esc(cur)}</blockquote>\n"
        f"Напишите новый. Подстановки: <code>{ar.HINT}</code>",
        reply_markup=_cancel())
    await callback.answer()


@router.message(AutoReplyState.fallback)
async def ar_fb_save(message: Message, state: FSMContext) -> None:
    body = (message.text or "").strip()
    if not body:
        await message.answer("❌ Текст не может быть пустым.")
        return
    await state.clear()
    s, conf = _conf(message.from_user.id)
    conf["fallback"] = {"on": True, "text": body}
    _save(message.from_user.id, s)
    await message.answer("✅ Запасной ответ сохранён и включён")
    await message.answer(_rules_text(conf), reply_markup=_rules_kb(conf))


# ─────────────────────────── готовые наборы ───────────────────────────

@router.callback_query(F.data == "ar:tpl")
async def ar_templates(callback: CallbackQuery) -> None:
    lines = ["✨ <b>Готовые наборы</b>", "",
             "Одно нажатие — и автоответы начинают работать. "
             "Тексты потом можно поправить."]
    b = InlineKeyboardBuilder()
    for key, tpl in ar.TEMPLATES.items():
        lines.append("")
        lines.append(f"<b>{tpl['name']}</b> — {tpl['about']}")
        b.button(text=f"➕ {tpl['name']}", callback_data=f"ar:tpl:{key}")
    b.button(text="⬅️ Автоответы", callback_data="ar:menu")
    b.adjust(1)
    await callback.message.edit_text("\n".join(lines), reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("ar:tpl:"))
async def ar_template_apply(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data[len("ar:tpl:"):]
    s, conf = _conf(callback.from_user.id)
    _added, what = ar.apply_template(conf, key)
    _save(callback.from_user.id, s)
    await callback.answer(f"✅ {what}", show_alert=True)
    await state.clear()
    await callback.message.edit_text(_ar_text(conf), reply_markup=_ar_kb(conf))


# ─────────────────────────── когда отвечать ───────────────────────────

def _opts_text(conf: dict) -> str:
    return "\n".join([
        "⚙️ <b>Когда отвечать</b>", "",
        f"🕐 Режим: <b>{'только вне рабочего времени' if conf.get('quiet_only') else 'круглосуточно'}</b>",
        f"🌙 Нерабочее время: <b>{int(conf.get('from_hour', 22)):02d}:00 — "
        f"{int(conf.get('to_hour', 9)):02d}:00</b>",
        "",
        f"⏸ Пауза между ответами в один чат: <b>{int(conf.get('cooldown_min', 30))} мин</b>",
        f"🔢 Не больше <b>{int(conf.get('max_per_order', 3))}</b> автоответов на заказ",
        "",
        f"🚨 Отвечать на жалобы: {_sw(bool(conf.get('reply_to_complaints')))}",
        "<i>По умолчанию выключено: на «верните деньги» шаблон только злит, "
        "тут нужен живой ответ.</i>",
    ])


def _opts_kb(conf: dict) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=("🌙 Только вне рабочего времени" if not conf.get("quiet_only")
                   else "🕐 Отвечать круглосуточно"), callback_data="ar:t:quiet")
    b.button(text="🌙 Часы", callback_data="ar:set:hours")
    b.button(text="⏸ Пауза", callback_data="ar:set:cd")
    b.button(text="🔢 Лимит на заказ", callback_data="ar:set:cap")
    b.button(text=f"{_sw(bool(conf.get('reply_to_complaints')))} Жалобы",
             callback_data="ar:t:compl")
    b.button(text="⬅️ Автоответы", callback_data="ar:menu")
    b.adjust(1, 2, 1, 1, 1)
    return b.as_markup()


@router.callback_query(F.data == "ar:opts")
async def ar_opts(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    _, conf = _conf(callback.from_user.id)
    await callback.message.edit_text(_opts_text(conf), reply_markup=_opts_kb(conf))
    await callback.answer()


@router.callback_query(F.data == "ar:t:quiet")
async def ar_toggle_quiet(callback: CallbackQuery, state: FSMContext) -> None:
    s, conf = _conf(callback.from_user.id)
    conf["quiet_only"] = not conf.get("quiet_only", False)
    _save(callback.from_user.id, s)
    await ar_opts(callback, state)


@router.callback_query(F.data == "ar:t:compl")
async def ar_toggle_compl(callback: CallbackQuery, state: FSMContext) -> None:
    s, conf = _conf(callback.from_user.id)
    conf["reply_to_complaints"] = not conf.get("reply_to_complaints", False)
    _save(callback.from_user.id, s)
    await ar_opts(callback, state)


@router.callback_query(F.data == "ar:set:cd")
async def ar_set_cd(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AutoReplyState.cooldown)
    cur = int(get_settings(callback.from_user.id).get("autoreplies", {})
              .get("cooldown_min", 30))
    await callback.message.edit_text(
        f"⏸ <b>Пауза между автоответами</b>\n\nСейчас: <b>{cur} мин</b>\n\n"
        "Сколько минут молчать после ответа в этот же чат, чтобы бот не "
        "отвечал на каждое «ок». Введите число (0–1440):",
        reply_markup=_cancel("ar:opts"))
    await callback.answer()


@router.message(AutoReplyState.cooldown)
async def ar_save_cd(message: Message, state: FSMContext) -> None:
    try:
        value = int((message.text or "").strip())
        if not 0 <= value <= 1440:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите число от 0 до 1440")
        return
    await state.clear()
    s, conf = _conf(message.from_user.id)
    conf["cooldown_min"] = value
    _save(message.from_user.id, s)
    await message.answer(f"✅ Пауза: <b>{value} мин</b>")
    await message.answer(_opts_text(conf), reply_markup=_opts_kb(conf))


@router.callback_query(F.data == "ar:set:cap")
async def ar_set_cap(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AutoReplyState.cap)
    cur = int(get_settings(callback.from_user.id).get("autoreplies", {})
              .get("max_per_order", 3))
    await callback.message.edit_text(
        f"🔢 <b>Лимит автоответов на заказ</b>\n\nСейчас: <b>{cur}</b>\n\n"
        "После этого бот замолкает и ждёт вас — переписку должен вести "
        "человек. Введите число (0 — без лимита):",
        reply_markup=_cancel("ar:opts"))
    await callback.answer()


@router.message(AutoReplyState.cap)
async def ar_save_cap(message: Message, state: FSMContext) -> None:
    try:
        value = int((message.text or "").strip())
        if not 0 <= value <= 50:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите число от 0 до 50")
        return
    await state.clear()
    s, conf = _conf(message.from_user.id)
    conf["max_per_order"] = value
    _save(message.from_user.id, s)
    await message.answer(f"✅ Лимит: <b>{value or 'без лимита'}</b>")
    await message.answer(_opts_text(conf), reply_markup=_opts_kb(conf))


@router.callback_query(F.data == "ar:set:hours")
async def ar_set_hours(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AutoReplyState.hours)
    conf = get_settings(callback.from_user.id).get("autoreplies", {})
    await callback.message.edit_text(
        f"🌙 <b>Нерабочее время</b>\n\nСейчас: "
        f"<b>{int(conf.get('from_hour', 22)):02d}:00 — "
        f"{int(conf.get('to_hour', 9)):02d}:00</b>\n\n"
        "Введите два часа через дефис, например <code>22-9</code>:",
        reply_markup=_cancel("ar:opts"))
    await callback.answer()


@router.message(AutoReplyState.hours)
async def ar_save_hours(message: Message, state: FSMContext) -> None:
    import re as _re
    m = _re.match(r"^\s*(\d{1,2})\s*[-–—:]\s*(\d{1,2})\s*$", message.text or "")
    if not m or not all(0 <= int(g) <= 23 for g in m.groups()):
        await message.answer("❌ Формат: <code>22-9</code> (часы от 0 до 23)")
        return
    await state.clear()
    s, conf = _conf(message.from_user.id)
    conf["from_hour"], conf["to_hour"] = int(m.group(1)), int(m.group(2))
    _save(message.from_user.id, s)
    await message.answer(f"✅ Нерабочее время: <b>{conf['from_hour']:02d}:00 — "
                         f"{conf['to_hour']:02d}:00</b>")
    await message.answer(_opts_text(conf), reply_markup=_opts_kb(conf))


# ─────────────────────────────── проверка ───────────────────────────────

@router.callback_query(F.data == "ar:test")
async def ar_test(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AutoReplyState.test)
    await callback.message.edit_text(
        "🧪 <b>Проверка</b>\n\nНапишите сообщение так, как его написал бы "
        "покупатель. Покажу, какое правило сработает, что именно уйдёт в чат "
        "и отправится ли вообще.\n\n"
        "<i>Ничего никому не отправляется — это черновой прогон.</i>",
        reply_markup=_cancel("ar:menu"))
    await callback.answer()


@router.message(AutoReplyState.test)
async def ar_test_run(message: Message, state: FSMContext) -> None:
    """Прогнать сообщение через тот же подбор, что работает в фоне.

    Это и есть ответ на «а оно вообще работает»: видно правило, видно готовый
    текст с подставленными данными и видно, пропустит ли отправку пауза или
    режим «только ночью».
    """
    s, conf = _conf(message.from_user.id)
    probe = message.text or ""

    rule, matched = ar.pick(conf, probe)
    lines = [f"🧪 <b>Проверка</b>\n\n<blockquote>{_esc(probe[:200])}</blockquote>"]

    if not rule:
        lines.append("\n🔇 <b>Ответа нет</b> — ни одно слово не совпало, "
                     "запасной ответ выключен.")
        lines.append("Добавьте правило или включите запасной ответ.")
    else:
        # Данные берём из последнего реального заказа — так видно, как
        # подстановки выглядят на живом тексте, а не на «{товар}».
        details = {}
        known = s.get("known_order_details", {})
        oid = ""
        if known:
            oid, details = sorted(
                known.items(), key=lambda kv: float(kv[1].get("seen_at", 0) or 0),
                reverse=True)[0]
        body = ar.render(rule.get("text", ""),
                         ar.context(details, oid, s.get("shop_name", "")))
        where = (f"правило «{_esc(matched)}»" if matched else "запасной ответ")
        lines.append(f"\n✅ Сработает: <b>{where}</b>")
        lines.append(f"\n💬 Уйдёт в чат:\n<blockquote>{_esc(body)}</blockquote>")
        if details:
            lines.append(f"<i>Подставлены данные заказа #{_esc(oid)}.</i>")

        allowed, why = ar.gate(conf, "тест-чат")
        lines.append(f"\n{'🟢' if allowed else '🔴'} Отправка: {_esc(why)}")

    b = InlineKeyboardBuilder()
    b.button(text="🧪 Ещё проверка", callback_data="ar:test")
    b.button(text="📝 Правила", callback_data="ar:rules")
    b.button(text="⬅️ Автоответы", callback_data="ar:menu")
    b.adjust(2, 1)
    await state.clear()
    await message.answer("\n".join(lines), reply_markup=b.as_markup())


# ─────────────────────────────── журнал ───────────────────────────────

@router.callback_query(F.data == "ar:why")
async def ar_why(callback: CallbackQuery) -> None:
    """Почему бот молчит — по состоянию самого механизма, а не по догадкам.

    «Автоответы не работают» может значить что угодно: выключены, нет правил,
    фоновый опрос не дошёл до чата, сработала пауза. Здесь показано, что
    происходит на самом деле.
    """
    await callback.answer()
    s = get_settings(callback.from_user.id)
    conf = ar.cfg(s)
    poll = s.get("_chat_poll") or {}

    lines = ["🔍 <b>Почему бот молчит</b>", ""]

    live = [r for r in (conf.get("rules") or []) if r.get("on", True)
            and (r.get("text") or "").strip()]
    fb_on = bool((conf.get("fallback") or {}).get("on"))
    lines.append(f"{_sw(conf.get('enabled'))} Автоответы "
                 f"{'включены' if conf.get('enabled') else 'выключены'}")
    lines.append(f"{_sw(bool(live) or fb_on)} Правил: <b>{len(live)}</b>"
                 + (", есть запасной ответ" if fb_on else ""))

    # Ночной режим показываем здесь, а не только в «промолчал». Иначе он
    # виден лишь постфактум: включён набором «🌙 Ночной ответ», молчит весь
    # день, и понять это можно только поймав пропущенное сообщение.
    if conf.get("quiet_only"):
        window = (f"{int(conf.get('from_hour', 22)):02d}:00—"
                  f"{int(conf.get('to_hour', 9)):02d}:00")
        if ar.in_quiet_window(conf):
            lines.append(f"🟢 Режим: только {window} — сейчас это время")
        else:
            lines.append(f"🟡 Режим: только {window} — <b>сейчас бот молчит "
                         f"по расписанию</b>")

    ts = float(poll.get("ts") or 0)
    if ts:
        ago = int((datetime.now().timestamp() - ts) / 60)
        lines.append(f"{_sw(ago < 5)} Чаты читались "
                     + ("только что" if ago < 1 else f"{ago} мин назад"))
        watched = int(poll.get("chats", 0) or 0)
        total = int(poll.get("orders", 0) or 0)
        lines.append(f"   под наблюдением: <b>{watched}</b> из {total} заказов")
        if total > watched:
            # Опрашиваются самые свежие заказы. Сообщение в старом чате бот
            # не увидит вовсе — и это не «не сработало правило», а другое.
            lines.append(f"   <i>Старые {total - watched} заказов не "
                         f"опрашиваются — в них бот сообщений не увидит.</i>")
        if poll.get("new_msgs"):
            lines.append(f"   новых сообщений в прошлый заход: "
                         f"<b>{poll['new_msgs']}</b>")
        if poll.get("error"):
            lines.append(f"   ⚠️ {_esc(poll['error'])}")
    else:
        lines.append("🔴 Чаты ещё ни разу не читались")
        lines.append("   <i>Фоновый опрос запускается после /start и идёт "
                     "раз в минуту. Если так и осталось — перезапустите бота.</i>")

    skip = conf.get("last_skip") or {}
    if skip:
        when = datetime.fromtimestamp(float(skip.get("ts", 0) or 0)).strftime("%d.%m %H:%M")
        lines += ["", f"🔇 <b>Последний раз промолчал</b> ({when})",
                  f"   На сообщение: <i>{_esc(str(skip.get('text', ''))[:70])}</i>",
                  f"   Причина: <b>{_esc(skip.get('why', ''))}</b>"]

    sent = conf.get("log") or []
    if sent:
        last = sent[0]
        when = datetime.fromtimestamp(float(last.get("ts", 0) or 0)).strftime("%d.%m %H:%M")
        lines += ["", f"📜 Последняя отправка: {'✅' if last.get('ok') else '❌'} {when}"]
        if not last.get("ok"):
            why, fixable = ar.explain_error(str(last.get("err", "")))
            lines.append(f"   {_esc(why)}")
            if not fixable:
                lines.append("   <i>Это не поломка бота — писать в такой чат "
                             "маркетплейс не даёт никому.</i>")
    else:
        lines += ["", "📜 Бот пока ничего не отправлял."]

    b = InlineKeyboardBuilder()
    b.button(text="🔄 Обновить", callback_data="ar:why")
    b.button(text="🧪 Проверить правило", callback_data="ar:test")
    night = bool(conf.get("quiet_only"))
    if night:
        b.button(text="🕐 Отвечать круглосуточно", callback_data="ar:why:quiet")
    b.button(text="📜 Журнал", callback_data="ar:log")
    b.button(text="⬅️ Автоответы", callback_data="ar:menu")
    b.adjust(*((2, 1, 2) if night else (2, 2)))
    await callback.message.edit_text("\n".join(lines), reply_markup=b.as_markup())


@router.callback_query(F.data == "ar:why:quiet")
async def ar_why_quiet_off(callback: CallbackQuery) -> None:
    """Снять ночной режим прямо здесь.

    Причина молчания названа на этом экране — значит и выключаться должна
    отсюда, а не через «Автоответы → Когда отвечать → режим». Ответ на
    нажатие даёт сам `ar_why`; второй `answer()` Telegram уже не примет.
    """
    s, conf = _conf(callback.from_user.id)
    conf["quiet_only"] = False
    _save(callback.from_user.id, s)
    await ar_why(callback)


@router.callback_query(F.data == "ar:log")
async def ar_log(callback: CallbackQuery) -> None:
    _, conf = _conf(callback.from_user.id)
    entries = conf.get("log") or []
    lines = ["📜 <b>Журнал автоответов</b>", ""]
    if not entries:
        lines.append("Пока пусто — бот ещё ничего не отправлял.")
    for e in entries[:15]:
        when = datetime.fromtimestamp(float(e.get("ts", 0) or 0)).strftime("%d.%m %H:%M")
        mark = "✅" if e.get("ok") else "❌"
        tag = f" · {_esc(e.get('rule'))}" if e.get("rule") else ""
        lines.append(f"{mark} <code>{when}</code>{tag}")
        lines.append(f"   <i>{_esc(str(e.get('text', ''))[:90])}</i>")
        if not e.get("ok") and e.get("err"):
            lines.append(f"   ❌ {_esc(ar.explain_error(str(e.get('err')))[0])}")
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Обновить", callback_data="ar:log")
    b.button(text="🧹 Очистить", callback_data="ar:logclr")
    b.button(text="⬅️ Автоответы", callback_data="ar:menu")
    b.adjust(2, 1)
    await callback.message.edit_text("\n".join(lines)[:3900],
                                     reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data == "ar:logclr")
async def ar_log_clear(callback: CallbackQuery) -> None:
    s, conf = _conf(callback.from_user.id)
    conf["log"] = []
    _save(callback.from_user.id, s)
    await callback.answer("🧹 Очищено")
    await ar_log(callback)


# ─────────────────────── ответы на события заказа ───────────────────────

@router.callback_query(F.data == "ar:events")
async def ar_events(callback: CallbackQuery) -> None:
    """Автоответы, привязанные к событию заказа, а не к сообщению."""
    s = get_settings(callback.from_user.id)
    reply = s.get("auto_reply", {})
    ev = s.get("auto_events", {})
    conf_ = ev.get("on_confirmed", {})
    ref = ev.get("on_refunded", {})

    def block(title: str, node: dict) -> list[str]:
        on = bool(node.get("enabled"))
        out = [f"{_sw(on)} <b>{title}</b>"]
        if on:
            out.append(f"   <i>{_esc(str(node.get('message', ''))[:90])}</i>")
        return out

    lines = ["📦 <b>Ответы на события заказа</b>", ""]
    lines += block("Новый заказ", reply)
    lines += block("Заказ выполнен", conf_)
    lines += block("Возврат", ref)
    lines += ["", f"<i>Подстановки работают и здесь: {ar.HINT}</i>"]

    b = InlineKeyboardBuilder()
    b.button(text=f"{_sw(bool(reply.get('enabled')))} Новый заказ",
             callback_data="auto:toggle:reply")
    b.button(text="✏️", callback_data="auto:set:reply_msg")
    b.button(text=f"{_sw(bool(conf_.get('enabled')))} Выполнен",
             callback_data="auto:toggle:confirmed")
    b.button(text="✏️", callback_data="auto:set:confirmed_msg")
    b.button(text=f"{_sw(bool(ref.get('enabled')))} Возврат",
             callback_data="auto:toggle:refunded")
    b.button(text="✏️", callback_data="auto:set:refunded_msg")
    b.button(text="⬅️ Автоответы", callback_data="ar:menu")
    b.adjust(2, 2, 2, 1)
    await callback.message.edit_text("\n".join(lines), reply_markup=b.as_markup())
    await callback.answer()
