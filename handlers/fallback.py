from aiogram import Router, types
from aiogram.filters import Command

router = Router(name="fallback")
@router.message(
    ~Command(
        commands=[
            "start",
            "buy",
            "connect",
            "extend",
            "status",
            "help",
            "clients",
            "broadcast",
            "payments",
            "stars_reconcile",
            "refund_stars",
            "admin_broadcast",
            "cancel",
        ]
    )
)
async def handle_unknown(message: types.Message, create_main_menu_keyboard) -> None:
    """Handle messages that were not consumed by a feature router."""
    user_id = message.from_user.id
    if (message.text or "").strip().lower() == "start":
        return
    await message.answer(
        "❓ Неизвестная команда.\n\nИспользуй кнопки ниже или команды:\n"
        "/start - главное меню\n/buy - купить доступ\n/connect - получить конфиг",
        reply_markup=create_main_menu_keyboard(user_id),
    )
