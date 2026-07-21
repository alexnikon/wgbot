import asyncio
import logging

from aiogram import Bot, F, Router, types
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command

from callbacks import (
    PaymentAction,
    PaymentActionCallback,
    PaymentMethod,
    PaymentMethodCallback,
    RefundConfirmationCallback,
)
from cascade_api import CascadeCapacityError, CascadeRouter
from database import Database
from payment import PaymentManager
from stars import StarsReconciler
from telegram_runtime import UserActionLocks, serialized_user_action
from utils import generate_peer_name

logger = logging.getLogger(__name__)
router = Router(name="payments")


def _parse_legacy_method(
    data: str | None, method: str, payment_manager: PaymentManager
) -> tuple[str, int] | None:
    prefix = f"pay_{method}_"
    if not data or not data.startswith(prefix):
        return None
    try:
        tariff, raw_user_id = data[len(prefix) :].rsplit("_", 1)
        user_id = int(raw_user_id)
    except (TypeError, ValueError):
        return None
    if user_id <= 0 or not payment_manager.is_tariff_enabled(tariff):
        return None
    return tariff, user_id


@router.message(Command("buy"))
async def cmd_buy(message: types.Message, payment_manager: PaymentManager):
    """Handle the /buy command (payment method selection)."""
    user_id = message.from_user.id

    # Send payment method selection
    await payment_manager.send_payment_selection(message.chat.id, user_id)


# Callback button handlers for payment method selection
@router.callback_query(PaymentMethodCallback.filter(F.method == PaymentMethod.STARS))
@router.callback_query(F.data.startswith("pay_stars_"))
async def handle_pay_stars_callback(
    callback_query: types.CallbackQuery,
    payment_manager: PaymentManager,
    cascade_router: CascadeRouter,
    safe_answer_callback,
    safe_edit_callback_message,
    create_back_to_menu_keyboard,
    user_action_locks: UserActionLocks,
    runtime_metrics,
    callback_data: PaymentMethodCallback | None = None,
):
    """Handle Telegram Stars payment selection."""
    # Extract tariff_key and user_id from callback_data (format: pay_stars_14_days_123456789)
    if callback_data is not None:
        tariff_key = callback_data.tariff
        user_id = callback_data.user_id
    else:
        runtime_metrics.telegram_event("legacy_callbacks")
        parsed = _parse_legacy_method(callback_query.data, "stars", payment_manager)
        if parsed is None:
            await safe_answer_callback(callback_query, "❌ Некорректная кнопка оплаты")
            return
        tariff_key, user_id = parsed
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

    async with user_action_locks.hold(user_id):
        try:
            await cascade_router.ensure_reservation(user_id)
        except CascadeCapacityError:
            await safe_edit_callback_message(
                callback_query.message,
                "⚠️ Все VPN серверы временно заполнены. Оплата сейчас недоступна, попробуй позже.",
                reply_markup=create_back_to_menu_keyboard(),
            )
            return

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


@router.callback_query(PaymentMethodCallback.filter(F.method == PaymentMethod.YOOKASSA))
@router.callback_query(F.data.regexp(r"^pay_yookassa_(14|30|90)_days_[0-9]+$"))
async def handle_pay_yookassa_callback(
    callback_query: types.CallbackQuery,
    payment_manager: PaymentManager,
    cascade_router: CascadeRouter,
    safe_answer_callback,
    safe_edit_callback_message,
    create_back_to_menu_keyboard,
    user_action_locks: UserActionLocks,
    runtime_metrics,
    callback_data: PaymentMethodCallback | None = None,
):
    """Handle YooKassa payment selection."""
    # Extract tariff_key and user_id from callback_data (format: pay_yookassa_14_days_123456789)
    if callback_data is not None:
        tariff_key = callback_data.tariff
        user_id = callback_data.user_id
    else:
        runtime_metrics.telegram_event("legacy_callbacks")
        parsed = _parse_legacy_method(callback_query.data, "yookassa", payment_manager)
        if parsed is None:
            await safe_answer_callback(callback_query, "❌ Некорректная кнопка оплаты")
            return
        tariff_key, user_id = parsed
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

    async with user_action_locks.hold(user_id):
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
    async with user_action_locks.hold(user_id):
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

@router.callback_query(
    PaymentMethodCallback.filter(F.method == PaymentMethod.YOOKASSA_DISABLED)
)
@router.callback_query(F.data.startswith("pay_yookassa_disabled_"))
async def handle_pay_yookassa_disabled_callback(
    callback_query: types.CallbackQuery,
    payment_manager: PaymentManager,
    safe_answer_callback,
    safe_edit_callback_message,
    create_back_to_menu_keyboard,
    callback_data: PaymentMethodCallback | None = None,
):
    """Handle clicks on the disabled YooKassa button."""
    if callback_data is not None:
        user_id = callback_data.user_id
    else:
        try:
            raw_user_id = (callback_query.data or "").removeprefix(
                "pay_yookassa_disabled_"
            )
            user_id = int(raw_user_id)
        except ValueError:
            await safe_answer_callback(callback_query, "❌ Некорректная кнопка оплаты")
            return

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


@router.callback_query(
    PaymentActionCallback.filter(F.action == PaymentAction.CANCEL_YOOKASSA)
)
@router.callback_query(F.data.startswith("cancel_yookassa_"))
async def handle_cancel_yookassa_callback(
    callback_query: types.CallbackQuery,
    payment_manager: PaymentManager,
    safe_answer_callback,
    safe_edit_callback_message,
    callback_data: PaymentActionCallback | None = None,
):
    """Return from the YooKassa payment screen to tariff selection."""
    try:
        user_id = callback_data.user_id if callback_data else int(
            (callback_query.data or "").removeprefix("cancel_yookassa_")
        )
    except ValueError:
        await safe_answer_callback(callback_query, "❌ Некорректная кнопка")
        return
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


@router.callback_query(PaymentActionCallback.filter(F.action == PaymentAction.CANCEL_STARS))
@router.callback_query(F.data.startswith("cancel_stars_invoice_"))
async def handle_cancel_stars_invoice_callback(
    callback_query: types.CallbackQuery,
    safe_answer_callback,
    callback_data: PaymentActionCallback | None = None,
):
    """Delete the Stars invoice message on cancel."""
    try:
        user_id = callback_data.user_id if callback_data else int(
            (callback_query.data or "").removeprefix("cancel_stars_invoice_")
        )
    except ValueError:
        await safe_answer_callback(callback_query, "❌ Некорректная кнопка")
        return
    if callback_query.from_user.id != user_id:
        await safe_answer_callback(callback_query, "❌ Ошибка: неверный пользователь")
        return

    await safe_answer_callback(callback_query)
    try:
        await callback_query.message.delete()
    except TelegramAPIError as e:
        logger.error(f"Failed to delete Stars invoice message for user {user_id}: {e}")


# Retry config creation after successful payment if the initial attempt failed
@router.callback_query(PaymentActionCallback.filter(F.action == PaymentAction.RETRY_PEER))
@router.callback_query(F.data.startswith("retry_peer_"))
async def handle_retry_peer_callback(
    callback_query: types.CallbackQuery,
    db: Database,
    safe_answer_callback,
    create_main_menu_keyboard,
    user_action_locks: UserActionLocks,
    callback_data: PaymentActionCallback | None = None,
):
    try:
        if callback_data:
            tariff_key = callback_data.tariff
            passed_user_id = callback_data.user_id
        else:
            remainder = (callback_query.data or "").removeprefix("retry_peer_")
            tariff_key, raw_user_id = remainder.rsplit("_", 1)
            passed_user_id = int(raw_user_id)
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
        async with user_action_locks.hold(user_id):
            task_id = await asyncio.to_thread(
                db.add_provisioning_task,
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
async def process_pre_checkout_query(pre_checkout_query, payment_manager: PaymentManager):
    """Handle pre-checkout validation."""
    logger.info(
        "Incoming pre-checkout operation: user_id=%s",
        pre_checkout_query.from_user.id,
    )
    await payment_manager.process_payment(pre_checkout_query)


@router.message(F.successful_payment)
@serialized_user_action
async def process_successful_payment(
    message: types.Message,
    db: Database,
    cascade_router: CascadeRouter,
    payment_manager: PaymentManager,
    create_home_keyboard,
    create_or_restore_peer_for_user,
    send_config_with_confirmation,
    notify_admins,
    format_admin_payment_notification,
    user_action_locks: UserActionLocks,
):
    """Handle a successful Telegram Stars payment and synchronize Cascade."""
    user_id = message.from_user.id
    username = message.from_user.username
    successful_payment = message.successful_payment
    confirmed, _, amount_paid = await payment_manager.confirm_payment(
        successful_payment, payer_user_id=user_id
    )
    parsed = payment_manager.parse_invoice_payload(successful_payment.invoice_payload)
    if not confirmed or not parsed or parsed[0] != "stars":
        await message.reply("❌ Ошибка при обработке платежа.")
        return

    tariff_key = parsed[1]
    tariff_data = payment_manager.tariffs.get(tariff_key)
    if not tariff_data:
        await message.reply("❌ Ошибка в данных платежа.")
        return

    payment = await asyncio.to_thread(
        db.get_payment_by_invoice_payload, successful_payment.invoice_payload
    )
    if not payment:
        logger.error("Stars payment has no matching intent")
        await message.reply("⚠️ Платеж получен и передан администратору на сверку.")
        return
    intent_matches = (
        int(payment["user_id"]) == user_id
        and int(payment["amount"]) == int(amount_paid)
        and payment["currency"] == "XTR"
        and payment["payment_method"] == "stars"
        and payment["tariff_key"] == tariff_key
    )
    if not intent_matches:
        logger.error("Stars payment does not match its pending intent")
        await asyncio.to_thread(
            db.log_operation,
            f"telegram:{user_id}",
            "stars_payment_discrepancy",
            f"payment_id={payment['payment_id']};reason=intent_mismatch",
        )
        await notify_admins(
            "⚠️ Stars payment requires manual review\n\n"
            f"Payment ID: {payment['payment_id']}\nTelegram ID: {user_id}"
        )
        await message.reply("⚠️ Платеж получен и передан администратору на сверку.")
        return
    payment_id = payment["payment_id"]
    payment_result = await asyncio.to_thread(
        db.apply_verified_payment,
        payment_id,
        user_id,
        username,
        amount_paid,
        "stars",
        tariff_key,
        tariff_data["days"],
        telegram_payment_charge_id=successful_payment.telegram_payment_charge_id,
        provider_payment_charge_id=successful_payment.provider_payment_charge_id,
        invoice_payload=successful_payment.invoice_payload,
        is_recurring=bool(successful_payment.is_recurring),
        is_first_recurring=bool(successful_payment.is_first_recurring),
        subscription_expiration_date=successful_payment.subscription_expiration_date,
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


@router.message(F.refunded_payment)
async def process_refunded_payment(
    message: types.Message,
    db: Database,
    notify_admins,
):
    """Record a Telegram Stars refund without changing VPN access automatically."""
    refund = message.refunded_payment
    matched = await asyncio.to_thread(
        db.mark_stars_refund_observed,
        refund.telegram_payment_charge_id,
        refund.total_amount,
    )
    await asyncio.to_thread(
        db.record_star_transaction,
        refund.telegram_payment_charge_id,
        "outgoing",
        refund.total_amount,
        int(message.date.timestamp()),
        transaction_type="invoice_payment",
        user_id=message.from_user.id if message.from_user else None,
        invoice_payload=refund.invoice_payload,
        status="refund_pending_review" if matched else "discrepancy",
    )
    await asyncio.to_thread(
        db.log_operation,
        f"telegram:{message.from_user.id if message.from_user else 'unknown'}",
        "stars_refund_observed",
        f"charge_matched={int(matched)}",
    )
    await notify_admins(
        "⚠️ Telegram Stars сообщил о возврате\n\n"
        f"Charge ID: {refund.telegram_payment_charge_id}\n"
        f"Telegram ID: {message.from_user.id if message.from_user else 'unknown'}\n"
        "Доступ автоматически не изменен."
    )


@router.message(Command("payments"))
async def cmd_payments(message: types.Message, db: Database, is_admin):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Недостаточно прав.")
        return
    payments = await asyncio.to_thread(db.list_recent_payments, 10)
    lines = ["💳 Последние платежи"]
    for payment in payments:
        lines.append(
            f"{payment['payment_id']} | {payment['user_id']} | "
            f"{payment['payment_method']} | {payment['status']} | {payment['amount']}"
        )
    latest = await asyncio.to_thread(db.get_latest_star_reconciliation_run)
    if latest:
        lines.append(
            "\nПоследняя сверка: "
            f"{latest['status']}, расхождений: {latest['discrepancy_count']}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("stars_reconcile"))
async def cmd_stars_reconcile(
    message: types.Message,
    stars_reconciler: StarsReconciler,
    is_admin,
):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Недостаточно прав.")
        return
    result = await stars_reconciler.run_once()
    await message.answer(stars_reconciler.format_report(result))


@router.message(Command("refund_stars"))
async def cmd_refund_stars(message: types.Message, db: Database, is_admin):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Недостаточно прав.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Использование: /refund_stars <telegram_charge_id>")
        return
    payment = await asyncio.to_thread(db.get_payment_by_telegram_charge, parts[1].strip())
    if not payment or payment["payment_method"] != "stars":
        await message.answer("❌ Платеж Telegram Stars не найден.")
        return
    later_payments = [
        item
        for item in await asyncio.to_thread(db.list_recent_payments, 100)
        if item["user_id"] == payment["user_id"] and item["id"] > payment["id"]
    ]
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="Подтвердить возврат",
                    callback_data=RefundConfirmationCallback(
                        payment_id=payment["payment_id"]
                    ).pack(),
                )
            ]
        ]
    )
    await message.answer(
        "Подтвердить возврат Telegram Stars?\n\n"
        f"Telegram ID: {payment['user_id']}\n"
        f"Сумма: {payment['amount']} Stars\n"
        f"Тариф: {payment['tariff_key']}\n"
        f"Более поздних платежей: {len(later_payments)}\n\n"
        "VPN-доступ автоматически изменен не будет.",
        reply_markup=keyboard,
    )


@router.callback_query(RefundConfirmationCallback.filter())
async def confirm_stars_refund(
    callback_query: types.CallbackQuery,
    callback_data: RefundConfirmationCallback,
    bot: Bot,
    db: Database,
    safe_answer_callback,
    is_admin,
):
    if not is_admin(callback_query.from_user.id):
        await safe_answer_callback(callback_query, "❌ Недостаточно прав.")
        return
    await safe_answer_callback(callback_query)
    payment = await asyncio.to_thread(db.get_payment_by_id, callback_data.payment_id)
    if not payment or not payment.get("telegram_payment_charge_id"):
        await callback_query.message.edit_text("❌ Платеж не найден.")
        return
    claimed = await asyncio.to_thread(db.claim_stars_refund_request, payment["payment_id"])
    if not claimed:
        await callback_query.message.edit_text("Возврат уже запрошен или обработан.")
        return
    try:
        await bot.refund_star_payment(
            user_id=payment["user_id"],
            telegram_payment_charge_id=payment["telegram_payment_charge_id"],
        )
    except Exception:
        await asyncio.to_thread(
            db.update_refund_request_status, payment["payment_id"], "request_failed"
        )
        raise
    await asyncio.to_thread(
        db.update_refund_request_status, payment["payment_id"], "completed"
    )
    db.log_operation(
        f"telegram:{payment['user_id']}",
        "stars_refund_requested",
        f"payment_id={payment['payment_id']}",
    )
    await callback_query.message.edit_text(
        "✅ Возврат отправлен Telegram. VPN-доступ оставлен без изменений."
    )


# Unknown command handler
