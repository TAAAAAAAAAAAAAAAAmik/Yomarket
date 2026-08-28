import logging

from aiogram import F, Router
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ui

from api.yoomarket import YooMarketAPI
from keyboards.main import main_menu_keyboard
from storage import delete_token, get_token, save_token, get_settings, save_settings, get_shop_name, save_shop_name, _DATA_DIR

router = Router()
logger = logging.getLogger(__name__)

# Поднимается при каждом значимом изменении: по ней видно, доехал ли код.
BOT_VERSION = "2026-08-28-policy"

# Метка процесса, разная у каждого запуска. Два контейнера с одним токеном
# ведут каждый свой фоновый цикл, и продавец получает все уведомления
# дважды — при этом команды отвечают как обычно, потому что апдейт Telegram
# отдаёт только одному из них. Отличить это от ошибки в коде можно единственным
# способом: вызвать /version несколько раз и посмотреть, меняется ли метка.
import time as _time
import uuid as _uuid

INSTANCE_ID = _uuid.uuid4().hex[:6]
STARTED_AT = _time.time()


class AuthState(StatesGroup):
    waiting_for_token = State()


def _extract_shop(info: dict) -> tuple[str, str]:
    """Название магазина и баланс строкой из ответа /check.

    /check отдаёт {status, shop:{id,title}, integration:{…}, ts} — то есть
    только «кто вы», без денег. Баланс здесь возвращается прочерком
    намеренно: он читается из панели, где и лежит на самом деле.
    """
    shop = info.get("shop") or info.get("data") or info
    if isinstance(shop, dict):
        name = shop.get("name") or shop.get("shop_name") or shop.get("title") or "Магазин"
        balance = shop.get("balance") or shop.get("wallet") or shop.get("money") or "—"
    else:
        name = "Магазин"
        balance = "—"
    return str(name), str(balance)


async def _send_menu(target: Message | CallbackQuery, user_id: int) -> None:
    from storage import is_admin, menu_header_html
    # Название магазина приходит с маркетплейса: одиночный `<` в нём роняет
    # отправку целиком, и продавец после подключения не видит меню вообще.
    name = ui.esc(get_shop_name(user_id) or "Магазин")
    text = (f"🏪 <b>{name}</b>\n\n{menu_header_html()} <b>Главное меню</b>\n"
            "Выберите раздел:")
    kb = main_menu_keyboard(is_admin_user=is_admin(user_id))
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=kb)
        await target.answer()
    else:
        await target.answer(text, reply_markup=kb)


PANEL_URL = "https://panel.yoomarket.net"


def _welcome_kb(back: bool = False) -> "InlineKeyboardMarkup":
    """Кнопки под приветствием.

    До этого под первым экраном не было ни одной кнопки: человек читал
    инструкцию и должен был сам догадаться скопировать ссылку из текста,
    сходить в браузер и вернуться. Кнопка-ссылка убирает из этой цепочки
    три шага, а «Не нахожу токен» — единственная причина, по которой на
    первом экране застревают.
    """
    b = InlineKeyboardBuilder()
    b.button(text="🌐 Открыть панель", url=PANEL_URL)
    # На самом экране помощи вторая кнопка ведёт назад, а не по кругу в него же.
    if back:
        b.button(text="⬅️ К подключению", callback_data="start:back")
    else:
        b.button(text="❓ Не нахожу токен", callback_data="start:token_help")
    return ui.lay(b).as_markup()


@router.callback_query(F.data == "start:token_help")
async def token_help(callback: CallbackQuery, state: FSMContext) -> None:
    """Подробная подсказка. Ожидание токена при этом не сбрасывается:
    человек уходит читать и возвращается вставить — форма должна его дождаться."""
    from storage import render_custom_text
    await state.set_state(AuthState.waiting_for_token)
    await callback.message.edit_text(
        render_custom_text("token_help"), reply_markup=_welcome_kb(back=True),
        disable_web_page_preview=True)
    await callback.answer()


@router.callback_query(F.data == "start:back")
async def back_to_welcome(callback: CallbackQuery, state: FSMContext) -> None:
    from storage import render_custom_text
    await state.set_state(AuthState.waiting_for_token)
    await callback.message.edit_text(
        render_custom_text("welcome"), reply_markup=_welcome_kb(),
        disable_web_page_preview=True)
    await callback.answer()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    token = get_token(message.from_user.id)
    if token:
        api = YooMarketAPI(token)
        await api.start()
        try:
            info = await api.check()
            name, _ = _extract_shop(info)
            save_shop_name(message.from_user.id, name)
        except Exception:
            pass
        finally:
            await api.close()
        await _send_menu(message, message.from_user.id)
        return

    from storage import render_custom_text
    await state.set_state(AuthState.waiting_for_token)
    # В приветствии есть ссылка на панель, и превью к ней завалило бы собой
    # сами шаги подключения.
    await message.answer(render_custom_text("welcome"),
                         reply_markup=_welcome_kb(),
                         disable_web_page_preview=True)


@router.message(Command("version"))
async def cmd_version(message: Message) -> None:
    """Версия работающего бота и где лежат его данные — диагностика выката."""
    import os
    import storage

    uid = message.from_user.id

    # 1. Storage backend
    if storage._USE_DB:
        backend = "🟢 PostgreSQL (постоянная БД)"
        db_ok = "?"
        try:
            storage._db_read_raw("tokens")  # touch the DB
            db_ok = "✅ подключение работает"
        except Exception as e:
            db_ok = f"❌ ошибка: {str(e)[:60]}"
        storage_lines = [f"💾 Хранилище: {backend}", f"   {db_ok}"]
    else:
        backend = "🔴 JSON-файлы (эфемерно на Railway!)"
        storage_lines = [
            f"💾 Хранилище: {backend}",
            "   ⚠️ DATABASE_URL не задан — данные сотрутся при редеплое!",
            f"   📁 {_DATA_DIR}",
        ]

    # 2. Redis (FSM)
    redis_on = bool(os.environ.get("REDIS_URL", "").strip())
    redis_line = "🧩 FSM: 🟢 Redis" if redis_on else "🧩 FSM: ⚪ память (сбрасывается при рестарте)"

    # 3. Лежит ли прямо сейчас токен ИМЕННО этого продавца
    has_token = bool(storage.get_token(uid))
    accounts = storage.get_accounts(uid)
    token_line = (f"🔑 Ваш токен: {'✅ сохранён' if has_token else '❌ нет'}"
                  f"  ({len(accounts)} аккаунт(ов))")
    panel = storage.get_panel_creds(uid)
    panel_line = f"🌐 Куки панели: {'✅ есть' if panel and panel.get('cookies') else '❌ нет'}"

    # Seed-фраза TON — это доступ к чужому кошельку, и лежит она в том же
    # хранилище, что и всё остальное. Видеть, зашифрована ли она на самом
    # деле, важнее, чем верить, что «наверное, да».
    has_seed = bool((storage.get_fragment_creds(uid) or {}).get("mnemonic"))
    if not has_seed:
        seed_line = "🔐 Seed-фраза TON: не сохранена"
    elif storage.encryption_on():
        seed_line = "🔐 Seed-фраза TON: ✅ зашифрована"
    else:
        seed_line = ("🔐 Seed-фраза TON: ⚠️ <b>в открытом виде</b> — "
                     "задайте SECRET_KEY в переменных окружения")

    # Время видно только внутри «Настроек», а зависит от него многое: час
    # итогов дня, окно ночного режима, граница суток в статистике. Вопрос
    # «какое время в боте» не должен требовать хождения по экранам.
    import localtime as _lt
    _s = storage.get_settings(uid)
    time_line = (f"🕐 Ваше время: <b>{_lt.now(_s).strftime('%d.%m %H:%M')}</b>"
                 f" · {_lt.offset_label(_s)}")

    uptime = int((_time.time() - STARTED_AT) / 60)

    # Получаем ли мы вообще сообщения. Если это читают — получаем; но строка
    # нужна для случая «отвечает через раз», когда обновления достаются то
    # нам, то второму экземпляру.
    try:
        from main import POLLING, polling_trouble
        trouble = polling_trouble()
        if trouble:
            polling_line = f"🔇 <b>Приём сообщений:</b> {trouble}"
        elif POLLING.get("last_update"):
            ago = int(_time.time() - POLLING["last_update"])
            polling_line = f"📡 Приём сообщений: 🟢 последнее {ago} с назад"
        else:
            polling_line = "📡 Приём сообщений: 🟢 работает"
        if POLLING.get("webhook"):
            # Снять не удалось — значит опрос не заработает, и это важнее
            # всего остального в этой строке.
            polling_line += ("\n🔌 На токене висит вебхук — снять его не "
                             "вышло, опросом сообщения не придут")
    except Exception:
        polling_line = ""

    await message.answer(
        f"🤖 <b>Версия:</b> <code>{BOT_VERSION}</code>\n"
        f"🆔 Процесс: <code>{INSTANCE_ID}</code> · PID {os.getpid()} · "
        f"работает {uptime} мин\n"
        f"<i>Вызовите /version 4–5 раз. Если метка процесса меняется — "
        f"запущено несколько ботов на одном токене, и все уведомления "
        f"приходят по столько же раз.</i>\n\n"
        + "\n".join(storage_lines)
        + f"\n{redis_line}\n\n"
        + f"{token_line}\n{panel_line}\n{seed_line}\n{time_line}"
        + (f"\n{polling_line}" if polling_line else "")
    )


@router.message(Command("sent"))
async def cmd_sent(message: Message) -> None:
    """Что ЭТОТ процесс отправил за последнее время.

    Продавец видит дубль — а здесь одна запись: значит вторую копию прислал
    другой процесс. Две записи подряд — беда внутри одного. Различить это
    иначе нельзя: в Telegram обе копии выглядят одинаково.
    """
    import storage
    from tasks.manager import _SENT_LOG, _SENDER_LEASE_TTL

    import localtime as _lt
    _settings = storage.get_settings(message.from_user.id)
    lease = (_settings.get("_sender") or {})
    owner = str(lease.get("inst") or "—")
    age = _time.time() - float(lease.get("ts") or 0)
    mine = owner == INSTANCE_ID

    lines = [f"📤 <b>Отправлено этим процессом</b>  <code>{INSTANCE_ID}</code>",
             "",
             f"{'🟢' if mine else '🟡'} Рассылку ведёт: <code>{owner}</code>"
             + (" — это я" if mine else " — другой процесс"),
             f"   продлена {int(age)} с назад "
             f"(аренда {int(_SENDER_LEASE_TTL)} с)", ""]
    if not _SENT_LOG:
        lines.append("<i>этот процесс ещё ничего не отправлял</i>")
    seen: dict[str, int] = {}
    for ts, head in _SENT_LOG[-15:]:
        seen[head] = seen.get(head, 0) + 1
    import html as _h
    for ts, head in _SENT_LOG[-15:]:
        when = _lt.fmt(ts, _settings, "%H:%M:%S")
        mark = "‼️" if seen.get(head, 0) > 1 else "•"
        lines.append(f"{mark} <code>{when}</code> {_h.escape(head)}")
    lines += ["", "<i>‼️ — этот процесс отправил одно и то же дважды. "
              "Если дубль в чате есть, а здесь запись одна — вторую копию "
              "прислал другой процесс.</i>"]
    await message.answer("\n".join(lines)[:3900])


async def _edit(message: Message, text: str,
                markup: InlineKeyboardMarkup | None = None) -> None:
    """Переписать сообщение «⏳ проверяю» результатом.

    Раньше каждый шаг подключения добавлял в чат новое сообщение, и к
    третьей попытке экран был завален «⏳ Проверяю токен...» вперемешку с
    отказами — а какой из них последний, приходилось искать глазами.
    """
    try:
        await message.edit_text(text, reply_markup=markup,
                                disable_web_page_preview=True)
    except Exception:                            # noqa: BLE001
        await message.answer(text, reply_markup=markup,
                             disable_web_page_preview=True)


def _hidden_note(hidden: bool) -> str:
    """Что стало с сообщением, в котором был токен.

    Сказать «удалено» там, где удалить не вышло, — ровно та ложь, из-за
    которой продавец оставит ключ от своего магазина висеть в переписке.
    """
    return ("<i>🔒 Сообщение с токеном удалено из переписки.</i>" if hidden else
            "<i>⚠️ Сообщение с токеном удалить не получилось — сотрите его "
            "вручную: оно остаётся в истории чата.</i>")


async def _hide_token(message: Message) -> bool:
    """Убрать из переписки сообщение с токеном.

    Токен остаётся в истории чата навсегда: у продавца в телефоне, у того,
    кому он покажет экран, и в резервной копии Telegram. Удалить его —
    единственное, что мы можем с этим сделать. Отвечает, получилось ли:
    обещать безопасность, которой не случилось, хуже, чем не обещать
    ничего (Telegram не даёт удалять сообщения старше 48 часов, и это не
    единственная причина отказа).
    """
    try:
        await message.delete()
        return True
    except Exception as e:                       # noqa: BLE001 — причин много
        logger.info("сообщение с токеном не удалилось: %s", e)
        return False


_WAIT = ui.screen("🔑 <b>Подключаю магазин</b>",
                  ["⏳ Спрашиваю Юмаркет, признаёт ли он этот токен…"])


def _retry_kb(again: bool = False) -> InlineKeyboardMarkup:
    """Кнопки под отказом. `again` — предложить тот же токен ещё раз.

    Она к месту ровно там, где токен ни при чём: предлагать «повторить» на
    отказе «токен не признан» — обещать, что со второго раза выйдет.
    """
    b = InlineKeyboardBuilder()
    if again:
        b.button(text="🔁 Повторить", callback_data="start:retry")
    b.button(text="🌐 Открыть панель", url=PANEL_URL)
    b.button(text="❓ Не нахожу токен", callback_data="start:token_help")
    return ui.lay(b).as_markup()


@router.message(AuthState.waiting_for_token)
async def process_token(message: Message, state: FSMContext, **data) -> None:
    token = (message.text or "").strip()
    if not token:
        await message.answer("❌ Пустое сообщение. Пришлите токен одной строкой:",
                             reply_markup=_retry_kb())
        return

    # Сначала убрать токен с экрана, потом идти в сеть: проверка занимает
    # секунды, и всё это время строка висит в переписке.
    hidden = await _hide_token(message)
    wait = await message.answer(_WAIT)
    await _connect(message.from_user.id, token, wait, state,
                   data.get("task_manager"), hidden)


@router.callback_query(F.data == "start:retry")
async def retry_token(callback: CallbackQuery, state: FSMContext, **data) -> None:
    """Повторить проверку тем же токеном.

    Кнопка появляется только там, где токен ни при чём: маркетплейс не
    ответил или попросил сбавить темп. Без неё продавец оказывался в
    тупике — сообщение с токеном мы уже удалили, а панель показывает токен
    ровно один раз, и второй раз скопировать его неоткуда.
    """
    token = str((await state.get_data()).get("token") or "")
    if not token:
        await callback.answer("Токен уже не сохранён — пришлите его снова",
                              show_alert=True)
        return
    await _edit(callback.message, _WAIT)
    await _connect(callback.from_user.id, token, callback.message, state,
                   data.get("task_manager"), hidden=True)
    await callback.answer()


async def _connect(uid: int, token: str, wait: Message, state: FSMContext,
                   task_manager, hidden: bool) -> None:
    """Спросить Юмаркет про токен и показать результат в сообщении `wait`."""
    api = YooMarketAPI(token)
    await api.start()
    try:
        info = await api.check()
    except Exception as e:                       # noqa: BLE001
        await api.close()
        # Английский код ошибки на этом экране — отписка: это первое, что
        # видит человек, ещё ничего в боте не сделавший.
        from api.yoomarket import auth_trouble
        why, what, ours = auth_trouble(str(e), token)
        # «Токен ни при чём» и «пришлите токен ещё раз» в одном сообщении —
        # это экран, спорящий сам с собой. Что делать дальше, зависит от
        # того, в токене ли дело.
        if ours:
            nxt = "Пришлите токен ещё раз — я жду его здесь же."
            await state.update_data(token=None)
        else:
            nxt = ("Токен я запомнил — нажмите «Повторить» через пару минут. "
                   "Заново копировать его из панели не нужно.")
            await state.update_data(token=token)
        await _edit(wait, ui.screen(
            f"⚠️ <b>{why}</b>", [what, "", nxt],
            footer=f"<i>Ответ маркетплейса: <code>{ui.esc(str(e))[:120]}</code></i>",
        ), _retry_kb(again=not ours))
        return
    finally:
        await api.close()

    save_token(uid, token)
    await state.clear()

    name, balance = _extract_shop(info)
    save_shop_name(uid, name)

    if task_manager:
        task_manager.start_for_user(uid)

    # Шаг 2 подключения — вход в панель. Тому, кто уже вошёл, шага не
    # показываем: проводить человека по сделанному незачем.
    from storage import get_panel_creds, render_custom_text
    # Название магазина приходит с маркетплейса и уходит в HTML-сообщение:
    # одиночный `<` в нём уронил бы отправку целиком.
    safe_name = ui.esc(name)
    if get_panel_creds(uid):
        await _edit(wait, ui.screen(
            "✅ <b>Токен обновлён</b>",
            [f"🏪 <b>{safe_name}</b>"
             # Только если в ответе была сумма: «— ₽» — это не баланс.
             + (f"   ·   💰 <b>{ui.esc(balance)} ₽</b>" if balance != "—" else "")],
            footer=_hidden_note(hidden)))
        await _send_menu(wait, uid)
        return

    b = InlineKeyboardBuilder()
    b.button(text="📧 Войти по email", callback_data="panel:sms_start")
    b.button(text="⏭ Позже", callback_data="menu:main")
    ui.lay(b)
    await _edit(wait, render_custom_text(
        "token_ok", name=safe_name, balance=ui.esc(str(balance)))
        + "\n\n" + _hidden_note(hidden), b.as_markup())


@router.callback_query(F.data == "menu:main")
async def back_to_main(callback: CallbackQuery) -> None:
    await _send_menu(callback, callback.from_user.id)


@router.message(Command("logout"))
async def cmd_logout(message: Message, state: FSMContext) -> None:
    await state.clear()
    delete_token(message.from_user.id)
    await message.answer("🚪 Вы вышли. Для входа — /start")
