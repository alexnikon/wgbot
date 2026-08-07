import logging
from contextlib import suppress
from typing import Any

from aiogram import F, Router, types
from aiogram.filters import BaseFilter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from callbacks import ClientConfigCallback
from cascade_api import CascadeCapacityError, CascadeError, CascadeNotFound, CascadeRouter
from database import MANAGED_CONFIG_ROLE, MAX_CLIENT_CONFIGS, Database, normalize_config_name
from payment import PaymentManager
from subscription_view import subscription_status_message
from telegram_runtime import edit_telegram_text
from telegram_text import TelegramText, rich_date
from utils import format_date_for_user, location_config_filename

logger = logging.getLogger(__name__)
router = Router(name="access")
CLIENT_CONFIGS_PAGE_SIZE = 8
CLIENT_CONFIG_WORKFLOW_TYPE = "client_config_flow"


def get_client_config_workflow(db: Database, user_id: int) -> dict[str, Any] | None:
    workflow = db.get_admin_workflow(user_id, CLIENT_CONFIG_WORKFLOW_TYPE)
    return workflow["data"] if workflow else None


def set_client_config_workflow(
    db: Database, user_id: int, state: str, **data: Any
) -> None:
    db.set_admin_workflow(
        user_id,
        CLIENT_CONFIG_WORKFLOW_TYPE,
        state,
        {"state": state, **data},
    )


def clear_client_config_workflow(db: Database, user_id: int) -> None:
    db.delete_admin_workflow(user_id, CLIENT_CONFIG_WORKFLOW_TYPE)


class ActiveClientConfigWorkflow(BaseFilter):
    async def __call__(self, message: types.Message, db: Database) -> bool:
        command = (message.text or "").split(maxsplit=1)[0].casefold()
        if command == "/start" or command.startswith("/start@"):
            return False
        return bool(
            message.from_user
            and get_client_config_workflow(db, message.from_user.id)
        )


def client_config_keyboard(
    db: Database, user_id: int, page: int = 0
) -> tuple[InlineKeyboardMarkup, int]:
    configs = db.get_client_visible_configs(user_id)
    pages = max(1, (len(configs) + CLIENT_CONFIGS_PAGE_SIZE - 1) // CLIENT_CONFIGS_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * CLIENT_CONFIGS_PAGE_SIZE
    rows = [
        [
            InlineKeyboardButton(
                text=str(config["config_name"]),
                callback_data=ClientConfigCallback(
                    action="view", peer_id=int(config["id"]), page=page
                ).pack(),
            )
        ]
        for config in configs[start : start + CLIENT_CONFIGS_PAGE_SIZE]
    ]
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=ClientConfigCallback(action="page", page=page - 1).pack(),
            )
        )
    if page + 1 < pages:
        navigation.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=ClientConfigCallback(action="page", page=page + 1).pack(),
            )
        )
    if navigation:
        rows.append(navigation)
    if db.count_managed_configs(user_id) < MAX_CLIENT_CONFIGS:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📥 Создать файл конфигурации",
                    callback_data=ClientConfigCallback(action="create").pack(),
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="main")])
    return InlineKeyboardMarkup(inline_keyboard=rows), len(configs)


def config_file_back_keyboard(peer_id: int = 0, page: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=ClientConfigCallback(
                        action="view" if peer_id else "back",
                        peer_id=peer_id,
                        page=max(0, page),
                    ).pack(),
                )
            ],
            [InlineKeyboardButton(text="🏠 На главную", callback_data="main")],
        ]
    )


def client_config_details_keyboard(config: dict[str, Any], page: int) -> InlineKeyboardMarkup:
    peer_id = int(config["id"])
    rows: list[list[InlineKeyboardButton]] = []
    if config.get("enabled"):
        rows.append(
            [
                InlineKeyboardButton(
                    text="📥 Скачать",
                    callback_data=ClientConfigCallback(
                        action="download", peer_id=peer_id, page=page
                    ).pack(),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="✏️ Переименовать",
                callback_data=ClientConfigCallback(
                    action="rename", peer_id=peer_id, page=page
                ).pack(),
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="🗑 Удалить навсегда",
                callback_data=ClientConfigCallback(
                    action="delete", peer_id=peer_id, page=page
                ).pack(),
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ К конфигам",
                callback_data=ClientConfigCallback(action="back", page=page).pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def client_config_details_text(config: dict[str, Any], server_name: str) -> str:
    status = "активен" if config.get("enabled") else "недоступен"
    return (
        f"🗂 {config['config_name']}\n\n"
        f"Локация: {server_name}\n"
        f"Статус: {status}"
    )


@router.callback_query(F.data == "get_config")
async def handle_get_config_callback(
    callback_query: types.CallbackQuery,
    db: Database,
    safe_answer_callback,
    safe_edit_callback_message,
    create_main_menu_keyboard,
    is_access_active,
    user_action_locks,
):
    """Show the list of configurations available to the current user."""
    await safe_answer_callback(callback_query)
    user_id = callback_query.from_user.id
    async with user_action_locks.hold(user_id):
        existing_peer = db.get_peer_by_telegram_id(user_id)
        if not existing_peer:
            await safe_edit_callback_message(
                callback_query.message,
                "❌ У тебя нет VPN доступа.",
                reply_markup=create_main_menu_keyboard(user_id),
            )
            return
        if not is_access_active(existing_peer):
            if existing_peer.get("payment_status") == "paid":
                expire_date = existing_peer.get("expire_date", "Неизвестно")
                formatted = (
                    format_date_for_user(expire_date)
                    if expire_date != "Неизвестно"
                    else "Неизвестно"
                )
                plain_text = f"""
⚠️ Твой доступ к VPN истек!

📅 Дата истечения: {formatted}

⚠️ Для продолжения пользования сервисом, необходимо продлить доступ.
                """
                text = TelegramText.from_plain_with_replacements(
                    plain_text,
                    {formatted: rich_date(expire_date, formatted)},
                )
            else:
                text = """
❌ У тебя нет активного доступа.

💎 Чтобы получить файл конфигурации, нужно оплатить доступ.
                """
            await safe_edit_callback_message(
                callback_query.message,
                text,
                reply_markup=create_main_menu_keyboard(user_id),
            )
            return
        keyboard, count = client_config_keyboard(db, user_id)
        if not count:
            await safe_edit_callback_message(
                callback_query.message,
                "❌ Сейчас нет доступных файлов конфигурации. Обратись в поддержку.",
                reply_markup=create_main_menu_keyboard(user_id),
            )
            return
        await safe_edit_callback_message(
            callback_query.message,
            "📥 Выбери файл конфигурации для скачивания.",
            reply_markup=keyboard,
        )


@router.callback_query(ClientConfigCallback.filter(F.action == "back"))
async def return_to_client_configs(
    callback_query: types.CallbackQuery,
    db: Database,
    chat_panel,
    safe_answer_callback,
    create_main_menu_keyboard,
    is_access_active,
    callback_data: ClientConfigCallback,
) -> None:
    """Return from a configuration document to the persistent config menu."""
    await safe_answer_callback(callback_query)
    user_id = callback_query.from_user.id
    existing_peer = db.get_peer_by_telegram_id(user_id)
    if existing_peer and is_access_active(existing_peer):
        keyboard, count = client_config_keyboard(db, user_id, callback_data.page)
        if count:
            await chat_panel.restore_or_create(
                callback_query.message.chat.id,
                user_id,
                "📥 Выбери файл конфигурации для скачивания.",
                keyboard,
            )
            return
    await chat_panel.restore_or_create(
        callback_query.message.chat.id,
        user_id,
        "👋🏻 Главное меню",
        create_main_menu_keyboard(user_id),
    )


@router.callback_query(ClientConfigCallback.filter(F.action == "page"))
async def change_client_config_page(
    callback_query: types.CallbackQuery,
    db: Database,
    safe_answer_callback,
    safe_edit_callback_message,
    create_main_menu_keyboard,
    is_access_active,
    callback_data: ClientConfigCallback,
) -> None:
    await safe_answer_callback(callback_query)
    user_id = callback_query.from_user.id
    existing_peer = db.get_peer_by_telegram_id(user_id)
    if not existing_peer or not is_access_active(existing_peer):
        await safe_edit_callback_message(
            callback_query.message,
            "❌ Доступ больше не активен.",
            reply_markup=create_main_menu_keyboard(user_id),
        )
        return
    keyboard, _ = client_config_keyboard(db, user_id, callback_data.page)
    await safe_edit_callback_message(
        callback_query.message,
        "📥 Выбери файл конфигурации для скачивания.",
        reply_markup=keyboard,
    )


@router.callback_query(ClientConfigCallback.filter(F.action == "view"))
async def show_client_config_details(
    callback_query: types.CallbackQuery,
    db: Database,
    cascade_router: CascadeRouter,
    safe_answer_callback,
    safe_edit_callback_message,
    create_main_menu_keyboard,
    is_access_active,
    callback_data: ClientConfigCallback,
) -> None:
    await safe_answer_callback(callback_query)
    user_id = callback_query.from_user.id
    access = db.get_peer_by_telegram_id(user_id)
    config = db.get_client_peer(callback_data.peer_id, user_id)
    if (
        db.is_client_banned(user_id)
        or not access
        or not is_access_active(access)
        or not config
        or config["role"] != MANAGED_CONFIG_ROLE
        or not config["admin_enabled"]
    ):
        await safe_edit_callback_message(
            callback_query.message,
            "❌ Этот конфиг недоступен.",
            reply_markup=create_main_menu_keyboard(user_id),
        )
        return
    try:
        server_name = cascade_router.get_server_name(str(config["server_key"]))
    except CascadeError:
        server_name = str(config.get("server_key") or "не назначена")
    await safe_edit_callback_message(
        callback_query.message,
        client_config_details_text(config, server_name),
        reply_markup=client_config_details_keyboard(config, callback_data.page),
    )


@router.callback_query(ClientConfigCallback.filter(F.action.in_({"create", "rename"})))
async def start_client_config_workflow(
    callback_query: types.CallbackQuery,
    db: Database,
    safe_answer_callback,
    safe_edit_callback_message,
    create_main_menu_keyboard,
    is_access_active,
    callback_data: ClientConfigCallback,
) -> None:
    await safe_answer_callback(callback_query)
    user_id = callback_query.from_user.id
    access = db.get_peer_by_telegram_id(user_id)
    if db.is_client_banned(user_id) or not access or not is_access_active(access):
        await safe_edit_callback_message(
            callback_query.message,
            "❌ Нужна действующая оплаченная подписка.",
            reply_markup=create_main_menu_keyboard(user_id),
        )
        return
    peer_id = int(callback_data.peer_id)
    if callback_data.action == "create":
        if db.count_managed_configs(user_id) >= MAX_CLIENT_CONFIGS:
            await safe_edit_callback_message(
                callback_query.message,
                "❌ Достигнут лимит: три файла конфигурации.",
                reply_markup=client_config_keyboard(db, user_id)[0],
            )
            return
        state = "await_create_name"
        prompt = (
            "Придумай название файла конфигурации.\n"
            "Можешь указать устройство на котором будет использоваться этот файл"
        )
    else:
        config = db.get_client_peer(peer_id, user_id)
        if (
            not config
            or config["role"] != MANAGED_CONFIG_ROLE
            or not config["admin_enabled"]
        ):
            await safe_edit_callback_message(callback_query.message, "❌ Конфиг недоступен.")
            return
        state = "await_rename_name"
        prompt = f"Введи новое название для «{config['config_name']}»."
    set_client_config_workflow(
        db,
        user_id,
        state,
        peer_id=peer_id,
        page=callback_data.page,
        service_chat_id=callback_query.message.chat.id,
        service_message_id=callback_query.message.message_id,
    )
    await safe_edit_callback_message(
        callback_query.message,
        prompt,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data=ClientConfigCallback(action="cancel").pack(),
                    )
                ]
            ]
        ),
    )


@router.callback_query(ClientConfigCallback.filter(F.action == "location"))
async def select_client_config_location(
    callback_query: types.CallbackQuery,
    db: Database,
    cascade_router: CascadeRouter,
    safe_answer_callback,
    safe_edit_callback_message,
    callback_data: ClientConfigCallback,
) -> None:
    await safe_answer_callback(callback_query)
    user_id = callback_query.from_user.id
    flow = get_client_config_workflow(db, user_id)
    if not flow or flow.get("state") != "select_location":
        await safe_edit_callback_message(callback_query.message, "❌ Создание устарело.")
        return
    locations = cascade_router.get_client_production_locations()
    index = int(callback_data.value)
    if index < 0 or index >= len(locations):
        await safe_edit_callback_message(callback_query.message, "❌ Локация недоступна.")
        return
    location = locations[index]
    set_client_config_workflow(
        db,
        user_id,
        "confirm_create",
        **{key: value for key, value in flow.items() if key != "state"},
        server_key=location["server_key"],
        interface_id=location["interface_id"],
        server_name=location["server_name"],
    )
    await safe_edit_callback_message(
        callback_query.message,
        f"Создать конфиг «{flow['config_name']}»?\n\nЛокация: {location['server_name']}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Создать",
                        callback_data=ClientConfigCallback(action="create_confirm").pack(),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data=ClientConfigCallback(action="cancel").pack(),
                    )
                ],
            ]
        ),
    )


@router.callback_query(ClientConfigCallback.filter(F.action == "create_confirm"))
async def create_client_config(
    callback_query: types.CallbackQuery,
    db: Database,
    cascade_router: CascadeRouter,
    safe_answer_callback,
    safe_edit_callback_message,
    send_config_with_confirmation,
) -> None:
    await safe_answer_callback(callback_query)
    user_id = callback_query.from_user.id
    flow = get_client_config_workflow(db, user_id)
    if not flow or flow.get("state") != "confirm_create":
        await safe_edit_callback_message(callback_query.message, "❌ Создание устарело.")
        return
    try:
        config, config_content = await cascade_router.create_managed_config(
            user_id,
            str(flow["config_name"]),
            str(flow["server_key"]),
            str(flow["interface_id"]),
            reassign_existing_group=False,
            self_service_limit=MAX_CLIENT_CONFIGS,
            production_only=True,
        )
    except (CascadeCapacityError, CascadeError):
        logger.exception("Failed to create a self-service configuration")
        await safe_edit_callback_message(
            callback_query.message,
            "❌ Не удалось создать конфиг. Проверь лимит и попробуй позже.",
            reply_markup=client_config_keyboard(db, user_id)[0],
        )
        return
    clear_client_config_workflow(db, user_id)
    db.log_client_config_change(
        user_id,
        int(config["id"]),
        "client_create_config",
        server_key=str(config["server_key"]),
        config_name=str(config["config_name"]),
    )
    server_name = cascade_router.get_server_name(str(config["server_key"]))
    sent = await send_config_with_confirmation(
        callback_query.message.chat.id,
        config_content,
        source_message=callback_query.message,
        caption=None,
        filename=location_config_filename(server_name),
        server_name=server_name,
        reply_markup=config_file_back_keyboard(int(config["id"])),
    )
    if not sent:
        await safe_edit_callback_message(
            callback_query.message,
            "✅ Конфиг создан, но файл не удалось отправить. Скачай его из списка.",
            reply_markup=client_config_keyboard(db, user_id)[0],
        )


@router.callback_query(ClientConfigCallback.filter(F.action == "delete"))
async def confirm_client_config_deletion(
    callback_query: types.CallbackQuery,
    db: Database,
    safe_answer_callback,
    safe_edit_callback_message,
    callback_data: ClientConfigCallback,
) -> None:
    await safe_answer_callback(callback_query)
    user_id = callback_query.from_user.id
    config = db.get_client_peer(callback_data.peer_id, user_id)
    if (
        db.is_client_banned(user_id)
        or not db.has_active_access(user_id)
        or not config
        or config["role"] != MANAGED_CONFIG_ROLE
        or not config["admin_enabled"]
    ):
        await safe_edit_callback_message(callback_query.message, "❌ Конфиг недоступен.")
        return
    await safe_edit_callback_message(
        callback_query.message,
        f"Удалить «{config['config_name']}» навсегда? Это действие нельзя отменить.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🗑 Удалить",
                        callback_data=ClientConfigCallback(
                            action="delete_confirm",
                            peer_id=int(config["id"]),
                            page=callback_data.page,
                        ).pack(),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад",
                        callback_data=ClientConfigCallback(
                            action="view",
                            peer_id=int(config["id"]),
                            page=callback_data.page,
                        ).pack(),
                    )
                ],
            ]
        ),
    )


@router.callback_query(ClientConfigCallback.filter(F.action == "delete_confirm"))
async def delete_client_config(
    callback_query: types.CallbackQuery,
    db: Database,
    cascade_router: CascadeRouter,
    safe_answer_callback,
    safe_edit_callback_message,
    callback_data: ClientConfigCallback,
) -> None:
    await safe_answer_callback(callback_query)
    user_id = callback_query.from_user.id
    config = db.get_client_peer(callback_data.peer_id, user_id)
    if (
        db.is_client_banned(user_id)
        or not db.has_active_access(user_id)
        or not config
        or config["role"] != MANAGED_CONFIG_ROLE
        or not config["admin_enabled"]
    ):
        await safe_edit_callback_message(callback_query.message, "❌ Конфиг недоступен.")
        return
    try:
        deleted, missing = await cascade_router.delete_managed_config(
            user_id, callback_data.peer_id
        )
    except CascadeNotFound:
        await safe_edit_callback_message(callback_query.message, "❌ Конфиг не найден.")
        return
    db.log_client_config_change(
        user_id,
        int(deleted["id"]),
        "client_delete_config",
        server_key=str(deleted["server_key"]),
        config_name=str(deleted["config_name"]),
        cascade_missing=missing,
    )
    await safe_edit_callback_message(
        callback_query.message,
        "✅ Конфиг удалён.",
        reply_markup=client_config_keyboard(db, user_id, callback_data.page)[0],
    )


@router.callback_query(ClientConfigCallback.filter(F.action == "cancel"))
async def cancel_client_config_workflow(
    callback_query: types.CallbackQuery,
    db: Database,
    safe_answer_callback,
    safe_edit_callback_message,
) -> None:
    await safe_answer_callback(callback_query)
    clear_client_config_workflow(db, callback_query.from_user.id)
    await safe_edit_callback_message(
        callback_query.message,
        "Действие отменено.",
        reply_markup=client_config_keyboard(db, callback_query.from_user.id)[0],
    )


@router.message(ActiveClientConfigWorkflow())
async def capture_client_config_name(
    message: types.Message,
    db: Database,
    cascade_router: CascadeRouter,
) -> None:
    user_id = message.from_user.id
    flow = get_client_config_workflow(db, user_id)
    if not flow or flow.get("state") not in {"await_create_name", "await_rename_name"}:
        return
    if db.is_client_banned(user_id) or not db.has_active_access(user_id):
        clear_client_config_workflow(db, user_id)
        return
    try:
        config_name = normalize_config_name(message.text or "")
    except ValueError:
        await edit_telegram_text(
            message.bot,
            flow["service_chat_id"],
            flow["service_message_id"],
            "Название должно содержать от 1 до 48 символов без управляющих знаков.",
        )
        with suppress(Exception):
            await message.delete()
        return
    peer_id = int(flow.get("peer_id", 0))
    duplicate = any(
        str(item.get("config_name") or "").casefold() == config_name.casefold()
        and int(item["id"]) != peer_id
        for item in db.get_managed_client_configs(user_id)
    )
    if duplicate:
        await edit_telegram_text(
            message.bot,
            flow["service_chat_id"],
            flow["service_message_id"],
            "У тебя уже есть конфиг с таким названием.",
        )
        with suppress(Exception):
            await message.delete()
        return
    if flow["state"] == "await_rename_name":
        config = db.get_client_peer(peer_id, user_id)
        if (
            not config
            or config["role"] != MANAGED_CONFIG_ROLE
            or not config["admin_enabled"]
            or not db.rename_managed_config(peer_id, user_id, config_name)
        ):
            await edit_telegram_text(
                message.bot,
                flow["service_chat_id"],
                flow["service_message_id"],
                "❌ Не удалось переименовать конфиг.",
            )
            return
        clear_client_config_workflow(db, user_id)
        db.log_client_config_change(
            user_id,
            peer_id,
            "client_rename_config",
            server_key=str(config.get("server_key") or ""),
            config_name=config_name,
        )
        refreshed = db.get_client_peer(peer_id, user_id)
        server_name = cascade_router.get_server_name(str(refreshed["server_key"]))
        await edit_telegram_text(
            message.bot,
            flow["service_chat_id"],
            flow["service_message_id"],
            f"✅ Конфиг переименован в «{config_name}».\n\n"
            + client_config_details_text(refreshed, server_name),
            reply_markup=client_config_details_keyboard(
                refreshed, int(flow.get("page", 0))
            ),
        )
    else:
        if db.count_managed_configs(user_id) >= MAX_CLIENT_CONFIGS:
            clear_client_config_workflow(db, user_id)
            return
        locations = cascade_router.get_client_production_locations()
        if not locations:
            clear_client_config_workflow(db, user_id)
            await edit_telegram_text(
                message.bot,
                flow["service_chat_id"],
                flow["service_message_id"],
                "❌ Сейчас нет доступных локаций.",
            )
            return
        set_client_config_workflow(
            db,
            user_id,
            "select_location",
            **{key: value for key, value in flow.items() if key != "state"},
            config_name=config_name,
        )
        rows = [
            [
                InlineKeyboardButton(
                    text=location["server_name"],
                    callback_data=ClientConfigCallback(
                        action="location", value=index
                    ).pack(),
                )
            ]
            for index, location in enumerate(locations)
        ]
        rows.append(
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=ClientConfigCallback(action="cancel").pack(),
                )
            ]
        )
        await edit_telegram_text(
            message.bot,
            flow["service_chat_id"],
            flow["service_message_id"],
            f"Название: {config_name}\n\nВыбери локацию.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    with suppress(Exception):
        await message.delete()


@router.callback_query(ClientConfigCallback.filter(F.action == "download"))
async def download_client_config(
    callback_query: types.CallbackQuery,
    db: Database,
    cascade_router: CascadeRouter,
    safe_answer_callback,
    safe_edit_callback_message,
    create_back_to_menu_keyboard,
    create_main_menu_keyboard,
    send_config_with_confirmation,
    is_access_active,
    user_action_locks,
    callback_data: ClientConfigCallback,
) -> None:
    await safe_answer_callback(callback_query)
    user_id = callback_query.from_user.id
    async with user_action_locks.hold(user_id):
        existing_peer = db.get_peer_by_telegram_id(user_id)
        config = db.get_client_peer(callback_data.peer_id, user_id)
        if (
            not existing_peer
            or not is_access_active(existing_peer)
            or not config
            or config["role"] != MANAGED_CONFIG_ROLE
            or not config["admin_enabled"]
            or not config["enabled"]
        ):
            await safe_edit_callback_message(
                callback_query.message,
                "❌ Этот файл конфигурации больше недоступен.",
                reply_markup=create_main_menu_keyboard(user_id),
            )
            return
        await safe_edit_callback_message(
            callback_query.message,
            "⏳ Скачиваю конфигурацию...",
            reply_markup=create_back_to_menu_keyboard(),
        )
        try:
            peer_config = await cascade_router.get_managed_config(
                user_id, callback_data.peer_id
            )
            server_name = cascade_router.get_server_name(str(config["server_key"]))
            sent = await send_config_with_confirmation(
                callback_query.message.chat.id,
                peer_config,
                source_message=callback_query.message,
                caption=None,
                filename=location_config_filename(server_name),
                server_name=server_name,
                reply_markup=config_file_back_keyboard(
                    callback_data.peer_id, callback_data.page
                ),
            )
            if not sent:
                await safe_edit_callback_message(
                    callback_query.message,
                    "❌ Не удалось отправить конфигурацию.\n\nИспользуй кнопку ниже, чтобы вернуться в меню:",
                    reply_markup=create_back_to_menu_keyboard(),
                )
            else:
                return
        except CascadeNotFound:
            await safe_edit_callback_message(
                callback_query.message,
                "❌ Файл конфигурации отсутствует на сервере. Администратор должен создать новый.",
                reply_markup=create_main_menu_keyboard(user_id),
            )
        except Exception:
            logger.exception("Error while fetching/restoring configuration")
            await safe_edit_callback_message(
                callback_query.message,
                "❌ Ошибка при получении конфигурации. Попробуй позже или обратись в поддержку.\n\nИспользуй кнопку ниже, чтобы вернуться в меню:",
                reply_markup=create_back_to_menu_keyboard(),
            )


@router.callback_query(F.data == "extend")
async def handle_extend_callback(
    callback_query: types.CallbackQuery,
    db: Database,
    payment_manager: PaymentManager,
    safe_answer_callback,
    safe_edit_callback_message,
    show_menu_from_callback,
    create_main_menu_keyboard,
):
    """Handle the 'Extend access' button."""
    await safe_answer_callback(callback_query)

    user_id = callback_query.from_user.id
    existing_peer = db.get_peer_by_telegram_id(user_id)
    if db.get_client_access_state(user_id).source == "complimentary":
        await safe_edit_callback_message(
            callback_query.message,
            "🎁 У тебя действует бесплатный доступ без ограничения срока.",
            reply_markup=create_main_menu_keyboard(user_id),
        )
        return
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


@router.callback_query(F.data == "status")
async def handle_status_callback(
    callback_query: types.CallbackQuery,
    db: Database,
    safe_answer_callback,
    show_menu_from_callback,
    create_main_menu_keyboard,
    ui_renderer,
):
    """Handle the 'Access status' button."""
    await safe_answer_callback(callback_query)

    user_id = callback_query.from_user.id
    status_text = subscription_status_message(db, user_id)
    if status_text is None:
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

    try:
        await ui_renderer.edit_rich_or_text(
            callback_query.message,
            content=status_text,
            reply_markup=create_main_menu_keyboard(user_id),
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
