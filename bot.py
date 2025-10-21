import logging
import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError

from config import TELEGRAM_BOT_TOKEN
from wg_api import WGDashboardAPI
from database import Database
from payment import PaymentManager
from utils import (
    generate_peer_name, format_peer_info, format_peer_list, 
    validate_peer_name, sanitize_filename
)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/wgbot.log'),
        logging.StreamHandler()
    ]
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

# Состояния для FSM
class PeerStates(StatesGroup):
    waiting_for_peer_name = State()

# Функция для создания главного меню с inline кнопками
def create_main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Создает главное меню с inline кнопками"""
    # Проверяем, есть ли у пользователя оплаченный доступ
    existing_peer = db.get_peer_by_telegram_id(user_id)
    has_paid_access = existing_peer and existing_peer.get('payment_status') == 'paid'
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💎 Купить доступ" if not has_paid_access else "✅ Доступ приобретен",
                    callback_data="pay" if not has_paid_access else "already_paid"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📁 Получить\nконфиг",
                    callback_data="get_config"
                ),
                InlineKeyboardButton(
                    text="⏰ Продлить\nдоступ",
                    callback_data="extend"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📊 Статус доступа",
                    callback_data="status"
                )
            ]
        ]
    )
    return keyboard

# Функция для создания клавиатуры инструкции
def create_guide_keyboard() -> InlineKeyboardMarkup:
    """Создает клавиатуру для инструкции"""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔙 Вернуться в меню",
                    callback_data="main"
                )
            ]
        ]
    )
    return keyboard

# Обработчики команд
@dp.message(F.text == '/start')
async def cmd_start(message: types.Message):
    """Обработчик команды /start"""
    user_id = message.from_user.id
    payment_info = payment_manager.get_payment_info()
    tariffs = payment_info['tariffs']
    
    # Формируем текст с доступными тарифами
    tariff_text = ""
    for tariff_key, tariff_data in tariffs.items():
        tariff_text += f"⭐ {tariff_data['name']} - {tariff_data['stars_price']} Stars\n"
        tariff_text += f"💳 {tariff_data['name']} - {tariff_data['rub_price']} руб.\n\n"
    
    welcome_text = f"""
Привет! Здесь ты можешь подключиться к быстрому и безопасному VPN.

Чтобы начать пользоваться нашим vpn, скачай клиент с нашего сайта >> https://nikonvpn.xyz

💎 Доступные тарифы:
{tariff_text}Выбери действие с помощью кнопок ниже:
    """
    
    await message.answer(welcome_text, reply_markup=create_main_menu_keyboard(user_id))

# Обработчики inline кнопок
@dp.callback_query(F.data == "pay")
async def handle_pay_callback(callback_query: types.CallbackQuery):
    """Обработчик кнопки 'Купить доступ'"""
    user_id = callback_query.from_user.id
    username = callback_query.from_user.username
    
    await callback_query.answer()
    
    # Отправляем выбор способа оплаты (это создает новое сообщение с инвойсом)
    await payment_manager.send_payment_selection(callback_query.message.chat.id, user_id)

@dp.callback_query(F.data == "already_paid")
async def handle_already_paid_callback(callback_query: types.CallbackQuery):
    """Обработчик кнопки 'Доступ приобретен'"""
    await callback_query.answer("✅ У тебя уже есть доступ!")
    
    # Обновляем сообщение с информацией о доступе
    user_id = callback_query.from_user.id
    payment_info = payment_manager.get_payment_info()
    
    already_paid_text = """
✅ У тебя уже есть активный VPN доступ!

Используй кнопки ниже для управления доступом:
    """
    
    await callback_query.message.edit_text(
        already_paid_text,
        reply_markup=create_main_menu_keyboard(user_id)
    )

@dp.callback_query(F.data == "get_config")
async def handle_get_config_callback(callback_query: types.CallbackQuery):
    """Обработчик кнопки 'Получить конфиг'"""
    await callback_query.answer()
    
    user_id = callback_query.from_user.id
    username = callback_query.from_user.username
    
    # Проверяем, есть ли уже активный пир у пользователя
    existing_peer = db.get_peer_by_telegram_id(user_id)
    if existing_peer:
        # Проверяем статус оплаты
        if existing_peer.get('payment_status') != 'paid':
            # Пользователь не оплатил доступ
            error_text = """
❌ У тебя нет активного доступа.

💎 Для получения VPN конфигурации необходимо оплатить доступ.

Выбери действие с помощью кнопок ниже:
            """
            await callback_query.message.edit_text(
                error_text,
                reply_markup=create_main_menu_keyboard(user_id)
            )
            return
        
        # Пользователь оплатил доступ, отправляем конфигурацию
        try:
            # Получаем конфигурацию пира
            peer_config = wg_api.download_peer_config(existing_peer['peer_id'])
            if peer_config:
                # Отправляем конфигурацию как файл (это создает новое сообщение)
                config_filename = "nikonVPN.conf"
                
                # Проверяем, нужно ли кодировать в байты
                if isinstance(peer_config, str):
                    config_bytes = peer_config.encode('utf-8')
                else:
                    config_bytes = peer_config
                
                await callback_query.message.reply_document(
                    document=types.BufferedInputFile(
                        config_bytes,
                        filename=config_filename
                    ),
                    caption="Вот твой файл конфигурации, добавь его в приложение AmneziaWG"
                )
                
                # Возвращаемся к главному меню
                user_id = callback_query.from_user.id
                
                success_text = """
✅ Конфигурация отправлена!

Выбери действие с помощью кнопок ниже:
                """
                
                await callback_query.message.edit_text(
                    success_text,
                    reply_markup=create_main_menu_keyboard(user_id)
                )
            else:
                error_text = """
❌ Ошибка при получении конфигурации.

Выбери действие с помощью кнопок ниже:
                """
                await callback_query.message.edit_text(
                    error_text,
                    reply_markup=create_main_menu_keyboard(user_id)
                )
        except Exception as e:
            logger.error(f"Ошибка при получении конфигурации: {e}")
            error_text = """
❌ Ошибка при получении конфигурации.

Выбери действие с помощью кнопок ниже:
            """
            await callback_query.message.edit_text(
                error_text,
                reply_markup=create_main_menu_keyboard(user_id)
            )
    else:
        # Пользователь не имеет пира
        error_text = """
❌ У тебя нет VPN доступа.

💎 Для получения VPN конфигурации необходимо сначала оплатить доступ.

Выбери действие с помощью кнопок ниже:
        """
        await callback_query.message.edit_text(
            error_text,
            reply_markup=create_main_menu_keyboard(user_id)
        )

@dp.callback_query(F.data == "extend")
async def handle_extend_callback(callback_query: types.CallbackQuery):
    """Обработчик кнопки 'Продлить доступ'"""
    await callback_query.answer()
    
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
            error_text,
            reply_markup=create_main_menu_keyboard(user_id)
        )
        return
    
    # Проверяем статус оплаты
    if existing_peer.get('payment_status') != 'paid':
        error_text = """
❌ У тебя нет оплаченного доступа.

💎 Сначала необходимо оплатить доступ.

Выбери действие с помощью кнопок ниже:
        """
        await callback_query.message.edit_text(
            error_text,
            reply_markup=create_main_menu_keyboard(user_id)
        )
        return
    
    # Отправляем выбор способа оплаты для продления (это создает новое сообщение с инвойсом)
    await payment_manager.send_payment_selection(callback_query.message.chat.id, user_id)

@dp.callback_query(F.data == "status")
async def handle_status_callback(callback_query: types.CallbackQuery):
    """Обработчик кнопки 'Статус доступа'"""
    await callback_query.answer()
    
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
            error_text,
            reply_markup=create_main_menu_keyboard(user_id)
        )
        return
    
    # Проверяем статус оплаты
    if existing_peer.get('payment_status') != 'paid':
        error_text = """
❌ У тебя нет оплаченного доступа.

💎 Для получения доступа необходимо его оплатить.

Выбери действие с помощью кнопок ниже:
        """
        await callback_query.message.edit_text(
            error_text,
            reply_markup=create_main_menu_keyboard(user_id)
        )
        return
    
    # Получаем информацию о пире из базы данных
    try:
        # Создаем простую информацию о пире из данных базы
        peer_info = {
            'name': existing_peer['peer_name'],
            'id': existing_peer['peer_id'],
            'expire_date': existing_peer.get('expire_date', 'Неизвестно'),
            'created_at': existing_peer.get('created_at', 'Неизвестно'),
            'payment_status': existing_peer.get('payment_status', 'Неизвестно')
        }
        
        # Форматируем информацию о пире
        status_text = f"""
📊 Статус доступа:

📅 Доступ приобретен: {peer_info['created_at']}
⏰ Доступ закончится: {peer_info['expire_date']}

Выбери действие с помощью кнопок ниже:
        """
        
        await callback_query.message.edit_text(
            status_text,
            reply_markup=create_main_menu_keyboard(user_id)
        )
        
    except Exception as e:
        logger.error(f"Ошибка при получении информации о пире: {e}")
        error_text = """
❌ Ошибка при получении информации о пире.

Выбери действие с помощью кнопок ниже:
        """
        await callback_query.message.edit_text(
            error_text,
            reply_markup=create_main_menu_keyboard(user_id)
        )

@dp.callback_query(F.data == "guide")
async def handle_guide_callback(callback_query: types.CallbackQuery):
    """Обработчик кнопки 'Инструкция'"""
    await callback_query.answer()
    
    guide_text = """
📖 Инструкция по использованию VPN:

1️⃣ Скачайте клиент WireGuard:
   • Windows/Mac/Linux: https://www.wireguard.com/install/
   • Android: WireGuard в Google Play
   • iOS: WireGuard в App Store

2️⃣ Получите конфигурацию:
   • Нажмите "📁 Получить конфиг"
   • Скачайте .conf файл

3️⃣ Импортируйте конфигурацию:
   • Откройте WireGuard
   • Нажмите "Добавить туннель"
   • Выберите скачанный файл

4️⃣ Подключитесь:
   • Нажмите "Подключить"
   • Готово! 🎉
    """
    
    await callback_query.message.edit_text(
        guide_text,
        reply_markup=create_guide_keyboard()
    )

@dp.callback_query(F.data == "main")
async def handle_main_callback(callback_query: types.CallbackQuery):
    """Обработчик кнопки 'Вернуться в меню'"""
    await callback_query.answer()
    
    user_id = callback_query.from_user.id
    payment_info = payment_manager.get_payment_info()
    
    welcome_text = f"""
Привет! Здесь ты можешь подключиться к быстрому и безопасному VPN.

Чтобы начать пользоваться нашим vpn, скачай клиент с нашего сайта >> https://nikonvpn.xyz

💎 Стоимость за {payment_info['period']}:
⭐ Telegram Stars: {payment_info['stars_price']} Stars
💳 Картой (Юmoney): {payment_info['rub_price']} руб.

Выбери действие с помощью кнопок ниже:
    """
    
    await callback_query.message.edit_text(
        welcome_text,
        reply_markup=create_main_menu_keyboard(user_id)
    )

@dp.message(F.text == '/connect')
async def cmd_connect(message: types.Message):
    """Обработчик команды /connect"""
    user_id = message.from_user.id
    username = message.from_user.username
    
    # Проверяем, есть ли уже активный пир у пользователя
    existing_peer = db.get_peer_by_telegram_id(user_id)
    if existing_peer:
        # Проверяем статус оплаты
        if existing_peer.get('payment_status') != 'paid':
            # Пользователь не оплатил доступ
            payment_info = payment_manager.get_payment_info()
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
        # Проверяем, существует ли пир на сервере
        peer_exists = wg_api.check_peer_exists(existing_peer['peer_id'])
        
        if peer_exists:
            # Если пир существует, отправляем его конфигурацию
            try:
                await message.reply("Скачиваю конфиг...")
                config_content = wg_api.download_peer_config(existing_peer['peer_id'])
                filename = "nikonVPN.conf"
                
                await bot.send_document(
                    chat_id=message.chat.id,
                    document=types.BufferedInputFile(
                        file=config_content,
                        filename=filename
                    ),
                    caption="📁 Твой файл конфигурации"
                )
                return
            except Exception as e:
                logger.error(f"Ошибка при скачивании существующей конфигурации: {e}")
                await message.reply("❌ Ошибка при получении конфигурации.")
                return
        else:
            # Пир не существует на сервере, но есть в базе - создаем новый
            await message.reply("Создаю новый конфиг...")
            
            try:
                # Создаем новый пир с тем же именем
                peer_result = wg_api.add_peer(existing_peer['peer_name'])
                
                if not peer_result or 'id' not in peer_result:
                    await message.reply("❌ Ошибка при создании нового пира.")
                    return
                
                new_peer_id = peer_result['id']
                
                # Создаем новый job с той же датой истечения
                job_result, new_job_id, new_expire_date = wg_api.create_restrict_job(new_peer_id, existing_peer['expire_date'])
                
                # Обновляем информацию в базе данных
                db.update_peer_info(existing_peer['peer_name'], new_peer_id, new_job_id, new_expire_date)
                
                # Скачиваем и отправляем конфигурацию
                config_content = wg_api.download_peer_config(new_peer_id)
                filename = "nikonVPN.conf"
                
                await bot.send_document(
                    chat_id=message.chat.id,
                    document=types.BufferedInputFile(
                        file=config_content,
                        filename=filename
                    ),
                    caption="📁 Твоя VPN конфигурация"
                )
                return
                
            except Exception as e:
                logger.error(f"Ошибка при восстановлении пира: {e}")
                await message.reply("❌ Ошибка при восстановлении пира.")
                return
    
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


@dp.message(F.text == '/extend')
async def cmd_extend(message: types.Message):
    """Обработчик команды /extend - продление доступа"""
    user_id = message.from_user.id
    username = message.from_user.username
    
    # Проверяем, есть ли активный пир у пользователя
    existing_peer = db.get_peer_by_telegram_id(user_id)
    if not existing_peer:
        await message.reply("❌ У тебя нет активного VPN доступа.\nИспользуй /connect для создания нового.")
        return
    
    # Проверяем, оплачен ли текущий доступ
    if existing_peer.get('payment_status') != 'paid':
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


@dp.message(F.text == '/status')
async def cmd_status(message: types.Message):
    """Обработчик команды /status - проверка оставшегося времени доступа"""
    user_id = message.from_user.id
    
    # Проверяем, есть ли активный пир у пользователя
    existing_peer = db.get_peer_by_telegram_id(user_id)
    if not existing_peer:
        await message.reply("❌ Нет активного VPN доступа.\nИспользуй /connect для создания нового.")
        return
    
    # Проверяем, оплачен ли доступ
    if existing_peer.get('payment_status') != 'paid':
        await message.reply("❌ Доступ не оплачен.\nИспользуй /connect для оплаты.")
        return
    
    # Получаем дату истечения
    expire_date_str = existing_peer.get('expire_date')
    if not expire_date_str:
        await message.reply("❌ Не удалось получить информацию о сроке доступа.")
        return
    
    try:
        from datetime import datetime
        expire_date = datetime.strptime(expire_date_str, "%Y-%m-%d %H:%M:%S")
        now = datetime.now()
        
        if expire_date <= now:
            await message.reply("⚠️ Твой VPN доступ истек!\nИспользуйте /extend для продления.")
            return
        
        # Рассчитываем оставшееся время
        time_left = expire_date - now
        days_left = time_left.days
        hours_left = time_left.seconds // 3600
        minutes_left = (time_left.seconds % 3600) // 60
        
        # Формируем сообщение
        status_text = f"📊 Статус твоего VPN доступа:\n\n"
        status_text += f"📅 Дата истечения: {expire_date.strftime('%d.%m.%Y %H:%M')}\n\n"
        
        if days_left > 0:
            status_text += f"⏰ Осталось: {days_left} дн. {hours_left} ч. {minutes_left} мин."
        elif hours_left > 0:
            status_text += f"⏰ Осталось: {hours_left} ч. {minutes_left} мин."
        else:
            status_text += f"⏰ Осталось: {minutes_left} мин."
        
        if days_left <= 3:
            status_text += "\n\n⚠️ Доступ истекает скоро! Используй /extend для продления."
        
        await message.reply(status_text)
        
    except ValueError as e:
        logger.error(f"Ошибка при парсинге даты истечения: {e}")
        await message.reply("❌ Ошибка при получении информации о доступе.")


@dp.message(F.text == '/buy')
async def cmd_buy(message: types.Message):
    """Обработчик команды /buy - выбор способа оплаты"""
    user_id = message.from_user.id
    username = message.from_user.username
    
    # Отправляем выбор способа оплаты
    await payment_manager.send_payment_selection(message.chat.id, user_id)


# Обработчики callback-кнопок для выбора способа оплаты
@dp.callback_query(F.data.startswith('pay_stars_'))
async def handle_pay_stars_callback(callback_query: types.CallbackQuery):
    """Обработчик выбора оплаты через Telegram Stars"""
    # Извлекаем tariff_key и user_id из callback_data (формат: pay_stars_14_days_123456789)
    callback_parts = callback_query.data.split('_')
    tariff_key = f"{callback_parts[2]}_{callback_parts[3]}"  # 14_days, 30_days или 90_days
    user_id = int(callback_parts[-1])  # Последняя часть - user_id
    username = callback_query.from_user.username
    
    # Проверяем, что callback от правильного пользователя
    if callback_query.from_user.id != user_id:
        await callback_query.answer("❌ Ошибка: неверный пользователь")
        return
    
    await callback_query.answer()
    
    # Отправляем инвойс для оплаты через Stars
    success = await payment_manager.send_stars_payment_request(
        callback_query.message.chat.id, user_id, tariff_key, username
    )
    
    if not success:
        tariff_data = payment_manager.tariffs.get(tariff_key, {})
        tariff_name = tariff_data.get('name', 'неизвестный тариф')
        stars_price = tariff_data.get('stars_price', 1)
        await callback_query.message.reply(
            f"❌ Ошибка при создании запроса на оплату через Telegram Stars.\n\n"
            f"💡 Убедись, что у тебя есть Telegram Stars на балансе.\n"
            f"⭐ Стоимость: {stars_price} Stars за {tariff_name} доступа"
        )


@dp.callback_query(F.data.startswith('pay_yookassa_'))
async def handle_pay_yookassa_callback(callback_query: types.CallbackQuery):
    """Обработчик выбора оплаты через ЮKassa"""
    # Извлекаем tariff_key и user_id из callback_data (формат: pay_yookassa_14_days_123456789)
    callback_parts = callback_query.data.split('_')
    tariff_key = f"{callback_parts[2]}_{callback_parts[3]}"  # 14_days, 30_days или 90_days
    user_id = int(callback_parts[-1])  # Последняя часть - user_id
    username = callback_query.from_user.username
    
    # Проверяем, что callback от правильного пользователя
    if callback_query.from_user.id != user_id:
        await callback_query.answer("❌ Ошибка: неверный пользователь")
        return
    
    await callback_query.answer()
    
    # Проверяем, настроен ли ЮKassa
    if not payment_manager.yookassa_client.shop_id or not payment_manager.yookassa_client.secret_key:
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
        tariff_data = payment_manager.tariffs.get(tariff_key, {})
        tariff_name = tariff_data.get('name', 'неизвестный тариф')
        rub_price = tariff_data.get('rub_price', 0)
        await callback_query.message.reply(
            f"❌ Ошибка при создании запроса на оплату через ЮKassa.\n\n"
            f"🔧 Возможные причины:\n"
            f"• Проблемы с настройкой платежей\n\n"
            f"💡 Используйте оплату через Telegram Stars.\n"
            f"💳 Стоимость: {rub_price} руб. за {tariff_name} доступа"
        )


@dp.callback_query(F.data.startswith('pay_yookassa_disabled_'))
async def handle_pay_yookassa_disabled_callback(callback_query: types.CallbackQuery):
    """Обработчик нажатия на неактивную кнопку ЮKassa"""
    user_id = int(callback_query.data.replace('pay_yookassa_disabled_', ''))
    
    # Проверяем, что callback от правильного пользователя
    if callback_query.from_user.id != user_id:
        await callback_query.answer("❌ Ошибка: неверный пользователь")
        return
    
    await callback_query.answer()
    
    await callback_query.message.reply(
        "❌ Оплата через банковскую карту временно недоступна.\n\n"
        "💡 Используй оплату через Telegram Stars:\n"
        "⭐ 1 Starsа за 30 дней доступа\n\n"
        "🔧 Для настройки ЮKassa обратитесь к администратору."
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
    payment_confirmed, payment_type, amount_paid = await payment_manager.confirm_payment(successful_payment)
    if not payment_confirmed:
        await message.reply("❌ Ошибка при обработке платежа.")
        return
    
    # Обрабатываем только Stars платежи (ЮKassa обрабатывается через webhook)
    if not payload.startswith('vpn_access_stars_'):
        await message.reply("❌ Неизвестный тип платежа.")
        return
    
    # Извлекаем тариф из payload
    payload_parts = payload.split('_')
    if len(payload_parts) >= 4:
        tariff_key = f"{payload_parts[3]}_{payload_parts[4]}"  # 14_days, 30_days
    else:
        await message.reply("❌ Ошибка в данных платежа.")
        return
    
    payment_method = 'stars'
    
    # Обновляем статус оплаты в базе данных
    db.update_payment_status(user_id, 'paid', amount_paid, payment_method, tariff_key)
    
    # Определяем период доступа на основе тарифа
    tariff_data = payment_manager.tariffs.get(tariff_key, {})
    access_days = tariff_data.get('days', 30)
    
    # Проверяем, есть ли уже пир у пользователя
    existing_peer = db.get_peer_by_telegram_id(user_id)
    
    if existing_peer:
        # Продлеваем доступ существующего пира
        success, new_expire_date = db.extend_access(user_id, access_days)
        
        if not success:
            await message.reply("❌ Ошибка при продлении доступа. Обратитесь в поддержку.")
            return
        
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
        
        # При продлении не отправляем конфигурацию повторно
        await message.reply(
            f"✅ Платеж успешно обработан!\n"
            f"🎉 Продлили тебе доступ на {access_days} дней!\n"
            f"💳 Способ оплаты: ⭐ Telegram Stars\n\n"
            f"Текущая конфигурация остается актуальной."
        )
        
        # Не отправляем дополнительное сообщение после продления доступа
    else:
        # Создаем новый пир для пользователя
        try:
            await message.reply("🔄 Создаю VPN доступ...")
            
            # Генерируем имя пира
            peer_name = generate_peer_name(username, user_id)
            
            # Создаем пира
            peer_result = wg_api.add_peer(peer_name)
            
            if not peer_result or 'id' not in peer_result:
                await message.reply("❌ Ошибка при создании пира. Обратитесь в поддержку.")
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
                telegram_username=username,
                expire_date=expire_date,
                payment_status='paid',
                stars_paid=amount_paid if payment_method == 'stars' else 0,
                tariff_key=tariff_key,
                payment_method=payment_method,
                rub_paid=amount_paid if payment_method == 'yookassa' else 0
            )
            
            if not success:
                await message.reply("❌ Ошибка при сохранении данных. Обратитесь в поддержку.")
                return
            
            # Скачиваем и отправляем конфигурацию
            config_content = wg_api.download_peer_config(peer_id)
            filename = "nikonVPN.conf"
            
            await bot.send_document(
                chat_id=message.chat.id,
                document=types.BufferedInputFile(
                    file=config_content,
                    filename=filename
                ),
                caption=f"✅ Платеж успешно обработан!\n💳 Способ оплаты: ⭐ Telegram Stars\n🎉 VPN доступ на {access_days} дней!\n📁 Ваша VPN конфигурация готова!"
            )
            
            # Не отправляем дополнительное сообщение после создания нового доступа
            
        except Exception as e:
            logger.error(f"Ошибка при создании пира после оплаты: {e}")
            await message.reply("❌ Ошибка при создании VPN доступа. Обратитесь в поддержку.")


# Обработчик неизвестных команд
@dp.message()
async def handle_unknown(message: types.Message):
    """Обработчик неизвестных сообщений"""
    user_id = message.from_user.id
    
    # Проверяем, есть ли у пользователя оплаченный доступ
    existing_peer = db.get_peer_by_telegram_id(user_id)
    has_paid_access = existing_peer and existing_peer.get('payment_status') == 'paid'
    
    # Показываем главное меню для неизвестных команд
    await message.answer(
        "❓ Неизвестная команда.\n\nИспользуй кнопки ниже или команды:\n/start - главное меню\n/buy - купить доступ\n/connect - получить конфиг",
        reply_markup=create_main_menu_keyboard(user_id)
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
                        chat_id=peer['telegram_user_id'],
                        text=f"⚠️ Твой VPN доступ истек!\n\n"
                             f"Используй /extend для продления доступа на 30 дней."
                    )
                except TelegramAPIError:
                    logger.warning(f"Не удалось отправить уведомление об истечении пользователю {peer['telegram_user_id']}")
            
            # Проверяем пользователей для уведомления за 1 день
            users_for_notification = db.get_users_for_notification(1)
            
            for user in users_for_notification:
                try:
                    payment_info = payment_manager.get_payment_info()
                    tariffs = payment_info['tariffs']
                    
                    # Формируем текст с доступными тарифами
                    tariff_text = ""
                    for tariff_key, tariff_data in tariffs.items():
                        tariff_text += f"⭐ {tariff_data['name']} - {tariff_data['stars_price']} Stars\n"
                        tariff_text += f"💳 {tariff_data['name']} - {tariff_data['rub_price']} руб.\n\n"
                    
                    await bot.send_message(
                        chat_id=user['telegram_user_id'],
                        text=f"⏰ Твой VPN доступ истекает завтра!\n\n"
                             f"💎 Доступные тарифы для продления:\n{tariff_text}"
                             f"Используй кнопки ниже для продления доступа."
                    )
                    
                    # Отмечаем, что уведомление отправлено
                    db.mark_notification_sent(user['telegram_user_id'])
                    
                except TelegramAPIError:
                    logger.warning(f"Не удалось отправить уведомление пользователю {user['telegram_user_id']}")
            
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

if __name__ == '__main__':
    asyncio.run(main())
