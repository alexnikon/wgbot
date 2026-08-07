import asyncio
import hmac
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from callbacks import ClientConfigCallback
from cascade_api import CascadeRouter
from config import (
    INTERNAL_METRICS_TOKEN,
    TELEGRAM_BOT_TOKEN,
    WEBHOOK_MAX_BODY_BYTES,
    get_admin_telegram_ids,
    get_tariffs,
)
from database import Database
from logging_setup import configure_logging
from message_templates import (
    format_remaining_time,
    format_remaining_until,
    initial_config_caption,
    payment_success_message,
    yookassa_refund_success_message,
)
from services import AppServices
from telegram_text import (
    TelegramText,
    TelegramTextLike,
    ensure_telegram_text,
    rich_date,
)
from utils import format_date_for_user
from yookassa_client import (
    YooKassaClient,
    YooKassaError,
    YooKassaNotFound,
    YooKassaUnavailable,
)

configure_logging()
logger = logging.getLogger(__name__)
telegram_http_client: httpx.AsyncClient | None = None
db: Database
cascade_router: CascadeRouter
yookassa_client: YooKassaClient
app_services: AppServices | None = None


def configure_runtime(services: AppServices) -> None:
    """Inject explicitly created runtime services before serving requests."""
    global app_services, cascade_router, db, yookassa_client
    app_services = services
    db = services.db
    cascade_router = services.cascade_router
    yookassa_client = services.yookassa_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage webhook process HTTP resources."""
    global telegram_http_client
    logger.info("Webhook server starting")
    telegram_http_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
    yield
    if telegram_http_client and not telegram_http_client.is_closed:
        await telegram_http_client.aclose()
    logger.info("Webhook server stopping")


app = FastAPI(title="WGBot Webhook Server", lifespan=lifespan)


def get_telegram_http_client() -> httpx.AsyncClient:
    global telegram_http_client
    if telegram_http_client is None or telegram_http_client.is_closed:
        telegram_http_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
    return telegram_http_client


def create_home_reply_markup() -> dict:
    return {"inline_keyboard": [[{"text": "На главную", "callback_data": "main"}]]}


def create_access_reply_markup(user_id: int) -> dict:
    if db.count_managed_configs(user_id) == 0:
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "📥 Создать файл конфигурации",
                        "callback_data": ClientConfigCallback(action="create").pack(),
                    }
                ]
            ]
        }
    return {
        "inline_keyboard": [
            [{"text": "📥 Файлы конфигурации", "callback_data": "get_config"}]
        ]
    }


async def send_telegram_message(
    chat_id: int, text: TelegramTextLike, reply_markup: dict | None = None
) -> bool:
    ban_checker = getattr(db, "is_client_banned", None)
    ban_value = await asyncio.to_thread(ban_checker, chat_id) if callable(ban_checker) else False
    if ban_value is True or (isinstance(ban_value, int) and ban_value == 1):
        logger.info("Skipping webhook Telegram message for banned user %s", chat_id)
        return False
    rendered = ensure_telegram_text(text)
    candidates = (
        (
            "sendRichMessage",
            {"chat_id": chat_id, "rich_message": {"html": rendered.html}},
        ),
        (
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": rendered.regular_html,
                "parse_mode": "HTML",
            },
        ),
        ("sendMessage", {"chat_id": chat_id, "text": rendered.plain}),
    )
    for method, candidate_payload in candidates:
        if reply_markup is not None:
            candidate_payload["reply_markup"] = reply_markup
        for attempt in range(3):
            try:
                response = await get_telegram_http_client().post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
                    json=candidate_payload,
                    timeout=10.0,
                )
                if response.is_success:
                    await asyncio.to_thread(db.mark_telegram_reachable, chat_id)
                    return True
                if response.status_code == 403:
                    await asyncio.to_thread(
                        db.mark_telegram_unreachable,
                        chat_id,
                        "TelegramForbiddenError",
                    )
                    return False
                if response.status_code == 429 and attempt < 2:
                    retry_after = response.json().get("parameters", {}).get("retry_after", 1)
                    await asyncio.sleep(float(retry_after))
                    continue
                if response.status_code == 400:
                    break
                logger.error(
                    "Telegram %s failed: status=%s",
                    method,
                    response.status_code,
                )
                return False
            except Exception as exc:
                logger.error("Telegram %s error: %s", method, type(exc).__name__)
                return False
    return False


async def send_telegram_document(chat_id: int, filename: str, content: bytes | str) -> bool:
    try:
        response = await get_telegram_http_client().post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
            files={"document": (filename, content, "application/octet-stream")},
            data={"chat_id": str(chat_id)},
            timeout=30.0,
        )
        if response.is_success:
            await asyncio.to_thread(db.mark_telegram_reachable, chat_id)
            return True
        if response.status_code == 403:
            await asyncio.to_thread(db.mark_telegram_unreachable, chat_id, "TelegramForbiddenError")
            return False
        logger.error("Telegram sendDocument failed: status=%s", response.status_code)
    except Exception as exc:
        logger.error("Telegram sendDocument error: %s", type(exc).__name__)
    return False


async def send_config_with_confirmation(
    chat_id: int, config: bytes | str, filename: str = "nikonVPN.conf"
) -> bool:
    caption = initial_config_caption()
    try:
        for caption_text, parse_mode in (
            (caption.regular_html, "HTML"),
            (caption.plain, None),
        ):
            data = {
                "chat_id": str(chat_id),
                "caption": caption_text,
                "reply_markup": json.dumps(create_home_reply_markup()),
            }
            if parse_mode:
                data["parse_mode"] = parse_mode
            response = await get_telegram_http_client().post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
                files={"document": (filename, config, "application/octet-stream")},
                data=data,
                timeout=30.0,
            )
            if response.is_success:
                await asyncio.to_thread(db.mark_telegram_reachable, chat_id)
                return True
            if response.status_code == 403:
                await asyncio.to_thread(
                    db.mark_telegram_unreachable, chat_id, "TelegramForbiddenError"
                )
                return False
            if response.status_code != 400:
                break
        logger.error("Telegram sendDocument failed: status=%s", response.status_code)
    except Exception as exc:
        logger.error("Telegram sendDocument error: %s", type(exc).__name__)
    return False


async def notify_admins(text: TelegramTextLike) -> None:
    for admin_id in get_admin_telegram_ids():
        await send_telegram_message(admin_id, text)


async def delete_payment_message(metadata: dict) -> None:
    try:
        chat_id = int(metadata.get("payment_chat_id"))
        message_id = int(metadata.get("payment_message_id"))
    except TypeError, ValueError:
        return
    try:
        await get_telegram_http_client().post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage",
            json={"chat_id": chat_id, "message_id": message_id},
            timeout=10.0,
        )
    except Exception as exc:
        logger.warning("Failed to delete payment message: %s", exc)


def admin_payment_text(
    title: str,
    user_id: int,
    username: str | None,
    tariff_name: str,
    amount: str,
    expire_date: str,
) -> TelegramText:
    formatted_date = format_date_for_user(expire_date)
    plain = (
        f"{title}\n\n"
        f"👤 Пользователь: @{username if username else 'без username'}\n"
        f"🆔 Telegram ID: {user_id}\n"
        f"📋 Тариф: {tariff_name}\n"
        f"💰 Стоимость: {amount}\n"
        f"💳 Способ оплаты: Банковская карта\n"
        f"📅 Новый срок: {formatted_date}"
    )
    return TelegramText.from_plain_with_replacements(
        plain,
        {formatted_date: rich_date(expire_date, formatted_date)},
    )


async def process_successful_payment(payment_data: dict) -> None:
    """Activate or extend Cascade access after a verified YooKassa payment."""
    payment_id = str(payment_data.get("id") or "")
    local_payment = await asyncio.to_thread(db.get_payment_by_id, payment_id)
    if not local_payment:
        raise ValueError("Verified payment does not exist locally")
    metadata = json.loads(local_payment.get("metadata") or "{}")
    user_id = int(local_payment["user_id"])
    tariff_key = str(local_payment.get("tariff_key") or "")
    tariff = get_tariffs().get(tariff_key)
    if not tariff:
        logger.error("Unknown tariff %s in payment %s", tariff_key, payment_id)
        return
    username = str(metadata.get("username") or "").strip().lstrip("@") or None
    amount_kopeks = yookassa_client.get_payment_amount(payment_data)
    amount_text = str(payment_data.get("amount", {}).get("value", "0"))

    await delete_payment_message(metadata)
    payment_result = await asyncio.to_thread(
        db.apply_verified_payment,
        payment_id,
        user_id,
        username,
        amount_kopeks,
        "yookassa",
        tariff_key,
        int(tariff["days"]),
    )
    if not payment_result:
        logger.info("Ignoring duplicate YooKassa payment event %s", payment_id)
        return
    expire_date = payment_result["expire_date"]
    ban_checker = getattr(db, "is_client_banned", None)
    ban_value = await asyncio.to_thread(ban_checker, user_id) if callable(ban_checker) else False
    if ban_value is True or (isinstance(ban_value, int) and ban_value == 1):
        result = await cascade_router.sync_client_state(user_id)
        if result["failed"]:
            db.add_provisioning_task(
                user_id,
                "sync_client_state",
                {},
                f"Failed peers: {result['failed']}",
            )
        await notify_admins(
            admin_payment_text(
                "🚫 Забаненный клиент оплатил подписку",
                user_id,
                username,
                tariff.get("name", tariff_key),
                f"{amount_text} руб.",
                expire_date,
            )
        )
        return
    await send_telegram_message(
        user_id,
        payment_success_message(
            str(tariff.get("name") or tariff_key),
            format_remaining_until(expire_date),
        ),
        create_access_reply_markup(user_id),
    )
    result = await cascade_router.sync_user_access(user_id, expire_date)
    if result["failed"]:
        db.add_provisioning_task(
            user_id,
            "sync_access",
            {"expire_date": expire_date},
            f"Failed peers: {result['failed']}",
        )
    title = (
        "🔁 Клиент продлил подписку"
        if payment_result["is_extension"]
        else "🆕 Новый клиент оплатил подписку"
    )

    await notify_admins(
        admin_payment_text(
            title,
            user_id,
            username,
            tariff.get("name", tariff_key),
            f"{amount_text} руб.",
            expire_date,
        )
    )


async def process_canceled_payment(payment_data: dict) -> None:
    payment_id = str(payment_data.get("id") or "")
    local_payment = await asyncio.to_thread(db.get_payment_by_id, payment_id)
    if not local_payment:
        return
    user_id = int(local_payment["user_id"])
    canceled = await asyncio.to_thread(db.cancel_pending_payment, payment_id)
    if not canceled:
        logger.info("Ignoring late or duplicate cancellation for payment %s", payment_id)
        return
    await send_telegram_message(
        user_id,
        "❌ Платеж был отменен или не прошел.\n\nПопробуйте оплатить снова или обратитесь в поддержку.",
    )


async def process_waiting_for_capture_payment(payment_data: dict) -> None:
    local_payment = await asyncio.to_thread(db.get_payment_by_id, str(payment_data.get("id") or ""))
    if not local_payment:
        return
    user_id = int(local_payment["user_id"])
    await send_telegram_message(
        user_id,
        "⏳ Платеж получен и ожидает подтверждения. Обычно это занимает несколько минут.",
    )


async def process_refund_succeeded(refund_data: dict) -> None:
    payment_id = str(refund_data.get("payment_id") or "")
    payment = await asyncio.to_thread(db.get_payment_by_id, payment_id)
    if not payment:
        logger.error("Original payment %s was not found for refund", payment_id)
        return
    if payment.get("status") == "refunded":
        logger.info("Ignoring duplicate refund event for payment %s", payment_id)
        return
    refund_amount = yookassa_client.get_payment_amount(refund_data)
    if refund_amount != int(payment["amount"]):
        logger.warning("Partial refund for payment %s requires manual adjustment", payment_id)
        await notify_admins(
            "⚠️ Частичный возврат требует ручной корректировки\n\n"
            f"Payment ID: {payment_id}\n"
            f"Telegram ID: {payment['user_id']}\n"
            f"Сумма возврата: {refund_data.get('amount', {}).get('value', '0')} руб."
        )
        return
    tariff = get_tariffs().get(payment.get("tariff_key"))
    if not tariff:
        raise ValueError("Refund references an unknown tariff")
    applied = await asyncio.to_thread(db.apply_refund, payment_id, tariff["days"])
    if not applied:
        raise RuntimeError(f"Failed to reduce access for user {payment['user_id']}")
    user_id, expire_date = applied.user_id, applied.expire_date
    result = await cascade_router.sync_user_access(user_id, expire_date)
    if result["failed"]:
        db.add_provisioning_task(
            user_id,
            "sync_access",
            {"expire_date": expire_date},
            f"Failed peers: {result['failed']}",
        )
    amount = refund_data.get("amount", {}).get("value", "0")
    expire_at = datetime.fromisoformat(expire_date)
    if expire_at.tzinfo is None:
        expire_at = expire_at.replace(tzinfo=UTC)
    time_left = expire_at - datetime.now(UTC)
    remaining = format_remaining_time(time_left) if time_left.total_seconds() > 0 else None
    await send_telegram_message(
        user_id,
        yookassa_refund_success_message(
            amount,
            str(tariff.get("name") or payment.get("tariff_key") or ""),
            remaining,
        ),
        create_home_reply_markup(),
    )


@app.get("/health")
async def health_check():
    if app_services is None or not app_services.runtime_ready:
        return JSONResponse({"status": "starting"}, status_code=503)
    return {"status": "healthy"}


@app.get("/internal/metrics")
async def runtime_metrics(request: Request):
    """Return protected in-process metrics and queue gauges."""
    if not INTERNAL_METRICS_TOKEN:
        return JSONResponse({"status": "not_found"}, status_code=404)
    authorization = request.headers.get("authorization", "")
    expected = f"Bearer {INTERNAL_METRICS_TOKEN}"
    if not hmac.compare_digest(authorization, expected):
        return JSONResponse({"status": "unauthorized"}, status_code=401)
    if app_services is None:
        return JSONResponse({"status": "starting"}, status_code=503)
    snapshot = app_services.metrics.snapshot()
    snapshot["database"] = await asyncio.to_thread(db.get_runtime_stats)
    snapshot["database"]["legacy_callback_zero_streak_days"] = await asyncio.to_thread(
        db.get_legacy_callback_zero_streak
    )
    snapshot["ready"] = app_services.runtime_ready
    return snapshot


@app.get("/webhook/yookassa/health")
async def webhook_health_check():
    return {"status": "webhook_healthy", "endpoint": "/webhook/yookassa"}


@app.post("/webhook/yookassa")
async def yookassa_webhook(request: Request):
    """Verify YooKassa payment state through its API and dispatch the event."""
    try:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > WEBHOOK_MAX_BODY_BYTES:
                return JSONResponse({"status": "error", "message": "Payload too large"}, 413)
        body_text = bytes(body).decode("utf-8")
        webhook_data = yookassa_client.parse_webhook(body_text)
        if not webhook_data:
            return JSONResponse({"status": "error", "message": "Invalid JSON"}, 400)
        event_type = webhook_data.get("event") or webhook_data.get("event_type") or ""
        event_data = webhook_data.get("object") or webhook_data.get("payment") or webhook_data
        if not isinstance(event_data, dict):
            return JSONResponse({"status": "error", "message": "Invalid object"}, 400)
        if not event_type:
            status = event_data.get("status")
            event_type = f"payment.{status}" if status else ""

        object_id = str(event_data.get("id") or "")
        if not object_id:
            return JSONResponse({"status": "error", "message": "Missing object ID"}, 400)

        if event_type.startswith("payment."):
            event_data = await yookassa_client.get_payment(object_id)
            expected_status = event_type.removeprefix("payment.")
            if event_data.get("status") != expected_status:
                logger.warning(
                    "Ignoring payment event with status mismatch: event=%s actual=%s",
                    expected_status,
                    event_data.get("status"),
                )
                return JSONResponse({"status": "ignored"})
            local_payment = await asyncio.to_thread(db.get_payment_by_id, object_id)
            if not local_payment:
                logger.warning("Ignoring unknown YooKassa payment %s", object_id)
                return JSONResponse({"status": "ignored"})
            metadata = yookassa_client.get_payment_metadata(event_data)
            matches_local = (
                yookassa_client.get_payment_amount(event_data) == int(local_payment["amount"])
                and event_data.get("amount", {}).get("currency")
                == local_payment.get("currency", "RUB")
                and str(metadata.get("user_id") or "") == str(local_payment["user_id"])
                and str(metadata.get("tariff_key") or "") == str(local_payment["tariff_key"])
            )
            if not matches_local:
                logger.error("Rejected mismatched YooKassa payment %s", object_id)
                return JSONResponse({"status": "ignored"})
        elif event_type == "refund.succeeded":
            event_data = await yookassa_client.get_refund(object_id)
            if event_data.get("status") != "succeeded":
                return JSONResponse({"status": "ignored"})

        if event_type == "payment.succeeded":
            await process_successful_payment(event_data)
        elif event_type == "payment.canceled":
            await process_canceled_payment(event_data)
        elif event_type == "payment.waiting_for_capture":
            await process_waiting_for_capture_payment(event_data)
        elif event_type == "refund.succeeded":
            await process_refund_succeeded(event_data)
        else:
            logger.warning("Unknown YooKassa event: %s", event_type)
        return JSONResponse({"status": "ok"})
    except YooKassaUnavailable as exc:
        logger.warning("YooKassa verification is temporarily unavailable: %s", exc)
        return JSONResponse({"status": "retry"}, 503)
    except YooKassaNotFound:
        return JSONResponse({"status": "ignored"})
    except (UnicodeDecodeError, YooKassaError, ValueError) as exc:
        logger.warning("Rejected YooKassa webhook: %s", exc)
        return JSONResponse({"status": "error", "message": "Invalid event"}, 400)
    except Exception as exc:
        logger.error("Webhook processing error: %s", exc, exc_info=True)
        return JSONResponse({"status": "error", "message": "Internal error"}, 500)


if __name__ == "__main__":
    uvicorn.run("webhook_server:app", host="0.0.0.0", port=8001, log_level="info")
