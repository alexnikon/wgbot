from datetime import UTC, datetime

from database import Database
from message_templates import (
    active_subscription_status,
    expired_subscription_status,
    format_remaining_time,
    unavailable_subscription_status,
    welcome_message,
)
from telegram_text import TelegramText
from utils import format_date_for_user


def subscription_status_message(db: Database, user_id: int) -> TelegramText | None:
    """Build one subscription status view for every Telegram entry point."""
    subscription = db.get_peer_by_telegram_id(user_id)
    access_reader = getattr(db, "get_client_access_state", None)
    access_state = access_reader(user_id) if callable(access_reader) else None
    is_complimentary = (
        access_state.source == "complimentary"
        if access_state is not None
        else bool(
            subscription
            and subscription.get("is_complimentary")
            and not subscription.get("is_banned")
        )
    )
    if is_complimentary:
        return TelegramText.from_plain(
            "🎁 Бесплатный доступ активен.\n\n"
            f"Подключённых конфигов: {db.get_peer_count(user_id)}\n"
            "Срок действия: без ограничений"
        )
    expire_date = str((subscription or {}).get("expire_date") or "").strip()
    if not expire_date:
        return None
    connected_devices = db.get_peer_count(user_id)
    formatted_date = format_date_for_user(expire_date)
    try:
        expires_at = datetime.fromisoformat(expire_date)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        time_left = expires_at - datetime.now(UTC)
    except ValueError, TypeError:
        return unavailable_subscription_status(connected_devices, formatted_date)
    if time_left.total_seconds() <= 0:
        return expired_subscription_status(
            connected_devices,
            expire_date,
            formatted_date,
        )
    return active_subscription_status(
        connected_devices,
        expire_date,
        formatted_date,
        format_remaining_time(time_left),
    )


def home_message(db: Database, user_id: int) -> TelegramText:
    """Show the welcome message only until a subscription expiry exists."""
    return subscription_status_message(db, user_id) or welcome_message()
