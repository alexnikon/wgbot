import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from config import TELEGRAM_BOT_TOKEN, get_admin_telegram_ids, get_tariffs
from logging_setup import configure_logging
from services import (
    cascade_router,
    close_shared_services,
    db,
    is_runtime_ready,
    yookassa_client,
)
from utils import format_date_for_user, generate_peer_name


configure_logging()
logger = logging.getLogger(__name__)
telegram_http_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage webhook process HTTP resources."""
    global telegram_http_client
    logger.info("Webhook server starting")
    telegram_http_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
    yield
    if telegram_http_client and not telegram_http_client.is_closed:
        await telegram_http_client.aclose()
    await close_shared_services()
    logger.info("Webhook server stopping")


app = FastAPI(title="WGBot Webhook Server", lifespan=lifespan)


def get_telegram_http_client() -> httpx.AsyncClient:
    global telegram_http_client
    if telegram_http_client is None or telegram_http_client.is_closed:
        telegram_http_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
    return telegram_http_client


def create_home_reply_markup() -> dict:
    return {"inline_keyboard": [[{"text": "На главную", "callback_data": "main"}]]}


async def send_telegram_message(
    chat_id: int, text: str, reply_markup: dict | None = None
) -> bool:
    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        response = await get_telegram_http_client().post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=10.0,
        )
        if response.is_success:
            return True
        logger.error("Telegram sendMessage failed: %s", response.text)
    except Exception as exc:
        logger.error("Telegram sendMessage error: %s", exc)
    return False


async def send_telegram_document(
    chat_id: int, filename: str, content: bytes | str
) -> bool:
    try:
        response = await get_telegram_http_client().post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
            files={"document": (filename, content, "application/octet-stream")},
            data={"chat_id": str(chat_id)},
            timeout=30.0,
        )
        if response.is_success:
            return True
        logger.error("Telegram sendDocument failed: %s", response.text)
    except Exception as exc:
        logger.error("Telegram sendDocument error: %s", exc)
    return False


async def send_config_with_confirmation(chat_id: int, config: bytes | str) -> bool:
    if not await send_telegram_document(chat_id, "nikonVPN.conf", config):
        return False
    await asyncio.sleep(3)
    await send_telegram_message(
        chat_id,
        "✅ Прислал тебе конфиг файл.\nДобавь его в приложение AmneziWG",
        create_home_reply_markup(),
    )
    return True


async def notify_admins(text: str) -> None:
    for admin_id in get_admin_telegram_ids():
        await send_telegram_message(admin_id, text)


async def delete_payment_message(metadata: dict) -> None:
    try:
        chat_id = int(metadata.get("payment_chat_id"))
        message_id = int(metadata.get("payment_message_id"))
    except (TypeError, ValueError):
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
) -> str:
    return (
        f"{title}\n\n"
        f"👤 Пользователь: @{username if username else 'без username'}\n"
        f"🆔 Telegram ID: {user_id}\n"
        f"📋 Тариф: {tariff_name}\n"
        f"💰 Стоимость: {amount}\n"
        f"💳 Способ оплаты: Банковская карта\n"
        f"📅 Новый срок: {format_date_for_user(expire_date)}"
    )


async def process_successful_payment(payment_data: dict) -> None:
    """Activate or extend Cascade access after a verified YooKassa payment."""
    payment_id = str(payment_data.get("id") or "")
    metadata = yookassa_client.get_payment_metadata(payment_data)
    try:
        user_id = int(metadata.get("user_id") or 0)
    except (TypeError, ValueError):
        user_id = 0
    if not payment_id or not user_id:
        logger.error("YooKassa payment is missing payment_id or user_id")
        return

    tariff_key = str(metadata.get("tariff_key") or "30_days")
    tariff = get_tariffs().get(tariff_key)
    if not tariff:
        logger.error("Unknown tariff %s in payment %s", tariff_key, payment_id)
        return
    username = str(metadata.get("username") or "").strip().lstrip("@") or None
    amount_kopeks = yookassa_client.get_payment_amount(payment_data)
    amount_text = str(payment_data.get("amount", {}).get("value", "0"))

    await delete_payment_message(metadata)
    if not db.claim_payment_success(payment_id):
        logger.info("Ignoring duplicate YooKassa payment event %s", payment_id)
        return
    primary = db.get_primary_client_peer(user_id)

    if primary:
        success, expire_date = db.extend_access(user_id, tariff["days"])
        if not success:
            raise RuntimeError(f"Failed to extend subscription for user {user_id}")
        db.update_payment_status(
            user_id, "paid", amount_kopeks // 100, "yookassa", tariff_key
        )
        result = await cascade_router.sync_user_access(user_id, expire_date)
        if result["failed"]:
            db.add_provisioning_task(
                user_id,
                "sync_access",
                {"expire_date": expire_date},
                f"Failed peers: {result['failed']}",
            )
        await send_telegram_message(
            user_id,
            f"✅ Платеж успешно обработан!\n"
            f"🎉 Продлили тебе доступ на {tariff['days']} дней!\n"
            f"💳 Способ оплаты: Банковская карта\n\n"
            f"Текущая конфигурация остается актуальной.",
            create_home_reply_markup(),
        )
        title = "🔁 Клиент продлил подписку"
    else:
        expire_date = db.activate_new_access(
            user_id, username, tariff["days"], tariff_key, "yookassa"
        )
        db.update_payment_status(
            user_id, "paid", amount_kopeks // 100, "yookassa", tariff_key
        )
        peer_name = generate_peer_name(username, user_id)
        try:
            _, config = await cascade_router.create_user_peer(
                user_id, username, peer_name, expire_date
            )
            if not await send_config_with_confirmation(user_id, config):
                await send_telegram_message(
                    user_id,
                    "✅ Доступ активирован, но конфиг не удалось отправить. Используй /connect.",
                    create_home_reply_markup(),
                )
        except Exception as exc:
            task_id = db.add_provisioning_task(
                user_id,
                "create_peer",
                {
                    "username": username or "",
                    "peer_name": peer_name,
                    "expire_date": expire_date,
                    "tariff_key": tariff_key,
                },
                str(exc),
            )
            logger.error("Queued provisioning task %s after YooKassa payment: %s", task_id, exc)
            await send_telegram_message(
                user_id,
                "⚠️ Платеж получен. Доступ будет создан автоматически после восстановления VPN сервера.",
                create_home_reply_markup(),
            )
            await notify_admins(
                f"⚠️ Оплата получена, provisioning отложен\n\nTelegram ID: {user_id}\nTask: {task_id}"
            )
            return
        title = "🆕 Новый клиент подключился"

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
    metadata = yookassa_client.get_payment_metadata(payment_data)
    try:
        user_id = int(metadata.get("user_id") or 0)
    except (TypeError, ValueError):
        return
    db.release_reservation(user_id)
    await send_telegram_message(
        user_id,
        "❌ Платеж был отменен или не прошел.\n\nПопробуйте оплатить снова или обратитесь в поддержку.",
    )


async def process_waiting_for_capture_payment(payment_data: dict) -> None:
    metadata = yookassa_client.get_payment_metadata(payment_data)
    try:
        user_id = int(metadata.get("user_id") or 0)
    except (TypeError, ValueError):
        return
    await send_telegram_message(
        user_id,
        "⏳ Платеж получен и ожидает подтверждения. Обычно это занимает несколько минут.",
    )


async def process_refund_succeeded(refund_data: dict) -> None:
    payment_id = str(refund_data.get("payment_id") or "")
    payment = db.get_payment_by_id(payment_id)
    if not payment:
        logger.error("Original payment %s was not found for refund", payment_id)
        return
    if payment.get("status") == "refunded":
        logger.info("Ignoring duplicate refund event for payment %s", payment_id)
        return
    tariff = get_tariffs().get(payment.get("tariff_key"), get_tariffs()["30_days"])
    applied = db.apply_refund(payment_id, tariff["days"])
    if not applied:
        raise RuntimeError(f"Failed to reduce access for user {payment['user_id']}")
    user_id, expire_date = applied
    result = await cascade_router.sync_user_access(user_id, expire_date)
    if result["failed"]:
        db.add_provisioning_task(
            user_id,
            "sync_access",
            {"expire_date": expire_date},
            f"Failed peers: {result['failed']}",
        )
    amount = refund_data.get("amount", {}).get("value", "0")
    await send_telegram_message(
        user_id,
        f"💰 Возврат успешно обработан!\n\n"
        f"💳 Сумма возврата: {amount} руб.\n"
        f"📉 Оплаченный период уменьшен на {tariff['days']} дней.\n"
        f"📅 Срок действия доступа обновлен.",
        create_home_reply_markup(),
    )


@app.get("/health")
async def health_check():
    if not is_runtime_ready():
        return JSONResponse({"status": "starting"}, status_code=503)
    return {"status": "healthy"}


@app.get("/webhook/yookassa/health")
async def webhook_health_check():
    return {"status": "webhook_healthy", "endpoint": "/webhook/yookassa"}


@app.post("/webhook/yookassa")
async def yookassa_webhook(request: Request):
    """Verify YooKassa payment state through its API and dispatch the event."""
    try:
        body_text = (await request.body()).decode("utf-8")
        webhook_data = yookassa_client.parse_webhook(body_text)
        if not webhook_data:
            return JSONResponse({"status": "error", "message": "Invalid JSON"}, 200)
        event_type = webhook_data.get("event") or webhook_data.get("event_type") or ""
        event_data = webhook_data.get("object") or webhook_data.get("payment") or webhook_data
        if not isinstance(event_data, dict):
            return JSONResponse({"status": "error", "message": "Invalid object"}, 200)
        if not event_type:
            status = event_data.get("status")
            event_type = f"payment.{status}" if status else ""

        if event_type.startswith("payment.") and event_data.get("id"):
            verified = await yookassa_client.get_payment(event_data["id"])
            if verified:
                event_data = verified
                verified_status = verified.get("status")
                if event_type == "payment.succeeded" and verified_status != "succeeded":
                    logger.warning("Ignoring unverified succeeded payment %s", event_data.get("id"))
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
    except Exception as exc:
        logger.error("Webhook processing error: %s", exc, exc_info=True)
        return JSONResponse({"status": "error", "message": str(exc)}, 200)


if __name__ == "__main__":
    uvicorn.run("webhook_server:app", host="0.0.0.0", port=8001, log_level="info")
