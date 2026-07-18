import asyncio
import logging
from contextlib import suppress
from typing import Any

from aiogram import F, Router, types
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import get_admin_telegram_ids
from utils import format_date_for_user

logger = logging.getLogger(__name__)
router = Router(name="admin")
ADMIN_CLIENTS_PAGE_SIZE = 8

bot: Any
db: Any
payment_manager: Any
safe_answer_callback: Any
safe_edit_callback_message: Any
show_menu_from_callback: Any
create_main_menu_keyboard: Any

_awaiting_admin_message: dict[int, dict[str, Any]] = {}
_pending_admin_message: dict[int, dict[str, Any]] = {}
_admin_client_selection_pages: dict[int, int] = {}
_admin_discount_pages: dict[int, int] = {}
_admin_discount_queries: dict[int, str] = {}
_awaiting_admin_discount_input: dict[int, dict[str, Any]] = {}
_pending_admin_discounts: dict[int, dict[str, Any]] = {}


def configure(
    *,
    runtime_bot: Any,
    runtime_db: Any,
    runtime_payment_manager: Any,
    answer_callback: Any,
    edit_callback_message: Any,
    show_callback_menu: Any,
    main_menu_keyboard: Any,
) -> None:
    """Inject runtime services and shared UI helpers."""
    global bot, db, payment_manager
    global safe_answer_callback, safe_edit_callback_message
    global show_menu_from_callback, create_main_menu_keyboard
    bot = runtime_bot
    db = runtime_db
    payment_manager = runtime_payment_manager
    safe_answer_callback = answer_callback
    safe_edit_callback_message = edit_callback_message
    show_menu_from_callback = show_callback_menu
    create_main_menu_keyboard = main_menu_keyboard


def is_admin(user_id: int) -> bool:
    """Check whether a Telegram user is configured as an admin."""
    return user_id in get_admin_telegram_ids()


def clear_admin_state(admin_id: int) -> None:
    """Clear transient admin interaction state."""
    _awaiting_admin_message.pop(admin_id, None)
    _pending_admin_message.pop(admin_id, None)
    _admin_client_selection_pages.pop(admin_id, None)
    _admin_discount_pages.pop(admin_id, None)
    _admin_discount_queries.pop(admin_id, None)
    _awaiting_admin_discount_input.pop(admin_id, None)
    _pending_admin_discounts.pop(admin_id, None)


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


def format_discount_client_label(client: dict[str, Any]) -> str:
    """Format a compact client label for discount management."""
    username = str(client.get("telegram_username") or "").strip()
    identity = f"@{username}" if username else str(client["telegram_user_id"])
    return f"{identity} | ID {client['telegram_user_id']} | {int(client.get('promo') or 0)}%"


def create_admin_discount_clients_keyboard(
    admin_id: int, page: int = 0
) -> tuple[InlineKeyboardMarkup, int, int]:
    """Create a paginated discount-management client keyboard."""
    query = _admin_discount_queries.get(admin_id, "")
    clients, total = db.get_admin_clients_page(page, ADMIN_CLIENTS_PAGE_SIZE, query)
    total_pages = max(1, (total + ADMIN_CLIENTS_PAGE_SIZE - 1) // ADMIN_CLIENTS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    if page and not clients:
        clients, total = db.get_admin_clients_page(page, ADMIN_CLIENTS_PAGE_SIZE, query)

    rows: list[list[InlineKeyboardButton]] = []
    for client in clients:
        rows.append(
            [
                InlineKeyboardButton(
                    text=format_discount_client_label(client),
                    callback_data=f"admin_discount_client_{client['telegram_user_id']}",
                )
            ]
        )

    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_discount_page_{page - 1}")
        )
    if page < total_pages - 1:
        navigation.append(
            InlineKeyboardButton(text="Далее ➡️", callback_data=f"admin_discount_page_{page + 1}")
        )
    if navigation:
        rows.append(navigation)

    rows.append(
        [InlineKeyboardButton(text="🔎 Найти клиента", callback_data="admin_discount_search")]
    )
    if query:
        rows.append(
            [
                InlineKeyboardButton(
                    text="✖️ Сбросить поиск", callback_data="admin_discount_search_reset"
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="На главную", callback_data="main")])
    return InlineKeyboardMarkup(inline_keyboard=rows), page, total


def format_admin_client_details(client: dict[str, Any]) -> str:
    """Build the admin-facing client details and effective tariff prices."""
    user_id = int(client["telegram_user_id"])
    username = str(client.get("telegram_username") or "").strip()
    expire_date = client.get("expire_date")
    expire_text = format_date_for_user(expire_date) if expire_date else "Не задан"
    server_key = client.get("server_key") or "Не назначен"
    interface_id = client.get("interface_id") or "Не назначен"
    peer_name = client.get("peer_name") or "Не назначен"
    tariffs = payment_manager.get_user_tariffs(user_id)
    prices = "\n".join(
        f"• {tariff['name']}: ⭐ {tariff['stars_price']} | 💳 {tariff['rub_price']} руб."
        for tariff in tariffs.values()
    )
    return (
        "👤 Клиент\n\n"
        f"Username: {'@' + username if username else 'не указан'}\n"
        f"Telegram ID: {user_id}\n"
        f"Статус: {client.get('payment_status') or 'unpaid'}\n"
        f"Доступ до: {expire_text}\n"
        f"Скидка: {int(client.get('promo') or 0)}%\n"
        f"Сервер: {server_key}\n"
        f"Интерфейс: {interface_id}\n"
        f"Primary peer: {peer_name}\n"
        f"Устройств: {int(client.get('device_count') or 0)}\n\n"
        f"Цены со скидкой:\n{prices}"
    )


def create_admin_client_discount_keyboard(user_id: int) -> InlineKeyboardMarkup:
    presets = (0, 10, 20, 30, 50)
    rows = [
        [
            InlineKeyboardButton(
                text=f"{value}%",
                callback_data=f"admin_discount_value_{user_id}_{value}",
            )
            for value in presets[:3]
        ],
        [
            InlineKeyboardButton(
                text=f"{value}%",
                callback_data=f"admin_discount_value_{user_id}_{value}",
            )
            for value in presets[3:]
        ],
        [
            InlineKeyboardButton(
                text="✏️ Другая скидка",
                callback_data=f"admin_discount_custom_{user_id}",
            )
        ],
        [InlineKeyboardButton(text="🔙 К списку", callback_data="admin_manage_clients")],
        [InlineKeyboardButton(text="На главную", callback_data="main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def create_admin_discount_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data="admin_discount_confirm"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="admin_discount_cancel"),
            ],
            [InlineKeyboardButton(text="На главную", callback_data="main")],
        ]
    )


def create_admin_discount_input_cancel_keyboard(user_id: int | None = None) -> InlineKeyboardMarkup:
    callback = f"admin_discount_client_{user_id}" if user_id else "admin_manage_clients"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data=callback)],
            [InlineKeyboardButton(text="На главную", callback_data="main")],
        ]
    )


def create_admin_message_confirm_keyboard() -> InlineKeyboardMarkup:
    """Create the admin message confirmation keyboard."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Отправить", callback_data="admin_broadcast_confirm"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="admin_broadcast_cancel"),
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


@router.message(Command("admin_broadcast"))
async def cmd_admin_broadcast(message: types.Message):
    """Show the admin broadcast menu from a command."""
    await send_admin_broadcast_menu(message.chat.id, message.from_user.id)


@router.callback_query(F.data == "admin_broadcast")
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


async def show_admin_discount_clients(callback_query: types.CallbackQuery, page: int = 0) -> None:
    admin_id = callback_query.from_user.id
    keyboard, page, total = create_admin_discount_clients_keyboard(admin_id, page)
    _admin_discount_pages[admin_id] = page
    query = _admin_discount_queries.get(admin_id, "")
    query_text = f"\nПоиск: {query}" if query else ""
    await show_menu_from_callback(
        callback_query,
        f"👥 Управление клиентами\n\nНайдено: {total}{query_text}\nВыбери клиента:",
        keyboard,
    )


@router.message(Command("admin_clients"))
async def cmd_admin_clients(message: types.Message):
    """Open discount management from a command."""
    admin_id = message.from_user.id
    if not is_admin(admin_id):
        await message.answer("❌ Недостаточно прав.")
        return
    _admin_discount_queries.pop(admin_id, None)
    _admin_discount_pages[admin_id] = 0
    keyboard, _, total = create_admin_discount_clients_keyboard(admin_id, 0)
    await message.answer(
        f"👥 Управление клиентами\n\nНайдено: {total}\nВыбери клиента:",
        reply_markup=keyboard,
    )


@router.callback_query(F.data == "admin_manage_clients")
async def handle_admin_manage_clients(callback_query: types.CallbackQuery):
    await safe_answer_callback(callback_query)
    admin_id = callback_query.from_user.id
    if not is_admin(admin_id):
        await safe_answer_callback(callback_query, "❌ Недостаточно прав.")
        return
    _awaiting_admin_discount_input.pop(admin_id, None)
    _pending_admin_discounts.pop(admin_id, None)
    await show_admin_discount_clients(callback_query, _admin_discount_pages.get(admin_id, 0))


@router.callback_query(F.data.startswith("admin_discount_page_"))
async def handle_admin_discount_page(callback_query: types.CallbackQuery):
    await safe_answer_callback(callback_query)
    admin_id = callback_query.from_user.id
    if not is_admin(admin_id):
        await safe_answer_callback(callback_query, "❌ Недостаточно прав.")
        return
    try:
        page = int(callback_query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        page = 0
    await show_admin_discount_clients(callback_query, page)


@router.callback_query(F.data == "admin_discount_search")
async def handle_admin_discount_search(callback_query: types.CallbackQuery):
    await safe_answer_callback(callback_query)
    admin_id = callback_query.from_user.id
    if not is_admin(admin_id):
        await safe_answer_callback(callback_query, "❌ Недостаточно прав.")
        return
    _pending_admin_discounts.pop(admin_id, None)
    _awaiting_admin_discount_input[admin_id] = {
        "mode": "search",
        "service_chat_id": callback_query.message.chat.id,
        "service_message_id": callback_query.message.message_id,
    }
    await show_menu_from_callback(
        callback_query,
        "🔎 Отправь Telegram ID, username или часть username клиента.",
        create_admin_discount_input_cancel_keyboard(),
    )


@router.callback_query(F.data == "admin_discount_search_reset")
async def handle_admin_discount_search_reset(callback_query: types.CallbackQuery):
    await safe_answer_callback(callback_query)
    admin_id = callback_query.from_user.id
    if not is_admin(admin_id):
        await safe_answer_callback(callback_query, "❌ Недостаточно прав.")
        return
    _admin_discount_queries.pop(admin_id, None)
    await show_admin_discount_clients(callback_query, 0)


@router.callback_query(F.data.startswith("admin_discount_client_"))
async def handle_admin_discount_client(callback_query: types.CallbackQuery):
    await safe_answer_callback(callback_query)
    admin_id = callback_query.from_user.id
    if not is_admin(admin_id):
        await safe_answer_callback(callback_query, "❌ Недостаточно прав.")
        return
    try:
        user_id = int(callback_query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await safe_answer_callback(callback_query, "❌ Некорректный Telegram ID")
        return
    client = db.get_admin_client_details(user_id)
    if not client:
        await safe_answer_callback(callback_query, "❌ Клиент не найден")
        await show_admin_discount_clients(callback_query, 0)
        return
    _awaiting_admin_discount_input.pop(admin_id, None)
    _pending_admin_discounts.pop(admin_id, None)
    await show_menu_from_callback(
        callback_query,
        format_admin_client_details(client),
        create_admin_client_discount_keyboard(user_id),
    )


async def show_admin_discount_confirmation(
    callback_query: types.CallbackQuery,
    client: dict[str, Any],
    new_promo: int,
) -> None:
    admin_id = callback_query.from_user.id
    user_id = int(client["telegram_user_id"])
    old_promo = int(client.get("promo") or 0)
    _pending_admin_discounts[admin_id] = {
        "user_id": user_id,
        "old_promo": old_promo,
        "new_promo": new_promo,
    }
    username = str(client.get("telegram_username") or "").strip()
    identity = f"@{username}" if username else str(user_id)
    await show_menu_from_callback(
        callback_query,
        "Подтвердить изменение скидки?\n\n"
        f"Клиент: {identity}\n"
        f"Telegram ID: {user_id}\n"
        f"Сервер: {client.get('server_key') or 'Не назначен'}\n"
        f"Текущая скидка: {old_promo}%\n"
        f"Новая скидка: {new_promo}%",
        create_admin_discount_confirm_keyboard(),
    )


@router.callback_query(F.data.startswith("admin_discount_value_"))
async def handle_admin_discount_value(callback_query: types.CallbackQuery):
    await safe_answer_callback(callback_query)
    admin_id = callback_query.from_user.id
    if not is_admin(admin_id):
        await safe_answer_callback(callback_query, "❌ Недостаточно прав.")
        return
    try:
        _, user_id_text, promo_text = callback_query.data.rsplit("_", 2)
        user_id, promo = int(user_id_text), int(promo_text)
    except (ValueError, IndexError):
        await safe_answer_callback(callback_query, "❌ Некорректные данные")
        return
    if promo not in {0, 10, 20, 30, 50}:
        await safe_answer_callback(callback_query, "❌ Недопустимая скидка")
        return
    client = db.get_admin_client_details(user_id)
    if not client:
        await safe_answer_callback(callback_query, "❌ Клиент не найден")
        return
    await show_admin_discount_confirmation(callback_query, client, promo)


@router.callback_query(F.data.startswith("admin_discount_custom_"))
async def handle_admin_discount_custom(callback_query: types.CallbackQuery):
    await safe_answer_callback(callback_query)
    admin_id = callback_query.from_user.id
    if not is_admin(admin_id):
        await safe_answer_callback(callback_query, "❌ Недостаточно прав.")
        return
    try:
        user_id = int(callback_query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await safe_answer_callback(callback_query, "❌ Некорректный Telegram ID")
        return
    if not db.get_admin_client_details(user_id):
        await safe_answer_callback(callback_query, "❌ Клиент не найден")
        return
    _pending_admin_discounts.pop(admin_id, None)
    _awaiting_admin_discount_input[admin_id] = {
        "mode": "promo",
        "user_id": user_id,
        "service_chat_id": callback_query.message.chat.id,
        "service_message_id": callback_query.message.message_id,
    }
    await show_menu_from_callback(
        callback_query,
        "✏️ Отправь размер скидки целым числом от 0 до 90.",
        create_admin_discount_input_cancel_keyboard(user_id),
    )


@router.callback_query(F.data == "admin_discount_confirm")
async def handle_admin_discount_confirm(callback_query: types.CallbackQuery):
    await safe_answer_callback(callback_query)
    admin_id = callback_query.from_user.id
    if not is_admin(admin_id):
        await safe_answer_callback(callback_query, "❌ Недостаточно прав.")
        return
    pending = _pending_admin_discounts.pop(admin_id, None)
    if not pending:
        await safe_answer_callback(callback_query, "Изменение уже обработано")
        return
    user_id = int(pending["user_id"])
    client = db.get_admin_client_details(user_id)
    if not client:
        await safe_answer_callback(callback_query, "❌ Клиент не найден")
        return
    old_promo = int(client.get("promo") or 0)
    new_promo = int(pending["new_promo"])
    if not db.set_client_promo(user_id, new_promo):
        await safe_answer_callback(callback_query, "❌ Не удалось сохранить скидку")
        return
    db.log_admin_promo_change(admin_id, user_id, client.get("server_key"), old_promo, new_promo)
    updated = db.get_admin_client_details(user_id)
    await show_menu_from_callback(
        callback_query,
        "✅ Скидка сохранена.\n\n" + format_admin_client_details(updated),
        create_admin_client_discount_keyboard(user_id),
    )


@router.callback_query(F.data == "admin_discount_cancel")
async def handle_admin_discount_cancel(callback_query: types.CallbackQuery):
    await safe_answer_callback(callback_query)
    admin_id = callback_query.from_user.id
    if not is_admin(admin_id):
        await safe_answer_callback(callback_query, "❌ Недостаточно прав.")
        return
    pending = _pending_admin_discounts.pop(admin_id, None)
    if pending and db.get_admin_client_details(int(pending["user_id"])):
        client = db.get_admin_client_details(int(pending["user_id"]))
        await show_menu_from_callback(
            callback_query,
            format_admin_client_details(client),
            create_admin_client_discount_keyboard(int(pending["user_id"])),
        )
        return
    await show_admin_discount_clients(callback_query, _admin_discount_pages.get(admin_id, 0))


@router.message(Command("cancel"))
async def cmd_cancel(message: types.Message):
    """Cancel the current admin action."""
    user_id = message.from_user.id
    if (
        user_id in _awaiting_admin_message
        or user_id in _pending_admin_message
        or user_id in _awaiting_admin_discount_input
        or user_id in _pending_admin_discounts
    ):
        _awaiting_admin_message.pop(user_id, None)
        _pending_admin_message.pop(user_id, None)
        _admin_client_selection_pages.pop(user_id, None)
        _awaiting_admin_discount_input.pop(user_id, None)
        _pending_admin_discounts.pop(user_id, None)
        await message.answer(
            "Действие администратора отменено.",
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
        logger.warning(f"Rejected admin message capture from non-admin user {admin_id}")
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


@router.callback_query(F.data == "admin_broadcast_all")
async def handle_admin_broadcast_all_callback(callback_query: types.CallbackQuery):
    """Start broadcast-to-all message capture."""
    await safe_answer_callback(callback_query)
    admin_id = callback_query.from_user.id
    if not is_admin(admin_id):
        logger.warning(f"Rejected broadcast-all access from non-admin user {admin_id}")
        await safe_answer_callback(callback_query, "❌ Недостаточно прав.")
        return

    await start_admin_message_capture(callback_query, mode="all")


@router.callback_query(F.data == "admin_broadcast_client_menu")
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


@router.callback_query(F.data.startswith("admin_clients_page_"))
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


@router.callback_query(F.data.startswith("admin_message_client_"))
async def handle_admin_message_client(callback_query: types.CallbackQuery):
    """Start direct message capture for a selected client."""
    await safe_answer_callback(callback_query)
    admin_id = callback_query.from_user.id
    if not is_admin(admin_id):
        logger.warning(f"Rejected direct message access from non-admin user {admin_id}")
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


@router.message(
    lambda message: message.from_user and message.from_user.id in _awaiting_admin_discount_input
)
async def handle_admin_discount_input(message: types.Message):
    """Handle admin client search and custom discount input."""
    admin_id = message.from_user.id
    flow = _awaiting_admin_discount_input.get(admin_id)
    if not flow or not is_admin(admin_id):
        _awaiting_admin_discount_input.pop(admin_id, None)
        return
    value = (message.text or "").strip()
    if flow["mode"] == "search":
        if not value:
            await message.answer("Введи Telegram ID или username клиента.")
            return
        _awaiting_admin_discount_input.pop(admin_id, None)
        _admin_discount_queries[admin_id] = value[:100]
        _admin_discount_pages[admin_id] = 0
        keyboard, _, total = create_admin_discount_clients_keyboard(admin_id, 0)
        text = (
            f"👥 Управление клиентами\n\nНайдено: {total}\n"
            f"Поиск: {_admin_discount_queries[admin_id]}\nВыбери клиента:"
        )
        edited = await edit_admin_service_message(flow, text, keyboard)
        if not edited:
            await message.answer(text, reply_markup=keyboard)
    else:
        try:
            promo = int(value)
        except ValueError:
            promo = -1
        if not 0 <= promo <= 90 or str(promo) != value:
            error_text = "Скидка должна быть целым числом от 0 до 90. Попробуй еще раз."
            edited = await edit_admin_service_message(
                flow,
                error_text,
                create_admin_discount_input_cancel_keyboard(flow.get("user_id")),
            )
            if not edited:
                await message.answer(error_text)
            return
        user_id = int(flow["user_id"])
        client = db.get_admin_client_details(user_id)
        if not client:
            _awaiting_admin_discount_input.pop(admin_id, None)
            await message.answer("❌ Клиент не найден.")
            return
        _awaiting_admin_discount_input.pop(admin_id, None)
        old_promo = int(client.get("promo") or 0)
        _pending_admin_discounts[admin_id] = {
            "user_id": user_id,
            "old_promo": old_promo,
            "new_promo": promo,
        }
        username = str(client.get("telegram_username") or "").strip()
        identity = f"@{username}" if username else str(user_id)
        text = (
            "Подтвердить изменение скидки?\n\n"
            f"Клиент: {identity}\n"
            f"Telegram ID: {user_id}\n"
            f"Сервер: {client.get('server_key') or 'Не назначен'}\n"
            f"Текущая скидка: {old_promo}%\n"
            f"Новая скидка: {promo}%"
        )
        edited = await edit_admin_service_message(
            flow, text, create_admin_discount_confirm_keyboard()
        )
        if not edited:
            await message.answer(text, reply_markup=create_admin_discount_confirm_keyboard())
    with suppress(TelegramAPIError):
        await message.delete()


@router.message(
    lambda message: message.from_user and message.from_user.id in _awaiting_admin_message
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
            "Этот тип сообщения не поддерживается.\nОтправь текст, фото, видео или файл с подписью."
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
        target_text = f"Получатель: {flow.get('recipient_label') or flow.get('recipient_id')}"

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


@router.callback_query(F.data == "admin_broadcast_cancel")
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


@router.callback_query(F.data == "admin_message_cancel")
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


@router.callback_query(F.data == "admin_broadcast_confirm")
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
            logger.warning(f"Failed to copy admin message to user {recipient_id}: {e}")
        except Exception as e:
            failed_count += 1
            logger.warning(f"Unexpected admin message error for user {recipient_id}: {e}")
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
            f"📣 Рассылка завершена.\n\nОтправлено: {sent_count}\nНе доставлено: {failed_count}"
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
