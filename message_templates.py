from collections.abc import Mapping
from datetime import timedelta
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
        "⏰ Выбери период  доступа к сервису:\n\n"
        "Тариф можно приобрести повторно, срок доступа добавится к текущей подписке:"
    )
    rich_html = (
        f"{rich_bold('⏰ Выбери период  доступа к сервису:')}\n\n"
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
        "• Учти то что один конфиг будет работать только на одном устройстве.\n"
        "• В стоимость подписки входит 3 устройства, для получения дополнительного "
        "конфига, напиши в поддержку.\n\n"
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
        "• Учти то что один конфиг будет работать только на одном устройстве.\n"
        "• В стоимость подписки входит 3 устройства, для получения дополнительного "
        "конфига, напиши в поддержку.\n\n"
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
    heading = f"⏰ Доступ к nikonVPN истекает {deadline}!"
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
        "📊 Статус подписки\n"
        f"⚙️Подключено устройств: {connected_devices}\n"
        f"📅 Дата истечения: {formatted_date}\n"
        "Чтобы продолжить пользоваться сервисом, продли доступ.\n\n"
        "Выбери варианты продления с помощью кнопок ниже 👇:"
    )
    rich_html = (
        f"{rich_bold('📊 Статус подписки')}\n"
        f"⚙️Подключено устройств: {rich_bold(connected_devices)}\n"
        f"📅 Дата истечения: {rich_expire_date}\n"
        "Чтобы продолжить пользоваться сервисом, продли доступ.\n\n"
        "Выбери варианты продления с помощью кнопок ниже 👇:"
    )
    return TelegramText.from_html(plain, rich_html)


def active_subscription_status(
    connected_devices: int,
    expire_date: str,
    formatted_date: str,
    remaining: str,
) -> TelegramText:
    plain = (
        "📊 Статус подписки\n\n"
        f"⚙️Подключено устройств: {connected_devices}\n"
        f"⏰ Доступ закончится: {formatted_date}\n\n"
        f"⏰ Осталось: {remaining}\n\n"
        "Ты можешь продлить действующую подписку, оплатив доступ еще раз, "
        "срок добавится к текущей подписке"
    )
    rich_html = (
        f"{rich_bold('📊 Статус подписки')}\n\n"
        f"⚙️Подключено устройств: {rich_bold(connected_devices)}\n"
        f"⏰ Доступ закончится: {rich_date(expire_date, formatted_date)}\n\n"
        f"⏰ Осталось: {escape_rich_text(remaining)}\n\n"
        "Ты можешь продлить действующую подписку, оплатив доступ еще раз, "
        "срок добавится к текущей подписке"
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
        "‼ Обрати внимание, один конфиг может использоваться только на одном устройстве!"
    )
    rich_html = (
        f"🌍 Локация: {escape_rich_text(server_name)}\n\n"
        f"{rich_bold('✅ Это твой файл конфигурации для доступа к сервису.')}\n"
        "Добавь этот файл в приложение AmneziaWG.\n"
        "‼ Обрати внимание, один конфиг может использоваться только на одном устройстве!"
    )
    return TelegramText.from_html(plain, rich_html)


def initial_config_caption() -> TelegramText:
    plain = (
        "✅ Это твой файл конфигурации для доступа к сервису.\n"
        "Добавь этот файл в приложение AmneziaWG.\n"
        "‼ Обрати внимание, один конфиг может использоваться только на одном устройстве!"
    )
    rich_html = (
        f"{rich_bold('✅ Это твой файл конфигурации для доступа к сервису.')}\n"
        "Добавь этот файл в приложение AmneziaWG.\n"
        "‼ Обрати внимание, один конфиг может использоваться только на одном устройстве!"
    )
    return TelegramText.from_html(plain, rich_html)
