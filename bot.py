import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import (
    CASCADE_RETRY_INTERVAL_SECONDS,
    get_admin_telegram_ids,
    SUPPORT_URL,
    TELEGRAM_BOT_TOKEN,
)
from cascade_api import CascadeCapacityError, CascadeNotFound
from logging_setup import configure_logging
from payment import PaymentManager
from services import (
    cascade_router,
    close_shared_services,
    db,
    set_runtime_ready,
    yookassa_client,
)
from utils import (
    generate_peer_name,
    parse_date_flexible,
    format_date_for_user,
)

configure_logging()
logger = logging.getLogger(__name__)

# Bot and dispatcher initialization
bot = Bot(token=TELEGRAM_BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

class OperationLoggingMiddleware:
    """Log every incoming bot operation for docker/file logs."""

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: types.TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, types.Message):
            user_id = event.from_user.id if event.from_user else "unknown"
            chat_id = event.chat.id if event.chat else "unknown"
            text = (event.text or event.caption or "").replace("\n", " ").strip()
            logger.info(
                f"Incoming message operation: user_id={user_id}, chat_id={chat_id}, text={text[:200] or '<non-text>'}"
            )
        elif isinstance(event, types.CallbackQuery):
            user_id = event.from_user.id if event.from_user else "unknown"
            message = event.message
            chat_id = message.chat.id if message and message.chat else "unknown"
            logger.info(
                f"Incoming callback operation: user_id={user_id}, chat_id={chat_id}, data={event.data}"
            )

        return await handler(event, data)


dp.message.outer_middleware(OperationLoggingMiddleware())
dp.callback_query.outer_middleware(OperationLoggingMiddleware())

payment_manager = PaymentManager(
    bot,
    yookassa_client=yookassa_client,
    db=db,
    cascade_router=cascade_router,
)
_last_start_sent_at: dict[int, float] = {}
START_DEBOUNCE_SECONDS = 5.0
ADMIN_CLIENTS_PAGE_SIZE = 8
_awaiting_admin_message: dict[int, dict[str, Any]] = {}
_pending_admin_message: dict[int, dict[str, Any]] = {}
_admin_client_selection_pages: dict[int, int] = {}


def is_admin(user_id: int) -> bool:
    """Check whether a Telegram user is configured as an admin."""
    return user_id in get_admin_telegram_ids()


def get_broadcast_recipients() -> list[int]:
    """Return all unique client Telegram IDs for admin broadcasts."""
    return db.get_client_telegram_ids()


def get_admin_client_options() -> list[dict[str, Any]]:
    """Return clients available for admin direct messages."""
    return db.get_admin_client_options()


def format_admin_client_label(client: dict[str, Any]) -> str:
    """Format a client button label for admin selection."""
    username = client.get("username") or "без username"
    username_label = f"@{username}" if username != "без username" else username
    return f"{client['telegramId']} | {username_label}"


def build_admin_recipient_short_labels(user_ids: list[int]) -> dict[int, str]:
    """Build compact recipient labels for admin send reports."""
    labels = {user_id: str(user_id) for user_id in user_ids}
    for client in get_admin_client_options():
        telegram_id = client["telegramId"]
        username = client.get("username") or ""
        if telegram_id in labels and username:
            labels[telegram_id] = f"@{username}"
    return labels


def format_admin_payment_notification(
    title: str,
    user_id: int,
    username: str | None,
    tariff_name: str,
    amount: str,
    payment_method: str,
    expire_date: str | None = None,
) -> str:
    user_line = f"@{username}" if username else "без username"
    text = (
        f"{title}\n\n"
        f"👤 Пользователь: {user_line}\n"
        f"🆔 Telegram ID: {user_id}\n"
        f"📋 Тариф: {tariff_name}\n"
        f"💰 Стоимость: {amount}\n"
        f"💳 Способ оплаты: {payment_method}"
    )
    if expire_date:
        text += f"\n📅 Новый срок: {format_date_for_user(expire_date)}"
    return text


async def notify_admins(text: str) -> None:
    """Send a best-effort notification to configured admins."""
    for admin_id in get_admin_telegram_ids():
        try:
            await bot.send_message(admin_id, text)
        except TelegramAPIError as e:
            logger.warning(f"Failed to send admin notification to {admin_id}: {e}")
        except Exception as e:
            logger.warning(f"Unexpected admin notification error for {admin_id}: {e}")


async def send_config_file(
    chat_id: int,
    config_content: bytes | str | None,
    caption: str | None = "📁 Твой файл конфигурации",
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    if not config_content:
        return False

    try:
        logger.info(f"Sending config file to chat {chat_id}")
        config_bytes = (
            config_content
            if isinstance(config_content, (bytes, bytearray))
            else config_content.encode("utf-8")
        )
        await bot.send_document(
            chat_id=chat_id,
            document=types.BufferedInputFile(file=config_bytes, filename="nikonVPN.conf"),
            caption=caption,
            reply_markup=reply_markup,
        )
        logger.info(f"Config file sent to chat {chat_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to send config file to chat {chat_id}: {e}", exc_info=True)
        return False


async def send_config_confirmation(chat_id: int) -> None:
    """Send delayed confirmation after a config document is delivered."""
    await asyncio.sleep(3)
    logger.info(f"Sending delayed config confirmation to chat {chat_id}")
    await bot.send_message(
        chat_id=chat_id,
        text="✅ Прислал тебе конфиг файл.\nДобавь его в приложение AmneziWG",
        reply_markup=create_home_keyboard(),
    )


async def send_config_with_confirmation(
    chat_id: int,
    config_content: bytes | str | None,
    source_message: types.Message | None = None,
    caption: str | None = None,
) -> bool:
    """Send config file first, then send a delayed confirmation message."""
    sent = await send_config_file(chat_id, config_content, caption=caption)
    if not sent:
        return False

    if source_message is not None:
        try:
            await source_message.delete()
        except Exception as e:
            logger.warning(f"Failed to delete temporary config status message: {e}")

    await send_config_confirmation(chat_id)
    return True


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

        # Peer name uses the Telegram username when available; otherwise it falls
        # back to the Telegram ID.
        peer_name = generate_peer_name(username, user_id)

        try:
            _, config_content = await cascade_router.create_user_peer(
                user_id=user_id,
                username=username,
                peer_name=peer_name,
                expire_date=target_expire_date,
            )
            return True, "", config_content
        except CascadeCapacityError:
            return False, "Все VPN серверы временно заполнены", None
        except Exception as e:
            task_id = db.add_provisioning_task(
                user_id,
                "create_peer",
                {
                    "username": username or "",
                    "peer_name": peer_name,
                    "expire_date": target_expire_date,
                    "tariff_key": tariff_key,
                },
                str(e),
            )
            logger.error(
                "Cascade provisioning failed for user %s; queued task %s: %s",
                user_id,
                task_id,
                e,
            )
            return False, "Доступ оплачен и будет создан автоматически после восстановления сервера", None
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


async def safe_edit_callback_message(
    message: types.Message, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> bool:
    """Edit a callback source message, falling back safely for media messages."""
    try:
        await message.edit_text(text, reply_markup=reply_markup)
        return True
    except TelegramBadRequest as e:
        error_text = str(e).lower()
        if "message is not modified" in error_text:
            logger.debug("Skip edit_text: message is not modified")
            return False
        if "there is no text in the message to edit" not in error_text:
            if "message can't be edited" not in error_text and "message to edit not found" not in error_text:
                raise

    try:
        await message.edit_caption(caption=text, reply_markup=reply_markup)
        return True
    except TelegramBadRequest as e:
        error_text = str(e).lower()
        if "message is not modified" in error_text:
            logger.debug("Skip edit_caption: message is not modified")
            return False
        logger.info(f"Falling back to send_message after edit failure: {e}")

    await bot.send_message(
        message.chat.id,
        text,
        reply_markup=reply_markup,
    )
    return True


async def show_menu_from_callback(
    callback_query: types.CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup
) -> None:
    """Show a menu from any callback source message without crashing on media messages."""
    message = callback_query.message
    if message is None:
        await bot.send_message(
            callback_query.from_user.id,
            text,
            reply_markup=reply_markup,
        )
        return

    await safe_edit_callback_message(message, text, reply_markup=reply_markup)


def create_home_keyboard() -> InlineKeyboardMarkup:
    """Create a compact keyboard with a main menu button."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="На главную", callback_data="main")]
        ]
    )


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

    if has_active_access:
        inline_keyboard = [
            [InlineKeyboardButton(text="✅ Доступ приобретен", callback_data="already_paid")],
            [InlineKeyboardButton(text="📅 Статус доступа", callback_data="status")],
        ]
    else:
        inline_keyboard = [
            [InlineKeyboardButton(text="💵 Оплатить доступ", callback_data="pay")],
        ]
    if has_active_access:
        inline_keyboard.append(
            [InlineKeyboardButton(text="💵 Продлить доступ", callback_data="extend")]
        )
        inline_keyboard.append(
            [InlineKeyboardButton(text="💾 Получить конфиг", callback_data="get_config")]
        )
    inline_keyboard.extend(
        [
            [
                InlineKeyboardButton(text="📖 Инструкция", callback_data="guide"),
                InlineKeyboardButton(text="❓ Есть вопрос?", url=SUPPORT_URL),
            ],
        ]
    )
    if is_admin(user_id):
        inline_keyboard.append(
            [InlineKeyboardButton(text="📣 Рассылка", callback_data="admin_broadcast")]
        )
    keyboard = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
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


def create_back_to_menu_keyboard() -> InlineKeyboardMarkup:
    """Create a single-button keyboard to return to the main menu."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Вернуться в меню", callback_data="main")]
        ]
    )


def create_admin_broadcast_menu_keyboard() -> InlineKeyboardMarkup:
    """Create the admin broadcast menu keyboard."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📣 Рассылка всем",
                    callback_data="admin_broadcast_all",
                )
            ],
            [
                InlineKeyboardButton(
                    text="👤 Сообщение клиенту",
                    callback_data="admin_broadcast_client_menu",
                )
            ],
            [InlineKeyboardButton(text="На главную", callback_data="main")],
        ]
    )


def create_admin_clients_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    """Create a paginated client selection keyboard for admins."""
    clients = get_admin_client_options()
    total_pages = max(
        1,
        (len(clients) + ADMIN_CLIENTS_PAGE_SIZE - 1) // ADMIN_CLIENTS_PAGE_SIZE,
    )
    page = max(0, min(page, total_pages - 1))
    start = page * ADMIN_CLIENTS_PAGE_SIZE
    page_clients = clients[start : start + ADMIN_CLIENTS_PAGE_SIZE]

    rows: list[list[InlineKeyboardButton]] = []
    for client in page_clients:
        rows.append(
            [
                InlineKeyboardButton(
                    text=format_admin_client_label(client),
                    callback_data=f"admin_message_client_{client['telegramId']}",
                )
            ]
        )

    navigation_row: list[InlineKeyboardButton] = []
    if page > 0:
        navigation_row.append(
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"admin_clients_page_{page - 1}",
            )
        )
    if page < total_pages - 1:
        navigation_row.append(
            InlineKeyboardButton(
                text="Далее ➡️",
                callback_data=f"admin_clients_page_{page + 1}",
            )
        )
    if navigation_row:
        rows.append(navigation_row)

    rows.append(
        [
            InlineKeyboardButton(
                text="🔙 Назад",
                callback_data="admin_broadcast",
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="На главную", callback_data="main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def create_admin_message_confirm_keyboard() -> InlineKeyboardMarkup:
    """Create the admin message confirmation keyboard."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Отправить", callback_data="admin_broadcast_confirm"
                ),
                InlineKeyboardButton(
                    text="❌ Отмена", callback_data="admin_broadcast_cancel"
                ),
            ],
        ]
    )


def create_admin_message_cancel_keyboard() -> InlineKeyboardMarkup:
    """Create a cancel keyboard for admin message capture."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="admin_message_cancel",
                )
            ]
        ]
    )


# Command handlers
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    """Handle the /start command."""
    user_id = message.from_user.id
    now_monotonic = time.monotonic()
    last_start = _last_start_sent_at.get(user_id, 0.0)
    if now_monotonic - last_start < START_DEBOUNCE_SECONDS:
        logger.info(
            f"Skipping duplicate /start for user {user_id} within {START_DEBOUNCE_SECONDS}s window"
        )
        return
    _last_start_sent_at[user_id] = now_monotonic

    welcome_text = """
👋🏻 Привет! Здесь ты можешь подключиться к быстрому и безопасному VPN.

Чтобы начать пользоваться нашим VPN, скачай клиент AmneziaWG из своего магазина приложений.
В инструкции есть ссылки на скачивание приложения и описан процесс подключения.
    """

    await message.answer(welcome_text, reply_markup=create_main_menu_keyboard(user_id))


# Inline button handlers
@dp.callback_query(F.data == "pay")
async def handle_pay_callback(callback_query: types.CallbackQuery):
    """Handle the 'Buy access' button."""
    user_id = callback_query.from_user.id

    await safe_answer_callback(callback_query)

    payment_text, keyboard = await payment_manager.get_payment_selection_view(user_id)
    await safe_edit_callback_message(
        callback_query.message,
        payment_text,
        reply_markup=keyboard,
    )


@dp.callback_query(F.data.startswith("tariff_label_"))
async def handle_tariff_label_callback(callback_query: types.CallbackQuery):
    """Ignore taps on tariff label rows."""
    await safe_answer_callback(callback_query)


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

        expired_text = f"""
⚠️ Твой доступ к VPN истек!

📅 Дата истечения: {expire_date_formatted}

⚠️ Для продолжения пользования сервисом, необходимо продлить доступ.

Выбери действие с помощью кнопок ниже:
        """
        # Update message with new keyboard (button switches to "Buy access")
        await show_menu_from_callback(
            callback_query,
            expired_text,
            create_main_menu_keyboard(user_id),
        )
        return

    await safe_answer_callback(callback_query, "✅ У тебя уже есть доступ!")

    already_paid_text = """
✅ У тебя уже есть активный доступ к VPN!

Используй кнопки ниже для управления доступом:
    """

    # Update message with the current keyboard
    await show_menu_from_callback(
        callback_query,
        already_paid_text,
        create_main_menu_keyboard(user_id),
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

⚠️ Для продолжения пользования сервисом, необходимо продлить доступ.

Выбери действие с помощью кнопок ниже:
                """
            else:
                # Access not paid
                error_text = """
❌ У тебя нет активного доступа.

💎 Чтобы получить конфиг, нужно оплатить доступ.

Выбери действие с помощью кнопок ниже:
                """
            await safe_edit_callback_message(
                callback_query.message,
                error_text,
                reply_markup=create_main_menu_keyboard(user_id),
            )
            return

        # User has active access: try to send config or restore if missing
        try:
            await safe_edit_callback_message(
                callback_query.message,
                "⏳ Скачиваю конфигурацию...",
                reply_markup=create_back_to_menu_keyboard(),
            )

            try:
                peer_config = await cascade_router.get_primary_config(user_id)
            except CascadeNotFound:
                logger.warning("Primary Cascade peer is explicitly missing for user %s", user_id)
                ok, err, new_config = await create_or_restore_peer_for_user(
                    user_id, username, existing_peer.get("tariff_key")
                )
                if not ok:
                    await safe_edit_callback_message(
                        callback_query.message,
                        f"❌ {err}\n\nВыбери действие с помощью кнопок ниже:",
                        reply_markup=create_main_menu_keyboard(user_id),
                    )
                    return
                
                # Use the received config
                peer_config = new_config

            sent = await send_config_with_confirmation(
                callback_query.message.chat.id,
                peer_config,
                source_message=callback_query.message,
                caption=None,
            )
            if not sent:
                await safe_edit_callback_message(
                    callback_query.message,
                    "❌ Не удалось отправить конфигурацию.\n\nИспользуй кнопку ниже, чтобы вернуться в меню:",
                    reply_markup=create_back_to_menu_keyboard(),
                )
        except Exception as e:
            logger.error(
                f"Error while fetching/restoring configuration: {e}", exc_info=True
            )
            await safe_edit_callback_message(
                callback_query.message,
                "❌ Ошибка при получении конфигурации. Попробуй позже или обратись в поддержку.\n\nИспользуй кнопку ниже, чтобы вернуться в меню:",
                reply_markup=create_back_to_menu_keyboard(),
            )
    else:
        # User has no peer
        error_text = """
❌ У тебя нет VPN доступа.

💎 Чтобы получить конфиг, нужно оплатить доступ.

Выбери действие с помощью кнопок ниже:
        """
        await show_menu_from_callback(
            callback_query,
            error_text,
            create_main_menu_keyboard(user_id),
        )


@dp.callback_query(F.data == "extend")
async def handle_extend_callback(callback_query: types.CallbackQuery):
    """Handle the 'Extend access' button."""
    await safe_answer_callback(callback_query)

    user_id = callback_query.from_user.id
    # Check if the user has an active peer
    existing_peer = db.get_peer_by_telegram_id(user_id)
    if not existing_peer:
        error_text = """
❌ У тебя нет активного VPN доступа.

💎 Сначала необходимо купить доступ.

Выбери действие с помощью кнопок ниже:
        """
        await show_menu_from_callback(
            callback_query,
            error_text,
            create_main_menu_keyboard(user_id),
        )
        return

    # Check payment status
    if existing_peer.get("payment_status") != "paid":
        error_text = """
❌ У тебя нет оплаченного доступа.

💎 Сначала необходимо оплатить доступ.

Выбери действие с помощью кнопок ниже:
        """
        await safe_edit_callback_message(
            callback_query.message,
            error_text,
            reply_markup=create_main_menu_keyboard(user_id),
        )
        return

    payment_text, keyboard = await payment_manager.get_payment_selection_view(user_id)
    await safe_edit_callback_message(
        callback_query.message,
        payment_text,
        reply_markup=keyboard,
    )


@dp.callback_query(F.data == "status")
async def handle_status_callback(callback_query: types.CallbackQuery):
    """Handle the 'Access status' button."""
    await safe_answer_callback(callback_query)

    user_id = callback_query.from_user.id
    # Check if the user has an active peer
    existing_peer = db.get_peer_by_telegram_id(user_id)
    if not existing_peer:
        error_text = """
❌ У тебя нет активного VPN доступа.

💎 Для получения доступа необходимо его оплатить.

Выбери действие с помощью кнопок ниже:
        """
        await show_menu_from_callback(
            callback_query,
            error_text,
            create_main_menu_keyboard(user_id),
        )
        return

    # Get peer info from the database
    try:
        expire_date_str = existing_peer.get("expire_date", "Неизвестно")
        
        # Format dates for display
        expire_date_formatted = format_date_for_user(expire_date_str) if expire_date_str != "Неизвестно" else "Неизвестно"
        connected_devices = db.get_peer_count(user_id)
        devices_line = (
            f"\nПодключено устройств: {connected_devices}"
            if connected_devices
            else ""
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

⚠️ Для продолжения пользования сервисом, необходимо продлить доступ.

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
                        '\n\n⚠️ Доступ к сервису скоро истекает! Нажми "Продлить доступ" для продления.'
                    )

                status_text += "\n\nВыбери действие с помощью кнопок ниже:"
            except (ValueError, TypeError):
                status_text = f"""
📊 Статус доступа:

⏰ Доступ закончится: {expire_date_formatted}{devices_line}

Выбери действие с помощью кнопок ниже:
                """

        await show_menu_from_callback(
            callback_query,
            status_text,
            create_main_menu_keyboard(user_id),
        )

    except Exception as e:
        logger.error(f"Failed to fetch peer info: {e}")
        error_text = """
❌ Ошибка при получении информации о пире.

Выбери действие с помощью кнопок ниже:
        """
        await show_menu_from_callback(
            callback_query,
            error_text,
            create_main_menu_keyboard(user_id),
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
   • Нажмите "💾 Получить конфиг"
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

    welcome_text = """
👋🏻 Привет! Здесь ты можешь подключиться к быстрому и безопасному VPN.

Чтобы начать пользоваться нашим VPN, скачай клиент AmneziaWG из своего магазина приложений.
В инструкции есть ссылки на скачивание приложения и описан процесс подключения.
    """

    await show_menu_from_callback(
        callback_query,
        welcome_text,
        create_main_menu_keyboard(user_id),
    )


async def send_admin_broadcast_menu(chat_id: int, admin_id: int) -> None:
    """Send the admin broadcast menu."""
    if not is_admin(admin_id):
        logger.warning(f"Rejected admin menu access from non-admin user {admin_id}")
        await bot.send_message(chat_id, "❌ Недостаточно прав.")
        return

    await bot.send_message(
        chat_id,
        "📣 Админ-рассылка\n\nВыбери действие:",
        reply_markup=create_admin_broadcast_menu_keyboard(),
    )


@dp.message(Command("admin_broadcast"))
async def cmd_admin_broadcast(message: types.Message):
    """Show the admin broadcast menu from a command."""
    await send_admin_broadcast_menu(message.chat.id, message.from_user.id)


@dp.callback_query(F.data == "admin_broadcast")
async def handle_admin_broadcast_callback(callback_query: types.CallbackQuery):
    """Show the admin broadcast menu from the main menu."""
    await safe_answer_callback(callback_query)
    admin_id = callback_query.from_user.id
    if not is_admin(admin_id):
        logger.warning(f"Rejected admin menu access from non-admin user {admin_id}")
        await safe_answer_callback(callback_query, "❌ Недостаточно прав.")
        return

    await show_menu_from_callback(
        callback_query,
        "📣 Админ-рассылка\n\nВыбери действие:",
        create_admin_broadcast_menu_keyboard(),
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message):
    """Cancel the current admin action."""
    user_id = message.from_user.id
    if user_id in _awaiting_admin_message or user_id in _pending_admin_message:
        _awaiting_admin_message.pop(user_id, None)
        _pending_admin_message.pop(user_id, None)
        _admin_client_selection_pages.pop(user_id, None)
        await message.answer(
            "Рассылка отменена.",
            reply_markup=create_main_menu_keyboard(user_id),
        )
        return

    await message.answer("Нет активного действия для отмены.")


async def show_admin_previous_step(
    callback_query: types.CallbackQuery,
    flow: dict[str, Any] | None,
) -> None:
    """Return the admin UI to the previous step for the current flow."""
    admin_id = callback_query.from_user.id
    if flow and flow.get("mode") == "client":
        page = int(flow.get("return_page") or 0)
        _admin_client_selection_pages[admin_id] = page
        await show_menu_from_callback(
            callback_query,
            "👤 Выбери клиента для отправки сообщения:",
            create_admin_clients_keyboard(page),
        )
        return

    await show_menu_from_callback(
        callback_query,
        "📣 Админ-рассылка\n\nВыбери действие:",
        create_admin_broadcast_menu_keyboard(),
    )


async def edit_admin_service_message(
    flow: dict[str, Any],
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Edit the stored admin service message when possible."""
    chat_id = flow.get("service_chat_id")
    message_id = flow.get("service_message_id")
    if not chat_id or not message_id:
        return False

    try:
        await bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
        )
        return True
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return True
        logger.warning(f"Failed to edit admin service message: {e}")
        return False
    except TelegramAPIError as e:
        logger.warning(f"Failed to edit admin service message: {e}")
        return False


async def start_admin_message_capture(
    callback_query: types.CallbackQuery,
    mode: str,
    recipient_id: int | None = None,
    recipient_label: str | None = None,
    return_page: int = 0,
) -> None:
    """Ask an admin to send the message that should be copied."""
    admin_id = callback_query.from_user.id
    if not is_admin(admin_id):
        logger.warning(
            f"Rejected admin message capture from non-admin user {admin_id}"
        )
        await safe_answer_callback(callback_query, "❌ Недостаточно прав.")
        return

    _awaiting_admin_message[admin_id] = {
        "mode": mode,
        "recipient_id": recipient_id,
        "recipient_label": recipient_label,
        "return_page": return_page,
        "service_chat_id": callback_query.message.chat.id,
        "service_message_id": callback_query.message.message_id,
    }
    _pending_admin_message.pop(admin_id, None)

    if mode == "all":
        recipient_text = f"Получателей: {len(get_broadcast_recipients())}"
    else:
        recipient_text = f"Получатель: {recipient_label or recipient_id}"

    await show_menu_from_callback(
        callback_query,
        "Отправь сообщение для пересылки клиентам.\n\n"
        f"{recipient_text}\n\n"
        "Можно отправить текст, фото, видео или файл с подписью.",
        create_admin_message_cancel_keyboard(),
    )


@dp.callback_query(F.data == "admin_broadcast_all")
async def handle_admin_broadcast_all_callback(callback_query: types.CallbackQuery):
    """Start broadcast-to-all message capture."""
    await safe_answer_callback(callback_query)
    admin_id = callback_query.from_user.id
    if not is_admin(admin_id):
        logger.warning(f"Rejected broadcast-all access from non-admin user {admin_id}")
        await safe_answer_callback(callback_query, "❌ Недостаточно прав.")
        return

    await start_admin_message_capture(callback_query, mode="all")


@dp.callback_query(F.data == "admin_broadcast_client_menu")
async def handle_admin_broadcast_client_menu(callback_query: types.CallbackQuery):
    """Show clients for a direct admin message."""
    await safe_answer_callback(callback_query)
    admin_id = callback_query.from_user.id
    if not is_admin(admin_id):
        logger.warning(f"Rejected client selection from non-admin user {admin_id}")
        await safe_answer_callback(callback_query, "❌ Недостаточно прав.")
        return

    clients = get_admin_client_options()
    if not clients:
        await show_menu_from_callback(
            callback_query,
            "В базе данных пока нет клиентов.",
            create_admin_broadcast_menu_keyboard(),
        )
        return

    _admin_client_selection_pages[admin_id] = 0
    await show_menu_from_callback(
        callback_query,
        "👤 Выбери клиента для отправки сообщения:",
        create_admin_clients_keyboard(0),
    )


@dp.callback_query(F.data.startswith("admin_clients_page_"))
async def handle_admin_clients_page(callback_query: types.CallbackQuery):
    """Show a requested clients page for admin direct messages."""
    await safe_answer_callback(callback_query)
    admin_id = callback_query.from_user.id
    if not is_admin(admin_id):
        logger.warning(f"Rejected client page access from non-admin user {admin_id}")
        await safe_answer_callback(callback_query, "❌ Недостаточно прав.")
        return

    try:
        page = int(callback_query.data.replace("admin_clients_page_", ""))
    except ValueError:
        page = 0

    _admin_client_selection_pages[admin_id] = page
    await show_menu_from_callback(
        callback_query,
        "👤 Выбери клиента для отправки сообщения:",
        create_admin_clients_keyboard(page),
    )


@dp.callback_query(F.data.startswith("admin_message_client_"))
async def handle_admin_message_client(callback_query: types.CallbackQuery):
    """Start direct message capture for a selected client."""
    await safe_answer_callback(callback_query)
    admin_id = callback_query.from_user.id
    if not is_admin(admin_id):
        logger.warning(
            f"Rejected direct message access from non-admin user {admin_id}"
        )
        await safe_answer_callback(callback_query, "❌ Недостаточно прав.")
        return

    try:
        recipient_id = int(callback_query.data.replace("admin_message_client_", ""))
    except ValueError:
        await safe_answer_callback(callback_query, "❌ Некорректный Telegram ID")
        return

    recipient_label = str(recipient_id)
    for client in get_admin_client_options():
        if client["telegramId"] == recipient_id:
            recipient_label = format_admin_client_label(client)
            break

    await start_admin_message_capture(
        callback_query,
        mode="client",
        recipient_id=recipient_id,
        recipient_label=recipient_label,
        return_page=_admin_client_selection_pages.get(admin_id, 0),
    )


def is_supported_admin_message(message: types.Message) -> bool:
    """Check if the message can be copied to users."""
    return any(
        [
            message.text,
            message.photo,
            message.document,
            message.video,
            message.animation,
            message.audio,
            message.voice,
            message.video_note,
        ]
    )


def get_admin_message_text(message: types.Message) -> str:
    """Return a short text representation for an admin message."""
    text = (message.text or message.caption or "").strip()
    if text:
        return text
    return "[медиа/файл без текста]"


@dp.message(
    lambda message: message.from_user
    and message.from_user.id in _awaiting_admin_message
)
async def handle_admin_message_content(message: types.Message):
    """Save a message reference for admin broadcast/direct sending."""
    admin_id = message.from_user.id
    flow = _awaiting_admin_message.get(admin_id)
    if not flow or not is_admin(admin_id):
        _awaiting_admin_message.pop(admin_id, None)
        return

    if not is_supported_admin_message(message):
        error_text = (
            "Этот тип сообщения не поддерживается.\n"
            "Отправь текст, фото, видео или файл с подписью."
        )
        edited = await edit_admin_service_message(
            flow,
            error_text,
            create_admin_message_cancel_keyboard(),
        )
        if not edited:
            await message.answer(error_text)
        return

    _awaiting_admin_message.pop(admin_id, None)
    pending = {
        **flow,
        "source_chat_id": message.chat.id,
        "source_message_id": message.message_id,
        "source_text": get_admin_message_text(message),
    }
    _pending_admin_message[admin_id] = pending

    if flow["mode"] == "all":
        target_text = f"Получателей: {len(get_broadcast_recipients())}"
    else:
        target_text = (
            f"Получатель: {flow.get('recipient_label') or flow.get('recipient_id')}"
        )

    edited = await edit_admin_service_message(
        flow,
        f"{target_text}\n\nОтправить это сообщение?",
        create_admin_message_confirm_keyboard(),
    )
    if not edited:
        await message.answer(
            f"{target_text}\n\nОтправить это сообщение?",
            reply_markup=create_admin_message_confirm_keyboard(),
        )


@dp.callback_query(F.data == "admin_broadcast_cancel")
async def handle_admin_broadcast_cancel(callback_query: types.CallbackQuery):
    """Cancel a pending admin message."""
    admin_id = callback_query.from_user.id
    if not is_admin(admin_id):
        logger.warning(f"Rejected admin cancel from non-admin user {admin_id}")
        await safe_answer_callback(callback_query, "❌ Недостаточно прав.")
        return

    await safe_answer_callback(callback_query)
    flow = _pending_admin_message.pop(admin_id, None)
    if flow is None:
        flow = _awaiting_admin_message.pop(admin_id, None)
    elif flow.get("source_chat_id") and flow.get("source_message_id"):
        try:
            await bot.delete_message(
                chat_id=flow["source_chat_id"],
                message_id=flow["source_message_id"],
            )
        except TelegramAPIError as e:
            logger.warning(f"Failed to delete canceled admin source message: {e}")
    await show_admin_previous_step(callback_query, flow)


@dp.callback_query(F.data == "admin_message_cancel")
async def handle_admin_message_capture_cancel(callback_query: types.CallbackQuery):
    """Cancel admin message capture and return to the previous step."""
    admin_id = callback_query.from_user.id
    if not is_admin(admin_id):
        logger.warning(f"Rejected admin capture cancel from non-admin user {admin_id}")
        await safe_answer_callback(callback_query, "❌ Недостаточно прав.")
        return

    await safe_answer_callback(callback_query)
    flow = _awaiting_admin_message.pop(admin_id, None)
    if flow is None:
        flow = _pending_admin_message.pop(admin_id, None)
        if flow and flow.get("source_chat_id") and flow.get("source_message_id"):
            try:
                await bot.delete_message(
                    chat_id=flow["source_chat_id"],
                    message_id=flow["source_message_id"],
                )
            except TelegramAPIError as e:
                logger.warning(f"Failed to delete canceled admin source message: {e}")
    await show_admin_previous_step(callback_query, flow)


@dp.callback_query(F.data == "admin_broadcast_confirm")
async def handle_admin_broadcast_confirm(callback_query: types.CallbackQuery):
    """Copy the pending admin message to selected recipients."""
    admin_id = callback_query.from_user.id
    if not is_admin(admin_id):
        logger.warning(f"Rejected broadcast confirm from non-admin user {admin_id}")
        await safe_answer_callback(callback_query, "❌ Недостаточно прав.")
        return
    await safe_answer_callback(callback_query)

    pending = _pending_admin_message.pop(admin_id, None)
    _awaiting_admin_message.pop(admin_id, None)
    if not pending:
        await show_menu_from_callback(
            callback_query,
            "Нет подготовленного сообщения.",
            create_main_menu_keyboard(admin_id),
        )
        return

    if pending["mode"] == "all":
        recipients = get_broadcast_recipients()
        target_description = f"Получателей: {len(recipients)}"
    else:
        recipients = [pending["recipient_id"]]
        target_description = (
            f"Получатель: {pending.get('recipient_label') or pending['recipient_id']}"
        )

    logger.info(
        f"Starting admin message send: admin_id={admin_id}, mode={pending['mode']}, recipients={len(recipients)}"
    )
    await show_menu_from_callback(
        callback_query,
        f"📣 Отправка запущена.\n{target_description}",
        create_main_menu_keyboard(admin_id),
    )

    sent_count = 0
    failed_count = 0
    sent_recipients: list[int] = []
    for recipient_id in recipients:
        try:
            await bot.copy_message(
                chat_id=recipient_id,
                from_chat_id=pending["source_chat_id"],
                message_id=pending["source_message_id"],
            )
            sent_count += 1
            sent_recipients.append(recipient_id)
        except TelegramAPIError as e:
            failed_count += 1
            logger.warning(
                f"Failed to copy admin message to user {recipient_id}: {e}"
            )
        except Exception as e:
            failed_count += 1
            logger.warning(
                f"Unexpected admin message error for user {recipient_id}: {e}"
            )
        await asyncio.sleep(0.07)

    try:
        await bot.delete_message(
            chat_id=pending["source_chat_id"],
            message_id=pending["source_message_id"],
        )
    except TelegramAPIError as e:
        logger.warning(f"Failed to delete admin source message after send: {e}")
    except Exception as e:
        logger.warning(f"Unexpected admin source message delete error: {e}")

    logger.info(
        f"Admin message send completed: admin_id={admin_id}, sent={sent_count}, failed={failed_count}"
    )
    message_text = pending.get("source_text") or "[медиа/файл без текста]"
    if pending["mode"] == "all":
        recipient_labels = build_admin_recipient_short_labels(sent_recipients)
        delivered_labels = [
            recipient_labels[recipient_id]
            for recipient_id in sent_recipients
            if recipient_id in recipient_labels
        ]
        displayed_labels = delivered_labels[:30]
        recipients_text = "\n".join(displayed_labels)
        remaining_count = max(0, len(delivered_labels) - len(displayed_labels))
        if remaining_count:
            recipients_text += f"\nи ещё {remaining_count}"
        result_text = (
            "📣 Рассылка завершена.\n\n"
            f"Отправлено: {sent_count}\n"
            f"Не доставлено: {failed_count}"
        )
        if recipients_text:
            result_text += f"\n\nПолучатели:\n{recipients_text}"
    else:
        recipient_label = build_admin_recipient_short_labels([pending["recipient_id"]])[
            pending["recipient_id"]
        ]
        result_text = f"📣 Отправка {recipient_label} завершена.\n\n{message_text}"

    await show_menu_from_callback(
        callback_query,
        result_text,
        create_main_menu_keyboard(admin_id),
    )


@dp.message(Command("connect"))
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

        # User has active access. Recreate only after an explicit Cascade 404.
        try:
            try:
                progress_message = await message.reply("Скачиваю конфиг...")
                config_content = await cascade_router.get_primary_config(user_id)
                await send_config_with_confirmation(
                    message.chat.id,
                    config_content,
                    source_message=progress_message,
                )
                return
            except CascadeNotFound:
                progress_message = await message.reply("Создаю новый конфиг...")
                ok, err, new_config = await create_or_restore_peer_for_user(
                    user_id, username, existing_peer.get("tariff_key")
                )
                if not ok:
                    await message.reply(f"❌ {err}")
                    return
                if not await send_config_with_confirmation(
                    message.chat.id,
                    new_config,
                    source_message=progress_message,
                ):
                    await message.reply(
                        "❌ Не удалось отправить конфигурацию. Используй /connect для повторной попытки."
                    )
                return
        except Exception as e:
            logger.error(f"Error while getting config in /connect: {e}", exc_info=True)
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


@dp.message(Command("extend"))
async def cmd_extend(message: types.Message):
    """Handle the /extend command (access extension)."""
    user_id = message.from_user.id

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


@dp.message(Command("status"))
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
                "⚠️ Оплаченный период закончился, для возобновления доступа к сервису, необходимо оплатить доступ.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="💵 Оплатить доступ",
                                callback_data="pay",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="На главную",
                                callback_data="main",
                            )
                        ],
                    ]
                ),
            )
            return

        # Calculate remaining time
        time_left = expire_date - now
        days_left = time_left.days
        hours_left = time_left.seconds // 3600
        minutes_left = (time_left.seconds % 3600) // 60

        # Build message
        status_text = "📊 Статус твоего VPN доступа:\n\n"
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
                '\n\n⚠️ Доступ к сервису скоро истекает! Нажми "Продлить доступ" для продления.'
            )

        await message.reply(status_text)

    except ValueError as e:
        logger.error(f"Failed to parse expiration date: {e}")
        await message.reply("❌ Ошибка при получении информации о доступе.")


@dp.message(Command("buy"))
async def cmd_buy(message: types.Message):
    """Handle the /buy command (payment method selection)."""
    user_id = message.from_user.id

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

    try:
        await cascade_router.ensure_reservation(user_id)
    except CascadeCapacityError:
        await safe_edit_callback_message(
            callback_query.message,
            "⚠️ Все VPN серверы временно заполнены. Оплата сейчас недоступна, попробуй позже.",
            reply_markup=create_back_to_menu_keyboard(),
        )
        return

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


@dp.callback_query(F.data.startswith("pay_yookassa_disabled_"))
async def handle_pay_yookassa_disabled_callback(callback_query: types.CallbackQuery):
    """Handle clicks on the disabled YooKassa button."""
    user_id = int(callback_query.data.replace("pay_yookassa_disabled_", ""))

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


@dp.callback_query(F.data.startswith("cancel_yookassa_"))
async def handle_cancel_yookassa_callback(callback_query: types.CallbackQuery):
    """Return from the YooKassa payment screen to tariff selection."""
    user_id = int(callback_query.data.replace("cancel_yookassa_", ""))
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


@dp.callback_query(F.data.startswith("cancel_stars_invoice_"))
async def handle_cancel_stars_invoice_callback(callback_query: types.CallbackQuery):
    """Delete the Stars invoice message on cancel."""
    user_id = int(callback_query.data.replace("cancel_stars_invoice_", ""))
    if callback_query.from_user.id != user_id:
        await safe_answer_callback(callback_query, "❌ Ошибка: неверный пользователь")
        return

    await safe_answer_callback(callback_query)
    try:
        await callback_query.message.delete()
    except TelegramAPIError as e:
        logger.error(f"Failed to delete Stars invoice message for user {user_id}: {e}")


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
        ok, err, new_config = await create_or_restore_peer_for_user(
            user_id, username, tariff_key
        )
        if ok:
            await callback_query.message.edit_text(
                "✅ Доступ создан. Отправляю конфигурацию...",
                reply_markup=create_main_menu_keyboard(user_id),
            )
            sent = await send_config_with_confirmation(
                callback_query.message.chat.id,
                new_config,
                source_message=callback_query.message,
            )
            if not sent:
                await callback_query.message.reply(
                    "❌ Не удалось отправить конфигурацию. Используй /connect для повторной попытки."
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
    logger.info(
        f"Incoming pre-checkout operation: user_id={pre_checkout_query.from_user.id}, payload={pre_checkout_query.invoice_payload}"
    )
    await payment_manager.process_payment(pre_checkout_query)


@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    """Handle a successful Telegram Stars payment and synchronize Cascade."""
    user_id = message.from_user.id
    username = message.from_user.username
    successful_payment = message.successful_payment
    confirmed, _, amount_paid = await payment_manager.confirm_payment(successful_payment)
    if not confirmed or not successful_payment.invoice_payload.startswith("vpn_access_stars_"):
        await message.reply("❌ Ошибка при обработке платежа.")
        return

    parts = successful_payment.invoice_payload.split("_")
    tariff_key = f"{parts[3]}_{parts[4]}" if len(parts) >= 5 else ""
    tariff_data = payment_manager.tariffs.get(tariff_key)
    if not tariff_data:
        await message.reply("❌ Ошибка в данных платежа.")
        return

    payment_id = (
        getattr(successful_payment, "telegram_payment_charge_id", None)
        or getattr(successful_payment, "provider_payment_charge_id", None)
        or f"stars_{user_id}_{tariff_key}"
    )
    db.add_payment(
        payment_id, user_id, amount_paid, "stars", tariff_key,
        {"source": "telegram_stars"},
    )
    if not db.claim_payment_success(payment_id):
        logger.info("Ignoring duplicate Stars payment event %s", payment_id)
        return

    primary_peer = db.get_primary_client_peer(user_id)
    if primary_peer:
        success, expire_date = db.extend_access(user_id, tariff_data["days"])
        if not success:
            await message.reply("❌ Ошибка при продлении доступа. Обратитесь в поддержку.")
            return
        db.update_payment_status(user_id, "paid", amount_paid, "stars", tariff_key)
        sync_result = await cascade_router.sync_user_access(user_id, expire_date)
        if sync_result["failed"]:
            db.add_provisioning_task(
                user_id, "sync_access", {"expire_date": expire_date},
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
        expire_date = db.activate_new_access(
            user_id, username, tariff_data["days"], tariff_key, "stars"
        )
        db.update_payment_status(user_id, "paid", amount_paid, "stars", tariff_key)
        await message.reply("🔄 Создаю VPN доступ...")
        ok, error, config = await create_or_restore_peer_for_user(
            user_id, username, tariff_key
        )
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


# Unknown command handler
@dp.message(
    ~Command(
        commands=[
            "start",
            "buy",
            "connect",
            "extend",
            "status",
            "admin_broadcast",
            "cancel",
        ]
    )
)
async def handle_unknown(message: types.Message):
    """Handle unknown messages."""
    user_id = message.from_user.id
    message_text = (message.text or "").strip().lower()
    if message_text == "start":
        return

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
            db.sync_expired_access_statuses()

            # Check expired peers
            expired_peers = db.get_expired_peers()

            for peer in expired_peers:
                try:
                    await bot.send_message(
                        chat_id=peer["telegram_user_id"],
                        text=(
                            "⚠️ Оплаченный период закончился, для возобновления доступа к сервису, необходимо оплатить доступ."
                        ),
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [
                                    InlineKeyboardButton(
                                        text="💵 Оплатить доступ",
                                        callback_data="pay",
                                    )
                                ],
                                [
                                    InlineKeyboardButton(
                                        text="На главную",
                                        callback_data="main",
                                    )
                                ],
                            ]
                        ),
                    )
                    db.mark_expired_notification_sent(peer["telegram_user_id"])
                except TelegramAPIError:
                    logger.warning(
                        f"Failed to send expiration notice to user {peer['telegram_user_id']}"
                    )

            # Check users for 1-hour reminder
            users_for_hour_notification = db.get_users_for_hour_notification()

            for user in users_for_hour_notification:
                try:
                    user_id = user["telegram_user_id"]
                    tariffs = payment_manager.get_user_tariffs(user_id)

                    tariff_text = ""
                    for tariff_data in tariffs.values():
                        tariff_text += f"⭐ {tariff_data['name']} - {tariff_data['stars_price']} Stars\n"
                        tariff_text += f"💳 {tariff_data['name']} - {tariff_data['rub_price']} руб.\n\n"

                    await bot.send_message(
                        chat_id=user_id,
                        text=f"⏰ Доступ к nikonVPN истекает через 1 час!\n\n"
                             f"💎 Доступные тарифы для продления:\n{tariff_text}",
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [
                                    InlineKeyboardButton(
                                        text="💵 Продлить доступ",
                                        callback_data="extend",
                                    )
                                ],
                                [
                                    InlineKeyboardButton(
                                        text="На главную",
                                        callback_data="main",
                                    )
                                ],
                            ]
                        ),
                    )

                    db.mark_hour_notification_sent(user_id)

                except TelegramAPIError:
                    logger.warning(
                        f"Failed to send one-hour notification to user {user['telegram_user_id']}"
                    )

            # Check users for 1-day reminder
            users_for_notification = db.get_users_for_notification(1)

            for user in users_for_notification:
                try:
                    user_id = user["telegram_user_id"]
                    tariffs = payment_manager.get_user_tariffs(user_id)

                    # Build text with available tariffs
                    tariff_text = ""
                    for tariff_data in tariffs.values():
                        tariff_text += f"⭐ {tariff_data['name']} - {tariff_data['stars_price']} Stars\n"
                        tariff_text += f"💳 {tariff_data['name']} - {tariff_data['rub_price']} руб.\n\n"

                    await bot.send_message(
                        chat_id=user_id,
                        text=f"⏰ Доступ к nikonVPN истекает завтра!\n\n"
                             f"💎 Доступные тарифы для продления:\n{tariff_text}",
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [
                                    InlineKeyboardButton(
                                        text="💵 Продлить доступ",
                                        callback_data="extend",
                                    )
                                ],
                                [
                                    InlineKeyboardButton(
                                        text="На главную",
                                        callback_data="main",
                                    )
                                ],
                            ]
                        ),
                    )

                    # Mark notification as sent
                    db.mark_notification_sent(user_id)

                except TelegramAPIError:
                    logger.warning(
                        f"Failed to send notification to user {user['telegram_user_id']}"
                    )

            # Check every 30 minutes
            await asyncio.sleep(30 * 60)

        except Exception as e:
            logger.error(f"Error while checking expired peers: {e}")
            await asyncio.sleep(60)  # Wait a minute on error


async def retry_provisioning_tasks() -> None:
    """Retry paid provisioning operations that previously failed."""
    while True:
        for task in db.get_pending_provisioning_tasks():
            try:
                payload = task["payload"]
                if task["operation"] == "create_peer":
                    if db.get_primary_client_peer(task["telegram_user_id"]):
                        config = await cascade_router.get_primary_config(
                            task["telegram_user_id"]
                        )
                    else:
                        _, config = await cascade_router.create_user_peer(
                            task["telegram_user_id"],
                            payload.get("username"),
                            payload["peer_name"],
                            payload["expire_date"],
                        )
                    config_sent = await send_config_with_confirmation(
                        task["telegram_user_id"], config, caption=None
                    )
                elif task["operation"] == "sync_access":
                    result = await cascade_router.sync_user_access(
                        task["telegram_user_id"], payload["expire_date"]
                    )
                    if result["failed"]:
                        raise RuntimeError(f"Failed peers: {result['failed']}")
                else:
                    raise RuntimeError(f"Unknown provisioning operation: {task['operation']}")
                db.complete_provisioning_task(task["id"])
                delivery_note = "" if task["operation"] != "create_peer" or config_sent else "\nКонфиг не доставлен; пользователь может запросить его повторно."
                await notify_admins(
                    f"✅ Отложенная операция выполнена\n\nTelegram ID: {task['telegram_user_id']}\nОперация: {task['operation']}{delivery_note}"
                )
            except Exception as exc:
                db.fail_provisioning_task(task["id"], str(exc))
                logger.error("Provisioning retry %s failed: %s", task["id"], exc)
        await asyncio.sleep(CASCADE_RETRY_INTERVAL_SECONDS)


async def main():
    """Main bot entry point."""
    try:
        db.sync_expired_access_statuses()

        cascade_status = await cascade_router.validate()
        logger.info("Cascade startup validation: %s", cascade_status)
        if not any(status.startswith("ok") for status in cascade_status.values()):
            raise RuntimeError("No healthy Cascade server is configured")
        set_runtime_ready(True)

        # Start background checks for expired peers and notifications
        asyncio.create_task(check_expired_peers())
        asyncio.create_task(retry_provisioning_tasks())

        # Start the bot
        logger.info("Starting Wgbot app...")
        await dp.start_polling(bot, skip_updates=True)

    except Exception as e:
        logger.error(f"Critical error: {e}")
    finally:
        set_runtime_ready(False)
        await close_shared_services()


if __name__ == "__main__":
    asyncio.run(main())
