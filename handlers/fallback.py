from typing import Any

from aiogram import Router, types
from aiogram.filters import Command

router = Router(name="fallback")
create_main_menu_keyboard: Any


def configure(*, main_menu_keyboard: Any) -> None:
    """Inject the main menu keyboard builder."""
    global create_main_menu_keyboard
    create_main_menu_keyboard = main_menu_keyboard


@router.message(
    ~Command(
        commands=[
            "start",
            "buy",
            "connect",
            "extend",
            "status",
            "admin_broadcast",
            "cancel",
        ]
    )
)
async def handle_unknown(message: types.Message) -> None:
    """Handle messages that were not consumed by a feature router."""
    user_id = message.from_user.id
    if (message.text or "").strip().lower() == "start":
        return
    await message.answer(
        "❓ Неизвестная команда.\n\nИспользуй кнопки ниже или команды:\n"
        "/start - главное меню\n/buy - купить доступ\n/connect - получить конфиг",
        reply_markup=create_main_menu_keyboard(user_id),
    )
