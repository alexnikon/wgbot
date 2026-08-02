from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from telegram_text import (
    TelegramText,
    escape_rich_text,
    rich_bold,
    rich_code,
    rich_date,
)

WINDOWS_CLIENT_URL = (
    "https://github.com/amnezia-vpn/amneziawg-windows-client/releases"
)
ANDROID_CLIENT_URL = (
    "https://play.google.com/store/apps/details?id=org.amnezia.awg"
)
APPLE_CLIENT_URL = "https://apps.apple.com/pl/app/amneziawg/id6478942365"


def welcome_message() -> TelegramText:
    plain = (
        "👋🏻 Привет! Здесь ты можешь подключиться к быстрому и безопасному VPN.\n\n"
        "Чтобы начать пользоваться сервисом, скачай приложение AmneziaWG из "
        "магазина приложений на твоем устройстве.\n"
        "В инструкции есть ссылки на установку приложения и описан процесс подключения."
    )
    rich_html = (
        f"{rich_bold('👋🏻 Привет! Здесь ты можешь подключиться к быстрому и безопасному VPN.')}"
        "\n\nЧтобы начать пользоваться сервисом, скачай приложение "
        f"{rich_bold('AmneziaWG')} из магазина приложений на твоем устройстве.\n"
        "В инструкции есть ссылки на установку приложения и описан процесс подключения."
    )
    return TelegramText.from_html(plain, rich_html)


def payment_selection_message() -> TelegramText:
    plain = (
        "📅 Выбери период  доступа к сервису:\n\n"
        "Тариф можно приобрести повторно, срок доступа добавится к текущей подписке:"
    )
    rich_html = (
        f"{rich_bold('📅 Выбери период  доступа к сервису:')}\n\n"
        "Тариф можно приобрести повторно, срок доступа добавится к текущей подписке:"
    )
    return TelegramText.from_html(plain, rich_html)


def active_access_message() -> TelegramText:
    plain = (
        "✅ У тебя уже есть активный доступ к сервису!\n\n"
        'Нажми "ℹ️ Статус подписки" чтобы проверить информацию по твоей подписке:'
    )
    rich_html = (
        f"{rich_bold('✅ У тебя уже есть активный доступ к сервису!')}\n\n"
        'Нажми "ℹ️ Статус подписки" чтобы проверить информацию по твоей подписке:'
    )
    return TelegramText.from_html(plain, rich_html)


def payment_success_message(tariff_name: str, remaining: str) -> TelegramText:
    remaining_text = remaining.rstrip(".")
    plain = (
        "✅ Оплачено!\n\n"
        f"Доступ к сервису продлен на {tariff_name}\n"
        f"📅 Осталось: {remaining_text}."
    )
    rich_html = (
        f"{rich_bold('✅ Оплачено!')}\n\n"
        f"Доступ к сервису продлен на {escape_rich_text(tariff_name)}\n"
        f"📅 Осталось: {escape_rich_text(remaining_text)}."
    )
    return TelegramText.from_html(plain, rich_html)


def yookassa_refund_success_message(
    amount: object,
    tariff_name: str,
    remaining: str | None,
) -> TelegramText:
    remaining_text = remaining.rstrip(".") if remaining is not None else None
    status_line = (
        f"📅 Осталось: {remaining_text}."
        if remaining_text is not None
        else "📅 Осталось: подписка не активна."
    )
    plain = (
        "💰 Успешный возврат\n\n"
        f"💳 Сумма возврата: {amount} руб.\n"
        f"📉 Оплаченный период уменьшен на {tariff_name}.\n"
        f"{status_line}"
    )
    rich_status_line = (
        f"📅 Осталось: {escape_rich_text(remaining_text)}."
        if remaining_text is not None
        else "📅 Осталось: подписка не активна."
    )
    rich_html = (
        f"{rich_bold('💰 Успешный возврат')}\n\n"
        f"💳 Сумма возврата: {escape_rich_text(amount)} руб.\n"
        f"📉 Оплаченный период уменьшен на {escape_rich_text(tariff_name)}.\n"
        f"{rich_status_line}"
    )
    return TelegramText.from_html(plain, rich_html)


def format_remaining_until(expire_date: str, now: datetime | None = None) -> str:
    """Format the non-negative time remaining until a stored UTC expiry."""
    expires_at = datetime.fromisoformat(expire_date)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return format_remaining_time(max(expires_at - current, timedelta(0)))


def service_guide_message() -> TelegramText:
    plain = (
        "📖 Как подключиться к сервису:\n\n"
        "1️⃣ Скачай приложение AmneziaWG:\n"
        f"• Windows: {WINDOWS_CLIENT_URL}\n"
        f"• Android: Google Play {ANDROID_CLIENT_URL}\n"
        f"• iOS/macOS: App Store {APPLE_CLIENT_URL}\n\n"
        "2️⃣ Скачай файл конфигурации:\n"
        '• Нажми "Получить конфигурацию"\n'
        "• Скачай .conf файл\n"
        "• Учти то что один файл конфигурации будет работать только на одном устройстве.\n"
        "• В стоимость подписки входит 3 устройства, для получения дополнительного "
        "файла конфигурации, напиши в поддержку.\n\n"
        "3️⃣ Добавь файл конфигурации в приложение AmneziaWG:\n"
        "• Открой AmneziaWG\n"
        '• Нажми "Добавить туннель"\n'
        "• Выбери скачаный файл\n"
        "• Подключись"
    )
    rich_html = (
        f"{rich_bold('📖 Как подключиться к сервису:')}\n\n"
        f"{rich_bold('1️⃣ Скачай приложение AmneziaWG:')}\n"
        f'• <a href="{WINDOWS_CLIENT_URL}">Windows</a>\n'
        f'• Android: <a href="{ANDROID_CLIENT_URL}">Google Play</a>\n'
        f'• iOS/macOS: <a href="{APPLE_CLIENT_URL}">App Store</a>\n\n'
        f"{rich_bold('2️⃣ Скачай файл конфигурации:')}\n"
        '• Нажми "Получить конфигурацию"\n'
        "• Скачай .conf файл\n"
        "• Учти то что один файл конфигурации будет работать только на одном устройстве.\n"
        "• В стоимость подписки входит 3 устройства, для получения дополнительного "
        "файла конфигурации, напиши в поддержку.\n\n"
        f"{rich_bold('3️⃣ Добавь файл конфигурации в приложение AmneziaWG:')}\n"
        "• Открой AmneziaWG\n"
        '• Нажми "Добавить туннель"\n'
        "• Выбери скачаный файл\n"
        "• Подключись"
    )
    return TelegramText.from_html(plain, rich_html)


def expired_period_notice() -> TelegramText:
    plain = (
        "⚠️ Оплаченный период закончился!\n"
        "Для возобновления доступа к сервису, необходимо оплатить доступ."
    )
    rich_html = (
        f"{rich_bold('⚠️ Оплаченный период закончился!')}\n"
        "Для возобновления доступа к сервису, необходимо оплатить доступ."
    )
    return TelegramText.from_html(plain, rich_html)


def renewal_reminder(
    deadline: str,
    tariffs: Mapping[str, Mapping[str, Any]],
) -> TelegramText:
    heading = f"📅 Доступ к сервису истекает {deadline}!"
    plain_lines = [heading, "💎 Доступные варианты продления:", ""]
    rich_lines = [rich_bold(heading), "💎 Доступные варианты продления:", ""]
    for tariff in tariffs.values():
        name = str(tariff["name"])
        stars_price = str(tariff["stars_price"])
        rub_price = str(tariff["rub_price"])
        plain_lines.extend(
            [
                f"⭐ {name} - {stars_price} Stars",
                f"💳 {name} - {rub_price} руб.",
                "",
            ]
        )
        escaped_name = escape_rich_text(name)
        rich_lines.extend(
            [
                f"⭐ {escaped_name} - {rich_code(stars_price)} Stars",
                f"💳 {escaped_name} - {rich_code(rub_price)} руб.",
                "",
            ]
        )
    return TelegramText.from_html(
        "\n".join(plain_lines).rstrip(),
        "\n".join(rich_lines).rstrip(),
    )


def expired_subscription_status(
    connected_devices: int,
    expire_date: str,
    formatted_date: str,
) -> TelegramText:
    try:
        rich_expire_date = rich_date(expire_date, formatted_date)
    except TypeError, ValueError:
        rich_expire_date = escape_rich_text(formatted_date)
    plain = (
        "📊 Статус подписки - Неактивна\n\n"
        "📅 Осталось: 0 мин.\n"
        f"📅 Доступ закончился: {formatted_date}\n"
        f"📱Подключено устройств: {connected_devices}\n\n"
        "Чтобы продолжить пользоваться сервисом, продли доступ.\n\n"
        "Нажми 💳 Купить доступ чтобы возобновить доступ к сервису"
    )
    rich_html = (
        f"{rich_bold('📊 Статус подписки - Неактивна')}\n\n"
        "📅 Осталось: 0 мин.\n"
        f"📅 Доступ закончился: {rich_expire_date}\n"
        f"📱Подключено устройств: {rich_bold(connected_devices)}\n\n"
        "Чтобы продолжить пользоваться сервисом, продли доступ.\n\n"
        f"Нажми 💳 {rich_bold('Купить доступ')} чтобы возобновить доступ к сервису"
    )
    return TelegramText.from_html(plain, rich_html)


def active_subscription_status(
    connected_devices: int,
    expire_date: str,
    formatted_date: str,
    remaining: str,
) -> TelegramText:
    plain = (
        "📊 Статус подписки - Активна\n\n"
        f"📅 Осталось: {remaining}\n"
        f"📅 Доступ закончится: {formatted_date}\n"
        f"📱Подключено устройств: {connected_devices}\n\n"
        "Ты можешь продлить действующую подписку, оплатив доступ еще раз, "
        "срок добавится к текущей подписке"
    )
    rich_html = (
        f"{rich_bold('📊 Статус подписки - Активна')}\n\n"
        f"📅 Осталось: {escape_rich_text(remaining)}\n"
        f"📅 Доступ закончится: {rich_date(expire_date, formatted_date)}\n"
        f"📱Подключено устройств: {rich_bold(connected_devices)}\n\n"
        "Ты можешь продлить действующую подписку, оплатив доступ еще раз, "
        "срок добавится к текущей подписке"
    )
    return TelegramText.from_html(plain, rich_html)


def unavailable_subscription_status(
    connected_devices: int,
    formatted_date: str,
) -> TelegramText:
    """Render a safe status card when a stored expiry cannot be parsed."""
    plain = (
        "📊 Статус подписки - Не определена\n\n"
        "📅 Осталось: не определено\n"
        f"📅 Доступ закончится: {formatted_date}\n"
        f"📱Подключено устройств: {connected_devices}\n\n"
        "Не удалось определить текущее состояние подписки. Обратись в поддержку."
    )
    rich_html = (
        f"{rich_bold('📊 Статус подписки - Не определена')}\n\n"
        "📅 Осталось: не определено\n"
        f"📅 Доступ закончится: {escape_rich_text(formatted_date)}\n"
        f"📱Подключено устройств: {rich_bold(connected_devices)}\n\n"
        "Не удалось определить текущее состояние подписки. Обратись в поддержку."
    )
    return TelegramText.from_html(plain, rich_html)


def format_remaining_time(time_left: timedelta) -> str:
    days_left = time_left.days
    hours_left = time_left.seconds // 3600
    minutes_left = (time_left.seconds % 3600) // 60
    if days_left > 0:
        return f"{days_left} дн. {hours_left} ч. {minutes_left} мин."
    if hours_left > 0:
        return f"{hours_left} ч. {minutes_left} мин."
    return f"{minutes_left} мин."


def config_instructions(server_name: str) -> TelegramText:
    plain = (
        f"🌍 Локация: {server_name}\n\n"
        "✅ Это твой файл конфигурации для доступа к сервису.\n"
        "Добавь этот файл в приложение AmneziaWG.\n"
        "‼ Обрати внимание, один файл конфигурации может использоваться только на одном устройстве!"
    )
    rich_html = (
        f"🌍 Локация: {escape_rich_text(server_name)}\n\n"
        f"{rich_bold('✅ Это твой файл конфигурации для доступа к сервису.')}\n"
        "Добавь этот файл в приложение AmneziaWG.\n"
        "‼ Обрати внимание, один файл конфигурации может использоваться только на одном устройстве!"
    )
    return TelegramText.from_html(plain, rich_html)


def initial_config_caption() -> TelegramText:
    plain = (
        "✅ Это твой файл конфигурации для доступа к сервису.\n"
        "Добавь этот файл в приложение AmneziaWG.\n"
        "‼ Обрати внимание, один файл конфигурации может использоваться только на одном устройстве!"
    )
    rich_html = (
        f"{rich_bold('✅ Это твой файл конфигурации для доступа к сервису.')}\n"
        "Добавь этот файл в приложение AmneziaWG.\n"
        "‼ Обрати внимание, один файл конфигурации может использоваться только на одном устройстве!"
    )
    return TelegramText.from_html(plain, rich_html)
