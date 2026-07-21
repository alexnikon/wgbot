from aiogram import Router, types

router = Router(name="fallback")


@router.message()
async def handle_unknown(
    message: types.Message, create_main_menu_keyboard, chat_panel
) -> None:
    """Discard unhandled input and restore the button-only control panel."""
    user_id = message.from_user.id
    await chat_panel.delete_user_message(message)
    await chat_panel.restore_or_create(
        message.chat.id,
        user_id,
        "Выбери действие с помощью кнопок ниже:",
        create_main_menu_keyboard(user_id),
    )
