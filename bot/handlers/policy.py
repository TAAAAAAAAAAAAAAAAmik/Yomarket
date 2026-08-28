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
