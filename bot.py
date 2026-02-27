import asyncio
import logging

from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import (
    CLIENTS_JSON_PATH,
    CUSTOM_CLIENTS_PATH,
    SUPPORT_URL,
    TELEGRAM_BOT_TOKEN,
)
from custom_clients import CustomClientsManager, sync_custom_peers_access
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

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("logs/wgbot.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# Bot and dispatcher initialization
bot = Bot(token=TELEGRAM_BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Component initialization
wg_api = WGDashboardAPI()
db = Database()
payment_manager = PaymentManager(bot)
clients_manager = ClientsJsonManager(CLIENTS_JSON_PATH)
custom_clients_manager = CustomClientsManager(CUSTOM_CLIENTS_PATH)


def sync_clients_json_for_user(
    user_id: int, username: str | None, peer_id: str
) -> bool:
    client_id_for_json = username if username else str(user_id)
    if username:
        clients_manager.remove_client(str(user_id))
    return clients_manager.add_update_client(
        client_id_for_json, peer_id, force_write=True
    )


def sync_bound_custom_peers_for_user(
    user_id: int,
    expire_date: str,
    allow_access: bool = True,
    exclude_peer_id: str | None = None,
) -> None:
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


# Helper: create or restore a peer and return config
async def create_or_restore_peer_for_user(
    user_id: int, username: str | None, tariff_key: str | None = None
) -> tuple[bool, str, bytes | None]:
    """Create a peer or restore it if missing on the server. Returns (success, error_message, config_content)."""
    try:
        existing_peer = db.get_peer_by_telegram_id(user_id)

        # Determine expiration
        if existing_peer and existing_peer.get("expire_date"):
            # Restore using the existing date
            target_expire_date = existing_peer["expire_date"]
        else:
            # New user or no date: take it from the tariff
            access_days = 30
            if tariff_key:
                tariff_data = payment_manager.tariffs.get(tariff_key, {})
                access_days = tariff_data.get("days", 30)
            from datetime import datetime, timedelta

            target_expire_date = (
                datetime.now() + timedelta(days=access_days)
            ).strftime("%Y-%m-%d %H:%M:%S")

        # Peer name
        # Prefer username_id format when username is present
        # This fixes cases where the old peer used user_id and the user now has a username
        peer_name = generate_peer_name(username, user_id)

        # Step 1. Stage the DB record first
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
            # Step 2. Create peer in WGDashboard
            peer_result = wg_api.add_peer(peer_name)
            if not peer_result or "id" not in peer_result:
                raise Exception("Ошибка при создании пира на сервере")
            peer_id = peer_result["id"]

            # Step 3. Create job in WGDashboard
            logger.info(f"Creating new job for peer {peer_id}")
            job_result, new_job_id, final_expire_date = wg_api.create_restrict_job(
                peer_id, target_expire_date
            )
            if not job_result or (
                isinstance(job_result, dict) and job_result.get("status") is False
            ):
                raise Exception("Ошибка при создании job на сервере")

            # Finalize DB record with real peer_id/job_id
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

            # Step 4. Update clients.json
            client_id_for_json = username if username else str(user_id)
            if username:
                clients_manager.remove_client(str(user_id))
            if not sync_clients_json_for_user(user_id, username, peer_id):
                raise Exception("Ошибка при обновлении clients.json")

            sync_bound_custom_peers_for_user(
                user_id=user_id,
                expire_date=final_expire_date,
                allow_access=True,
                exclude_peer_id=peer_id,
            )
        except Exception as e:
            # Compensation: delete created peer, then roll back staged DB record
            if peer_id:
                try:
                    wg_api.delete_peer(peer_id)
                except Exception as delete_error:
                    logger.error(f"Failed to delete peer {peer_id} after error: {delete_error}")

            rollback_ok = db.rollback_staged_peer(user_id, stage_info)
            if not rollback_ok:
                logger.error(f"Failed to roll back staged record for user {user_id}")
            logger.error(f"Error during peer create/restore flow: {e}")
            return False, "Ошибка при создании/восстановлении доступа", None

        # Download config (to confirm it exists) with retries
        config_content = None
        # Increase wait: 10 tries * 2 seconds = 20 seconds max
        for attempt in range(10):
            try:
                config_content = wg_api.download_peer_config(peer_id)
                if config_content:
                    break
            except Exception as e:
                logger.info(
                    f"Attempt {attempt + 1}: config for {peer_id} is not ready yet (error: {e})"
                )
            
            if attempt < 9:  # Do not wait after the last attempt
                logger.info("Waiting 2 seconds before the next attempt...")
                await asyncio.sleep(2)
            
        if not config_content:
             return False, "Не удалось скачать конфигурацию (превышено время ожидания 20с)", None

        return True, "", config_content
    except Exception as e:
        logger.error(f"Error in create_or_restore_peer_for_user: {e}")
        return False, "Ошибка при создании/восстановлении доступа", None


# Helper to safely answer callback queries
async def safe_answer_callback(callback_query: types.CallbackQuery, text: str = None):
    """Safely answer callback queries, ignoring expired query errors."""
    try:
        await callback_query.answer(text=text)
    except TelegramAPIError as e:
        # Ignore expired callback query errors (happen after bot restarts)
        if "query is too old" in str(e) or "query ID is invalid" in str(e):
            logger.debug(f"Callback query expired: {e}")
        else:
            # Log other errors
            logger.error(f"Error answering callback query: {e}")


# FSM states
class PeerStates(StatesGroup):
    waiting_for_peer_name = State()


# Helper to check active access
def is_access_active(existing_peer: dict) -> bool:
    """Check if the user has active (paid and not expired) access."""
    if not existing_peer:
        logger.debug("is_access_active: no existing_peer")
        return False

    payment_status = existing_peer.get("payment_status")
    if payment_status != "paid":
        logger.debug(f"is_access_active: payment_status={payment_status}, not 'paid'")
        return False

    # Check expiration
    expire_date_str = existing_peer.get("expire_date")
    if not expire_date_str:
        logger.debug("is_access_active: no expire_date")
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
            f"is_access_active: failed to parse date {expire_date_str}: {e}"
        )
        return False


# Build main menu keyboard
def create_main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Create the main menu inline keyboard."""
    # Check whether the user has active (paid and not expired) access
    # IMPORTANT: always fetch fresh data from the DB when building the keyboard
    existing_peer = db.get_peer_by_telegram_id(user_id)
    has_active_access = is_access_active(existing_peer)

    # Debug logging
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


# Build instruction keyboard
def create_guide_keyboard() -> InlineKeyboardMarkup:
    """Create the instruction keyboard."""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Вернуться в меню", callback_data="main")]
        ]
    )
    return keyboard


# Command handlers
@dp.message(F.text == "/start")
async def cmd_start(message: types.Message):
    """Handle the /start command."""
    user_id = message.from_user.id
    welcome_text = """
Привет! Здесь ты можешь подключиться к быстрому и безопасному VPN, который не подвержен блокировкам.

Чтобы начать пользоваться нашим vpn, скачай клиент AmneziaWG из своего магазина приложений

Выбери действие с помощью кнопок ниже:
    """

    await message.answer(welcome_text, reply_markup=create_main_menu_keyboard(user_id))


# Inline button handlers
@dp.callback_query(F.data == "pay")
async def handle_pay_callback(callback_query: types.CallbackQuery):
    """Handle the 'Buy access' button."""
    user_id = callback_query.from_user.id
    username = callback_query.from_user.username

    await safe_answer_callback(callback_query)

    # Send payment method selection (creates a new invoice message)
    await payment_manager.send_payment_selection(
        callback_query.message.chat.id, user_id
    )


@dp.callback_query(F.data == "already_paid")
async def handle_already_paid_callback(callback_query: types.CallbackQuery):
    """Handle the 'Access purchased' button."""
    user_id = callback_query.from_user.id
    # IMPORTANT: fetch fresh data from the DB
    existing_peer = db.get_peer_by_telegram_id(user_id)

    # Check if access is active (re-check on every tap)
    if not is_access_active(existing_peer):
        # Access expired but was paid: update keyboard to "Buy access"
        expire_date_str = existing_peer.get("expire_date", "Неизвестно") if existing_peer else "Неизвестно"
        expire_date_formatted = format_date_for_user(expire_date_str) if expire_date_str != "Неизвестно" else "Неизвестно"
        await safe_answer_callback(callback_query, "⚠️ Твой VPN доступ истек!")

        # Fetch current tariffs
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
        # Update message with new keyboard (button switches to "Buy access")
        await callback_query.message.edit_text(
            expired_text, reply_markup=create_main_menu_keyboard(user_id)
        )
        return

    await safe_answer_callback(callback_query, "✅ У тебя уже есть доступ!")

    # Update message with access info
    payment_info = payment_manager.get_payment_info()

    already_paid_text = """
✅ У тебя уже есть активный доступ к VPN!

Используй кнопки ниже для управления доступом:
    """

    # Update message with the current keyboard
    await callback_query.message.edit_text(
        already_paid_text, reply_markup=create_main_menu_keyboard(user_id)
    )


@dp.callback_query(F.data == "get_config")
async def handle_get_config_callback(callback_query: types.CallbackQuery):
    """Handle the 'Get config' button."""
    await safe_answer_callback(callback_query)

    user_id = callback_query.from_user.id
    username = callback_query.from_user.username

    # Check if the user already has an active peer
    existing_peer = db.get_peer_by_telegram_id(user_id)
    if existing_peer:
        # Check if access is active (paid and not expired)
        if not is_access_active(existing_peer):
            # Access expired or not paid
            if existing_peer.get("payment_status") == "paid":
                # Access was paid, but expired
                expire_date_str = existing_peer.get("expire_date", "Неизвестно")
                expire_date_formatted = format_date_for_user(expire_date_str) if expire_date_str != "Неизвестно" else "Неизвестно"
                error_text = f"""
⚠️ Твой доступ к VPN истек!

📅 Дата истечения: {expire_date_formatted}

💎 Для получения VPN конфигурации необходимо продлить доступ.

Выбери действие с помощью кнопок ниже:
                """
            else:
                # Access not paid
                error_text = """
❌ У тебя нет активного доступа.

💎 Для получения VPN конфигурации необходимо оплатить доступ.

Выбери действие с помощью кнопок ниже:
                """
            await callback_query.message.edit_text(
                error_text, reply_markup=create_main_menu_keyboard(user_id)
            )
            return

        # User has active access: try to send config or restore if missing
        try:
            # First try to check if the peer exists
            peer_exists = False
            try:
                peer_exists = wg_api.check_peer_exists(existing_peer["peer_id"])
            except Exception as e:
                logger.warning(
                    f"Failed to check peer {existing_peer['peer_id']} existence: {e}, trying download"
                )

            # Try to download config
            config_downloaded = False
            peer_config = None
            if peer_exists:
                try:
                    peer_config = wg_api.download_peer_config(existing_peer["peer_id"])
                    config_downloaded = True
                    if not sync_clients_json_for_user(
                        user_id, username, existing_peer["peer_id"]
                    ):
                        logger.warning(
                            f"Failed to update clients.json for user {user_id}"
                        )
                except Exception as e:
                    logger.warning(
                        f"Failed to download config for existing peer: {e}, trying to create a new one"
                    )
                    config_downloaded = False

            # If config download failed (peer missing or error), create a new peer
            if not config_downloaded or not peer_config:
                logger.info(
                    f"Creating a new peer for user {user_id} because the existing one is unavailable"
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
                
                # Use the received config
                peer_config = new_config
                refreshed_peer = db.get_peer_by_telegram_id(user_id)
                if refreshed_peer and refreshed_peer.get("peer_id"):
                    if not sync_clients_json_for_user(
                        user_id, username, refreshed_peer["peer_id"]
                    ):
                        logger.warning(
                            f"Failed to update clients.json for user {user_id}"
                        )

            # Send config
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
                f"Error while fetching/restoring configuration: {e}", exc_info=True
            )
            # On any error, try to create a new peer (only if access is paid)
            try:
                logger.info(
                    f"Attempting to create a new peer after error for user {user_id}"
                )
                ok, err, new_config = await create_or_restore_peer_for_user(
                    user_id, username, existing_peer.get("tariff_key")
                )
                if ok and new_config:
                    refreshed_peer = db.get_peer_by_telegram_id(user_id)
                    if refreshed_peer and refreshed_peer.get("peer_id"):
                        if not sync_clients_json_for_user(
                            user_id, username, refreshed_peer["peer_id"]
                        ):
                            logger.warning(
                                f"Failed to update clients.json for user {user_id}"
                            )
                    # If created successfully, send the config
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
                logger.error(f"Critical error while creating new peer: {e2}")
                await callback_query.message.edit_text(
                    "❌ Ошибка при получении конфигурации. Попробуй позже или обратись в поддержку.",
                    reply_markup=create_main_menu_keyboard(user_id),
                )
    else:
        # User has no peer
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
    """Handle the 'Extend access' button."""
    await safe_answer_callback(callback_query)

    user_id = callback_query.from_user.id
    username = callback_query.from_user.username

    # Check if the user has an active peer
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

    # Check payment status
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

    # Send payment method selection for extension (creates a new invoice message)
    await payment_manager.send_payment_selection(
        callback_query.message.chat.id, user_id
    )


@dp.callback_query(F.data == "status")
async def handle_status_callback(callback_query: types.CallbackQuery):
    """Handle the 'Access status' button."""
    await safe_answer_callback(callback_query)

    user_id = callback_query.from_user.id
    username = callback_query.from_user.username

    # Check if the user has an active peer
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

    # Get peer info from the database
    try:
        expire_date_str = existing_peer.get("expire_date", "Неизвестно")
        
        # Format dates for display
        expire_date_formatted = format_date_for_user(expire_date_str) if expire_date_str != "Неизвестно" else "Неизвестно"
        custom_peer_ids = custom_clients_manager.get_peers_for_user(user_id)
        devices_line = (
            f"\nПодключено устройств: {len(custom_peer_ids)}" if custom_peer_ids else ""
        )

        # Check if access has expired
        from datetime import datetime

        is_expired = False
        if expire_date_str and expire_date_str != "Неизвестно":
            try:
                expire_date = parse_date_flexible(expire_date_str)
                now = datetime.now()
                is_expired = expire_date <= now
            except (ValueError, TypeError):
                pass

        # Format peer info
        if is_expired:
            status_text = f"""
📊 Статус доступа:

⏰ Доступ закончился: {expire_date_formatted}{devices_line}

⚠️ Твой VPN доступ истек!

💎 Для продолжения использования VPN необходимо продлить доступ.

Выбери действие с помощью кнопок ниже:
            """
        else:
            # Access active: calculate remaining time
            try:
                expire_date = parse_date_flexible(expire_date_str)
                now = datetime.now()
                time_left = expire_date - now
                days_left = time_left.days
                hours_left = time_left.seconds // 3600
                minutes_left = (time_left.seconds % 3600) // 60

                status_text = f"""
📊 Статус доступа:

⏰ Доступ закончится: {expire_date_formatted}{devices_line}
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

⏰ Доступ закончится: {expire_date_formatted}{devices_line}

Выбери действие с помощью кнопок ниже:
                """

        await callback_query.message.edit_text(
            status_text, reply_markup=create_main_menu_keyboard(user_id)
        )

    except Exception as e:
        logger.error(f"Failed to fetch peer info: {e}")
        error_text = """
❌ Ошибка при получении информации о пире.

Выбери действие с помощью кнопок ниже:
        """
        await callback_query.message.edit_text(
            error_text, reply_markup=create_main_menu_keyboard(user_id)
        )


@dp.callback_query(F.data == "guide")
async def handle_guide_callback(callback_query: types.CallbackQuery):
    """Handle the 'Guide' button."""
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
    """Handle the 'Back to menu' button."""
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
    """Handle the /connect command."""
    user_id = message.from_user.id
    username = message.from_user.username

    # Check if the user already has an active peer
    existing_peer = db.get_peer_by_telegram_id(user_id)
    if existing_peer:
        # Check if access is active (paid and not expired)
        if not is_access_active(existing_peer):
            # Access expired or not paid
            payment_info = payment_manager.get_payment_info()
            if existing_peer.get("payment_status") == "paid":
                # Access was paid but expired
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
                # Access not paid
                await message.reply(
                    f"❌ Доступ не оплачен!\n\n"
                    f"💎 Стоимость за {payment_info['period']}:\n"
                    f"⭐ Telegram Stars: {payment_info['stars_price']} Stars\n"
                    f"💳 Банковская карта: {payment_info['rub_price']} руб.\n\n"
                    f"Для получения конфигурации необходимо оплатить доступ."
                )

            # Send payment method selection
            await payment_manager.send_payment_selection(message.chat.id, user_id)
            return

        # User has active access
        # Check whether the peer exists on the server and whether we can download the config
        try:
            peer_exists = False
            try:
                peer_exists = wg_api.check_peer_exists(existing_peer["peer_id"])
            except Exception as e:
                logger.warning(
                    f"Failed to check peer existence: {e}, trying to download"
                )

            config_downloaded = False
            if peer_exists:
                try:
                    await message.reply("Скачиваю конфиг...")
                    config_content = wg_api.download_peer_config(
                        existing_peer["peer_id"]
                    )
                    filename = "nikonVPN.conf"
                    if not sync_clients_json_for_user(
                        user_id, username, existing_peer["peer_id"]
                    ):
                        logger.warning(
                            f"Failed to update clients.json for user {user_id}"
                        )

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
                        f"Failed to download config for existing peer: {e}, trying to create a new one"
                    )
                    config_downloaded = False

            # If config download failed, create a new peer
            if not config_downloaded:
                await message.reply("Создаю новый конфиг...")
                ok, err, _ = await create_or_restore_peer_for_user(
                    user_id, username, existing_peer.get("tariff_key")
                )
                if not ok:
                    await message.reply(f"❌ {err}")
                refreshed_peer = db.get_peer_by_telegram_id(user_id)
                if refreshed_peer and refreshed_peer.get("peer_id"):
                    if not sync_clients_json_for_user(
                        user_id, username, refreshed_peer["peer_id"]
                    ):
                        logger.warning(
                            f"Failed to update clients.json for user {user_id}"
                        )
                return
        except Exception as e:
            logger.error(f"Error while getting config in /connect: {e}", exc_info=True)
            # Try to create a new peer on any error
            try:
                await message.reply("Попытка создать новый конфиг...")
                ok, err, _ = await create_or_restore_peer_for_user(
                    user_id, username, existing_peer.get("tariff_key")
                )
                if not ok:
                    await message.reply(f"❌ {err}")
            except Exception as e2:
                logger.error(f"Critical error while creating new peer: {e2}")
                await message.reply(
                    "❌ Ошибка при получении конфигурации. Попробуй позже или обратись в поддержку."
                )

    # New user: payment required
    payment_info = payment_manager.get_payment_info()
    await message.reply(
        f"💎 Для получения VPN конфигурации необходимо оплатить доступ!\n\n"
        f"Стоимость за {payment_info['period']}:\n"
        f"⭐ Telegram Stars: {payment_info['stars_price']} Stars\n"
        f"💳 Картой (Юmoney): {payment_info['rub_price']} руб.\n\n"
        f"После оплаты предоставим тебе конфигурацию и доступ на {payment_info['period']}."
    )

    # Send payment method selection
    await payment_manager.send_payment_selection(message.chat.id, user_id)


@dp.message(F.text == "/extend")
async def cmd_extend(message: types.Message):
    """Handle the /extend command (access extension)."""
    user_id = message.from_user.id
    username = message.from_user.username

    # Check if the user has an active peer
    existing_peer = db.get_peer_by_telegram_id(user_id)
    if not existing_peer:
        await message.reply(
            "❌ У тебя нет активного VPN доступа.\nИспользуй /connect для создания нового."
        )
        return

    # Check if current access is paid
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

    # Send payment method selection for extension
    await payment_manager.send_payment_selection(message.chat.id, user_id)


@dp.message(F.text == "/status")
async def cmd_status(message: types.Message):
    """Handle the /status command (remaining access time)."""
    user_id = message.from_user.id

    # Check if the user has an active peer
    existing_peer = db.get_peer_by_telegram_id(user_id)
    if not existing_peer:
        await message.reply(
            "❌ Нет активного VPN доступа.\nИспользуй /connect для создания нового."
        )
        return

    # Check if access is paid
    if existing_peer.get("payment_status") != "paid":
        await message.reply("❌ Доступ не оплачен.\nИспользуй /connect для оплаты.")
        return

    # Get expiration date
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

        # Calculate remaining time
        time_left = expire_date - now
        days_left = time_left.days
        hours_left = time_left.seconds // 3600
        minutes_left = (time_left.seconds % 3600) // 60

        # Build message
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
        logger.error(f"Failed to parse expiration date: {e}")
        await message.reply("❌ Ошибка при получении информации о доступе.")


@dp.message(F.text == "/buy")
async def cmd_buy(message: types.Message):
    """Handle the /buy command (payment method selection)."""
    user_id = message.from_user.id
    username = message.from_user.username

    # Send payment method selection
    await payment_manager.send_payment_selection(message.chat.id, user_id)


# Callback button handlers for payment method selection
@dp.callback_query(F.data.startswith("pay_stars_"))
async def handle_pay_stars_callback(callback_query: types.CallbackQuery):
    """Handle Telegram Stars payment selection."""
    # Extract tariff_key and user_id from callback_data (format: pay_stars_14_days_123456789)
    callback_parts = callback_query.data.split("_")
    tariff_key = (
        f"{callback_parts[2]}_{callback_parts[3]}"  # 14_days, 30_days or 90_days
    )
    user_id = int(callback_parts[-1])  # Last part is user_id
    username = callback_query.from_user.username

    # Ensure callback belongs to the correct user
    if callback_query.from_user.id != user_id:
        await safe_answer_callback(callback_query, "❌ Ошибка: неверный пользователь")
        return

    await safe_answer_callback(callback_query)

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


@dp.callback_query(F.data.startswith("pay_yookassa_"))
async def handle_pay_yookassa_callback(callback_query: types.CallbackQuery):
    """Handle YooKassa payment selection."""
    # Extract tariff_key and user_id from callback_data (format: pay_yookassa_14_days_123456789)
    callback_parts = callback_query.data.split("_")
    tariff_key = (
        f"{callback_parts[2]}_{callback_parts[3]}"  # 14_days, 30_days or 90_days
    )
    user_id = int(callback_parts[-1])  # Last part is user_id
    username = callback_query.from_user.username

    # Ensure callback belongs to the correct user
    if callback_query.from_user.id != user_id:
        await safe_answer_callback(callback_query, "❌ Ошибка: неверный пользователь")
        return

    await safe_answer_callback(callback_query)

    # Check if YooKassa is configured
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

    # Send invoice for YooKassa payment
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
    """Handle clicks on the disabled YooKassa button."""
    user_id = int(callback_query.data.replace("pay_yookassa_disabled_", ""))

    # Ensure callback belongs to the correct user
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


# Retry config creation after successful payment if the initial attempt failed
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
        logger.error(f"Error in retry_peer handler: {e}")
        await callback_query.message.edit_text(
            "❌ Ошибка при повторном создании. Попробуй ещё раз позже.",
            reply_markup=create_main_menu_keyboard(callback_query.from_user.id),
        )


# Payment handlers
@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query):
    """Handle pre-checkout validation."""
    await payment_manager.process_payment(pre_checkout_query)


@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    """Handle successful payment."""
    user_id = message.from_user.id
    username = message.from_user.username
    successful_payment = message.successful_payment

    # Get payload from successful payment
    payload = successful_payment.invoice_payload

    # Confirm payment
    (
        payment_confirmed,
        payment_type,
        amount_paid,
    ) = await payment_manager.confirm_payment(successful_payment)
    if not payment_confirmed:
        await message.reply("❌ Ошибка при обработке платежа.")
        return

    # Handle Stars payments only (YooKassa uses webhook)
    if not payload.startswith("vpn_access_stars_"):
        await message.reply("❌ Неизвестный тип платежа.")
        return

    # Extract tariff from payload
    payload_parts = payload.split("_")
    if len(payload_parts) >= 4:
        tariff_key = f"{payload_parts[3]}_{payload_parts[4]}"  # 14_days, 30_days
    else:
        await message.reply("❌ Ошибка в данных платежа.")
        return

    payment_method = "stars"

    # Log payment and update payment status
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
        logger.warning(f"Failed to record Stars payment in DB: {e}")

    # Update payment status for the user
    db.update_payment_status(user_id, "paid", amount_paid, payment_method, tariff_key)

    # Determine access period based on tariff
    tariff_data = payment_manager.tariffs.get(tariff_key, {})
    access_days = tariff_data.get("days", 30)

    # Check if the user already has a peer
    existing_peer = db.get_peer_by_telegram_id(user_id)

    if existing_peer:
        # Extend access for the existing peer
        success, new_expire_date = db.extend_access(user_id, access_days)

        if not success:
            await message.reply(
                "❌ Ошибка при продлении доступа. Обратитесь в поддержку."
            )
            return

        # Check peer existence in WGDashboard
        peer_exists = None
        try:
            peer_exists = wg_api.check_peer_exists(existing_peer["peer_id"])
        except Exception as e:
            logger.error(f"Error checking peer existence in WGDashboard: {e}")

        allow_result = None
        try:
            allow_result = wg_api.allow_access_peer(existing_peer["peer_id"])
            if allow_result and allow_result.get("status"):
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
                    existing_peer["job_id"], existing_peer["peer_id"], new_expire_date
                )

                if job_update_result and job_update_result.get("status"):
                    logger.info(
                        f"Job updated for user {user_id}, new date: {new_expire_date}"
                    )
                else:
                    logger.error(
                        f"Error updating job for user {user_id}: {job_update_result}"
                    )

            except Exception as e:
                logger.error(f"Error updating job in WGDashboard: {e}")

            # Do not resend config on extension
            await message.reply(
                f"✅ Платеж успешно обработан!\n"
                f"🎉 Продлили тебе доступ на {access_days} дней!\n"
                f"💳 Способ оплаты: ⭐ Telegram Stars\n\n"
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
                f"Peer for user {user_id} not found in WGDashboard, creating new one"
            )
            await message.reply("🔄 Восстанавливаю VPN доступ...")
            ok, err, _ = await create_or_restore_peer_for_user(user_id, username, tariff_key)
            if not ok:
                await message.reply(
                    "❌ Ошибка при восстановлении VPN доступа. Обратитесь в поддержку."
                )
                logger.error(
                    f"Failed to recreate peer for user {user_id}: {err}"
                )
                return

            await message.reply(
                f"✅ Платеж успешно обработан!\n"
                f"🎉 Продлили тебе доступ на {access_days} дней!\n"
                f"💳 Способ оплаты: ⭐ Telegram Stars\n\n"
                f"Доступ восстановлен, используй /connect для получения актуального конфига."
            )
            refreshed_peer = db.get_peer_by_telegram_id(user_id)
            if refreshed_peer and refreshed_peer.get("expire_date"):
                sync_bound_custom_peers_for_user(
                    user_id=user_id,
                    expire_date=refreshed_peer["expire_date"],
                    allow_access=True,
                    exclude_peer_id=refreshed_peer.get("peer_id"),
                )
        else:
            await message.reply(
                "❌ Не удалось проверить статус VPN на сервере. Попробуйте еще раз через минуту или обратитесь в поддержку."
            )
            logger.error(
                f"Peer status for user {user_id} is unknown, recreation canceled to avoid duplicate"
            )
            return

        # Do not send extra message after extension
    else:
        # Create a new peer for the user
        try:
            await message.reply("🔄 Создаю VPN доступ...")
            ok, err, _ = await create_or_restore_peer_for_user(
                user_id, username, tariff_key
            )
            if not ok:
                # Offer retry and support
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
            refreshed_peer = db.get_peer_by_telegram_id(user_id)
            if refreshed_peer and refreshed_peer.get("expire_date"):
                sync_bound_custom_peers_for_user(
                    user_id=user_id,
                    expire_date=refreshed_peer["expire_date"],
                    allow_access=True,
                    exclude_peer_id=refreshed_peer.get("peer_id"),
                )
        except Exception as e:
            logger.error(f"Error creating peer after payment: {e}")
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


# Unknown command handler
@dp.message()
async def handle_unknown(message: types.Message):
    """Handle unknown messages."""
    user_id = message.from_user.id

    # Check if the user has paid access
    existing_peer = db.get_peer_by_telegram_id(user_id)
    has_paid_access = existing_peer and existing_peer.get("payment_status") == "paid"

    # Show the main menu for unknown commands
    await message.answer(
        "❓ Неизвестная команда.\n\nИспользуй кнопки ниже или команды:\n/start - главное меню\n/buy - купить доступ\n/connect - получить конфиг",
        reply_markup=create_main_menu_keyboard(user_id),
    )


# Periodic check for expired peers and notifications
async def check_expired_peers():
    """Check expired peers and notify users."""
    while True:
        try:
            # Check expired peers
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
                        f"Failed to send expiration notice to user {peer['telegram_user_id']}"
                    )

            # Check users for 1-day reminder
            users_for_notification = db.get_users_for_notification(1)

            for user in users_for_notification:
                try:
                    payment_info = payment_manager.get_payment_info()
                    tariffs = payment_info["tariffs"]

                    # Build text with available tariffs
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

                    # Mark notification as sent
                    db.mark_notification_sent(user["telegram_user_id"])

                except TelegramAPIError:
                    logger.warning(
                        f"Failed to send notification to user {user['telegram_user_id']}"
                    )

            # Check every 30 minutes
            await asyncio.sleep(30 * 60)

        except Exception as e:
            logger.error(f"Error while checking expired peers: {e}")
            await asyncio.sleep(60)  # Wait a minute on error


async def main():
    """Main bot entry point."""
    try:
        # Start background checks for expired peers and notifications
        asyncio.create_task(check_expired_peers())

        # Start the bot
        logger.info("Starting WireGuard bot...")
        await dp.start_polling(bot, skip_updates=True)

    except Exception as e:
        logger.error(f"Critical error: {e}")


if __name__ == "__main__":
    asyncio.run(main())
