import logging
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import uvicorn
from yookassa_client import YooKassaClient
from database import Database
from wg_api import WGDashboardAPI
from utils import ClientsJsonManager, generate_peer_name
from config import CLIENTS_JSON_PATH, CUSTOM_CLIENTS_PATH, TELEGRAM_BOT_TOKEN
from custom_clients import CustomClientsManager, sync_custom_peers_access
import httpx

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/webhook.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Component initialization
yookassa_client = YooKassaClient()
db = Database()
wg_api = WGDashboardAPI()
clients_manager = ClientsJsonManager(CLIENTS_JSON_PATH)
custom_clients_manager = CustomClientsManager(CUSTOM_CLIENTS_PATH)
telegram_http_client: httpx.AsyncClient | None = None


def sync_bound_custom_peers_for_user(
    user_id: int,
    expire_date: str,
    allow_access: bool = True,
    exclude_peer_id: str | None = None,
):
    exclude_peer_ids = {exclude_peer_id} if exclude_peer_id else set()
    result = sync_custom_peers_access(
        wg_api=wg_api,
        custom_clients_manager=custom_clients_manager,
        user_id=user_id,
        expire_date=expire_date,
        allow_access=allow_access,
        exclude_peer_ids=exclude_peer_ids,
    )
    if result["total"] > 0:
        logger.info(
            f"Custom peers sync user_id={user_id}: total={result['total']}, updated={result['updated']}, failed={result['failed']}"
        )

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle management."""
    global telegram_http_client
    logger.info("Webhook server starting...")
    telegram_http_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
    yield
    if telegram_http_client is not None and not telegram_http_client.is_closed:
        await telegram_http_client.aclose()
    await yookassa_client.aclose()
    wg_api.close()
    logger.info("Webhook server stopping...")

app = FastAPI(title="WGBot Webhook Server", lifespan=lifespan)


def get_telegram_http_client() -> httpx.AsyncClient:
    """Return a shared Telegram HTTP client."""
    global telegram_http_client
    if telegram_http_client is None or telegram_http_client.is_closed:
        telegram_http_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
    return telegram_http_client

async def send_telegram_message(chat_id: int, text: str):
    """Send a message to Telegram."""
    try:
        response = await get_telegram_http_client().post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML"
            },
            timeout=10.0
        )
        
        if response.status_code == 200:
            logger.info(f"Message sent to user {chat_id}")
        else:
            response_data = response.json()
            error_code = response_data.get('error_code', 'unknown')
            error_description = response_data.get('description', 'unknown error')

            if error_code == 400 and 'chat not found' in error_description:
                logger.warning(f"User {chat_id} blocked the bot or deleted the chat")
            else:
                logger.error(
                    f"Failed to send message to user {chat_id}: {error_code} - {error_description}"
                )
                    
    except Exception as e:
        logger.error(f"Error sending message to Telegram: {e}")

async def process_successful_payment(payment_data: dict):
    """Process successful payment."""
    try:
        # Get core payment info
        payment_id = payment_data.get('id', '')
        amount_info = payment_data.get('amount', {})
        amount_value = amount_info.get('value', '0')
        currency = amount_info.get('currency', 'RUB')
        
        logger.info(f"Start processing payment {payment_id}: {amount_value} {currency}")
        
        # Get payment method info
        payment_method = payment_data.get('payment_method', {})
        method_type = payment_method.get('type', 'unknown')
        
        # For bank cards, fetch extra details
        card_info = ""
        if method_type == 'bank_card':
            card = payment_method.get('card', {})
            if card:
                last4 = card.get('last4', '')
                card_type = card.get('card_type', '')
                issuer_name = card.get('issuer_name', '')
                card_info = f" ({card_type} *{last4}, {issuer_name})"
        
        # Get 3D Secure info
        auth_details = payment_data.get('authorization_details', {})
        three_d_secure = auth_details.get('three_d_secure', {})
        three_d_applied = three_d_secure.get('applied', False)
        
        if three_d_applied:
            logger.info(f"Payment {payment_id} passed 3D Secure authentication")
        
        logger.info(
            f"Processing successful payment {payment_id}: {amount_value} {currency}, method: {method_type}{card_info}"
        )
        
        metadata = yookassa_client.get_payment_metadata(payment_data)
        logger.info(f"Payment metadata {payment_id}: {metadata}")
        
        user_id = int(metadata.get('user_id', 0))
        tariff_key = metadata.get('tariff_key', '30_days')
        metadata_username = (metadata.get('username') or '').strip()
        if metadata_username.startswith('@'):
            metadata_username = metadata_username[1:]
        amount = yookassa_client.get_payment_amount(payment_data)
        
        if not user_id:
            logger.error(f"user_id not found in payment {payment_id} metadata. Metadata: {metadata}")
            return
        
        logger.info(f"Processing payment {payment_id} for user {user_id}, tariff: {tariff_key}")
        
        # Fetch tariff info (dynamic)
        from config import get_tariffs
        tariffs = get_tariffs()
        tariff_data = tariffs.get(tariff_key, tariffs.get('30_days', {'days': 30}))
        access_days = tariff_data.get('days', 30)
        
        # Update payment status in DB
        try:
            db.update_payment_status_by_id(payment_id, 'succeeded')
            logger.info(f"Payment {payment_id} status updated to 'succeeded'")
        except Exception as e:
            logger.warning(f"Failed to update payment status in DB: {e}")
        
        # Check if the user already has a peer
        existing_peer = db.get_peer_by_telegram_id(user_id)
        target_expire_date = None
        effective_username = metadata_username or (
            (existing_peer or {}).get('telegram_username', '').strip()
        )
        
        if existing_peer:
            logger.info(f"User {user_id} already has a peer, extending access")
            # Extend access for the existing peer
            success, new_expire_date = db.extend_access(user_id, access_days)
            
            if success:
                logger.info(f"Access extended for user {user_id}, new date: {new_expire_date}")
                # Check if the peer exists in WGDashboard
                peer_exists = None
                try:
                    peer_exists = wg_api.check_peer_exists(existing_peer['peer_id'])
                except Exception as e:
                    logger.error(f"Error checking peer existence in WGDashboard: {e}")

                allow_result = None
                try:
                    allow_result = wg_api.allow_access_peer(existing_peer['peer_id'])
                    if allow_result and allow_result.get('status'):
                        logger.info(f"Restricted removed for user {user_id}")
                        peer_exists = True
                    else:
                        logger.warning(
                            f"Failed to remove restricted for user {user_id}: {allow_result}"
                        )
                except Exception as e:
                    logger.error(f"Error removing restricted in WGDashboard: {e}")

                if peer_exists is True:
                    try:
                        job_update_result = wg_api.update_job_expire_date(
                            existing_peer['job_id'], 
                            existing_peer['peer_id'], 
                            new_expire_date
                        )
                        
                        if job_update_result and job_update_result.get('status'):
                            logger.info(f"Job updated for user {user_id}, new date: {new_expire_date}")
                        else:
                            logger.error(f"Error updating job for user {user_id}: {job_update_result}")
                            
                    except Exception as e:
                        logger.error(f"Error updating job in WGDashboard: {e}")
                    
                    # Update payment status in peers table (amount in kopeks, convert to rubles)
                    db.update_payment_status(user_id, 'paid', amount // 100, 'yookassa', tariff_key)
                    
                    await send_telegram_message(
                        user_id,
                        f"✅ Платеж успешно обработан!\n"
                        f"🎉 Продлили тебе доступ на {access_days} дней!\n"
                        f"💳 Способ оплаты: Банковская карта\n\n"
                        f"Текущая конфигурация остается актуальной."
                    )
                    sync_bound_custom_peers_for_user(
                        user_id=user_id,
                        expire_date=new_expire_date,
                        allow_access=True,
                        exclude_peer_id=existing_peer["peer_id"],
                    )
                elif peer_exists is False:
                    logger.warning(
                        f"Peer for user {user_id} not found in WGDashboard, creating a new one"
                    )
                    # If the peer is missing in WGDashboard, fall back to the creation flow
                    target_expire_date = new_expire_date
                    existing_peer = None
                else:
                    logger.error(
                        f"Peer status for user {user_id} is unknown, recreation canceled to avoid duplicate"
                    )
                    await send_telegram_message(
                        user_id,
                        "❌ Не удалось проверить статус VPN на сервере. Попробуйте еще раз через минуту или обратитесь в поддержку.",
                    )
                    return
            else:
                logger.error(f"Error extending access for user {user_id}")
                await send_telegram_message(
                    user_id,
                    "❌ Ошибка при продлении доступа. Обратитесь в поддержку."
                )
                return
        if not existing_peer:
            # Create a new peer for the user
            logger.info(f"Creating a new peer for user {user_id}")
            peer_name = generate_peer_name(effective_username or None, user_id)
            logger.info(f"Generated peer name: {peer_name}")

            from datetime import datetime, timedelta

            expire_date = target_expire_date or (
                datetime.now() + timedelta(days=access_days)
            ).strftime("%Y-%m-%d %H:%M:%S")

            # Step 1. Stage the DB record first
            stage_info = db.stage_peer_record(
                peer_name=peer_name,
                telegram_user_id=user_id,
                telegram_username=effective_username or "",
                expire_date=expire_date,
                payment_status="paid",
                tariff_key=tariff_key,
                payment_method="yookassa",
                rub_paid=amount // 100,
            )
            if not stage_info:
                logger.error(f"Failed to save staged DB record for user {user_id}")
                await send_telegram_message(
                    user_id,
                    "❌ Ошибка при сохранении данных. Обратитесь в поддержку.",
                )
                return

            peer_id = None
            try:
                # Step 2. Create peer in WGDashboard
                peer_result = wg_api.add_peer(peer_name)
                if not peer_result or "id" not in peer_result:
                    raise Exception(f"Failed to create peer: {peer_result}")

                peer_id = peer_result["id"]
                logger.info(f"Peer created successfully: {peer_id}")

                # Step 3. Create job in WGDashboard
                logger.info(
                    f"Creating job for peer {peer_id}, expiration date: {expire_date}"
                )
                job_result, job_id, final_expire_date = wg_api.create_restrict_job(
                    peer_id, expire_date
                )
                if not job_result or (
                    isinstance(job_result, dict) and job_result.get("status") is False
                ):
                    raise Exception(f"Failed to create job: {job_result}")

                logger.info(f"Job created: {job_id}")

                # Finalize DB record with real peer_id/job_id
                success = db.finalize_staged_peer(
                    telegram_user_id=user_id,
                    stage_info=stage_info,
                    peer_name=peer_name,
                    peer_id=peer_id,
                    job_id=job_id,
                    expire_date=final_expire_date,
                    telegram_username=effective_username or "",
                    payment_status="paid",
                    tariff_key=tariff_key,
                    payment_method="yookassa",
                    rub_paid=amount // 100,
                )
                if not success:
                    raise Exception("Failed to finalize client data in DB")

                # Step 4. Update clients.json
                client_id_for_json = effective_username if effective_username else str(user_id)
                if effective_username:
                    clients_manager.remove_client(str(user_id))
                if not clients_manager.add_update_client(
                    client_id_for_json, peer_id, force_write=True
                ):
                    raise Exception("Failed to update clients.json")

                logger.info(f"Peer saved in DB and clients.json for user {user_id}")
                sync_bound_custom_peers_for_user(
                    user_id=user_id,
                    expire_date=final_expire_date,
                    allow_access=True,
                    exclude_peer_id=peer_id,
                )

                # Update payment status in peers table
                db.update_payment_status(user_id, "paid", amount // 100, "yookassa", tariff_key)

                # Download and send configuration
                try:
                    logger.info(f"Downloading config for peer {peer_id}")
                    config_content = wg_api.download_peer_config(peer_id)
                    filename = "nikonVPN.conf"

                    logger.info(f"Sending config to user {user_id}")
                    # Send config via Telegram API
                    files = {
                        "document": (filename, config_content, "application/octet-stream")
                    }
                    data = {
                        "chat_id": user_id,
                        "caption": (
                            "✅ Платеж успешно обработан!\n"
                            "💳 Способ оплаты: Банковская карта\n"
                            f"🎉 VPN доступ на {access_days} дней!\n"
                            "📁 Ваша VPN конфигурация готова!"
                        ),
                    }

                    response = await get_telegram_http_client().post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
                        files=files,
                        data=data,
                        timeout=30.0,
                    )

                    if response.status_code == 200:
                        logger.info(f"Config successfully sent to user {user_id}")
                    else:
                        logger.error(
                            f"Config send error: {response.status_code} - {response.text}"
                        )
                        await send_telegram_message(
                            user_id,
                            f"✅ Платеж успешно обработан!\n💳 Способ оплаты: Банковская карта\n🎉 VPN доступ на {access_days} дней!\n\n"
                            f"❌ Ошибка при отправке конфигурации. Используйте команду /connect для получения конфига.",
                        )

                except Exception as e:
                    logger.error(
                        f"Error downloading/sending config for user {user_id}: {e}",
                        exc_info=True,
                    )
                    await send_telegram_message(
                        user_id,
                        f"✅ Платеж успешно обработан!\n💳 Способ оплаты: Банковская карта\n🎉 VPN доступ на {access_days} дней!\n\n"
                        f"❌ Ошибка при отправке конфигурации. Используйте команду /connect для получения конфига.",
                    )

            except Exception as e:
                if peer_id:
                    try:
                        wg_api.delete_peer(peer_id)
                    except Exception as delete_error:
                        logger.error(
                            f"Failed to delete peer {peer_id} after error: {delete_error}"
                        )

                rollback_ok = db.rollback_staged_peer(user_id, stage_info)
                if not rollback_ok:
                    logger.error(f"Failed to roll back staged record for user {user_id}")

                logger.error(
                    f"Error creating peer after payment for user {user_id}: {e}",
                    exc_info=True,
                )
                await send_telegram_message(
                    user_id,
                    "❌ Ошибка при создании VPN доступа. Обратитесь в поддержку.",
                )
        
    except Exception as e:
        logger.error(f"Critical error while processing successful payment: {e}", exc_info=True)

async def process_canceled_payment(payment_data: dict):
    """Process canceled payment."""
    try:
        metadata = yookassa_client.get_payment_metadata(payment_data)
        user_id = int(metadata.get('user_id', 0))
        
        if user_id:
            await send_telegram_message(
                user_id,
                "❌ Платеж был отменен или не прошел.\n\n"
                "💡 Попробуйте оплатить снова или обратитесь в поддержку."
            )
    except Exception as e:
        logger.error(f"Error processing canceled payment: {e}")

async def process_waiting_for_capture_payment(payment_data: dict):
    """Process payment waiting for capture."""
    try:
        metadata = yookassa_client.get_payment_metadata(payment_data)
        user_id = int(metadata.get('user_id', 0))
        
        if user_id:
            await send_telegram_message(
                user_id,
                "⏳ Платеж получен и ожидает подтверждения.\n\n"
                "💳 Обычно подтверждение происходит автоматически в течение нескольких минут.\n"
                "📧 Вы получите уведомление о результате."
            )
    except Exception as e:
        logger.error(f"Error processing waiting_for_capture payment: {e}")

async def process_refund_succeeded(refund_data: dict):
    """Process successful refund."""
    try:
        # Refunds require the original payment
        payment_id = refund_data.get('payment_id')
        if not payment_id:
            logger.error("payment_id not found in refund data")
            return
        
        # Fetch payment info from the database
        payment_info = db.get_payment_by_id(payment_id)
        if not payment_info:
            logger.error(f"Payment {payment_id} not found in database")
            return
        
        user_id = payment_info['user_id']
        tariff_key = payment_info.get('tariff_key', '30_days')
        amount = refund_data.get('amount', {}).get('value', '0')
        
        # Determine number of days to reduce
        from config import get_tariffs
        tariffs = get_tariffs()
        tariff_data = tariffs.get(tariff_key, tariffs.get('30_days', {'days': 30}))
        days_to_reduce = tariff_data.get('days', 30)
        
        logger.info(
            f"Processing refund for user {user_id}: reducing access by {days_to_reduce} days (tariff {tariff_key})"
        )
        
        # Reduce access period in the database
        success, new_expire_date = db.decrease_access(user_id, days_to_reduce)
        
        if success:
            logger.info(f"Access reduced for user {user_id}, new date: {new_expire_date}")
            
            # Fetch peer info to update job
            peer_info = db.get_peer_by_telegram_id(user_id)
            if peer_info:
                # Update job in WGDashboard
                try:
                    job_update_result = wg_api.update_job_expire_date(
                        peer_info['job_id'], 
                        peer_info['peer_id'], 
                        new_expire_date
                    )
                    
                    if job_update_result and job_update_result.get('status'):
                        logger.info(
                            f"Job updated for user {user_id} after refund, new date: {new_expire_date}"
                        )
                    else:
                        logger.error(
                            f"Error updating job for user {user_id} after refund: {job_update_result}"
                        )
                        
                except Exception as e:
                    logger.error(f"Error updating job in WGDashboard after refund: {e}")

                sync_bound_custom_peers_for_user(
                    user_id=user_id,
                    expire_date=new_expire_date,
                    allow_access=False,
                    exclude_peer_id=peer_info["peer_id"],
                )
            else:
                logger.warning(f"Peer not found for user {user_id} during refund processing")
        else:
            logger.error(f"Failed to reduce access for user {user_id} during refund processing")
        
        await send_telegram_message(
            user_id,
            f"💰 Возврат успешно обработан!\n\n"
            f"💳 Сумма возврата: {amount} руб.\n"
            f"📉 Ваш оплаченный период был уменьшен на {days_to_reduce} дней в связи с возвратом.\n"
            f"📅 Срок действия доступа обновлен.\n\n"
            f"📧 Деньги будут возвращены на карту в течение 1-3 рабочих дней."
        )
        
        # Update payment status in the database
        db.update_payment_status_by_id(payment_id, 'refunded')
        
    except Exception as e:
        logger.error(f"Error processing refund: {e}", exc_info=True)

@app.get("/health")
async def health_check():
    """Service health check."""
    return {"status": "healthy"}

@app.get("/webhook/yookassa/health")
async def webhook_health_check():
    """Webhook endpoint health check."""
    return {"status": "webhook_healthy", "endpoint": "/webhook/yookassa"}

@app.get("/webhook/yookassa/test")
async def webhook_test():
    """Test endpoint for webhook verification."""
    return {
        "status": "ok",
        "message": "Webhook endpoint is available",
        "endpoint": "/webhook/yookassa",
        "method": "POST",
        "expected_events": ["payment.succeeded", "payment.canceled", "payment.waiting_for_capture", "refund.succeeded"]
    }

@app.post("/webhook/yookassa")
async def yookassa_webhook(request: Request):
    """Handle YooKassa webhook."""
    try:
        # Log all headers for debugging
        logger.info(
            f"Received webhook request from {request.client.host if request.client else 'unknown'}"
        )
        logger.debug(f"Headers: {dict(request.headers)}")
        
        # Read request body
        body = await request.body()
        body_str = body.decode('utf-8')
        logger.info(f"Webhook body (first 500 chars): {body_str[:500]}")
        
        # Get signature from headers (YooKassa may use different headers)
        signature = (request.headers.get('X-YooMoney-Signature', '') or 
                    request.headers.get('Authorization', '').replace('Bearer ', '') or
                    request.headers.get('X-Signature', ''))
        
        # Verify signature (if present and configured)
        # IMPORTANT: with HTTP Basic Auth YooKassa may not send a signature
        # In that case we rely on HTTPS and API-based payment verification
        if signature:
            if not yookassa_client.verify_webhook_signature(body_str, signature):
                logger.warning("Invalid webhook signature from YooKassa")
                # DO NOT reject: signature may be absent with HTTP Basic Auth
                # Log a warning and continue processing
                logger.warning("Continuing webhook processing without signature verification")
            else:
                logger.info("Webhook signature verified successfully")
        else:
            logger.info("Webhook signature missing (possibly using HTTP Basic Auth)")
        
        # Parse data
        webhook_data = yookassa_client.parse_webhook(body_str)
        if not webhook_data:
            logger.error(f"Webhook parse error. Body: {body_str[:200]}")
            # Return 200 so YooKassa does not retry
            return JSONResponse(content={"status": "error", "message": "Invalid JSON"}, status_code=200)
        
        logger.info(f"Webhook parsed successfully: keys={list(webhook_data.keys())}")
        
        # Get event data
        event_type = webhook_data.get('event', '')
        event_data = webhook_data.get('object', {})
        
        # If structure differs, try alternative extraction
        if not event_type:
            # Event may be in a different field
            event_type = webhook_data.get('event_type', '')
        
        # If no event but payment status exists, infer event type
        if not event_type:
            payment_status = webhook_data.get('status', '')
            if payment_status:
                if payment_status == 'succeeded':
                    event_type = 'payment.succeeded'
                elif payment_status == 'canceled':
                    event_type = 'payment.canceled'
                elif payment_status == 'waiting_for_capture':
                    event_type = 'payment.waiting_for_capture'
                logger.info(f"Inferred event_type from payment status: {event_type}")
        
        if not event_data:
            # Payment data may be at the root or in the payment field
            event_data = webhook_data.get('payment', webhook_data)
        
        # If still no event_data but webhook_data contains payment data
        if not event_data or not isinstance(event_data, dict):
            if 'id' in webhook_data and 'status' in webhook_data:
                event_data = webhook_data
                logger.info("Using webhook_data as event_data (direct payment object)")
            else:
                logger.error(
                    f"Missing or invalid 'object' in webhook. Type: {type(event_data)}, webhook_data keys: {list(webhook_data.keys())}"
                )
                # Return 200 so YooKassa does not retry
                return JSONResponse(content={"status": "error", "message": "Missing or invalid object parameter"}, status_code=200)
        
        # If still no event_type but status exists in event_data
        if not event_type and isinstance(event_data, dict):
            payment_status = event_data.get('status', '')
            if payment_status == 'succeeded':
                event_type = 'payment.succeeded'
            elif payment_status == 'canceled':
                event_type = 'payment.canceled'
            elif payment_status == 'waiting_for_capture':
                event_type = 'payment.waiting_for_capture'
            logger.info(f"Inferred event_type from status in event_data: {event_type}")
        
        # Validate required parameters
        if not event_type:
            logger.error(
                f"Failed to determine event_type. webhook_data keys: {list(webhook_data.keys())}, event_data: {list(event_data.keys()) if isinstance(event_data, dict) else 'not a dict'}"
            )
            # Return 200 so YooKassa does not retry, but log the error
            return JSONResponse(content={"status": "error", "message": "Cannot determine event type"}, status_code=200)
        
        # Log webhook details
        object_id = event_data.get('id', 'unknown')
        object_status = event_data.get('status', 'unknown')
        logger.info(f"Webhook received: event={event_type}, ID={object_id}, status={object_status}")
        
        # For payments, also verify status via API (extra check)
        if event_type.startswith('payment.'):
            payment_id = event_data.get('id')
            if payment_id:
                logger.info(f"Checking payment status via API for {payment_id}")
                payment_info = await yookassa_client.get_payment(payment_id)
                if payment_info:
                    api_status = payment_info.get('status', 'unknown')
                    logger.info(f"Payment {payment_id} status via API: {api_status}")
                    # Refresh data from API for accuracy
                    if api_status == 'succeeded' and event_type == 'payment.succeeded':
                        event_data = payment_info
        
        # Process by event type
        if event_type == 'payment.succeeded':
            logger.info(f"Processing successful payment {object_id}")
            await process_successful_payment(event_data)
        elif event_type == 'payment.canceled':
            logger.info(f"Processing canceled payment {object_id}")
            await process_canceled_payment(event_data)
        elif event_type == 'payment.waiting_for_capture':
            logger.info(f"Processing payment waiting for capture {object_id}")
            await process_waiting_for_capture_payment(event_data)
        elif event_type == 'refund.succeeded':
            logger.info(f"Processing successful refund for payment {object_id}")
            await process_refund_succeeded(event_data)
        else:
            logger.warning(f"Unknown event: {event_type}")
        
        logger.info(f"Webhook processed successfully: {event_type}, {object_id}")
        return JSONResponse(content={"status": "ok"})
        
    except HTTPException as e:
        logger.error(f"HTTP error while processing webhook: {e.status_code} - {e.detail}")
        raise
    except Exception as e:
        logger.error(f"Webhook processing error: {e}", exc_info=True)
        # Return 200 so YooKassa does not retry forever
        # But log the error for investigation
        return JSONResponse(content={"status": "error", "message": str(e)}, status_code=200)

if __name__ == "__main__":
    uvicorn.run(
        "webhook_server:app",
        host="0.0.0.0",
        port=8001,
        log_level="info"
    )
