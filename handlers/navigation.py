import logging

from aiogram import F, Router, types
from aiogram.filters import CommandStart

from database import Database
from message_templates import (
    active_access_message,
    expired_subscription_status,
    service_guide_message,
)
from payment import PaymentManager
from subscription_view import home_message
from telegram_runtime import serialized_user_action
from utils import format_date_for_user

logger = logging.getLogger(__name__)
router = Router(name="navigation")


@router.message(CommandStart())
@serialized_user_action
async def cmd_start(
    message: types.Message,
    db: Database,
    create_main_menu_keyboard,
    chat_panel,
    clear_admin_state,
    user_action_locks,
    notify_admins=None,
):
    """Reset transient UI state and restore the main control panel."""
    user_id = message.from_user.id
    await chat_panel.delete_user_message(message)
    clear_admin_state(user_id)
    parts = (getattr(message, "text", None) or "").split(maxsplit=1)
    payload = parts[1].strip() if len(parts) == 2 else ""
    if payload.startswith("claim_"):
        invitation = db.claim_client_invitation(
            payload.removeprefix("claim_"),
            user_id,
            getattr(message.from_user, "username", None),
        )
        if invitation:
            if notify_admins is not None:
                await notify_admins(
                    "⏳ Новая заявка на привязку клиента\n\n"
                    f"Ожидался: @{invitation['expected_username']}\n"
                    f"Telegram ID: {user_id}\n"
                    f"Username: @{getattr(message.from_user, 'username', None) or 'нет'}\n"
                    "Подтверди её в разделе «Ожидают привязки»."
                )
            await chat_panel.restore_or_create(
                message.chat.id,
                user_id,
                "✅ Заявка отправлена администратору. Доступ появится после подтверждения.",
                create_main_menu_keyboard(user_id),
            )
            return
        await chat_panel.restore_or_create(
            message.chat.id,
            user_id,
            "❌ Ссылка недействительна или истекла. Попроси администратора перевыпустить её.",
            create_main_menu_keyboard(user_id),
        )
        return
    client_reader = getattr(db, "get_admin_client_details", None)
    if callable(client_reader) and client_reader(user_id):
        db.upsert_client(user_id, getattr(message.from_user, "username", None))
    await chat_panel.restore_or_create(
        message.chat.id,
        user_id,
        home_message(db, user_id),
        create_main_menu_keyboard(user_id),
    )


# Inline button handlers
@router.callback_query(F.data == "pay")
async def handle_pay_callback(
    callback_query: types.CallbackQuery,
    db: Database,
    payment_manager: PaymentManager,
    safe_answer_callback,
    safe_edit_callback_message,
):
    """Handle the 'Buy access' button."""
    user_id = callback_query.from_user.id

    await safe_answer_callback(callback_query)
    if db.get_client_access_state(user_id).source == "complimentary":
        await safe_edit_callback_message(
            callback_query.message,
            "🎁 У тебя уже действует бесплатный доступ.",
        )
        return

    payment_text, keyboard = await payment_manager.get_payment_selection_view(user_id)
    await safe_edit_callback_message(
        callback_query.message,
        payment_text,
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("tariff_label_"))
async def handle_tariff_label_callback(callback_query: types.CallbackQuery, safe_answer_callback):
    """Ignore taps on tariff label rows."""
    await safe_answer_callback(callback_query)


@router.callback_query(F.data == "already_paid")
async def handle_already_paid_callback(
    callback_query: types.CallbackQuery,
    db: Database,
    safe_answer_callback,
    show_menu_from_callback,
    create_main_menu_keyboard,
    is_access_active,
):
    """Handle the 'Access purchased' button."""
    user_id = callback_query.from_user.id
    # IMPORTANT: fetch fresh data from the DB
    existing_peer = db.get_peer_by_telegram_id(user_id)

    # Check if access is active (re-check on every tap)
    if not is_access_active(existing_peer):
        # Access expired but was paid: update keyboard to "Buy access"
        expire_date_str = (
            existing_peer.get("expire_date", "Неизвестно") if existing_peer else "Неизвестно"
        )
        expire_date_formatted = (
            format_date_for_user(expire_date_str)
            if expire_date_str != "Неизвестно"
            else "Неизвестно"
        )
        await safe_answer_callback(callback_query, "⚠️ Твой VPN доступ истек!")

        expired_text = expired_subscription_status(
            db.get_peer_count(user_id),
            expire_date_str,
            expire_date_formatted,
        )
        # Update message with new keyboard (button switches to "Buy access")
        await show_menu_from_callback(
            callback_query,
            expired_text,
            create_main_menu_keyboard(user_id),
        )
        return

    await safe_answer_callback(callback_query, "✅ У тебя уже есть доступ!")

    # Update message with the current keyboard
    await show_menu_from_callback(
        callback_query,
        active_access_message(),
        create_main_menu_keyboard(user_id),
    )


@router.callback_query(F.data == "guide")
async def handle_guide_callback(
    callback_query: types.CallbackQuery,
    safe_answer_callback,
    create_guide_keyboard,
    chat_panel,
):
    """Handle the 'Guide' button."""
    await safe_answer_callback(callback_query)

    await chat_panel.render_from_message(
        callback_query.message,
        service_guide_message(),
        create_guide_keyboard(),
        user_id=callback_query.from_user.id,
    )


@router.callback_query(F.data == "main")
async def handle_main_callback(
    callback_query: types.CallbackQuery,
    db: Database,
    safe_answer_callback,
    show_menu_from_callback,
    create_main_menu_keyboard,
    is_admin,
    clear_admin_state,
):
    """Handle the 'Back to menu' button."""
    await safe_answer_callback(callback_query)

    user_id = callback_query.from_user.id
    if is_admin(user_id):
        clear_admin_state(user_id)

    await show_menu_from_callback(
        callback_query,
        home_message(db, user_id),
        create_main_menu_keyboard(user_id),
    )
