"""Экран правовых документов: /policy.

Соглашение, оферта и политика конфиденциальности — не украшение: это то,
на что ссылаются, когда спорят о деньгах. Поэтому экран устроен по правилу
«бот не должен врать»:

* **Кнопка есть только у документа, ссылка на который задана.** Кнопка,
  ведущая в никуда, — это обещание документа, которого нет; а кнопка с
  пустым адресом вдобавок роняет отправку целиком, потому что такую
  клавиатуру Telegram не принимает, и экран не приходит вовсе.
* **Пока владелец не добавил ни одного документа, так и написано** — и
  сказано, где их добавить. Пустой экран без объяснения читается как
  поломка бота.

Ссылки — общие на весь бот и принадлежат его владельцу, а не продавцу,
который взял подписку: правовые документы у продукта одни.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ui
from storage import POLICY_DOCS, get_policy_links, is_admin, render_custom_text

router = Router()

# Куда ведёт «Назад». Отдельным именем, а не строкой в трёх местах: экран
# зовут и командой, и кнопкой, и возвращать они обязаны в одно место.
_BACK = "menu:main"


def policy_keyboard(back: str = _BACK) -> InlineKeyboardMarkup:
    """Кнопки экрана: по одной на заданный документ, плюс «Назад»."""
    links = get_policy_links()
    b = InlineKeyboardBuilder()
    for key, title in POLICY_DOCS:
        url = links.get(key)
        if url:
            b.button(text=title, url=url)
    b.button(text="⬅️ Назад", callback_data=back)
    return ui.lay(b).as_markup()


def policy_text(for_admin: bool = False) -> str:
    """Текст экрана. Пустой список документов объясняется, а не замалчивается."""
    text = render_custom_text("policy")
    if get_policy_links():
        return text
    missing = ["", ui.RULE,
               "⚠️ <b>Документы пока не добавлены.</b>"]
    missing.append(
        "Владелец бота ещё не указал ссылки — до этого ссылаться здесь не на "
        "что." if not for_admin else
        "Ссылки задаются в «👑 Админ-панель → 📄 Правовые документы»."
    )
    return text + "\n" + "\n".join(missing)


@router.message(Command("policy"))
async def cmd_policy(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        policy_text(is_admin(message.from_user.id)),
        reply_markup=policy_keyboard(),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data == "menu:policy")
async def show_policy(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(
        policy_text(is_admin(callback.from_user.id)),
        reply_markup=policy_keyboard(),
        disable_web_page_preview=True,
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Удаление своих данных
# ---------------------------------------------------------------------------
#
# Право «быть забытым» из политики конфиденциальности. Раздел 12 обещает
# срок до 72 часов через поддержку — здесь это делается сразу и самим
# продавцом, потому что обещание, выполняемое кнопкой, надёжнее обещания,
# выполняемого чужой памятью.

_PURGE_WARNING = (
    "Будут стёрты <b>безвозвратно</b>:",
    "",
    "• токен Юмаркета и вход в панель",
    "• все настройки автоматики и правила автоответов",
    "• история заказов, покупателей и переписки, которую видел бот",
    "• данные Fragment и seed-фраза кошелька TON",
    "• ключи поставщиков и данные прокси",
    "• остаток подписки — он <b>сгорит</b>, вернуть его нельзя",
)


def _purge_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🗑 Да, удалить всё", callback_data="policy:purge")
    b.button(text="❌ Отмена", callback_data=_BACK)
    # Столбиком, и это не недоделка раскладки: обе кнопки коротки и встали бы
    # рядом, а промах пальцем здесь стирает магазин без возможности вернуть.
    b.adjust(1)
    return b.as_markup()


@router.message(Command("forget_me"))
async def cmd_forget_me(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(ui.screen(
        "🗑 <b>Удалить мои данные</b>", list(_PURGE_WARNING),
        footer="<i>Бот не может отозвать выданный ему токен на стороне "
               "Юмаркета и вывести деньги с кошелька TON — это остаётся за "
               "вами. Сделайте это после удаления.</i>"),
        reply_markup=_purge_kb())


@router.callback_query(F.data == "policy:forget")
async def forget_me(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(ui.screen(
        "🗑 <b>Удалить мои данные</b>", list(_PURGE_WARNING),
        footer="<i>Бот не может отозвать выданный ему токен на стороне "
               "Юмаркета и вывести деньги с кошелька TON — это остаётся за "
               "вами. Сделайте это после удаления.</i>"),
        reply_markup=_purge_kb())
    await callback.answer()


@router.callback_query(F.data == "policy:purge")
async def purge_confirmed(callback: CallbackQuery, state: FSMContext,
                          **data) -> None:
    from storage import purge_user

    uid = callback.from_user.id
    # Сначала остановить фоновые проходы этого продавца, потом стирать. Они
    # держат его настройки в памяти и сохраняют их в конце прохода: удаление
    # на ходу было бы стёрто обратно секундой позже, а продавец получил бы
    # «✅ удалено» про данные, которые остались на месте.
    tm = data.get("task_manager")
    if tm:
        tm.stop_for_user(uid)

    await state.clear()
    report = purge_user(uid)
    if "отказ" in report:
        await callback.answer(str(report["отказ"]), show_alert=True)
        return

    body = ([f"Стёрто записей: <b>{sum(report.values())}</b>",
             "", *(f"• {name}: {n}" for name, n in sorted(report.items()))]
            if report else
            ["Стирать было нечего — данных о вас в боте не осталось."])
    b = InlineKeyboardBuilder()
    b.button(text="🚀 Начать заново", callback_data="menu:main")
    await callback.message.edit_text(ui.screen(
        "✅ <b>Данные удалены</b>", body,
        footer="<i>Отзовите выданный боту токен в панели Юмаркета — этого "
               "он за вас сделать не может.</i>"),
        reply_markup=ui.lay(b).as_markup())
    await callback.answer()
