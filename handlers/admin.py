import asyncio
import logging
from contextlib import suppress
from typing import Any

from aiogram import Bot, F, Router, types
from aiogram.filters import BaseFilter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from callbacks import (
    AdminClientCallback,
    AdminDiscountCallback,
    AdminPageCallback,
    RefundConfirmationCallback,
)
from config import get_admin_telegram_ids
from database import Database
from utils import format_date_for_user

logger = logging.getLogger(__name__)
router = Router(name="admin")
ADMIN_CLIENTS_PAGE_SIZE = 8
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
    if view == "discount":
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
                text="⬅️ Управление клиентами", callback_data="admin_manage_clients"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_client(client: dict[str, Any]) -> str:
    username = str(client.get("telegram_username") or "")
    identity = f"@{username}" if username else "без username"
    expiry = client.get("expire_date")
    return (
        "👤 Клиент\n\n"
        f"Telegram ID: {client['telegram_user_id']}\n"
        f"Username: {identity}\n"
        f"Скидка: {int(client.get('promo') or 0)}%\n"
        f"Сервер: {client.get('server_key') or 'не назначен'}\n"
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
    keyboard, total = client_list_keyboard(db, view="discount", page=0)
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
        view = "message" if data.startswith("admin_clients_page_") else "discount"
        try:
            page = int(data.rsplit("_", 1)[1])
        except ValueError:
            page = 0
    keyboard, total = client_list_keyboard(db, view=view, page=page)
    await callback.message.edit_text(
        f"👥 Клиенты\n\nНайдено: {total}", reply_markup=keyboard
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
        reply_markup=admin_dashboard_keyboard(),
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


@router.message(ActiveAdminWorkflow())
async def capture_admin_input(
    message: types.Message,
    bot: Bot,
    db: Database,
    admin_workflows: AdminWorkflowService,
) -> None:
    flow = admin_workflows.get(message.from_user.id)
    if not flow:
        return
    state = flow["state"]
    if state == "await_search":
        query = (message.text or "").strip()[:100]
        keyboard, total = client_list_keyboard(
            db, view="discount", page=0, query=query
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
            reply_markup=admin_dashboard_keyboard(),
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
    await callback.message.edit_text(
        "Действие отменено.", reply_markup=admin_dashboard_keyboard()
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
