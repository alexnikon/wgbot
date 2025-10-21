import logging
import asyncio
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import uvicorn
from yookassa_client import YooKassaClient
from database import Database
from wg_api import WGDashboardAPI
from utils import generate_peer_name
from config import TELEGRAM_BOT_TOKEN
import httpx

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/webhook.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Инициализация компонентов
yookassa_client = YooKassaClient()
db = Database()
wg_api = WGDashboardAPI()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Управление жизненным циклом приложения"""
    logger.info("Webhook сервер запускается...")
    yield
    logger.info("Webhook сервер останавливается...")

app = FastAPI(title="WGBot Webhook Server", lifespan=lifespan)

async def send_telegram_message(chat_id: int, text: str):
    """Отправляет сообщение в Telegram"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML"
                },
                timeout=10.0
            )
            if response.status_code != 200:
                logger.error(f"Ошибка отправки сообщения: {response.text}")
    except Exception as e:
        logger.error(f"Ошибка при отправке сообщения в Telegram: {e}")

async def process_successful_payment(payment_data: dict):
    """Обрабатывает успешный платеж"""
    try:
        metadata = yookassa_client.get_payment_metadata(payment_data)
        user_id = int(metadata.get('user_id', 0))
        tariff_key = metadata.get('tariff_key', '30_days')
        amount = yookassa_client.get_payment_amount(payment_data)
        
        if not user_id:
            logger.error("Не найден user_id в метаданных платежа")
            return
        
        # Получаем информацию о тарифе
        from config import TARIFFS
        tariff_data = TARIFFS.get(tariff_key, TARIFFS['30_days'])
        access_days = tariff_data.get('days', 30)
        
        # Проверяем, есть ли уже пир у пользователя
        existing_peer = db.get_peer_by_telegram_id(user_id)
        
        if existing_peer:
            # Продлеваем доступ существующего пира
            success, new_expire_date = db.extend_access(user_id, access_days)
            
            if success:
                # Обновляем job в WGDashboard
                try:
                    job_update_result = wg_api.update_job_expire_date(
                        existing_peer['job_id'], 
                        existing_peer['peer_id'], 
                        new_expire_date
                    )
                    
                    if job_update_result and job_update_result.get('status'):
                        logger.info(f"Job обновлен для пользователя {user_id}, новая дата: {new_expire_date}")
                    else:
                        logger.error(f"Ошибка при обновлении job для пользователя {user_id}")
                        
                except Exception as e:
                    logger.error(f"Ошибка при обновлении job в WGDashboard: {e}")
                
                # Обновляем статус оплаты
                db.update_payment_status(user_id, 'paid', amount, 'yookassa', tariff_key)
                
                await send_telegram_message(
                    user_id,
                    f"✅ Платеж успешно обработан!\n"
                    f"🎉 Продлили тебе доступ на {access_days} дней!\n"
                    f"💳 Способ оплаты: Банковская карта\n\n"
                    f"Текущая конфигурация остается актуальной."
                )
            else:
                await send_telegram_message(
                    user_id,
                    "❌ Ошибка при продлении доступа. Обратитесь в поддержку."
                )
        else:
            # Создаем новый пир для пользователя
            try:
                # Получаем username из базы или генерируем имя
                peer_name = generate_peer_name(None, user_id)
                
                # Создаем пира
                peer_result = wg_api.add_peer(peer_name)
                
                if not peer_result or 'id' not in peer_result:
                    await send_telegram_message(
                        user_id,
                        "❌ Ошибка при создании пира. Обратитесь в поддержку."
                    )
                    return
                
                peer_id = peer_result['id']
                
                # Создаем job для ограничения через определенное количество дней
                from datetime import datetime, timedelta
                expire_date = (datetime.now() + timedelta(days=access_days)).strftime('%Y-%m-%d %H:%M:%S')
                job_result, job_id, expire_date = wg_api.create_restrict_job(peer_id, expire_date)
                
                # Сохраняем в базу данных с оплаченным статусом
                success = db.add_peer(
                    peer_name=peer_name,
                    peer_id=peer_id,
                    job_id=job_id,
                    telegram_user_id=user_id,
                    telegram_username=None,
                    expire_date=expire_date,
                    payment_status='paid',
                    stars_paid=0,
                    tariff_key=tariff_key,
                    payment_method='yookassa',
                    rub_paid=amount
                )
                
                if not success:
                    await send_telegram_message(
                        user_id,
                        "❌ Ошибка при сохранении данных. Обратитесь в поддержку."
                    )
                    return
                
                # Скачиваем и отправляем конфигурацию
                config_content = wg_api.download_peer_config(peer_id)
                filename = "nikonVPN.conf"
                
                # Отправляем конфигурацию через Telegram API
                try:
                    async with httpx.AsyncClient() as client:
                        files = {
                            'document': (filename, config_content, 'application/octet-stream')
                        }
                        data = {
                            'chat_id': user_id,
                            'caption': f"✅ Платеж успешно обработан!\n💳 Способ оплаты: Банковская карта\n🎉 VPN доступ на {access_days} дней!\n📁 Ваша VPN конфигурация готова!"
                        }
                        
                        response = await client.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
                            files=files,
                            data=data,
                            timeout=30.0
                        )
                        
                        if response.status_code != 200:
                            logger.error(f"Ошибка отправки конфигурации: {response.text}")
                            
                except Exception as e:
                    logger.error(f"Ошибка при отправке конфигурации: {e}")
                    await send_telegram_message(
                        user_id,
                        "✅ Платеж обработан, но ошибка при отправке конфигурации. Обратитесь в поддержку."
                    )
                
            except Exception as e:
                logger.error(f"Ошибка при создании пира после оплаты: {e}")
                await send_telegram_message(
                    user_id,
                    "❌ Ошибка при создании VPN доступа. Обратитесь в поддержку."
                )
        
    except Exception as e:
        logger.error(f"Ошибка при обработке успешного платежа: {e}")

async def process_canceled_payment(payment_data: dict):
    """Обрабатывает отмененный платеж"""
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
        logger.error(f"Ошибка при обработке отмененного платежа: {e}")

async def process_waiting_for_capture_payment(payment_data: dict):
    """Обрабатывает платеж, ожидающий подтверждения"""
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
        logger.error(f"Ошибка при обработке платежа waiting_for_capture: {e}")

async def process_refund_succeeded(refund_data: dict):
    """Обрабатывает успешный возврат"""
    try:
        # Для возвратов нужно найти оригинальный платеж
        payment_id = refund_data.get('payment_id')
        if not payment_id:
            logger.error("Не найден payment_id в данных возврата")
            return
        
        # Получаем информацию о платеже из базы данных
        payment_info = db.get_payment_by_id(payment_id)
        if not payment_info:
            logger.error(f"Не найден платеж {payment_id} в базе данных")
            return
        
        user_id = payment_info['user_id']
        amount = refund_data.get('amount', {}).get('value', '0')
        
        await send_telegram_message(
            user_id,
            f"💰 Возврат успешно обработан!\n\n"
            f"💳 Сумма возврата: {amount} руб.\n"
            f"📧 Деньги будут возвращены на карту в течение 1-3 рабочих дней.\n\n"
            f"❓ Если у вас есть вопросы, обратитесь в поддержку."
        )
        
        # Обновляем статус платежа в базе данных
        db.update_payment_status(payment_id, 'refunded')
        
    except Exception as e:
        logger.error(f"Ошибка при обработке возврата: {e}")

@app.get("/health")
async def health_check():
    """Проверка здоровья сервиса"""
    return {"status": "healthy"}

@app.post("/webhook/yookassa")
async def yookassa_webhook(request: Request):
    """Обработчик webhook от ЮKassa"""
    try:
        # Получаем тело запроса
        body = await request.body()
        body_str = body.decode('utf-8')
        
        # Получаем подпись из заголовков
        signature = request.headers.get('X-YooMoney-Signature', '')
        
        # Проверяем подпись
        if not yookassa_client.verify_webhook_signature(body_str, signature):
            logger.warning("Неверная подпись webhook от ЮKassa")
            raise HTTPException(status_code=400, detail="Invalid signature")
        
        # Парсим данные
        webhook_data = yookassa_client.parse_webhook(body_str)
        if not webhook_data:
            logger.error("Ошибка парсинга webhook")
            raise HTTPException(status_code=400, detail="Invalid JSON")
        
        # Получаем данные события
        event_type = webhook_data.get('event', '')
        event_data = webhook_data.get('object', {})
        
        logger.info(f"Получен webhook: событие {event_type}, ID {event_data.get('id')}")
        
        # Обрабатываем в зависимости от типа события
        if event_type == 'payment.succeeded':
            await process_successful_payment(event_data)
        elif event_type == 'payment.canceled':
            await process_canceled_payment(event_data)
        elif event_type == 'payment.waiting_for_capture':
            await process_waiting_for_capture_payment(event_data)
        elif event_type == 'refund.succeeded':
            await process_refund_succeeded(event_data)
        else:
            logger.info(f"Неизвестное событие: {event_type}")
        
        return JSONResponse(content={"status": "ok"})
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ошибка обработки webhook: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

if __name__ == "__main__":
    uvicorn.run(
        "webhook_server:app",
        host="0.0.0.0",
        port=8001,
        log_level="info"
    )
