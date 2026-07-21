import logging
import time

from aiogram import F, Router, types
from aiogram.filters import CommandStart

from database import Database
from payment import PaymentManager
from utils import format_date_for_user

logger = logging.getLogger(__name__)
router = Router(name="navigation")
START_DEBOUNCE_SECONDS = 5.0
_last_start_sent_at: dict[int, float] = {}

@router.message(CommandStart())
async def cmd_start(
    message: types.Message, create_main_menu_keyboard, chat_panel
):
    """Restore the hidden bootstrap panel without exposing slash commands."""
    user_id = message.from_user.id
    await chat_panel.delete_user_message(message)
    now_monotonic = time.monotonic()
    last_start = _last_start_sent_at.get(user_id, 0.0)
    if now_monotonic - last_start < START_DEBOUNCE_SECONDS:
        logger.info(
            "Skipping duplicate hidden start for user %s within %ss",
            user_id,
            START_DEBOUNCE_SECONDS,
        )
        return
    _last_start_sent_at[user_id] = now_monotonic
    welcome_text = (
        "👋🏻 Привет! Здесь ты можешь подключиться к быстрому и безопасному VPN.\n\n"
        "Чтобы начать пользоваться нашим VPN, скачай клиент AmneziaWG из своего "
        "магазина приложений. В инструкции есть ссылки на скачивание приложения "
        "и описан процесс подключения."
    )
    await chat_panel.restore_or_create(
        message.chat.id,
        user_id,
        welcome_text,
        create_main_menu_keyboard(user_id),
    )


# Inline button handlers
@router.callback_query(F.data == "pay")
async def handle_pay_callback(
    callback_query: types.CallbackQuery,
    payment_manager: PaymentManager,
    safe_answer_callback,
    safe_edit_callback_message,
):
    """Handle the 'Buy access' button."""
    user_id = callback_query.from_user.id

    await safe_answer_callback(callback_query)

    payment_text, keyboard = await payment_manager.get_payment_selection_view(user_id)
    await safe_edit_callback_message(
        callback_query.message,
        payment_text,
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("tariff_label_"))
async def handle_tariff_label_callback(callback_query: types.CallbackQuery, safe_answer_callback):
    """Ignore taps on tariff label rows."""
    await safe_answer_callback(callback_query)


@router.callback_query(F.data == "already_paid")
async def handle_already_paid_callback(
    callback_query: types.CallbackQuery,
    db: Database,
    safe_answer_callback,
    show_menu_from_callback,
    create_main_menu_keyboard,
    is_access_active,
):
    """Handle the 'Access purchased' button."""
    user_id = callback_query.from_user.id
    # IMPORTANT: fetch fresh data from the DB
    existing_peer = db.get_peer_by_telegram_id(user_id)

    # Check if access is active (re-check on every tap)
    if not is_access_active(existing_peer):
        # Access expired but was paid: update keyboard to "Buy access"
        expire_date_str = (
            existing_peer.get("expire_date", "Неизвестно") if existing_peer else "Неизвестно"
        )
        expire_date_formatted = (
            format_date_for_user(expire_date_str)
            if expire_date_str != "Неизвестно"
            else "Неизвестно"
        )
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


@router.callback_query(F.data == "guide")
async def handle_guide_callback(
    callback_query: types.CallbackQuery,
    safe_answer_callback,
    create_guide_keyboard,
    chat_panel,
):
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

    await chat_panel.render_from_message(
        callback_query.message,
        guide_text,
        create_guide_keyboard(),
        user_id=callback_query.from_user.id,
        rich_markdown=(
            "# 📖 Инструкция\n\n"
            "1. Установите AmneziaWG кнопкой ниже.\n"
            "2. Получите конфигурацию в главном меню.\n"
            "3. Импортируйте `.conf` и включите туннель."
        ),
    )


@router.callback_query(F.data == "main")
async def handle_main_callback(
    callback_query: types.CallbackQuery,
    safe_answer_callback,
    show_menu_from_callback,
    create_main_menu_keyboard,
    is_admin,
    clear_admin_state,
):
    """Handle the 'Back to menu' button."""
    await safe_answer_callback(callback_query)

    user_id = callback_query.from_user.id
    if is_admin(user_id):
        clear_admin_state(user_id)

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
