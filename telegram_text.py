import html
import re
import textwrap
from dataclasses import dataclass
from datetime import UTC, datetime

_PRICE_PATTERN = re.compile(
    r"(?<![\d>])(\d+(?:[.,]\d+)?)(?=\s*(?:Stars|зв[её]зд|руб\.))",
    re.IGNORECASE,
)


def _normalize(value: str) -> str:
    return textwrap.dedent(value).strip()


def _style_plain_html(value: str) -> str:
    escaped = html.escape(value)
    escaped = _PRICE_PATTERN.sub(r"<code>\1</code>", escaped)
    lines = escaped.splitlines()
    for index, line in enumerate(lines):
        if line.strip():
            lines[index] = f"<b>{line}</b>"
            break
    return "\n".join(lines)


@dataclass(frozen=True)
class TelegramText:
    """Store one authored Telegram message in rich and plain representations."""

    plain: str
    html: str

    @classmethod
    def from_plain(cls, value: str) -> TelegramText:
        plain = _normalize(value)
        return cls(plain=plain, html=_style_plain_html(plain))

    @classmethod
    def from_html(cls, plain: str, rich_html: str) -> TelegramText:
        return cls(plain=_normalize(plain), html=_normalize(rich_html))

    @classmethod
    def from_plain_with_replacements(
        cls,
        value: str,
        replacements: dict[str, str],
    ) -> TelegramText:
        plain = _normalize(value)
        rich_html = _style_plain_html(plain)
        for source, replacement in replacements.items():
            rich_html = rich_html.replace(html.escape(source), replacement)
        return cls(plain=plain, html=rich_html)


TelegramTextLike = str | TelegramText


def ensure_telegram_text(value: TelegramTextLike) -> TelegramText:
    if isinstance(value, TelegramText):
        return value
    return TelegramText.from_plain(value)


def escape_rich_text(value: object) -> str:
    return html.escape(str(value))


def rich_code(value: object) -> str:
    return f"<code>{escape_rich_text(value)}</code>"


def rich_bold(value: object) -> str:
    return f"<b>{escape_rich_text(value)}</b>"


def rich_date(
    stored_value: str,
    fallback: str,
    *,
    date_time_format: str = "d",
) -> str:
    """Build a Telegram date-time entity from a UTC-compatible stored value."""
    parsed = datetime.fromisoformat(stored_value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    unix_time = int(parsed.timestamp())
    return (
        f'<tg-time unix="{unix_time}" format="{date_time_format}">'
        f"{escape_rich_text(fallback)}</tg-time>"
    )
