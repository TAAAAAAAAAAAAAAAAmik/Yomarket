from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ui

from api.yoomarket import YooMarketAPI
from keyboards.main import AdCallback, PaginationCallback, back_keyboard

router = Router()

# На деле этот маркетплейс говорит «опубликовано» и «снято с публикации».
# Прежние имена оставлены потому, что ими пользуются другие вызовы.
STATUS_EMOJI = {
    "publish": "🟢 В продаже",
    "active": "🟢 Активен",
    "unpublish": "🙈 Снят с продажи",
    "inactive": "🔴 Неактивен",
    "moderate": "🕓 На модерации",
    "blocked": "⛔ Заблокирован",
    "sold": "✅ Продан",
}

def _manual_only(raw: str) -> bool:
    """Снято руками: этот маркетплейс возвращает в продажу только истёкшие.

    Кнопка, которая заведомо ответит `incorrect_status`, хуже прямого «это
    делается на сайте»: она обещает то, чего не будет.
    """
    from api.yoomarket import YooMarketAPI
    return str(raw).lower() in YooMarketAPI._MANUAL_ONLY


def _status(raw: str) -> str:
    return STATUS_EMOJI.get(raw, f"⚪ {raw}")


def _price_text(ad: dict) -> str:
    """Цена объявления для показа. Маркетплейс присылает её объектом
    {"amount": …, "currency": …} — выведенная как есть, она была видна
    продавцу словарём."""
    from orderfields import ad_price, money
    value = ad_price(ad)
    return money(value) if value is not None else "—"


def _load_error(e: Exception) -> str:
    """Сказать, что на самом деле помешало загрузить товары.

    Пустая или непонятная ошибка здесь — это разница между «бот сломался» и
    «у вас истёк токен»: со вторым продавец может что-то сделать.
    """
    s = str(e) or type(e).__name__
    low = s.lower()
    if "timeout" in low or "timed out" in low:
        return ("⏱ <b>Юмаркет не ответил вовремя</b>\n\n"
                "Маркетплейс сейчас медленный или недоступен. Попробуйте ещё раз.")
    if "401" in s or "unauthenticated" in low or "unauthorized" in low:
        return ("🔑 <b>Токен не принят</b>\n\n"
                "Похоже, API-токен отозван или истёк. Создайте новый в панели "
                "(Мой магазин → Интеграции) и пришлите его командой /start.")
    if any(k in s for k in ("502", "503", "504")) or "недоступен" in low:
        return ("🛠 <b>Юмаркет сейчас лежит</b>\n\n"
                "Сервер маркетплейса вернул ошибку <b>502</b> — это авария на "
                "их стороне, не в боте. Бот уже пробовал повторить.\n\n"
                "Проверьте <a href=\"https://yoomarket.net\">сайт</a>: если он "
                "тоже не открывается — остаётся ждать. Все авто-функции "
                "продолжат работать, как только Юмаркет поднимется.")
    if "429" in s or "too many" in low:
        return ("🚦 <b>Слишком много запросов</b>\n\n"
                "Юмаркет ограничил частоту. Подождите минуту и повторите.")
    if any(k in low for k in ("cannot connect", "dns", "network", "unreachable",
                              "connection")):
        return ("🌐 <b>Нет связи с Юмаркетом</b>\n\n"
                "Сервер маркетплейса недоступен. Это пройдёт само — повторите позже.")
    # Экранируем: в сырой ошибке может оказаться «<», и тогда Telegram
    # whole edit — leaving the "⏳ Загружаю..." text on screen forever, i.e.
    # зависание, ради объяснения которого это сообщение и написано.
    import html as _html
    return (f"❌ <b>Не удалось загрузить объявления</b>\n\n"
            f"<code>{_html.escape(str(s)[:250])}</code>")


async def _safe_edit(message, text: str, reply_markup=None) -> None:
    """Replace a message's text, or say something rather than nothing.

    A rejected edit is worse than a wrong one here: the message being replaced
    is the "⏳ Загружаю..." placeholder, so a failure freezes it.
    """
    try:
        await message.edit_text(text[:4000], reply_markup=reply_markup)
        return
    except Exception:
        pass
    try:
        import re as _re
        plain = _re.sub(r"<[^>]+>", "", text)[:4000]
        await message.edit_text(plain, parse_mode=None, reply_markup=reply_markup)
    except Exception:
        try:
            await message.answer(text[:4000], reply_markup=reply_markup)
        except Exception:
            pass


def _fmt_list(ads: list[dict], total: int | None) -> str:
    """Summary only. The listings are browsed under «📦 Товары», so repeating
    them here just makes the menu scroll."""
    if not ads:
        return ("🚀 <b>Объявления</b>\n\n"
                "Товаров пока нет — добавьте первый.")
    count = total or len(ads)
    return (f"🚀 <b>Объявления</b>\n\n"
            f"Всего товаров: <b>{count}</b>\n\n"
            f"Откройте «📦 Товары», чтобы посмотреть их по категориям.")


def _ads_keyboard(ads: list[dict], next_cursor: str | None):
    """Actions only — the listings themselves live behind «📦 Товары».

    This menu used to repeat every ad as its own button, which duplicated the
    category browser and pushed the actions off the screen.
    """
    b = InlineKeyboardBuilder()
    b.button(text="📦 Товары", callback_data="pitems:cats")
    b.button(text="💰 Цены", callback_data="prices:menu")
    b.button(text="➕ Добавить товар", callback_data="create_ad:start")
    b.button(text="📦 Паки", callback_data="packs:menu")
    # Автоматика по товарам живёт здесь, а не в меню авто-функций: и то и
    # другое делается над объявлениями, и ищут это именно тут.
    b.button(text="⭐ Премиум продвижение", callback_data="selenium:bump:menu")
    b.button(text="🔄 Авто-восстановление", callback_data="selenium:restore:menu")
    b.button(text="🔄 Обновить", callback_data="ads_load")
    b.button(text="⬅️ Меню", callback_data="menu:main")
    b.adjust(2, 2, 2, 2, 1)
    return b.as_markup()


@router.callback_query(F.data.in_({"menu:ads", "ads_load"}))
async def ads_menu(callback: CallbackQuery, api: YooMarketAPI) -> None:
    # Сначала отвечаем на нажатие: иначе Telegram несколько секунд крутит
    # кнопку и сам сообщает о таймауте.
    await callback.answer()
    await _safe_edit(callback.message, "⏳ Загружаю объявления...")
    try:
        data = await api.get_ads()
        ads: list[dict] = data.get("data") or data.get("items") or []
        meta = data.get("meta", {})
        next_cursor: str | None = meta.get("next_cursor")
        total: int | None = meta.get("total")
        text = _fmt_list(ads, total)
        keyboard = _ads_keyboard(ads, next_cursor)
    except Exception as e:
        text = _load_error(e)
        b = InlineKeyboardBuilder()
        b.button(text="🔄 Повторить", callback_data="ads_load")
        b.button(text="📦 Товары (панель)", callback_data="pitems:cats")
        b.button(text="➕ Добавить товар", callback_data="create_ad:start")
        b.button(text="⬅️ Главное меню", callback_data="menu:main")
        b.adjust(1, 1)
        keyboard = b.as_markup()
    await _safe_edit(callback.message, text, keyboard)


@router.callback_query(PaginationCallback.filter(F.entity == "ads"))
async def paginate_ads(
    callback: CallbackQuery,
    callback_data: PaginationCallback,
    api: YooMarketAPI,
) -> None:
    await callback.message.edit_text("⏳ Загружаю...")
    try:
        data = await api.get_ads(cursor=callback_data.cursor)
        ads: list[dict] = data.get("data") or data.get("items") or []
        meta = data.get("meta", {})
        next_cursor: str | None = meta.get("next_cursor")
        total: int | None = meta.get("total")
        text = _fmt_list(ads, total)
        keyboard = _ads_keyboard(ads, next_cursor)
    except Exception as e:
        text = f"❌ Ошибка: {e}"
        keyboard = back_keyboard()
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(AdCallback.filter())
async def show_ad_detail(
    callback: CallbackQuery,
    callback_data: AdCallback,
    api: YooMarketAPI,
    state: FSMContext,
) -> None:
    await state.clear()  # сюда возвращаются из других экранов
    await callback.message.edit_text("⏳ Загружаю...")
    try:
        ad = await api.get_ad(callback_data.ad_id)
        title = ad.get("title") or ad.get("name") or "—"
        price = _price_text(ad)
        status_raw = ad.get("status", "")
        status = _status(status_raw)
        description = ad.get("description") or "Нет описания"
        views = ad.get("views_count", "—")
        category = ad.get("category") or "—"
        text = (
            f"📦 <b>{title}</b>\n\n"
            f"💰 Цена: <b>{price} ₽</b>\n"
            f"📊 Статус: {status}\n"
            f"🏷 Категория: {category}\n"
            f"👁 Просмотры: {views}\n\n"
            + ("ℹ️ <i>Снят с продажи вручную. Юмаркет возвращает автоматически "
               "только истёкшие объявления — это нужно опубликовать на сайте.</i>"
               "\n\n" if _manual_only(status_raw) else "")
            + f"📝 <b>Описание:</b>\n{description}"
        )
        # Ручные «снять с продажи» и «вернуть в продажу» убраны по решению
        # продавца: то же самое делается на сайте в два нажатия, а в боте
        # это была лишняя кнопка рядом с платными. Автовозврат истёкших
        # объявлений — отдельная функция и остаётся (пункты C3 и C4).
        b = InlineKeyboardBuilder()
        b.button(text="⬅️ К товарам", callback_data="ads_load")
        b.adjust(1, 1)
        keyboard = b.as_markup()
    except Exception as e:
        text = f"❌ Ошибка: {e}"
        keyboard = back_keyboard()

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("ad_bump:"))
async def bump_ad(callback: CallbackQuery, api: YooMarketAPI) -> None:
    """Ask before promoting: «Премиум» is a paid action on this marketplace."""
    ad_id = callback.data.split(":", 1)[1]
    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, продвинуть", callback_data=f"ad_bump_ok:{ad_id}")
    b.button(text="❌ Отмена", callback_data=f"ad:{ad_id}")
    b.adjust(1)
    await callback.message.answer(
        "⚠️ <b>Продвижение платное</b>\n\n"
        "На Юмаркете поднятие — это действие «Премиум». Оплата идёт не с "
        "баланса магазина, а выбранным способом — придёт ссылка на "
        "оплату.\n\nПродвинуть этот товар?",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ad_bump_ok:"))
async def bump_ad_confirmed(callback: CallbackQuery, api: YooMarketAPI) -> None:
    """Платное продвижение идёт через панель: в API такого метода нет вовсе."""
    import asyncio

    from automation.panel import panel_bump_item_sync
    from storage import get_panel_creds

    from handlers.selenium_settings import promo_params
    from storage import get_settings

    ad_id = callback.data.split(":", 1)[1]
    creds = get_panel_creds(callback.from_user.id)
    if not creds or not creds.get("cookies"):
        await callback.answer("⚠️ Нужен вход в панель продавца", show_alert=True)
        return

    s = get_settings(callback.from_user.id)
    params = promo_params(s)
    if not params:
        b = InlineKeyboardBuilder()
        b.button(text="⚙️ Выбрать тариф", callback_data="promo:setup")
        b.button(text="⬅️ Назад", callback_data=f"ad:{ad_id}")
        ui.lay(b)
        await callback.message.edit_text(
            "⚙️ <b>Сначала выберите тариф</b>\n\n"
            "«Премиум» требует услугу, срок и способ оплаты — сроки стоят "
            "по-разному, поэтому я не подставляю их сам.",
            reply_markup=b.as_markup())
        await callback.answer()
        return

    await callback.answer("⏳ Продвигаю...", show_alert=False)
    try:
        loop = asyncio.get_event_loop()
        ok, msg = await asyncio.wait_for(
            loop.run_in_executor(
                None, panel_bump_item_sync, creds["cookies"], ad_id,
                callback.from_user.id, True, params),
            timeout=60,
        )
        if ok:
            await callback.message.edit_text(f"✅ {msg[:600]}")
        else:
            hint = ""
            if "нет прав" in msg.lower():
                # Nova разрешает по каждой записи отдельно — значит дело в этом товаре
                hint = ("\n\nПанель отказала в самом действии. Чаще всего "
                        "это значит, что <b>панель и токен — разные "
                        "магазины</b>: чужие объявления панель трогать не "
                        "даёт.\n\nСведите их: войдите в панель аккаунтом "
                        "этого магазина, либо переключите бота на аккаунт "
                        "панели («Настройки» → «Аккаунты»). Реже — профиль "
                        "ещё не прошёл проверку.")
            await callback.message.edit_text(f"⛔ {msg[:400]}{hint}")
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {str(e)[:200]}")


