import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router, types
from aiogram.filters import BaseFilter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.deep_linking import create_start_link

from callbacks import (
    AdminClientCallback,
    AdminConfigCallback,
    AdminDiscountCallback,
    AdminInviteCallback,
    AdminPageCallback,
    RefundConfirmationCallback,
)
from cascade_api import CascadeError, CascadeNotFound, CascadeRouter
from config import get_admin_telegram_ids
from database import Database, normalize_config_name
from telegram_runtime import edit_bound_message, edit_telegram_text
from telegram_text import TelegramText, ensure_telegram_text, rich_date
from utils import format_date_for_user, location_config_filename

logger = logging.getLogger(__name__)
router = Router(name="admin")
ADMIN_CLIENTS_PAGE_SIZE = 8
ADMIN_CONFIGS_PAGE_SIZE = 6
ADMIN_WORKFLOW_TYPE = "admin_flow"
MOSCOW_TIMEZONE = ZoneInfo("Europe/Moscow")


def parse_admin_expiry_input(value: str) -> str:
    """Parse an administrator-entered Moscow date into the stored UTC format."""
    normalized = " ".join(value.split())
    for date_format, default_end_of_day in (
        ("%d-%m-%Y %H:%M", False),
        ("%d-%m-%Y", True),
    ):
        try:
            parsed = datetime.strptime(normalized, date_format)
            if default_end_of_day:
                parsed = parsed.replace(hour=23, minute=59)
            return (
                parsed.replace(tzinfo=MOSCOW_TIMEZONE)
                .astimezone(UTC)
                .replace(tzinfo=None)
                .strftime("%Y-%m-%d %H:%M:%S")
            )
        except OverflowError, ValueError:
            continue
    raise ValueError("Unsupported expiry date format")


def format_admin_expiry(value: str | None) -> str:
    """Format a stored UTC expiry for an administrator in Moscow time."""
    if not value:
        return "нет"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(MOSCOW_TIMEZONE).strftime("%d-%m-%Y %H:%M")


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
    async def __call__(self, message: types.Message, admin_workflows: AdminWorkflowService) -> bool:
        command = (message.text or "").split(maxsplit=1)[0].casefold()
        if command == "/start" or command.startswith("/start@"):
            return False
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
            [InlineKeyboardButton(text="👥 Клиенты и скидки", callback_data="admin_client_list")],
            [InlineKeyboardButton(text="📣 Рассылка", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="💳 Платежи и расхождения", callback_data="admin_payments")],
            [InlineKeyboardButton(text="⭐ Сверить Stars", callback_data="admin_stars_reconcile")],
            [InlineKeyboardButton(text="↩️ Возврат Stars", callback_data="admin_refund_stars")],
            [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="main")],
        ]
    )


def client_management_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👥 Клиенты", callback_data="admin_client_list")],
            [InlineKeyboardButton(text="➕ Добавить клиента", callback_data="admin_add_client")],
            [
                InlineKeyboardButton(
                    text="⏳ Ожидают привязки", callback_data="admin_pending_clients"
                )
            ],
            [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="main")],
        ]
    )


def pending_clients_keyboard(db: Database) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=(
                    f"{'🟢' if item['display_status'] == 'claim_pending' else '⏳'} "
                    f"@{item['expected_username']}"
                ),
                callback_data=AdminInviteCallback(
                    action="view", invitation_id=int(item["id"])
                ).pack(),
            )
        ]
        for item in db.list_client_invitations()
    ]
    rows.append(
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_manage_clients")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def invitation_details_keyboard(invitation: dict[str, Any]) -> InlineKeyboardMarkup:
    invitation_id = int(invitation["id"])
    rows: list[list[InlineKeyboardButton]] = []
    if invitation["status"] == "claim_pending":
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        text="✅ Подтвердить привязку",
                        callback_data=AdminInviteCallback(
                            action="approve", invitation_id=invitation_id
                        ).pack(),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Отклонить заявку",
                        callback_data=AdminInviteCallback(
                            action="reject", invitation_id=invitation_id
                        ).pack(),
                    )
                ],
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔄 Перевыпустить ссылку",
                    callback_data=AdminInviteCallback(
                        action="reissue", invitation_id=invitation_id
                    ).pack(),
                )
            ]
        )
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="💸 Скидка",
                    callback_data=AdminInviteCallback(
                        action="discount", invitation_id=invitation_id
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text=(
                        "🎁 Убрать free"
                        if invitation["is_complimentary"]
                        else "🎁 Сделать free"
                    ),
                    callback_data=AdminInviteCallback(
                        action="complimentary", invitation_id=invitation_id
                    ).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Удалить приглашение",
                    callback_data=AdminInviteCallback(
                        action="delete", invitation_id=invitation_id
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ К ожидающим", callback_data="admin_pending_clients"
                )
            ],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_invitation(invitation: dict[str, Any], link: str | None = None) -> str:
    status = {
        "pending": "ожидает открытия ссылки",
        "claim_pending": "ожидает подтверждения администратора",
        "rejected": "заявка отклонена",
        "expired": "ссылка истекла",
    }.get(str(invitation.get("display_status") or invitation["status"]), "неизвестен")
    text = (
        "⏳ Предварительный клиент\n\n"
        f"Username: @{invitation['expected_username']}\n"
        f"Статус: {status}\n"
        f"Скидка: {int(invitation['promo'])}%\n"
        f"Бесплатный доступ: {'да' if invitation['is_complimentary'] else 'нет'}"
    )
    if invitation.get("claimant_user_id"):
        text += (
            f"\n\nЗаявка от ID: {invitation['claimant_user_id']}\n"
            f"Фактический username: @{invitation.get('claimant_username') or 'нет'}"
        )
    if link:
        text += f"\n\nОдноразовая ссылка (7 дней):\n{link}"
    return text


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
    clients, total = db.get_admin_clients_page(page, ADMIN_CLIENTS_PAGE_SIZE, query=query)
    pages = max(1, (total + ADMIN_CLIENTS_PAGE_SIZE - 1) // ADMIN_CLIENTS_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    rows: list[list[InlineKeyboardButton]] = []
    for client in clients:
        user_id = int(client["telegram_user_id"])
        username = str(client.get("telegram_username") or "")
        label = f"{user_id} | @{username}" if username else str(user_id)
        if client.get("is_banned"):
            label = f"🚫 {label}"
        elif client.get("is_complimentary"):
            label = f"🎁 {label}"
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
        [InlineKeyboardButton(text="⬅️ Управление клиентами", callback_data="admin_manage_clients")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows), total


def discount_keyboard(
    user_id: int, is_complimentary: bool = False
) -> InlineKeyboardMarkup:
    rows = []
    for values in ((0, 5, 10), (15, 20, 25)):
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{value}%",
                    callback_data=AdminDiscountCallback(user_id=user_id, value=value).pack(),
                )
                for value in values
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="✏️ Другое значение",
                callback_data=AdminClientCallback(action="custom_discount", user_id=user_id).pack(),
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text=(
                    "🎁 Отменить бесплатный доступ"
                    if is_complimentary
                    else "🎁 Сделать бесплатным"
                ),
                callback_data=AdminClientCallback(
                    action="complimentary", user_id=user_id
                ).pack(),
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ К клиенту",
                callback_data=AdminClientCallback(action="details", user_id=user_id).pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def client_card_keyboard(user_id: int, is_banned: bool = False) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="♻️ Разбанить" if is_banned else "🚫 Забанить",
                    callback_data=AdminClientCallback(
                        action="unban" if is_banned else "ban", user_id=user_id
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="💸 Скидка",
                    callback_data=AdminClientCallback(action="discount", user_id=user_id).pack(),
                ),
                InlineKeyboardButton(
                    text="🗂 Конфиги",
                    callback_data=AdminConfigCallback(action="list", user_id=user_id).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📅 Срок доступа",
                    callback_data=AdminClientCallback(action="expiry", user_id=user_id).pack(),
                ),
                InlineKeyboardButton(
                    text="👥 Группа",
                    callback_data=AdminClientCallback(action="group", user_id=user_id).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Удалить клиента",
                    callback_data=AdminClientCallback(action="delete", user_id=user_id).pack(),
                )
            ],
            [InlineKeyboardButton(text="⬅️ К списку", callback_data="admin_client_list")],
        ]
    )


def config_list_keyboard(
    db: Database, user_id: int, page: int = 0
) -> tuple[InlineKeyboardMarkup, int]:
    configs = db.get_admin_client_configs(user_id)
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
                    text=f"{status} {config_display_name(config)}",
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
                    callback_data=AdminConfigCallback(action="add", user_id=user_id).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ К клиенту",
                    callback_data=AdminClientCallback(action="details", user_id=user_id).pack(),
                )
            ],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows), page


def config_details_keyboard(config: dict[str, Any]) -> InlineKeyboardMarkup:
    user_id = int(config["telegram_user_id"])
    peer_id = int(config["id"])
    can_download = bool(
        config.get(
            "has_active_access",
            config.get("payment_status") == "paid" or config.get("is_complimentary"),
        )
    ) and not config.get("is_banned")
    rows = [
        *(
            [
                [
                    InlineKeyboardButton(
                        text="📥 Скачать конфиг",
                        callback_data=AdminConfigCallback(
                            action="download", user_id=user_id, peer_id=peer_id
                        ).pack(),
                    )
                ]
            ]
            if can_download
            else []
        ),
        *(
            [
                [
                    InlineKeyboardButton(
                        text="✏️ Переименовать",
                        callback_data=AdminConfigCallback(
                            action="rename", user_id=user_id, peer_id=peer_id
                        ).pack(),
                    )
                ]
            ]
            if config["role"] in {"primary", "additional"}
            else []
        ),
    ]
    if config["role"] == "additional":
        action = "deactivate" if config["admin_enabled"] else "restore"
        text = "⏸ Деактивировать" if config["admin_enabled"] else "♻️ Восстановить"
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
                    text="🗑 Удалить навсегда",
                    callback_data=AdminConfigCallback(
                        action="delete", user_id=user_id, peer_id=peer_id
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


def config_display_name(config: dict[str, Any]) -> str:
    """Return a stable display name for managed and historical configs."""
    return str(config.get("config_name") or config.get("peer_name") or "Конфиг")


def format_config(config: dict[str, Any], server_name: str | None = None) -> str:
    if not config["admin_enabled"]:
        status = "деактивирован"
    elif config["enabled"]:
        status = "активен"
    else:
        status = "недоступен или срок истёк"
    server_key = str(config["server_key"])
    server_label = (
        f"{server_name} ({server_key})" if server_name and server_name != server_key else server_key
    )
    role_label = {
        "primary": "основной",
        "additional": "дополнительный",
    }.get(str(config["role"]), str(config["role"]))
    return (
        f"🗂 {config_display_name(config)}\n\n"
        f"Тип: {role_label}\n"
        f"Сервер: {server_label}\n"
        f"Интерфейс: {config['interface_id']}\n"
        f"Группа: {config.get('client_group') or 'не подтверждена'}\n"
        f"Состояние: {status}"
    )


def client_group_label(client: dict[str, Any]) -> str:
    """Describe the verified group state of all managed client configs."""
    groups = [
        value.strip()
        for value in str(client.get("client_groups") or "").split(",")
        if value.strip()
    ]
    if int(client.get("unknown_group_count") or 0):
        return "не подтверждена"
    if len(groups) > 1:
        return f"несогласовано: {', '.join(groups)}"
    return groups[0] if groups else "не подтверждена"


def confirmed_managed_client_group(configs: list[dict[str, Any]]) -> str | None:
    """Return the single confirmed group shared by all managed client peers."""
    if not configs:
        return None
    groups: dict[str, str] = {}
    for config in configs:
        group_name = str(config.get("client_group") or "").strip()
        if not group_name:
            return None
        groups.setdefault(group_name.casefold(), group_name)
    return next(iter(groups.values())) if len(groups) == 1 else None


def format_client(client: dict[str, Any]) -> TelegramText:
    username = str(client.get("telegram_username") or "")
    identity = f"@{username}" if username else "без username"
    expiry = client.get("expire_date")
    formatted_expiry = format_date_for_user(expiry) if expiry else "нет"
    banned = bool(client.get("is_banned"))
    ban_status = "забанен" if banned else "не забанен"
    plain = (
        "👤 Клиент\n\n"
        f"Telegram ID: {client['telegram_user_id']}\n"
        f"Username: {identity}\n"
        f"Скидка: {int(client.get('promo') or 0)}%\n"
        f"Бесплатный доступ: {'да' if client.get('is_complimentary') else 'нет'}\n"
        f"Сервер: {client.get('server_keys') or 'не назначен'}\n"
        f"Группа: {client_group_label(client)}\n"
        f"Устройств: {int(client.get('device_count') or 0)}\n"
        f"Доступ до: {formatted_expiry}\n"
        f"Бан: {ban_status}"
    )
    if banned:
        banned_at = str(client.get("banned_at") or "")
        formatted_banned_at = (
            format_date_for_user(banned_at) if banned_at else "неизвестно"
        )
        plain += (
            f"\nВремя бана: {formatted_banned_at}"
            f"\nЗабанил: {client.get('banned_by') or 'неизвестно'}"
            f"\nПричина: {client.get('ban_reason') or 'не указана'}"
        )
    if not expiry:
        return TelegramText.from_plain(plain)
    return TelegramText.from_plain_with_replacements(
        plain,
        {formatted_expiry: rich_date(expiry, formatted_expiry)},
    )


@router.callback_query(F.data == "admin_broadcast")
async def open_broadcast(callback: types.CallbackQuery, safe_answer_callback) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    await edit_bound_message(
        callback.message, "📣 Рассылка", reply_markup=broadcast_menu_keyboard()
    )


@router.callback_query(F.data == "admin_manage_clients")
async def open_clients(callback: types.CallbackQuery, db: Database, safe_answer_callback) -> None:
    if not is_admin(callback.from_user.id):
        await safe_answer_callback(callback, "❌ Недостаточно прав.")
        return
    await safe_answer_callback(callback)
    await edit_bound_message(
        callback.message,
        "👥 Управление клиентами",
        reply_markup=client_management_keyboard(),
    )


@router.callback_query(F.data == "admin_add_client")
async def start_add_client(
    callback: types.CallbackQuery,
    admin_workflows: AdminWorkflowService,
    safe_answer_callback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    admin_workflows.set(
        callback.from_user.id,
        "await_client_identity",
        service_chat_id=callback.message.chat.id,
        service_message_id=callback.message.message_id,
    )
    await edit_bound_message(
        callback.message,
        "Введи числовой Telegram ID или @username клиента.",
        reply_markup=cancel_keyboard(),
    )


@router.callback_query(F.data == "admin_pending_clients")
async def show_pending_clients(
    callback: types.CallbackQuery, db: Database, safe_answer_callback
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    invitations = db.list_client_invitations()
    await edit_bound_message(
        callback.message,
        f"⏳ Ожидают привязки\n\nЗаписей: {len(invitations)}",
        reply_markup=pending_clients_keyboard(db),
    )


@router.callback_query(AdminInviteCallback.filter(F.action == "view"))
async def show_invitation(
    callback: types.CallbackQuery,
    db: Database,
    safe_answer_callback,
    callback_data: AdminInviteCallback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    invitation = db.get_client_invitation(callback_data.invitation_id)
    if not invitation or invitation["status"] == "claimed":
        await edit_bound_message(callback.message, "❌ Приглашение не найдено.")
        return
    link = None
    if invitation.get("token") and invitation.get("display_status") == "pending":
        link = await create_start_link(
            callback.bot, f"claim_{invitation['token']}", encode=False
        )
    await edit_bound_message(
        callback.message,
        format_invitation(invitation, link),
        reply_markup=invitation_details_keyboard(invitation),
    )


@router.callback_query(AdminInviteCallback.filter(F.action == "reissue"))
async def reissue_invitation(
    callback: types.CallbackQuery,
    db: Database,
    safe_answer_callback,
    callback_data: AdminInviteCallback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    invitation = db.reissue_client_invitation(
        callback_data.invitation_id, callback.from_user.id
    )
    if not invitation:
        await edit_bound_message(callback.message, "❌ Приглашение не найдено.")
        return
    link = await create_start_link(
        callback.bot, f"claim_{invitation['token']}", encode=False
    )
    await edit_bound_message(
        callback.message,
        format_invitation(invitation, link),
        reply_markup=invitation_details_keyboard(invitation),
    )


@router.callback_query(AdminInviteCallback.filter(F.action == "approve"))
async def approve_invitation(
    callback: types.CallbackQuery,
    db: Database,
    cascade_router: CascadeRouter,
    safe_answer_callback,
    callback_data: AdminInviteCallback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    client = db.approve_client_invitation(
        callback_data.invitation_id, callback.from_user.id
    )
    if not client:
        await edit_bound_message(callback.message, "❌ Заявка устарела.")
        return
    warning = ""
    if client.get("is_complimentary"):
        sync_result = {"total": 1, "updated": 0, "missing": 0, "failed": 1}
        try:
            result = await cascade_router.ensure_client_access(
                int(client["telegram_user_id"])
            )
            sync_result = result
            if result["failed"]:
                raise CascadeError("Complimentary provisioning failed")
        except CascadeError as exc:
            db.add_provisioning_task(
                int(client["telegram_user_id"]),
                "create_peer",
                {
                    "username": client.get("telegram_username") or "",
                    "peer_name": client.get("telegram_username")
                    or str(client["telegram_user_id"]),
                    "expire_date": "2099-12-31 23:59:59",
                    "tariff_key": "complimentary",
                },
                str(exc),
            )
            warning = "\n⚠️ Создание VPN поставлено в очередь."
        db.log_client_state_sync(
            callback.from_user.id,
            int(client["telegram_user_id"]),
            "admin_approve_complimentary_invitation_sync",
            sync_result,
        )
    await edit_bound_message(
        callback.message,
        f"✅ Клиент привязан к Telegram ID {client['telegram_user_id']}.{warning}",
        reply_markup=client_card_keyboard(
            int(client["telegram_user_id"]), bool(client.get("is_banned"))
        ),
    )


@router.callback_query(AdminInviteCallback.filter(F.action == "reject"))
async def reject_invitation(
    callback: types.CallbackQuery,
    db: Database,
    safe_answer_callback,
    callback_data: AdminInviteCallback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    if not db.reject_client_invitation(
        callback_data.invitation_id, callback.from_user.id
    ):
        await edit_bound_message(callback.message, "❌ Заявка устарела.")
        return
    invitation = db.get_client_invitation(callback_data.invitation_id)
    await edit_bound_message(
        callback.message,
        "✅ Заявка отклонена. Для новой попытки перевыпусти ссылку.",
        reply_markup=invitation_details_keyboard(invitation)
        if invitation
        else pending_clients_keyboard(db),
    )


@router.callback_query(AdminInviteCallback.filter(F.action == "delete"))
async def delete_invitation(
    callback: types.CallbackQuery,
    db: Database,
    safe_answer_callback,
    callback_data: AdminInviteCallback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    db.delete_client_invitation(callback_data.invitation_id, callback.from_user.id)
    await edit_bound_message(
        callback.message,
        "✅ Приглашение удалено.",
        reply_markup=pending_clients_keyboard(db),
    )


@router.callback_query(AdminInviteCallback.filter(F.action == "discount"))
async def show_invitation_discount(
    callback: types.CallbackQuery,
    db: Database,
    safe_answer_callback,
    callback_data: AdminInviteCallback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    invitation = db.get_client_invitation(callback_data.invitation_id)
    if not invitation:
        await edit_bound_message(callback.message, "❌ Приглашение не найдено.")
        return
    rows = [
        [
            InlineKeyboardButton(
                text=f"{value}%",
                callback_data=AdminInviteCallback(
                    action="discount_set",
                    invitation_id=callback_data.invitation_id,
                    value=value,
                ).pack(),
            )
            for value in values
        ]
        for values in ((0, 10, 25), (30, 50, 75))
    ]
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="✏️ Другое значение",
                    callback_data=AdminInviteCallback(
                        action="discount_custom",
                        invitation_id=callback_data.invitation_id,
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=AdminInviteCallback(
                        action="view", invitation_id=callback_data.invitation_id
                    ).pack(),
                )
            ],
        ]
    )
    await edit_bound_message(
        callback.message,
        f"Скидка для @{invitation['expected_username']}: {invitation['promo']}%",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(AdminInviteCallback.filter(F.action == "discount_set"))
async def set_invitation_discount(
    callback: types.CallbackQuery,
    db: Database,
    safe_answer_callback,
    callback_data: AdminInviteCallback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id) or not db.set_invitation_promo(
        callback_data.invitation_id, callback.from_user.id, callback_data.value
    ):
        await edit_bound_message(callback.message, "❌ Не удалось сохранить скидку.")
        return
    invitation = db.get_client_invitation(callback_data.invitation_id)
    await edit_bound_message(
        callback.message,
        f"✅ Скидка {callback_data.value}% сохранена.",
        reply_markup=invitation_details_keyboard(invitation),
    )


@router.callback_query(AdminInviteCallback.filter(F.action == "discount_custom"))
async def request_invitation_discount(
    callback: types.CallbackQuery,
    admin_workflows: AdminWorkflowService,
    safe_answer_callback,
    callback_data: AdminInviteCallback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    admin_workflows.set(
        callback.from_user.id,
        "await_invitation_discount",
        invitation_id=callback_data.invitation_id,
        service_chat_id=callback.message.chat.id,
        service_message_id=callback.message.message_id,
    )
    await edit_bound_message(
        callback.message,
        "Введи скидку целым числом от 0 до 90.",
        reply_markup=cancel_keyboard(),
    )


@router.callback_query(AdminInviteCallback.filter(F.action == "complimentary"))
async def confirm_invitation_complimentary(
    callback: types.CallbackQuery,
    db: Database,
    safe_answer_callback,
    callback_data: AdminInviteCallback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    invitation = db.get_client_invitation(callback_data.invitation_id)
    if not invitation:
        await edit_bound_message(callback.message, "❌ Приглашение не найдено.")
        return
    enabled = not bool(invitation["is_complimentary"])
    await edit_bound_message(
        callback.message,
        ("Назначить бесплатный доступ?" if enabled else "Отменить бесплатный доступ?"),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Подтвердить",
                        callback_data=AdminInviteCallback(
                            action="complimentary_confirm",
                            invitation_id=callback_data.invitation_id,
                            value=int(enabled),
                        ).pack(),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад",
                        callback_data=AdminInviteCallback(
                            action="view", invitation_id=callback_data.invitation_id
                        ).pack(),
                    )
                ],
            ]
        ),
    )


@router.callback_query(
    AdminInviteCallback.filter(F.action == "complimentary_confirm")
)
async def set_invitation_complimentary(
    callback: types.CallbackQuery,
    db: Database,
    safe_answer_callback,
    callback_data: AdminInviteCallback,
) -> None:
    await safe_answer_callback(callback)
    enabled = bool(callback_data.value)
    if not is_admin(callback.from_user.id) or not db.set_invitation_complimentary(
        callback_data.invitation_id, callback.from_user.id, enabled
    ):
        await edit_bound_message(callback.message, "❌ Не удалось изменить доступ.")
        return
    invitation = db.get_client_invitation(callback_data.invitation_id)
    await edit_bound_message(
        callback.message,
        "✅ Бесплатный статус обновлён.",
        reply_markup=invitation_details_keyboard(invitation),
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
    await edit_bound_message(
        callback.message, f"👥 Клиенты и скидки\n\nНайдено: {total}", reply_markup=keyboard
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
    await edit_bound_message(
        callback.message,
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
    await edit_bound_message(
        callback.message, f"👤 Выбери получателя\n\nНайдено: {total}", reply_markup=keyboard
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
    await edit_bound_message(
        callback.message, f"👥 Клиенты\n\nНайдено: {total}", reply_markup=keyboard
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
        await edit_bound_message(
            callback.message, "❌ Клиент не найден.", reply_markup=admin_dashboard_keyboard()
        )
        return
    await edit_bound_message(
        callback.message,
        format_client(client),
        reply_markup=client_card_keyboard(
            callback_data.user_id, bool(client.get("is_banned"))
        ),
    )


@router.callback_query(AdminClientCallback.filter(F.action == "delete"))
async def confirm_client_deletion(
    callback: types.CallbackQuery,
    db: Database,
    safe_answer_callback,
    callback_data: AdminClientCallback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    if callback.from_user.id == callback_data.user_id:
        await edit_bound_message(
            callback.message,
            "❌ Нельзя удалить собственный профиль администратора.",
            reply_markup=client_card_keyboard(callback_data.user_id),
        )
        return
    client = db.get_admin_client_details(callback_data.user_id)
    if not client:
        await edit_bound_message(
            callback.message, "❌ Клиент не найден.", reply_markup=admin_dashboard_keyboard()
        )
        return
    username = str(client.get("telegram_username") or "")
    identity = f"@{username}" if username else "без username"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Удалить навсегда",
                    callback_data=AdminClientCallback(
                        action="delete_confirm", user_id=callback_data.user_id
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=AdminClientCallback(
                        action="details", user_id=callback_data.user_id
                    ).pack(),
                )
            ],
        ]
    )
    await edit_bound_message(
        callback.message,
        "Удалить клиента навсегда?\n\n"
        f"Telegram ID: {callback_data.user_id}\n"
        f"Клиент: {identity}\n"
        f"Конфигов: {int(client.get('device_count') or 0)}\n\n"
        "Все peer'ы будут удалены из Cascade, а профиль, подписка и конфиги — "
        "из базы данных. Платёжная история и аудит сохранятся. "
        "Это действие нельзя отменить.",
        reply_markup=keyboard,
    )


@router.callback_query(AdminClientCallback.filter(F.action.in_({"ban", "unban"})))
async def show_client_ban_confirmation(
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
        await edit_bound_message(callback.message, "❌ Клиент не найден.")
        return
    if callback_data.user_id == callback.from_user.id:
        await edit_bound_message(
            callback.message,
            "❌ Нельзя забанить собственный Telegram ID.",
            reply_markup=client_card_keyboard(
                callback_data.user_id, bool(client.get("is_banned"))
            ),
        )
        return
    if callback_data.action == "unban":
        rows = [
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить разбан",
                    callback_data=AdminClientCallback(
                        action="unban_confirm", user_id=callback_data.user_id
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=AdminClientCallback(
                        action="details", user_id=callback_data.user_id
                    ).pack(),
                )
            ],
        ]
        text = f"♻️ Разбанить клиента {callback_data.user_id}?"
    else:
        rows = [
            [
                InlineKeyboardButton(
                    text="🚫 Забанить без причины",
                    callback_data=AdminClientCallback(
                        action="ban_confirm", user_id=callback_data.user_id
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Указать причину",
                    callback_data=AdminClientCallback(
                        action="ban_reason", user_id=callback_data.user_id
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=AdminClientCallback(
                        action="details", user_id=callback_data.user_id
                    ).pack(),
                )
            ],
        ]
        text = (
            f"🚫 Забанить клиента {callback_data.user_id}?\n\n"
            "Бот станет недоступен, а все VPN-конфиги будут отключены."
        )
    await edit_bound_message(
        callback.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@router.callback_query(AdminClientCallback.filter(F.action == "ban_reason"))
async def request_client_ban_reason(
    callback: types.CallbackQuery,
    db: Database,
    admin_workflows: AdminWorkflowService,
    safe_answer_callback,
    callback_data: AdminClientCallback,
) -> None:
    await safe_answer_callback(callback)
    if (
        not is_admin(callback.from_user.id)
        or callback_data.user_id == callback.from_user.id
        or not db.get_admin_client_details(callback_data.user_id)
    ):
        return
    admin_workflows.set(
        callback.from_user.id,
        "await_ban_reason",
        user_id=callback_data.user_id,
        service_chat_id=callback.message.chat.id,
        service_message_id=callback.message.message_id,
    )
    await edit_bound_message(
        callback.message,
        "Введи причину бана (до 500 символов). Клиент её не увидит.",
        reply_markup=cancel_keyboard(),
    )


@router.callback_query(
    AdminClientCallback.filter(F.action.in_({"ban_confirm", "unban_confirm"}))
)
async def apply_client_ban(
    callback: types.CallbackQuery,
    db: Database,
    cascade_router: CascadeRouter,
    admin_workflows: AdminWorkflowService,
    safe_answer_callback,
    callback_data: AdminClientCallback,
) -> None:
    await safe_answer_callback(callback)
    admin_id = callback.from_user.id
    banned = callback_data.action == "ban_confirm"
    if not is_admin(admin_id):
        return
    client = db.get_admin_client_details(callback_data.user_id)
    if not client:
        await edit_bound_message(callback.message, "❌ Клиент не найден.")
        return
    if callback_data.user_id == admin_id:
        await edit_bound_message(callback.message, "❌ Нельзя забанить себя.")
        return
    flow = admin_workflows.get(admin_id)
    reason = None
    if banned and flow and flow.get("state") == "confirm_ban":
        if int(flow.get("user_id", 0)) != callback_data.user_id:
            await edit_bound_message(callback.message, "❌ Подтверждение устарело.")
            return
        reason = str(flow.get("reason") or "") or None
    admin_workflows.clear(admin_id)
    try:
        result = await cascade_router.set_client_ban(
            callback_data.user_id, admin_id, banned, reason
        )
    except CascadeNotFound:
        await edit_bound_message(callback.message, "❌ Клиент не найден.")
        return
    if result["failed"]:
        primary = db.get_primary_client_peer(callback_data.user_id)
        operation = "sync_client_state" if primary else "create_peer"
        client = db.get_admin_client_details(callback_data.user_id) or {}
        db.add_provisioning_task(
            callback_data.user_id,
            operation,
            {}
            if primary
            else {
                "username": client.get("telegram_username") or "",
                "peer_name": client.get("telegram_username")
                or str(callback_data.user_id),
                "expire_date": "2099-12-31 23:59:59",
                "tariff_key": "complimentary",
            },
            f"Failed peers: {result['failed']}",
        )
    state = "забанен" if banned else "разбанен"
    warning = (
        "\n⚠️ Часть VPN-конфигов будет синхронизирована автоматически."
        if result["failed"]
        else ""
    )
    await edit_bound_message(
        callback.message,
        f"✅ Клиент {state}.\n\n"
        f"Обновлено: {result['updated']} · Отсутствует: {result['missing']} · "
        f"Ошибок: {result['failed']}{warning}",
        reply_markup=client_card_keyboard(callback_data.user_id, banned),
    )


@router.callback_query(AdminClientCallback.filter(F.action == "delete_confirm"))
async def delete_client(
    callback: types.CallbackQuery,
    db: Database,
    cascade_router: CascadeRouter,
    safe_answer_callback,
    callback_data: AdminClientCallback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    if callback.from_user.id == callback_data.user_id:
        await edit_bound_message(
            callback.message,
            "❌ Нельзя удалить собственный профиль администратора.",
            reply_markup=client_card_keyboard(callback_data.user_id),
        )
        return
    if not db.get_admin_client_details(callback_data.user_id):
        await edit_bound_message(
            callback.message, "❌ Клиент не найден.", reply_markup=admin_dashboard_keyboard()
        )
        return
    try:
        result = await cascade_router.delete_client(
            callback_data.user_id, callback.from_user.id
        )
    except CascadeNotFound:
        await edit_bound_message(
            callback.message, "❌ Клиент не найден.", reply_markup=admin_dashboard_keyboard()
        )
        return
    except CascadeError:
        logger.exception("Failed to delete client")
        await edit_bound_message(
            callback.message,
            "❌ Не удалось завершить удаление клиента. Локальные данные сохранены; "
            "попробуй повторить.",
            reply_markup=client_card_keyboard(callback_data.user_id),
        )
        return
    if result.failed:
        await edit_bound_message(
            callback.message,
            "❌ Не все peer'ы удалось удалить из Cascade. Локальные данные сохранены.\n\n"
            f"Удалено: {result.deleted}\n"
            f"Уже отсутствовало: {result.already_missing}\n"
            f"Ошибок: {result.failed}\n\n"
            "Повтори удаление, когда серверы будут доступны.",
            reply_markup=client_card_keyboard(callback_data.user_id),
        )
        return
    keyboard, total = client_list_keyboard(db, view="details", page=0)
    await edit_bound_message(
        callback.message,
        "✅ Клиент удалён навсегда.\n\n"
        f"Удалено из Cascade: {result.deleted}\n"
        f"Уже отсутствовало: {result.already_missing}\n\n"
        f"Клиентов осталось: {total}",
        reply_markup=keyboard,
    )


@router.callback_query(AdminClientCallback.filter(F.action == "group"))
async def start_client_group_change(
    callback: types.CallbackQuery,
    db: Database,
    cascade_router: CascadeRouter,
    admin_workflows: AdminWorkflowService,
    safe_answer_callback,
    callback_data: AdminClientCallback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    client = db.get_admin_client_details(callback_data.user_id)
    if not client:
        await edit_bound_message(
            callback.message, "❌ Клиент не найден.", reply_markup=admin_dashboard_keyboard()
        )
        return
    try:
        groups = await cascade_router.list_assignable_client_groups(callback_data.user_id)
    except CascadeError:
        logger.exception("Failed to list client groups for group change")
        await edit_bound_message(
            callback.message,
            "❌ Не удалось проверить группы Cascade.",
            reply_markup=client_card_keyboard(callback_data.user_id),
        )
        return
    if not groups:
        await edit_bound_message(
            callback.message,
            "❌ Для серверов клиента нет общей назначаемой группы.",
            reply_markup=client_card_keyboard(callback_data.user_id),
        )
        return
    old_groups = sorted(
        {
            str(peer["client_group"])
            for peer in db.get_managed_client_configs(callback_data.user_id)
            if peer.get("client_group")
        }
    )
    admin_workflows.set(
        callback.from_user.id,
        "select_client_group",
        user_id=callback_data.user_id,
        groups=groups,
        old_groups=old_groups,
        service_chat_id=callback.message.chat.id,
        service_message_id=callback.message.message_id,
    )
    rows = [
        [
            InlineKeyboardButton(
                text=group_name,
                callback_data=AdminConfigCallback(
                    action="client_group", user_id=callback_data.user_id, value=index
                ).pack(),
            )
        ]
        for index, group_name in enumerate(groups)
    ]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admin_flow_cancel")])
    await edit_bound_message(
        callback.message,
        f"Текущая группа: {client_group_label(client)}\n\nВыбери новую группу клиента.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(AdminConfigCallback.filter(F.action == "client_group"))
async def select_client_group_change(
    callback: types.CallbackQuery,
    admin_workflows: AdminWorkflowService,
    safe_answer_callback,
    callback_data: AdminConfigCallback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    flow = admin_workflows.get(callback.from_user.id)
    groups = flow.get("groups", []) if flow else []
    if (
        not flow
        or flow.get("state") != "select_client_group"
        or int(flow.get("user_id", 0)) != callback_data.user_id
        or not 0 <= callback_data.value < len(groups)
    ):
        await edit_bound_message(callback.message, "❌ Сценарий смены группы устарел.")
        return
    group_name = str(groups[callback_data.value])
    admin_workflows.set(
        callback.from_user.id,
        "confirm_client_group",
        **{key: value for key, value in flow.items() if key not in {"state", "groups"}},
        client_group=group_name,
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Изменить группу",
                    callback_data=AdminConfigCallback(
                        action="client_group_confirm", user_id=callback_data.user_id
                    ).pack(),
                )
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_flow_cancel")],
        ]
    )
    await edit_bound_message(
        callback.message,
        f"Перевести все конфиги клиента в группу {group_name}?\n\n"
        "Срок и состояние конфигов не изменятся.",
        reply_markup=keyboard,
    )


@router.callback_query(AdminConfigCallback.filter(F.action == "client_group_confirm"))
async def confirm_client_group_change(
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
        or flow.get("state") != "confirm_client_group"
        or int(flow.get("user_id", 0)) != callback_data.user_id
    ):
        await edit_bound_message(callback.message, "❌ Сценарий смены группы устарел.")
        return
    group_name = str(flow["client_group"])
    try:
        peer_count = await cascade_router.change_client_group(
            callback_data.user_id, group_name
        )
    except CascadeError:
        logger.exception("Failed to change unified client group")
        db.log_admin_client_group_change(
            callback.from_user.id,
            callback_data.user_id,
            list(flow.get("old_groups", [])),
            group_name,
            len(db.get_managed_client_configs(callback_data.user_id)),
            operation="admin_change_client_group_failed",
        )
        await edit_bound_message(
            callback.message,
            "❌ Не удалось изменить группу. Изменения отменены или поставлены в очередь восстановления.",
            reply_markup=client_card_keyboard(callback_data.user_id),
        )
        return
    admin_workflows.clear(callback.from_user.id)
    db.log_admin_client_group_change(
        callback.from_user.id,
        callback_data.user_id,
        list(flow.get("old_groups", [])),
        group_name,
        peer_count,
    )
    await edit_bound_message(
        callback.message,
        f"✅ Все конфиги клиента переведены в группу {group_name}.",
        reply_markup=client_card_keyboard(callback_data.user_id),
    )


@router.callback_query(AdminClientCallback.filter(F.action == "expiry"))
async def start_expiry_change(
    callback: types.CallbackQuery,
    db: Database,
    admin_workflows: AdminWorkflowService,
    safe_answer_callback,
    callback_data: AdminClientCallback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    client = db.get_admin_client_details(callback_data.user_id)
    subscription = db.get_peer_by_telegram_id(callback_data.user_id)
    if not client:
        await edit_bound_message(
            callback.message, "❌ Клиент не найден.", reply_markup=admin_dashboard_keyboard()
        )
        return
    if not subscription or subscription.get("payment_status") is None:
        await edit_bound_message(
            callback.message,
            "❌ У клиента нет подписки. Изменить срок доступа нельзя.",
            reply_markup=client_card_keyboard(callback_data.user_id),
        )
        return
    admin_workflows.set(
        callback.from_user.id,
        "await_expiry",
        user_id=callback_data.user_id,
        old_expire_date=subscription.get("expire_date"),
        service_chat_id=callback.message.chat.id,
        service_message_id=callback.message.message_id,
    )
    await edit_bound_message(
        callback.message,
        "Введи новый срок доступа:\n\n"
        "• ДД-ММ-ГГГГ\n"
        "• ДД-ММ-ГГГГ ЧЧ:ММ\n\n"
        "Если время не указано, будет использовано 23:59 по Москве.",
        reply_markup=cancel_keyboard(),
    )


@router.callback_query(AdminClientCallback.filter(F.action == "message"))
@router.callback_query(F.data.regexp(r"^admin_message_client_[0-9]+$"))
async def choose_message_client(
    callback: types.CallbackQuery,
    db: Database,
    admin_workflows: AdminWorkflowService,
    safe_answer_callback,
    callback_data: AdminClientCallback | None = None,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    user_id = (
        callback_data.user_id
        if callback_data
        else int((callback.data or "").removeprefix("admin_message_client_"))
    )
    if db.is_client_banned(user_id):
        await edit_bound_message(
            callback.message,
            "❌ Забаненным клиентам нельзя отправлять сообщения.",
            reply_markup=admin_dashboard_keyboard(),
        )
        return
    admin_workflows.set(
        callback.from_user.id,
        "await_message",
        mode="client",
        recipient_id=user_id,
        service_chat_id=callback.message.chat.id,
        service_message_id=callback.message.message_id,
    )
    await edit_bound_message(
        callback.message,
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
    user_id = (
        callback_data.user_id
        if callback_data
        else int((callback.data or "").removeprefix("admin_discount_client_"))
    )
    client = db.get_admin_client_details(user_id)
    if not client:
        await edit_bound_message(
            callback.message, "❌ Клиент не найден.", reply_markup=admin_dashboard_keyboard()
        )
        return
    await edit_bound_message(
        callback.message,
        format_client(client),
        reply_markup=discount_keyboard(
            user_id, bool(client.get("is_complimentary"))
        ),
    )


@router.callback_query(AdminClientCallback.filter(F.action == "complimentary"))
async def confirm_client_complimentary(
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
        await edit_bound_message(callback.message, "❌ Клиент не найден.")
        return
    enabled = not bool(client.get("is_complimentary"))
    await edit_bound_message(
        callback.message,
        (
            "Назначить клиенту бессрочный бесплатный доступ?"
            if enabled
            else "Отменить бесплатный доступ и вернуться к оплаченной подписке?"
        ),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Подтвердить",
                        callback_data=AdminClientCallback(
                            action=(
                                "complimentary_enable_confirm"
                                if enabled
                                else "complimentary_disable_confirm"
                            ),
                            user_id=callback_data.user_id,
                        ).pack(),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад",
                        callback_data=AdminClientCallback(
                            action="discount", user_id=callback_data.user_id
                        ).pack(),
                    )
                ],
            ]
        ),
    )


@router.callback_query(
    AdminClientCallback.filter(
        F.action.in_(
            {"complimentary_enable_confirm", "complimentary_disable_confirm"}
        )
    )
)
async def set_client_complimentary(
    callback: types.CallbackQuery,
    db: Database,
    cascade_router: CascadeRouter,
    safe_answer_callback,
    callback_data: AdminClientCallback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    client = db.get_admin_client_details(callback_data.user_id)
    if not client:
        await edit_bound_message(callback.message, "❌ Клиент не найден.")
        return
    enabled = callback_data.action == "complimentary_enable_confirm"
    result = await cascade_router.set_client_complimentary(
        callback_data.user_id, callback.from_user.id, enabled
    )
    warning = ""
    if result["failed"]:
        primary = db.get_primary_client_peer(callback_data.user_id)
        operation = "sync_client_state" if primary else "create_peer"
        payload = (
            {}
            if primary
            else {
                "username": client.get("telegram_username") or "",
                "peer_name": client.get("telegram_username")
                or str(callback_data.user_id),
                "expire_date": "2099-12-31 23:59:59",
                "tariff_key": "complimentary",
            }
        )
        db.add_provisioning_task(
            callback_data.user_id,
            operation,
            payload,
            f"Failed peers: {result['failed']}",
        )
        warning = "\n⚠️ Синхронизация VPN поставлена в очередь."
    await edit_bound_message(
        callback.message,
        ("✅ Бесплатный доступ назначен." if enabled else "✅ Бесплатный доступ отменён.")
        + warning,
        reply_markup=discount_keyboard(callback_data.user_id, enabled),
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
        await edit_bound_message(callback.message, "❌ Не удалось сохранить скидку.")
        return
    db.log_admin_promo_change(
        callback.from_user.id,
        user_id,
        client.get("server_key"),
        int(client.get("promo") or 0),
        value,
    )
    await edit_bound_message(
        callback.message,
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
    user_id = (
        callback_data.user_id
        if callback_data
        else int((callback.data or "").removeprefix("admin_discount_custom_"))
    )
    admin_workflows.set(
        callback.from_user.id,
        "await_discount",
        user_id=user_id,
        service_chat_id=callback.message.chat.id,
        service_message_id=callback.message.message_id,
    )
    await edit_bound_message(
        callback.message, "Введи скидку целым числом от 0 до 90.", reply_markup=cancel_keyboard()
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
    await edit_bound_message(
        callback.message, "Введи Telegram ID или username.", reply_markup=cancel_keyboard()
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
    await edit_bound_message(
        callback.message,
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
        await edit_bound_message(callback.message, "❌ Клиент не найден.")
        return
    page = callback_data.value if callback_data.action == "page" else 0
    keyboard, current_page = config_list_keyboard(db, callback_data.user_id, page)
    configs = db.get_admin_client_configs(callback_data.user_id)
    await edit_bound_message(
        callback.message,
        f"🗂 Конфиги клиента {callback_data.user_id}\n\n"
        f"Всего: {len(configs)} · Страница: {current_page + 1}",
        reply_markup=keyboard,
    )


@router.callback_query(AdminConfigCallback.filter(F.action == "view"))
async def show_config_details(
    callback: types.CallbackQuery,
    db: Database,
    cascade_router: CascadeRouter,
    safe_answer_callback,
    callback_data: AdminConfigCallback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    config = db.get_admin_managed_config(callback_data.peer_id, callback_data.user_id)
    if not config:
        await edit_bound_message(callback.message, "❌ Конфиг не найден.")
        return
    try:
        server_name = cascade_router.get_server_name(str(config["server_key"]))
    except CascadeError:
        server_name = str(config["server_key"])
    await edit_bound_message(
        callback.message,
        format_config(config, server_name),
        reply_markup=config_details_keyboard(config),
    )


@router.callback_query(AdminConfigCallback.filter(F.action == "download"))
async def download_paid_client_config(
    callback: types.CallbackQuery,
    bot: Bot,
    db: Database,
    cascade_router: CascadeRouter,
    safe_answer_callback,
    callback_data: AdminConfigCallback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    config = db.get_admin_managed_config(callback_data.peer_id, callback_data.user_id)
    if not config:
        await edit_bound_message(
            callback.message,
            "❌ Конфиг не найден.",
            reply_markup=config_error_back_keyboard(callback_data.user_id, callback_data.peer_id),
        )
        return
    if not db.has_active_access(callback_data.user_id):
        await edit_bound_message(
            callback.message,
            "❌ Скачивание доступно только для клиентов с подтверждённой оплатой.",
            reply_markup=config_error_back_keyboard(callback_data.user_id, callback_data.peer_id),
        )
        return
    try:
        config, content = await cascade_router.get_admin_managed_config(
            callback_data.user_id, callback_data.peer_id
        )
        server_name = cascade_router.get_server_name(str(config["server_key"]))
        caption = ensure_telegram_text(
            f"Клиент: {callback_data.user_id}\n"
            f"Конфиг: {config['config_name']}\n"
            f"Локация: {server_name}"
        )
        await bot.send_document(
            chat_id=callback.from_user.id,
            document=types.BufferedInputFile(
                file=content,
                filename=location_config_filename(server_name),
            ),
            caption=caption.regular_html,
            parse_mode="HTML",
        )
    except CascadeNotFound:
        await edit_bound_message(
            callback.message,
            "❌ Peer отсутствует в Cascade. Создай новый конфиг.",
            reply_markup=config_error_back_keyboard(callback_data.user_id, callback_data.peer_id),
        )
        return
    except CascadeError:
        logger.exception("Failed to download a paid client configuration")
        await edit_bound_message(
            callback.message,
            "❌ Не удалось скачать конфиг.",
            reply_markup=config_error_back_keyboard(callback_data.user_id, callback_data.peer_id),
        )
        return
    except Exception:
        logger.exception("Failed to send a paid client configuration to the admin")
        await edit_bound_message(
            callback.message,
            "❌ Не удалось отправить конфиг.",
            reply_markup=config_error_back_keyboard(callback_data.user_id, callback_data.peer_id),
        )
        return
    db.log_admin_config_change(
        callback.from_user.id,
        callback_data.user_id,
        callback_data.peer_id,
        "admin_download_config",
        server_key=str(config["server_key"]),
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
    access = db.get_client_access_state(callback_data.user_id)
    if not db.get_primary_client_peer(callback_data.user_id) or not (
        access.cascade_expiry or access.paid_expiry
    ):
        await edit_bound_message(
            callback.message,
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
    await edit_bound_message(
        callback.message,
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
        await edit_bound_message(callback.message, "❌ Сценарий создания устарел.")
        return
    server_key = str(servers[callback_data.value])
    try:
        interfaces = await cascade_router.list_server_interfaces(server_key)
    except CascadeError:
        logger.exception("Failed to list Cascade interfaces for %s", server_key)
        await edit_bound_message(
            callback.message,
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
        await edit_bound_message(
            callback.message,
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
    await edit_bound_message(
        callback.message,
        f"Выбери интерфейс на сервере {server_key}.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(AdminConfigCallback.filter(F.action == "interface"))
async def select_config_interface(
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
    interfaces = flow.get("interfaces", []) if flow else []
    if (
        not flow
        or flow.get("state") != "select_config_interface"
        or int(flow.get("user_id", 0)) != callback_data.user_id
        or not 0 <= callback_data.value < len(interfaces)
    ):
        await edit_bound_message(callback.message, "❌ Сценарий создания устарел.")
        return
    interface = interfaces[callback_data.value]
    group_name = confirmed_managed_client_group(
        db.get_managed_client_configs(callback_data.user_id)
    )
    if group_name is None:
        admin_workflows.clear(callback.from_user.id)
        await edit_bound_message(
            callback.message,
            "❌ Группа файлов клиента не подтверждена или не согласована.\n\n"
            "Сначала используй «👥 Изменить группу» в карточке клиента.",
            reply_markup=client_card_keyboard(callback_data.user_id),
        )
        return
    try:
        groups = await cascade_router.list_assignable_client_groups(
            callback_data.user_id, str(flow["server_key"])
        )
    except CascadeError:
        logger.exception("Failed to list assignable client groups")
        await edit_bound_message(
            callback.message,
            "❌ Не удалось проверить группы Cascade.",
            reply_markup=cancel_keyboard(),
        )
        return
    live_group = next(
        (group for group in groups if group.casefold() == group_name.casefold()),
        None,
    )
    if live_group is None:
        await edit_bound_message(
            callback.message,
            f"❌ Группа «{group_name}» недоступна на выбранном сервере.",
            reply_markup=cancel_keyboard(),
        )
        return
    try:
        peer_name = await cascade_router.build_additional_peer_name(
            callback_data.user_id,
            str(flow["config_name"]),
            str(flow["server_key"]),
            str(interface["id"]),
        )
    except CascadeError:
        await edit_bound_message(
            callback.message,
            "❌ Не удалось проверить техническое имя peer.",
            reply_markup=cancel_keyboard(),
        )
        return
    admin_workflows.set(
        callback.from_user.id,
        "confirm_config_create",
        **{key: value for key, value in flow.items() if key not in {"state", "interfaces"}},
        interface_id=interface["id"],
        interface_name=interface["name"],
        client_group=live_group,
        peer_name=peer_name,
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Создать",
                    callback_data=AdminConfigCallback(
                        action="create", user_id=callback_data.user_id
                    ).pack(),
                )
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_flow_cancel")],
        ]
    )
    await edit_bound_message(
        callback.message,
        "Создать дополнительный конфиг?\n\n"
        f"Название: {flow['config_name']}\n"
        f"Peer: {peer_name}\n"
        f"Сервер: {flow['server_key']}\n"
        f"Интерфейс: {interface['name']} · {str(interface['id'])[:8]}\n"
        f"Группа: {live_group}",
        reply_markup=keyboard,
    )


@router.callback_query(AdminConfigCallback.filter(F.action == "group"))
async def reject_legacy_config_group_selection(
    callback: types.CallbackQuery,
    admin_workflows: AdminWorkflowService,
    safe_answer_callback,
    callback_data: AdminConfigCallback,
) -> None:
    """Reject group-selection callbacks left by workflows from older releases."""
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    flow = admin_workflows.get(callback.from_user.id)
    if (
        flow
        and flow.get("state") == "select_config_group"
        and int(flow.get("user_id", 0)) == callback_data.user_id
    ):
        admin_workflows.clear(callback.from_user.id)
    await edit_bound_message(
        callback.message,
        "❌ Сценарий создания устарел. Начни создание файла заново.",
        reply_markup=client_card_keyboard(callback_data.user_id),
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
        await edit_bound_message(callback.message, "❌ Сценарий создания устарел.")
        return
    try:
        config = await cascade_router.create_additional_config(
            callback_data.user_id,
            str(flow["config_name"]),
            str(flow["server_key"]),
            str(flow["interface_id"]),
            str(flow["client_group"]),
            reassign_existing_group=False,
        )
    except CascadeError:
        logger.exception("Failed to create an additional configuration")
        db.log_admin_client_group_change(
            callback.from_user.id,
            callback_data.user_id,
            sorted(
                {
                    str(peer["client_group"])
                    for peer in db.get_managed_client_configs(callback_data.user_id)
                    if peer.get("client_group")
                }
            ),
            str(flow["client_group"]),
            len(db.get_managed_client_configs(callback_data.user_id)),
            operation="admin_create_config_rolled_back",
        )
        await edit_bound_message(
            callback.message,
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
        client_group=str(config["client_group"]),
    )
    keyboard, _ = config_list_keyboard(db, callback_data.user_id)
    await edit_bound_message(
        callback.message,
        f"✅ Конфиг «{config['config_name']}» создан.\n\n"
        f"Peer: {config['peer_name']}\n"
        f"Группа: {config['client_group']}",
        reply_markup=keyboard,
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
        await edit_bound_message(callback.message, "❌ Конфиг не найден.")
        return
    admin_workflows.set(
        callback.from_user.id,
        "await_config_rename",
        user_id=callback_data.user_id,
        peer_id=callback_data.peer_id,
        service_chat_id=callback.message.chat.id,
        service_message_id=callback.message.message_id,
    )
    await edit_bound_message(
        callback.message,
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
        await edit_bound_message(callback.message, "❌ Дополнительный конфиг не найден.")
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
    await edit_bound_message(
        callback.message,
        f"Деактивировать конфиг «{config['config_name']}»?\n"
        "Peer останется в Cascade и сможет быть восстановлен.",
        reply_markup=keyboard,
    )


@router.callback_query(AdminConfigCallback.filter(F.action == "delete"))
async def confirm_config_deletion(
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
        await edit_bound_message(callback.message, "❌ Дополнительный конфиг не найден.")
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Удалить навсегда",
                    callback_data=AdminConfigCallback(
                        action="delete_confirm",
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
    await edit_bound_message(
        callback.message,
        f"Удалить конфиг «{config_display_name(config)}» навсегда?\n\n"
        "Peer будет удалён из Cascade и базы данных. Это действие нельзя отменить.",
        reply_markup=keyboard,
    )


@router.callback_query(AdminConfigCallback.filter(F.action == "delete_confirm"))
async def delete_additional_config(
    callback: types.CallbackQuery,
    db: Database,
    cascade_router: CascadeRouter,
    safe_answer_callback,
    callback_data: AdminConfigCallback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    try:
        config, cascade_peer_missing = await cascade_router.delete_additional_config(
            callback_data.user_id, callback_data.peer_id
        )
    except CascadeNotFound:
        await edit_bound_message(
            callback.message,
            "❌ Дополнительный конфиг не найден.",
            reply_markup=config_list_keyboard(db, callback_data.user_id)[0],
        )
        return
    except CascadeError:
        logger.exception("Failed to permanently delete additional configuration")
        await edit_bound_message(
            callback.message,
            "❌ Не удалось удалить конфиг. Данные не изменены.",
            reply_markup=config_error_back_keyboard(
                callback_data.user_id, callback_data.peer_id
            ),
        )
        return
    db.log_admin_config_change(
        callback.from_user.id,
        callback_data.user_id,
        callback_data.peer_id,
        "admin_delete_config",
        server_key=str(config["server_key"]),
        client_group=config.get("client_group"),
    )
    keyboard, _ = config_list_keyboard(db, callback_data.user_id)
    suffix = (
        "\nPeer уже отсутствовал в Cascade; удалена локальная запись."
        if cascade_peer_missing
        else ""
    )
    await edit_bound_message(
        callback.message,
        f"✅ Конфиг «{config_display_name(config)}» удалён навсегда.{suffix}",
        reply_markup=keyboard,
    )


@router.callback_query(AdminConfigCallback.filter(F.action.in_({"deactivate_confirm", "restore"})))
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
        await edit_bound_message(
            callback.message,
            "❌ Peer не найден в Cascade. Создай новый дополнительный конфиг.",
            reply_markup=config_error_back_keyboard(callback_data.user_id, callback_data.peer_id),
        )
        return
    except CascadeError:
        logger.exception("Failed to change additional configuration state")
        await edit_bound_message(
            callback.message,
            "❌ Не удалось изменить состояние конфига.",
            reply_markup=config_error_back_keyboard(callback_data.user_id, callback_data.peer_id),
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
    refreshed_config = db.get_admin_managed_config(callback_data.peer_id, callback_data.user_id)
    await edit_bound_message(
        callback.message,
        "✅ Конфиг восстановлен." if active else "✅ Конфиг деактивирован.",
        reply_markup=config_details_keyboard(refreshed_config or config),
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
    if state == "await_client_identity":
        identity = (message.text or "").strip()
        if identity.isdigit():
            user_id = int(identity)
            if user_id <= 0:
                await edit_telegram_text(
                    bot,
                    flow["service_chat_id"],
                    flow["service_message_id"],
                    "Telegram ID должен быть положительным числом.",
                    reply_markup=cancel_keyboard(),
                )
                return
            client = db.admin_add_client(user_id, message.from_user.id)
            admin_workflows.clear(message.from_user.id)
            await edit_telegram_text(
                bot,
                flow["service_chat_id"],
                flow["service_message_id"],
                format_client(client),
                reply_markup=client_card_keyboard(
                    user_id, bool(client.get("is_banned"))
                ),
            )
        else:
            username = identity.lstrip("@").casefold()
            if not username:
                await edit_telegram_text(
                    bot,
                    flow["service_chat_id"],
                    flow["service_message_id"],
                    "Введи числовой Telegram ID или @username.",
                    reply_markup=cancel_keyboard(),
                )
                return
            matches = db.find_clients_by_username(username)
            if len(matches) == 1:
                client = db.get_admin_client_details(
                    int(matches[0]["telegram_user_id"])
                )
                admin_workflows.clear(message.from_user.id)
                await edit_telegram_text(
                    bot,
                    flow["service_chat_id"],
                    flow["service_message_id"],
                    format_client(client),
                    reply_markup=client_card_keyboard(
                        int(client["telegram_user_id"]),
                        bool(client.get("is_banned")),
                    ),
                )
            elif len(matches) > 1:
                await edit_telegram_text(
                    bot,
                    flow["service_chat_id"],
                    flow["service_message_id"],
                    "Найдено несколько клиентов с этим username. Используй Telegram ID.",
                    reply_markup=cancel_keyboard(),
                )
                return
            else:
                try:
                    invitation = db.create_client_invitation(
                        username, message.from_user.id
                    )
                except ValueError:
                    await edit_telegram_text(
                        bot,
                        flow["service_chat_id"],
                        flow["service_message_id"],
                        "Некорректный Telegram username.",
                        reply_markup=cancel_keyboard(),
                    )
                    return
                admin_workflows.clear(message.from_user.id)
                link = (
                    await create_start_link(
                        bot, f"claim_{invitation['token']}", encode=False
                    )
                    if invitation.get("token")
                    else None
                )
                await edit_telegram_text(
                    bot,
                    flow["service_chat_id"],
                    flow["service_message_id"],
                    format_invitation(invitation, link),
                    reply_markup=invitation_details_keyboard(invitation),
                )
        with suppress(Exception):
            await message.delete()
    elif state == "await_invitation_discount":
        try:
            value = int((message.text or "").strip())
        except ValueError:
            value = -1
        if not 0 <= value <= 90 or not db.set_invitation_promo(
            int(flow["invitation_id"]), message.from_user.id, value
        ):
            await edit_telegram_text(
                bot,
                flow["service_chat_id"],
                flow["service_message_id"],
                "Скидка должна быть целым числом от 0 до 90.",
                reply_markup=cancel_keyboard(),
            )
            return
        invitation = db.get_client_invitation(int(flow["invitation_id"]))
        admin_workflows.clear(message.from_user.id)
        await edit_telegram_text(
            bot,
            flow["service_chat_id"],
            flow["service_message_id"],
            f"✅ Скидка {value}% сохранена.",
            reply_markup=invitation_details_keyboard(invitation),
        )
        with suppress(Exception):
            await message.delete()
    elif state == "await_ban_reason":
        reason = " ".join((message.text or "").split())
        if not reason or len(reason) > 500:
            await edit_telegram_text(
                bot,
                chat_id=flow["service_chat_id"],
                message_id=flow["service_message_id"],
                text="Причина должна содержать от 1 до 500 символов.",
                reply_markup=cancel_keyboard(),
            )
            with suppress(Exception):
                await message.delete()
            return
        admin_workflows.set(
            message.from_user.id,
            "confirm_ban",
            **{key: value for key, value in flow.items() if key != "state"},
            reason=reason,
        )
        await edit_telegram_text(
            bot,
            chat_id=flow["service_chat_id"],
            message_id=flow["service_message_id"],
            text=(
                f"Подтверди бан клиента {flow['user_id']}.\n\n"
                f"Причина: {reason}"
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🚫 Подтвердить бан",
                            callback_data=AdminClientCallback(
                                action="ban_confirm", user_id=int(flow["user_id"])
                            ).pack(),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="❌ Отмена", callback_data="admin_flow_cancel"
                        )
                    ],
                ]
            ),
        )
    elif state == "await_expiry":
        try:
            expire_date = parse_admin_expiry_input(message.text or "")
        except ValueError:
            await edit_telegram_text(
                bot,
                chat_id=flow["service_chat_id"],
                message_id=flow["service_message_id"],
                text=("❌ Неверный формат даты.\n\nВведи ДД-ММ-ГГГГ или ДД-ММ-ГГГГ ЧЧ:ММ."),
                reply_markup=cancel_keyboard(),
            )
            with suppress(Exception):
                await message.delete()
            return
        is_future = datetime.fromisoformat(expire_date) > datetime.now(UTC).replace(tzinfo=None)
        admin_workflows.set(
            message.from_user.id,
            "confirm_expiry",
            **{key: value for key, value in flow.items() if key != "state"},
            expire_date=expire_date,
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Применить",
                        callback_data=AdminClientCallback(
                            action="expiry_confirm",
                            user_id=int(flow["user_id"]),
                        ).pack(),
                    )
                ],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_flow_cancel")],
            ]
        )
        old_expire_date = flow.get("old_expire_date")
        old_formatted = format_admin_expiry(old_expire_date)
        new_formatted = format_admin_expiry(expire_date)
        confirmation_text = (
            "Подтверди изменение срока доступа.\n\n"
            f"Старый срок: {old_formatted}\n"
            f"Новый срок: {new_formatted}\n"
            f"Состояние: {'активен' if is_future else 'истёк'}"
        )
        replacements = {
            new_formatted: rich_date(
                expire_date,
                new_formatted,
                date_time_format="dt",
            )
        }
        if old_expire_date:
            replacements[old_formatted] = rich_date(
                str(old_expire_date),
                old_formatted,
                date_time_format="dt",
            )
        await edit_telegram_text(
            bot,
            chat_id=flow["service_chat_id"],
            message_id=flow["service_message_id"],
            text=TelegramText.from_plain_with_replacements(
                confirmation_text,
                replacements,
            ),
            reply_markup=keyboard,
        )
    elif state in {"await_config_name", "await_config_rename"}:
        try:
            config_name = normalize_config_name(message.text or "")
        except ValueError:
            await edit_telegram_text(
                bot,
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
                if str(item.get("config_name") or "").casefold() == config_name.casefold()
                and int(item["id"]) != int(flow.get("peer_id", 0))
            ),
            None,
        )
        if duplicate:
            await edit_telegram_text(
                bot,
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
                await edit_telegram_text(
                    bot,
                    chat_id=flow["service_chat_id"],
                    message_id=flow["service_message_id"],
                    text="❌ Не удалось переименовать конфиг.",
                    reply_markup=cancel_keyboard(),
                )
                with suppress(Exception):
                    await message.delete()
                return
            config = db.get_admin_managed_config(peer_id, int(flow["user_id"]))
            admin_workflows.clear(message.from_user.id)
            db.log_admin_config_change(
                message.from_user.id,
                int(flow["user_id"]),
                peer_id,
                "admin_rename_config",
                server_key=str(config["server_key"]) if config else None,
            )
            await edit_telegram_text(
                bot,
                chat_id=flow["service_chat_id"],
                message_id=flow["service_message_id"],
                text=f"✅ Конфиг переименован в «{config_name}».",
                reply_markup=config_details_keyboard(config)
                if config
                else admin_dashboard_keyboard(),
            )
        else:
            servers = [server.server_key for server in cascade_router.get_enabled_servers()]
            if not servers:
                await edit_telegram_text(
                    bot,
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
            rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admin_flow_cancel")])
            await edit_telegram_text(
                bot,
                chat_id=flow["service_chat_id"],
                message_id=flow["service_message_id"],
                text=f"Название: {config_name}\n\nВыбери сервер.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
    elif state == "await_search":
        query = (message.text or "").strip()[:100]
        keyboard, total = client_list_keyboard(db, view="details", page=0, query=query)
        admin_workflows.clear(message.from_user.id)
        await edit_telegram_text(
            bot,
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
            await edit_telegram_text(
                bot,
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
        await edit_telegram_text(
            bot,
            chat_id=flow["service_chat_id"],
            message_id=flow["service_message_id"],
            text=f"✅ Скидка {value}% сохранена.",
            reply_markup=client_card_keyboard(int(flow["user_id"])),
        )
    elif state == "await_refund_charge":
        charge_id = (message.text or "").strip()[:200]
        payment = await asyncio.to_thread(db.get_payment_by_telegram_charge, charge_id)
        if not payment or payment["payment_method"] != "stars":
            await edit_telegram_text(
                bot,
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
                [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_flow_cancel")],
            ]
        )
        await edit_telegram_text(
            bot,
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
        await edit_telegram_text(
            bot,
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
            await callback.bot.delete_message(flow["source_chat_id"], flow["source_message_id"])
    reply_markup = admin_dashboard_keyboard()
    if (
        flow
        and flow.get("user_id")
        and any(
            marker in str(flow.get("state", ""))
            for marker in ("config", "expiry", "group")
        )
    ):
        reply_markup = client_card_keyboard(int(flow["user_id"]))
    await edit_bound_message(callback.message, "Действие отменено.", reply_markup=reply_markup)


@router.callback_query(AdminClientCallback.filter(F.action == "expiry_confirm"))
async def confirm_expiry_change(
    callback: types.CallbackQuery,
    db: Database,
    cascade_router: CascadeRouter,
    admin_workflows: AdminWorkflowService,
    safe_answer_callback,
    callback_data: AdminClientCallback,
) -> None:
    await safe_answer_callback(callback)
    if not is_admin(callback.from_user.id):
        return
    flow = admin_workflows.get(callback.from_user.id)
    if (
        not flow
        or flow.get("state") != "confirm_expiry"
        or int(flow.get("user_id", 0)) != callback_data.user_id
    ):
        await edit_bound_message(
            callback.message,
            "❌ Изменение срока устарело или не соответствует клиенту.",
            reply_markup=client_card_keyboard(callback_data.user_id),
        )
        return
    if not db.get_admin_client_details(callback_data.user_id):
        admin_workflows.clear(callback.from_user.id)
        await edit_bound_message(
            callback.message, "❌ Клиент не найден.", reply_markup=admin_dashboard_keyboard()
        )
        return
    result = db.set_admin_subscription_expiry(
        callback.from_user.id,
        callback_data.user_id,
        str(flow["expire_date"]),
    )
    if not result:
        admin_workflows.clear(callback.from_user.id)
        await edit_bound_message(
            callback.message,
            "❌ У клиента нет подписки. Срок доступа не изменён.",
            reply_markup=client_card_keyboard(callback_data.user_id),
        )
        return
    admin_workflows.clear(callback.from_user.id)
    sync_result = await cascade_router.sync_user_access(
        callback_data.user_id, str(result["expire_date"])
    )
    warning = ""
    if sync_result["failed"]:
        db.add_provisioning_task(
            callback_data.user_id,
            "sync_access",
            {"expire_date": result["expire_date"]},
            f"Failed peers: {sync_result['failed']}",
        )
        warning = (
            "\n\n⚠️ Часть конфигов не синхронизирована. Создана задача автоматического повтора."
        )
    if sync_result["missing"]:
        warning += f"\n\n⚠️ Недоступных дополнительных конфигов: {sync_result['missing']}."
    result_expiry = str(result["expire_date"])
    formatted_result_expiry = format_admin_expiry(result_expiry)
    result_text = (
        "✅ Срок доступа изменён.\n\n"
        f"Новый срок: {formatted_result_expiry}\n"
        f"Состояние: "
        f"{'активен' if result['payment_status'] == 'paid' else 'истёк'}"
        f"{warning}"
    )
    await edit_bound_message(
        callback.message,
        TelegramText.from_plain_with_replacements(
            result_text,
            {
                formatted_result_expiry: rich_date(
                    result_expiry,
                    formatted_result_expiry,
                    date_time_format="dt",
                )
            },
        ),
        reply_markup=client_card_keyboard(callback_data.user_id),
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
        await edit_bound_message(callback.message, "Нет подготовленного сообщения.")
        return
    recipients = (
        db.get_client_telegram_ids() if flow["mode"] == "all" else [int(flow["recipient_id"])]
    )
    admin_workflows.clear(callback.from_user.id)
    await edit_bound_message(
        callback.message, f"📣 Отправка запущена. Получателей: {len(recipients)}"
    )
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
    await edit_bound_message(
        callback.message,
        f"📣 Рассылка завершена.\n\nОтправлено: {sent}\nНе доставлено: {failed}",
        reply_markup=admin_dashboard_keyboard(),
    )
