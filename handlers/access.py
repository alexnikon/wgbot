import logging

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cascade_api import CascadeNotFound, CascadeRouter
from database import Database
from payment import PaymentManager
from telegram_runtime import serialized_user_action
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


@router.message(Command("connect"))
@serialized_user_action
async def cmd_connect(
    message: types.Message,
    db: Database,
    cascade_router: CascadeRouter,
    payment_manager: PaymentManager,
    create_or_restore_peer_for_user,
    send_config_with_confirmation,
    is_access_active,
    user_action_locks,
):
    """Handle the /connect command."""
    user_id = message.from_user.id
    username = message.from_user.username

    # Check if the user already has an active peer
    existing_peer = db.get_peer_by_telegram_id(user_id)
    if existing_peer:
        # Check if access is active (paid and not expired)
        if not is_access_active(existing_peer):
            # Access expired or not paid
            payment_info = payment_manager.get_payment_info()
            if existing_peer.get("payment_status") == "paid":
                # Access was paid but expired
                expire_date_str = existing_peer.get("expire_date", "Неизвестно")
                expire_date_formatted = (
                    format_date_for_user(expire_date_str)
                    if expire_date_str != "Неизвестно"
                    else "Неизвестно"
                )
                await message.reply(
                    f"⚠️ Твой VPN доступ истек!\n\n"
                    f"📅 Дата истечения: {expire_date_formatted}\n\n"
                    f"💎 Для получения конфигурации необходимо продлить доступ.\n\n"
                    f"Стоимость за {payment_info['period']}:\n"
                    f"⭐ Telegram Stars: {payment_info['stars_price']} Stars\n"
                    f"💳 Банковская карта: {payment_info['rub_price']} руб."
                )
            else:
                # Access not paid
                await message.reply(
                    f"❌ Доступ не оплачен!\n\n"
                    f"💎 Стоимость за {payment_info['period']}:\n"
                    f"⭐ Telegram Stars: {payment_info['stars_price']} Stars\n"
                    f"💳 Банковская карта: {payment_info['rub_price']} руб.\n\n"
                    f"Для получения конфигурации необходимо оплатить доступ."
                )

            # Send payment method selection
            await payment_manager.send_payment_selection(message.chat.id, user_id)
            return

        # User has active access. Recreate only after an explicit Cascade 404.
        try:
            try:
                progress_message = await message.reply("Скачиваю конфиг...")
                config_content = await cascade_router.get_primary_config(user_id)
                await send_config_with_confirmation(
                    message.chat.id,
                    config_content,
                    source_message=progress_message,
                )
                return
            except CascadeNotFound:
                progress_message = await message.reply("Создаю новый конфиг...")
                ok, err, new_config = await create_or_restore_peer_for_user(
                    user_id, username, existing_peer.get("tariff_key")
                )
                if not ok:
                    await message.reply(f"❌ {err}")
                    return
                if not await send_config_with_confirmation(
                    message.chat.id,
                    new_config,
                    source_message=progress_message,
                ):
                    await message.reply(
                        "❌ Не удалось отправить конфигурацию. Используй /connect для повторной попытки."
                    )
                return
        except Exception as e:
            logger.error(f"Error while getting config in /connect: {e}", exc_info=True)
            await message.reply(
                "❌ Ошибка при получении конфигурации. Попробуй позже или обратись в поддержку."
            )

    # New user: payment required
    payment_info = payment_manager.get_payment_info()
    await message.reply(
        f"💎 Для получения VPN конфигурации необходимо оплатить доступ!\n\n"
        f"Стоимость за {payment_info['period']}:\n"
        f"⭐ Telegram Stars: {payment_info['stars_price']} Stars\n"
        f"💳 Картой (Юmoney): {payment_info['rub_price']} руб.\n\n"
        f"После оплаты предоставим тебе конфигурацию и доступ на {payment_info['period']}."
    )

    # Send payment method selection
    await payment_manager.send_payment_selection(message.chat.id, user_id)


@router.message(Command("extend"))
async def cmd_extend(message: types.Message, db: Database, payment_manager: PaymentManager):
    """Handle the /extend command (access extension)."""
    user_id = message.from_user.id

    # Check if the user has an active peer
    existing_peer = db.get_peer_by_telegram_id(user_id)
    if not existing_peer:
        await message.reply(
            "❌ У тебя нет активного VPN доступа.\nИспользуй /connect для создания нового."
        )
        return

    # Check if current access is paid
    if existing_peer.get("payment_status") != "paid":
        await message.reply("❌ Доступ не оплачен.\nИспользуй /connect для оплаты.")
        return

    payment_info = payment_manager.get_payment_info()
    await message.reply(
        f"💎 Продление доступа на {payment_info['period']}\n\n"
        f"Стоимость:\n"
        f"⭐ Telegram Stars: {payment_info['stars_price']} Stars\n"
        f"💳 Банковская карта: {payment_info['rub_price']} руб.\n\n"
        f"После оплаты доступ будет продлен на {payment_info['period']}."
    )

    # Send payment method selection for extension
    await payment_manager.send_payment_selection(message.chat.id, user_id)


@router.message(Command("status"))
async def cmd_status(message: types.Message, db: Database, ui_renderer):
    """Handle the /status command (remaining access time)."""
    user_id = message.from_user.id

    # Check if the user has an active peer
    existing_peer = db.get_peer_by_telegram_id(user_id)
    if not existing_peer:
        await message.reply(
            "❌ Нет активного VPN доступа.\nИспользуй /connect для создания нового."
        )
        return

    # Check if access is paid
    if existing_peer.get("payment_status") != "paid":
        await message.reply("❌ Доступ не оплачен.\nИспользуй /connect для оплаты.")
        return

    # Get expiration date
    expire_date_str = existing_peer.get("expire_date")
    if not expire_date_str:
        await message.reply("❌ Не удалось получить информацию о сроке доступа.")
        return

    try:
        from datetime import datetime

        expire_date = parse_date_flexible(expire_date_str)
        now = datetime.now()

        if expire_date <= now:
            await message.reply(
                "⚠️ Оплаченный период закончился, для возобновления доступа к сервису, необходимо оплатить доступ.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="💵 Оплатить доступ",
                                callback_data="pay",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="На главную",
                                callback_data="main",
                            )
                        ],
                    ]
                ),
            )
            return

        # Calculate remaining time
        time_left = expire_date - now
        days_left = time_left.days
        hours_left = time_left.seconds // 3600
        minutes_left = (time_left.seconds % 3600) // 60

        # Build message
        status_text = "📊 Статус твоего VPN доступа:\n\n"
        status_text += f"📅 Дата истечения: {expire_date.strftime('%d.%m.%Y')}\n\n"

        if days_left > 0:
            status_text += f"⏰ Осталось: {days_left} дн. {hours_left} ч. {minutes_left} мин."
        elif hours_left > 0:
            status_text += f"⏰ Осталось: {hours_left} ч. {minutes_left} мин."
        else:
            status_text += f"⏰ Осталось: {minutes_left} мин."

        if days_left <= 3:
            status_text += (
                '\n\n⚠️ Доступ к сервису скоро истекает! Нажми "Продлить доступ" для продления.'
            )

        await ui_renderer.send_rich_or_text(
            message.chat.id,
            rich_markdown=(
                "# 📊 Статус подписки\n\n"
                f"**Действует до:** {expire_date.strftime('%d.%m.%Y %H:%M')}\n\n"
                + status_text.split("\n\n", 2)[-1]
            ),
            fallback_text=status_text,
        )

    except ValueError as e:
        logger.error(f"Failed to parse expiration date: {e}")
        await message.reply("❌ Ошибка при получении информации о доступе.")
