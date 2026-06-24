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
    confirm = State()


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
    lines.append(f"\n📄 Описание:\n{description}")
    return "\n".join(lines)


def _confirm_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Создать товар", callback_data="create_ad:submit")
    b.button(text="💾 Сохранить как шаблон", callback_data="create_ad:save_template")
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
    await state.update_data(title=title)
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
    await state.update_data(price=price)
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
    await state.update_data(description=desc)
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
    await _show_preview(callback.message, state, edit=True)
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
    await _show_preview(message, state, edit=False)


async def _show_preview(msg, state: FSMContext, edit: bool) -> None:
    data = await state.get_data()
    await state.set_state(CreateAdState.confirm)
    text = _preview(data)
    kb = _confirm_kb()
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
    await state.set_state(states[field])
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="menu:ads")
    await callback.message.edit_text(prompts[field], reply_markup=b.as_markup())
    await callback.answer()


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "create_ad:submit")
async def submit_ad(callback: CallbackQuery, state: FSMContext, api: YooMarketAPI) -> None:
    data = await state.get_data()
    await state.clear()
    uid = callback.from_user.id

    title = data.get("title", "")
    price = data.get("price", 0)
    description = data.get("description", "")
    quantity = data.get("quantity", 1)
    category = data.get("category", "")

    if not title or not price:
        await callback.answer("❌ Не хватает данных", show_alert=True)
        return

    await callback.message.edit_text("⏳ Создаю товар...")

    # ── Шаг 1: Integration API ──────────────────────────────────────────────
    if api:
        try:
            result = await api.create_ad(
                title=title, price=price, description=description,
                quantity=quantity, category=category,
            )
            ad_id = result.get("id") or (result.get("data") or {}).get("id") or "—"
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

    # ── Шаг 2: Panel (Nova API) ─────────────────────────────────────────────
    from storage import get_panel_creds
    from automation.panel import PanelSession

    creds = get_panel_creds(uid)
    if not creds or not creds.get("cookies"):
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

    status_msg = await callback.message.edit_text(
        "⏳ Подключаюсь к панели YooMarket...\n"
        "<i>(обычно занимает 5–15 секунд)</i>"
    )

    ps = PanelSession(creds["cookies"])
    await ps.start()
    try:
        ok, result_msg = await asyncio.wait_for(
            ps.create_product(
                title=title, price=price, description=description,
                quantity=quantity, category=category,
            ),
            timeout=30,
        )
    except asyncio.TimeoutError:
        ok, result_msg = False, (
            "⏱ <b>Панель не ответила за 30 секунд.</b>\n\n"
            "Возможные причины:\n"
            "• Сессия истекла — войдите в панель снова\n"
            "• Сервер YooMarket недоступен\n\n"
            "Попробуйте создать товар вручную."
        )
    except Exception as e:
        ok, result_msg = False, f"Неожиданная ошибка: {str(e)[:150]}"
    finally:
        try:
            await asyncio.wait_for(ps.close(), timeout=3)
        except Exception:
            pass

    if ok:
        b = InlineKeyboardBuilder()
        b.button(text="➕ Добавить ещё", callback_data="create_ad:start")
        b.button(text="📦 Мои товары", callback_data="menu:ads")
        b.adjust(1)
        await callback.message.edit_text(
            f"✅ <b>Товар создан через панель!</b>\n\n"
            f"📝 {title}\n💰 {price} ₽",
            reply_markup=b.as_markup(),
        )
        await callback.answer()
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

    await callback.message.edit_text(
        f"{header}\n\n{result_msg}",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


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
    await state.update_data(
        title=t.get("title", ""),
        price=t.get("price", 0),
        description=t.get("description", ""),
        quantity=t.get("quantity", 1),
    )
    await _show_preview(callback.message, state, edit=True)
    await callback.answer()
