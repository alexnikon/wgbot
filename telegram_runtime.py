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
        rich_markdown: str,
        fallback_text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> Any:
        try:
            return await self.bot.send_rich_message(
                chat_id=chat_id,
                rich_message=InputRichMessage(markdown=rich_markdown),
                reply_markup=reply_markup,
            )
        except TelegramBadRequest:
            return await self.bot.send_message(
                chat_id=chat_id,
                text=fallback_text,
                reply_markup=reply_markup,
            )

    async def edit_rich_or_text(
        self,
        message: Any,
        *,
        rich_markdown: str,
        fallback_text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> Any:
        try:
            return await self.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=message.message_id,
                rich_message=InputRichMessage(markdown=rich_markdown),
                reply_markup=reply_markup,
            )
        except TelegramBadRequest:
            return await message.edit_text(fallback_text, reply_markup=reply_markup)


class ChatPanelService:
    """Keep one persistent, editable control-panel message per private chat."""

    def __init__(self, bot: Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

    async def _save(self, user_id: int, chat_id: int, message_id: int) -> None:
        await asyncio.to_thread(
            self.db.set_telegram_ui_panel, user_id, chat_id, message_id
        )

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
            await self._delete_message(
                int(panel["chat_id"]), int(panel["message_id"])
            )
        await self._save(user_id, message.chat.id, message.message_id)

    async def _edit(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None,
        rich_markdown: str | None,
    ) -> bool:
        if rich_markdown:
            try:
                await self.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    rich_message=InputRichMessage(markdown=rich_markdown),
                    reply_markup=reply_markup,
                )
                return True
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    return True
        try:
            await self.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
            )
            return True
        except TelegramBadRequest as exc:
            return "message is not modified" in str(exc).lower()
        except TelegramForbiddenError:
            return False

    async def render(
        self,
        chat_id: int,
        user_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
        *,
        rich_markdown: str | None = None,
    ) -> Message | None:
        panel = await asyncio.to_thread(self.db.get_telegram_ui_panel, user_id)
        if panel and await self._edit(
            int(panel["chat_id"]),
            int(panel["message_id"]),
            text,
            reply_markup,
            rich_markdown,
        ):
            return None
        if panel:
            await self._delete_message(
                int(panel["chat_id"]), int(panel["message_id"])
            )
            await asyncio.to_thread(self.db.delete_telegram_ui_panel, user_id)
        sent = await self.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
        )
        await self._save(user_id, chat_id, sent.message_id)
        return sent

    async def render_from_message(
        self,
        message: Message,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
        *,
        user_id: int | None = None,
        rich_markdown: str | None = None,
    ) -> Message | None:
        effective_user_id = user_id or message.chat.id
        await self.adopt(message, effective_user_id)
        return await self.render(
            message.chat.id,
            effective_user_id,
            text,
            reply_markup,
            rich_markdown=rich_markdown,
        )

    async def restore_or_create(
        self,
        chat_id: int,
        user_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
        *,
        rich_markdown: str | None = None,
    ) -> Message | None:
        return await self.render(
            chat_id,
            user_id,
            text,
            reply_markup,
            rich_markdown=rich_markdown,
        )

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
