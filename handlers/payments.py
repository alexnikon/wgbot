import asyncio
import logging
from typing import Any

from aiogram import F, Router, types
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command

from cascade_api import CascadeCapacityError
from utils import generate_peer_name

logger = logging.getLogger(__name__)
router = Router(name="payments")

bot: Any
db: Any
cascade_router: Any
payment_manager: Any
safe_answer_callback: Any
safe_edit_callback_message: Any
create_back_to_menu_keyboard: Any
create_home_keyboard: Any
create_main_menu_keyboard: Any
create_or_restore_peer_for_user: Any
send_config_with_confirmation: Any
notify_admins: Any
format_admin_payment_notification: Any


def configure(
    *,
    runtime_bot: Any,
    runtime_db: Any,
    runtime_cascade_router: Any,
    runtime_payment_manager: Any,
    answer_callback: Any,
    edit_callback_message: Any,
    back_to_menu_keyboard: Any,
    home_keyboard: Any,
    main_menu_keyboard: Any,
    create_or_restore_peer: Any,
    send_config: Any,
    admin_notifier: Any,
    admin_payment_formatter: Any,
) -> None:
    """Inject runtime services and shared payment helpers."""
    global bot, db, cascade_router, payment_manager
    global safe_answer_callback, safe_edit_callback_message
    global create_back_to_menu_keyboard, create_home_keyboard
    global create_main_menu_keyboard, create_or_restore_peer_for_user
    global send_config_with_confirmation, notify_admins
    global format_admin_payment_notification
    bot = runtime_bot
    db = runtime_db
    cascade_router = runtime_cascade_router
    payment_manager = runtime_payment_manager
    safe_answer_callback = answer_callback
    safe_edit_callback_message = edit_callback_message
    create_back_to_menu_keyboard = back_to_menu_keyboard
    create_home_keyboard = home_keyboard
    create_main_menu_keyboard = main_menu_keyboard
    create_or_restore_peer_for_user = create_or_restore_peer
    send_config_with_confirmation = send_config
    notify_admins = admin_notifier
    format_admin_payment_notification = admin_payment_formatter


@router.message(Command("buy"))
async def cmd_buy(message: types.Message):
    """Handle the /buy command (payment method selection)."""
    user_id = message.from_user.id

    # Send payment method selection
    await payment_manager.send_payment_selection(message.chat.id, user_id)


# Callback button handlers for payment method selection
@router.callback_query(F.data.startswith("pay_stars_"))
async def handle_pay_stars_callback(callback_query: types.CallbackQuery):
    """Handle Telegram Stars payment selection."""
    # Extract tariff_key and user_id from callback_data (format: pay_stars_14_days_123456789)
    callback_parts = callback_query.data.split("_")
    tariff_key = f"{callback_parts[2]}_{callback_parts[3]}"  # 14_days, 30_days or 90_days
    user_id = int(callback_parts[-1])  # Last part is user_id
    username = callback_query.from_user.username

    # Ensure callback belongs to the correct user
    if callback_query.from_user.id != user_id:
        await safe_answer_callback(callback_query, "❌ Ошибка: неверный пользователь")
        return

    if not payment_manager.is_tariff_enabled(tariff_key):
        await safe_answer_callback(callback_query, "Этот тариф сейчас недоступен")
        payment_text, keyboard = await payment_manager.get_payment_selection_view(user_id)
        await safe_edit_callback_message(
            callback_query.message,
            payment_text,
            reply_markup=keyboard,
        )
        return

    await safe_answer_callback(callback_query)

    try:
        await cascade_router.ensure_reservation(user_id)
    except CascadeCapacityError:
        await safe_edit_callback_message(
            callback_query.message,
            "⚠️ Все VPN серверы временно заполнены. Оплата сейчас недоступна, попробуй позже.",
            reply_markup=create_back_to_menu_keyboard(),
        )
        return

    # Send invoice for Stars payment
    success = await payment_manager.send_stars_payment_request(
        callback_query.message.chat.id, user_id, tariff_key, username
    )

    if not success:
        user_tariffs = payment_manager.get_user_tariffs(user_id)
        tariff_data = user_tariffs.get(tariff_key, {})
        tariff_name = tariff_data.get("name", "неизвестный тариф")
        stars_price = tariff_data.get("stars_price", 1)
        await callback_query.message.reply(
            f"❌ Ошибка при создании запроса на оплату через Telegram Stars.\n\n"
            f"💡 Убедись, что у тебя есть Telegram Stars на балансе.\n"
            f"⭐ Стоимость: {stars_price} Stars за {tariff_name} доступа"
        )


@router.callback_query(F.data.startswith("pay_yookassa_"))
async def handle_pay_yookassa_callback(callback_query: types.CallbackQuery):
    """Handle YooKassa payment selection."""
    # Extract tariff_key and user_id from callback_data (format: pay_yookassa_14_days_123456789)
    callback_parts = callback_query.data.split("_")
    tariff_key = f"{callback_parts[2]}_{callback_parts[3]}"  # 14_days, 30_days or 90_days
    user_id = int(callback_parts[-1])  # Last part is user_id
    username = callback_query.from_user.username

    # Ensure callback belongs to the correct user
    if callback_query.from_user.id != user_id:
        await safe_answer_callback(callback_query, "❌ Ошибка: неверный пользователь")
        return

    if not payment_manager.is_tariff_enabled(tariff_key):
        await safe_answer_callback(callback_query, "Этот тариф сейчас недоступен")
        payment_text, keyboard = await payment_manager.get_payment_selection_view(user_id)
        await safe_edit_callback_message(
            callback_query.message,
            payment_text,
            reply_markup=keyboard,
        )
        return

    await safe_answer_callback(callback_query)

    try:
        await cascade_router.ensure_reservation(user_id)
    except CascadeCapacityError:
        await safe_edit_callback_message(
            callback_query.message,
            "⚠️ Все VPN серверы временно заполнены. Оплата сейчас недоступна, попробуй позже.",
            reply_markup=create_back_to_menu_keyboard(),
        )
        return

    # Check if YooKassa is configured
    if (
        not payment_manager.yookassa_client.shop_id
        or not payment_manager.yookassa_client.secret_key
    ):
        await safe_edit_callback_message(
            callback_query.message,
            "❌ Оплата через банковскую карту временно недоступна.\n\n"
            "💡 Используйте оплату через Telegram Stars.\n\n"
            "🔧 Для настройки ЮKassa обратитесь к администратору.",
            reply_markup=create_back_to_menu_keyboard(),
        )
        return

    payment_chat_id = callback_query.message.chat.id if callback_query.message else None
    payment_message_id = callback_query.message.message_id if callback_query.message else None
    payment_view = await payment_manager.get_yookassa_payment_view(
        user_id,
        tariff_key,
        username,
        payment_chat_id=payment_chat_id,
        payment_message_id=payment_message_id,
    )
    if not payment_view:
        user_tariffs = payment_manager.get_user_tariffs(user_id)
        tariff_data = user_tariffs.get(tariff_key, {})
        tariff_name = tariff_data.get("name", "неизвестный тариф")
        rub_price = tariff_data.get("rub_price", 0)
        await safe_edit_callback_message(
            callback_query.message,
            f"❌ Ошибка при создании запроса на оплату через ЮKassa.\n\n"
            f"🔧 Возможные причины:\n"
            f"• Проблемы с настройкой платежей\n\n"
            f"💡 Используйте оплату через Telegram Stars.\n"
            f"💳 Стоимость: {rub_price} руб. за {tariff_name} доступа",
            reply_markup=create_back_to_menu_keyboard(),
        )
        return

    payment_text, keyboard = payment_view
    await safe_edit_callback_message(
        callback_query.message,
        payment_text,
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("pay_yookassa_disabled_"))
async def handle_pay_yookassa_disabled_callback(callback_query: types.CallbackQuery):
    """Handle clicks on the disabled YooKassa button."""
    user_id = int(callback_query.data.replace("pay_yookassa_disabled_", ""))

    # Ensure callback belongs to the correct user
    if callback_query.from_user.id != user_id:
        await safe_answer_callback(callback_query, "❌ Ошибка: неверный пользователь")
        return

    await safe_answer_callback(callback_query)

    await safe_edit_callback_message(
        callback_query.message,
        "❌ Оплата через банковскую карту временно недоступна.\n\n"
        "💡 Используй оплату через Telegram Stars:\n"
        "⭐ 1 Starsа за 30 дней доступа\n\n"
        "🔧 Для настройки ЮKassa обратитесь к администратору.",
        reply_markup=create_back_to_menu_keyboard(),
    )


@router.callback_query(F.data.startswith("cancel_yookassa_"))
async def handle_cancel_yookassa_callback(callback_query: types.CallbackQuery):
    """Return from the YooKassa payment screen to tariff selection."""
    user_id = int(callback_query.data.replace("cancel_yookassa_", ""))
    if callback_query.from_user.id != user_id:
        await safe_answer_callback(callback_query, "❌ Ошибка: неверный пользователь")
        return

    await safe_answer_callback(callback_query)
    payment_text, keyboard = await payment_manager.get_payment_selection_view(user_id)
    await safe_edit_callback_message(
        callback_query.message,
        payment_text,
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("cancel_stars_invoice_"))
async def handle_cancel_stars_invoice_callback(callback_query: types.CallbackQuery):
    """Delete the Stars invoice message on cancel."""
    user_id = int(callback_query.data.replace("cancel_stars_invoice_", ""))
    if callback_query.from_user.id != user_id:
        await safe_answer_callback(callback_query, "❌ Ошибка: неверный пользователь")
        return

    await safe_answer_callback(callback_query)
    try:
        await callback_query.message.delete()
    except TelegramAPIError as e:
        logger.error(f"Failed to delete Stars invoice message for user {user_id}: {e}")


# Retry config creation after successful payment if the initial attempt failed
@router.callback_query(F.data.startswith("retry_peer_"))
async def handle_retry_peer_callback(callback_query: types.CallbackQuery):
    try:
        parts = callback_query.data.split("_")
        # retry_peer_{tariff_key}_{user_id}
        tariff_key = f"{parts[2]}_{parts[3]}" if len(parts) >= 5 else parts[2]
        passed_user_id = int(parts[-1])
        if callback_query.from_user.id != passed_user_id:
            await safe_answer_callback(callback_query, "❌ Ошибка: неверный пользователь")
            return
        await safe_answer_callback(callback_query)

        user_id = callback_query.from_user.id
        username = callback_query.from_user.username

        subscription = db.get_peer_by_telegram_id(user_id)
        if not subscription or not subscription.get("expire_date"):
            await callback_query.message.edit_text(
                "❌ Активная подписка не найдена.",
                reply_markup=create_main_menu_keyboard(user_id),
            )
            return
        task_id = db.add_provisioning_task(
            user_id,
            "create_peer",
            {
                "username": username or "",
                "peer_name": generate_peer_name(username, user_id),
                "expire_date": subscription["expire_date"],
                "tariff_key": tariff_key,
            },
            "Manual retry requested by user",
        )
        logger.info("User %s requested provisioning retry task %s", user_id, task_id)
        await callback_query.message.edit_text(
            "🔄 Создание доступа поставлено в очередь. Бот отправит конфиг автоматически.",
            reply_markup=create_main_menu_keyboard(user_id),
        )
    except Exception as e:
        logger.error(f"Error in retry_peer handler: {e}")
        await callback_query.message.edit_text(
            "❌ Ошибка при повторном создании. Попробуй ещё раз позже.",
            reply_markup=create_main_menu_keyboard(callback_query.from_user.id),
        )


# Payment handlers
@router.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query):
    """Handle pre-checkout validation."""
    logger.info(
        f"Incoming pre-checkout operation: user_id={pre_checkout_query.from_user.id}, payload={pre_checkout_query.invoice_payload}"
    )
    await payment_manager.process_payment(pre_checkout_query)


@router.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    """Handle a successful Telegram Stars payment and synchronize Cascade."""
    user_id = message.from_user.id
    username = message.from_user.username
    successful_payment = message.successful_payment
    confirmed, _, amount_paid = await payment_manager.confirm_payment(
        successful_payment, payer_user_id=user_id
    )
    if not confirmed or not successful_payment.invoice_payload.startswith("vpn_access_stars_"):
        await message.reply("❌ Ошибка при обработке платежа.")
        return

    parts = successful_payment.invoice_payload.split("_")
    tariff_key = f"{parts[3]}_{parts[4]}" if len(parts) >= 5 else ""
    tariff_data = payment_manager.tariffs.get(tariff_key)
    if not tariff_data:
        await message.reply("❌ Ошибка в данных платежа.")
        return

    payment_id = (
        getattr(successful_payment, "telegram_payment_charge_id", None)
        or getattr(successful_payment, "provider_payment_charge_id", None)
        or f"stars_{user_id}_{tariff_key}"
    )
    await asyncio.to_thread(
        db.add_payment,
        payment_id,
        user_id,
        amount_paid,
        "stars",
        tariff_key,
        {"source": "telegram_stars"},
    )
    payment_result = await asyncio.to_thread(
        db.apply_verified_payment,
        payment_id,
        user_id,
        username,
        amount_paid,
        "stars",
        tariff_key,
        tariff_data["days"],
    )
    if not payment_result:
        logger.info("Ignoring duplicate Stars payment event %s", payment_id)
        return

    expire_date = payment_result["expire_date"]
    primary_peer = await asyncio.to_thread(db.get_primary_client_peer, user_id)
    if primary_peer:
        sync_result = await cascade_router.sync_user_access(user_id, expire_date)
        if sync_result["failed"]:
            db.add_provisioning_task(
                user_id,
                "sync_access",
                {"expire_date": expire_date},
                f"Failed peers: {sync_result['failed']}",
            )
        await message.reply(
            f"✅ Платеж успешно обработан!\n"
            f"🎉 Продлили тебе доступ на {tariff_data['days']} дней!\n"
            f"💳 Способ оплаты: ⭐ Telegram Stars\n\n"
            f"Текущая конфигурация остается актуальной.",
            reply_markup=create_home_keyboard(),
        )
        title = "🔁 Клиент продлил подписку"
    else:
        await message.reply("🔄 Создаю VPN доступ...")
        ok, error, config = await create_or_restore_peer_for_user(user_id, username, tariff_key)
        if not ok:
            await message.reply(
                f"⚠️ {error}. Мы повторим создание автоматически.",
                reply_markup=create_home_keyboard(),
            )
            await notify_admins(
                f"⚠️ Оплата получена, provisioning отложен\n\nTelegram ID: {user_id}\nПричина: {error}"
            )
            return
        if not await send_config_with_confirmation(message.chat.id, config, caption=None):
            await message.reply(
                "✅ Доступ активирован, но конфиг не удалось отправить. Используй /connect.",
                reply_markup=create_home_keyboard(),
            )
        title = "🆕 Новый клиент подключился"

    await notify_admins(
        format_admin_payment_notification(
            title,
            user_id=user_id,
            username=username,
            tariff_name=tariff_data.get("name", tariff_key),
            amount=f"{amount_paid} Stars",
            payment_method="Telegram Stars",
            expire_date=expire_date,
        )
    )


# Unknown command handler
