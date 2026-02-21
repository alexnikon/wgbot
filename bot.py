import asyncio
import logging

from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import SUPPORT_URL, TELEGRAM_BOT_TOKEN, CLIENTS_JSON_PATH
from database import Database
from payment import PaymentManager
from utils import (
    format_peer_info,
    format_peer_list,
    generate_peer_name,
    sanitize_filename,
    validate_peer_name,
    parse_date_flexible,
    format_date_for_user,
    ClientsJsonManager,
)
from wg_api import WGDashboardAPI

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("logs/wgbot.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# Инициализация бота и диспетчера
bot = Bot(token=TELEGRAM_BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Инициализация компонентов
wg_api = WGDashboardAPI()
db = Database()
payment_manager = PaymentManager(bot)
clients_manager = ClientsJsonManager(CLIENTS_JSON_PATH)


# Хелпер: создать или восстановить пира и вернуть конфиг
async def create_or_restore_peer_for_user(
    user_id: int, username: str | None, tariff_key: str | None = None
) -> tuple[bool, str, bytes | None]:
    """Создаёт нового пира либо восстанавливает, если он отсутствует на сервере. Возвращает (success, error_message, config_content)."""
    try:
        existing_peer = db.get_peer_by_telegram_id(user_id)

        # Определяем срок действия
        if existing_peer and existing_peer.get("expire_date"):
            # Восстановление по существующей дате
            target_expire_date = existing_peer["expire_date"]
        else:
            # Новый пользователь или нет даты — возьмём из тарифа
            access_days = 30
            if tariff_key:
                tariff_data = payment_manager.tariffs.get(tariff_key, {})
                access_days = tariff_data.get("days", 30)
            from datetime import datetime, timedelta

            target_expire_date = (
                datetime.now() + timedelta(days=access_days)
            ).strftime("%Y-%m-%d %H:%M:%S")

        # Имя пира
        # Всегда стараемся использовать формат username_id, если есть username
        # Это исправляет ситуацию, когда старый пир был user_id, а теперь у пользователя есть username
        peer_name = generate_peer_name(username, user_id)

        # Шаг 1. Сначала staging-запись в БД
        stage_info = db.stage_peer_record(
            peer_name=peer_name,
            telegram_user_id=user_id,
            telegram_username=username or "",
            expire_date=target_expire_date,
            payment_status="paid",
            tariff_key=tariff_key,
        )
        if not stage_info:
            return False, "Ошибка при сохранении клиента в БД", None

        peer_id = None
        try:
            # Шаг 2. Создаём peer в WGDashboard
            peer_result = wg_api.add_peer(peer_name)
            if not peer_result or "id" not in peer_result:
                raise Exception("Ошибка при создании пира на сервере")
            peer_id = peer_result["id"]

            # Шаг 3. Создаём job в WGDashboard
            logger.info(f"Создаем новый job для пира {peer_id}")
            job_result, new_job_id, final_expire_date = wg_api.create_restrict_job(
                peer_id, target_expire_date
            )
            if not job_result or (
                isinstance(job_result, dict) and job_result.get("status") is False
            ):
                raise Exception("Ошибка при создании job на сервере")

            # Финализируем запись в БД реальными peer_id/job_id
            finalized = db.finalize_staged_peer(
                telegram_user_id=user_id,
                stage_info=stage_info,
                peer_name=peer_name,
                peer_id=peer_id,
                job_id=new_job_id,
                expire_date=final_expire_date,
                telegram_username=username or "",
                payment_status="paid",
                tariff_key=tariff_key,
            )
            if not finalized:
                raise Exception("Ошибка при финализации клиента в БД")

            # Шаг 4. Обновляем clients.json
            client_id_for_json = username if username else str(user_id)
            if not clients_manager.add_update_client(client_id_for_json, peer_id):
                raise Exception("Ошибка при обновлении clients.json")
        except Exception as e:
            # Компенсация: удаляем уже созданный peer, затем откатываем staged-запись в БД
            if peer_id:
                try:
                    wg_api.delete_peer(peer_id)
                except Exception as delete_error:
                    logger.error(f"Не удалось удалить peer {peer_id} после ошибки: {delete_error}")

            rollback_ok = db.rollback_staged_peer(user_id, stage_info)
            if not rollback_ok:
                logger.error(f"Не удалось откатить staged-запись пользователя {user_id}")
            logger.error(f"Ошибка в процессе создания/пересоздания пира: {e}")
            return False, "Ошибка при создании/восстановлении доступа", None

        # Скачиваем конфиг (для проверки что он есть) с повторными попытками
        config_content = None
        # Увеличиваем время ожидания: 10 попыток по 2 секунды = 20 секунд макс
        for attempt in range(10):
            try:
                config_content = wg_api.download_peer_config(peer_id)
                if config_content:
                    break
            except Exception as e:
                logger.info(f"Попытка {attempt + 1}: конфиг для {peer_id} еще не готов (ошибка: {e})")
            
            if attempt < 9: # Не ждать после последней попытки
                logger.info(f"Ждем 2 сек перед следующей попыткой...")
                await asyncio.sleep(2)
            
        if not config_content:
             return False, "Не удалось скачать конфигурацию (превышено время ожидания 20с)", None

        return True, "", config_content
    except Exception as e:
        logger.error(f"Ошибка в create_or_restore_peer_for_user: {e}")
        return False, "Ошибка при создании/восстановлении доступа", None


# Вспомогательная функция для безопасного ответа на callback query
async def safe_answer_callback(callback_query: types.CallbackQuery, text: str = None):
    """Безопасно отвечает на callback query, игнорируя ошибки истекших запросов"""
    try:
        await callback_query.answer(text=text)
    except TelegramAPIError as e:
        # Игнорируем ошибки истекших callback queries (возникают при перезапуске бота)
        if "query is too old" in str(e) or "query ID is invalid" in str(e):
            logger.debug(f"Callback query expired: {e}")
        else:
            # Другие ошибки логируем
            logger.error(f"Error answering callback query: {e}")


# Состояния для FSM
class PeerStates(StatesGroup):
    waiting_for_peer_name = State()


# Вспомогательная функция для проверки активного доступа
def is_access_active(existing_peer: dict) -> bool:
    """Проверяет, есть ли у пользователя активный (оплаченный и не истекший) доступ"""
    if not existing_peer:
        logger.debug("is_access_active: нет existing_peer")
        return False

    payment_status = existing_peer.get("payment_status")
    if payment_status != "paid":
        logger.debug(f"is_access_active: payment_status={payment_status}, не 'paid'")
        return False

    # Проверяем срок действия
    expire_date_str = existing_peer.get("expire_date")
    if not expire_date_str:
        logger.debug("is_access_active: нет expire_date")
        return False

    try:
        from datetime import datetime

        expire_date = parse_date_flexible(expire_date_str)
        now = datetime.now()
        is_active = expire_date > now
        logger.debug(
            f"is_access_active: expire_date={expire_date_str}, now={now}, is_active={is_active}"
        )
        return is_active
    except (ValueError, TypeError) as e:
        logger.error(
            f"is_access_active: ошибка при парсинге даты {expire_date_str}: {e}"
        )
        return False


# Функция для создания главного меню с inline кнопками
def create_main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Создает главное меню с inline кнопками"""
    # Проверяем, есть ли у пользователя активный (оплаченный и не истекший) доступ
    # ВАЖНО: всегда получаем свежие данные из БД при создании клавиатуры
    existing_peer = db.get_peer_by_telegram_id(user_id)
    has_active_access = is_access_active(existing_peer)

    # Логируем для отладки
    if existing_peer:
        logger.debug(
            f"create_main_menu_keyboard user_id={user_id}, payment_status={existing_peer.get('payment_status')}, expire_date={existing_peer.get('expire_date')}, has_active_access={has_active_access}"
        )
    else:
        logger.debug(
            f"create_main_menu_keyboard user_id={user_id}, existing_peer=None, has_active_access={has_active_access}"
        )

    button_text = "✅ Доступ приобретен" if has_active_access else "💎 Купить доступ"
    button_callback = "already_paid" if has_active_access else "pay"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=button_text, callback_data=button_callback)],
            [
                InlineKeyboardButton(
                    text="📁 Получить\nконфиг", callback_data="get_config"
                ),
                InlineKeyboardButton(
                    text="⏰ Продлить\nдоступ", callback_data="extend"
                ),
            ],
            [InlineKeyboardButton(text="📊 Статус доступа", callback_data="status")],
            [
                InlineKeyboardButton(text="📖 Инструкция", callback_data="guide"),
                InlineKeyboardButton(text="❓ Есть вопрос?", url=SUPPORT_URL),
            ],
        ]
    )
    return keyboard


# Функция для создания клавиатуры инструкции
def create_guide_keyboard() -> InlineKeyboardMarkup:
    """Создает клавиатуру для инструкции"""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Вернуться в меню", callback_data="main")]
        ]
    )
    return keyboard


# Обработчики команд
@dp.message(F.text == "/start")
async def cmd_start(message: types.Message):
    """Обработчик команды /start"""
    user_id = message.from_user.id
    welcome_text = """
Привет! Здесь ты можешь подключиться к быстрому и безопасному VPN, который не подвержен блокировкам.

Чтобы начать пользоваться нашим vpn, скачай клиент AmneziaWG из своего магазина приложений

Выбери действие с помощью кнопок ниже:
    """

    await message.answer(welcome_text, reply_markup=create_main_menu_keyboard(user_id))


# Обработчики inline кнопок
@dp.callback_query(F.data == "pay")
async def handle_pay_callback(callback_query: types.CallbackQuery):
    """Обработчик кнопки 'Купить доступ'"""
    user_id = callback_query.from_user.id
    username = callback_query.from_user.username

    await safe_answer_callback(callback_query)

    # Отправляем выбор способа оплаты (это создает новое сообщение с инвойсом)
    await payment_manager.send_payment_selection(
        callback_query.message.chat.id, user_id
    )


@dp.callback_query(F.data == "already_paid")
async def handle_already_paid_callback(callback_query: types.CallbackQuery):
    """Обработчик кнопки 'Доступ приобретен'"""
    user_id = callback_query.from_user.id
    # ВАЖНО: получаем свежие данные из БД
    existing_peer = db.get_peer_by_telegram_id(user_id)

    # Проверяем, активен ли доступ (проверяем заново при каждом нажатии)
    if not is_access_active(existing_peer):
        # Доступ истек, но был оплачен - обновляем клавиатуру на "Купить доступ"
        expire_date_str = existing_peer.get("expire_date", "Неизвестно") if existing_peer else "Неизвестно"
        expire_date_formatted = format_date_for_user(expire_date_str) if expire_date_str != "Неизвестно" else "Неизвестно"
        await safe_answer_callback(callback_query, "⚠️ Твой VPN доступ истек!")

        # Получаем актуальные тарифы
        payment_info = payment_manager.get_payment_info()
        tariffs = payment_info["tariffs"]
        tariff_text = ""
        for tariff_key, tariff_data in tariffs.items():
            tariff_text += (
                f"⭐ {tariff_data['name']} - {tariff_data['stars_price']} Stars\n"
            )
            tariff_text += (
                f"💳 {tariff_data['name']} - {tariff_data['rub_price']} руб.\n\n"
            )

        expired_text = f"""
⚠️ Твой доступ к VPN истек!

📅 Дата истечения: {expire_date_formatted}

💎 Для продолжения использования VPN необходимо продлить доступ.

💎 Доступные тарифы:
{tariff_text}Выбери действие с помощью кнопок ниже:
        """
        # Обновляем сообщение с новой клавиатурой, где кнопка будет "Купить доступ"
        await callback_query.message.edit_text(
            expired_text, reply_markup=create_main_menu_keyboard(user_id)
        )
        return

    await safe_answer_callback(callback_query, "✅ У тебя уже есть доступ!")

    # Обновляем сообщение с информацией о доступе
    payment_info = payment_manager.get_payment_info()

    already_paid_text = """
✅ У тебя уже есть активный доступ к VPN!

Используй кнопки ниже для управления доступом:
    """

    # Обновляем сообщение с актуальной клавиатурой
    await callback_query.message.edit_text(
        already_paid_text, reply_markup=create_main_menu_keyboard(user_id)
    )


@dp.callback_query(F.data == "get_config")
async def handle_get_config_callback(callback_query: types.CallbackQuery):
    """Обработчик кнопки 'Получить конфиг'"""
    await safe_answer_callback(callback_query)

    user_id = callback_query.from_user.id
    username = callback_query.from_user.username

    # Проверяем, есть ли уже активный пир у пользователя
    existing_peer = db.get_peer_by_telegram_id(user_id)
    if existing_peer:
        # Проверяем, активен ли доступ (оплачен и не истек)
        if not is_access_active(existing_peer):
            # Доступ истек или не оплачен
            if existing_peer.get("payment_status") == "paid":
                # Доступ был оплачен, но истек
                expire_date_str = existing_peer.get("expire_date", "Неизвестно")
                expire_date_formatted = format_date_for_user(expire_date_str) if expire_date_str != "Неизвестно" else "Неизвестно"
                error_text = f"""
⚠️ Твой доступ к VPN истек!

📅 Дата истечения: {expire_date_formatted}

💎 Для получения VPN конфигурации необходимо продлить доступ.

Выбери действие с помощью кнопок ниже:
                """
            else:
                # Доступ не оплачен
                error_text = """
❌ У тебя нет активного доступа.

💎 Для получения VPN конфигурации необходимо оплатить доступ.

Выбери действие с помощью кнопок ниже:
                """
            await callback_query.message.edit_text(
                error_text, reply_markup=create_main_menu_keyboard(user_id)
            )
            return

        # Пользователь имеет активный доступ, пытаемся отдать конфиг или восстановить при отсутствии
        try:
            # Сначала пытаемся проверить существование пира
            peer_exists = False
            try:
                peer_exists = wg_api.check_peer_exists(existing_peer["peer_id"])
            except Exception as e:
                logger.warning(
                    f"Не удалось проверить существование пира {existing_peer['peer_id']}: {e}, попробуем скачать"
                )

            # Пытаемся скачать конфиг
            config_downloaded = False
            peer_config = None
            if peer_exists:
                try:
                    peer_config = wg_api.download_peer_config(existing_peer["peer_id"])
                    config_downloaded = True
                except Exception as e:
                    logger.warning(
                        f"Не удалось скачать конфиг существующего пира: {e}, попробуем создать новый"
                    )
                    config_downloaded = False

            # Если не удалось скачать конфиг (пир не существует или ошибка), создаем новый
            if not config_downloaded or not peer_config:
                logger.info(
                    f"Создаю новый пир для пользователя {user_id}, так как существующий недоступен"
                )
                ok, err, new_config = await create_or_restore_peer_for_user(
                    user_id, username, existing_peer.get("tariff_key")
                )
                if not ok:
                    await callback_query.message.edit_text(
                        f"❌ {err}\n\nВыбери действие с помощью кнопок ниже:",
                        reply_markup=create_main_menu_keyboard(user_id),
                    )
                    return
                
                # Используем полученный конфиг
                peer_config = new_config

            # Отправляем конфиг
            config_filename = "nikonVPN.conf"
            config_bytes = (
                peer_config
                if isinstance(peer_config, (bytes, bytearray))
                else peer_config.encode("utf-8")
            )
            await callback_query.message.reply_document(
                document=types.BufferedInputFile(
                    config_bytes, filename=config_filename
                ),
                caption="Вот твой файл конфигурации, добавь его в приложение AmneziaWG",
            )
            success_text = """
✅ Конфигурация отправлена!

Выбери действие с помощью кнопок ниже:
            """
            await callback_query.message.edit_text(
                success_text, reply_markup=create_main_menu_keyboard(user_id)
            )
        except Exception as e:
            logger.error(
                f"Ошибка при получении/восстановлении конфигурации: {e}", exc_info=True
            )
            # При любой ошибке пытаемся создать новый пир (только если доступ оплачен)
            try:
                logger.info(
                    f"Попытка создать новый пир после ошибки для пользователя {user_id}"
                )
                ok, err, new_config = await create_or_restore_peer_for_user(
                    user_id, username, existing_peer.get("tariff_key")
                )
                if ok and new_config:
                    # Если удалось создать, отправляем конфиг
                    config_filename = "nikonVPN.conf"
                    config_bytes = (
                        new_config
                        if isinstance(new_config, (bytes, bytearray))
                        else new_config.encode("utf-8")
                    )
                    await callback_query.message.reply_document(
                        document=types.BufferedInputFile(
                            config_bytes, filename=config_filename
                        ),
                        caption="Вот твой файл конфигурации, добавь его в приложение AmneziaWG",
                    )
                    await callback_query.message.edit_text(
                        "✅ Конфигурация отправлена!\n\nВыбери действие с помощью кнопок ниже:",
                        reply_markup=create_main_menu_keyboard(user_id),
                    )
                    return

                await callback_query.message.edit_text(
                    f"❌ Ошибка при получении конфигурации: {err if not ok else 'Не удалось скачать конфиг'}.\n\nВыбери действие с помощью кнопок ниже:",
                    reply_markup=create_main_menu_keyboard(user_id),
                )
            except Exception as e2:
                logger.error(f"Критическая ошибка при создании нового пира: {e2}")
                await callback_query.message.edit_text(
                    "❌ Ошибка при получении конфигурации. Попробуй позже или обратись в поддержку.",
                    reply_markup=create_main_menu_keyboard(user_id),
                )
    else:
        # Пользователь не имеет пира
        error_text = """
❌ У тебя нет VPN доступа.

💎 Для получения VPN конфигурации необходимо сначала оплатить доступ.

Выбери действие с помощью кнопок ниже:
        """
        await callback_query.message.edit_text(
            error_text, reply_markup=create_main_menu_keyboard(user_id)
        )


@dp.callback_query(F.data == "extend")
async def handle_extend_callback(callback_query: types.CallbackQuery):
    """Обработчик кнопки 'Продлить доступ'"""
    await safe_answer_callback(callback_query)

    user_id = callback_query.from_user.id
    username = callback_query.from_user.username

    # Проверяем, есть ли у пользователя активный пир
    existing_peer = db.get_peer_by_telegram_id(user_id)
    if not existing_peer:
        error_text = """
❌ У тебя нет активного VPN доступа.

💎 Сначала необходимо купить доступ.

Выбери действие с помощью кнопок ниже:
        """
        await callback_query.message.edit_text(
            error_text, reply_markup=create_main_menu_keyboard(user_id)
        )
        return

    # Проверяем статус оплаты
    if existing_peer.get("payment_status") != "paid":
        error_text = """
❌ У тебя нет оплаченного доступа.

💎 Сначала необходимо оплатить доступ.

Выбери действие с помощью кнопок ниже:
        """
        await callback_query.message.edit_text(
            error_text, reply_markup=create_main_menu_keyboard(user_id)
        )
        return

    # Отправляем выбор способа оплаты для продления (это создает новое сообщение с инвойсом)
    await payment_manager.send_payment_selection(
        callback_query.message.chat.id, user_id
    )


@dp.callback_query(F.data == "status")
async def handle_status_callback(callback_query: types.CallbackQuery):
    """Обработчик кнопки 'Статус доступа'"""
    await safe_answer_callback(callback_query)

    user_id = callback_query.from_user.id
    username = callback_query.from_user.username

    # Проверяем, есть ли у пользователя активный пир
    existing_peer = db.get_peer_by_telegram_id(user_id)
    if not existing_peer:
        error_text = """
❌ У тебя нет активного VPN доступа.

💎 Для получения доступа необходимо его оплатить.

Выбери действие с помощью кнопок ниже:
        """
        await callback_query.message.edit_text(
            error_text, reply_markup=create_main_menu_keyboard(user_id)
        )
        return

    # Получаем информацию о пире из базы данных
    try:
        expire_date_str = existing_peer.get("expire_date", "Неизвестно")
        created_at_str = existing_peer.get("created_at", "Неизвестно")
        
        # Форматируем даты для отображения
        expire_date_formatted = format_date_for_user(expire_date_str) if expire_date_str != "Неизвестно" else "Неизвестно"
        created_at_formatted = format_date_for_user(created_at_str) if created_at_str != "Неизвестно" else "Неизвестно"

        # Проверяем, истек ли доступ
        from datetime import datetime

        is_expired = False
        if expire_date_str and expire_date_str != "Неизвестно":
            try:
                expire_date = parse_date_flexible(expire_date_str)
                now = datetime.now()
                is_expired = expire_date <= now
            except (ValueError, TypeError):
                pass

        # Форматируем информацию о пире
        if is_expired:
            status_text = f"""
📊 Статус доступа:

📅 Доступ приобретен: {created_at_formatted}
⏰ Доступ закончился: {expire_date_formatted}

⚠️ Твой VPN доступ истек!

💎 Для продолжения использования VPN необходимо продлить доступ.

Выбери действие с помощью кнопок ниже:
            """
        else:
            # Доступ активен, рассчитываем оставшееся время
            try:
                expire_date = parse_date_flexible(expire_date_str)
                now = datetime.now()
                time_left = expire_date - now
                days_left = time_left.days
                hours_left = time_left.seconds // 3600
                minutes_left = (time_left.seconds % 3600) // 60

                status_text = f"""
📊 Статус доступа:

📅 Доступ приобретен: {created_at_formatted}
⏰ Доступ закончится: {expire_date_formatted}
                """

                if days_left > 0:
                    status_text += f"\n⏰ Осталось: {days_left} дн. {hours_left} ч. {minutes_left} мин."
                elif hours_left > 0:
                    status_text += f"\n⏰ Осталось: {hours_left} ч. {minutes_left} мин."
                else:
                    status_text += f"\n⏰ Осталось: {minutes_left} мин."

                if days_left <= 3:
                    status_text += (
                        "\n\n⚠️ Доступ истекает скоро! Используй /extend для продления."
                    )

                status_text += "\n\nВыбери действие с помощью кнопок ниже:"
            except (ValueError, TypeError):
                status_text = f"""
📊 Статус доступа:

📅 Доступ приобретен: {created_at_formatted}
⏰ Доступ закончится: {expire_date_formatted}

Выбери действие с помощью кнопок ниже:
                """

        await callback_query.message.edit_text(
            status_text, reply_markup=create_main_menu_keyboard(user_id)
        )

    except Exception as e:
        logger.error(f"Ошибка при получении информации о пире: {e}")
        error_text = """
❌ Ошибка при получении информации о пире.

Выбери действие с помощью кнопок ниже:
        """
        await callback_query.message.edit_text(
            error_text, reply_markup=create_main_menu_keyboard(user_id)
        )


@dp.callback_query(F.data == "guide")
async def handle_guide_callback(callback_query: types.CallbackQuery):
    """Обработчик кнопки 'Инструкция'"""
    await safe_answer_callback(callback_query)

    guide_text = """
📖 Инструкция по использованию VPN:

1️⃣ Скачайте клиент AmneziaWG:
   • Windows: https://github.com/amnezia-vpn/amneziawg-windows-client/releases
   • Android: Google Play https://play.google.com/store/apps/details?id=org.amnezia.awg
   • iOS/macOS: App Store https://apps.apple.com/pl/app/amneziawg/id6478942365

2️⃣ Получите конфигурацию:
   • Нажмите "📁 Получить конфиг"
   • Скачайте .conf файл

3️⃣ Импортируйте конфигурацию:
   • Откройте AmneziaWG
   • Нажмите "Добавить туннель"
   • Выберите скачанный файл

4️⃣ Подключитесь:
   • Нажмите "Подключить"
   • Готово! 🎉
    """

    await callback_query.message.edit_text(
        guide_text, reply_markup=create_guide_keyboard()
    )


@dp.callback_query(F.data == "main")
async def handle_main_callback(callback_query: types.CallbackQuery):
    """Обработчик кнопки 'Вернуться в меню'"""
    await safe_answer_callback(callback_query)

    user_id = callback_query.from_user.id
    payment_info = payment_manager.get_payment_info()

    welcome_text = f"""
Привет! Здесь ты можешь подключиться к быстрому и безопасному VPN.

Чтобы начать пользоваться нашим vpn, скачай клиент AmneziaWG из своего магазина приложений

💎 Стоимость за {payment_info["period"]}:
⭐ Telegram Stars: {payment_info["stars_price"]} Stars
💳 Картой (Юmoney): {payment_info["rub_price"]} руб.

Выбери действие с помощью кнопок ниже:
    """

    await callback_query.message.edit_text(
        welcome_text, reply_markup=create_main_menu_keyboard(user_id)
    )


@dp.message(F.text == "/connect")
async def cmd_connect(message: types.Message):
    """Обработчик команды /connect"""
    user_id = message.from_user.id
    username = message.from_user.username

    # Проверяем, есть ли уже активный пир у пользователя
    existing_peer = db.get_peer_by_telegram_id(user_id)
    if existing_peer:
        # Проверяем, активен ли доступ (оплачен и не истек)
        if not is_access_active(existing_peer):
            # Доступ истек или не оплачен
            payment_info = payment_manager.get_payment_info()
            if existing_peer.get("payment_status") == "paid":
                # Доступ был оплачен, но истек
                expire_date_str = existing_peer.get("expire_date", "Неизвестно")
                expire_date_formatted = format_date_for_user(expire_date_str) if expire_date_str != "Неизвестно" else "Неизвестно"
                await message.reply(
                    f"⚠️ Твой VPN доступ истек!\n\n"
                    f"📅 Дата истечения: {expire_date_formatted}\n\n"
                    f"💎 Для получения конфигурации необходимо продлить доступ.\n\n"
                    f"Стоимость за {payment_info['period']}:\n"
                    f"⭐ Telegram Stars: {payment_info['stars_price']} Stars\n"
                    f"💳 Банковская карта: {payment_info['rub_price']} руб."
                )
            else:
                # Доступ не оплачен
                await message.reply(
                    f"❌ Доступ не оплачен!\n\n"
                    f"💎 Стоимость за {payment_info['period']}:\n"
                    f"⭐ Telegram Stars: {payment_info['stars_price']} Stars\n"
                    f"💳 Банковская карта: {payment_info['rub_price']} руб.\n\n"
                    f"Для получения конфигурации необходимо оплатить доступ."
                )

            # Отправляем выбор способа оплаты
            await payment_manager.send_payment_selection(message.chat.id, user_id)
            return

        # Пользователь имеет активный доступ
        # Проверяем, существует ли пир на сервере и можем ли скачать конфиг
        try:
            peer_exists = False
            try:
                peer_exists = wg_api.check_peer_exists(existing_peer["peer_id"])
            except Exception as e:
                logger.warning(
                    f"Не удалось проверить существование пира: {e}, попробуем скачать"
                )

            config_downloaded = False
            if peer_exists:
                try:
                    await message.reply("Скачиваю конфиг...")
                    config_content = wg_api.download_peer_config(
                        existing_peer["peer_id"]
                    )
                    filename = "nikonVPN.conf"

                    await bot.send_document(
                        chat_id=message.chat.id,
                        document=types.BufferedInputFile(
                            file=config_content, filename=filename
                        ),
                        caption="📁 Твой файл конфигурации",
                    )
                    return
                except Exception as e:
                    logger.warning(
                        f"Не удалось скачать конфиг существующего пира: {e}, попробуем создать новый"
                    )
                    config_downloaded = False

            # Если не удалось скачать конфиг, создаем новый пир
            if not config_downloaded:
                await message.reply("Создаю новый конфиг...")
                ok, err, _ = await create_or_restore_peer_for_user(
                    user_id, username, existing_peer.get("tariff_key")
                )
                if not ok:
                    await message.reply(f"❌ {err}")
                return
        except Exception as e:
            logger.error(f"Ошибка при получении конфига в /connect: {e}", exc_info=True)
            # Пытаемся создать новый пир при любой ошибке
            try:
                await message.reply("Попытка создать новый конфиг...")
                ok, err, _ = await create_or_restore_peer_for_user(
                    user_id, username, existing_peer.get("tariff_key")
                )
                if not ok:
                    await message.reply(f"❌ {err}")
            except Exception as e2:
                logger.error(f"Критическая ошибка при создании нового пира: {e2}")
                await message.reply(
                    "❌ Ошибка при получении конфигурации. Попробуй позже или обратись в поддержку."
                )

    # Новый пользователь - нужно оплатить доступ
    payment_info = payment_manager.get_payment_info()
    await message.reply(
        f"💎 Для получения VPN конфигурации необходимо оплатить доступ!\n\n"
        f"Стоимость за {payment_info['period']}:\n"
        f"⭐ Telegram Stars: {payment_info['stars_price']} Stars\n"
        f"💳 Картой (Юmoney): {payment_info['rub_price']} руб.\n\n"
        f"После оплаты предоставим тебе конфигурацию и доступ на {payment_info['period']}."
    )

    # Отправляем выбор способа оплаты
    await payment_manager.send_payment_selection(message.chat.id, user_id)


@dp.message(F.text == "/extend")
async def cmd_extend(message: types.Message):
    """Обработчик команды /extend - продление доступа"""
    user_id = message.from_user.id
    username = message.from_user.username

    # Проверяем, есть ли активный пир у пользователя
    existing_peer = db.get_peer_by_telegram_id(user_id)
    if not existing_peer:
        await message.reply(
            "❌ У тебя нет активного VPN доступа.\nИспользуй /connect для создания нового."
        )
        return

    # Проверяем, оплачен ли текущий доступ
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

    # Отправляем выбор способа оплаты для продления
    await payment_manager.send_payment_selection(message.chat.id, user_id)


@dp.message(F.text == "/status")
async def cmd_status(message: types.Message):
    """Обработчик команды /status - проверка оставшегося времени доступа"""
    user_id = message.from_user.id

    # Проверяем, есть ли активный пир у пользователя
    existing_peer = db.get_peer_by_telegram_id(user_id)
    if not existing_peer:
        await message.reply(
            "❌ Нет активного VPN доступа.\nИспользуй /connect для создания нового."
        )
        return

    # Проверяем, оплачен ли доступ
    if existing_peer.get("payment_status") != "paid":
        await message.reply("❌ Доступ не оплачен.\nИспользуй /connect для оплаты.")
        return

    # Получаем дату истечения
    expire_date_str = existing_peer.get("expire_date")
    if not expire_date_str:
        await message.reply("❌ Не удалось получить информацию о сроке доступа.")
        return

    try:
        from datetime import datetime

        expire_date = datetime.strptime(expire_date_str, "%Y-%m-%d")
        now = datetime.now()

        if expire_date <= now:
            await message.reply(
                "⚠️ Твой VPN доступ истек!\nИспользуйте /extend для продления."
            )
            return

        # Рассчитываем оставшееся время
        time_left = expire_date - now
        days_left = time_left.days
        hours_left = time_left.seconds // 3600
        minutes_left = (time_left.seconds % 3600) // 60

        # Формируем сообщение
        status_text = f"📊 Статус твоего VPN доступа:\n\n"
        status_text += (
            f"📅 Дата истечения: {expire_date.strftime('%d.%m.%Y')}\n\n"
        )

        if days_left > 0:
            status_text += (
                f"⏰ Осталось: {days_left} дн. {hours_left} ч. {minutes_left} мин."
            )
        elif hours_left > 0:
            status_text += f"⏰ Осталось: {hours_left} ч. {minutes_left} мин."
        else:
            status_text += f"⏰ Осталось: {minutes_left} мин."

        if days_left <= 3:
            status_text += (
                "\n\n⚠️ Доступ истекает скоро! Используй /extend для продления."
            )

        await message.reply(status_text)

    except ValueError as e:
        logger.error(f"Ошибка при парсинге даты истечения: {e}")
        await message.reply("❌ Ошибка при получении информации о доступе.")


@dp.message(F.text == "/buy")
async def cmd_buy(message: types.Message):
    """Обработчик команды /buy - выбор способа оплаты"""
    user_id = message.from_user.id
    username = message.from_user.username

    # Отправляем выбор способа оплаты
    await payment_manager.send_payment_selection(message.chat.id, user_id)


# Обработчики callback-кнопок для выбора способа оплаты
@dp.callback_query(F.data.startswith("pay_stars_"))
async def handle_pay_stars_callback(callback_query: types.CallbackQuery):
    """Обработчик выбора оплаты через Telegram Stars"""
    # Извлекаем tariff_key и user_id из callback_data (формат: pay_stars_14_days_123456789)
    callback_parts = callback_query.data.split("_")
    tariff_key = (
        f"{callback_parts[2]}_{callback_parts[3]}"  # 14_days, 30_days или 90_days
    )
    user_id = int(callback_parts[-1])  # Последняя часть - user_id
    username = callback_query.from_user.username

    # Проверяем, что callback от правильного пользователя
    if callback_query.from_user.id != user_id:
        await safe_answer_callback(callback_query, "❌ Ошибка: неверный пользователь")
        return

    await safe_answer_callback(callback_query)

    # Отправляем инвойс для оплаты через Stars
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


@dp.callback_query(F.data.startswith("pay_yookassa_"))
async def handle_pay_yookassa_callback(callback_query: types.CallbackQuery):
    """Обработчик выбора оплаты через ЮKassa"""
    # Извлекаем tariff_key и user_id из callback_data (формат: pay_yookassa_14_days_123456789)
    callback_parts = callback_query.data.split("_")
    tariff_key = (
        f"{callback_parts[2]}_{callback_parts[3]}"  # 14_days, 30_days или 90_days
    )
    user_id = int(callback_parts[-1])  # Последняя часть - user_id
    username = callback_query.from_user.username

    # Проверяем, что callback от правильного пользователя
    if callback_query.from_user.id != user_id:
        await safe_answer_callback(callback_query, "❌ Ошибка: неверный пользователь")
        return

    await safe_answer_callback(callback_query)

    # Проверяем, настроен ли ЮKassa
    if (
        not payment_manager.yookassa_client.shop_id
        or not payment_manager.yookassa_client.secret_key
    ):
        await callback_query.message.reply(
            "❌ Оплата через банковскую карту временно недоступна.\n\n"
            "💡 Используйте оплату через Telegram Stars.\n\n"
            "🔧 Для настройки ЮKassa обратитесь к администратору."
        )
        return

    # Отправляем инвойс для оплаты через ЮKassa
    success = await payment_manager.send_yookassa_payment_request(
        callback_query.message.chat.id, user_id, tariff_key, username
    )

    if not success:
        user_tariffs = payment_manager.get_user_tariffs(user_id)
        tariff_data = user_tariffs.get(tariff_key, {})
        tariff_name = tariff_data.get("name", "неизвестный тариф")
        rub_price = tariff_data.get("rub_price", 0)
        await callback_query.message.reply(
            f"❌ Ошибка при создании запроса на оплату через ЮKassa.\n\n"
            f"🔧 Возможные причины:\n"
            f"• Проблемы с настройкой платежей\n\n"
            f"💡 Используйте оплату через Telegram Stars.\n"
            f"💳 Стоимость: {rub_price} руб. за {tariff_name} доступа"
        )


@dp.callback_query(F.data.startswith("pay_yookassa_disabled_"))
async def handle_pay_yookassa_disabled_callback(callback_query: types.CallbackQuery):
    """Обработчик нажатия на неактивную кнопку ЮKassa"""
    user_id = int(callback_query.data.replace("pay_yookassa_disabled_", ""))

    # Проверяем, что callback от правильного пользователя
    if callback_query.from_user.id != user_id:
        await safe_answer_callback(callback_query, "❌ Ошибка: неверный пользователь")
        return

    await safe_answer_callback(callback_query)

    await callback_query.message.reply(
        "❌ Оплата через банковскую карту временно недоступна.\n\n"
        "💡 Используй оплату через Telegram Stars:\n"
        "⭐ 1 Starsа за 30 дней доступа\n\n"
        "🔧 Для настройки ЮKassa обратитесь к администратору."
    )


# Повторное создание конфига после успешной оплаты, если первоначально упало
@dp.callback_query(F.data.startswith("retry_peer_"))
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

        await callback_query.message.edit_text("🔄 Повторяю создание VPN доступа...")
        ok, err, _ = await create_or_restore_peer_for_user(user_id, username, tariff_key)
        if ok:
            await callback_query.message.edit_text(
                "✅ Доступ создан и конфигурация отправлена!",
                reply_markup=create_main_menu_keyboard(user_id),
            )
        else:
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🔁 Повторить ещё раз",
                            callback_data=f"retry_peer_{tariff_key}_{user_id}",
                        )
                    ],
                    [InlineKeyboardButton(text="🆘 Поддержка", url=SUPPORT_URL)],
                ]
            )
            await callback_query.message.edit_text(
                f"❌ {err}\n\nПопробуй ещё раз или обратись в поддержку.",
                reply_markup=keyboard,
            )
    except Exception as e:
        logger.error(f"Ошибка в обработчике retry_peer: {e}")
        await callback_query.message.edit_text(
            "❌ Ошибка при повторном создании. Попробуй ещё раз позже.",
            reply_markup=create_main_menu_keyboard(callback_query.from_user.id),
        )


# Обработчики платежей
@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query):
    """Обработчик предварительной проверки платежа"""
    await payment_manager.process_payment(pre_checkout_query)


@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    """Обработчик успешного платежа"""
    user_id = message.from_user.id
    username = message.from_user.username
    successful_payment = message.successful_payment

    # Получаем payload из успешного платежа
    payload = successful_payment.invoice_payload

    # Подтверждаем платеж
    (
        payment_confirmed,
        payment_type,
        amount_paid,
    ) = await payment_manager.confirm_payment(successful_payment)
    if not payment_confirmed:
        await message.reply("❌ Ошибка при обработке платежа.")
        return

    # Обрабатываем только Stars платежи (ЮKassa обрабатывается через webhook)
    if not payload.startswith("vpn_access_stars_"):
        await message.reply("❌ Неизвестный тип платежа.")
        return

    # Извлекаем тариф из payload
    payload_parts = payload.split("_")
    if len(payload_parts) >= 4:
        tariff_key = f"{payload_parts[3]}_{payload_parts[4]}"  # 14_days, 30_days
    else:
        await message.reply("❌ Ошибка в данных платежа.")
        return

    payment_method = "stars"

    # Логируем платеж и обновляем статус оплаты
    try:
        payment_id = (
            getattr(successful_payment, "telegram_payment_charge_id", None)
            or getattr(successful_payment, "provider_payment_charge_id", None)
            or f"stars_{user_id}_{tariff_key}"
        )
        db.add_payment(
            payment_id=payment_id,
            user_id=user_id,
            amount=amount_paid,
            payment_method="stars",
            tariff_key=tariff_key,
            metadata={"source": "telegram_stars"},
        )
        db.update_payment_status_by_id(payment_id, "succeeded")
    except Exception as e:
        logger.warning(f"Не удалось зафиксировать платеж Stars в БД: {e}")

    # Обновляем статус оплаты для пользователя
    db.update_payment_status(user_id, "paid", amount_paid, payment_method, tariff_key)

    # Определяем период доступа на основе тарифа
    tariff_data = payment_manager.tariffs.get(tariff_key, {})
    access_days = tariff_data.get("days", 30)

    # Проверяем, есть ли уже пир у пользователя
    existing_peer = db.get_peer_by_telegram_id(user_id)

    if existing_peer:
        # Продлеваем доступ существующего пира
        success, new_expire_date = db.extend_access(user_id, access_days)

        if not success:
            await message.reply(
                "❌ Ошибка при продлении доступа. Обратитесь в поддержку."
            )
            return

        # Проверяем существование пира в WGDashboard
        peer_exists = None
        try:
            peer_exists = wg_api.check_peer_exists(existing_peer["peer_id"])
        except Exception as e:
            logger.error(f"Ошибка при проверке существования пира в WGDashboard: {e}")

        allow_result = None
        try:
            allow_result = wg_api.allow_access_peer(existing_peer["peer_id"])
            if allow_result and allow_result.get("status"):
                logger.info(f"Restricted снят для пользователя {user_id}")
                peer_exists = True
            else:
                logger.warning(
                    f"Не удалось снять restricted для пользователя {user_id}: {allow_result}"
                )
        except Exception as e:
            logger.error(f"Ошибка при снятии restricted в WGDashboard: {e}")

        if peer_exists is True:
            try:
                job_update_result = wg_api.update_job_expire_date(
                    existing_peer["job_id"], existing_peer["peer_id"], new_expire_date
                )

                if job_update_result and job_update_result.get("status"):
                    logger.info(
                        f"Job обновлен для пользователя {user_id}, новая дата: {new_expire_date}"
                    )
                else:
                    logger.error(
                        f"Ошибка при обновлении job для пользователя {user_id}: {job_update_result}"
                    )

            except Exception as e:
                logger.error(f"Ошибка при обновлении job в WGDashboard: {e}")

            # При продлении не отправляем конфигурацию повторно
            await message.reply(
                f"✅ Платеж успешно обработан!\n"
                f"🎉 Продлили тебе доступ на {access_days} дней!\n"
                f"💳 Способ оплаты: ⭐ Telegram Stars\n\n"
                f"Текущая конфигурация остается актуальной."
            )
        elif peer_exists is False:
            logger.warning(
                f"Пир пользователя {user_id} не найден в WGDashboard, создаем новый"
            )
            await message.reply("🔄 Восстанавливаю VPN доступ...")
            ok, err, _ = await create_or_restore_peer_for_user(user_id, username, tariff_key)
            if not ok:
                await message.reply(
                    "❌ Ошибка при восстановлении VPN доступа. Обратитесь в поддержку."
                )
                logger.error(
                    f"Не удалось пересоздать пир для пользователя {user_id}: {err}"
                )
                return

            await message.reply(
                f"✅ Платеж успешно обработан!\n"
                f"🎉 Продлили тебе доступ на {access_days} дней!\n"
                f"💳 Способ оплаты: ⭐ Telegram Stars\n\n"
                f"Доступ восстановлен, используй /connect для получения актуального конфига."
            )
        else:
            await message.reply(
                "❌ Не удалось проверить статус VPN на сервере. Попробуйте еще раз через минуту или обратитесь в поддержку."
            )
            logger.error(
                f"Статус пира пользователя {user_id} не определен, пересоздание отменено чтобы избежать дубля"
            )
            return

        # Не отправляем дополнительное сообщение после продления доступа
    else:
        # Создаем новый пир для пользователя
        try:
            await message.reply("🔄 Создаю VPN доступ...")
            ok, err, _ = await create_or_restore_peer_for_user(
                user_id, username, tariff_key
            )
            if not ok:
                # Предлагаем повторить и поддержку
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="🔁 Повторить создание",
                                callback_data=f"retry_peer_{tariff_key}_{user_id}",
                            )
                        ],
                        [InlineKeyboardButton(text="🆘 Поддержка", url=SUPPORT_URL)],
                    ]
                )
                await message.reply(
                    "❌ Ошибка при создании VPN доступа. Ты можешь попробовать ещё раз или обратиться в поддержку.",
                    reply_markup=keyboard,
                )
                return
        except Exception as e:
            logger.error(f"Ошибка при создании пира после оплаты: {e}")
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🔁 Повторить создание",
                            callback_data=f"retry_peer_{tariff_key}_{user_id}",
                        )
                    ],
                    [InlineKeyboardButton(text="🆘 Поддержка", url=SUPPORT_URL)],
                ]
            )
            await message.reply(
                "❌ Ошибка при создании VPN доступа. Ты можешь попробовать ещё раз или обратиться в поддержку.",
                reply_markup=keyboard,
            )


# Обработчик неизвестных команд
@dp.message()
async def handle_unknown(message: types.Message):
    """Обработчик неизвестных сообщений"""
    user_id = message.from_user.id

    # Проверяем, есть ли у пользователя оплаченный доступ
    existing_peer = db.get_peer_by_telegram_id(user_id)
    has_paid_access = existing_peer and existing_peer.get("payment_status") == "paid"

    # Показываем главное меню для неизвестных команд
    await message.answer(
        "❓ Неизвестная команда.\n\nИспользуй кнопки ниже или команды:\n/start - главное меню\n/buy - купить доступ\n/connect - получить конфиг",
        reply_markup=create_main_menu_keyboard(user_id),
    )


# Функция для периодической проверки истекших пиров и уведомлений
async def check_expired_peers():
    """Проверяет истекшие пиры и уведомляет пользователей"""
    while True:
        try:
            # Проверяем истекшие пиры
            expired_peers = db.get_expired_peers()

            for peer in expired_peers:
                try:
                    await bot.send_message(
                        chat_id=peer["telegram_user_id"],
                        text=f"⚠️ Твой VPN доступ истек!\n\n"
                        f"Используй /extend для продления доступа на 30 дней.",
                    )
                    db.mark_expired_notification_sent(peer["telegram_user_id"])
                except TelegramAPIError:
                    logger.warning(
                        f"Не удалось отправить уведомление об истечении пользователю {peer['telegram_user_id']}"
                    )

            # Проверяем пользователей для уведомления за 1 день
            users_for_notification = db.get_users_for_notification(1)

            for user in users_for_notification:
                try:
                    payment_info = payment_manager.get_payment_info()
                    tariffs = payment_info["tariffs"]

                    # Формируем текст с доступными тарифами
                    tariff_text = ""
                    for tariff_key, tariff_data in tariffs.items():
                        tariff_text += f"⭐ {tariff_data['name']} - {tariff_data['stars_price']} Stars\n"
                        tariff_text += f"💳 {tariff_data['name']} - {tariff_data['rub_price']} руб.\n\n"

                    await bot.send_message(
                        chat_id=user["telegram_user_id"],
                        text=f"⏰ Твой VPN доступ истекает завтра!\n\n"
                             f"💎 Доступные тарифы для продления:\n{tariff_text}"
                             f"Используй кнопки ниже для продления доступа.",
                    )

                    # Отмечаем, что уведомление отправлено
                    db.mark_notification_sent(user["telegram_user_id"])

                except TelegramAPIError:
                    logger.warning(
                        f"Не удалось отправить уведомление пользователю {user['telegram_user_id']}"
                    )

            # Проверяем каждые 30 минут
            await asyncio.sleep(30 * 60)

        except Exception as e:
            logger.error(f"Ошибка при проверке истекших пиров: {e}")
            await asyncio.sleep(60)  # Ждем минуту при ошибке


async def main():
    """Основная функция запуска бота"""
    try:
        # Запускаем проверку истекших пиров и уведомлений в фоне
        asyncio.create_task(check_expired_peers())

        # Запускаем бота
        logger.info("Запуск WireGuard бота...")
        await dp.start_polling(bot, skip_updates=True)

    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")


if __name__ == "__main__":
    asyncio.run(main())
