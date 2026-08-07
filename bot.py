import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot, Dispatcher, types
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from callbacks import ClientConfigCallback
from cascade_api import CascadeRouter
from config import (
    CASCADE_RETRY_INTERVAL_SECONDS,
    LOG_TELEGRAM_CONTENT,
    PROVISIONING_LEASE_SECONDS,
    STARS_RECONCILIATION_INTERVAL_SECONDS,
    SUPPORT_URL,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_TASKS_CONCURRENCY_LIMIT,
    get_admin_telegram_ids,
)
from database import Database
from handlers.access import router as access_router
from handlers.admin import AdminWorkflowService, is_admin
from handlers.admin import router as admin_router
from handlers.fallback import router as fallback_router
from handlers.navigation import router as navigation_router
from handlers.payments import router as payment_router
from logging_setup import configure_logging
from message_templates import config_instructions, initial_config_caption
from payment import PaymentManager
from provisioning import ProvisioningWorker
from services import AppServices
from stars import StarsReconciler
from subscription_notifications import SubscriptionNotificationWorker
from telegram_runtime import (
    ChatPanelService,
    TelegramSender,
    TelegramUIRenderer,
    UserActionLocks,
    redact_telegram_content,
    send_telegram_text,
)
from telegram_text import TelegramText, TelegramTextLike, ensure_telegram_text, rich_date
from utils import (
    format_date_for_user,
    parse_date_flexible,
)
from yookassa_client import YooKassaClient

configure_logging()
logger = logging.getLogger(__name__)

dp = Dispatcher()
bot: Bot
db: Database
cascade_router: CascadeRouter
yookassa_client: YooKassaClient
payment_manager: PaymentManager
app_services: AppServices
user_action_locks: UserActionLocks
telegram_sender: TelegramSender
ui_renderer: TelegramUIRenderer
stars_reconciler: StarsReconciler
admin_workflows: AdminWorkflowService
chat_panel: ChatPanelService


def configure_runtime(services: AppServices) -> None:
    """Inject explicitly created runtime services before polling starts."""
    global app_services, bot, cascade_router, db, payment_manager, yookassa_client
    global user_action_locks, telegram_sender, ui_renderer, stars_reconciler
    global admin_workflows
    global chat_panel
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    app_services = services
    db = services.db
    cascade_router = services.cascade_router
    yookassa_client = services.yookassa_client
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    payment_manager = PaymentManager(
        bot,
        yookassa_client=yookassa_client,
        db=db,
        cascade_router=cascade_router,
    )
    user_action_locks = UserActionLocks()
    services.metrics.set_telegram_gauge_provider(user_action_locks.snapshot)
    telegram_sender = TelegramSender(bot, db)
    ui_renderer = TelegramUIRenderer(bot)
    chat_panel = ChatPanelService(bot, db)
    admin_workflows = AdminWorkflowService(db)
    stars_reconciler = StarsReconciler(
        bot,
        db,
        payment_manager,
        cascade_router,
        notify_admins,
        STARS_RECONCILIATION_INTERVAL_SECONDS,
    )
    dp.workflow_data.update(
        db=db,
        runtime_metrics=services.metrics,
        cascade_router=cascade_router,
        payment_manager=payment_manager,
        user_action_locks=user_action_locks,
        telegram_sender=telegram_sender,
        ui_renderer=ui_renderer,
        chat_panel=chat_panel,
        stars_reconciler=stars_reconciler,
        admin_workflows=admin_workflows,
        safe_answer_callback=safe_answer_callback,
        safe_edit_callback_message=safe_edit_callback_message,
        show_menu_from_callback=show_menu_from_callback,
        create_guide_keyboard=create_guide_keyboard,
        create_back_to_menu_keyboard=create_back_to_menu_keyboard,
        create_home_keyboard=create_home_keyboard,
        create_main_menu_keyboard=create_main_menu_keyboard,
        send_config_with_confirmation=send_config_with_confirmation,
        is_access_active=is_access_active,
        is_admin=is_admin,
        clear_admin_state=admin_workflows.clear,
        notify_admins=notify_admins,
        format_admin_payment_notification=format_admin_payment_notification,
    )


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
            text = event.text or event.caption or ""
            logger.debug(
                "Incoming message operation: user_id=%s, chat_id=%s, content_type=%s, length=%s",
                user_id,
                chat_id,
                event.content_type,
                len(text),
            )
            if LOG_TELEGRAM_CONTENT and text:
                logger.debug(
                    "Incoming message debug preview: user_id=%s, text=%s",
                    user_id,
                    redact_telegram_content(text),
                )
        elif isinstance(event, types.CallbackQuery):
            user_id = event.from_user.id if event.from_user else "unknown"
            message = event.message
            chat_id = message.chat.id if message and message.chat else "unknown"
            callback_type = (event.data or "").split(":", 1)[0].split("_", 1)[0]
            logger.debug(
                "Incoming callback operation: user_id=%s, chat_id=%s, type=%s",
                user_id,
                chat_id,
                callback_type,
            )

        return await handler(event, data)


class ClientIdentityMiddleware:
    """Verify pre-added Telegram IDs before access checks and handlers run."""

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if not user:
            return await handler(event, data)
        activation = await cascade_router.activate_preadded_client(
            int(user.id), getattr(user, "username", None)
        )
        if activation:
            verification = activation["verification"]
            sync_result = activation["sync"]
            user_id = int(user.id)
            if sync_result["failed"]:
                db.add_provisioning_task(
                    user_id,
                    "sync_client_state",
                    {},
                    activation.get("error") or "Identity activation sync failed",
                )
            duplicates = verification.get("duplicate_username_ids") or []
            duplicate_note = (
                f"\n⚠️ Этот username также записан у ID: {', '.join(map(str, duplicates))}"
                if duplicates
                else ""
            )
            queue_note = (
                "\n⚠️ Синхронизация VPN поставлена в очередь."
                if sync_result["failed"]
                else ""
            )
            await notify_admins(
                "✅ Предварительный клиент подтвердил Telegram ID\n\n"
                f"Telegram ID: {user_id}\n"
                f"Username: @{getattr(user, 'username', None) or 'нет'}"
                f"{duplicate_note}{queue_note}"
            )
        return await handler(event, data)


class BannedClientMiddleware:
    """Block the interactive bot surface for administratively banned clients."""

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if not user or not db.is_client_banned(int(user.id)):
            return await handler(event, data)
        if isinstance(event, types.Message) and (
            event.successful_payment or event.refunded_payment
        ):
            return await handler(event, data)
        text = "⛔ Доступ к боту ограничен. Если это ошибка, обратись в поддержку."
        markup = None
        if SUPPORT_URL:
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="💬 Поддержка", url=SUPPORT_URL)]
                ]
            )
        if isinstance(event, types.CallbackQuery):
            await event.answer(text, show_alert=True)
        elif isinstance(event, types.Message):
            await event.answer(text, reply_markup=markup)
        return None


class ConcurrencyMetricsMiddleware:
    """Track active Telegram handlers and concurrency-limit saturation."""

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: types.TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        metrics = data["runtime_metrics"]
        metrics.telegram_handler_started(TELEGRAM_TASKS_CONCURRENCY_LIMIT)
        try:
            return await handler(event, data)
        finally:
            metrics.telegram_handler_finished()


class PanelTrackingMiddleware:
    """Persist callback source messages as the active panel, excluding invoices."""

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: types.CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        message = event.message
        panel = data.get("chat_panel")
        if (
            panel is not None
            and isinstance(message, types.Message)
            and message.invoice is None
            and message.document is None
        ):
            await panel.adopt(message, event.from_user.id)
        return await handler(event, data)


dp.message.outer_middleware(OperationLoggingMiddleware())
dp.callback_query.outer_middleware(OperationLoggingMiddleware())
dp.message.outer_middleware(ClientIdentityMiddleware())
dp.callback_query.outer_middleware(ClientIdentityMiddleware())
dp.message.outer_middleware(BannedClientMiddleware())
dp.callback_query.outer_middleware(BannedClientMiddleware())
dp.callback_query.outer_middleware(PanelTrackingMiddleware())
dp.update.outer_middleware(ConcurrencyMetricsMiddleware())


def format_admin_payment_notification(
    title: str,
    user_id: int,
    username: str | None,
    tariff_name: str,
    amount: str,
    payment_method: str,
    expire_date: str | None = None,
) -> TelegramText:
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
        formatted_date = format_date_for_user(expire_date)
        text += f"\n📅 Новый срок: {formatted_date}"
        return TelegramText.from_plain_with_replacements(
            text,
            {formatted_date: rich_date(expire_date, formatted_date)},
        )
    return TelegramText.from_plain(text)


async def notify_admins(text: TelegramTextLike) -> None:
    """Send a best-effort notification to configured admins."""
    for admin_id in get_admin_telegram_ids():
        try:
            await telegram_sender.call(
                admin_id,
                lambda admin_id=admin_id: send_telegram_text(bot, admin_id, text),
            )
        except TelegramAPIError as e:
            logger.warning(f"Failed to send admin notification to {admin_id}: {e}")
        except Exception as e:
            logger.warning(f"Unexpected admin notification error for {admin_id}: {e}")


async def send_config_file(
    chat_id: int,
    config_content: bytes | str | None,
    caption: TelegramTextLike | None = "📁 Твой файл конфигурации",
    reply_markup: InlineKeyboardMarkup | None = None,
    filename: str = "nikonVPN.conf",
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
        rendered_caption = ensure_telegram_text(caption) if caption is not None else None
        document = types.BufferedInputFile(file=config_bytes, filename=filename)
        try:
            await bot.send_document(
                chat_id=chat_id,
                document=document,
                caption=rendered_caption.regular_html if rendered_caption else None,
                parse_mode="HTML" if rendered_caption else None,
                reply_markup=reply_markup,
            )
        except TelegramBadRequest:
            await bot.send_document(
                chat_id=chat_id,
                document=types.BufferedInputFile(file=config_bytes, filename=filename),
                caption=rendered_caption.plain if rendered_caption else None,
                reply_markup=reply_markup,
            )
        logger.info(f"Config file sent to chat {chat_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to send config file to chat {chat_id}: {e}", exc_info=True)
        return False


async def send_config_with_confirmation(
    chat_id: int,
    config_content: bytes | str | None,
    source_message: types.Message | None = None,
    caption: TelegramTextLike | None = None,
    filename: str = "nikonVPN.conf",
    server_name: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Send a configuration document with its import instructions."""
    if server_name:
        sent = await send_config_file(
            chat_id,
            config_content,
            caption=None,
            reply_markup=None,
            filename=filename,
        )
        if not sent:
            return False
        try:
            message = await send_telegram_text(
                bot,
                chat_id,
                config_instructions(server_name),
                reply_markup=reply_markup,
            )
            await chat_panel.adopt(message, chat_id)
            return True
        except Exception:
            logger.exception("Failed to send configuration instructions to chat %s", chat_id)
            return False

    effective_caption = caption or initial_config_caption()
    return await send_config_file(
        chat_id,
        config_content,
        caption=effective_caption,
        reply_markup=reply_markup,
        filename=filename,
    )


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
    """Render callback state through the persistent chat panel."""
    await chat_panel.render_from_message(message, text, reply_markup)
    return True


async def show_menu_from_callback(
    callback_query: types.CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup
) -> None:
    """Show a menu from any callback source message without crashing on media messages."""
    message = callback_query.message
    if message is None:
        await chat_panel.restore_or_create(
            callback_query.from_user.id,
            callback_query.from_user.id,
            text,
            reply_markup,
        )
        return
    await chat_panel.render_from_message(
        message,
        text,
        reply_markup,
        user_id=callback_query.from_user.id,
    )


def create_home_keyboard() -> InlineKeyboardMarkup:
    """Create a compact keyboard with a main menu button."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="На главную", callback_data="main")]]
    )


# Helper to check active access
def is_access_active(existing_peer: dict) -> bool:
    """Check if the user has active (paid and not expired) access."""
    if not existing_peer:
        logger.debug("is_access_active: no existing_peer")
        return False

    if existing_peer.get("is_banned"):
        return False
    if not existing_peer.get("identity_verified", 1):
        return False
    if existing_peer.get("is_complimentary"):
        return True

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
        logger.error(f"is_access_active: failed to parse date {expire_date_str}: {e}")
        return False


# Build main menu keyboard
def create_main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Create the main menu inline keyboard."""
    existing_peer = db.get_peer_by_telegram_id(user_id)
    access_reader = getattr(db, "get_client_access_state", None)
    access_state = access_reader(user_id) if callable(access_reader) else None
    has_active_access = (
        access_state.active if access_state is not None else is_access_active(existing_peer)
    )

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
        is_complimentary = (
            access_state.source == "complimentary"
            if access_state is not None
            else bool(existing_peer and existing_peer.get("is_complimentary"))
        )
        inline_keyboard = [
            [
                InlineKeyboardButton(
                    text=("🎁 Бесплатный доступ" if is_complimentary else "✅ Доступ приобретен"),
                    callback_data="already_paid",
                )
            ],
            [InlineKeyboardButton(text="ℹ️ Статус подписки", callback_data="status")],
        ]
    else:
        inline_keyboard = [
            [InlineKeyboardButton(text="💳 Купить доступ", callback_data="pay")],
        ]
    if has_active_access and not is_complimentary:
        inline_keyboard.append(
            [InlineKeyboardButton(text="🔄 Продлить подписку", callback_data="extend")]
        )
    if has_active_access:
        config_counter = getattr(db, "count_managed_configs", None)
        config_count = config_counter(user_id) if callable(config_counter) else 0
        inline_keyboard.append(
            [
                InlineKeyboardButton(
                    text=(
                        "Создать файл конфигурации"
                        if config_count == 0
                        else "Файлы конфигурации"
                    ),
                    callback_data=(
                        ClientConfigCallback(action="create").pack()
                        if config_count == 0
                        else "get_config"
                    ),
                )
            ]
        )
    inline_keyboard.extend(
        [
            [
                InlineKeyboardButton(text="📖 Инструкция", callback_data="guide"),
                InlineKeyboardButton(text="💬 Поддержка", url=SUPPORT_URL),
            ],
        ]
    )
    if is_admin(user_id):
        inline_keyboard.append(
            [
                InlineKeyboardButton(
                    text="👥 Управление клиентами", callback_data="admin_manage_clients"
                )
            ]
        )
    keyboard = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
    return keyboard


# Build instruction keyboard
def create_guide_keyboard() -> InlineKeyboardMarkup:
    """Create the instruction keyboard."""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Вернуться в меню", callback_data="main")],
        ]
    )
    return keyboard


def create_back_to_menu_keyboard() -> InlineKeyboardMarkup:
    """Create a single-button keyboard to return to the main menu."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 Вернуться в меню", callback_data="main")]]
    )


# Command handlers
dp.include_router(admin_router)
dp.include_router(navigation_router)
dp.include_router(access_router)
dp.include_router(payment_router)
dp.include_router(fallback_router)


@dp.my_chat_member()
async def handle_bot_chat_member_update(event: types.ChatMemberUpdated, db: Database) -> None:
    """Track private-chat block and unblock events."""
    if event.chat.type != ChatType.PRIVATE:
        return
    user_id = event.chat.id
    if event.new_chat_member.status == ChatMemberStatus.KICKED:
        await asyncio.to_thread(db.mark_telegram_unreachable, user_id, "my_chat_member:kicked")
    elif event.new_chat_member.status in {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.CREATOR,
    }:
        await asyncio.to_thread(db.mark_telegram_reachable, user_id)


@dp.error()
async def telegram_error_handler(event: ErrorEvent) -> bool:
    """Log unhandled update failures without exposing update content."""
    update_id = event.update.update_id if event.update else None
    app_services.metrics.telegram_event("unhandled_errors")
    update_event = event.update.event if event.update else None
    update_type = type(update_event).__name__ if update_event is not None else "unknown"
    from_user = getattr(update_event, "from_user", None)
    if isinstance(event.exception, TelegramForbiddenError) and from_user:
        await asyncio.to_thread(
            db.mark_telegram_unreachable,
            from_user.id,
            "TelegramForbiddenError",
        )
    retry_class = "rate_limit" if isinstance(event.exception, TelegramRetryAfter) else None
    if isinstance(event.exception, TelegramNetworkError):
        retry_class = "network"
    elif isinstance(event.exception, TelegramBadRequest):
        retry_class = "non_retryable"
    logger.error(
        "Unhandled Telegram update error: update_id=%s, update_type=%s, exception=%s, class=%s",
        update_id,
        update_type,
        type(event.exception).__name__,
        retry_class or "other",
        exc_info=(
            type(event.exception),
            event.exception,
            event.exception.__traceback__,
        ),
    )
    return True


async def register_bot_commands() -> None:
    """Expose the public restart command and remove stale chat-specific scopes."""
    await bot.set_my_commands(
        [BotCommand(command="start", description="Перезапустить бота")],
        scope=BotCommandScopeDefault(),
    )
    for admin_id in get_admin_telegram_ids():
        await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=admin_id))


# Periodic check for expired peers and notifications
async def check_expired_peers():
    """Check expired peers and notify users."""
    worker = SubscriptionNotificationWorker(
        bot, db, payment_manager, telegram_sender=telegram_sender
    )
    await worker.run()


async def retry_provisioning_tasks() -> None:
    """Run the durable Cascade provisioning worker."""

    async def send_worker_config(user_id: int, config: bytes) -> bool:
        return await send_config_with_confirmation(user_id, config, caption=None)

    worker = ProvisioningWorker(
        db,
        cascade_router,
        send_worker_config,
        notify_admins,
        CASCADE_RETRY_INTERVAL_SECONDS,
        PROVISIONING_LEASE_SECONDS,
        metrics=app_services.metrics,
    )
    await worker.run()


async def main(services: AppServices):
    """Main bot entry point."""
    background_tasks: list[asyncio.Task] = []
    try:
        configure_runtime(services)
        db.sync_expired_access_statuses()

        cascade_status = await cascade_router.validate()
        logger.info("Cascade startup validation: %s", cascade_status)
        if not any(status.startswith("ok") for status in cascade_status.values()):
            raise RuntimeError("No healthy Cascade server is configured")
        group_reconciliation = await cascade_router.reconcile_client_groups()
        logger.info("Cascade client-group reconciliation: %s", group_reconciliation)
        services.runtime_ready = True

        # Start background checks for expired peers and notifications
        background_tasks = [
            asyncio.create_task(check_expired_peers(), name="subscription-notifications"),
            asyncio.create_task(retry_provisioning_tasks(), name="provisioning-worker"),
            asyncio.create_task(stars_reconciler.run(), name="stars-reconciliation"),
        ]

        # Start the bot
        logger.info("Starting Wgbot app...")
        await register_bot_commands()
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            tasks_concurrency_limit=TELEGRAM_TASKS_CONCURRENCY_LIMIT,
            handle_signals=False,
        )

    except Exception as e:
        logger.error(f"Critical error: {e}")
        raise
    finally:
        for task in background_tasks:
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        services.runtime_ready = False


if __name__ == "__main__":
    raise SystemExit("Run the application through app.py")
