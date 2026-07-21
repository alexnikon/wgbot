import logging

from aiogram import F, Router, types

from cascade_api import CascadeNotFound, CascadeRouter
from database import Database
from payment import PaymentManager
from utils import format_date_for_user, parse_date_flexible

logger = logging.getLogger(__name__)
router = Router(name="access")

@router.callback_query(F.data == "get_config")
async def handle_get_config_callback(
    callback_query: types.CallbackQuery,
    db: Database,
    cascade_router: CascadeRouter,
    safe_answer_callback,
    safe_edit_callback_message,
    show_menu_from_callback,
    create_back_to_menu_keyboard,
    create_main_menu_keyboard,
    create_or_restore_peer_for_user,
    send_config_with_confirmation,
    is_access_active,
    user_action_locks,
):
    """Handle the 'Get config' button."""
    await safe_answer_callback(callback_query)

    user_id = callback_query.from_user.id

    async with user_action_locks.hold(user_id):
        return await _handle_get_config_locked(
            callback_query,
            db,
            cascade_router,
            safe_edit_callback_message,
            show_menu_from_callback,
            create_back_to_menu_keyboard,
            create_main_menu_keyboard,
            create_or_restore_peer_for_user,
            send_config_with_confirmation,
            is_access_active,
        )


async def _handle_get_config_locked(
    callback_query,
    db,
    cascade_router,
    safe_edit_callback_message,
    show_menu_from_callback,
    create_back_to_menu_keyboard,
    create_main_menu_keyboard,
    create_or_restore_peer_for_user,
    send_config_with_confirmation,
    is_access_active,
):
    user_id = callback_query.from_user.id
    username = callback_query.from_user.username
    existing_peer = db.get_peer_by_telegram_id(user_id)
    if existing_peer:
        # Check if access is active (paid and not expired)
        if not is_access_active(existing_peer):
            # Access expired or not paid
            if existing_peer.get("payment_status") == "paid":
                # Access was paid, but expired
                expire_date_str = existing_peer.get("expire_date", "Неизвестно")
                expire_date_formatted = (
                    format_date_for_user(expire_date_str)
                    if expire_date_str != "Неизвестно"
                    else "Неизвестно"
                )
                error_text = f"""
⚠️ Твой доступ к VPN истек!

📅 Дата истечения: {expire_date_formatted}

⚠️ Для продолжения пользования сервисом, необходимо продлить доступ.

Выбери действие с помощью кнопок ниже:
                """
            else:
                # Access not paid
                error_text = """
❌ У тебя нет активного доступа.

💎 Чтобы получить конфиг, нужно оплатить доступ.

Выбери действие с помощью кнопок ниже:
                """
            await safe_edit_callback_message(
                callback_query.message,
                error_text,
                reply_markup=create_main_menu_keyboard(user_id),
            )
            return

        # User has active access: try to send config or restore if missing
        try:
            await safe_edit_callback_message(
                callback_query.message,
                "⏳ Скачиваю конфигурацию...",
                reply_markup=create_back_to_menu_keyboard(),
            )

            try:
                peer_config = await cascade_router.get_primary_config(user_id)
            except CascadeNotFound:
                logger.warning("Primary Cascade peer is explicitly missing for user %s", user_id)
                ok, err, new_config = await create_or_restore_peer_for_user(
                    user_id, username, existing_peer.get("tariff_key")
                )
                if not ok:
                    await safe_edit_callback_message(
                        callback_query.message,
                        f"❌ {err}\n\nВыбери действие с помощью кнопок ниже:",
                        reply_markup=create_main_menu_keyboard(user_id),
                    )
                    return

                # Use the received config
                peer_config = new_config

            sent = await send_config_with_confirmation(
                callback_query.message.chat.id,
                peer_config,
                source_message=callback_query.message,
                caption=None,
            )
            if not sent:
                await safe_edit_callback_message(
                    callback_query.message,
                    "❌ Не удалось отправить конфигурацию.\n\nИспользуй кнопку ниже, чтобы вернуться в меню:",
                    reply_markup=create_back_to_menu_keyboard(),
                )
            else:
                await safe_edit_callback_message(
                    callback_query.message,
                    "✅ Конфигурация отправлена отдельным файлом.\n\n"
                    "Открой её через AmneziaWG и добавь новый туннель.",
                    reply_markup=create_main_menu_keyboard(user_id),
                )
        except Exception as e:
            logger.error(f"Error while fetching/restoring configuration: {e}", exc_info=True)
            await safe_edit_callback_message(
                callback_query.message,
                "❌ Ошибка при получении конфигурации. Попробуй позже или обратись в поддержку.\n\nИспользуй кнопку ниже, чтобы вернуться в меню:",
                reply_markup=create_back_to_menu_keyboard(),
            )
    else:
        # User has no peer
        error_text = """
❌ У тебя нет VPN доступа.

💎 Чтобы получить конфиг, нужно оплатить доступ.

Выбери действие с помощью кнопок ниже:
        """
        await show_menu_from_callback(
            callback_query,
            error_text,
            create_main_menu_keyboard(user_id),
        )


@router.callback_query(F.data == "extend")
async def handle_extend_callback(
    callback_query: types.CallbackQuery,
    db: Database,
    payment_manager: PaymentManager,
    safe_answer_callback,
    safe_edit_callback_message,
    show_menu_from_callback,
    create_main_menu_keyboard,
):
    """Handle the 'Extend access' button."""
    await safe_answer_callback(callback_query)

    user_id = callback_query.from_user.id
    # Check if the user has an active peer
    existing_peer = db.get_peer_by_telegram_id(user_id)
    if not existing_peer:
        error_text = """
❌ У тебя нет активного VPN доступа.

💎 Сначала необходимо купить доступ.

Выбери действие с помощью кнопок ниже:
        """
        await show_menu_from_callback(
            callback_query,
            error_text,
            create_main_menu_keyboard(user_id),
        )
        return

    # Check payment status
    if existing_peer.get("payment_status") != "paid":
        error_text = """
❌ У тебя нет оплаченного доступа.

💎 Сначала необходимо оплатить доступ.

Выбери действие с помощью кнопок ниже:
        """
        await safe_edit_callback_message(
            callback_query.message,
            error_text,
            reply_markup=create_main_menu_keyboard(user_id),
        )
        return

    payment_text, keyboard = await payment_manager.get_payment_selection_view(user_id)
    await safe_edit_callback_message(
        callback_query.message,
        payment_text,
        reply_markup=keyboard,
    )


@router.callback_query(F.data == "status")
async def handle_status_callback(
    callback_query: types.CallbackQuery,
    db: Database,
    safe_answer_callback,
    show_menu_from_callback,
    create_main_menu_keyboard,
    ui_renderer,
):
    """Handle the 'Access status' button."""
    await safe_answer_callback(callback_query)

    user_id = callback_query.from_user.id
    # Check if the user has an active peer
    existing_peer = db.get_peer_by_telegram_id(user_id)
    if not existing_peer:
        error_text = """
❌ У тебя нет активного VPN доступа.

💎 Для получения доступа необходимо его оплатить.

Выбери действие с помощью кнопок ниже:
        """
        await show_menu_from_callback(
            callback_query,
            error_text,
            create_main_menu_keyboard(user_id),
        )
        return

    # Get peer info from the database
    try:
        expire_date_str = existing_peer.get("expire_date", "Неизвестно")

        # Format dates for display
        expire_date_formatted = (
            format_date_for_user(expire_date_str)
            if expire_date_str != "Неизвестно"
            else "Неизвестно"
        )
        connected_devices = db.get_peer_count(user_id)
        devices_line = f"\nПодключено устройств: {connected_devices}" if connected_devices else ""

        # Check if access has expired
        from datetime import datetime

        is_expired = False
        if expire_date_str and expire_date_str != "Неизвестно":
            try:
                expire_date = parse_date_flexible(expire_date_str)
                now = datetime.now()
                is_expired = expire_date <= now
            except (ValueError, TypeError):
                pass

        # Format peer info
        if is_expired:
            status_text = f"""
📊 Статус доступа:

⏰ Доступ закончился: {expire_date_formatted}{devices_line}

⚠️ Твой VPN доступ истек!

⚠️ Для продолжения пользования сервисом, необходимо продлить доступ.

Выбери действие с помощью кнопок ниже:
            """
        else:
            # Access active: calculate remaining time
            try:
                expire_date = parse_date_flexible(expire_date_str)
                now = datetime.now()
                time_left = expire_date - now
                days_left = time_left.days
                hours_left = time_left.seconds // 3600
                minutes_left = (time_left.seconds % 3600) // 60

                status_text = f"""
📊 Статус доступа:

⏰ Доступ закончится: {expire_date_formatted}{devices_line}
                """

                if days_left > 0:
                    status_text += (
                        f"\n⏰ Осталось: {days_left} дн. {hours_left} ч. {minutes_left} мин."
                    )
                elif hours_left > 0:
                    status_text += f"\n⏰ Осталось: {hours_left} ч. {minutes_left} мин."
                else:
                    status_text += f"\n⏰ Осталось: {minutes_left} мин."

                if days_left <= 3:
                    status_text += '\n\n⚠️ Доступ к сервису скоро истекает! Нажми "Продлить доступ" для продления.'

                status_text += "\n\nВыбери действие с помощью кнопок ниже:"
            except (ValueError, TypeError):
                status_text = f"""
📊 Статус доступа:

⏰ Доступ закончится: {expire_date_formatted}{devices_line}

Выбери действие с помощью кнопок ниже:
                """

        await ui_renderer.edit_rich_or_text(
            callback_query.message,
            rich_markdown=(
                "# 📊 Статус подписки\n\n"
                + status_text.replace("📊 Статус доступа:", "").strip()
            ),
            fallback_text=status_text,
            reply_markup=create_main_menu_keyboard(user_id),
        )

    except Exception as e:
        logger.error(f"Failed to fetch peer info: {e}")
        error_text = """
❌ Ошибка при получении информации о пире.

Выбери действие с помощью кнопок ниже:
        """
        await show_menu_from_callback(
            callback_query,
            error_text,
            create_main_menu_keyboard(user_id),
        )
