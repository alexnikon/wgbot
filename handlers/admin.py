import asyncio
import logging
from contextlib import suppress
from typing import Any

from aiogram import Bot, F, Router, types
from aiogram.filters import BaseFilter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from callbacks import (
    AdminClientCallback,
    AdminConfigCallback,
    AdminDiscountCallback,
    AdminPageCallback,
    RefundConfirmationCallback,
)
from cascade_api import CascadeError, CascadeNotFound, CascadeRouter
from config import get_admin_telegram_ids
from database import Database, normalize_config_name
from utils import format_date_for_user

logger = logging.getLogger(__name__)
router = Router(name="admin")
ADMIN_CLIENTS_PAGE_SIZE = 8
ADMIN_CONFIGS_PAGE_SIZE = 6
ADMIN_WORKFLOW_TYPE = "admin_flow"


def is_admin(user_id: int) -> bool:
    return user_id in get_admin_telegram_ids()


class AdminWorkflowService:
    """Persist administrative conversation state in SQLite."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, admin_id: int) -> dict[str, Any] | None:
        workflow = self.db.get_admin_workflow(admin_id, ADMIN_WORKFLOW_TYPE)
        return workflow["data"] if workflow else None

    def set(self, admin_id: int, state: str, **data: Any) -> None:
        self.db.set_admin_workflow(
            admin_id,
            ADMIN_WORKFLOW_TYPE,
            state,
            {"state": state, **data},
        )

    def clear(self, admin_id: int) -> None:
        self.db.delete_admin_workflow(admin_id, ADMIN_WORKFLOW_TYPE)


class ActiveAdminWorkflow(BaseFilter):
    async def __call__(
        self, message: types.Message, admin_workflows: AdminWorkflowService
    ) -> bool:
        return bool(
            message.from_user
            and is_admin(message.from_user.id)
            and admin_workflows.get(message.from_user.id)
        )


def broadcast_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📣 Рассылка всем", callback_data="admin_broadcast_all")],
            [
                InlineKeyboardButton(
                    text="👤 Сообщение клиенту",
                    callback_data="admin_broadcast_client_menu",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Управление клиентами",
                    callback_data="admin_manage_clients",
                )
            ],
        ]
    )


def admin_dashboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👥 Клиенты и скидки", callback_data="admin_client_list"
                )
            ],
            [InlineKeyboardButton(text="📣 Рассылка", callback_data="admin_broadcast")],
            [
                InlineKeyboardButton(
                    text="💳 Платежи и расхождения", callback_data="admin_payments"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⭐ Сверить Stars", callback_data="admin_stars_reconcile"
                )
            ],
            [
                InlineKeyboardButton(
                    text="↩️ Возврат Stars", callback_data="admin_refund_stars"
                )
            ],
            [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="main")],
        ]
    )


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_flow_cancel")]
        ]
    )


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Отправить", callback_data="admin_flow_confirm"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="admin_flow_cancel"),
            ]
        ]
    )


def client_list_keyboard(
    db: Database, *, view: str, page: int, query: str = ""
) -> tuple[InlineKeyboardMarkup, int]:
    clients, total = db.get_admin_clients_page(
        page, ADMIN_CLIENTS_PAGE_SIZE, query=query
    )
    pages = max(1, (total + ADMIN_CLIENTS_PAGE_SIZE - 1) // ADMIN_CLIENTS_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    rows: list[list[InlineKeyboardButton]] = []
    for client in clients:
        user_id = int(client["telegram_user_id"])
        username = str(client.get("telegram_username") or "")
        label = f"{user_id} | @{username}" if username else str(user_id)
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=AdminClientCallback(action=view, user_id=user_id).pack(),
                )
            ]
        )
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=AdminPageCallback(view=view, page=page - 1).pack(),
            )
        )
    if page + 1 < pages:
        navigation.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=AdminPageCallback(view=view, page=page + 1).pack(),
            )
        )
    if navigation:
        rows.append(navigation)
    if view in {"discount", "details"}:
        rows.append(
            [InlineKeyboardButton(text="🔎 Найти клиента", callback_data="admin_search_client")]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Управление клиентами", callback_data="admin_manage_clients"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows), total


def discount_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = []
    for values in ((0, 5, 10), (15, 20, 25)):
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{value}%",
                    callback_data=AdminDiscountCallback(
                        user_id=user_id, value=value
                    ).pack(),
                )
                for value in values
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="✏️ Другое значение",
                callback_data=AdminClientCallback(
                    action="custom_discount", user_id=user_id
                ).pack(),
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ К клиенту",
                callback_data=AdminClientCallback(
                    action="details", user_id=user_id
                ).pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def client_card_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💸 Скидка",
                    callback_data=AdminClientCallback(
                        action="discount", user_id=user_id
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text="🗂 Конфиги",
                    callback_data=AdminConfigCallback(
                        action="list", user_id=user_id
                    ).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ К списку", callback_data="admin_client_list"
                )
            ],
        ]
    )


def config_list_keyboard(
    db: Database, user_id: int, page: int = 0
) -> tuple[InlineKeyboardMarkup, int]:
    configs = db.get_managed_client_configs(user_id)
    pages = max(1, (len(configs) + ADMIN_CONFIGS_PAGE_SIZE - 1) // ADMIN_CONFIGS_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * ADMIN_CONFIGS_PAGE_SIZE
    rows: list[list[InlineKeyboardButton]] = []
    for config in configs[start : start + ADMIN_CONFIGS_PAGE_SIZE]:
        active = bool(config["admin_enabled"])
        status = "✅" if active and config["enabled"] else ("⏸" if not active else "⚠️")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{status} {config['config_name']}",
                    callback_data=AdminConfigCallback(
                        action="view",
                        user_id=user_id,
                        peer_id=int(config["id"]),
                    ).pack(),
                )
            ]
        )
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=AdminConfigCallback(
                    action="page", user_id=user_id, value=page - 1
                ).pack(),
            )
        )
    if page + 1 < pages:
        navigation.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=AdminConfigCallback(
                    action="page", user_id=user_id, value=page + 1
                ).pack(),
            )
        )
    if navigation:
        rows.append(navigation)
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="➕ Добавить конфиг",
                    callback_data=AdminConfigCallback(
                        action="add", user_id=user_id
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ К клиенту",
                    callback_data=AdminClientCallback(
                        action="details", user_id=user_id
                    ).pack(),
                )
            ],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows), page


def config_details_keyboard(config: dict[str, Any]) -> InlineKeyboardMarkup:
    user_id = int(config["telegram_user_id"])
    peer_id = int(config["id"])
    rows = [
        [
            InlineKeyboardButton(
                text="✏️ Переименовать",
                callback_data=AdminConfigCallback(
                    action="rename", user_id=user_id, peer_id=peer_id
                ).pack(),
            )
        ]
    ]
    if config["role"] == "additional":
        action = "deactivate" if config["admin_enabled"] else "restore"
        text = "🗑 Деактивировать" if config["admin_enabled"] else "♻️ Восстановить"
        rows.append(
            [
                InlineKeyboardButton(
                    text=text,
                    callback_data=AdminConfigCallback(
                        action=action, user_id=user_id, peer_id=peer_id
                    ).pack(),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ К конфигам",
                callback_data=AdminConfigCallback(action="list", user_id=user_id).pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def config_error_back_keyboard(user_id: int, peer_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=AdminConfigCallback(
                        action="view", user_id=user_id, peer_id=peer_id
                    ).pack(),
                )
            ]
        ]
    )


def format_config(config: dict[str, Any]) -> str:
    if not config["admin_enabled"]:
        status = "деактивирован"
    elif config["enabled"]:
        status = "активен"
    else:
        status = "недоступен или срок истёк"
    return (
        f"🗂 {config['config_name']}\n\n"
        f"Тип: {'основной' if config['role'] == 'primary' else 'дополнительный'}\n"
        f"Сервер: {config['server_key']}\n"
        f"Интерфейс: {config['interface_id']}\n"
        f"Состояние: {status}"
    )


def format_client(client: dict[str, Any]) -> str:
    username = str(client.get("telegram_username") or "")
    identity = f"@{username}" if username else "без username"
    expiry = client.get("expire_date")
    return (
        "👤 Клиент\n\n"
        f"Telegram ID: {client['telegram_user_id']}\n"
        f"Username: {identity}\n"
        f"Скидка: {int(client.get('promo') or 0)}%\n"
        f"Сервер: {client.get('server_keys') or 'не назначен'}\n"
        f"Устройств: {int(client.get('device_count') or 0)}\n"
        f"Доступ до: {format_date_for_user(expiry) if expiry else 'нет'}"
    )



@router.callback_query(F.data == "admin_broadcast")
async def open_broadcast(callback: types.CallbackQuery, safe_answer_callback) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    await callback.message.edit_text("📣 Рассылка", reply_markup=broadcast_menu_keyboard())


@router.callback_query(F.data == "admin_manage_clients")
async def open_clients(
    callback: types.CallbackQuery, db: Database, safe_answer_callback
) -> None:
    if not is_admin(callback.from_user.id):
        await safe_answer_callback(callback, "❌ Недостаточно прав.")
        return
    await safe_answer_callback(callback)
    await callback.message.edit_text(
        "👥 Управление клиентами", reply_markup=admin_dashboard_keyboard()
    )


@router.callback_query(F.data == "admin_client_list")
async def open_client_list(
    callback: types.CallbackQuery, db: Database, safe_answer_callback
) -> None:
    if not is_admin(callback.from_user.id):
        await safe_answer_callback(callback, "❌ Недостаточно прав.")
        return
    await safe_answer_callback(callback)
    keyboard, total = client_list_keyboard(db, view="details", page=0)
    await callback.message.edit_text(
        f"👥 Клиенты и скидки\n\nНайдено: {total}", reply_markup=keyboard
    )


@router.callback_query(F.data == "admin_broadcast_all")
async def start_broadcast_all(
    callback: types.CallbackQuery,
    db: Database,
    admin_workflows: AdminWorkflowService,
    safe_answer_callback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    admin_workflows.set(
        callback.from_user.id,
        "await_message",
        mode="all",
        service_chat_id=callback.message.chat.id,
        service_message_id=callback.message.message_id,
    )
    recipients = db.get_client_telegram_ids()
    await callback.message.edit_text(
        f"Отправь сообщение для рассылки.\n\nПолучателей: {len(recipients)}",
        reply_markup=cancel_keyboard(),
    )


@router.callback_query(F.data == "admin_broadcast_client_menu")
async def open_message_clients(
    callback: types.CallbackQuery, db: Database, safe_answer_callback
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    keyboard, total = client_list_keyboard(db, view="message", page=0)
    await callback.message.edit_text(
        f"👤 Выбери получателя\n\nНайдено: {total}", reply_markup=keyboard
    )


@router.callback_query(AdminPageCallback.filter())
@router.callback_query(F.data.regexp(r"^admin_clients_page_[0-9]+$"))
@router.callback_query(F.data.regexp(r"^admin_discount_page_[0-9]+$"))
async def change_page(
    callback: types.CallbackQuery,
    db: Database,
    safe_answer_callback,
    callback_data: AdminPageCallback | None = None,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    if callback_data:
        view, page = callback_data.view, callback_data.page
    else:
        data = callback.data or ""
        view = "message" if data.startswith("admin_clients_page_") else "details"
        try:
            page = int(data.rsplit("_", 1)[1])
        except ValueError:
            page = 0
    keyboard, total = client_list_keyboard(db, view=view, page=page)
    await callback.message.edit_text(
        f"👥 Клиенты\n\nНайдено: {total}", reply_markup=keyboard
    )


@router.callback_query(AdminClientCallback.filter(F.action == "details"))
async def show_client_details(
    callback: types.CallbackQuery,
    db: Database,
    safe_answer_callback,
    callback_data: AdminClientCallback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    client = db.get_admin_client_details(callback_data.user_id)
    if not client:
        await callback.message.edit_text(
            "❌ Клиент не найден.", reply_markup=admin_dashboard_keyboard()
        )
        return
    await callback.message.edit_text(
        format_client(client), reply_markup=client_card_keyboard(callback_data.user_id)
    )


@router.callback_query(AdminClientCallback.filter(F.action == "message"))
@router.callback_query(F.data.regexp(r"^admin_message_client_[0-9]+$"))
async def choose_message_client(
    callback: types.CallbackQuery,
    admin_workflows: AdminWorkflowService,
    safe_answer_callback,
    callback_data: AdminClientCallback | None = None,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    user_id = callback_data.user_id if callback_data else int(
        (callback.data or "").removeprefix("admin_message_client_")
    )
    admin_workflows.set(
        callback.from_user.id,
        "await_message",
        mode="client",
        recipient_id=user_id,
        service_chat_id=callback.message.chat.id,
        service_message_id=callback.message.message_id,
    )
    await callback.message.edit_text(
        f"Отправь сообщение для пользователя {user_id}.",
        reply_markup=cancel_keyboard(),
    )


@router.callback_query(AdminClientCallback.filter(F.action == "discount"))
@router.callback_query(F.data.regexp(r"^admin_discount_client_[0-9]+$"))
async def choose_discount_client(
    callback: types.CallbackQuery,
    db: Database,
    safe_answer_callback,
    callback_data: AdminClientCallback | None = None,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    user_id = callback_data.user_id if callback_data else int(
        (callback.data or "").removeprefix("admin_discount_client_")
    )
    client = db.get_admin_client_details(user_id)
    if not client:
        await callback.message.edit_text(
            "❌ Клиент не найден.", reply_markup=admin_dashboard_keyboard()
        )
        return
    await callback.message.edit_text(
        format_client(client), reply_markup=discount_keyboard(user_id)
    )


@router.callback_query(AdminDiscountCallback.filter())
@router.callback_query(F.data.regexp(r"^admin_discount_value_[0-9]+_[0-9]+$"))
async def set_discount(
    callback: types.CallbackQuery,
    db: Database,
    safe_answer_callback,
    callback_data: AdminDiscountCallback | None = None,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    if callback_data:
        user_id, value = callback_data.user_id, callback_data.value
    else:
        raw = (callback.data or "").removeprefix("admin_discount_value_")
        raw_user_id, raw_value = raw.rsplit("_", 1)
        user_id, value = int(raw_user_id), int(raw_value)
    client = db.get_admin_client_details(user_id)
    if not client or not db.set_client_promo(user_id, value):
        await callback.message.edit_text("❌ Не удалось сохранить скидку.")
        return
    db.log_admin_promo_change(
        callback.from_user.id,
        user_id,
        client.get("server_key"),
        int(client.get("promo") or 0),
        value,
    )
    await callback.message.edit_text(
        f"✅ Скидка {value}% сохранена.",
        reply_markup=client_card_keyboard(user_id),
    )


@router.callback_query(AdminClientCallback.filter(F.action == "custom_discount"))
@router.callback_query(F.data.regexp(r"^admin_discount_custom_[0-9]+$"))
async def start_custom_discount(
    callback: types.CallbackQuery,
    admin_workflows: AdminWorkflowService,
    safe_answer_callback,
    callback_data: AdminClientCallback | None = None,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    user_id = callback_data.user_id if callback_data else int(
        (callback.data or "").removeprefix("admin_discount_custom_")
    )
    admin_workflows.set(
        callback.from_user.id,
        "await_discount",
        user_id=user_id,
        service_chat_id=callback.message.chat.id,
        service_message_id=callback.message.message_id,
    )
    await callback.message.edit_text(
        "Введи скидку целым числом от 0 до 90.", reply_markup=cancel_keyboard()
    )


@router.callback_query(F.data == "admin_search_client")
async def start_search(
    callback: types.CallbackQuery,
    admin_workflows: AdminWorkflowService,
    safe_answer_callback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    admin_workflows.set(
        callback.from_user.id,
        "await_search",
        service_chat_id=callback.message.chat.id,
        service_message_id=callback.message.message_id,
    )
    await callback.message.edit_text(
        "Введи Telegram ID или username.", reply_markup=cancel_keyboard()
    )


@router.callback_query(F.data == "admin_refund_stars")
async def start_stars_refund(
    callback: types.CallbackQuery,
    admin_workflows: AdminWorkflowService,
    safe_answer_callback,
) -> None:
    if not is_admin(callback.from_user.id):
        await safe_answer_callback(callback, "❌ Недостаточно прав.")
        return
    await safe_answer_callback(callback)
    admin_workflows.set(
        callback.from_user.id,
        "await_refund_charge",
        service_chat_id=callback.message.chat.id,
        service_message_id=callback.message.message_id,
    )
    await callback.message.edit_text(
        "Введи Telegram charge ID платежа Stars.",
        reply_markup=cancel_keyboard(),
    )


@router.callback_query(AdminConfigCallback.filter(F.action.in_({"list", "page"})))
async def show_client_configs(
    callback: types.CallbackQuery,
    db: Database,
    safe_answer_callback,
    callback_data: AdminConfigCallback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    if not db.get_admin_client_details(callback_data.user_id):
        await callback.message.edit_text("❌ Клиент не найден.")
        return
    page = callback_data.value if callback_data.action == "page" else 0
    keyboard, current_page = config_list_keyboard(db, callback_data.user_id, page)
    configs = db.get_managed_client_configs(callback_data.user_id)
    await callback.message.edit_text(
        f"🗂 Конфиги клиента {callback_data.user_id}\n\n"
        f"Всего: {len(configs)} · Страница: {current_page + 1}",
        reply_markup=keyboard,
    )


@router.callback_query(AdminConfigCallback.filter(F.action == "view"))
async def show_config_details(
    callback: types.CallbackQuery,
    db: Database,
    safe_answer_callback,
    callback_data: AdminConfigCallback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    config = db.get_client_peer(callback_data.peer_id, callback_data.user_id)
    if not config or config["role"] not in {"primary", "additional"}:
        await callback.message.edit_text("❌ Конфиг не найден.")
        return
    await callback.message.edit_text(
        format_config(config), reply_markup=config_details_keyboard(config)
    )


@router.callback_query(AdminConfigCallback.filter(F.action == "add"))
async def start_additional_config(
    callback: types.CallbackQuery,
    db: Database,
    admin_workflows: AdminWorkflowService,
    safe_answer_callback,
    callback_data: AdminConfigCallback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    if not db.get_primary_client_peer(
        callback_data.user_id
    ) or not db.get_subscription_expiry(callback_data.user_id):
        await callback.message.edit_text(
            "❌ Для создания нужен основной конфиг и установленный срок доступа.",
            reply_markup=client_card_keyboard(callback_data.user_id),
        )
        return
    admin_workflows.set(
        callback.from_user.id,
        "await_config_name",
        user_id=callback_data.user_id,
        service_chat_id=callback.message.chat.id,
        service_message_id=callback.message.message_id,
    )
    await callback.message.edit_text(
        "Введи название нового конфига (1–48 символов).",
        reply_markup=cancel_keyboard(),
    )


@router.callback_query(AdminConfigCallback.filter(F.action == "server"))
async def select_config_server(
    callback: types.CallbackQuery,
    cascade_router: CascadeRouter,
    admin_workflows: AdminWorkflowService,
    safe_answer_callback,
    callback_data: AdminConfigCallback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    flow = admin_workflows.get(callback.from_user.id)
    servers = flow.get("servers", []) if flow else []
    if (
        not flow
        or flow.get("state") != "select_config_server"
        or int(flow.get("user_id", 0)) != callback_data.user_id
        or not 0 <= callback_data.value < len(servers)
    ):
        await callback.message.edit_text("❌ Сценарий создания устарел.")
        return
    server_key = str(servers[callback_data.value])
    try:
        interfaces = await cascade_router.list_server_interfaces(server_key)
    except CascadeError:
        logger.exception("Failed to list Cascade interfaces for %s", server_key)
        await callback.message.edit_text(
            "❌ Не удалось получить интерфейсы сервера.",
            reply_markup=cancel_keyboard(),
        )
        return
    options = [
        {
            "id": str(item.get("id") or ""),
            "name": str(item.get("name") or item.get("address") or "Интерфейс"),
        }
        for item in interfaces
        if item.get("id")
    ]
    if not options:
        await callback.message.edit_text(
            "❌ На сервере нет доступных интерфейсов.",
            reply_markup=cancel_keyboard(),
        )
        return
    admin_workflows.set(
        callback.from_user.id,
        "select_config_interface",
        **{key: value for key, value in flow.items() if key not in {"state", "servers"}},
        server_key=server_key,
        interfaces=options,
    )
    rows = [
        [
            InlineKeyboardButton(
                text=f"{item['name']} · {item['id'][:8]}",
                callback_data=AdminConfigCallback(
                    action="interface",
                    user_id=callback_data.user_id,
                    value=index,
                ).pack(),
            )
        ]
        for index, item in enumerate(options)
    ]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admin_flow_cancel")])
    await callback.message.edit_text(
        f"Выбери интерфейс на сервере {server_key}.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(AdminConfigCallback.filter(F.action == "interface"))
async def select_config_interface(
    callback: types.CallbackQuery,
    admin_workflows: AdminWorkflowService,
    safe_answer_callback,
    callback_data: AdminConfigCallback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    flow = admin_workflows.get(callback.from_user.id)
    interfaces = flow.get("interfaces", []) if flow else []
    if (
        not flow
        or flow.get("state") != "select_config_interface"
        or int(flow.get("user_id", 0)) != callback_data.user_id
        or not 0 <= callback_data.value < len(interfaces)
    ):
        await callback.message.edit_text("❌ Сценарий создания устарел.")
        return
    interface = interfaces[callback_data.value]
    admin_workflows.set(
        callback.from_user.id,
        "confirm_config_create",
        **{
            key: value
            for key, value in flow.items()
            if key not in {"state", "interfaces"}
        },
        interface_id=interface["id"],
        interface_name=interface["name"],
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Создать",
                    callback_data=AdminConfigCallback(
                        action="create",
                        user_id=callback_data.user_id,
                    ).pack(),
                )
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_flow_cancel")],
        ]
    )
    await callback.message.edit_text(
        "Создать дополнительный конфиг?\n\n"
        f"Название: {flow['config_name']}\n"
        f"Сервер: {flow['server_key']}\n"
        f"Интерфейс: {interface['name']} · {interface['id'][:8]}",
        reply_markup=keyboard,
    )


@router.callback_query(AdminConfigCallback.filter(F.action == "create"))
async def confirm_config_create(
    callback: types.CallbackQuery,
    db: Database,
    cascade_router: CascadeRouter,
    admin_workflows: AdminWorkflowService,
    safe_answer_callback,
    callback_data: AdminConfigCallback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    flow = admin_workflows.get(callback.from_user.id)
    if (
        not flow
        or flow.get("state") != "confirm_config_create"
        or int(flow.get("user_id", 0)) != callback_data.user_id
    ):
        await callback.message.edit_text("❌ Сценарий создания устарел.")
        return
    try:
        config = await cascade_router.create_additional_config(
            callback_data.user_id,
            str(flow["config_name"]),
            str(flow["server_key"]),
            str(flow["interface_id"]),
        )
    except CascadeError:
        logger.exception("Failed to create an additional configuration")
        await callback.message.edit_text(
            "❌ Не удалось создать конфиг. Проверь сервер, интерфейс и ёмкость.",
            reply_markup=cancel_keyboard(),
        )
        return
    admin_workflows.clear(callback.from_user.id)
    db.log_admin_config_change(
        callback.from_user.id,
        callback_data.user_id,
        int(config["id"]),
        "admin_create_config",
        server_key=str(config["server_key"]),
    )
    keyboard, _ = config_list_keyboard(db, callback_data.user_id)
    await callback.message.edit_text(
        f"✅ Конфиг «{config['config_name']}» создан.", reply_markup=keyboard
    )


@router.callback_query(AdminConfigCallback.filter(F.action == "rename"))
async def start_config_rename(
    callback: types.CallbackQuery,
    db: Database,
    admin_workflows: AdminWorkflowService,
    safe_answer_callback,
    callback_data: AdminConfigCallback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    config = db.get_client_peer(callback_data.peer_id, callback_data.user_id)
    if not config or config["role"] not in {"primary", "additional"}:
        await callback.message.edit_text("❌ Конфиг не найден.")
        return
    admin_workflows.set(
        callback.from_user.id,
        "await_config_rename",
        user_id=callback_data.user_id,
        peer_id=callback_data.peer_id,
        service_chat_id=callback.message.chat.id,
        service_message_id=callback.message.message_id,
    )
    await callback.message.edit_text(
        "Введи новое название конфига (1–48 символов).",
        reply_markup=cancel_keyboard(),
    )


@router.callback_query(AdminConfigCallback.filter(F.action == "deactivate"))
async def confirm_config_deactivation(
    callback: types.CallbackQuery,
    db: Database,
    safe_answer_callback,
    callback_data: AdminConfigCallback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    config = db.get_client_peer(callback_data.peer_id, callback_data.user_id)
    if not config or config["role"] != "additional":
        await callback.message.edit_text("❌ Дополнительный конфиг не найден.")
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Подтвердить",
                    callback_data=AdminConfigCallback(
                        action="deactivate_confirm",
                        user_id=callback_data.user_id,
                        peer_id=callback_data.peer_id,
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=AdminConfigCallback(
                        action="view",
                        user_id=callback_data.user_id,
                        peer_id=callback_data.peer_id,
                    ).pack(),
                )
            ],
        ]
    )
    await callback.message.edit_text(
        f"Деактивировать конфиг «{config['config_name']}»?\n"
        "Peer останется в Cascade и сможет быть восстановлен.",
        reply_markup=keyboard,
    )


@router.callback_query(
    AdminConfigCallback.filter(F.action.in_({"deactivate_confirm", "restore"}))
)
async def change_config_state(
    callback: types.CallbackQuery,
    db: Database,
    cascade_router: CascadeRouter,
    safe_answer_callback,
    callback_data: AdminConfigCallback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    active = callback_data.action == "restore"
    try:
        config = await cascade_router.set_additional_config_active(
            callback_data.user_id, callback_data.peer_id, active
        )
    except CascadeNotFound:
        await callback.message.edit_text(
            "❌ Peer не найден в Cascade. Создай новый дополнительный конфиг.",
            reply_markup=config_error_back_keyboard(
                callback_data.user_id, callback_data.peer_id
            ),
        )
        return
    except CascadeError:
        logger.exception("Failed to change additional configuration state")
        await callback.message.edit_text(
            "❌ Не удалось изменить состояние конфига.",
            reply_markup=config_error_back_keyboard(
                callback_data.user_id, callback_data.peer_id
            ),
        )
        return
    operation = "admin_restore_config" if active else "admin_deactivate_config"
    db.log_admin_config_change(
        callback.from_user.id,
        callback_data.user_id,
        callback_data.peer_id,
        operation,
        server_key=str(config["server_key"]),
    )
    await callback.message.edit_text(
        "✅ Конфиг восстановлен." if active else "✅ Конфиг деактивирован.",
        reply_markup=config_details_keyboard(config),
    )


@router.message(ActiveAdminWorkflow())
async def capture_admin_input(
    message: types.Message,
    bot: Bot,
    db: Database,
    cascade_router: CascadeRouter,
    admin_workflows: AdminWorkflowService,
) -> None:
    flow = admin_workflows.get(message.from_user.id)
    if not flow:
        return
    state = flow["state"]
    if state in {"await_config_name", "await_config_rename"}:
        try:
            config_name = normalize_config_name(message.text or "")
        except ValueError:
            await bot.edit_message_text(
                chat_id=flow["service_chat_id"],
                message_id=flow["service_message_id"],
                text="Название должно содержать от 1 до 48 символов без управляющих знаков.",
                reply_markup=cancel_keyboard(),
            )
            with suppress(Exception):
                await message.delete()
            return
        existing = db.get_managed_client_configs(int(flow["user_id"]))
        duplicate = next(
            (
                item
                for item in existing
                if str(item.get("config_name") or "").casefold()
                == config_name.casefold()
                and int(item["id"]) != int(flow.get("peer_id", 0))
            ),
            None,
        )
        if duplicate:
            await bot.edit_message_text(
                chat_id=flow["service_chat_id"],
                message_id=flow["service_message_id"],
                text="У этого клиента уже есть конфиг с таким названием.",
                reply_markup=cancel_keyboard(),
            )
            with suppress(Exception):
                await message.delete()
            return
        if state == "await_config_rename":
            peer_id = int(flow["peer_id"])
            if not db.rename_managed_config(peer_id, int(flow["user_id"]), config_name):
                await bot.edit_message_text(
                    chat_id=flow["service_chat_id"],
                    message_id=flow["service_message_id"],
                    text="❌ Не удалось переименовать конфиг.",
                    reply_markup=cancel_keyboard(),
                )
                with suppress(Exception):
                    await message.delete()
                return
            config = db.get_client_peer(peer_id, int(flow["user_id"]))
            admin_workflows.clear(message.from_user.id)
            db.log_admin_config_change(
                message.from_user.id,
                int(flow["user_id"]),
                peer_id,
                "admin_rename_config",
                server_key=str(config["server_key"]) if config else None,
            )
            await bot.edit_message_text(
                chat_id=flow["service_chat_id"],
                message_id=flow["service_message_id"],
                text=f"✅ Конфиг переименован в «{config_name}».",
                reply_markup=config_details_keyboard(config)
                if config
                else admin_dashboard_keyboard(),
            )
        else:
            servers = [
                server.server_key for server in cascade_router.get_enabled_servers()
            ]
            if not servers:
                await bot.edit_message_text(
                    chat_id=flow["service_chat_id"],
                    message_id=flow["service_message_id"],
                    text="❌ Нет активных Cascade-серверов.",
                    reply_markup=cancel_keyboard(),
                )
                with suppress(Exception):
                    await message.delete()
                return
            admin_workflows.set(
                message.from_user.id,
                "select_config_server",
                **{key: value for key, value in flow.items() if key != "state"},
                config_name=config_name,
                servers=servers,
            )
            rows = [
                [
                    InlineKeyboardButton(
                        text=server_key,
                        callback_data=AdminConfigCallback(
                            action="server",
                            user_id=int(flow["user_id"]),
                            value=index,
                        ).pack(),
                    )
                ]
                for index, server_key in enumerate(servers)
            ]
            rows.append(
                [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_flow_cancel")]
            )
            await bot.edit_message_text(
                chat_id=flow["service_chat_id"],
                message_id=flow["service_message_id"],
                text=f"Название: {config_name}\n\nВыбери сервер.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
    elif state == "await_search":
        query = (message.text or "").strip()[:100]
        keyboard, total = client_list_keyboard(
            db, view="details", page=0, query=query
        )
        admin_workflows.clear(message.from_user.id)
        await bot.edit_message_text(
            chat_id=flow["service_chat_id"],
            message_id=flow["service_message_id"],
            text=f"👥 Результаты поиска: {total}",
            reply_markup=keyboard,
        )
    elif state == "await_discount":
        try:
            value = int((message.text or "").strip())
        except ValueError:
            value = -1
        if not 0 <= value <= 90:
            await bot.edit_message_text(
                chat_id=flow["service_chat_id"],
                message_id=flow["service_message_id"],
                text="Скидка должна быть целым числом от 0 до 90.",
                reply_markup=cancel_keyboard(),
            )
            with suppress(Exception):
                await message.delete()
            return
        client = db.get_admin_client_details(int(flow["user_id"]))
        if client and db.set_client_promo(int(flow["user_id"]), value):
            db.log_admin_promo_change(
                message.from_user.id,
                int(flow["user_id"]),
                client.get("server_key"),
                int(client.get("promo") or 0),
                value,
            )
        admin_workflows.clear(message.from_user.id)
        await bot.edit_message_text(
            chat_id=flow["service_chat_id"],
            message_id=flow["service_message_id"],
            text=f"✅ Скидка {value}% сохранена.",
            reply_markup=client_card_keyboard(int(flow["user_id"])),
        )
    elif state == "await_refund_charge":
        charge_id = (message.text or "").strip()[:200]
        payment = await asyncio.to_thread(db.get_payment_by_telegram_charge, charge_id)
        if not payment or payment["payment_method"] != "stars":
            await bot.edit_message_text(
                chat_id=flow["service_chat_id"],
                message_id=flow["service_message_id"],
                text="❌ Платеж Telegram Stars не найден. Введи другой charge ID.",
                reply_markup=cancel_keyboard(),
            )
            with suppress(Exception):
                await message.delete()
            return
        later_payments = [
            item
            for item in await asyncio.to_thread(db.list_recent_payments, 100)
            if item["user_id"] == payment["user_id"] and item["id"] > payment["id"]
        ]
        admin_workflows.clear(message.from_user.id)
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Подтвердить возврат",
                        callback_data=RefundConfirmationCallback(
                            payment_id=payment["payment_id"]
                        ).pack(),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Отмена", callback_data="admin_flow_cancel"
                    )
                ],
            ]
        )
        await bot.edit_message_text(
            chat_id=flow["service_chat_id"],
            message_id=flow["service_message_id"],
            text=(
                "Подтвердить возврат Telegram Stars?\n\n"
                f"Telegram ID: {payment['user_id']}\n"
                f"Сумма: {payment['amount']} Stars\n"
                f"Тариф: {payment['tariff_key']}\n"
                f"Более поздних платежей: {len(later_payments)}\n\n"
                "VPN-доступ автоматически изменен не будет."
            ),
            reply_markup=keyboard,
        )
    elif state == "await_message":
        admin_workflows.set(
            message.from_user.id,
            "confirm_message",
            **{key: value for key, value in flow.items() if key != "state"},
            source_chat_id=message.chat.id,
            source_message_id=message.message_id,
        )
        await bot.edit_message_text(
            chat_id=flow["service_chat_id"],
            message_id=flow["service_message_id"],
            text="Отправить это сообщение?",
            reply_markup=confirm_keyboard(),
        )
    with suppress(Exception):
        if state != "await_message":
            await message.delete()


@router.callback_query(F.data == "admin_flow_cancel")
@router.callback_query(F.data == "admin_broadcast_cancel")
@router.callback_query(F.data == "admin_message_cancel")
@router.callback_query(F.data == "admin_discount_cancel")
async def cancel_flow(
    callback: types.CallbackQuery,
    admin_workflows: AdminWorkflowService,
    safe_answer_callback,
) -> None:
    await safe_answer_callback(callback)
    flow = admin_workflows.get(callback.from_user.id)
    admin_workflows.clear(callback.from_user.id)
    if flow and flow.get("source_chat_id") and flow.get("source_message_id"):
        with suppress(Exception):
            await callback.bot.delete_message(
                flow["source_chat_id"], flow["source_message_id"]
            )
    reply_markup = admin_dashboard_keyboard()
    if flow and flow.get("user_id") and "config" in str(flow.get("state", "")):
        reply_markup = client_card_keyboard(int(flow["user_id"]))
    await callback.message.edit_text(
        "Действие отменено.", reply_markup=reply_markup
    )


@router.callback_query(F.data == "admin_flow_confirm")
@router.callback_query(F.data == "admin_broadcast_confirm")
async def confirm_flow(
    callback: types.CallbackQuery,
    bot: Bot,
    db: Database,
    admin_workflows: AdminWorkflowService,
    telegram_sender,
    safe_answer_callback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    flow = admin_workflows.get(callback.from_user.id)
    if not flow or flow["state"] != "confirm_message":
        await callback.message.edit_text("Нет подготовленного сообщения.")
        return
    recipients = (
        db.get_client_telegram_ids()
        if flow["mode"] == "all"
        else [int(flow["recipient_id"])]
    )
    admin_workflows.clear(callback.from_user.id)
    await callback.message.edit_text(f"📣 Отправка запущена. Получателей: {len(recipients)}")
    sent = 0
    failed = 0
    for recipient_id in recipients:
        result = await telegram_sender.call(
            recipient_id,
            lambda recipient_id=recipient_id: bot.copy_message(
                chat_id=recipient_id,
                from_chat_id=flow["source_chat_id"],
                message_id=flow["source_message_id"],
            ),
        )
        if result is None:
            failed += 1
        else:
            sent += 1
        await asyncio.sleep(0.07)
    with suppress(Exception):
        await bot.delete_message(flow["source_chat_id"], flow["source_message_id"])
    await callback.message.edit_text(
        f"📣 Рассылка завершена.\n\nОтправлено: {sent}\nНе доставлено: {failed}",
        reply_markup=admin_dashboard_keyboard(),
    )
