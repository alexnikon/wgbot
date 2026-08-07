import asyncio
import logging

from aiogram import Bot, F, Router, types
from aiogram.exceptions import TelegramAPIError

from callbacks import (
    PaymentAction,
    PaymentActionCallback,
    PaymentMethod,
    PaymentMethodCallback,
    RefundConfirmationCallback,
    StarApprovalCallback,
    YooKassaCancelCallback,
)
from cascade_api import CascadeRouter
from database import Database
from message_templates import format_remaining_until, payment_success_message
from payment import PaymentManager
from stars import StarsReconciler
from telegram_runtime import UserActionLocks, edit_bound_message, serialized_user_action

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
    except TypeError, ValueError:
        return None
    if user_id <= 0 or not payment_manager.is_tariff_enabled(tariff):
        return None
    return tariff, user_id


# Callback button handlers for payment method selection
@router.callback_query(PaymentMethodCallback.filter(F.method == PaymentMethod.STARS))
@router.callback_query(F.data.startswith("pay_stars_"))
async def handle_pay_stars_callback(
    callback_query: types.CallbackQuery,
    payment_manager: PaymentManager,
    safe_answer_callback,
    safe_edit_callback_message,
    create_back_to_menu_keyboard,
    user_action_locks: UserActionLocks,
    runtime_metrics,
    callback_data: PaymentMethodCallback | None = None,
    db: Database | None = None,
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
    if db is not None and db.get_client_access_state(user_id).source == "complimentary":
        await safe_answer_callback(callback_query, "🎁 Бесплатный доступ уже активен")
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
        success = await payment_manager.send_stars_payment_request(
            callback_query.message.chat.id, user_id, tariff_key, username
        )

    if not success:
        user_tariffs = payment_manager.get_user_tariffs(user_id)
        tariff_data = user_tariffs.get(tariff_key, {})
        tariff_name = tariff_data.get("name", "неизвестный тариф")
        stars_price = tariff_data.get("stars_price", 1)
        await safe_edit_callback_message(
            callback_query.message,
            f"❌ Ошибка при создании запроса на оплату через Telegram Stars.\n\n"
            f"💡 Убедись, что у тебя есть Telegram Stars на балансе.\n"
            f"⭐ Стоимость: {stars_price} Stars за {tariff_name} доступа",
            reply_markup=create_back_to_menu_keyboard(),
        )
    else:
        await safe_edit_callback_message(
            callback_query.message,
            "⭐ Счёт Telegram Stars отправлен отдельным сообщением.\n\n"
            "После оплаты эта панель обновится автоматически.",
            reply_markup=create_back_to_menu_keyboard(),
        )


@router.callback_query(PaymentMethodCallback.filter(F.method == PaymentMethod.YOOKASSA))
@router.callback_query(F.data.regexp(r"^pay_yookassa_(14|30|90)_days_[0-9]+$"))
async def handle_pay_yookassa_callback(
    callback_query: types.CallbackQuery,
    payment_manager: PaymentManager,
    safe_answer_callback,
    safe_edit_callback_message,
    create_back_to_menu_keyboard,
    user_action_locks: UserActionLocks,
    runtime_metrics,
    callback_data: PaymentMethodCallback | None = None,
    db: Database | None = None,
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
    if db is not None and db.get_client_access_state(user_id).source == "complimentary":
        await safe_answer_callback(callback_query, "🎁 Бесплатный доступ уже активен")
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


@router.callback_query(PaymentMethodCallback.filter(F.method == PaymentMethod.YOOKASSA_DISABLED))
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
            raw_user_id = (callback_query.data or "").removeprefix("pay_yookassa_disabled_")
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


@router.callback_query(YooKassaCancelCallback.filter())
@router.callback_query(PaymentActionCallback.filter(F.action == PaymentAction.CANCEL_YOOKASSA))
@router.callback_query(F.data.startswith("cancel_yookassa_"))
async def handle_cancel_yookassa_callback(
    callback_query: types.CallbackQuery,
    payment_manager: PaymentManager,
    safe_answer_callback,
    safe_edit_callback_message,
    callback_data: YooKassaCancelCallback | PaymentActionCallback | None = None,
):
    """Cancel a pending YooKassa attempt and return to tariff selection."""
    payment_id = (
        callback_data.payment_id
        if isinstance(callback_data, YooKassaCancelCallback)
        else None
    )
    if payment_id:
        payment = await asyncio.to_thread(
            payment_manager.db.get_payment_by_id,
            payment_id,
        )
        if not payment or payment.get("payment_method") != "yookassa":
            await safe_answer_callback(callback_query, "❌ Платеж не найден")
            return
        user_id = int(payment["user_id"])
    else:
        try:
            user_id = (
                callback_data.user_id
                if isinstance(callback_data, PaymentActionCallback)
                else int((callback_query.data or "").removeprefix("cancel_yookassa_"))
            )
        except ValueError:
            await safe_answer_callback(callback_query, "❌ Некорректная кнопка")
            return
    if callback_query.from_user.id != user_id:
        await safe_answer_callback(callback_query, "❌ Ошибка: неверный пользователь")
        return

    if payment_id:
        canceled = await asyncio.to_thread(
            payment_manager.db.cancel_pending_payment,
            payment_id,
        )
        if canceled:
            await safe_answer_callback(callback_query, "✅ Платеж отменен")
        else:
            payment = await asyncio.to_thread(
                payment_manager.db.get_payment_by_id,
                payment_id,
            )
            status = payment.get("status") if payment else None
            if status == "succeeded":
                await safe_answer_callback(callback_query, "✅ Платеж уже обработан")
            elif status == "canceled":
                await safe_answer_callback(callback_query, "Платеж уже отменен")
            else:
                await safe_answer_callback(callback_query, "Платеж уже завершен")
    else:
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
    db: Database,
    payment_manager: PaymentManager,
    chat_panel,
    safe_answer_callback,
    callback_data: PaymentActionCallback | None = None,
):
    """Delete the Stars invoice message on cancel."""
    try:
        user_id = (
            callback_data.user_id
            if callback_data
            else int((callback_query.data or "").removeprefix("cancel_stars_invoice_"))
        )
    except ValueError:
        await safe_answer_callback(callback_query, "❌ Некорректная кнопка")
        return
    if callback_query.from_user.id != user_id:
        await safe_answer_callback(callback_query, "❌ Ошибка: неверный пользователь")
        return

    await safe_answer_callback(callback_query)
    invoice_payload = getattr(getattr(callback_query.message, "invoice", None), "payload", None)
    try:
        await callback_query.message.delete()
    except TelegramAPIError as e:
        logger.error(f"Failed to delete Stars invoice message for user {user_id}: {e}")
    if invoice_payload:
        await asyncio.to_thread(db.set_stars_invoice_message, invoice_payload, None)
    payment_text, keyboard = await payment_manager.get_payment_selection_view(user_id)
    await chat_panel.render(
        callback_query.message.chat.id,
        user_id,
        f"Счёт Telegram Stars отменён.\n\n{payment_text.strip()}",
        keyboard,
    )


# Handle legacy retry buttons from releases that provisioned automatically.
@router.callback_query(PaymentActionCallback.filter(F.action == PaymentAction.RETRY_PEER))
@router.callback_query(F.data.startswith("retry_peer_"))
async def handle_retry_peer_callback(
    callback_query: types.CallbackQuery,
    db: Database,
    safe_answer_callback,
    create_main_menu_keyboard,
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
        if not db.has_active_access(user_id):
            await edit_bound_message(
                callback_query.message,
                "❌ Активная подписка не найдена.",
                reply_markup=create_main_menu_keyboard(user_id),
            )
            return
        await edit_bound_message(
            callback_query.message,
            "✅ Доступ активен. Создай файл конфигурации через главное меню.",
            reply_markup=create_main_menu_keyboard(user_id),
        )
    except Exception as e:
        logger.error(f"Error in retry_peer handler: {e}")
        await edit_bound_message(
            callback_query.message,
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
    notify_admins,
    format_admin_payment_notification,
    user_action_locks: UserActionLocks,
    chat_panel,
    create_main_menu_keyboard,
):
    """Handle a successful Telegram Stars payment and synchronize Cascade."""
    user_id = message.from_user.id
    ban_checker = getattr(db, "is_client_banned", None)
    ban_value = await asyncio.to_thread(ban_checker, user_id) if callable(ban_checker) else False
    is_banned = ban_value is True or (isinstance(ban_value, int) and ban_value == 1)
    username = message.from_user.username
    successful_payment = message.successful_payment
    await chat_panel.delete_user_message(message)
    confirmed, _, amount_paid = await payment_manager.confirm_payment(
        successful_payment, payer_user_id=user_id
    )
    parsed = payment_manager.parse_invoice_payload(successful_payment.invoice_payload)
    if not confirmed or not parsed or parsed[0] != "stars":
        if not is_banned:
            await chat_panel.render(
                message.chat.id,
                user_id,
                "❌ Ошибка при обработке платежа.",
                create_main_menu_keyboard(user_id),
            )
        return

    tariff_key = parsed[1]
    tariff_data = payment_manager.tariffs.get(tariff_key)
    if not tariff_data:
        if not is_banned:
            await chat_panel.render(
                message.chat.id,
                user_id,
                "❌ Ошибка в данных платежа.",
                create_main_menu_keyboard(user_id),
            )
        return

    payment = await asyncio.to_thread(
        db.get_payment_by_invoice_payload, successful_payment.invoice_payload
    )
    if not payment:
        logger.error("Stars payment has no matching intent")
        if not is_banned:
            await chat_panel.render(
                message.chat.id,
                user_id,
                "⚠️ Платеж получен и передан администратору на сверку.",
                create_main_menu_keyboard(user_id),
            )
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
        if not is_banned:
            await chat_panel.render(
                message.chat.id,
                user_id,
                "⚠️ Платеж получен и передан администратору на сверку.",
                create_main_menu_keyboard(user_id),
            )
        return
    invoice_message_id = payment.get("invoice_message_id")
    if invoice_message_id:
        try:
            await message.bot.delete_message(message.chat.id, int(invoice_message_id))
        except TelegramAPIError:
            logger.debug("Unable to delete paid Stars invoice for user %s", user_id)
        await asyncio.to_thread(
            db.set_stars_invoice_message, successful_payment.invoice_payload, None
        )
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
    if is_banned:
        sync_result = await cascade_router.sync_client_state(user_id)
        if sync_result["failed"]:
            db.add_provisioning_task(
                user_id,
                "sync_client_state",
                {},
                f"Failed peers: {sync_result['failed']}",
            )
        await notify_admins(
            format_admin_payment_notification(
                "🚫 Забаненный клиент оплатил подписку",
                user_id=user_id,
                username=username,
                tariff_name=tariff_data.get("name", tariff_key),
                amount=f"{amount_paid} Stars",
                payment_method="Telegram Stars",
                expire_date=expire_date,
            )
        )
        return
    await chat_panel.render(
        message.chat.id,
        user_id,
        payment_success_message(
            str(tariff_data.get("name") or tariff_key),
            format_remaining_until(expire_date),
        ),
        create_main_menu_keyboard(user_id),
    )
    sync_result = await cascade_router.sync_user_access(user_id, expire_date)
    if sync_result["failed"]:
        db.add_provisioning_task(
            user_id,
            "sync_access",
            {"expire_date": expire_date},
            f"Failed peers: {sync_result['failed']}",
        )
    title = (
        "🔁 Клиент продлил подписку"
        if payment_result["is_extension"]
        else "🆕 Новый клиент оплатил подписку"
    )

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
    chat_panel,
    create_main_menu_keyboard,
):
    """Record a Telegram Stars refund without changing VPN access automatically."""
    refund = message.refunded_payment
    user_id = message.from_user.id if message.from_user else message.chat.id
    await chat_panel.delete_user_message(message)
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
    if await asyncio.to_thread(db.is_client_banned, user_id):
        return
    await chat_panel.render(
        message.chat.id,
        user_id,
        "↩️ Telegram сообщил о возврате Stars.\n\n"
        "VPN-доступ автоматически не изменён; операция передана администратору.",
        create_main_menu_keyboard(user_id),
    )


@router.callback_query(F.data == "admin_payments")
async def open_admin_payments(
    callback_query: types.CallbackQuery,
    db: Database,
    safe_answer_callback,
    is_admin,
):
    if not is_admin(callback_query.from_user.id):
        await safe_answer_callback(callback_query, "❌ Недостаточно прав.")
        return
    await safe_answer_callback(callback_query)
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
            f"\nПоследняя сверка: {latest['status']}, расхождений: {latest['discrepancy_count']}"
        )
    discrepancies = await asyncio.to_thread(db.list_star_discrepancies, 5)
    buttons = []
    if discrepancies:
        lines.append("\n⚠️ Требуют ручной проверки:")
        for item in discrepancies:
            short_review_id = item["review_id"][:8]
            lines.append(
                f"#{short_review_id} | {item['direction']} | "
                f"{item['amount']} Stars | user {item['user_id'] or 'unknown'}"
            )
            buttons.append(
                [
                    types.InlineKeyboardButton(
                        text=f"Подтвердить #{short_review_id}",
                        callback_data=StarApprovalCallback(review_id=item["review_id"]).pack(),
                    )
                ]
            )
    buttons.append(
        [
            types.InlineKeyboardButton(
                text="⬅️ Управление клиентами",
                callback_data="admin_manage_clients",
            )
        ]
    )
    await edit_bound_message(
        callback_query.message,
        "\n".join(lines),
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data == "admin_stars_reconcile")
async def run_admin_stars_reconciliation(
    callback_query: types.CallbackQuery,
    stars_reconciler: StarsReconciler,
    safe_answer_callback,
    is_admin,
):
    if not is_admin(callback_query.from_user.id):
        await safe_answer_callback(callback_query, "❌ Недостаточно прав.")
        return
    await safe_answer_callback(callback_query)
    await edit_bound_message(callback_query.message, "⭐ Выполняю сверку Stars...")
    result = await stars_reconciler.run_once()
    await edit_bound_message(
        callback_query.message,
        stars_reconciler.format_report(result),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text="⬅️ Управление клиентами",
                        callback_data="admin_manage_clients",
                    )
                ]
            ]
        ),
    )


@router.callback_query(StarApprovalCallback.filter())
async def approve_star_discrepancy(
    callback_query: types.CallbackQuery,
    callback_data: StarApprovalCallback,
    db: Database,
    safe_answer_callback,
    is_admin,
):
    """Approve one reviewed historical Stars entry without granting access."""
    if not is_admin(callback_query.from_user.id):
        await safe_answer_callback(callback_query, "❌ Недостаточно прав.")
        return
    await safe_answer_callback(callback_query)
    approved = await asyncio.to_thread(
        db.approve_star_discrepancy,
        callback_data.review_id,
        callback_query.from_user.id,
    )
    if not approved:
        await edit_bound_message(
            callback_query.message, "Эта Stars-транзакция уже обработана или больше не существует."
        )
        return
    await edit_bound_message(
        callback_query.message,
        "✅ Историческая Stars-транзакция подтверждена. VPN-доступ автоматически не изменялся.",
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text="⬅️ К платежам", callback_data="admin_payments")]
            ]
        ),
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
        await edit_bound_message(callback_query.message, "❌ Платеж не найден.")
        return
    claimed = await asyncio.to_thread(db.claim_stars_refund_request, payment["payment_id"])
    if not claimed:
        await edit_bound_message(callback_query.message, "Возврат уже запрошен или обработан.")
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
    await asyncio.to_thread(db.update_refund_request_status, payment["payment_id"], "completed")
    db.log_operation(
        f"telegram:{payment['user_id']}",
        "stars_refund_requested",
        f"payment_id={payment['payment_id']}",
    )
    await edit_bound_message(
        callback_query.message,
        "✅ Возврат отправлен Telegram. VPN-доступ оставлен без изменений.",
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text="⬅️ Управление клиентами",
                        callback_data="admin_manage_clients",
                    )
                ]
            ]
        ),
    )


# Unknown command handler
