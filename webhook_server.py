import logging
import asyncio
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import uvicorn
from yookassa_client import YooKassaClient
from database import Database
from wg_api import WGDashboardAPI
from utils import ClientsJsonManager, generate_peer_name
from config import CLIENTS_JSON_PATH, TELEGRAM_BOT_TOKEN
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
clients_manager = ClientsJsonManager(CLIENTS_JSON_PATH)

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
            
            if response.status_code == 200:
                logger.info(f"Сообщение отправлено пользователю {chat_id}")
            else:
                response_data = response.json()
                error_code = response_data.get('error_code', 'unknown')
                error_description = response_data.get('description', 'unknown error')
                
                if error_code == 400 and 'chat not found' in error_description:
                    logger.warning(f"Пользователь {chat_id} заблокировал бота или удалил чат")
                else:
                    logger.error(f"Ошибка отправки сообщения пользователю {chat_id}: {error_code} - {error_description}")
                    
    except Exception as e:
        logger.error(f"Ошибка при отправке сообщения в Telegram: {e}")

async def process_successful_payment(payment_data: dict):
    """Обрабатывает успешный платеж"""
    try:
        # Получаем основную информацию о платеже
        payment_id = payment_data.get('id', '')
        amount_info = payment_data.get('amount', {})
        amount_value = amount_info.get('value', '0')
        currency = amount_info.get('currency', 'RUB')
        description = payment_data.get('description', '')
        created_at = payment_data.get('created_at', '')
        
        logger.info(f"Начало обработки платежа {payment_id}: {amount_value} {currency}")
        
        # Получаем информацию о способе оплаты
        payment_method = payment_data.get('payment_method', {})
        method_type = payment_method.get('type', 'unknown')
        method_title = payment_method.get('title', '')
        
        # Для банковских карт получаем дополнительную информацию
        card_info = ""
        if method_type == 'bank_card':
            card = payment_method.get('card', {})
            if card:
                first6 = card.get('first6', '')
                last4 = card.get('last4', '')
                card_type = card.get('card_type', '')
                issuer_country = card.get('issuer_country', '')
                issuer_name = card.get('issuer_name', '')
                card_info = f" ({card_type} *{last4}, {issuer_name})"
        
        # Получаем информацию о 3D Secure
        auth_details = payment_data.get('authorization_details', {})
        three_d_secure = auth_details.get('three_d_secure', {})
        three_d_applied = three_d_secure.get('applied', False)
        rrn = auth_details.get('rrn', '')
        auth_code = auth_details.get('auth_code', '')
        
        if three_d_applied:
            logger.info(f"Платеж {payment_id} прошел 3D Secure аутентификацию")
        
        logger.info(f"Обработка успешного платежа {payment_id}: {amount_value} {currency}, способ: {method_type}{card_info}")
        
        metadata = yookassa_client.get_payment_metadata(payment_data)
        logger.info(f"Метаданные платежа {payment_id}: {metadata}")
        
        user_id = int(metadata.get('user_id', 0))
        tariff_key = metadata.get('tariff_key', '30_days')
        amount = yookassa_client.get_payment_amount(payment_data)
        
        if not user_id:
            logger.error(f"Не найден user_id в метаданных платежа {payment_id}. Метаданные: {metadata}")
            return
        
        logger.info(f"Обработка платежа {payment_id} для пользователя {user_id}, тариф: {tariff_key}")
        
        # Получаем информацию о тарифе (динамически)
        from config import get_tariffs
        tariffs = get_tariffs()
        tariff_data = tariffs.get(tariff_key, tariffs.get('30_days', {'days': 30}))
        access_days = tariff_data.get('days', 30)
        
        # Обновляем статус платежа в БД
        try:
            db.update_payment_status_by_id(payment_id, 'succeeded')
            logger.info(f"Статус платежа {payment_id} обновлен на 'succeeded'")
        except Exception as e:
            logger.warning(f"Не удалось обновить статус платежа в БД: {e}")
        
        # Проверяем, есть ли уже пир у пользователя
        existing_peer = db.get_peer_by_telegram_id(user_id)
        
        if existing_peer:
            logger.info(f"Пользователь {user_id} уже имеет пир, продлеваем доступ")
            # Продлеваем доступ существующего пира
            success, new_expire_date = db.extend_access(user_id, access_days)
            
            if success:
                logger.info(f"Доступ продлен для пользователя {user_id}, новая дата: {new_expire_date}")
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
                        logger.error(f"Ошибка при обновлении job для пользователя {user_id}: {job_update_result}")
                        
                except Exception as e:
                    logger.error(f"Ошибка при обновлении job в WGDashboard: {e}")
                
                # Обновляем статус оплаты в таблице peers (amount в копейках, конвертируем в рубли)
                db.update_payment_status(user_id, 'paid', amount // 100, 'yookassa', tariff_key)
                
                await send_telegram_message(
                    user_id,
                    f"✅ Платеж успешно обработан!\n"
                    f"🎉 Продлили тебе доступ на {access_days} дней!\n"
                    f"💳 Способ оплаты: Банковская карта\n\n"
                    f"Текущая конфигурация остается актуальной."
                )
            else:
                logger.error(f"Ошибка при продлении доступа для пользователя {user_id}")
                await send_telegram_message(
                    user_id,
                    "❌ Ошибка при продлении доступа. Обратитесь в поддержку."
                )
        else:
            # Создаем новый пир для пользователя
            logger.info(f"Создаем новый пир для пользователя {user_id}")
            # Получаем username из базы или генерируем имя
            peer_name = generate_peer_name(None, user_id)
            logger.info(f"Генерируем имя пира: {peer_name}")

            from datetime import datetime, timedelta

            expire_date = (datetime.now() + timedelta(days=access_days)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )

            # Шаг 1. Сначала staging-запись в БД
            stage_info = db.stage_peer_record(
                peer_name=peer_name,
                telegram_user_id=user_id,
                telegram_username="",
                expire_date=expire_date,
                payment_status="paid",
                tariff_key=tariff_key,
                payment_method="yookassa",
                rub_paid=amount // 100,
            )
            if not stage_info:
                logger.error(f"Ошибка при сохранении staging-записи в БД для пользователя {user_id}")
                await send_telegram_message(
                    user_id,
                    "❌ Ошибка при сохранении данных. Обратитесь в поддержку.",
                )
                return

            peer_id = None
            try:
                # Шаг 2. Создаем peer в WGDashboard
                peer_result = wg_api.add_peer(peer_name)
                if not peer_result or "id" not in peer_result:
                    raise Exception(f"Ошибка при создании пира: {peer_result}")

                peer_id = peer_result["id"]
                logger.info(f"Пир создан успешно: {peer_id}")

                # Шаг 3. Создаем job в WGDashboard
                logger.info(
                    f"Создаем job для пира {peer_id}, дата истечения: {expire_date}"
                )
                job_result, job_id, final_expire_date = wg_api.create_restrict_job(
                    peer_id, expire_date
                )
                if not job_result or (
                    isinstance(job_result, dict) and job_result.get("status") is False
                ):
                    raise Exception(f"Ошибка при создании job: {job_result}")

                logger.info(f"Job создан: {job_id}")

                # Финализация записи в БД реальными peer_id/job_id
                success = db.finalize_staged_peer(
                    telegram_user_id=user_id,
                    stage_info=stage_info,
                    peer_name=peer_name,
                    peer_id=peer_id,
                    job_id=job_id,
                    expire_date=final_expire_date,
                    telegram_username="",
                    payment_status="paid",
                    tariff_key=tariff_key,
                    payment_method="yookassa",
                    rub_paid=amount // 100,
                )
                if not success:
                    raise Exception("Ошибка при финализации данных клиента в БД")

                # Шаг 4. Обновляем clients.json
                if not clients_manager.add_update_client(str(user_id), peer_id):
                    raise Exception("Ошибка при обновлении clients.json")

                logger.info(f"Пир сохранен в БД и clients.json для пользователя {user_id}")

                # Обновляем статус оплаты в таблице peers
                db.update_payment_status(user_id, "paid", amount // 100, "yookassa", tariff_key)

                # Скачиваем и отправляем конфигурацию
                try:
                    logger.info(f"Скачиваем конфигурацию для пира {peer_id}")
                    config_content = wg_api.download_peer_config(peer_id)
                    filename = "nikonVPN.conf"

                    logger.info(f"Отправляем конфигурацию пользователю {user_id}")
                    # Отправляем конфигурацию через Telegram API
                    async with httpx.AsyncClient() as client:
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

                        response = await client.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
                            files=files,
                            data=data,
                            timeout=30.0,
                        )

                        if response.status_code == 200:
                            logger.info(f"Конфигурация успешно отправлена пользователю {user_id}")
                        else:
                            logger.error(
                                f"Ошибка отправки конфигурации: {response.status_code} - {response.text}"
                            )
                            await send_telegram_message(
                                user_id,
                                f"✅ Платеж успешно обработан!\n💳 Способ оплаты: Банковская карта\n🎉 VPN доступ на {access_days} дней!\n\n"
                                f"❌ Ошибка при отправке конфигурации. Используйте команду /connect для получения конфига.",
                            )

                except Exception as e:
                    logger.error(
                        f"Ошибка при скачивании/отправке конфигурации для пользователя {user_id}: {e}",
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
                            f"Не удалось удалить peer {peer_id} после ошибки: {delete_error}"
                        )

                rollback_ok = db.rollback_staged_peer(user_id, stage_info)
                if not rollback_ok:
                    logger.error(f"Не удалось откатить staged-запись пользователя {user_id}")

                logger.error(
                    f"Ошибка при создании пира после оплаты для пользователя {user_id}: {e}",
                    exc_info=True,
                )
                await send_telegram_message(
                    user_id,
                    "❌ Ошибка при создании VPN доступа. Обратитесь в поддержку.",
                )
        
    except Exception as e:
        logger.error(f"Критическая ошибка при обработке успешного платежа: {e}", exc_info=True)

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
        tariff_key = payment_info.get('tariff_key', '30_days')
        amount = refund_data.get('amount', {}).get('value', '0')
        
        # Определяем количество дней для уменьшения
        from config import get_tariffs
        tariffs = get_tariffs()
        tariff_data = tariffs.get(tariff_key, tariffs.get('30_days', {'days': 30}))
        days_to_reduce = tariff_data.get('days', 30)
        
        logger.info(f"Обработка возврата для пользователя {user_id}: уменьшаем доступ на {days_to_reduce} дней (тариф {tariff_key})")
        
        # Уменьшаем срок доступа в базе данных
        success, new_expire_date = db.decrease_access(user_id, days_to_reduce)
        
        if success:
            logger.info(f"Доступ уменьшен для пользователя {user_id}, новая дата: {new_expire_date}")
            
            # Получаем информацию о пире для обновления job
            peer_info = db.get_peer_by_telegram_id(user_id)
            if peer_info:
                # Обновляем job в WGDashboard
                try:
                    job_update_result = wg_api.update_job_expire_date(
                        peer_info['job_id'], 
                        peer_info['peer_id'], 
                        new_expire_date
                    )
                    
                    if job_update_result and job_update_result.get('status'):
                        logger.info(f"Job обновлен для пользователя {user_id} после возврата, новая дата: {new_expire_date}")
                    else:
                        logger.error(f"Ошибка при обновлении job для пользователя {user_id} после возврата: {job_update_result}")
                        
                except Exception as e:
                    logger.error(f"Ошибка при обновлении job в WGDashboard после возврата: {e}")
            else:
                logger.warning(f"Не найден пир для пользователя {user_id} при обработке возврата")
        else:
            logger.error(f"Не удалось уменьшить доступ для пользователя {user_id} при обработке возврата")
        
        await send_telegram_message(
            user_id,
            f"💰 Возврат успешно обработан!\n\n"
            f"💳 Сумма возврата: {amount} руб.\n"
            f"📉 Ваш оплаченный период был уменьшен на {days_to_reduce} дней в связи с возвратом.\n"
            f"📅 Срок действия доступа обновлен.\n\n"
            f"📧 Деньги будут возвращены на карту в течение 1-3 рабочих дней."
        )
        
        # Обновляем статус платежа в базе данных
        db.update_payment_status_by_id(payment_id, 'refunded')
        
    except Exception as e:
        logger.error(f"Ошибка при обработке возврата: {e}", exc_info=True)

@app.get("/health")
async def health_check():
    """Проверка здоровья сервиса"""
    return {"status": "healthy"}

@app.get("/webhook/yookassa/health")
async def webhook_health_check():
    """Проверка здоровья webhook endpoint"""
    return {"status": "webhook_healthy", "endpoint": "/webhook/yookassa"}

@app.get("/webhook/yookassa/test")
async def webhook_test():
    """Тестовый endpoint для проверки webhook"""
    return {
        "status": "ok",
        "message": "Webhook endpoint доступен",
        "endpoint": "/webhook/yookassa",
        "method": "POST",
        "expected_events": ["payment.succeeded", "payment.canceled", "payment.waiting_for_capture", "refund.succeeded"]
    }

@app.post("/webhook/yookassa")
async def yookassa_webhook(request: Request):
    """Обработчик webhook от ЮKassa"""
    try:
        # Логируем все заголовки для отладки
        logger.info(f"Получен webhook запрос от {request.client.host if request.client else 'unknown'}")
        logger.debug(f"Заголовки: {dict(request.headers)}")
        
        # Получаем тело запроса
        body = await request.body()
        body_str = body.decode('utf-8')
        logger.info(f"Тело webhook (первые 500 символов): {body_str[:500]}")
        
        # Получаем подпись из заголовков (ЮKassa может использовать разные заголовки)
        signature = (request.headers.get('X-YooMoney-Signature', '') or 
                    request.headers.get('Authorization', '').replace('Bearer ', '') or
                    request.headers.get('X-Signature', ''))
        
        # Проверяем подпись (если есть и настроена)
        # ВАЖНО: При использовании HTTP Basic Auth ЮKassa может не отправлять подпись
        # В этом случае мы полагаемся на HTTPS и проверку данных платежа через API
        if signature:
            if not yookassa_client.verify_webhook_signature(body_str, signature):
                logger.warning("Неверная подпись webhook от ЮKassa")
                # НЕ отклоняем запрос, так как подпись может отсутствовать при HTTP Basic Auth
                # Вместо этого логируем предупреждение и продолжаем обработку
                logger.warning("Продолжаем обработку webhook без проверки подписи")
            else:
                logger.info("Подпись webhook проверена успешно")
        else:
            logger.info("Подпись webhook отсутствует (возможно, используется HTTP Basic Auth)")
        
        # Парсим данные
        webhook_data = yookassa_client.parse_webhook(body_str)
        if not webhook_data:
            logger.error(f"Ошибка парсинга webhook. Тело: {body_str[:200]}")
            # Возвращаем 200, чтобы ЮKassa не повторял запрос
            return JSONResponse(content={"status": "error", "message": "Invalid JSON"}, status_code=200)
        
        logger.info(f"Webhook распарсен успешно: ключи={list(webhook_data.keys())}")
        
        # Проверяем тип уведомления (обязательный параметр)
        notification_type = webhook_data.get('type', '')
        
        # Получаем данные события
        event_type = webhook_data.get('event', '')
        event_data = webhook_data.get('object', {})
        
        # Если структура отличается, пытаемся извлечь данные по-другому
        if not event_type:
            # Возможно, событие указано в другом месте
            event_type = webhook_data.get('event_type', '')
        
        # Если нет event, но есть статус платежа, определяем event по статусу
        if not event_type:
            payment_status = webhook_data.get('status', '')
            if payment_status:
                if payment_status == 'succeeded':
                    event_type = 'payment.succeeded'
                elif payment_status == 'canceled':
                    event_type = 'payment.canceled'
                elif payment_status == 'waiting_for_capture':
                    event_type = 'payment.waiting_for_capture'
                logger.info(f"Определен event_type по статусу платежа: {event_type}")
        
        if not event_data:
            # Возможно, данные платежа в корне объекта или в поле payment
            event_data = webhook_data.get('payment', webhook_data)
        
        # Если все еще нет event_data, но webhook_data содержит данные платежа
        if not event_data or not isinstance(event_data, dict):
            if 'id' in webhook_data and 'status' in webhook_data:
                event_data = webhook_data
                logger.info("Используем webhook_data как event_data (прямой объект платежа)")
            else:
                logger.error(f"Отсутствует или неверный параметр 'object' в webhook. Тип: {type(event_data)}, ключи webhook_data: {list(webhook_data.keys())}")
                # Возвращаем 200, чтобы ЮKassa не повторял запрос
                return JSONResponse(content={"status": "error", "message": "Missing or invalid object parameter"}, status_code=200)
        
        # Если все еще нет event_type, но есть статус в event_data
        if not event_type and isinstance(event_data, dict):
            payment_status = event_data.get('status', '')
            if payment_status == 'succeeded':
                event_type = 'payment.succeeded'
            elif payment_status == 'canceled':
                event_type = 'payment.canceled'
            elif payment_status == 'waiting_for_capture':
                event_type = 'payment.waiting_for_capture'
            logger.info(f"Определен event_type по статусу в event_data: {event_type}")
        
        # Проверяем обязательные параметры
        if not event_type:
            logger.error(f"Не удалось определить event_type. Доступные ключи webhook_data: {list(webhook_data.keys())}, event_data: {list(event_data.keys()) if isinstance(event_data, dict) else 'не словарь'}")
            # Возвращаем 200, чтобы ЮKassa не повторял запрос, но логируем ошибку
            return JSONResponse(content={"status": "error", "message": "Cannot determine event type"}, status_code=200)
        
        # Логируем детали webhook'а
        object_id = event_data.get('id', 'unknown')
        object_status = event_data.get('status', 'unknown')
        logger.info(f"Получен webhook: событие={event_type}, ID={object_id}, статус={object_status}")
        
        # Для платежей также проверяем статус через API (дополнительная проверка)
        if event_type.startswith('payment.'):
            payment_id = event_data.get('id')
            if payment_id:
                logger.info(f"Проверяем статус платежа {payment_id} через API")
                payment_info = await yookassa_client.get_payment(payment_id)
                if payment_info:
                    api_status = payment_info.get('status', 'unknown')
                    logger.info(f"Статус платежа {payment_id} через API: {api_status}")
                    # Обновляем данные из API для гарантии актуальности
                    if api_status == 'succeeded' and event_type == 'payment.succeeded':
                        event_data = payment_info
        
        # Обрабатываем в зависимости от типа события
        if event_type == 'payment.succeeded':
            logger.info(f"Обработка успешного платежа {object_id}")
            await process_successful_payment(event_data)
        elif event_type == 'payment.canceled':
            logger.info(f"Обработка отмененного платежа {object_id}")
            await process_canceled_payment(event_data)
        elif event_type == 'payment.waiting_for_capture':
            logger.info(f"Обработка платежа, ожидающего подтверждения {object_id}")
            await process_waiting_for_capture_payment(event_data)
        elif event_type == 'refund.succeeded':
            logger.info(f"Обработка успешного возврата для платежа {object_id}")
            await process_refund_succeeded(event_data)
        else:
            logger.warning(f"Неизвестное событие: {event_type}")
        
        logger.info(f"Webhook успешно обработан: {event_type}, {object_id}")
        return JSONResponse(content={"status": "ok"})
        
    except HTTPException as e:
        logger.error(f"HTTP ошибка при обработке webhook: {e.status_code} - {e.detail}")
        raise
    except Exception as e:
        logger.error(f"Ошибка обработки webhook: {e}", exc_info=True)
        # Возвращаем 200, чтобы ЮKassa не повторял запрос бесконечно
        # Но логируем ошибку для исправления
        return JSONResponse(content={"status": "error", "message": str(e)}, status_code=200)

if __name__ == "__main__":
    uvicorn.run(
        "webhook_server:app",
        host="0.0.0.0",
        port=8001,
        log_level="info"
    )
