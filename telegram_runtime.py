import asyncio
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import wraps
from typing import Any, TypeVar

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardMarkup, InputRichMessage, Message

from database import Database
from telegram_text import TelegramTextLike, ensure_telegram_text

logger = logging.getLogger(__name__)
T = TypeVar("T")


@dataclass
class _LockEntry:
    lock: asyncio.Lock
    users: int = 0


class UserActionLocks:
    """Serialize critical actions per user without retaining idle locks."""

    def __init__(self) -> None:
        self._entries: dict[int, _LockEntry] = {}
        self._guard = asyncio.Lock()

    @asynccontextmanager
    async def hold(self, user_id: int) -> AsyncIterator[None]:
        async with self._guard:
            entry = self._entries.setdefault(user_id, _LockEntry(asyncio.Lock()))
            entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            async with self._guard:
                entry.users -= 1
                if entry.users == 0 and not entry.lock.locked():
                    self._entries.pop(user_id, None)

    @property
    def active_keys(self) -> int:
        return len(self._entries)

    def snapshot(self) -> dict[str, int]:
        """Return non-sensitive lock gauges for operational metrics."""
        return {
            "locked_users": sum(int(entry.lock.locked()) for entry in self._entries.values()),
            "lock_participants": sum(entry.users for entry in self._entries.values()),
            "tracked_lock_users": len(self._entries),
        }


def serialized_user_action(handler):
    """Serialize one aiogram handler by the originating Telegram user."""

    @wraps(handler)
    async def wrapped(event, *args, **kwargs):
        user = getattr(event, "from_user", None)
        locks = kwargs["user_action_locks"]
        if user is None:
            return await handler(event, *args, **kwargs)
        async with locks.hold(user.id):
            return await handler(event, *args, **kwargs)

    return wrapped


class TelegramSender:
    """Send retry-safe Telegram notifications and track chat reachability."""

    def __init__(self, bot: Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

    async def call(
        self,
        user_id: int,
        operation: Callable[[], Awaitable[T]],
        *,
        retry_safe: bool = True,
    ) -> T | None:
        banned = getattr(self.db, "is_client_banned", None)
        if banned is not None and await asyncio.to_thread(banned, user_id):
            logger.info("Skipping outbound Telegram operation for banned user %s", user_id)
            return None
        identity_verified = getattr(self.db, "is_client_identity_verified", None)
        if identity_verified is not None and not await asyncio.to_thread(
            identity_verified, user_id
        ):
            logger.info(
                "Skipping outbound Telegram operation for unverified client %s",
                user_id,
            )
            return None
        attempts = 3 if retry_safe else 1
        for attempt in range(attempts):
            try:
                result = await operation()
                marker = getattr(self.db, "mark_telegram_reachable", None)
                if marker is not None:
                    await asyncio.to_thread(marker, user_id)
                return result
            except TelegramRetryAfter as exc:
                if attempt + 1 >= attempts:
                    logger.warning("Telegram rate limit exhausted for user %s", user_id)
                    return None
                await asyncio.sleep(float(exc.retry_after))
            except TelegramForbiddenError:
                marker = getattr(self.db, "mark_telegram_unreachable", None)
                if marker is not None:
                    await asyncio.to_thread(
                        marker,
                        user_id,
                        "TelegramForbiddenError",
                    )
                logger.info("Telegram user %s is unreachable", user_id)
                return None
            except TelegramBadRequest as exc:
                logger.warning(
                    "Telegram rejected operation for user %s: %s",
                    user_id,
                    type(exc).__name__,
                )
                return None
            except TelegramNetworkError:
                if attempt + 1 >= attempts:
                    logger.warning("Telegram network retries exhausted for user %s", user_id)
                    return None
                await asyncio.sleep(0.5 * (2**attempt))
        return None


class TelegramUIRenderer:
    """Render rich Telegram views with a plain-text compatibility fallback."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def send_rich_or_text(
        self,
        chat_id: int,
        *,
        content: TelegramTextLike,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> Any:
        return await send_telegram_text(
            self.bot,
            chat_id,
            content,
            reply_markup=reply_markup,
        )

    async def edit_rich_or_text(
        self,
        message: Any,
        *,
        content: TelegramTextLike,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> Any:
        return await edit_telegram_text(
            self.bot,
            message.chat.id,
            message.message_id,
            content,
            reply_markup=reply_markup,
        )


def _message_not_modified(exc: TelegramBadRequest) -> bool:
    return "message is not modified" in str(exc).lower()


async def send_telegram_text(
    bot: Bot,
    chat_id: int,
    content: TelegramTextLike,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Any:
    """Send rich HTML, falling back to regular HTML and then plain text."""
    rendered = ensure_telegram_text(content)
    try:
        return await bot.send_rich_message(
            chat_id=chat_id,
            rich_message=InputRichMessage(html=rendered.html),
            reply_markup=reply_markup,
        )
    except TelegramBadRequest, AttributeError, TypeError:
        try:
            return await bot.send_message(
                chat_id=chat_id,
                text=rendered.regular_html,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        except TelegramBadRequest:
            return await bot.send_message(
                chat_id=chat_id,
                text=rendered.plain,
                reply_markup=reply_markup,
            )


async def edit_telegram_text(
    bot: Bot,
    chat_id: int,
    message_id: int,
    content: TelegramTextLike | None = None,
    *,
    text: TelegramTextLike | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Any:
    """Edit rich HTML, falling back to regular HTML and then plain text."""
    effective_content = content if content is not None else text
    if effective_content is None:
        raise ValueError("Telegram message content is required")
    rendered = ensure_telegram_text(effective_content)
    try:
        return await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            rich_message=InputRichMessage(html=rendered.html),
            reply_markup=reply_markup,
        )
    except (TelegramBadRequest, AttributeError, TypeError) as exc:
        if isinstance(exc, TelegramBadRequest) and _message_not_modified(exc):
            return None
    try:
        return await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=rendered.regular_html,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    except TelegramBadRequest as exc:
        if _message_not_modified(exc):
            return None
    try:
        return await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=rendered.plain,
            reply_markup=reply_markup,
        )
    except TelegramBadRequest as exc:
        if _message_not_modified(exc):
            return None
        raise


async def edit_bound_message(
    message: Message,
    content: TelegramTextLike,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Any:
    """Edit an aiogram-bound message through the common rich renderer."""
    if not all(hasattr(message, attribute) for attribute in ("bot", "chat", "message_id")):
        rendered = ensure_telegram_text(content)
        return await message.edit_text(rendered.plain, reply_markup=reply_markup)
    return await edit_telegram_text(
        message.bot,
        message.chat.id,
        message.message_id,
        content,
        reply_markup=reply_markup,
    )


class ChatPanelService:
    """Keep one persistent, editable control-panel message per private chat."""

    def __init__(self, bot: Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

    async def _save(self, user_id: int, chat_id: int, message_id: int) -> None:
        await asyncio.to_thread(self.db.set_telegram_ui_panel, user_id, chat_id, message_id)

    async def _delete_message(self, chat_id: int, message_id: int) -> None:
        try:
            await self.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramAPIError:
            logger.debug(
                "Unable to delete obsolete panel chat_id=%s message_id=%s",
                chat_id,
                message_id,
            )

    async def adopt(self, message: Message, user_id: int) -> None:
        panel = await asyncio.to_thread(self.db.get_telegram_ui_panel, user_id)
        if panel and (
            int(panel["chat_id"]) != message.chat.id
            or int(panel["message_id"]) != message.message_id
        ):
            await self._delete_message(int(panel["chat_id"]), int(panel["message_id"]))
        await self._save(user_id, message.chat.id, message.message_id)

    async def _edit(
        self,
        chat_id: int,
        message_id: int,
        content: TelegramTextLike,
        reply_markup: InlineKeyboardMarkup | None,
    ) -> bool:
        try:
            await edit_telegram_text(
                self.bot,
                chat_id,
                message_id,
                content,
                reply_markup=reply_markup,
            )
            return True
        except TelegramBadRequest:
            return False
        except TelegramForbiddenError:
            return False

    async def render(
        self,
        chat_id: int,
        user_id: int,
        content: TelegramTextLike,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> Message | None:
        panel = await asyncio.to_thread(self.db.get_telegram_ui_panel, user_id)
        if panel and await self._edit(
            int(panel["chat_id"]),
            int(panel["message_id"]),
            content,
            reply_markup,
        ):
            return None
        if panel:
            await self._delete_message(int(panel["chat_id"]), int(panel["message_id"]))
            await asyncio.to_thread(self.db.delete_telegram_ui_panel, user_id)
        sent = await send_telegram_text(
            self.bot,
            chat_id,
            content,
            reply_markup=reply_markup,
        )
        await self._save(user_id, chat_id, sent.message_id)
        return sent

    async def render_from_message(
        self,
        message: Message,
        content: TelegramTextLike,
        reply_markup: InlineKeyboardMarkup | None = None,
        *,
        user_id: int | None = None,
    ) -> Message | None:
        effective_user_id = user_id or message.chat.id
        await self.adopt(message, effective_user_id)
        return await self.render(
            message.chat.id,
            effective_user_id,
            content,
            reply_markup,
        )

    async def restore_or_create(
        self,
        chat_id: int,
        user_id: int,
        content: TelegramTextLike,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> Message | None:
        return await self.render(
            chat_id,
            user_id,
            content,
            reply_markup,
        )

    async def recreate(
        self,
        chat_id: int,
        user_id: int,
        content: TelegramTextLike,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> Message:
        """Send a fresh panel at the bottom before removing the previous one."""
        panel = await asyncio.to_thread(self.db.get_telegram_ui_panel, user_id)
        sent = await send_telegram_text(
            self.bot,
            chat_id,
            content,
            reply_markup=reply_markup,
        )
        await self._save(user_id, chat_id, sent.message_id)
        if panel and (
            int(panel["chat_id"]) != chat_id
            or int(panel["message_id"]) != sent.message_id
        ):
            await self._delete_message(int(panel["chat_id"]), int(panel["message_id"]))
        return sent

    async def delete_user_message(self, message: Message) -> None:
        try:
            await message.delete()
        except TelegramAPIError:
            logger.debug(
                "Unable to delete incoming message chat_id=%s message_id=%s",
                message.chat.id,
                message.message_id,
            )


_SECRET_PATTERNS = (
    re.compile(r"(?i)(privatekey|presharedkey|token|secret)\s*[:=]\s*\S+"),
    re.compile(r"\b[A-Za-z0-9_=-]{40,}\b"),
)


def redact_telegram_content(value: str, limit: int = 200) -> str:
    """Return a bounded debug preview with credential-like values removed."""
    sanitized = value.replace("\n", " ").strip()
    for pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub("[REDACTED]", sanitized)
    return sanitized[:limit]
