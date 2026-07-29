"""Handler for creating new product listings via the API."""
from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from api.yoomarket import YooMarketAPI
from keyboards.main import back_keyboard
from storage import get_settings, save_settings

router = Router()
logger = logging.getLogger(__name__)


class CreateAdState(StatesGroup):
    title = State()
    price = State()
    description = State()
    quantity = State()
    category = State()
    photo = State()
    confirm = State()
    panel_select = State()  # choosing category/subcategory/type from panel options


def _cancel_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="menu:ads")
    return b.as_markup()


def _preview(data: dict) -> str:
    title = data.get("title", "—")
    price = data.get("price", "—")
    description = data.get("description", "—")
    quantity = data.get("quantity", 1)
    category = data.get("category", "")
    lines = [
        "📦 <b>Новый товар — предпросмотр</b>\n",
        f"📝 Название: <b>{title}</b>",
        f"💰 Цена: <b>{price} ₽</b>",
        f"🔢 Количество: <b>{quantity}</b>",
    ]
    if category:
        lines.append(f"🏷 Категория: <b>{category}</b>")
    lines.append(f"📷 Фото: <b>{'есть ✅' if data.get('photo_path') else 'нет'}</b>")
    lines.append(f"\n📄 Описание:\n{description}")
    return "\n".join(lines)


def _confirm_kb(has_photo: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Создать товар", callback_data="create_ad:submit")
    b.button(text="💾 Сохранить как шаблон", callback_data="create_ad:save_template")
    photo_label = "📷 Заменить фото" if has_photo else "📷 Добавить фото"
    b.button(text=photo_label, callback_data="create_ad:edit:photo")
    b.button(text="✏️ Изменить название", callback_data="create_ad:edit:title")
    b.button(text="✏️ Изменить цену", callback_data="create_ad:edit:price")
    b.button(text="✏️ Изменить описание", callback_data="create_ad:edit:description")
    b.button(text="❌ Отмена", callback_data="menu:ads")
    b.adjust(1)
    return b.as_markup()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "create_ad:start")
async def create_ad_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(CreateAdState.title)
    s = get_settings(callback.from_user.id)
    templates = s.get("ad_templates", [])
    b = InlineKeyboardBuilder()
    if templates:
        b.button(text=f"📋 Использовать шаблон ({len(templates)})", callback_data="create_ad:templates_list")
    b.button(text="❌ Отмена", callback_data="menu:ads")
    b.adjust(1)
    await callback.message.edit_text(
        "➕ <b>Добавить товар</b>\n\n"
        "<b>Шаг 1/4</b> — Введи название товара:",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Step 1: Title
# ---------------------------------------------------------------------------

@router.message(CreateAdState.title)
async def ad_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not title:
        await message.answer("❌ Название не может быть пустым:")
        return
    if len(title) > 100:
        await message.answer("❌ Название слишком длинное (макс. 100 символов):")
        return
    data = await state.get_data()
    await state.update_data(title=title)
    if data.get("editing"):
        await state.update_data(editing=False)
        await _show_preview(message, state, edit=False)
        return
    await state.set_state(CreateAdState.price)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="menu:ads")
    await message.answer(
        f"✅ Название: <b>{title}</b>\n\n<b>Шаг 2/4</b> — Введи цену (₽):",
        reply_markup=b.as_markup(),
    )


# ---------------------------------------------------------------------------
# Step 2: Price
# ---------------------------------------------------------------------------

@router.message(CreateAdState.price)
async def ad_price(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().replace(" ", "").replace(",", ".")
    try:
        price = int(float(raw))
        if price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введи цену числом, например: <b>500</b>")
        return
    data = await state.get_data()
    await state.update_data(price=price)
    if data.get("editing"):
        await state.update_data(editing=False)
        await _show_preview(message, state, edit=False)
        return
    await state.set_state(CreateAdState.description)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="menu:ads")
    await message.answer(
        f"✅ Цена: <b>{price} ₽</b>\n\n<b>Шаг 3/4</b> — Введи описание товара:",
        reply_markup=b.as_markup(),
    )


# ---------------------------------------------------------------------------
# Step 3: Description
# ---------------------------------------------------------------------------

@router.message(CreateAdState.description)
async def ad_description(message: Message, state: FSMContext) -> None:
    desc = (message.text or "").strip()
    if not desc:
        await message.answer("❌ Описание не может быть пустым:")
        return
    data = await state.get_data()
    await state.update_data(description=desc)
    if data.get("editing"):
        await state.update_data(editing=False)
        await _show_preview(message, state, edit=False)
        return
    await state.set_state(CreateAdState.quantity)
    b = InlineKeyboardBuilder()
    b.button(text="1️⃣ Пропустить (кол-во = 1)", callback_data="create_ad:qty:1")
    b.button(text="❌ Отмена", callback_data="menu:ads")
    b.adjust(1)
    await message.answer(
        "<b>Шаг 4/4</b> — Введи количество товаров или пропусти:",
        reply_markup=b.as_markup(),
    )


# ---------------------------------------------------------------------------
# Step 4: Quantity
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "create_ad:qty:1")
async def ad_qty_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(quantity=1)
    await _ask_photo(callback.message, state, edit=True)
    await callback.answer()


@router.message(CreateAdState.quantity)
async def ad_quantity(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    try:
        qty = int(raw)
        if qty <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введи целое число, например: <b>10</b>")
        return
    await state.update_data(quantity=qty)
    await _ask_photo(message, state, edit=False)


# ---------------------------------------------------------------------------
# Step 5: Photo (панель требует хотя бы одну картинку)
# ---------------------------------------------------------------------------

async def _ask_photo(msg, state: FSMContext, edit: bool) -> None:
    await state.set_state(CreateAdState.photo)
    b = InlineKeyboardBuilder()
    b.button(text="⏭ Без фото", callback_data="create_ad:photo_skip")
    b.button(text="❌ Отмена", callback_data="menu:ads")
    b.adjust(1)
    text = (
        "<b>Шаг 5/5</b> — Отправь <b>фото товара</b> 📷\n\n"
        "<i>Панель YooMarket требует картинку для объявления.\n"
        "Без фото создание может не пройти.</i>"
    )
    if edit:
        await msg.edit_text(text, reply_markup=b.as_markup())
    else:
        await msg.answer(text, reply_markup=b.as_markup())


@router.message(CreateAdState.photo, F.photo)
async def ad_photo(message: Message, state: FSMContext) -> None:
    import os
    from storage import _DATA_DIR
    photo = message.photo[-1]  # самое большое разрешение
    tmp_dir = os.path.join(_DATA_DIR, "photos")
    os.makedirs(tmp_dir, exist_ok=True)
    path = os.path.join(tmp_dir, f"{message.from_user.id}_{photo.file_unique_id}.jpg")
    try:
        await message.bot.download(photo, destination=path)
    except Exception as e:
        await message.answer(f"❌ Не удалось скачать фото: {str(e)[:100]}\nПопробуйте ещё раз.")
        return
    await state.update_data(photo_path=path)
    await _show_preview(message, state, edit=False)


@router.message(CreateAdState.photo)
async def ad_photo_not_photo(message: Message) -> None:
    await message.answer("📷 Отправьте фото (как изображение) или нажмите «Без фото».")


@router.callback_query(F.data == "create_ad:photo_skip")
async def ad_photo_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(photo_path=None)
    await _show_preview(callback.message, state, edit=True)
    await callback.answer()


async def _show_preview(msg, state: FSMContext, edit: bool) -> None:
    data = await state.get_data()
    await state.set_state(CreateAdState.confirm)
    text = _preview(data)
    kb = _confirm_kb(has_photo=bool(data.get("photo_path")))
    if edit:
        await msg.edit_text(text, reply_markup=kb)
    else:
        await msg.answer(text, reply_markup=kb)


# ---------------------------------------------------------------------------
# Edit fields from preview
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("create_ad:edit:"))
async def edit_field(callback: CallbackQuery, state: FSMContext) -> None:
    field = callback.data.split(":")[-1]
    if field == "photo":
        await _ask_photo(callback.message, state, edit=True)
        await callback.answer()
        return
    prompts = {
        "title": "Введи новое название:",
        "price": "Введи новую цену (₽):",
        "description": "Введи новое описание:",
    }
    states = {
        "title": CreateAdState.title,
        "price": CreateAdState.price,
        "description": CreateAdState.description,
    }
    if field not in prompts:
        await callback.answer()
        return
    # editing=True → step handler returns to preview instead of walking the wizard
    await state.update_data(editing=True)
    await state.set_state(states[field])
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="create_ad:back_preview")
    await callback.message.edit_text(prompts[field], reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data == "create_ad:back_preview")
async def back_to_preview(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(editing=False)
    await _show_preview(callback.message, state, edit=True)
    await callback.answer()


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "create_ad:submit")
async def submit_ad(callback: CallbackQuery, state: FSMContext, api: YooMarketAPI) -> None:
    data = await state.get_data()
    uid = callback.from_user.id

    title = data.get("title", "")
    price = data.get("price", 0)
    description = data.get("description", "")
    quantity = data.get("quantity", 1)
    category = data.get("category", "")

    if not title or not price:
        await callback.answer("❌ Не хватает данных", show_alert=True)
        return

    values = {
        "title": title, "price": price, "description": description,
        "quantity": quantity, "category": category,
        "photo_path": data.get("photo_path"),
    }

    await callback.message.edit_text("⏳ Создаю товар...")

    # ── Шаг 1: Integration API ──────────────────────────────────────────────
    if api:
        try:
            result = await api.create_ad(
                title=title, price=price, description=description,
                quantity=quantity, category=category,
            )
            ad_id = result.get("id") or (result.get("data") or {}).get("id") or "—"
            await state.clear()
            b = InlineKeyboardBuilder()
            b.button(text="➕ Добавить ещё", callback_data="create_ad:start")
            b.button(text="📦 Мои товары", callback_data="menu:ads")
            b.adjust(1)
            await callback.message.edit_text(
                f"✅ <b>Товар создан!</b>\n\n"
                f"📝 {title}\n💰 {price} ₽\n🆔 ID: {ad_id}",
                reply_markup=b.as_markup(),
            )
            await callback.answer()
            return
        except Exception:
            pass  # Fall through to panel

    # ── Шаг 2: Панель — проверяем куки ──────────────────────────────────────
    from storage import get_panel_creds

    creds = get_panel_creds(uid)
    if not creds or not creds.get("cookies"):
        await state.clear()
        b = InlineKeyboardBuilder()
        b.button(text="🌐 Войти в панель", callback_data="panel:sms_start")
        b.button(text="⬅️ Назад", callback_data="menu:ads")
        b.adjust(1)
        await callback.message.edit_text(
            "❌ <b>Не удалось создать товар</b>\n\n"
            "Integration API не поддерживает создание товаров.\n\n"
            "💡 Войдите в <b>Панель продавца</b> через email — бот будет создавать товары через неё.\n\n"
            "<b>Настройки → Панель продавца → Войти через email</b>",
            reply_markup=b.as_markup(),
        )
        await callback.answer()
        return

    # ── Шаг 3: Загружаем форму панели, спрашиваем категорию/тип кнопками ────
    from automation.panel import panel_get_item_form_sync

    await callback.message.edit_text("⏳ Загружаю форму панели… [v5]")
    loop = asyncio.get_event_loop()
    try:
        form_ok, form = await asyncio.wait_for(
            loop.run_in_executor(None, panel_get_item_form_sync, creds["cookies"]),
            timeout=30,
        )
    except Exception as e:
        form_ok, form = False, str(e)[:80]

    if form_ok and isinstance(form, dict):
        by_attr = {f["attribute"]: f for f in form["fields"]}
        # Селекты, которые панель требует: спрашиваем в порядке зависимости
        queue = [a for a in ("category", "subcategory", "type") if a in by_attr]
        queue += [
            f["attribute"] for f in form["fields"]
            if f.get("required") and f.get("options") and f["attribute"] not in queue
            and f["attribute"] not in ("title", "price", "content")
        ]
        if queue:
            await state.set_state(CreateAdState.panel_select)
            await state.update_data(
                pending=values,
                form_resource=form["resource"],
                form_fields=form["fields"],
                chosen={},
                select_queue=queue,
            )
            await _ask_next_select(callback.message, state, uid)
            await callback.answer()
            return

    # Форму не получили — пробуем создать напрямую (сервер сам скажет, чего не хватает)
    await state.clear()
    await _panel_create_and_report(callback.message, uid, values, extra=None)
    await callback.answer()


async def _ask_next_select(msg, state: FSMContext, uid: int) -> None:
    """Ask the user to pick the next required select option, or create the product."""
    from storage import get_panel_creds
    from automation.panel import panel_sync_field_options_sync

    data = await state.get_data()
    queue: list = data.get("select_queue") or []
    chosen: dict = data.get("chosen") or {}
    fields = {f["attribute"]: f for f in (data.get("form_fields") or [])}

    if not queue:
        values = data.get("pending") or {}
        await state.clear()
        await _panel_create_and_report(msg, uid, values, extra=chosen)
        return

    attr = queue[0]
    f = fields.get(attr) or {"attribute": attr, "label": attr, "options": []}
    options = f.get("options") or []
    trace = ""

    if not options:
        # BelongsTo/зависимый селект — тянем варианты отдельным запросом
        creds = get_panel_creds(uid)
        loop = asyncio.get_event_loop()
        try:
            options, trace = await asyncio.wait_for(
                loop.run_in_executor(
                    None, panel_sync_field_options_sync,
                    creds["cookies"], data.get("form_resource", "items"), attr, chosen,
                ),
                timeout=25,
            )
        except Exception as e:
            options, trace = [], f"ошибка: {str(e)[:60]}"

    if not options:
        # Обязательное поле без вариантов — это тупик, показываем диагностику
        if f.get("required") or attr in ("category", "subcategory", "type"):
            await state.clear()
            b = InlineKeyboardBuilder()
            b.button(text="🌐 Создать вручную в панели",
                     url="https://panel.yoomarket.net/goods/create")
            b.button(text="⬅️ Назад", callback_data="menu:ads")
            b.adjust(1)
            await msg.edit_text(
                f"❌ <b>Не удалось получить варианты поля «{f.get('label') or attr}»</b>\n\n"
                f"component: <code>{f.get('component','?')}</code>\n"
                f"связь: <code>{f.get('relationship','—')}</code>\n"
                f"Запросы:\n<code>{trace[:400]}</code>\n\n"
                f"Пришлите этот текст разработчику.",
                reply_markup=b.as_markup(),
            )
            return
        # Необязательное — пропускаем
        await state.update_data(select_queue=queue[1:])
        await _ask_next_select(msg, state, uid)
        return

    options = options[:500]
    label = f.get("label") or attr
    await state.update_data(
        current_attr=attr, current_label=label,
        current_options=options, current_view=options, current_page=0,
        select_queue=queue[1:],
    )
    await _render_select(msg, state, edit=True)


_PER_PAGE = 16


async def _render_select(msg, state: FSMContext, edit: bool = True) -> None:
    """Render the current select page with pagination + search hint."""
    data = await state.get_data()
    label = data.get("current_label") or data.get("current_attr") or ""
    view: list = data.get("current_view") or []
    page = int(data.get("current_page") or 0)

    total_pages = max(1, (len(view) + _PER_PAGE - 1) // _PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    chunk = view[page * _PER_PAGE:(page + 1) * _PER_PAGE]

    b = InlineKeyboardBuilder()
    for i, o in enumerate(chunk, start=page * _PER_PAGE):
        b.button(text=str(o.get("label", ""))[:32], callback_data=f"cadopt:{i}")
    b.adjust(2)
    if total_pages > 1:
        nav = []
        from aiogram.types import InlineKeyboardButton
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=f"cadpg:{page-1}"))
        nav.append(InlineKeyboardButton(
            text=f"{page+1}/{total_pages}", callback_data="cadpg:noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=f"cadpg:{page+1}"))
        b.row(*nav)
    from aiogram.types import InlineKeyboardButton
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data="menu:ads"))

    text = (
        f"📋 Выберите <b>{label}</b> (всего: {len(view)}):\n"
        f"<i>Не нашли нужное? Напишите название сообщением — я поищу.</i>"
    )
    sent = None
    try:
        if edit:
            sent = await msg.edit_text(text, reply_markup=b.as_markup())
        else:
            sent = await msg.answer(text, reply_markup=b.as_markup())
    except Exception:
        try:
            sent = await msg.answer(text, reply_markup=b.as_markup())
        except Exception:
            sent = None
    # Remember the live select message so search can edit it in place
    if sent is not None and getattr(sent, "message_id", None):
        await state.update_data(
            select_msg_id=sent.message_id, select_chat_id=sent.chat.id)


@router.callback_query(F.data.startswith("cadpg:"))
async def select_page(callback: CallbackQuery, state: FSMContext) -> None:
    arg = callback.data.split(":")[1]
    if arg == "noop":
        await callback.answer()
        return
    try:
        page = int(arg)
    except ValueError:
        await callback.answer()
        return
    await state.update_data(current_page=page)
    await _render_select(callback.message, state, edit=True)
    await callback.answer()


@router.message(CreateAdState.panel_select)
async def select_search(message: Message, state: FSMContext) -> None:
    """Free-text search over the current select's options (local + remote)."""
    from storage import get_panel_creds
    from automation.panel import panel_sync_field_options_sync

    q = (message.text or "").strip()
    data = await state.get_data()
    attr = data.get("current_attr")
    if not attr or not q:
        return

    base: list = data.get("current_options") or []
    ql = q.lower()
    filtered = [o for o in base if ql in str(o.get("label", "")).lower()]

    if not filtered:
        # Не нашли локально — спрашиваем сервер с параметром search
        creds = get_panel_creds(message.from_user.id)
        loop = asyncio.get_event_loop()
        try:
            remote, _tr = await asyncio.wait_for(
                loop.run_in_executor(
                    None, panel_sync_field_options_sync,
                    creds["cookies"], data.get("form_resource", "items"),
                    attr, data.get("chosen") or {}, q,
                ),
                timeout=25,
            )
        except Exception:
            remote = []
        if remote:
            # Добавляем новые варианты к базе, чтобы индексы кнопок работали
            known = {str(o.get("value")) for o in base}
            fresh = [o for o in remote if str(o.get("value")) not in known]
            base = base + fresh
            filtered = [o for o in base if ql in str(o.get("label", "")).lower()] or remote

    if not filtered:
        await message.answer(f"🔍 По запросу «{q}» ничего не найдено. Попробуйте иначе.")
        return

    await state.update_data(
        current_options=base, current_view=filtered, current_page=0,
    )
    # Remove the buyer's search text to keep the chat clean
    try:
        await message.delete()
    except Exception:
        pass
    # Edit the SAME select message in place so its button indices always match
    # current_view (a fresh message would leave a stale keyboard behind).
    sel_id = data.get("select_msg_id")
    sel_chat = data.get("select_chat_id")
    if sel_id and sel_chat:
        class _Editable:
            message_id = sel_id
            chat = type("C", (), {"id": sel_chat})()
            def __init__(self, bot):
                self._bot = bot
            async def edit_text(self, text, reply_markup=None, **kw):
                return await self._bot.edit_message_text(
                    text=text, chat_id=sel_chat, message_id=sel_id,
                    reply_markup=reply_markup, **kw)
            async def answer(self, text, reply_markup=None, **kw):
                return await self._bot.send_message(
                    sel_chat, text, reply_markup=reply_markup, **kw)
        await _render_select(_Editable(message.bot), state, edit=True)
    else:
        await _render_select(message, state, edit=False)


@router.callback_query(F.data.startswith("cadopt:"))
async def choose_select_option(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    attr = data.get("current_attr")
    options = data.get("current_view") or data.get("current_options") or []
    if not attr:
        await callback.answer("Сессия создания истекла — начните заново", show_alert=True)
        return
    try:
        idx = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        idx = -1
    if idx < 0 or idx >= len(options):
        await callback.answer()
        return
    chosen = data.get("chosen") or {}
    chosen[attr] = options[idx].get("value")
    await state.update_data(
        chosen=chosen, current_attr=None, current_options=[],
        current_view=[], current_page=0,
    )
    await callback.answer(f"✅ {str(options[idx].get('label',''))[:30]}")
    await _ask_next_select(callback.message, state, callback.from_user.id)


async def _panel_create_and_report(msg, uid: int, values: dict, extra: dict | None) -> None:
    """Run panel_create_product_sync in a thread with live progress, then report."""
    from storage import get_panel_creds
    from automation.panel import panel_create_product_sync

    creds = get_panel_creds(uid)
    if not creds or not creds.get("cookies"):
        await msg.edit_text("❌ Куки панели не найдены — войдите снова.")
        return

    status_msg = await msg.edit_text(
        "⏳ Создаю товар через панель YooMarket… [v5]\n<i>0 сек...</i>"
    )

    loop = asyncio.get_event_loop()
    panel_task = loop.run_in_executor(
        None,
        panel_create_product_sync,
        creds["cookies"], values["title"], values["price"], values["description"],
        values.get("quantity", 1), values.get("category", ""), uid, extra,
        values.get("photo_path"),
    )
    panel_future = asyncio.ensure_future(panel_task)

    DEADLINE = 42
    CHECK_INTERVAL = 6
    ok, result_msg = False, ""
    elapsed = 0

    while elapsed < DEADLINE:
        done, _ = await asyncio.wait({panel_future}, timeout=CHECK_INTERVAL)
        elapsed += CHECK_INTERVAL
        if panel_future in done:
            try:
                ok, result_msg = panel_future.result()
            except Exception as e:
                ok, result_msg = False, f"Неожиданная ошибка: {str(e)[:200]}"
            break
        try:
            await status_msg.edit_text(
                f"⏳ Создаю товар через панель YooMarket… [v5]\n<i>{elapsed} сек...</i>"
            )
        except Exception:
            pass
    else:
        panel_future.cancel()
        ok, result_msg = False, (
            "⏱ <b>Панель не ответила за 42 секунд.</b>\n\n"
            "Сессия истекла или сервер YooMarket недоступен.\n\n"
            "Войдите в панель снова или создайте товар вручную."
        )

    if ok:
        item_id = result_msg if str(result_msg).isdigit() else ""
        pub_note = ""
        if item_id:
            # Сразу пытаемся сделать товар публичным
            try:
                await status_msg.edit_text("⏳ Товар создан, делаю публичным...")
            except Exception:
                pass
            from automation.panel import panel_publish_item_sync
            try:
                pub_ok, pub_msg = await asyncio.wait_for(
                    loop.run_in_executor(
                        None, panel_publish_item_sync,
                        creds["cookies"], item_id, uid,
                    ),
                    timeout=30,
                )
            except Exception as e:
                pub_ok, pub_msg = False, f"ошибка: {str(e)[:80]}"
            if pub_ok:
                pub_note = f"\n🕓 Отправлен на модерацию ({pub_msg})"
            else:
                # A fresh listing usually has nothing to sell yet, and the
                # marketplace refuses to publish an empty one — say what to do
                # instead of just reporting a failure.
                pub_note = (
                    f"\n📦 Не опубликован — сначала добавьте остатки."
                    f"\n<i>{pub_msg[:150]}</i>"
                )

        b = InlineKeyboardBuilder()
        if item_id and "модерац" not in pub_note:
            b.button(text="📦 Добавить остатки",
                     callback_data=f"pitem_stock:{item_id}")
            b.button(text="🚀 На модерацию",
                     callback_data=f"cadpub:{item_id}")
        b.button(text="➕ Добавить ещё", callback_data="create_ad:start")
        b.button(text="📦 Мои товары", callback_data="menu:ads")
        b.adjust(1)
        await msg.edit_text(
            f"✅ <b>Товар создан через панель!</b>\n\n"
            f"📝 {values['title']}\n💰 {values['price']} ₽"
            f"{chr(10) + '🆔 ' + item_id if item_id else ''}"
            f"{pub_note}",
            reply_markup=b.as_markup(),
        )
        return

    # ── Ошибка — строим правильный набор кнопок ─────────────────────────────
    is_expired = any(w in result_msg for w in ("истекла", "Сессия", "войдите снова", "Войдите"))
    is_found = "✅ Ресурс" in result_msg  # creation-fields нашли, но POST не прошёл

    b = InlineKeyboardBuilder()
    if is_expired:
        b.button(text="🔑 Войти в панель снова", callback_data="panel:sms_start")
    else:
        b.button(
            text="🌐 Создать вручную в панели",
            url="https://panel.yoomarket.net/goods/create",
        )
        b.button(text="🔄 Обновить вход в панель", callback_data="panel:sms_start")
    b.button(text="⬅️ Назад", callback_data="menu:ads")
    b.adjust(1)

    header = "❌ <b>Не удалось создать товар</b>"
    if is_found:
        header = "⚠️ <b>Ресурс найден, но есть ошибка валидации</b>"

    await msg.edit_text(
        f"{header}\n\n{result_msg}",
        reply_markup=b.as_markup(),
    )


@router.callback_query(F.data.startswith("cadpub:"))
async def publish_item(callback: CallbackQuery) -> None:
    """Manually retry making a created item public."""
    from storage import get_panel_creds
    from automation.panel import panel_publish_item_sync

    item_id = callback.data.split(":", 1)[1]
    uid = callback.from_user.id
    creds = get_panel_creds(uid)
    if not creds or not creds.get("cookies"):
        await callback.answer("❌ Нет сессии панели — войдите снова", show_alert=True)
        return

    await callback.answer("⏳ Публикую...")
    loop = asyncio.get_event_loop()
    try:
        ok, msg_text = await asyncio.wait_for(
            loop.run_in_executor(
                None, panel_publish_item_sync, creds["cookies"], item_id, uid,
            ),
            timeout=30,
        )
    except Exception as e:
        ok, msg_text = False, f"ошибка: {str(e)[:80]}"

    b = InlineKeyboardBuilder()
    if not ok:
        b.button(text="🚀 Отправить на модерацию", callback_data=f"cadpub:{item_id}")
    b.button(text="➕ Добавить ещё", callback_data="create_ad:start")
    b.button(text="📦 Мои товары", callback_data="menu:ads")
    b.adjust(1)
    result = (f"🕓 <b>Товар {item_id} отправлен на модерацию</b> ({msg_text})"
              f"\nПоявится в маркете после проверки." if ok
              else f"⚠️ <b>Не удалось опубликовать товар {item_id}</b>\n\n{msg_text}")
    try:
        await callback.message.edit_text(result, reply_markup=b.as_markup())
    except Exception:
        await callback.message.answer(result, reply_markup=b.as_markup())


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "create_ad:save_template")
async def save_template(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("title"):
        await callback.answer("❌ Нет данных для сохранения", show_alert=True)
        return
    s = get_settings(callback.from_user.id)
    templates = s.setdefault("ad_templates", [])
    template = {
        "title": data.get("title", ""),
        "price": data.get("price", 0),
        "description": data.get("description", ""),
        "quantity": data.get("quantity", 1),
        "photo_path": data.get("photo_path"),
    }
    templates.append(template)
    save_settings(callback.from_user.id, s)
    await callback.answer(f"✅ Шаблон «{template['title'][:30]}» сохранён", show_alert=True)


@router.callback_query(F.data == "create_ad:templates_list")
async def templates_list(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    templates = s.get("ad_templates", [])
    if not templates:
        await callback.answer("Шаблонов нет", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for i, t in enumerate(templates[:8]):
        b.button(text=f"📋 {t.get('title','')[:30]} — {t.get('price',0)} ₽", callback_data=f"create_ad:use_template:{i}")
    b.button(text="➕ Новый товар", callback_data="create_ad:start")
    b.button(text="❌ Отмена", callback_data="menu:ads")
    b.adjust(1)
    await callback.message.edit_text(
        "📋 <b>Шаблоны товаров</b>\n\nВыберите шаблон:",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("create_ad:use_template:"))
async def use_template(callback: CallbackQuery, state: FSMContext) -> None:
    idx = int(callback.data.split(":")[-1])
    s = get_settings(callback.from_user.id)
    templates = s.get("ad_templates", [])
    if idx >= len(templates):
        await callback.answer("Шаблон не найден", show_alert=True)
        return
    t = templates[idx]
    await state.clear()
    import os
    photo_path = t.get("photo_path")
    if photo_path and not os.path.exists(photo_path):
        photo_path = None  # файл могли удалить при редеплое без volume
    await state.update_data(
        title=t.get("title", ""),
        price=t.get("price", 0),
        description=t.get("description", ""),
        quantity=t.get("quantity", 1),
        photo_path=photo_path,
    )
    await _show_preview(callback.message, state, edit=True)
    await callback.answer()
