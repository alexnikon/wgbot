import logging

from aiogram import F, Router, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from callbacks import ClientConfigCallback
from cascade_api import CascadeNotFound, CascadeRouter
from database import Database
from payment import PaymentManager
from utils import format_date_for_user, parse_date_flexible

logger = logging.getLogger(__name__)
router = Router(name="access")
CLIENT_CONFIGS_PAGE_SIZE = 8


def client_config_keyboard(
    db: Database, user_id: int, page: int = 0
) -> tuple[InlineKeyboardMarkup, int]:
    configs = db.get_managed_client_configs(user_id, available_only=True)
    pages = max(1, (len(configs) + CLIENT_CONFIGS_PAGE_SIZE - 1) // CLIENT_CONFIGS_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * CLIENT_CONFIGS_PAGE_SIZE
    rows = [
        [
            InlineKeyboardButton(
                text=str(config["config_name"]),
                callback_data=ClientConfigCallback(
                    action="download", peer_id=int(config["id"]), page=page
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
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="main")])
    return InlineKeyboardMarkup(inline_keyboard=rows), len(configs)


def config_filename(config_name: str) -> str:
    safe = "".join(
        character if character.isalnum() or character in " ._-" else "_"
        for character in config_name
    ).strip(" .")
    return f"{(safe or 'nikonVPN')[:48]}.conf"


def config_file_back_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=ClientConfigCallback(
                        action="back", page=max(0, page)
                    ).pack(),
                )
            ]
        ]
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
                text = f"""
⚠️ Твой доступ к VPN истек!

📅 Дата истечения: {formatted}

⚠️ Для продолжения пользования сервисом, необходимо продлить доступ.
                """
            else:
                text = """
❌ У тебя нет активного доступа.

💎 Чтобы получить конфиг, нужно оплатить доступ.
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
                "❌ Сейчас нет доступных конфигов. Обратись в поддержку.",
                reply_markup=create_main_menu_keyboard(user_id),
            )
            return
        await safe_edit_callback_message(
            callback_query.message,
            "📥 Выбери конфиг для скачивания.",
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
                "📥 Выбери конфиг для скачивания.",
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
        "📥 Выбери конфиг для скачивания.",
        reply_markup=keyboard,
    )


@router.callback_query(ClientConfigCallback.filter(F.action == "download"))
async def download_client_config(
    callback_query: types.CallbackQuery,
    db: Database,
    cascade_router: CascadeRouter,
    safe_answer_callback,
    safe_edit_callback_message,
    create_back_to_menu_keyboard,
    create_main_menu_keyboard,
    create_or_restore_peer_for_user,
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
            or config["role"] not in {"primary", "additional"}
            or not config["admin_enabled"]
            or (config["role"] == "additional" and not config["enabled"])
        ):
            await safe_edit_callback_message(
                callback_query.message,
                "❌ Этот конфиг больше недоступен.",
                reply_markup=create_main_menu_keyboard(user_id),
            )
            return
        await safe_edit_callback_message(
            callback_query.message,
            "⏳ Скачиваю конфигурацию...",
            reply_markup=create_back_to_menu_keyboard(),
        )
        try:
            try:
                peer_config = await cascade_router.get_managed_config(
                    user_id, callback_data.peer_id
                )
            except CascadeNotFound:
                if config["role"] != "primary":
                    raise
                logger.warning(
                    "Primary Cascade peer is explicitly missing for user %s", user_id
                )
                ok, err, new_config = await create_or_restore_peer_for_user(
                    user_id,
                    callback_query.from_user.username,
                    existing_peer.get("tariff_key"),
                )
                if not ok:
                    await safe_edit_callback_message(
                        callback_query.message,
                        f"❌ {err}\n\nВыбери действие с помощью кнопок ниже:",
                        reply_markup=create_main_menu_keyboard(user_id),
                    )
                    return
                peer_config = new_config
            sent = await send_config_with_confirmation(
                callback_query.message.chat.id,
                peer_config,
                source_message=callback_query.message,
                caption=None,
                filename=config_filename(str(config["config_name"])),
                server_name=cascade_router.get_server_name(
                    str(config["server_key"])
                ),
                reply_markup=config_file_back_keyboard(callback_data.page),
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
                "❌ Конфиг отсутствует на сервере. Администратор должен создать новый.",
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
        expire_date_formatted = (
            format_date_for_user(expire_date_str)
            if expire_date_str != "Неизвестно"
            else "Неизвестно"
        )
        connected_devices = db.get_peer_count(user_id)
        devices_line = f"\nПодключено устройств: {connected_devices}" if connected_devices else ""

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
                    status_text += (
                        f"\n⏰ Осталось: {days_left} дн. {hours_left} ч. {minutes_left} мин."
                    )
                elif hours_left > 0:
                    status_text += f"\n⏰ Осталось: {hours_left} ч. {minutes_left} мин."
                else:
                    status_text += f"\n⏰ Осталось: {minutes_left} мин."

                if days_left <= 3:
                    status_text += '\n\n⚠️ Доступ к сервису скоро истекает! Нажми "Продлить доступ" для продления.'

                status_text += "\n\nВыбери действие с помощью кнопок ниже:"
            except (ValueError, TypeError):
                status_text = f"""
📊 Статус доступа:

⏰ Доступ закончится: {expire_date_formatted}{devices_line}

Выбери действие с помощью кнопок ниже:
                """

        await ui_renderer.edit_rich_or_text(
            callback_query.message,
            rich_markdown=(
                "# 📊 Статус подписки\n\n"
                + status_text.replace("📊 Статус доступа:", "").strip()
            ),
            fallback_text=status_text,
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
