"""Мастер создания товара через API: шаги, предпросмотр, публикация."""
from __future__ import annotations

import asyncio
import html
import logging
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ui

from api.yoomarket import YooMarketAPI
from keyboards.main import back_keyboard
from storage import get_settings, save_settings

router = Router()
logger = logging.getLogger(__name__)

# Сколько товаров показывать в списке для копии. Панель отдаёт до
# пятидесяти, а клавиатура из пятидесяти кнопок — это не выбор, а
# свалка: копируют обычно последнее, а не то, что заведено год назад.
_COPY_LIMIT = 12


def _readable(said) -> str:
    """Отказ маркетплейса по-человечески.

    Он приходит JSON-ом, и русский текст в нём — экранированными кодами:
    на экран продавца уезжало «\\u041f\\u043e\\u043b\\u0435
    \\u041a\\u0430\\u0442...» вместо «Поле Категория обязательно».
    Прочитать это нельзя, то есть отказ есть, а причины нет.

    Сначала пробуем разобрать по полям (`explain_validation` знает их
    русские имена), потом — просто раскодировать. Что не разобралось,
    отдаём как есть: сырой текст хуже перевода, но лучше молчания.
    """
    import json

    from automation.panel import explain_validation

    text = str(said or "")
    # `json.loads` сам превращает \uXXXX в буквы — отдельного декодера не
    # нужно, нужен лишь найденный в строке объект.
    start = text.find("{")
    body = None
    if start >= 0:
        try:
            body = json.loads(text[start:])
        except ValueError:
            body = None

    # Конверт бывает вложенным: у панели `errors` лежит сверху, а
    # маркетплейс кладёт всё внутрь `error` — и разбор, знающий только
    # верхний уровень, показал живой отказ про `files` сырыми кодами.
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        body = body["error"]

    if isinstance(body, dict):
        # `errors` разбираем ПЕРВЫМ: в `message` лежит сводка вида
        # «x (and 3 more errors)», которая не говорит, каких именно, а по
        # полям видно, что чинить. `explain_validation` ждёт ровно такой
        # формы — {поле: [жалобы]} — и знает их русские имена.
        errs = body.get("errors")
        if isinstance(errs, dict) and errs:
            said_ru = explain_validation(json.dumps(errs, ensure_ascii=False))
            if said_ru:
                return said_ru[:400]
            rows = [str(v[0] if isinstance(v, list) and v else v)
                    for v in errs.values()]
            joined = " ".join(r for r in rows if r)
            if joined:
                return joined[:400]
        msg = str(body.get("message") or "").strip()
        if msg:
            return msg[:400]

    said_ru = explain_validation(text)
    return (said_ru or text)[:400]


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


# Шаги мастера по порядку. Здесь их пять, и раньше первые четыре экрана
# говорили «Шаг N/4», а пятый — «Шаг 5/5»: продавцу обещали четыре шага и
# показывали пятый. Считать шаги в двух местах по памяти — верный способ
# снова разойтись, поэтому порядок задан один раз и здесь.
_STEPS = ("title", "price", "description", "quantity", "photo")
_STEP_NAMES = {
    "title": "Название",
    "price": "Цена",
    "description": "Описание",
    "quantity": "Количество",
    "photo": "Фото",
}
# С какого шага возвращает «⬅️ Назад». Опечатка в цене на четвёртом шаге
# означала пройти мастер заново: назад было нельзя, только «Отмена».
_STEP_BACK = {"price": "title", "description": "price",
              "quantity": "description", "photo": "quantity"}


def _step_header(step: str) -> str:
    """Заголовок шага с полосой: видно, сколько пройдено и сколько осталось."""
    n = _STEPS.index(step) + 1
    total = len(_STEPS)
    bar = "▰" * n + "▱" * (total - n)
    return (f"<b>Шаг {n} из {total}</b> · {bar}\n"
            f"<b>{_STEP_NAMES[step]}</b>")


def _step_kb(step: str, extra: list | None = None) -> InlineKeyboardMarkup:
    """Кнопки шага: сначала свои, потом «Назад» и «Отмена».

    «Назад» ведёт на предыдущий шаг, а не в меню: заново вводить всё из-за
    одной опечатки — то же самое, что не дать исправить её вовсе.
    """
    b = InlineKeyboardBuilder()
    for text, data in (extra or []):
        b.button(text=text, callback_data=data)
    if step in _STEP_BACK:
        b.button(text="⬅️ Назад", callback_data=f"create_ad:back:{_STEP_BACK[step]}")
    b.button(text="❌ Отмена", callback_data="menu:ads")
    ui.lay(b)
    return b.as_markup()


def _preview(data: dict) -> str:
    """Карточка перед созданием.

    Всё, что ввёл продавец, экранируется: одиночный `<` в названии или
    описании роняет отправку целиком, и вместо предпросмотра он увидит
    молчание. Это уже случалось в этом проекте с ответом панели.
    """
    title = html.escape(str(data.get("title") or "—"))
    price = data.get("price", "—")
    description = html.escape(str(data.get("description") or "—"))
    quantity = data.get("quantity", 1)
    category = html.escape(str(data.get("category") or ""))
    lines = [
        "📦 <b>Предпросмотр товара</b>",
        "━━━━━━━━━━━━━━",
        f"📝 <b>{title}</b>",
        f"💰 {price} ₽   ·   🔢 {quantity} шт.",
    ]
    if category:
        lines.append(f"🏷 {category}")
    lines.append("📷 Фото: есть ✅" if data.get("photo_path")
                 else "📷 Фото: <b>нет</b> — без него товар не создать")
    lines.append("")
    lines.append("📄 <b>Описание</b>")
    lines.append(description)
    return "\n".join(lines)


def _confirm_kb(has_photo: bool = False) -> InlineKeyboardMarkup:
    """Клавиатура предпросмотра.

    Без фото кнопки «Создать товар» здесь нет вовсе. Показывать её и
    отвечать отказом на нажатие — то же самое, что обещать невыполнимое:
    панель объявление без картинки не принимает. Дорога отсюда одна —
    приложить фото, и она стоит первой.
    """
    b = InlineKeyboardBuilder()
    # «Сохранить как шаблон» отсюда снято. Образец теперь пишется сам, по
    # факту созданного товара, и помнит раздел панели с его полями — а
    # сохранённый ДО отправки не помнит их и помнить не может: раздел ещё
    # не выбран. Две кнопки про одно, из которых худшая быстрее, — это
    # список образцов, наполовину не умеющих копироваться.
    if has_photo:
        b.button(text="✅ Создать товар", callback_data="create_ad:submit")
        b.button(text="📷 Заменить фото", callback_data="create_ad:edit:photo")
    else:
        b.button(text="📷 Добавить фото", callback_data="create_ad:edit:photo")
    # Три правки — одной строкой, а не тремя. «Изменить» в каждой надписи
    # съедало половину ширины и повторяло то, что и так понятно из экрана
    # предпросмотра; без него все три помещаются в ряд, и видно, что это
    # один набор, а не три разных действия.
    b.button(text="✏️ Название", callback_data="create_ad:edit:title")
    b.button(text="✏️ Цена", callback_data="create_ad:edit:price")
    b.button(text="✏️ Описание", callback_data="create_ad:edit:description")
    b.button(text="❌ Отмена", callback_data="menu:ads")
    # «Отмена» отдельной строкой: она бросает набранное, и стоять под одним
    # пальцем с правкой описания ей незачем.
    # Столько единиц, сколько кнопок стоит НАД тремя правками. Число было
    # зашито, и снятая кнопка «сохранить как шаблон» сдвинула бы весь ряд:
    # правки разъехались бы по строкам, а «Отмена» встала бы рядом с ними.
    b.adjust(*([1] * (2 if has_photo else 1) + [3, 1]))
    return b.as_markup()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "create_ad:start")
async def create_ad_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Развилка: заводить товар с нуля или скопировать уже созданный.

    Развилка показывается, ТОЛЬКО когда есть что копировать. Кнопка
    «шаблонная копия» у того, кто ещё ничего не создавал, ведёт в пустой
    список — то есть обещает то, чего нет, и добавляет лишний шаг перед
    единственным настоящим действием.
    """
    from features import ad_templates_shown
    from storage import get_token

    await state.clear()
    uid = callback.from_user.id
    # Копия — только админам (решение владельца). Развилки у продавца нет
    # вовсе: кнопка, отвечающая отказом, хуже, чем её отсутствие.
    #
    # Второе условие — токен: и список объявлений, и само создание идут
    # через Integration API. Кнопка, за которой «подключи магазин», обещает
    # то, чего за ней нет.
    can_copy = ad_templates_shown(uid) and bool(get_token(uid))
    if not can_copy:
        await _ask_title(callback, state)
        return
    b = InlineKeyboardBuilder()
    b.button(text="✍️ Создать товар", callback_data="create_ad:new")
    b.button(text="📋 Шаблонная копия",
             callback_data="create_ad:templates_list")
    b.button(text="❌ Отмена", callback_data="menu:ads")
    await callback.message.edit_text(ui.screen("➕ <b>Новый товар</b>", [
        "Завести с нуля — мастер спросит название, цену, описание, "
        "количество, фото и раздел панели.",
        "",
        "Копия — тот же товар целиком, вместе с разделом и его полями. "
        "Один выбор, и товар уходит в панель.",
    ]), reply_markup=ui.lay(b, solo={"create_ad:new",
                                     "create_ad:templates_list"}).as_markup())
    await callback.answer()


@router.callback_query(F.data == "create_ad:new")
async def create_ad_new(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _ask_title(callback, state)


async def _ask_title(callback: CallbackQuery, state: FSMContext) -> None:
    """Первый шаг мастера. Один на оба входа — с развилки и без неё."""
    await state.set_state(CreateAdState.title)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="menu:ads")
    ui.lay(b)
    await callback.message.edit_text(
        "➕ <b>Новый товар</b>\n\n"
        + _step_header("title")
        + "\n\nПришли название товара — то, что увидит покупатель на "
          "витрине.",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("create_ad:back:"))
async def create_ad_back(callback: CallbackQuery, state: FSMContext) -> None:
    """Шаг назад. Введённое не теряется — оно уже в состоянии.

    Раньше отсюда вела одна дорога: «Отмена», то есть заново весь мастер
    из-за одной опечатки. Это то же самое, что не дать исправить её вовсе.
    """
    step = callback.data.split(":")[-1]
    if step not in _STEPS:
        await callback.answer("Такого шага нет", show_alert=True)
        return
    data = await state.get_data()
    was = {"title": data.get("title"), "price": data.get("price"),
           "description": data.get("description"),
           "quantity": data.get("quantity")}.get(step)
    await state.set_state(getattr(CreateAdState, step))
    hint = {
        "title": "Пришли название товара.",
        "price": "Пришли цену в рублях, одним числом.",
        "description": "Пришли описание. Ссылки панель не примет.",
        "quantity": "Сколько штук в наличии? Одним числом.",
    }[step]
    было = (f"\n\nСейчас: <b>{html.escape(str(was))}</b> — пришли новое "
            f"значение, чтобы заменить." if was not in (None, "") else "")
    extra = ([("1️⃣ Пропустить — одна штука", "create_ad:qty:1")]
             if step == "quantity" else [])
    await callback.message.edit_text(
        _step_header(step) + "\n\n" + hint + было,
        reply_markup=_step_kb(step, extra))
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
    await message.answer(
        f"✅ Название: <b>{html.escape(title)}</b>\n\n"
        + _step_header("price")
        + "\n\nПришли цену в рублях, одним числом.",
        reply_markup=_step_kb("price"),
    )


# ---------------------------------------------------------------------------
# Step 2: Price
# ---------------------------------------------------------------------------

@router.message(CreateAdState.price)
async def ad_price(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().replace(" ", "").replace(",", ".")
    try:
        exact = float(raw)
        price = int(exact)
        if price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Цену нужно числом, например: <b>500</b>")
        return
    # Копейки панель не берёт, и раньше 500.9 молча становились 500 — про
    # чужие деньги молчать нельзя даже в мелочи.
    if exact != price:
        await message.answer(
            f"⚠️ Копейки панель не принимает: беру <b>{price} ₽</b> "
            f"вместо {exact:g}. Если нужно дороже — пришли другое число.")
    data = await state.get_data()
    await state.update_data(price=price)
    if data.get("editing"):
        await state.update_data(editing=False)
        await _show_preview(message, state, edit=False)
        return
    await state.set_state(CreateAdState.description)
    await message.answer(
        f"✅ Цена: <b>{price} ₽</b>\n\n"
        + _step_header("description")
        + "\n\nПришли описание. Ссылки панель не примет — кроме "
          "видеосервисов и дисков из её белого списка.",
        reply_markup=_step_kb("description"),
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
    # Панель запрещает ссылки и отказывает 422 — но только на последнем шаге,
    # когда введено уже всё. Отказ здесь дешевле отказа в конце: править одно
    # поле, а не проходить мастер заново.
    from automation.panel import PANEL_LINK_ALLOWED, link_trouble
    found = link_trouble(desc)
    if found:
        await message.answer(
            f"❌ Панель не примет описание со ссылкой: "
            f"<code>{html.escape(found)}</code>\n\n"
            f"Разрешены только: {', '.join(PANEL_LINK_ALLOWED[:6])} и подобные "
            f"видеосервисы и диски.\n\n"
            f"Убери ссылку и пришли описание ещё раз.")
        return
    data = await state.get_data()
    await state.update_data(description=desc)
    if data.get("editing"):
        await state.update_data(editing=False)
        await _show_preview(message, state, edit=False)
        return
    await state.set_state(CreateAdState.quantity)
    await message.answer(
        _step_header("quantity")
        + "\n\nСколько штук в наличии? Одним числом — или пропусти, "
          "тогда будет одна.",
        reply_markup=_step_kb(
            "quantity", [("1️⃣ Пропустить — одна штука", "create_ad:qty:1")]),
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
    """Шаг фото. Пропуска здесь нет: панель без картинки товар не примет.

    Кнопка «Без фото» тут была и вела в тупик — продавец доходил до конца
    мастера, нажимал «Создать товар» и получал отказ панели, потратив на
    объявление весь путь. Отказ на первом шаге дешевле отказа на последнем.
    """
    await state.set_state(CreateAdState.photo)
    text = (
        _step_header("photo")
        + "\n\nПришли фото товара — картинкой, не файлом.\n\n"
          "<i>Панель требует картинку: без неё объявление не создать, и "
          "пропустить этот шаг нельзя.</i>"
    )
    kb = _step_kb("photo")
    if edit:
        await msg.edit_text(text, reply_markup=kb)
    else:
        await msg.answer(text, reply_markup=kb)


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
        await message.answer(f"❌ Не удалось скачать фото: {str(e)[:100]}\nПопробуй ещё раз.")
        return
    await state.update_data(photo_path=path)
    await _show_preview(message, state, edit=False)


@router.message(CreateAdState.photo)
async def ad_photo_not_photo(message: Message) -> None:
    await message.answer("📷 Отправь фото — именно изображением, а не файлом. "
                         "Без него товар создать нельзя.")


@router.callback_query(F.data == "create_ad:photo_skip")
async def ad_photo_skip(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка снята, но висит в сообщениях, отправленных до обновления.

    Обработчик оставлен нарочно: без него нажатие уходит в никуда — Telegram
    крутит часы и гасит их молча, а продавец видит экран, который перестал
    отвечать. Лучше сказать, что правило изменилось.
    """
    await callback.answer("Теперь фото обязательно", show_alert=True)
    await _ask_photo(callback.message, state, edit=True)


async def _edit_safely(msg, text: str, reply_markup=None) -> None:
    """Показать отчёт так, чтобы он дошёл даже при чужой разметке внутри.

    19.08 экран замер на «⏳ Товар создан, делаю публичным…» и остался так
    навсегда. В отчёт вставляется ответ панели, панель отвечает своим HTML,
    ответ обрезается по длине — и в сообщение попадает половина тега.
    Telegram отказал всему сообщению целиком
    (`can't parse entities: Unsupported start tag "co</i"`), обработчик
    упал на этой строке, а продавец остался с «делаю публичным» и без
    единого слова о том, что товар вообще создан.

    Чужие куски экранируются по месту, но одного этого мало: следующий
    такой кусок добавят завтра. Поэтому здесь последняя защита — не вышло
    с разметкой, шлём без неё. Некрасивый отчёт лучше замершего экрана.
    """
    try:
        await msg.edit_text(text, reply_markup=reply_markup)
        return
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        logger.warning("Telegram не принял разметку (%s) — шлю без неё",
                       str(e)[:120])
    try:
        await msg.edit_text(text[:4000], reply_markup=reply_markup,
                            parse_mode=None)
    except Exception as e:
        # Дальше идти некуда: сказать продавцу больше нечем, но в логе
        # это должно остаться — молчаливый экран мы уже проходили.
        logger.error("отчёт не доставлен: %s", str(e)[:200])


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
# Правка полей с экрана предпросмотра
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
    # editing=True → шаг вернёт на предпросмотр, а не поведёт дальше по мастеру
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

    # Фото проверяется ещё раз здесь, а не только кнопками. Сюда приходят
    # из шаблона, у которого файл фото исчез при редеплое без volume, и со
    # старых сообщений, где кнопка «Создать товар» осталась висеть. Панель
    # такое объявление не примет, и узнать об этом от бота дешевле, чем от
    # отказа на середине создания.
    import os
    if not data.get("photo_path") or not os.path.exists(data["photo_path"]):
        await state.update_data(photo_path=None)
        await callback.answer("❌ Без фото товар не создать", show_alert=True)
        await _ask_photo(callback.message, state, edit=True)
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
            ui.lay(b)
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
        ui.lay(b)
        await callback.message.edit_text(
            "❌ <b>Не удалось создать товар</b>\n\n"
            "Integration API не поддерживает создание товаров.\n\n"
            "💡 Войди в <b>Панель продавца</b> через email — бот будет создавать товары через неё.\n\n"
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
        queue = [a for a in _SECTION_TRIPLE if a in by_attr]
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
            await _ask_next_select(callback.message, state, uid, api)
            await callback.answer()
            return

    # Форму не получили — пробуем создать напрямую (сервер сам скажет, чего
    # не хватает). Состояние не чистим: панель называет недостающее поле, и
    # с живой формой мастера этот отказ становится вопросом, а не тупиком.
    await state.update_data(pending=values, chosen={})
    await _panel_create_and_report(callback.message, uid, values, extra=None,
                                   state=state, api=api)
    await callback.answer()


def _autopick_match(options: list, words) -> dict | None:
    """Единственный подходящий вариант селекта или None.

    Нужно для создания товаров из плагина: раздел витрины там известен
    заранее, и заставлять продавца выбирать «Roblox» вручную по каждому
    номиналу — работа, которую бот может сделать сам.

    Слова пробуются по порядку, от узкого к широкому: «robux» отличает
    валюту от аккаунтов и подарочных карт, а «roblox» подойдёт и им. Точное
    совпадение имени сильнее вхождения.

    **Несколько совпадений — не повод взять первое.** Раздел решает, где
    покупатель увидит товар; ошибиться здесь молча значит выставить код
    Robux среди аккаунтов и узнать об этом по отсутствию продаж. Ни одного
    совпадения — то же самое. В обоих случаях возвращается None, и продавца
    спрашивают, как раньше.
    """
    for word in words or []:
        w = str(word or "").strip().lower()
        if not w:
            continue
        pairs = [(o, str(o.get("label", "")).strip().lower()) for o in options]
        exact = [o for o, label in pairs if label == w]
        if len(exact) == 1:
            return exact[0]
        near = [o for o, label in pairs if w in label]
        if len(near) == 1:
            return near[0]
    return None


def _pick_option(options: list, value, label: str,
                 words: list) -> tuple[dict | None, str]:
    """Какой вариант списка подходит — и почему. → (вариант или None, как).

    Порядок не декоративный, он от сильного к слабому:

    1. **номер образца** — форма создания и карточка товара это одна панель,
       один раздел `items` и одно поле, значит и нумерация одна;
    2. **надпись образца** — если номера в списке нет, но «Аккаунты» в нём
       есть, это он и есть, каким бы номером ни звался;
    3. **слово** — название раздела с маркетплейса и слова названия товара;
       годится только единственное совпадение.

    Ни одного — None, и решает вызывающий: у него есть ещё поиск по панели
    и номер образца, который списком не опровергнут.
    """
    if value not in (None, ""):
        for o in options:
            if str(o.get("value")) == str(value):
                return o, "номер образца"
    want = str(label or "").strip().lower()
    if want:
        same = [o for o in options
                if str(o.get("label", "")).strip().lower() == want]
        if len(same) == 1:
            return same[0], "надпись образца"
    by_word = _autopick_match(options, words)
    if by_word is not None:
        return by_word, "подобран по названию"
    return None, ""


# Раздел, подраздел и тип выдачи. Их значение по умолчанию не берётся
# никогда: раздел решает, где покупатель увидит товар, а тип — как заказ
# будет выдаваться. Молча ошибиться здесь дороже, чем спросить.
_SECTION_TRIPLE = ("category", "subcategory", "type")


# Сколько вариантов держим в состоянии и показываем листалкой. Это же
# число говорит, оборван ли ответ панели: ровно столько — значит дальше не
# показали.
#
# Было 500, и панель отдавала 825 (живой /copy_debug 07.09). Триста
# двадцать пять разделов обрезались, «Standoff 2» — буква S — в остаток не
# попадал, и сверка не находила номер, КОТОРЫЙ ПАНЕЛЬ ПРИСЛАЛА. Продавцу
# это выглядело как список чужих игр вместо его раздела.
_OPTIONS_SHOWN = 1000


async def _search_options(uid: int, data: dict, attr: str,
                          terms: list) -> tuple[list, str]:
    """Спросить у панели варианты ПО ИМЕНИ. → (варианты, по какому слову).

    Без слова для поиска панель отдаёт первые несколько сотен вариантов по
    алфавиту, и «Standoff 2» в них не попадает: продавцу показывался список
    из пятисот чужих игр вместо раздела, который у товара уже стоит.
    Ровно этим адресом пользуется и поиск словом — только слово бот берёт у
    образца, а не у продавца.
    """
    from storage import get_panel_creds
    from automation.panel import panel_sync_field_options_sync

    creds = get_panel_creds(uid) or {}
    if not creds.get("cookies"):
        return [], ""
    loop = asyncio.get_event_loop()
    seen: set = set()
    for term in terms[:4]:
        t = str(term or "").strip()
        # Короткое слово подойдёт к чему угодно, и поиск по нему вернёт
        # такой же обрезок, как без него.
        if len(t) < 3 or t.lower() in seen:
            continue
        seen.add(t.lower())
        try:
            rows, _trace = await asyncio.wait_for(
                loop.run_in_executor(
                    None, panel_sync_field_options_sync, creds["cookies"],
                    data.get("form_resource", "items"), attr,
                    data.get("chosen") or {}, t),
                timeout=20,
            )
        except Exception as e:                            # noqa: BLE001
            logger.info("поиск вариантов «%s» не удался: %s", t, e)
            rows = []
        if rows:
            return rows, t
    return [], ""


async def _ask_next_select(msg, state: FSMContext, uid: int,
                           api=None) -> None:
    """Спросить следующее обязательное поле-список — или уже создать товар.

    `api` едет параметром, а не через состояние: клиент Integration API
    живёт один запрос, а форма мастера переживает десятки. Нужен он в
    самом конце — проставить остаток созданному товару.
    """
    from storage import get_panel_creds
    from automation.panel import panel_sync_field_options_sync

    data = await state.get_data()
    queue: list = data.get("select_queue") or []
    chosen: dict = data.get("chosen") or {}
    fields = {f["attribute"]: f for f in (data.get("form_fields") or [])}

    if not queue:
        values = data.get("pending") or {}
        picked = list(data.get("autopicked") or [])
        await _panel_create_and_report(msg, uid, values, extra=chosen,
                                       picked=picked, state=state, api=api)
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
        # У копии ответ уже есть — взятый у образца, из этой же панели.
        # Сверить его не с чем, но отправить лучше, чем упереться в тупик:
        # без раздела панель товар не примет вовсе.
        if chosen.get(attr) not in (None, ""):
            await state.update_data(select_queue=queue[1:])
            await _ask_next_select(msg, state, uid, api)
            return
        # Обязательное поле без вариантов — это тупик, показываем диагностику
        if f.get("required") or attr in ("category", "subcategory", "type"):
            await state.clear()
            b = InlineKeyboardBuilder()
            b.button(text="🌐 Создать вручную в панели",
                     url="https://panel.yoomarket.net/goods/create")
            b.button(text="⬅️ Назад", callback_data="menu:ads")
            ui.lay(b)
            await msg.edit_text(
                f"❌ <b>Не удалось получить варианты поля «{f.get('label') or attr}»</b>\n\n"
                f"component: <code>{f.get('component','?')}</code>\n"
                f"связь: <code>{f.get('relationship','—')}</code>\n"
                f"Запросы:\n<code>{trace[:400]}</code>\n\n"
                f"Пришли этот текст разработчику.",
                reply_markup=b.as_markup(),
            )
            return
        # Необязательное — пропускаем
        await state.update_data(select_queue=queue[1:])
        await _ask_next_select(msg, state, uid, api)
        return

    label = f.get("label") or attr
    hint = (data.get("source_labels") or {}).get(attr)
    words = list(data.get("autopick") or [])
    src = chosen.get(attr)

    # Сверяться надо со ВСЕМ, что панель прислала, а обрезать — только
    # показ. Обрезка до сверки и есть та ошибка, из-за которой копия не
    # находила номер, ПРИСЛАННЫЙ САМОЙ ПАНЕЛЬЮ: живой ответ — 825
    # вариантов, сверка шла по первым пятистам, «Standoff 2» на букву S.
    guess, took = _pick_option(options, src, hint, words)

    if guess is None:
        # В списке нужного нет — спрашиваем панель по имени, а не листаем
        # обрезок. Ровно то же делает продавец, когда пишет название словом.
        found, term = await _search_options(uid, data, attr,
                                            ([hint] if hint else []) + words)
        if found:
            guess, took = _pick_option(found, src, hint, [term] + words)
            if guess is None:
                # Выбрать не вышло, но найденное показать лучше, чем сотни
                # чужих строк по алфавиту: нужного среди них и не было.
                options = found

    if guess is None and attr not in _SECTION_TRIPLE:
        # Поле, которого у образца нет вовсе: в форме создания есть
        # `has_chat`, `created_order` и подобные, а у товара их не бывает —
        # спросить о них значит спросить о том, чего копировать неоткуда.
        # Что предлагает сама форма, то и уходит при обычном создании.
        default = f.get("value")
        if default not in (None, "", [], {}):
            guess, took = ({"value": default, "label": str(default)},
                           "по умолчанию формы")

    if guess is None and src not in (None, ""):
        # Номер взят с карточки ЭТОЙ ЖЕ панели, у ЭТОГО ЖЕ товара, и поле у
        # формы создания то же самое — значит и нумерация та же. Не сошлось
        # ни со списком, ни с поиском — отправляем как есть и говорим об
        # этом: панель, если номер не тот, ответит отказом по полю, и отказ
        # станет вопросом. Выбросить номер — это тупик вместо вопроса, и
        # ровно в него копия и упиралась.
        guess = {"value": src, "label": hint or str(src)}
        took = "как у образца, со списком не сверился"

    if guess is not None:
        chosen[attr] = guess.get("value")
        notes = list(data.get("autopicked") or [])
        notes.append(f"{label}: {guess.get('label')} ({took})")
        # Надпись — рядом с номером: по ней продавец узнаёт раздел в
        # отчёте, и её же запоминаем за образцом. Номер ему ничего не
        # говорит, а «Standoff 2» говорит всё.
        names = dict(data.get("chosen_labels") or {})
        names[attr] = str(guess.get("label") or "")
        await state.update_data(chosen=chosen, autopicked=notes,
                                chosen_labels=names, select_queue=queue[1:])
        await _ask_next_select(msg, state, uid, api)
        return

    # В состояние — сколько влезает; сверка уже прошла по всему списку.
    shown = options[:_OPTIONS_SHOWN]
    await state.update_data(
        current_attr=attr, current_label=label,
        current_options=shown, current_view=shown, current_page=0,
        select_queue=queue[1:],
    )
    await _render_select(msg, state, edit=True)


_PER_PAGE = 16


async def _render_select(msg, state: FSMContext, edit: bool = True) -> None:
    """Нарисовать страницу списка: варианты, листалка и подсказка про поиск."""
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
        f"📋 Выбери <b>{label}</b> (всего: {len(view)}):\n"
        f"<i>Не нашли нужное? Напиши название сообщением — я поищу.</i>"
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
    # Запоминаем сообщение со списком, чтобы поиск правил именно его
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
    """Поиск словом по вариантам списка — и по загруженным, и по запросу в панель."""
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
        await message.answer(f"🔍 По запросу «{q}» ничего не найдено. Попробуй иначе.")
        return

    await state.update_data(
        current_options=base, current_view=filtered, current_page=0,
    )
    # Убираем введённое слово, чтобы не засорять переписку
    try:
        await message.delete()
    except Exception:
        pass
    # Правим ТО ЖЕ сообщение со списком: номера кнопок должны совпадать с
    # текущей страницей, а новое сообщение оставило бы позади старую клавиатуру.
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
async def choose_select_option(callback: CallbackQuery, state: FSMContext,
                               api: YooMarketAPI = None) -> None:
    data = await state.get_data()
    attr = data.get("current_attr")
    options = data.get("current_view") or data.get("current_options") or []
    if not attr:
        await callback.answer("Сессия создания истекла — начни заново", show_alert=True)
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
    names = dict(data.get("chosen_labels") or {})
    names[attr] = str(options[idx].get("label") or "")
    await state.update_data(
        chosen=chosen, chosen_labels=names, current_attr=None,
        current_options=[], current_view=[], current_page=0,
    )
    await callback.answer(f"✅ {str(options[idx].get('label',''))[:30]}")
    await _ask_next_select(callback.message, state, callback.from_user.id,
                           api)


def _title_words(values: dict) -> list[str]:
    """Слова названия товара — подсказка для полей, которых у образца нет.

    От узкого к широкому, как того ждёт `_autopick_match`: длинные слова
    различают лучше («Standoff» точнее «аккаунта»). Короткие и служебные
    выброшены — по «на» подойдёт что угодно, а подошедшее «что угодно»
    положит товар не в тот раздел.
    """
    import re as _re

    raw = f"{values.get('title') or ''}"
    words = [w for w in _re.split(r"[^\w]+", raw, flags=_re.UNICODE)
             if len(w) >= 4]
    return sorted(dict.fromkeys(words), key=len, reverse=True)[:8]


async def _remember_selects(uid: int, state, extra: dict | None) -> dict:
    """Запомнить раздел, подраздел и тип за образцом. → что запомнили.

    Зовётся только после того, как панель товар ПРИНЯЛА. Запоминается
    ровно тройка: остальное у товара читается заново каждый раз, а эти три
    поля панель после создания не показывает вовсе.
    """
    out: dict = {"labels": {}, "source": ""}
    if state is None:
        return out
    try:
        data = await state.get_data()
    except Exception:                                     # noqa: BLE001
        return out
    out["labels"] = dict(data.get("chosen_labels") or {})
    source = str(data.get("copy_source_id") or "")
    if not source:
        return out                     # обычное создание мастером — не копия
    out["source"] = source
    picked = {k: v for k, v in (extra or {}).items() if k in _SECTION_TRIPLE}
    if not picked:
        return out
    try:
        from storage import remember_copy_marks
        remember_copy_marks(uid, source, picked, out["labels"])
    except Exception as e:                                # noqa: BLE001
        logger.info("раздел образца %s не запомнился: %s", source, e)
    return out


def _carried_note(extra: dict | None, labels: dict | None = None) -> str:
    """Строка «что бот заполнил сам» — числами, а не обещанием.

    Продавец трижды прочитал перечень полей ФОРМЫ панели как список того,
    что он должен заполнить: «и так же просит категории». Спорить об этом
    экранами бессмысленно — надо ПОКАЗАТЬ отправленное. Если раздела в
    строке нет, значит его правда нет, и видно это сразу обоим.
    """
    if not extra:
        return ""
    names = {"category": "раздел", "subcategory": "подраздел",
             "type": "тип выдачи"}
    # Надпись И номер. Номер здесь не украшение: продавец трижды прочитал
    # перечень полей формы как список того, что он должен заполнить сам, и
    # спор закрывают именно числа. А «Standoff 2» рядом с числом отвечает
    # на второй вопрос — туда ли ляжет товар.
    labels = labels or {}
    rows = [f"{names[k]}: {labels[k]} ({extra[k]})" if labels.get(k)
            else f"{names[k]}: {extra[k]}"
            for k in ("category", "subcategory", "type") if extra.get(k)]
    filters = sum(1 for k in extra if k.lower().startswith("filter__"))
    if filters:
        rows.append(f"полей раздела: {filters}")
    return ("\n🧩 Заполнено ботом — " + " · ".join(rows)) if rows else ""


def _picked_note(picked: list | None) -> str:
    """Строка о разделах, выбранных ботом. Пусто — если выбирал продавец."""
    if not picked:
        return ""
    rows = "; ".join(html.escape(str(p)) for p in picked)
    return f"\n🏷 <i>Раздел выбран автоматически — {rows}</i>"


# Сколько полей подряд готовы спросить по отказам панели. Три — это «панель
# называет их по одному», а не «мастер ходит по кругу».
_MAX_REFUSED_ROUNDS = 3


def _what_to_do(values: dict, fields: list, is_expired: bool) -> str:
    """Строка «что делать» — только там, где нам правда есть что сказать.

    Совет наугад хуже молчания: «попробуй ещё раз» на отказе по полю
    отправляет продавца делать бессмысленное. Поэтому советы здесь ровно на
    те случаи, где причина известна и поправима руками; на остальных
    возвращается пустая строка, и экран честно ограничивается тем, что
    сказала панель.
    """
    from automation.panel import link_trouble

    if is_expired:
        return ("<b>Что делать:</b> войти в панель заново — кнопка ниже. "
                "Сессия панели живёт несколько дней.")
    named = set(fields or [])
    if "content" in named:
        found = link_trouble(str(values.get("description") or ""))
        if found:
            return (f"<b>Что делать:</b> убрать ссылку "
                    f"<code>{html.escape(found)}</code> из описания — панель "
                    f"пускает только свой белый список.")
        return ("<b>Что делать:</b> поправить описание — панель не приняла "
                "именно его.")
    if "images" in named:
        return ("<b>Что делать:</b> добавить фото товара: без него панель "
                "этот раздел не принимает.")
    if "has_points" in named:
        return ("<b>Что делать:</b> добавить остатки — пустой товар "
                "маркетплейс не публикует.")
    return ""


async def _ask_for_refused_fields(msg, uid: int, values: dict,
                                  extra: dict | None, picked: list | None,
                                  state: FSMContext, result_msg: str,
                                  api=None) -> bool:
    """Отказ панели по полю → вопрос продавцу. True, если спросили.

    Обязательность полей у панели зависит от раздела: в форме создания
    `filter__8` приходит без метки required, а после выбора категории
    «Игровая валюта» отказ 422 называет его обязательным. Заранее об этом
    узнать неоткуда — зато отказ называет поле сам, и это ровно то, что
    нужно спросить.

    Спрашиваем один раз за создание: если и с заполненным полем панель
    откажет снова, продавец увидит отчёт, а не круг вопросов.
    """
    from automation.panel import panel_get_item_form_sync, validation_fields
    from storage import get_panel_creds

    data = await state.get_data()
    # Спрошенное уже не спрашиваем: панель называет недостающие поля списком,
    # и если после ответа отказ повторился тем же полем — вопрос не помог,
    # второй такой же будет кругом. А вот НОВОЕ имя в отказе означает, что
    # дело сдвинулось, и его спросить стоит. Потолок всё равно нужен: считать
    # прогрессом бесконечную череду новых полей нельзя.
    already = list(data.get("refused_asked") or [])
    if len(already) >= _MAX_REFUSED_ROUNDS:
        return False
    refused = [a for a in validation_fields(result_msg)
               if a not in (extra or {}) and a not in already]
    if not refused:
        return False

    fields = data.get("form_fields") or []
    resource = data.get("form_resource") or "items"
    if not fields:
        # Создание из плагина идёт мимо мастера, формы в состоянии нет —
        # читаем её сейчас, иначе спрашивать нечем: у поля нет ни названия,
        # ни вариантов, а `filter__8` продавцу ничего не говорит.
        creds = get_panel_creds(uid)
        if not creds or not creds.get("cookies"):
            return False
        loop = asyncio.get_event_loop()
        try:
            ok, form = await asyncio.wait_for(
                loop.run_in_executor(None, panel_get_item_form_sync,
                                     creds["cookies"]),
                timeout=30)
        except Exception:
            return False
        if not ok or not isinstance(form, dict):
            return False
        fields, resource = form["fields"], form["resource"]

    known = {f["attribute"] for f in fields}
    # Поля, значение которых мы УЖЕ отправили: название, цена, описание,
    # количество. Панель жалуется на них не «выбери», а «не годится» —
    # «content: Ссылки запрещены». Спрашивать их списком нечего: вариантов
    # у текстового поля нет, и продавец получал отладку «Пришли этот текст
    # разработчику» вместо русской причины, которую панель назвала сама.
    #
    # Слова те же, по которым форма создания раскладывает `values`: другой
    # набор однажды разошёлся бы с ней, и поле стало бы спрашиваться дважды.
    _SENT = ("title", "name", "header", "naimenov", "price", "cost", "cena",
             "desc", "opis", "text", "content", "count", "quantity", "qty",
             "stock")
    queue = [a for a in refused if a in known
             and not any(w in a.lower() for w in _SENT)]
    if not queue:
        return False

    # Панель только что назвала эти поля обязательными — это сильнее, чем
    # `rules` в её же форме, где их обязательность не объявлена вовсе. Без
    # этой отметки поле без готовых вариантов считалось бы необязательным и
    # **молча пропускалось**: товар ушёл бы заново без него и получил тот же
    # отказ, только двумя запросами позже.
    fields = [{**f, "required": True} if f["attribute"] in queue else f
              for f in fields]

    await state.set_state(CreateAdState.panel_select)
    await state.update_data(
        pending=values, form_resource=resource, form_fields=fields,
        chosen=dict(extra or {}), autopicked=list(picked or []),
        select_queue=queue, refused_asked=already + queue,
    )
    names = ", ".join(
        str(next((f.get("label") for f in fields if f["attribute"] == a), a))
        for a in queue)
    try:
        await msg.edit_text(
            f"⚠️ <b>Панель просит заполнить: {html.escape(names)}</b>\n\n"
            f"Это поле зависит от раздела, и до его выбора панель о нём "
            f"молчит. Спрошу — и отправлю товар заново.")
    except Exception:
        pass
    await _ask_next_select(msg, state, uid, api)
    return True


async def _fill_stock(api, item_id: str, want, ) -> str:
    """Проставить остаток новому товару. Отдаёт строку для отчёта.

    Без остатка панель товар не публикует, и продавцу приходилось жать
    «📦 Добавить остатки» и вводить число, которое мастер уже спрашивал.

    Три вещи, из-за которых это не одна строчка кода:

    * **сколько уже есть, читаем сначала.** Панель кладёт количество в свою
      форму при создании; добавить сверху столько же значит удвоить
      остаток. Дополняем до нужного, а не прибавляем;
    * **коды скопировать нельзя.** У товара с авто-выдачей остаток — это
      сами ключи, и они одноразовые. Придумать их бот не может, а взять из
      образца — значит продать один код дважды;
    * **перечитываем.** HTTP 200 не доказательство: в отчёте стоит то
      число, которое ответил маркетплейс, а не то, которое мы отправили.
    """
    try:
        want = int(float(want or 0))
    except (TypeError, ValueError):
        want = 0
    if not api or not item_id or want <= 0:
        return ""
    try:
        ad = await api.get_ad(item_id)
        kind = str(((ad.get("data") or ad) or {}).get("type") or "")
        if kind == "auto-delivery":
            return ("\n📦 Остатки — это коды, и они одноразовые: "
                    "скопировать их нельзя, пришли своим списком.")
        _has, said = await api.ad_stock(item_id, ad)
        now = _stock_number(said)
        if now >= want:
            return f"\n📦 Остаток на месте: {now}"
        await api.refill_ad_value(item_id, want - now)
        _has, said = await api.ad_stock(item_id)
        got = _stock_number(said)
        if got >= want:
            return f"\n📦 Остаток проставлен: {got}"
        # Молчать нельзя: без остатка товар не опубликуется, и продавец
        # будет искать причину на экране модерации.
        return (f"\n📦 Остаток проставить не вышло — сейчас {got} "
                f"из {want}. Добавь вручную.")
    except Exception as e:                                # noqa: BLE001
        logger.warning("остаток товару %s не проставлен: %s", item_id, e)
        return "\n📦 Остаток проставить не вышло — добавь вручную."


def _stock_number(said: str) -> int:
    """Число из «остаток: 500» или «позиций в наличии: 3». 0, если его нет.

    Разбирается строка `api.ad_stock` — та же, что показывают экраны. Свой
    второй разбор ответа маркетплейса означал бы, что однажды экран и отчёт
    назовут разные числа.
    """
    m = re.search(r"(\d+)", str(said or ""))
    return int(m.group(1)) if m else 0


async def _panel_create_and_report(msg, uid: int, values: dict,
                                   extra: dict | None,
                                   picked: list | None = None,
                                   state: FSMContext | None = None,
                                   api=None, cleaned: list | None = None) -> None:
    """Run panel_create_product_sync in a thread with live progress, then report.

    `cleaned` — слова, уже убранные из описания на прошлом заходе. Оно же
    и предохранитель от круга: чистка идёт ОДИН раз за создание.

    `picked` — разделы, выбранные ботом без спроса (создание из плагина).
    Печатаются в отчёте: продавец должен видеть, где оказался товар, а не
    обнаруживать это на витрине.

    `state` — форма мастера, если она ещё жива. С ней отказ по недостающему
    полю становится вопросом: панель назвала поле прямым текстом, спросить
    его и отправить заново дешевле, чем отправить продавца заводить товар
    руками. Без состояния (создание из плагина) поведение прежнее — отчёт.
    """
    from storage import get_panel_creds
    from automation.panel import panel_create_product_sync

    creds = get_panel_creds(uid)
    if not creds or not creds.get("cookies"):
        await msg.edit_text("❌ Куки панели не найдены — войди снова.")
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
            "Войди в панель снова или создай товар вручную."
        )

    if ok:
        # Запоминаем выбранное — ТОЛЬКО теперь, когда панель товар приняла.
        # Отказ означал бы, что значения не подошли, а запомненная неправда
        # хуже вопроса: раздел после создания не меняется, и товар остался
        # бы лежать не там.
        #
        # И ДО закрытия формы: закрытая — это пустое состояние, а раздел,
        # надписи и номер образца лежат именно в нём.
        names = await _remember_selects(uid, state, extra)
        # Форма отработала — закрываем её. Брошенный экран ловит любое
        # следующее сообщение, включая команду: так молча не работали
        # `/chat_debug` и `/withdraw_debug`.
        if state is not None:
            await state.clear()
        item_id = result_msg if str(result_msg).isdigit() else ""
        pub_note = ""
        stock_note = ""
        if item_id:
            # Остаток — ПЕРЕД публикацией: без него панель публиковать
            # отказывается, и «добавь остатки» после отказа было лишним
            # кругом с числом, которое мастер уже спрашивал.
            try:
                await status_msg.edit_text("⏳ Товар создан, ставлю остаток…")
            except Exception:
                pass
            stock_note = await _fill_stock(api, item_id,
                                           values.get("quantity", 0))
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
                pub_note = ("\n🕓 Отправлен на модерацию "
                            f"({html.escape(str(pub_msg)[:150])})")
            else:
                # У свежего товара обычно ещё нечего продавать, а пустой
                # маркетплейс публиковать отказывается. Говорим, что делать, а
                # не просто сообщаем об отказе.
                #
                # Раньше «отправлен на модерацию» писалось по одному коду
                # ответа панели, без проверки. Панель отвечает 200 и на отказ,
                # так что продавец считал товар отправленным, а он лежал
                # черновиком. Теперь публикация подтверждается перечитыванием,
                # и здесь остаются два честных шага по порядку.
                pub_note = (
                    "\n\n📦 <b>На модерацию пока не отправлен.</b>"
                    "\n1. Добавь остатки — без них публиковать нечего."
                    "\n2. Жми «🚀 На модерацию»."
                    f"\n\n<i>Панель ответила: "
                    f"{html.escape(str(pub_msg)[:150])}</i>"
                )

        b = InlineKeyboardBuilder()
        if item_id and "модерац" not in pub_note:
            b.button(text="📦 Добавить остатки",
                     callback_data=f"pitem_stock:{item_id}")
            b.button(text="🚀 На модерацию",
                     callback_data=f"cadpub:{item_id}")
        b.button(text="➕ Добавить ещё", callback_data="create_ad:start")
        if names.get("source"):
            # Ошибиться разделом можно один раз: панель менять его не даёт.
            # Значит забыть ответ продавец должен уметь сам.
            b.button(text="✏️ Раздел не тот",
                     callback_data=f"create_ad:forget:{names['source']}")
        b.button(text="📦 Мои товары", callback_data="menu:ads")
        ui.lay(b)
        await _edit_safely(
            msg,
            f"✅ <b>Товар создан через панель!</b>\n\n"
            f"📝 {html.escape(str(values['title']))}\n"            f"💰 {values['price']} ₽"
            f"{chr(10) + '🆔 ' + item_id if item_id else ''}"
            f"{_picked_note(picked)}"
            + _carried_note(extra, names.get("labels"))
            + (("\n✂️ Из описания убрано: "
                + ", ".join(f"«{html.escape(w)}»" for w in cleaned)
                + " — панель это слово не принимает.") if cleaned else "")
            + f"{stock_note}"
            f"{pub_note}",
            reply_markup=b.as_markup(),
        )
        return

    # ── Запрещённое слово — убираем и отправляем заново, без вопросов ─────
    #
    # Панель называет слово прямо в отказе, и другого пути у товара нет:
    # с этим словом она его не примет никогда. Показать отказ и ждать
    # нажатия значит остановить копию на том, что бот может сделать сам.
    #
    # Молчаливой правки при этом не происходит: убранное названо в отчёте.
    # И ровно один заход — `cleaned` не даёт кругу повториться.
    from automation.panel import (forbidden_words as _banned_words,
                                  strip_words as _strip,
                                  words_are_gone as _gone)

    if not cleaned:
        banned_now = _banned_words(result_msg)
        if banned_now:
            clean = _strip(values.get("description") or "", banned_now)
            if clean and _gone(clean, banned_now):
                try:
                    await status_msg.edit_text(
                        "✂️ Панель не принимает слово "
                        + ", ".join(f"«{html.escape(w)}»" for w in banned_now)
                        + " — убираю и отправляю заново…")
                except Exception:
                    pass
                await _panel_create_and_report(
                    msg, uid, dict(values, description=clean), extra=extra,
                    picked=picked, state=state, api=api,
                    cleaned=list(banned_now))
                return

    # ── Отказ по недостающему полю — спрашиваем его, а не сдаёмся ──────────
    # Панель называет поле прямым текстом: «filter__8: Поле Регион
    # обязательно для заполнения». Обязательность у неё зависит от раздела и
    # в форме заранее не объявлена (`Обязательные: []`), поэтому узнать про
    # такое поле можно только отсюда. Один раз: если и со спрошенным полем
    # отказ повторится, продавец получит отчёт, а не круг вопросов.
    # Надписи разделов — ДО закрытия формы: закрытая это пустое состояние,
    # и отказ показывал бы голые номера там, где продавец как раз и решает,
    # туда ли шёл товар. Запоминать при этом нечего: панель товар не приняла.
    seen_names = (await state.get_data()).get("chosen_labels") if state else {}
    if state is not None:
        asked = await _ask_for_refused_fields(msg, uid, values, extra, picked,
                                              state, result_msg, api)
        if asked:
            return
        await state.clear()

    # ── Ошибка — строим правильный набор кнопок ─────────────────────────────
    is_expired = any(w in result_msg for w in ("истекла", "Сессия", "войди снова", "Войди"))
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
    ui.lay(b)

    # Отчёт читается сверху вниз, и сверху должно стоять то, ради чего
    # продавец его открыл: что не так и что теперь делать. Диагностика
    # нужна тоже — но ей место под спойлером, а не поверх ответа.
    #
    # Прежний экран был устроен наоборот: две строки по делу и двадцать
    # строк дампа, причём наша собственная разметка внутри дампа вылезала
    # буквами — «Ресурс <b>items</b> найден» продавец видел именно так,
    # угловыми скобками.
    from automation.panel import as_plain, explain_validation, validation_fields

    why = explain_validation(result_msg)
    # Панель называет запрещённое слово прямо в отказе. Заставлять после
    # этого перенабирать описание целиком — работа на ровном месте: слово
    # известно, убрать его и отправить заново можно одним нажатием.
    #
    # Кнопка появляется, только если убранное ДЕЙСТВИТЕЛЬНО пропало:
    # предлагать «убрать и создать», не убедившись, что убрали, значит
    # обещать исход, которого не будет.
    header = ("⚠️ <b>Панель не приняла товар</b>" if why or is_found
              else "❌ <b>Не удалось создать товар</b>")

    parts = [f"{header}{_picked_note(picked)}"
             f"{_carried_note(extra, seen_names)}"]
    if why:
        parts.append("")
        parts.append(html.escape(why))
    advice = _what_to_do(values, validation_fields(result_msg), is_expired)
    if advice:
        parts.append("")
        parts.append(advice)
    # Подробности — под спойлером: они нужны раз в сто отказов, а место
    # занимают всегда. Внутри чужой текст, поэтому экранируется.
    #
    # И подписаны они прямо: внутри лежит перечень полей ФОРМЫ панели, и
    # продавец прочитал его как список того, что он должен заполнить сам
    # («и так же просит категории»). Отказ, который читается как требование
    # работы, — хуже отказа: он посылает делать лишнее.
    details = html.escape(as_plain(result_msg, 1200))
    if details:
        parts.append("")
        parts.append("<i>Заполнять ничего не нужно — раздел, подраздел и тип "
                     "бот отправил сам. Ниже ответ панели, для разбора:</i>")
        parts.append(f"<tg-spoiler>{details}</tg-spoiler>")

    await _edit_safely(msg, "\n".join(parts), b.as_markup())


@router.callback_query(F.data.startswith("cadpub:"))
async def publish_item(callback: CallbackQuery) -> None:
    """Повторить публикацию уже созданного товара вручную."""
    from storage import get_panel_creds
    from automation.panel import panel_publish_item_sync

    item_id = callback.data.split(":", 1)[1]
    uid = callback.from_user.id
    creds = get_panel_creds(uid)
    if not creds or not creds.get("cookies"):
        await callback.answer("❌ Нет сессии панели — войди снова", show_alert=True)
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
        # Публиковать пустой товар маркетплейс отказывается, так что первая
        # кнопка — остатки, а не повтор того же самого.
        b.button(text="📦 Добавить остатки", callback_data=f"pitem_stock:{item_id}")
        b.button(text="🚀 Попробовать снова", callback_data=f"cadpub:{item_id}")
    b.button(text="➕ Добавить ещё", callback_data="create_ad:start")
    b.button(text="📦 Мои товары", callback_data="menu:ads")
    ui.lay(b)
    # Ответ панели — чужой текст с чужой разметкой. Раньше он вклеивался в
    # заголовок скобками, обрезанный по счёту символов: заголовок из-за него
    # переставал читаться, а обрывок тега ронял всё сообщение.
    from automation.panel import as_plain

    said = html.escape(as_plain(msg_text, 600))
    tail = f"\n\n<tg-spoiler>{said}</tg-spoiler>" if said else ""
    result = (f"🕓 <b>Товар {item_id} отправлен на модерацию</b>\n"
              f"Появится в маркете после проверки.{tail}" if ok
              else (f"⚠️ <b>Товар {item_id} на модерацию не отправлен</b>\n\n"
                    f"Чаще всего причина одна: у товара нет остатков, а пустой "
                    f"маркетплейс не публикует.{tail}"))
    await _edit_safely(callback.message, result, b.as_markup())


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

async def _ads_by_section(api, uid: int):
    """Объявления продавца, разложенные по разделам. → (группы, отказ).

    Разделы считаются ТЕМ ЖЕ кодом, что и на экране «📦 Товары»
    (`_ad_category`, `_category_names`): второй разбор того же ответа
    однажды разошёлся бы с первым, и один и тот же товар лежал бы в двух
    экранах в разных разделах.
    """
    from handlers.panel_items import (_ad_category, _category_names,
                                      _wanted_cats)

    try:
        data = await api.get_ads()
        ads = data.get("data") or data.get("items") or []
        names = await _category_names(api, uid, _wanted_cats(ads))
    except Exception as e:                                # noqa: BLE001
        return {}, _readable(str(e))

    groups: dict[str, list] = {}
    for ad in ads:
        raw = ad.get("id")
        if raw is None or not str(raw).strip():
            continue                      # кнопка без номера скопирует не то
        groups.setdefault(_ad_category(ad, names), []).append(ad)
    return groups, ""


def _section_screen(groups: dict, back: str):
    """Экран разделов: по кнопке на раздел, с числом объявлений."""
    b = InlineKeyboardBuilder()
    rows = []
    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    for i, (name, ads) in enumerate(ordered):
        b.button(text=f"📂 {name[:24]} ({len(ads)})",
                 callback_data=f"create_ad:sect:{i}")
        rows.append(f"• <b>{html.escape(name[:40])}</b> — {len(ads)}")
    b.button(text="✍️ Создать с нуля", callback_data="create_ad:new")
    b.button(text="❌ Отмена", callback_data=back)
    ui.lay(b)
    return rows, b, [name for name, _ads in ordered]


@router.callback_query(F.data == "create_ad:templates_list")
async def templates_list(callback: CallbackQuery, state: FSMContext,
                         api: YooMarketAPI = None) -> None:
    """Разделы, в которых у продавца есть объявления.

    Плоским списком это не читается: объявлений у продавца бывает полсотни,
    а копируют обычно соседнее по разделу. Прежняя версия к тому же резала
    список на двенадцати МОЛЧА — то есть половина товаров просто не
    существовала для копии.

    Список читается ТЕМ ЖЕ API, которым создаётся копия. Через панель он не
    годится: у панели свои номера товаров, и номер из её списка, отданный в
    `GET /ads/{id}`, указал бы не туда.
    """
    from features import ad_templates_shown

    uid = callback.from_user.id
    # Заслон и на самом экране, а не только на кнопке: кнопка осталась в
    # прежних сообщениях, а нажатие создаёт настоящее объявление.
    if not ad_templates_shown(uid):
        await callback.answer("Этого раздела сейчас нет", show_alert=True)
        return
    if not api:
        await callback.answer("Не настроен API-токен — копия идёт через него",
                              show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text("⏳ Читаю объявления…")
    groups, err = await _ads_by_section(api, uid)

    if err:
        b = InlineKeyboardBuilder()
        b.button(text="✍️ Создать с нуля", callback_data="create_ad:new")
        b.button(text="❌ Отмена", callback_data="menu:ads")
        ui.lay(b)
        await callback.message.edit_text(ui.screen(
            "📋 <b>Шаблонная копия</b>",
            ["Список объявлений прочитать не вышло.", "",
             f"<i>{html.escape(err)}</i>"]),
            reply_markup=b.as_markup())
        return
    if not groups:
        b = InlineKeyboardBuilder()
        b.button(text="✍️ Создать с нуля", callback_data="create_ad:new")
        b.button(text="❌ Отмена", callback_data="menu:ads")
        ui.lay(b)
        await callback.message.edit_text(ui.screen(
            "📋 <b>Шаблонная копия</b>",
            ["На витрине нет ни одного объявления — копировать пока нечего."]),
            reply_markup=b.as_markup())
        return

    rows, b, order = _section_screen(groups, "menu:ads")
    # Разложенные объявления кладём в форму, а не в память процесса: она
    # пересобирается при каждом выкате, и список «устарел» у всех разом.
    #
    # Кладём ТОЛЬКО номер, название и цену: форма может лежать в Redis, и
    # хранить там карточки целиком — это чужие килобайты на каждое нажатие
    # при том, что для копии нужен один номер.
    await state.update_data(
        copy_groups=order,
        copy_ads={name: [{"id": a.get("id"),
                          "title": a.get("title") or a.get("name"),
                          # Номер раздела маркетплейса: раздела у товара в
                          # панели нет НИГДЕ, и это единственный источник.
                          # Он уже прочитан ради группировки — спрашивать
                          # его второй раз незачем.
                          "category_id": a.get("category_id"),
                          "price": a.get("price")} for a in ads]
                  for name, ads in groups.items()})
    # Раздел один — показывать выбор из одного не из чего: сразу объявления.
    if len(order) == 1:
        await _show_section(callback, state, 0)
        return
    await callback.message.edit_text(ui.screen(
        "📋 <b>Шаблонная копия</b>",
        ["Заведу такое же объявление: те же название, цена, описание, "
         "раздел и фото.", "", "<b>Разделы</b>"] + rows),
        reply_markup=b.as_markup())


async def _show_section(callback: CallbackQuery, state: FSMContext,
                        idx: int) -> None:
    """Объявления одного раздела."""
    from orderfields import ad_price

    data = await state.get_data()
    order = list(data.get("copy_groups") or [])
    ads_by = dict(data.get("copy_ads") or {})
    if not (0 <= idx < len(order)):
        await callback.answer("Список устарел — открой копию заново",
                              show_alert=True)
        return
    name = order[idx]
    ads = ads_by.get(name) or []

    b = InlineKeyboardBuilder()
    rows = []
    for j, ad in enumerate(ads[:_COPY_LIMIT]):
        title = str(ad.get("title") or ad.get("name") or "без названия")
        b.button(text=f"📋 {title[:30]}",
                 callback_data=f"create_ad:copy:{idx}:{j}"[:64])
        rows.append(f"• <b>{html.escape(title[:40])}</b> — "
                    f"{int(ad_price(ad) or 0)} ₽")
    # Обрезали — говорим. Молчаливое обрезание означает, что половины
    # товаров для копии просто нет, и понять это неоткуда.
    if len(ads) > _COPY_LIMIT:
        rows += ["", f"<i>Показаны первые {_COPY_LIMIT} из {len(ads)}.</i>"]
    if len(order) > 1:
        b.button(text="⬅️ К разделам", callback_data="create_ad:templates_list")
    b.button(text="✍️ Создать с нуля", callback_data="create_ad:new")
    b.button(text="❌ Отмена", callback_data="menu:ads")
    ui.lay(b)
    await callback.message.edit_text(ui.screen(
        f"📂 <b>{html.escape(name[:40])}</b>",
        ["Выбор здесь и есть подтверждение — объявление уйдёт сразу.", ""]
        + rows), reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("create_ad:sect:"))
async def open_section(callback: CallbackQuery, state: FSMContext) -> None:
    from features import ad_templates_shown

    if not ad_templates_shown(callback.from_user.id):
        await callback.answer("Этого раздела сейчас нет", show_alert=True)
        return
    await callback.answer()
    try:
        idx = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer("Такого раздела нет", show_alert=True)
        return
    await _show_section(callback, state, idx)


async def _ad_card(api, ad_id: str) -> dict:
    """Карточка объявления из Integration API. Пустая — значит не вышло."""
    if not api:
        return {}
    try:
        raw = await api.get_ad(str(ad_id))
    except Exception as e:                                # noqa: BLE001
        logger.warning("карточка объявления %s не прочиталась: %s", ad_id, e)
        return {}
    return (raw.get("data") or raw) if isinstance(raw, dict) else {}


async def _price_from_api(api, ad_id: str):
    """Цена объявления. Читается `ad_price` — маркетплейс отдаёт её
    объектом, и прочитанная как скаляр она превращается в ноль."""
    from orderfields import ad_price

    return ad_price(await _ad_card(api, ad_id))


async def _stock_from_api(api, ad_id: str):
    card = await _ad_card(api, ad_id)
    try:
        return int(float(card.get("stock") or 0)) or None
    except (TypeError, ValueError):
        return None


async def _section_words(api, ad_id: str, cid=None) -> list:
    """Названия разделов маркетплейса для этого товара — цепочкой.

    Третий источник раздела и самый устойчивый: номера у панели и у
    маркетплейса свои, а слова совпадают.

    Берётся именно ЦЕПОЧКА, а не имя листа. Товар лежит в листе
    («Аккаунты»), а панель раскладывает товары по играм («Standoff 2»):
    нужное слово стоит на среднем уровне дерева. Версия, читавшая только
    лист, искала в списке из 825 игр слово «Аккаунты» — и, разумеется, не
    находила ничего.

    Порядок — от узкого к широкому, как того ждёт `_autopick_match`: лист
    отличает подраздел, ветвь выше — раздел.
    """
    if not api:
        return []
    if cid in (None, "", 0):
        cid = (await _ad_card(api, ad_id)).get("category_id")
    if cid in (None, "", 0):
        return []
    out: list = []
    try:
        # Со сроком: обход дерева бывает долгим, а продавец ждёт экрана.
        path = await asyncio.wait_for(api.category_path(cid), timeout=40)
    except Exception as e:                                # noqa: BLE001
        logger.info("путь раздела %s не прочитался: %s", cid, e)
        path = []
    # Цепочка идёт от верхушки к листу; нам нужен обратный порядок.
    out += [w for w in reversed(path) if w]
    if not out:
        try:
            one = str(await api.resolve_category(cid) or "")
        except Exception as e:                            # noqa: BLE001
            logger.info("раздел %s не назвался: %s", cid, e)
            one = ""
        if one:
            out.append(one)
    return out


async def _copy_source_values(uid: int, ad_id: str, api=None,
                              cid=None) -> tuple[dict, dict, dict, list, str]:
    """Значения исходного товара для нового.

    → (values, extra, labels, слова-подсказки, причина отказа)

    Копия — это пробег по тем же шагам создания, но с готовыми значениями:
    читаем товар в панели ровно в том виде, в каком форма создания их ждёт,
    и картинку кладём файлом — панель принимает её только настоящей
    загрузкой.

    `labels` — надписи выбранного (раздел, подраздел, тип): по ним бот
    находит ту же строку в форме создания, когда номера в ней другие.
    Слова-подсказки — то же самое для полей, которых у образца нет вовсе.

    Картинка НЕ удаляется по дороге: отказ панели по недостающему полю
    превращается в вопрос, после ответа товар уходит заново — и файл нужен
    во второй раз. Лежит она там же, где фото мастера, и переживает
    перезапуск.
    """
    import os

    from automation.panel import (panel_fetch_image_sync,
                                  panel_item_values_sync)
    from storage import _DATA_DIR, get_panel_creds

    creds = get_panel_creds(uid) or {}
    cookies = creds.get("cookies")
    if not cookies:
        return {}, {}, {}, [], "куки панели не найдены — войди в панель заново"

    loop = asyncio.get_event_loop()
    ok, values, extra, labels, url, err = await loop.run_in_executor(
        None, panel_item_values_sync, cookies, str(ad_id), uid)
    if not ok:
        return {}, {}, {}, [], err or "панель не отдала поля товара"

    # Каждое поле берётся оттуда, где оно есть. У товара в панели ЦЕНЫ НЕТ
    # ВОВСЕ — давняя запись в CLAUDE.md, из-за неё же снята и правка цены
    # из бота, — зато она есть в Integration API, объектом. Ноль,
    # подставленный по умолчанию, уехал бы на витрину ценой.
    if values.get("price") in (None, "", 0):
        values["price"] = await _price_from_api(api, ad_id)
    if values.get("quantity") in (None, ""):
        values["quantity"] = await _stock_from_api(api, ad_id)
    try:
        if float(values["price"]) <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return {}, {}, {}, [], ("цену товара не отдали ни панель, ни "
                                "маркетплейс — копия ушла бы бесплатной")
    values["price"] = int(float(values["price"]))
    values["quantity"] = int(values.get("quantity") or 1)

    # Слова-подсказки: сначала название раздела с маркетплейса, потом слова
    # названия товара. Порядок тот, которого ждёт `_autopick_match`, — от
    # узкого к широкому: раздел назван точно, название лишь намекает.
    words = [w for w in await _section_words(api, ad_id, cid) if w]
    words += [w for w in _title_words(values) if w not in words]

    if not url:
        return {}, {}, {}, [], ("у товара в панели не нашлось картинки, а без "
                                "неё объявление не создать")
    data = await loop.run_in_executor(None, panel_fetch_image_sync,
                                      cookies, url)
    if not data:
        return {}, {}, {}, [], "картинку товара скачать не вышло"

    photos = os.path.join(_DATA_DIR, "photos")
    os.makedirs(photos, exist_ok=True)
    path = os.path.join(photos, f"copy_{uid}_{ad_id}.jpg")
    try:
        with open(path, "wb") as fh:
            fh.write(data)
    except OSError as e:
        return {}, {}, {}, [], f"картинку некуда сохранить: {str(e)[:100]}"
    values["photo_path"] = path
    return values, extra, labels, words, ""


@router.callback_query(F.data.startswith("create_ad:copy:"))
async def copy_item(callback: CallbackQuery, state: FSMContext,
                    api: YooMarketAPI = None) -> None:
    """Копия товара: те же шаги создания, но с готовыми значениями.

    Идёт тем же путём, что и мастер, — та же форма панели, та же очередь
    списков, тот же вызов создания. Разница одна: на каждый вопрос ответ уже
    есть, взятый у образца, и потому вопрос не задаётся.

    Форму панели копия читает ОБЯЗАТЕЛЬНО. Отправлять готовые номера, не
    сверив их со списком формы, значит однажды положить товар в чужой
    раздел: номера панели и маркетплейса совпадать не обязаны, а ошибка
    видна только по отсутствию продаж.
    """
    from features import ad_templates_shown

    uid = callback.from_user.id
    if not ad_templates_shown(uid):
        await callback.answer("Этого раздела сейчас нет", show_alert=True)
        return

    # Номер объявления берём из разложенного списка, а не из кнопки: так
    # кнопка из старого сообщения не может указать на объявление, которого
    # в списке не было.
    tail = callback.data.split(":")[2:]
    ad_id = ""
    try:
        idx, j = int(tail[0]), int(tail[1])
        data = await state.get_data()
        order = list(data.get("copy_groups") or [])
        ads = (dict(data.get("copy_ads") or {})).get(order[idx]) or []
        ad_id = str(ads[j].get("id") or "")
        cid = ads[j].get("category_id")
    except (ValueError, IndexError, KeyError, TypeError):
        ad_id, cid = "", None
    if not ad_id:
        await callback.answer("Список устарел — открой копию заново",
                              show_alert=True)
        return

    await callback.answer("Создаю копию…")
    await callback.message.edit_text("⏳ Читаю товар в панели…")
    values, extra, labels, words, why = await _copy_source_values(
        uid, ad_id, api, cid)
    if why:
        b = InlineKeyboardBuilder()
        b.button(text="📋 Ещё копию", callback_data="create_ad:templates_list")
        b.button(text="✍️ Создать с нуля", callback_data="create_ad:new")
        b.button(text="📦 Мои товары", callback_data="menu:ads")
        ui.lay(b)
        await callback.message.edit_text(ui.screen(
            "❌ <b>Копия не создалась</b>",
            ["Товар прочитать не вышло:", f"<i>{html.escape(why)}</i>", "",
             "Заведи товар мастером — он спросит недостающее."]),
            reply_markup=b.as_markup())
        return

    # Раздел, подраздел и тип, выбранные для этого образца раньше. Раздела
    # у товара в панели нет нигде — узнать его второй раз неоткуда, и без
    # памяти продавца спрашивали бы при каждой копии одного и того же
    # товара. Прочитанное у панели важнее запомненного: оно свежее.
    from storage import get_copy_marks

    marks = get_copy_marks(uid, ad_id)
    chosen = {**marks.get("values", {}), **extra}
    hints = {**marks.get("labels", {}), **labels}

    # Состояние живое: отказ по недостающему полю станет вопросом, а не
    # тупиком, и после ответа товар уйдёт заново — с той же картинкой.
    await state.set_state(CreateAdState.panel_select)
    await state.update_data(pending=values, chosen=chosen,
                            select_queue=[], autopick=words,
                            source_labels=hints,
                            chosen_labels=dict(marks.get("labels") or {}),
                            copy_source_id=str(ad_id))

    # Сверка идёт по живой форме панели, а это ещё пара запросов: без
    # строки о ней экран стоял бы «Читаю товар» и выглядел бы зависшим.
    await _edit_safely(callback.message, "⏳ Сверяю разделы с формой панели…")
    form = await _creation_form(uid)
    if form:
        queue = _select_queue(form["fields"])
        await state.update_data(form_resource=form["resource"],
                                form_fields=form["fields"],
                                select_queue=queue)
        # Дальше — та же дорога, что у мастера: на каждом списке бот сперва
        # смотрит, что стояло у образца, и спрашивает только там, где взять
        # ответ неоткуда.
        await _ask_next_select(callback.message, state, uid, api)
        return

    # Формы нет — создаём напрямую, панель сама скажет, чего не хватает.
    await _panel_create_and_report(callback.message, uid, values, extra=chosen,
                                   state=state, api=api)


async def _creation_form(uid: int) -> dict | None:
    """Форма создания товара из панели, или None.

    Живёт отдельно от мастера потому, что нужна двоим: мастер спрашивает по
    ней продавца, копия — сверяет по ней готовые значения.
    """
    from automation.panel import panel_get_item_form_sync
    from storage import get_panel_creds

    creds = get_panel_creds(uid) or {}
    if not creds.get("cookies"):
        return None
    loop = asyncio.get_event_loop()
    try:
        ok, form = await asyncio.wait_for(
            loop.run_in_executor(None, panel_get_item_form_sync,
                                 creds["cookies"]),
            timeout=30,
        )
    except Exception as e:                                # noqa: BLE001
        logger.info("форма создания не прочиталась: %s", e)
        return None
    return form if (ok and isinstance(form, dict) and form.get("fields")) else None


def _select_queue(fields: list) -> list:
    """Списки формы в порядке зависимости: раздел → подраздел → тип, потом
    прочие обязательные. Тот же порядок, что у мастера, — и он важен:
    варианты подраздела панель отдаёт только после выбранного раздела."""
    by_attr = {f["attribute"]: f for f in fields}
    queue = [a for a in _SECTION_TRIPLE if a in by_attr]
    queue += [
        f["attribute"] for f in fields
        if f.get("required") and f.get("options") and f["attribute"] not in queue
        and f["attribute"] not in ("title", "price", "content")
    ]
    return queue



@router.callback_query(F.data.startswith("create_ad:forget:"))
async def forget_marks(callback: CallbackQuery) -> None:
    """Забыть раздел, запомненный за образцом.

    Ошибиться разделом можно ровно один раз: панель менять его после
    создания не даёт, и товар придётся заводить заново. Значит отменить
    свой же ответ продавец должен уметь сам — иначе одна ошибка
    закрепляется за товаром навсегда.
    """
    from features import ad_templates_shown
    from storage import forget_copy_marks

    uid = callback.from_user.id
    if not ad_templates_shown(uid):
        await callback.answer("Этого раздела сейчас нет", show_alert=True)
        return
    ad_id = callback.data.split(":", 2)[2]
    if forget_copy_marks(uid, ad_id):
        await callback.answer(
            "Забыл. При следующей копии этого товара спрошу раздел заново.",
            show_alert=True)
    else:
        await callback.answer("За этим товаром ничего и не помнилось",
                              show_alert=True)


@router.message(Command("copy_debug"))
async def copy_debug(message: Message, api: YooMarketAPI = None) -> None:
    """/copy_debug <номер объявления> — что копия видит у товара.

    Копия читает панель тремя ответами: форма правки, карточка, форма
    создания со списками. Когда она спрашивает раздел у товара, где раздел
    есть, догадаться, который из трёх молчит, нельзя — а каждая догадка
    стоила дня. Здесь печатается каждый.

    Только чтение: те же GET, что делает сама копия, ничего не создаётся.
    Команда скрытая — печатает разбор ответов панели, а это устройство
    бота, не продавца.
    """
    from automation.panel import panel_copy_probe_sync
    from storage import get_panel_creds

    uid = message.from_user.id
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].strip().isdigit():
        # Без номера — список своих товаров с номерами. Пример в подсказке
        # один раз уже увели в тупик: номер из него был выдуманный, панель
        # ответила 403/404, и следующий час ушёл на разбор чужой ошибки.
        await message.answer(await _my_ad_numbers(api))
        return
    ad_id = parts[1].strip()

    creds = get_panel_creds(uid) or {}
    if not creds.get("cookies"):
        await message.answer("Куки панели не найдены — войди в панель заново.")
        return

    status = await message.answer(f"⏳ Смотрю товар {ad_id} глазами копии…")
    loop = asyncio.get_event_loop()
    try:
        rows = await asyncio.wait_for(
            loop.run_in_executor(None, panel_copy_probe_sync,
                                 creds["cookies"], ad_id, uid),
            timeout=120,
        )
    except Exception as e:                                # noqa: BLE001
        await status.edit_text(f"❌ {html.escape(str(e)[:300])}")
        return

    # 403 и 404 значат «панель не показывает этот товар», а не «копия
    # сломалась»: чаще всего номер просто не свой. Молчать об этом нельзя —
    # ровно на этом и потерялся час.
    # Но «не свой» — это когда молчат ОБА ответа. Карточка закрывается
    # отдельно от формы правки, и одна её 403 у своего же товара — повод
    # читать раздел из списка, а не объявлять товар чужим.
    if _not_ours(rows):
        rows = list(rows) + [
            "",
            "403/404 на обоих ответах — панель не показывает этот товар.",
            "Скорее всего номер не твой. Свои: /copy_debug без номера.",
        ]
    else:
        rows = list(rows) + [""] + await _select_verdicts(uid, ad_id, api)

    text = "🔍 <b>Что копия видит у товара " + html.escape(ad_id) + "</b>\n\n"
    body = "\n".join(html.escape(r) for r in rows)
    # 4096 знаков — потолок Telegram; обрезаем хвост, а не роняем отправку
    await status.edit_text((text + f"<code>{body}</code>")[:4000])


def _not_ours(rows: list) -> bool:
    """Панель не показала товар НИ ОДНИМ ответом — значит номер не свой.

    Одной закрытой карточки для такого вывода мало: Nova разрешает форму
    правки, карточку и список независимо друг от друга.
    """
    def line(head: str) -> str:
        return next((r for r in rows if r.startswith(head)), "")

    return ("HTTP 40" in line("форма правки:")
            and "HTTP 40" in line("карточка:"))


async def _select_verdicts(uid: int, ad_id: str, api) -> list[str]:
    """Спросит копия раздел или нет — и почему. Тем же кодом, что и она.

    Главный вопрос диагностики именно этот, и отвечать на него пересказом
    нельзя: «должно сработать» уже дважды оказывалось неправдой. Здесь
    вызываются ровно те функции, которыми выбирает `_ask_next_select`, — и
    тест сверяет, что вывод совпадает с тем, что копия делает на самом деле.
    """
    from automation.panel import (panel_item_values_sync,
                                  panel_sync_field_options_sync)
    from storage import get_panel_creds

    creds = get_panel_creds(uid) or {}
    loop = asyncio.get_event_loop()
    ok, values, extra, labels, _url, err = await loop.run_in_executor(
        None, panel_item_values_sync, creds["cookies"], str(ad_id), uid)
    if not ok:
        return [f"разбор списков: товар не прочитался ({err})"]

    form = await _creation_form(uid)
    if not form:
        return ["разбор списков: форма создания не прочиталась"]

    words = [w for w in await _section_words(api, ad_id) if w]
    words += [w for w in _title_words(values) if w not in words]

    # Раздела у товара в панели нет НИГДЕ — ни в форме правки, ни на
    # карточке, ни в строке списка (живой ответ 07.09 по товару 250614).
    # Значит единственный источник — маркетплейс, и если молчит он, копия
    # спросит. Что именно он сказал, и печатаем.
    card = await _ad_card(api, ad_id)
    out = [f"маркетплейс: поля {sorted(card)[:14]}" if card
           else "маркетплейс: карточку объявления не отдал",
           f"  category_id: {card.get('category_id')!r}"]
    cid = card.get("category_id")
    try:
        out.append(f"  путь раздела: {await api.category_path(cid)}"
                   if cid else "  путь раздела: —")
        if cid:
            out.append(f"  раздел словом: {await api.resolve_category(cid)!r}")
            for path, got in (await api.category_probe(cid)).items():
                out.append(f"  {path}: {got}")
    except Exception as e:                                # noqa: BLE001
        out.append(f"  путь раздела: не прочитался ({str(e)[:120]})")
    out.append(f"слова-подсказки: {words}")

    chosen = dict(extra)
    data = {"form_resource": form.get("resource", "items"), "chosen": chosen}
    for attr in _select_queue(form["fields"]):
        fld = next((f for f in form["fields"] if f["attribute"] == attr), {})
        options = fld.get("options") or []
        if not options:
            options, _t = await loop.run_in_executor(
                None, panel_sync_field_options_sync, creds["cookies"],
                data["form_resource"], attr, dict(chosen))
        hint = labels.get(attr)
        src = chosen.get(attr)
        pick, how = _pick_option(options, src, hint, words)
        where = f"список {len(options)}"
        if pick is None:
            found, term = await _search_options(
                uid, data, attr, ([hint] if hint else []) + words)
            if found:
                pick, how = _pick_option(found, src, hint, [term] + words)
                where += f", поиск «{term}» → {len(found)}"
        if pick is None and src not in (None, ""):
            pick, how = {"value": src, "label": hint or str(src)}, \
                "как у образца, со списком не сверился"
        if pick is None:
            out.append(f"{attr}: СПРОСИТ ({where}; у образца "
                       f"номер={src!r} надпись={hint!r})")
            continue
        chosen[attr] = pick.get("value")
        out.append(f"{attr}: {pick.get('value')} «{pick.get('label')}» "
                   f"— {how} ({where})")
    return out


async def _my_ad_numbers(api) -> str:
    """Номера своих объявлений — то, что просит `/copy_debug`.

    Спрашивается у маркетплейса: номер объявления там и номер товара в
    панели — одно и то же число, по нему копия и ходит. Пример с
    выдуманным номером однажды увёл разбор на час: панель ответила 403/404,
    и это прочиталось как поломка копии.
    """
    if not api:
        return "Не настроен API-токен — номера объявлений спросить негде."
    try:
        raw = await api.get_ads()
    except Exception as e:                                # noqa: BLE001
        return f"Объявления не прочитались: {html.escape(str(e)[:200])}"
    ads = (raw.get("data") if isinstance(raw, dict) else raw) or []
    if not ads:
        return "Объявлений нет — копировать нечего."
    rows = [f"<code>{a.get('id')}</code> — {html.escape(str(a.get('title'))[:40])}"
            for a in ads[:20]]
    return ("Номер объявления, а потом: <code>/copy_debug НОМЕР</code>\n\n"
            + "\n".join(rows))[:4000]
