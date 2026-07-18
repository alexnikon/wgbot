import datetime
import logging

logger = logging.getLogger(__name__)


def generate_peer_name(
    telegram_username: str | None = None, user_id: int | None = None
) -> str:
    """Use a Telegram username as the Cascade peer name, falling back to user ID."""
    username = (telegram_username or "").strip().lstrip("@")
    peer_name = username if username else str(user_id)
    return peer_name[:50]


def parse_date_flexible(date_str: str) -> datetime.datetime:
    """Parse the SQLite date formats used by the bot."""
    if not date_str:
        raise ValueError("Empty date string")
    for date_format in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(date_str, date_format)
        except ValueError:
            continue
    raise ValueError(f"Invalid date format: {date_str}")


def format_date_for_user(date_str: str) -> str:
    """Format a stored date as DD-MM-YYYY for Telegram messages."""
    try:
        return parse_date_flexible(date_str).strftime("%d-%m-%Y")
    except (ValueError, TypeError) as exc:
        logger.error("Failed to format date %s: %s", date_str, exc)
        return date_str
