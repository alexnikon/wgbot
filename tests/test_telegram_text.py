import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest

from message_templates import (
    active_access_message,
    active_subscription_status,
    config_instructions,
    expired_period_notice,
    expired_subscription_status,
    format_remaining_time,
    initial_config_caption,
    payment_selection_message,
    renewal_reminder,
    service_guide_message,
    welcome_message,
)
from telegram_runtime import ChatPanelService, send_telegram_text
from telegram_text import TelegramText, rich_date


class TelegramTextTests(unittest.IsolatedAsyncioTestCase):
    def test_authored_templates_match_requested_plain_text(self):
        self.assertEqual(
            expired_period_notice().plain,
            "⚠️ Оплаченный период закончился!\n"
            "Для возобновления доступа к сервису, необходимо оплатить доступ.",
        )
        self.assertEqual(
            welcome_message().plain,
            "👋🏻 Привет! Здесь ты можешь подключиться к быстрому и безопасному VPN.\n\n"
            "Чтобы начать пользоваться сервисом, скачай приложение AmneziaWG из "
            "магазина приложений на твоем устройстве.\n"
            "В инструкции есть ссылки на установку приложения и описан процесс подключения.",
        )
        self.assertEqual(
            initial_config_caption().plain,
            "✅ Это твой файл конфигурации для доступа к сервису.\n"
            "Добавь этот файл в приложение AmneziaWG.\n"
            "‼ Обрати внимание, один конфиг может использоваться только на одном устройстве!",
        )
        self.assertIn("<br><br>", welcome_message().html)
        self.assertIn("\n\n", welcome_message().regular_html)
        self.assertNotIn("<br>", welcome_message().regular_html)

    def test_payment_selection_message_snapshots(self):
        content = payment_selection_message()
        self.assertEqual(
            content.plain,
            "⏰ Выбери период  доступа к сервису:\n\n"
            "Тариф можно приобрести повторно, срок доступа добавится к текущей подписке:",
        )
        self.assertEqual(
            content.html,
            "<b>⏰ Выбери период  доступа к сервису:</b><br><br>"
            "Тариф можно приобрести повторно, срок доступа добавится к текущей подписке:",
        )
        self.assertEqual(
            content.regular_html,
            "<b>⏰ Выбери период  доступа к сервису:</b>\n\n"
            "Тариф можно приобрести повторно, срок доступа добавится к текущей подписке:",
        )

    def test_active_access_message_snapshots(self):
        content = active_access_message()
        self.assertEqual(
            content.plain,
            "✅ У тебя уже есть активный доступ к сервису!\n\n"
            'Нажми "ℹ️ Статус подписки" чтобы проверить информацию по твоей подписке:',
        )
        self.assertEqual(
            content.html,
            "<b>✅ У тебя уже есть активный доступ к сервису!</b><br><br>"
            'Нажми "ℹ️ Статус подписки" чтобы проверить информацию по твоей подписке:',
        )
        self.assertEqual(
            content.regular_html,
            "<b>✅ У тебя уже есть активный доступ к сервису!</b>\n\n"
            'Нажми "ℹ️ Статус подписки" чтобы проверить информацию по твоей подписке:',
        )

    def test_service_guide_message_snapshots(self):
        content = service_guide_message()
        self.assertEqual(
            content.plain,
            "📖 Как подключиться к сервису:\n\n"
            "1️⃣ Скачай приложение AmneziaWG:\n"
            "• Windows: https://github.com/amnezia-vpn/amneziawg-windows-client/releases\n"
            "• Android: Google Play "
            "https://play.google.com/store/apps/details?id=org.amnezia.awg\n"
            "• iOS/macOS: App Store "
            "https://apps.apple.com/pl/app/amneziawg/id6478942365\n\n"
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
            "• Подключись",
        )
        self.assertIn(
            '<a href="https://github.com/amnezia-vpn/'
            'amneziawg-windows-client/releases">Windows</a>',
            content.html,
        )
        self.assertIn(
            '<a href="https://play.google.com/store/apps/'
            'details?id=org.amnezia.awg">Google Play</a>',
            content.html,
        )
        self.assertIn(
            '<a href="https://apps.apple.com/pl/app/amneziawg/id6478942365">'
            "App Store</a>",
            content.html,
        )
        for heading in (
            "📖 Как подключиться к сервису:",
            "1️⃣ Скачай приложение AmneziaWG:",
            "2️⃣ Скачай файл конфигурации:",
            "3️⃣ Добавь файл конфигурации в приложение AmneziaWG:",
        ):
            self.assertIn(f"<b>{heading}</b>", content.html)
        self.assertIn("<br><br>", content.html)
        self.assertNotIn("<br>", content.regular_html)
        self.assertIn("\n\n", content.regular_html)

    def test_reminder_formats_every_price_as_code_and_escapes_names(self):
        content = renewal_reminder(
            "через 1 час",
            {
                "14_days": {
                    "name": "2 < недели",
                    "stars_price": 80,
                    "rub_price": 150,
                },
                "30_days": {
                    "name": "1 месяц",
                    "stars_price": 140,
                    "rub_price": 250,
                },
                "90_days": {
                    "name": "3 месяца",
                    "stars_price": 300,
                    "rub_price": 650,
                },
            },
        )
        self.assertIn(
            "<b>⏰ Доступ к nikonVPN истекает через 1 час!</b>",
            content.html,
        )
        self.assertIn("2 &lt; недели", content.html)
        self.assertIn("<code>80</code> Stars", content.html)
        self.assertIn("<code>150</code> руб.", content.html)
        for amount in (140, 250, 300, 650):
            self.assertIn(f"<code>{amount}</code>", content.html)

    def test_client_status_uses_date_entity_and_bold_device_count(self):
        expired = expired_subscription_status(
            1,
            "2026-07-29 00:00:00",
            "29-07-2026",
        )
        self.assertEqual(
            expired.plain,
            "📊 Статус подписки\n"
            "⚙️Подключено устройств: 1\n"
            "📅 Дата истечения: 29-07-2026\n"
            "Чтобы продолжить пользоваться сервисом, продли доступ.\n\n"
            "Выбери варианты продления с помощью кнопок ниже 👇:",
        )
        self.assertIn("⚙️Подключено устройств: <b>1</b>", expired.html)
        self.assertIn('format="d">29-07-2026</tg-time>', expired.html)

        active = active_subscription_status(
            2,
            "2026-08-04 00:00:00",
            "04-08-2026",
            "6 дн. 14 ч. 48 мин.",
        )
        self.assertEqual(
            active.plain,
            "📊 Статус подписки\n\n"
            "⚙️Подключено устройств: 2\n"
            "⏰ Доступ закончится: 04-08-2026\n\n"
            "⏰ Осталось: 6 дн. 14 ч. 48 мин.\n\n"
            "Ты можешь продлить действующую подписку, оплатив доступ еще раз, "
            "срок добавится к текущей подписке",
        )
        self.assertIn("⚙️Подключено устройств: <b>2</b>", active.html)
        zero_devices = active_subscription_status(
            0,
            "2026-08-04 00:00:00",
            "04-08-2026",
            "9 мин.",
        )
        self.assertIn("⚙️Подключено устройств: <b>0</b>", zero_devices.html)

    def test_remaining_time_keeps_all_existing_variants(self):
        self.assertEqual(
            format_remaining_time(timedelta(days=6, hours=14, minutes=48)),
            "6 дн. 14 ч. 48 мин.",
        )
        self.assertEqual(
            format_remaining_time(timedelta(hours=2, minutes=5)),
            "2 ч. 5 мин.",
        )
        self.assertEqual(
            format_remaining_time(timedelta(minutes=9)),
            "9 мин.",
        )

    def test_config_instructions_escape_location_and_keep_warning(self):
        content = config_instructions("Finland & Helsinki")
        self.assertIn("Finland &amp; Helsinki", content.html)
        self.assertIn(
            "<b>✅ Это твой файл конфигурации для доступа к сервису.</b>",
            content.html,
        )
        self.assertTrue(content.plain.endswith("только на одном устройстве!"))

    def test_date_entity_uses_stored_utc_timestamp(self):
        expected = int(datetime(2030, 1, 1, 9, 30, tzinfo=UTC).timestamp())
        entity = rich_date(
            "2030-01-01 09:30:00",
            "01-01-2030 12:30",
            date_time_format="dt",
        )
        self.assertEqual(
            entity,
            f'<tg-time unix="{expected}" format="dt">01-01-2030 12:30</tg-time>',
        )

    async def test_send_falls_back_from_rich_to_html_and_plain(self):
        bot = SimpleNamespace(
            send_rich_message=AsyncMock(
                side_effect=TelegramBadRequest(SimpleNamespace(), "unsupported")
            ),
            send_message=AsyncMock(
                side_effect=[
                    TelegramBadRequest(SimpleNamespace(), "bad html"),
                    "sent",
                ]
            ),
        )
        content = TelegramText.from_html("Plain", "<b>Rich</b>")
        result = await send_telegram_text(bot, 10, content)
        self.assertEqual(result, "sent")
        self.assertEqual(bot.send_message.await_count, 2)
        self.assertEqual(bot.send_message.await_args.kwargs["text"], "Plain")

    async def test_html_fallback_keeps_real_line_breaks(self):
        bot = SimpleNamespace(
            send_rich_message=AsyncMock(
                side_effect=TelegramBadRequest(SimpleNamespace(), "unsupported")
            ),
            send_message=AsyncMock(return_value="sent"),
        )
        content = TelegramText.from_html(
            "Heading\n\nBody",
            "<b>Heading</b>\n\nBody",
        )
        await send_telegram_text(bot, 10, content)
        self.assertEqual(
            bot.send_rich_message.await_args.kwargs["rich_message"].html,
            "<b>Heading</b><br><br>Body",
        )
        self.assertEqual(
            bot.send_message.await_args.kwargs["text"],
            "<b>Heading</b>\n\nBody",
        )

    async def test_new_panel_is_sent_as_rich_message(self):
        database = SimpleNamespace(
            get_telegram_ui_panel=lambda _user_id: None,
            set_telegram_ui_panel=lambda *_args: True,
        )
        sent_message = SimpleNamespace(message_id=77)
        bot = SimpleNamespace(
            send_rich_message=AsyncMock(return_value=sent_message),
            send_message=AsyncMock(),
        )
        panel = ChatPanelService(bot, database)
        await panel.restore_or_create(10, 10, welcome_message())
        bot.send_rich_message.assert_awaited_once()
        bot.send_message.assert_not_awaited()

    def test_authored_templates_do_not_bypass_rich_runtime(self):
        root = Path(__file__).resolve().parents[1]
        paths = [
            root / "bot.py",
            root / "payment.py",
            root / "subscription_notifications.py",
            *sorted((root / "handlers").glob("*.py")),
        ]
        for path in paths:
            source = path.read_text(encoding="utf-8")
            self.assertNotIn(".send_message(", source, path.name)
            self.assertNotIn(".edit_text(", source, path.name)
            self.assertNotIn("bot.edit_message_text(", source, path.name)


if __name__ == "__main__":
    unittest.main()
