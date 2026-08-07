import asyncio
import os
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)

import bot as bot_module
from callbacks import (
    AdminClientCallback,
    AdminConfigCallback,
    ClientConfigCallback,
    PaymentMethod,
    PaymentMethodCallback,
    RefundConfirmationCallback,
)
from cascade_api import CascadeNotFound, ClientDeletionResult
from database import Database
from handlers.access import (
    client_config_keyboard,
    config_file_back_keyboard,
    create_client_config,
    handle_status_callback,
    return_to_client_configs,
    set_client_config_workflow,
)
from handlers.admin import (
    ActiveAdminWorkflow,
    AdminWorkflowService,
    admin_dashboard_keyboard,
    client_card_keyboard,
    client_group_label,
    client_list_keyboard,
    config_details_keyboard,
    config_error_back_keyboard,
    config_list_keyboard,
    confirm_client_deletion,
    confirm_expiry_change,
    confirmed_managed_client_group,
    delete_client,
    delete_managed_config_handler,
    download_paid_client_config,
    format_admin_expiry,
    format_config,
    parse_admin_expiry_input,
    reject_legacy_config_group_selection,
    select_client_group_change,
    select_config_interface,
)
from handlers.fallback import handle_unknown
from handlers.navigation import cmd_start, handle_main_callback, home_message
from handlers.payments import (
    _parse_legacy_method,
    confirm_stars_refund,
    handle_pay_stars_callback,
    process_refunded_payment,
)
from handlers.payments import (
    process_successful_payment as process_successful_stars_payment,
)
from payment import PaymentManager
from stars import StarsReconciler
from telegram_runtime import (
    ChatPanelService,
    TelegramSender,
    TelegramUIRenderer,
    UserActionLocks,
    redact_telegram_content,
)
from telegram_text import TelegramText
from utils import location_config_filename


class TelegramModernizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_main_menu_uses_information_status_icon(self):
        peer = {
            "payment_status": "paid",
            "expire_date": "2099-01-01 00:00:00",
        }
        database = SimpleNamespace(
            get_peer_by_telegram_id=lambda _user_id: peer,
        )
        with patch.object(
            bot_module,
            "db",
            database,
            create=True,
        ):
            keyboard = bot_module.create_main_menu_keyboard(10)
        status_button = keyboard.inline_keyboard[1][0]
        self.assertEqual(status_button.text, "ℹ️ Статус подписки")
        self.assertEqual(status_button.callback_data, "status")

    async def test_guide_keyboard_keeps_only_back_button(self):
        keyboard = bot_module.create_guide_keyboard()
        self.assertEqual(len(keyboard.inline_keyboard), 1)
        button = keyboard.inline_keyboard[0][0]
        self.assertEqual(button.text, "🔙 Вернуться в меню")
        self.assertEqual(button.callback_data, "main")
        self.assertIsNone(button.url)

    async def test_payment_selection_keeps_tariff_and_payment_buttons(self):
        database = SimpleNamespace(get_user_promo_factor=lambda _user_id: 1.0)
        yookassa = SimpleNamespace(shop_id="shop", secret_key="secret")
        manager = PaymentManager(
            SimpleNamespace(),
            yookassa_client=yookassa,
            db=database,
        )
        content, keyboard = await manager.get_payment_selection_view(10)
        self.assertIsInstance(content, TelegramText)
        self.assertEqual(
            content.plain.splitlines()[0],
            "📅 Выбери период  доступа к сервису:",
        )
        labels = [row[0].text for row in keyboard.inline_keyboard[:-1:2]]
        self.assertEqual(labels, ["2 недели", "1 месяц", "3 месяца"])
        payment_rows = keyboard.inline_keyboard[1:-1:2]
        tariffs = list(manager.get_user_tariffs(10).values())
        self.assertEqual(
            [[button.text for button in row] for row in payment_rows],
            [
                [
                    f"⭐ {tariff['stars_price']} Stars",
                    f"💳 {tariff['rub_price']} руб.",
                ]
                for tariff in tariffs
            ],
        )

    async def test_user_action_locks_serialize_and_cleanup(self):
        locks = UserActionLocks()
        active = 0
        maximum = 0

        async def operation():
            nonlocal active, maximum
            async with locks.hold(10):
                active += 1
                maximum = max(maximum, active)
                await asyncio.sleep(0.01)
                active -= 1

        await asyncio.gather(operation(), operation())
        self.assertEqual(maximum, 1)
        self.assertEqual(locks.active_keys, 0)

    async def test_different_users_are_not_serialized(self):
        locks = UserActionLocks()
        entered = asyncio.Event()
        both_entered = asyncio.Event()
        count = 0

        async def operation(user_id):
            nonlocal count
            async with locks.hold(user_id):
                count += 1
                if count == 1:
                    entered.set()
                if count == 2:
                    both_entered.set()
                await asyncio.wait_for(both_entered.wait(), timeout=0.2)

        first = asyncio.create_task(operation(10))
        await entered.wait()
        second = asyncio.create_task(operation(20))
        await asyncio.gather(first, second)
        self.assertEqual(count, 2)

    def test_typed_payment_callback_round_trip(self):
        packed = PaymentMethodCallback(
            method=PaymentMethod.STARS, tariff="30_days", user_id=123
        ).pack()
        unpacked = PaymentMethodCallback.unpack(packed)
        self.assertEqual(unpacked.method, PaymentMethod.STARS)
        self.assertEqual(unpacked.tariff, "30_days")
        self.assertEqual(unpacked.user_id, 123)

    def test_v2_invoice_payload_is_owner_bound(self):
        payment_id = str(uuid.uuid4())
        self.assertEqual(
            PaymentManager.parse_invoice_payload(f"vpn2:{payment_id}:30_days:123"),
            ("stars", "30_days", 123),
        )
        self.assertIsNone(PaymentManager.parse_invoice_payload(f"vpn2:{payment_id}:unknown:123"))

    def test_log_preview_redacts_credentials(self):
        preview = redact_telegram_content(
            "PrivateKey = abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
        )
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", preview)
        self.assertIn("[REDACTED]", preview)

    async def test_rich_renderer_falls_back_to_plain_text(self):
        class FakeBot:
            def __init__(self):
                self.fallback = None

            async def send_rich_message(self, **kwargs):
                raise TelegramBadRequest(SimpleNamespace(), "unsupported")

            async def send_message(self, **kwargs):
                self.fallback = kwargs
                return "sent"

        fake = FakeBot()
        renderer = TelegramUIRenderer(fake)
        result = await renderer.send_rich_or_text(
            10,
            content=TelegramText.from_html("Status", "<b>Status</b>"),
        )
        self.assertEqual(result, "sent")
        self.assertEqual(fake.fallback["text"], "<b>Status</b>")
        self.assertEqual(fake.fallback["parse_mode"], "HTML")

    async def test_chat_panel_restores_persisted_message_without_sending(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            database = Database(path)
            database.set_telegram_ui_panel(10, 10, 77)
            fake_bot = SimpleNamespace(
                edit_message_text=AsyncMock(return_value=True),
                send_message=AsyncMock(),
                delete_message=AsyncMock(),
            )
            panel = ChatPanelService(fake_bot, database)
            await panel.restore_or_create(10, 10, "Main")
            fake_bot.edit_message_text.assert_awaited_once()
            fake_bot.send_message.assert_not_awaited()
            self.assertEqual(database.get_telegram_ui_panel(10)["message_id"], 77)
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_chat_panel_replaces_deleted_message_once(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            database = Database(path)
            database.set_telegram_ui_panel(10, 10, 77)
            fake_bot = SimpleNamespace(
                edit_message_text=AsyncMock(
                    side_effect=TelegramBadRequest(SimpleNamespace(), "message not found")
                ),
                send_message=AsyncMock(return_value=SimpleNamespace(message_id=88)),
                delete_message=AsyncMock(),
            )
            panel = ChatPanelService(fake_bot, database)
            await panel.restore_or_create(10, 10, "Main")
            fake_bot.send_message.assert_awaited_once()
            self.assertEqual(database.get_telegram_ui_panel(10)["message_id"], 88)
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_chat_panel_rich_fallback_edits_same_message(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            database = Database(path)
            database.set_telegram_ui_panel(10, 10, 77)
            fake_bot = SimpleNamespace(
                edit_message_text=AsyncMock(
                    side_effect=[
                        TelegramBadRequest(SimpleNamespace(), "unsupported rich"),
                        True,
                    ]
                ),
                send_message=AsyncMock(),
                delete_message=AsyncMock(),
            )
            panel = ChatPanelService(fake_bot, database)
            await panel.restore_or_create(
                10,
                10,
                TelegramText.from_html("Plain status", "<b>Rich status</b>"),
            )
            self.assertEqual(fake_bot.edit_message_text.await_count, 2)
            fake_bot.send_message.assert_not_awaited()
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass

    def test_polling_source_does_not_use_skip_updates(self):
        import inspect

        source = inspect.getsource(bot_module.main)
        self.assertNotIn("skip_updates", source)
        self.assertIn("tasks_concurrency_limit", source)

    async def test_dispatcher_injects_workflow_dependencies(self):
        from telegram_runtime import serialized_user_action

        dispatcher = Dispatcher()
        router = Router()
        locks = UserActionLocks()
        observed = []

        @router.message()
        @serialized_user_action
        async def injected_handler(message, user_action_locks: UserActionLocks):
            observed.append((message.from_user.id, user_action_locks.active_keys))

        dispatcher.include_router(router)
        dispatcher.workflow_data["user_action_locks"] = locks
        test_bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk")
        try:
            await dispatcher.feed_raw_update(
                test_bot,
                {
                    "update_id": 1,
                    "message": {
                        "message_id": 1,
                        "date": 1,
                        "chat": {"id": 77, "type": "private"},
                        "from": {"id": 77, "is_bot": False, "first_name": "Test"},
                        "text": "hello",
                    },
                },
            )
        finally:
            await test_bot.session.close()

        self.assertEqual(observed, [(77, 1)])
        self.assertEqual(locks.active_keys, 0)

    async def test_config_is_sent_once_with_caption_without_navigation_keyboard(self):
        fake_bot = SimpleNamespace(send_document=AsyncMock())
        with patch.object(bot_module, "bot", fake_bot, create=True):
            self.assertTrue(await bot_module.send_config_with_confirmation(10, b"config"))
        fake_bot.send_document.assert_awaited_once()
        arguments = fake_bot.send_document.await_args.kwargs
        self.assertIn("AmneziaWG", arguments["caption"])
        self.assertNotIn("Конфиг файл", arguments["caption"])
        self.assertIsNone(arguments["reply_markup"])

    async def test_selected_config_includes_server_name_and_location_filename(self):
        events = []
        instruction_message = SimpleNamespace(chat=SimpleNamespace(id=10), message_id=99)

        async def send_document(**_kwargs):
            events.append("document")

        async def send_message(**_kwargs):
            events.append("text")
            return instruction_message

        fake_bot = SimpleNamespace(
            send_document=AsyncMock(side_effect=send_document),
            send_message=AsyncMock(side_effect=send_message),
        )
        fake_panel = SimpleNamespace(adopt=AsyncMock())
        keyboard = config_file_back_keyboard()
        with (
            patch.object(bot_module, "bot", fake_bot, create=True),
            patch.object(bot_module, "chat_panel", fake_panel, create=True),
        ):
            self.assertTrue(
                await bot_module.send_config_with_confirmation(
                    10,
                    b"config",
                    filename=location_config_filename("Netherlands"),
                    server_name="Netherlands",
                    reply_markup=keyboard,
                )
            )
        self.assertEqual(events, ["document", "text"])
        document_arguments = fake_bot.send_document.await_args.kwargs
        self.assertEqual(
            document_arguments["document"].filename,
            "Netherlands.conf",
        )
        self.assertIsNone(document_arguments["caption"])
        self.assertIsNone(document_arguments["reply_markup"])
        text_arguments = fake_bot.send_message.await_args.kwargs
        self.assertIn("🌍 Локация: Netherlands", text_arguments["text"])
        self.assertIn("только на одном устройстве", text_arguments["text"])
        self.assertIs(text_arguments["reply_markup"], keyboard)
        self.assertEqual(
            [button.text for row in keyboard.inline_keyboard for button in row],
            ["⬅️ Назад", "🏠 На главную"],
        )
        self.assertEqual(keyboard.inline_keyboard[1][0].callback_data, "main")
        fake_panel.adopt.assert_awaited_once_with(instruction_message, 10)

    async def test_hidden_start_deletes_input_and_restores_panel(self):
        panel = SimpleNamespace(delete_user_message=AsyncMock(), restore_or_create=AsyncMock())
        clear_admin_state = unittest.mock.Mock()
        message = SimpleNamespace(from_user=SimpleNamespace(id=42), chat=SimpleNamespace(id=42))
        keyboard = SimpleNamespace()
        database = SimpleNamespace(get_peer_by_telegram_id=lambda _user_id: None)
        await cmd_start(
            message,
            database,
            lambda _user_id: keyboard,
            panel,
            clear_admin_state,
            user_action_locks=UserActionLocks(),
        )
        panel.delete_user_message.assert_awaited_once_with(message)
        panel.restore_or_create.assert_awaited_once()
        self.assertIs(panel.restore_or_create.await_args.args[3], keyboard)
        clear_admin_state.assert_called_once_with(42)

    async def test_start_is_not_debounced_and_bypasses_admin_workflow(self):
        active_restores = 0
        maximum_active_restores = 0

        async def restore_or_create(*_args):
            nonlocal active_restores, maximum_active_restores
            active_restores += 1
            maximum_active_restores = max(maximum_active_restores, active_restores)
            await asyncio.sleep(0)
            active_restores -= 1

        panel = SimpleNamespace(
            delete_user_message=AsyncMock(),
            restore_or_create=AsyncMock(side_effect=restore_or_create),
        )
        clear_admin_state = unittest.mock.Mock()
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=42),
            chat=SimpleNamespace(id=42),
        )
        locks = UserActionLocks()
        database = SimpleNamespace(get_peer_by_telegram_id=lambda _user_id: None)
        await asyncio.gather(
            cmd_start(
                message,
                database,
                lambda _user_id: SimpleNamespace(),
                panel,
                clear_admin_state,
                user_action_locks=locks,
            ),
            cmd_start(
                message,
                database,
                lambda _user_id: SimpleNamespace(),
                panel,
                clear_admin_state,
                user_action_locks=locks,
            ),
        )
        self.assertEqual(panel.restore_or_create.await_count, 2)
        self.assertEqual(maximum_active_restores, 1)

        workflow = SimpleNamespace(get=lambda _user_id: {"state": "await_expiry"})
        with patch("handlers.admin.is_admin", return_value=True):
            for text in ("/start", "/start payload", "/start@TestBot payload"):
                command_message = SimpleNamespace(text=text, from_user=SimpleNamespace(id=42))
                self.assertFalse(await ActiveAdminWorkflow()(command_message, workflow))

    async def test_document_callback_is_not_adopted_as_panel(self):
        middleware = bot_module.PanelTrackingMiddleware()
        panel = SimpleNamespace(adopt=AsyncMock())

        class FakeMessage:
            invoice = None
            document = SimpleNamespace()

        event = SimpleNamespace(
            message=FakeMessage(),
            from_user=SimpleNamespace(id=42),
        )
        handler = AsyncMock(return_value="done")
        with patch.object(bot_module.types, "Message", FakeMessage):
            result = await middleware(handler, event, {"chat_panel": panel})
        self.assertEqual(result, "done")
        panel.adopt.assert_not_awaited()

    async def test_document_back_restores_config_menu_without_editing_document(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            database = Database(path)
            database.activate_new_access(42, "alice", 30, "30_days", "stars")
            database.save_client_peer(
                42,
                "server-a",
                "if-a",
                "primary",
                "key-a",
                "alice",
                "primary",
            )
            callback = SimpleNamespace(
                from_user=SimpleNamespace(id=42),
                message=SimpleNamespace(chat=SimpleNamespace(id=42)),
            )
            panel = SimpleNamespace(restore_or_create=AsyncMock())
            answer = AsyncMock()
            await return_to_client_configs(
                callback,
                database,
                panel,
                answer,
                lambda _user_id: SimpleNamespace(),
                lambda _peer: True,
                ClientConfigCallback(action="back", page=0),
            )
            answer.assert_awaited_once_with(callback)
            panel.restore_or_create.assert_awaited_once()
            self.assertEqual(
                panel.restore_or_create.await_args.args[2],
            "📥 Выбери файл конфигурации для скачивания.",
            )
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_unknown_input_is_deleted_without_new_reply(self):
        panel = SimpleNamespace(delete_user_message=AsyncMock(), restore_or_create=AsyncMock())
        message = SimpleNamespace(from_user=SimpleNamespace(id=42), chat=SimpleNamespace(id=42))
        await handle_unknown(message, lambda _user_id: SimpleNamespace(), panel)
        panel.delete_user_message.assert_awaited_once_with(message)
        panel.restore_or_create.assert_awaited_once()

    def test_admin_dashboard_contains_all_button_only_operations(self):
        labels = [
            button.text for row in admin_dashboard_keyboard().inline_keyboard for button in row
        ]
        self.assertIn("👥 Клиенты и скидки", labels)
        self.assertIn("📣 Рассылка", labels)
        self.assertIn("💳 Платежи и расхождения", labels)
        self.assertIn("⭐ Сверить Stars", labels)
        self.assertIn("↩️ Возврат Stars", labels)


class TelegramDatabaseTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = Database(self.path)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except FileNotFoundError:
                pass

    def test_admin_workflow_survives_service_recreation(self):
        first = AdminWorkflowService(self.db)
        first.set(1, "await_message", mode="all")
        second = AdminWorkflowService(Database(self.path))
        self.assertEqual(second.get(1)["state"], "await_message")
        self.assertEqual(second.get(1)["mode"], "all")
        second.clear(1)
        self.assertIsNone(first.get(1))

    def test_home_message_switches_from_welcome_to_subscription_status(self):
        self.assertIn("👋🏻 Привет!", home_message(self.db, 10).plain)

        self.db.ensure_subscription(
            10, "alice", "2099-01-01 00:00:00", "paid", "30_days", "stars"
        )
        active = home_message(self.db, 10)
        self.assertIn("📊 Статус подписки - Активна", active.plain)
        self.assertIn("📅 Осталось:", active.plain)
        self.assertNotIn("👋🏻 Привет!", active.plain)

        self.db.ensure_subscription(
            10, "alice", "2000-01-01 00:00:00", "expired", "30_days", "stars"
        )
        expired = home_message(self.db, 10)
        self.assertIn("📊 Статус подписки - Неактивна", expired.plain)
        self.assertIn("📅 Осталось: 0 мин.", expired.plain)
        self.assertIn("Чтобы продолжить пользоваться сервисом", expired.plain)

    def test_home_message_keeps_status_for_invalid_saved_expiry(self):
        self.db.ensure_subscription(
            10, "alice", "invalid-date", "paid", "30_days", "stars"
        )

        content = home_message(self.db, 10)

        self.assertIn("📊 Статус подписки - Не определена", content.plain)
        self.assertIn("Не удалось определить", content.plain)
        self.assertNotIn("👋🏻 Привет!", content.plain)

    def test_main_button_uses_subscription_status_after_access_was_granted(self):
        self.db.ensure_subscription(
            10, "alice", "2099-01-01 00:00:00", "paid", "30_days", "stars"
        )
        callback = SimpleNamespace(from_user=SimpleNamespace(id=10))
        show_menu = AsyncMock()

        asyncio.run(
            handle_main_callback(
                callback,
                self.db,
                AsyncMock(),
                show_menu,
                lambda _user_id: SimpleNamespace(),
                lambda _user_id: False,
                unittest.mock.Mock(),
            )
        )

        content = show_menu.await_args.args[1]
        self.assertIn("📊 Статус подписки - Активна", content.plain)
        self.assertNotIn("👋🏻 Привет!", content.plain)

    def test_status_button_uses_the_same_active_status_layout(self):
        self.db.ensure_subscription(
            10, "alice", "2099-01-01 00:00:00", "paid", "30_days", "stars"
        )
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=10),
            message=SimpleNamespace(),
        )
        renderer = SimpleNamespace(edit_rich_or_text=AsyncMock())

        asyncio.run(
            handle_status_callback(
                callback,
                self.db,
                AsyncMock(),
                AsyncMock(),
                lambda _user_id: SimpleNamespace(),
                renderer,
            )
        )

        content = renderer.edit_rich_or_text.await_args.kwargs["content"]
        lines = content.plain.splitlines()
        self.assertEqual(lines[0], "📊 Статус подписки - Активна")
        self.assertTrue(lines[2].startswith("📅 Осталось:"))
        self.assertTrue(lines[3].startswith("📅 Доступ закончится:"))
        self.assertTrue(lines[4].startswith("📱Подключено устройств:"))

    def test_group_callback_rejects_mismatched_client(self):
        workflow = AdminWorkflowService(self.db)
        workflow.set(99, "select_client_group", user_id=10, groups=["Basic", "Premium"])
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=99),
            message=SimpleNamespace(edit_text=AsyncMock()),
        )
        with patch("handlers.admin.is_admin", return_value=True):
            asyncio.run(
                select_client_group_change(
                    callback,
                    workflow,
                    AsyncMock(),
                    AdminConfigCallback(action="client_group", user_id=11, value=1),
                )
            )
        self.assertEqual(workflow.get(99)["state"], "select_client_group")
        self.assertIn("устарел", callback.message.edit_text.await_args.args[0])

    def test_additional_config_inherits_confirmed_group_without_group_step(self):
        self.db.save_client_peer(
            10,
            "fin-1",
            "if-a",
            "peer-a",
            "key-a",
            "alice_main",
            "primary",
            client_group="Basic",
        )
        workflow = AdminWorkflowService(self.db)
        workflow.set(
            99,
            "select_config_interface",
            user_id=10,
            config_name="Телефон",
            server_key="fin-1",
            interfaces=[{"id": "if-b", "name": "wg1"}],
        )
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=99),
            message=SimpleNamespace(edit_text=AsyncMock()),
        )
        cascade_router = SimpleNamespace(
            list_assignable_client_groups=AsyncMock(
                return_value=["Basic", "Premium"]
            ),
            build_managed_peer_name=AsyncMock(return_value="alice_Телефон"),
        )

        with patch("handlers.admin.is_admin", return_value=True):
            asyncio.run(
                select_config_interface(
                    callback,
                    self.db,
                    cascade_router,
                    workflow,
                    AsyncMock(),
                    AdminConfigCallback(
                        action="interface", user_id=10, value=0
                    ),
                )
            )

        flow = workflow.get(99)
        self.assertEqual(flow["state"], "confirm_config_create")
        self.assertEqual(flow["client_group"], "Basic")
        self.assertNotIn("groups", flow)
        self.assertIn("Группа: Basic", callback.message.edit_text.await_args.args[0])
        cascade_router.list_assignable_client_groups.assert_awaited_once_with(
            10, "fin-1"
        )

    def test_additional_config_blocks_unknown_or_inconsistent_group(self):
        unknown = [{"client_group": None}]
        inconsistent = [
            {"client_group": "Basic"},
            {"client_group": "Premium"},
        ]
        self.assertIsNone(confirmed_managed_client_group(unknown))
        self.assertIsNone(confirmed_managed_client_group(inconsistent))

        self.db.save_client_peer(
            10,
            "fin-1",
            "if-a",
            "peer-a",
            "key-a",
            "alice_main",
            "primary",
        )
        workflow = AdminWorkflowService(self.db)
        workflow.set(
            99,
            "select_config_interface",
            user_id=10,
            config_name="Телефон",
            server_key="fin-1",
            interfaces=[{"id": "if-b", "name": "wg1"}],
        )
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=99),
            message=SimpleNamespace(edit_text=AsyncMock()),
        )
        cascade_router = SimpleNamespace(
            list_assignable_client_groups=AsyncMock(),
            build_managed_peer_name=AsyncMock(),
        )

        with patch("handlers.admin.is_admin", return_value=True):
            asyncio.run(
                select_config_interface(
                    callback,
                    self.db,
                    cascade_router,
                    workflow,
                    AsyncMock(),
                    AdminConfigCallback(
                        action="interface", user_id=10, value=0
                    ),
                )
            )

        self.assertIsNone(workflow.get(99))
        self.assertIn("Изменить группу", callback.message.edit_text.await_args.args[0])
        cascade_router.list_assignable_client_groups.assert_not_awaited()

    def test_legacy_config_group_workflow_is_cleared(self):
        workflow = AdminWorkflowService(self.db)
        workflow.set(99, "select_config_group", user_id=10, groups=["Basic"])
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=99),
            message=SimpleNamespace(edit_text=AsyncMock()),
        )
        with patch("handlers.admin.is_admin", return_value=True):
            asyncio.run(
                reject_legacy_config_group_selection(
                    callback,
                    workflow,
                    AsyncMock(),
                    AdminConfigCallback(action="group", user_id=10, value=0),
                )
            )
        self.assertIsNone(workflow.get(99))
        self.assertIn("устарел", callback.message.edit_text.await_args.args[0])

    def test_expired_admin_workflow_is_removed(self):
        self.db.set_admin_workflow(1, "input", "waiting", {}, ttl_hours=0)
        self.assertIsNone(self.db.get_admin_workflow(1, "input"))

    def test_named_config_keyboards_hide_deactivated_config_from_client(self):
        self.db.save_client_peer(10, "server-a", "if-a", "primary", "key-a", "alice", "primary")
        self.db.save_client_peer(
            10,
            "server-b",
            "if-b",
            "active",
            "key-b",
            "phone",
            "additional",
            config_name="Телефон",
        )
        self.db.save_client_peer(
            10,
            "server-b",
            "if-b",
            "disabled",
            "key-c",
            "tablet",
            "additional",
            enabled=False,
            config_name="Планшет",
            admin_enabled=False,
        )
        client_keyboard, count = client_config_keyboard(self.db, 10)
        client_labels = [button.text for row in client_keyboard.inline_keyboard for button in row]
        self.assertEqual(count, 2)
        self.assertIn("Конфигурация 1", client_labels)
        self.assertIn("Телефон", client_labels)
        self.assertNotIn("Планшет", client_labels)

        admin_keyboard, _ = config_list_keyboard(self.db, 10)
        admin_labels = [button.text for row in admin_keyboard.inline_keyboard for button in row]
        self.assertTrue(any("Планшет" in label for label in admin_labels))

    def test_explicit_config_creation_sends_file_immediately(self):
        self.db.ensure_subscription(
            10, "alice", "2099-01-01 00:00:00", "paid", "30_days", "stars"
        )
        set_client_config_workflow(
            self.db,
            10,
            "confirm_create",
            config_name="Телефон",
            server_key="fin-1",
            interface_id="if-a",
        )
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=10),
            message=SimpleNamespace(chat=SimpleNamespace(id=10)),
        )
        config = {
            "id": 1,
            "server_key": "fin-1",
            "config_name": "Телефон",
        }
        router = SimpleNamespace(
            create_managed_config=AsyncMock(return_value=(config, b"config")),
            get_server_name=lambda _server_key: "Finland",
        )
        sender = AsyncMock(return_value=True)

        asyncio.run(
            create_client_config(
                callback,
                self.db,
                router,
                AsyncMock(),
                AsyncMock(),
                sender,
            )
        )

        sender.assert_awaited_once()
        self.assertEqual(sender.await_args.args[1], b"config")
        self.assertEqual(sender.await_args.kwargs["filename"], "Finland.conf")

    def test_client_card_exposes_discount_and_config_management(self):
        labels = [button.text for row in client_card_keyboard(10).inline_keyboard for button in row]
        self.assertIn("💸 Скидка", labels)
        self.assertIn("🗂 Конфиги", labels)
        self.assertIn("📅 Срок доступа", labels)
        self.assertIn("👥 Группа", labels)
        self.assertIn("🗑 Удалить клиента", labels)
        self.assertEqual(location_config_filename("USA NY"), "USA-NY.conf")
        self.assertEqual(location_config_filename("Finland / Helsinki"), "Finland-Helsinki.conf")
        empty_keyboard, total = client_list_keyboard(self.db, view="details", page=0)
        self.assertEqual(total, 0)
        self.assertEqual(
            empty_keyboard.inline_keyboard[-1][0].text,
            "⬅️ Управление клиентами",
        )
        error_keyboard = config_error_back_keyboard(10, 20)
        self.assertEqual(error_keyboard.inline_keyboard[0][0].text, "⬅️ Назад")
        self.assertEqual(
            client_group_label(
                {"client_groups": "Basic, Premium", "unknown_group_count": 0}
            ),
            "несогласовано: Basic, Premium",
        )
        self.assertEqual(
            client_group_label(
                {"client_groups": "Basic", "unknown_group_count": 1}
            ),
            "не подтверждена",
        )

    def test_admin_expiry_input_uses_moscow_time(self):
        self.assertEqual(
            parse_admin_expiry_input("01-01-2030"),
            "2030-01-01 20:59:00",
        )
        self.assertEqual(
            parse_admin_expiry_input("01-01-2030 12:30"),
            "2030-01-01 09:30:00",
        )
        self.assertEqual(
            format_admin_expiry("2030-01-01 09:30:00"),
            "01-01-2030 12:30",
        )
        with self.assertRaises(ValueError):
            parse_admin_expiry_input("2030/01/01")

    def test_expiry_confirmation_syncs_and_queues_partial_failure(self):
        self.db.ensure_subscription(10, "alice", "2030-01-01 00:00:00", "paid", "30_days", "stars")
        workflow = AdminWorkflowService(self.db)
        workflow.set(
            99,
            "confirm_expiry",
            user_id=10,
            expire_date="2031-01-01 00:00:00",
            service_chat_id=99,
            service_message_id=1,
        )
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=99),
            message=SimpleNamespace(edit_text=AsyncMock()),
        )
        cascade_router = SimpleNamespace(
            sync_user_access=AsyncMock(
                return_value={"total": 1, "updated": 0, "missing": 0, "failed": 1}
            )
        )
        with patch("handlers.admin.is_admin", return_value=True):
            asyncio.run(
                confirm_expiry_change(
                    callback,
                    self.db,
                    cascade_router,
                    workflow,
                    AsyncMock(),
                    AdminClientCallback(action="expiry_confirm", user_id=10),
                )
            )
        self.assertEqual(self.db.get_subscription_expiry(10), "2031-01-01 00:00:00")
        self.assertIsNone(workflow.get(99))
        self.assertIn(
            "автоматического повтора",
            callback.message.edit_text.await_args.args[0],
        )
        self.assertEqual(self.db.get_runtime_stats()["provisioning_pending"], 1)

    def test_expiry_confirmation_rejects_mismatched_client(self):
        self.db.ensure_subscription(10, "alice", "2030-01-01 00:00:00", "paid", "30_days", "stars")
        workflow = AdminWorkflowService(self.db)
        workflow.set(
            99,
            "confirm_expiry",
            user_id=10,
            expire_date="2031-01-01 00:00:00",
        )
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=99),
            message=SimpleNamespace(edit_text=AsyncMock()),
        )
        cascade_router = SimpleNamespace(sync_user_access=AsyncMock())
        with patch("handlers.admin.is_admin", return_value=True):
            asyncio.run(
                confirm_expiry_change(
                    callback,
                    self.db,
                    cascade_router,
                    workflow,
                    AsyncMock(),
                    AdminClientCallback(action="expiry_confirm", user_id=11),
                )
            )
        cascade_router.sync_user_access.assert_not_awaited()
        self.assertEqual(self.db.get_subscription_expiry(10), "2030-01-01 00:00:00")

    def test_paid_config_details_include_download_and_display_location(self):
        config = {
            "telegram_user_id": 10,
            "id": 20,
            "role": "managed",
            "admin_enabled": 0,
            "enabled": 0,
            "config_name": "Old phone",
            "server_key": "fin-1",
            "interface_id": "if-a",
            "payment_status": "paid",
        }

        labels = [
            button.text for row in config_details_keyboard(config).inline_keyboard for button in row
        ]

        self.assertIn("📥 Скачать конфиг", labels)
        self.assertIn("🗑 Удалить навсегда", labels)
        self.assertIn("Сервер: Finland (fin-1)", format_config(config, "Finland"))
        self.assertIn("Группа: не подтверждена", format_config(config, "Finland"))

    def test_admin_permanent_delete_is_owner_bound_and_audited(self):
        self.db.save_client_peer(
            10,
            "fin-1",
            "if-a",
            "peer-a",
            "key-a",
            "alice_phone",
            "additional",
            config_name="Phone",
            client_group="Basic",
        )
        peer_id = self.db.get_managed_client_configs(10)[0]["id"]
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=99),
            message=SimpleNamespace(edit_text=AsyncMock()),
        )
        router = SimpleNamespace(
            delete_managed_config=AsyncMock(
                return_value=(self.db.get_client_peer(peer_id, 10), False)
            )
        )
        original_delete = router.delete_managed_config

        async def delete_and_persist(user_id, selected_peer_id):
            peer = self.db.get_client_peer(selected_peer_id, user_id)
            self.db.delete_managed_config(selected_peer_id, user_id)
            return peer, False

        original_delete.side_effect = delete_and_persist
        with patch("handlers.admin.is_admin", return_value=True):
            asyncio.run(
                delete_managed_config_handler(
                    callback,
                    self.db,
                    router,
                    AsyncMock(),
                    AdminConfigCallback(
                        action="delete_confirm", user_id=10, peer_id=peer_id
                    ),
                )
            )

        self.assertIsNone(self.db.get_client_peer(peer_id, 10))
        self.assertIn("удалён навсегда", callback.message.edit_text.await_args.args[0])
        with closing(sqlite3.connect(self.path)) as connection:
            operation = connection.execute(
                "SELECT operation FROM operation_logs ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        self.assertEqual(operation, "admin_delete_config")

        router.delete_managed_config.reset_mock()
        router.delete_managed_config.side_effect = CascadeNotFound("not owned")
        with patch("handlers.admin.is_admin", return_value=True):
            asyncio.run(
                delete_managed_config_handler(
                    callback,
                    self.db,
                    router,
                    AsyncMock(),
                    AdminConfigCallback(
                        action="delete_confirm", user_id=11, peer_id=peer_id
                    ),
                )
            )
        router.delete_managed_config.assert_awaited_once_with(11, peer_id)

    def test_admin_client_delete_confirmation_and_self_protection(self):
        self.db.upsert_client(10, "alice")
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=99),
            message=SimpleNamespace(edit_text=AsyncMock()),
        )
        with patch("handlers.admin.is_admin", return_value=True):
            asyncio.run(
                confirm_client_deletion(
                    callback,
                    self.db,
                    AsyncMock(),
                    AdminClientCallback(action="delete", user_id=10),
                )
            )
        confirmation = callback.message.edit_text.await_args.args[0]
        self.assertIn("Telegram ID: 10", confirmation)
        self.assertIn("Платёжная история и аудит сохранятся", confirmation)

        self_callback = SimpleNamespace(
            from_user=SimpleNamespace(id=10),
            message=SimpleNamespace(edit_text=AsyncMock()),
        )
        cascade_router = SimpleNamespace(delete_client=AsyncMock())
        with patch("handlers.admin.is_admin", return_value=True):
            asyncio.run(
                delete_client(
                    self_callback,
                    self.db,
                    cascade_router,
                    AsyncMock(),
                    AdminClientCallback(action="delete_confirm", user_id=10),
                )
            )
        cascade_router.delete_client.assert_not_awaited()
        self.assertIn(
            "Нельзя удалить собственный профиль",
            self_callback.message.edit_text.await_args.args[0],
        )

    def test_admin_client_delete_reports_success_and_stale_callback(self):
        self.db.upsert_client(10, "alice")
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=99),
            message=SimpleNamespace(edit_text=AsyncMock()),
        )

        async def delete_and_persist(user_id, admin_id):
            self.db.delete_client_operational_data(
                admin_id, user_id, deleted=2, already_missing=1
            )
            return ClientDeletionResult(deleted=2, already_missing=1)

        cascade_router = SimpleNamespace(delete_client=AsyncMock(side_effect=delete_and_persist))
        with patch("handlers.admin.is_admin", return_value=True):
            asyncio.run(
                delete_client(
                    callback,
                    self.db,
                    cascade_router,
                    AsyncMock(),
                    AdminClientCallback(action="delete_confirm", user_id=10),
                )
            )
        self.assertIn("Клиент удалён навсегда", callback.message.edit_text.await_args.args[0])

        cascade_router.delete_client.reset_mock()
        with patch("handlers.admin.is_admin", return_value=True):
            asyncio.run(
                delete_client(
                    callback,
                    self.db,
                    cascade_router,
                    AsyncMock(),
                    AdminClientCallback(action="delete_confirm", user_id=10),
                )
            )
        cascade_router.delete_client.assert_not_awaited()
        self.assertIn("Клиент не найден", callback.message.edit_text.await_args.args[0])

    def test_admin_download_sends_file_privately_and_audits(self):
        self.db.ensure_subscription(
            10, "alice", "2030-01-01 00:00:00", "paid", "30_days", "stars"
        )
        self.db.save_client_peer(
            10,
            "fin-1",
            "if-a",
            "peer-a",
            "key-a",
            "alice",
            "additional",
            enabled=False,
            config_name="Old / phone",
            admin_enabled=False,
        )
        peer_id = self.db.get_managed_client_configs(10)[0]["id"]
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=99),
            message=SimpleNamespace(edit_text=AsyncMock(), chat=SimpleNamespace(id=-100)),
        )
        telegram_bot = SimpleNamespace(send_document=AsyncMock())
        router = SimpleNamespace(
            get_admin_managed_config=AsyncMock(
                return_value=(
                    self.db.get_admin_managed_config(peer_id, 10),
                    b"config",
                )
            ),
            get_server_name=lambda _server_key: "Finland",
        )

        with patch("handlers.admin.is_admin", return_value=True):
            asyncio.run(
                download_paid_client_config(
                    callback,
                    telegram_bot,
                    self.db,
                    router,
                    AsyncMock(),
                    AdminConfigCallback(action="download", user_id=10, peer_id=peer_id),
                )
            )

        self.assertEqual(telegram_bot.send_document.await_args.kwargs["chat_id"], 99)
        self.assertEqual(
            telegram_bot.send_document.await_args.kwargs["document"].filename,
            "Finland.conf",
        )
        callback.message.edit_text.assert_not_awaited()
        with closing(sqlite3.connect(self.path)) as connection:
            operation = connection.execute(
                "SELECT operation FROM operation_logs ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        self.assertEqual(operation, "admin_download_config")

    def test_admin_download_rejects_unpaid_client_before_cascade(self):
        self.db.ensure_subscription(10, "alice", None, "unpaid")
        self.db.save_client_peer(10, "fin-1", "if-a", "peer-a", "key-a", "alice", "primary")
        peer_id = self.db.get_managed_client_configs(10)[0]["id"]
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=99),
            message=SimpleNamespace(edit_text=AsyncMock()),
        )
        router = SimpleNamespace(get_admin_managed_config=AsyncMock())

        with patch("handlers.admin.is_admin", return_value=True):
            asyncio.run(
                download_paid_client_config(
                    callback,
                    SimpleNamespace(send_document=AsyncMock()),
                    self.db,
                    router,
                    AsyncMock(),
                    AdminConfigCallback(action="download", user_id=10, peer_id=peer_id),
                )
            )

        router.get_admin_managed_config.assert_not_awaited()
        self.assertIn(
            "подтверждённой оплатой",
            callback.message.edit_text.await_args.args[0],
        )

    def test_daily_legacy_callback_counter_and_zero_streak(self):
        self.db.ensure_telegram_daily_metrics_day()
        self.assertEqual(self.db.get_legacy_callback_zero_streak(), 1)
        self.db.record_telegram_daily_metric("legacy_callbacks")
        self.assertEqual(self.db.get_legacy_callback_zero_streak(), 0)
        self.assertEqual(self.db.get_runtime_stats()["legacy_callbacks_today"], 1)

    def test_star_discrepancy_approval_is_atomic_and_does_not_grant_access(self):
        self.db.record_star_transaction(
            "manual-review",
            "incoming",
            50,
            1,
            transaction_type="invoice_payment",
            user_id=31,
            status="discrepancy",
        )
        review_id = self.db.list_star_discrepancies()[0]["review_id"]
        self.assertTrue(self.db.approve_star_discrepancy(review_id, 999))
        self.assertFalse(self.db.approve_star_discrepancy(review_id, 999))
        self.assertEqual(self.db.count_star_discrepancies(), 0)
        self.assertIsNone(self.db.get_peer_by_telegram_id(31))

    def test_unreachable_clients_are_excluded_and_can_return(self):
        self.db.upsert_client(1, "one")
        self.db.upsert_client(2, "two")
        self.db.mark_telegram_unreachable(1, "TelegramForbiddenError")
        self.assertEqual(self.db.get_client_telegram_ids(), [2])
        self.db.mark_telegram_reachable(1)
        self.assertEqual(self.db.get_client_telegram_ids(), [1, 2])

    def test_star_payment_fields_and_refund_review_do_not_reduce_access(self):
        payment_id = str(uuid.uuid4())
        payload = f"vpn2:{payment_id}:14_days:7"
        self.assertTrue(self.db.create_stars_payment_intent(payment_id, 7, 100, "14_days", payload))
        result = self.db.apply_verified_payment(
            payment_id,
            7,
            "alice",
            100,
            "stars",
            "14_days",
            14,
            telegram_payment_charge_id="charge-1",
            provider_payment_charge_id="provider-1",
            invoice_payload=payload,
        )
        expiry = result["expire_date"]
        self.assertTrue(self.db.mark_stars_refund_observed("charge-1", 100))
        payment = self.db.get_payment_by_id(payment_id)
        self.assertEqual(payment["telegram_payment_charge_id"], "charge-1")
        self.assertEqual(payment["refund_review_status"], "pending_review")
        self.assertEqual(self.db.get_peer_by_telegram_id(7)["expire_date"], expiry)

    def test_star_ledger_distinguishes_payment_and_refund_direction(self):
        self.assertTrue(self.db.record_star_transaction("same-id", "incoming", 100, 1))
        self.assertTrue(self.db.record_star_transaction("same-id", "outgoing", 100, 2))
        self.assertFalse(self.db.record_star_transaction("same-id", "incoming", 100, 1))

    def test_payment_schema_keeps_provider_and_telegram_ids_separate(self):
        payment_id = str(uuid.uuid4())
        payload = f"vpn2:{payment_id}:14_days:9"
        self.db.create_stars_payment_intent(payment_id, 9, 100, "14_days", payload)
        self.db.apply_verified_payment(
            payment_id,
            9,
            None,
            100,
            "stars",
            "14_days",
            14,
            telegram_payment_charge_id="tg-charge",
            provider_payment_charge_id="provider-charge",
            invoice_payload=payload,
        )
        row = self.db.get_payment_by_id(payment_id)
        self.assertEqual(row["telegram_payment_charge_id"], "tg-charge")
        self.assertEqual(row["provider_payment_charge_id"], "provider-charge")

    def test_exact_legacy_star_id_match_is_backfilled_without_reapplying_access(self):
        charge_id = "legacy-charge-id"
        payload = "vpn_access_stars_14_days_19"
        self.db.add_payment(
            charge_id,
            19,
            100,
            "stars",
            "14_days",
            currency="RUB",
        )
        self.db.update_payment_status_by_id(charge_id, "succeeded")
        self.db.record_star_transaction(
            charge_id,
            "incoming",
            100,
            1,
            transaction_type="invoice_payment",
            user_id=19,
            invoice_payload=payload,
            status="discrepancy",
        )

        self.assertEqual(self.db.repair_legacy_star_payment_matches(), 1)
        self.assertEqual(self.db.repair_legacy_star_payment_matches(), 0)
        payment = self.db.get_payment_by_id(charge_id)
        self.assertEqual(payment["telegram_payment_charge_id"], charge_id)
        self.assertEqual(payment["invoice_payload"], payload)
        self.assertEqual(payment["currency"], "XTR")
        self.assertIsNone(self.db.get_peer_by_telegram_id(19))
        self.assertEqual(self.db.count_star_discrepancies(), 0)


class TelegramSenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_forbidden_marks_user_unreachable(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            db = Database(path)
            sender = TelegramSender(SimpleNamespace(), db)

            async def forbidden():
                raise TelegramForbiddenError(SimpleNamespace(), "blocked")

            self.assertIsNone(await sender.call(42, forbidden))
            self.assertNotIn(42, db.get_client_telegram_ids())
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass

    def test_stars_invoice_message_is_persisted_and_cleared(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            database = Database(path)
            payment_id = str(uuid.uuid4())
            payload = f"vpn2:{payment_id}:14_days:10"
            self.assertTrue(
                database.create_stars_payment_intent(payment_id, 10, 100, "14_days", payload)
            )
            self.assertTrue(database.set_stars_invoice_message(payload, 55))
            self.assertEqual(
                database.get_payment_by_invoice_payload(payload)["invoice_message_id"],
                55,
            )
            self.assertTrue(database.set_stars_invoice_message(payload, None))
            self.assertIsNone(
                database.get_payment_by_invoice_payload(payload)["invoice_message_id"]
            )
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_retry_after_retries_only_the_operation(self):
        sender = TelegramSender(SimpleNamespace(), SimpleNamespace())
        attempts = 0

        async def operation():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TelegramRetryAfter(SimpleNamespace(), "slow down", 1)
            return "sent"

        with patch("telegram_runtime.asyncio.sleep", new=AsyncMock()) as sleep:
            self.assertEqual(await sender.call(42, operation), "sent")
        self.assertEqual(attempts, 2)
        sleep.assert_awaited_once_with(1.0)

    async def test_network_error_uses_bounded_backoff(self):
        sender = TelegramSender(SimpleNamespace(), SimpleNamespace())
        operation = AsyncMock(side_effect=TelegramNetworkError(SimpleNamespace(), "offline"))
        with patch("telegram_runtime.asyncio.sleep", new=AsyncMock()) as sleep:
            self.assertIsNone(await sender.call(42, operation))
        self.assertEqual(operation.await_count, 3)
        self.assertEqual(sleep.await_count, 2)

    async def test_bad_request_is_not_retried(self):
        sender = TelegramSender(SimpleNamespace(), SimpleNamespace())
        operation = AsyncMock(side_effect=TelegramBadRequest(SimpleNamespace(), "invalid"))
        self.assertIsNone(await sender.call(42, operation))
        self.assertEqual(operation.await_count, 1)


class TelegramHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_stars_payment_uses_unified_success_and_keeps_config_separate(self):
        successful_payment = SimpleNamespace(
            invoice_payload="payload",
            telegram_payment_charge_id="charge",
            provider_payment_charge_id="provider",
            is_recurring=False,
            is_first_recurring=False,
            subscription_expiration_date=None,
        )
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=10, username="alice"),
            chat=SimpleNamespace(id=10),
            successful_payment=successful_payment,
            bot=SimpleNamespace(delete_message=AsyncMock()),
            answer=AsyncMock(),
        )
        database = SimpleNamespace(
            get_payment_by_invoice_payload=lambda _payload: {
                "payment_id": "payment-1",
                "user_id": 10,
                "amount": 100,
                "currency": "XTR",
                "payment_method": "stars",
                "tariff_key": "14_days",
                "invoice_message_id": None,
            },
            apply_verified_payment=lambda *_args, **_kwargs: {
                "expire_date": "2099-01-01 00:00:00",
                "is_extension": False,
            },
            add_provisioning_task=unittest.mock.Mock(),
            log_operation=unittest.mock.Mock(),
        )
        payment_manager = SimpleNamespace(
            confirm_payment=AsyncMock(return_value=(True, "14_days", 100)),
            parse_invoice_payload=lambda _payload: ("stars", "14_days", 10),
            tariffs={
                "14_days": {
                    "days": 14,
                    "name": "2 недели",
                }
            },
        )
        cascade_router = SimpleNamespace(
            sync_user_access=AsyncMock(
                return_value={"total": 0, "updated": 0, "missing": 0, "failed": 0}
            ),
        )
        panel = SimpleNamespace(delete_user_message=AsyncMock(), render=AsyncMock())
        send_config = AsyncMock(return_value=True)

        await process_successful_stars_payment(
            message,
            database,
            cascade_router,
            payment_manager,
            AsyncMock(),
            lambda *_args, **_kwargs: "admin notification",
            user_action_locks=UserActionLocks(),
            chat_panel=panel,
            create_main_menu_keyboard=lambda _user_id: SimpleNamespace(),
        )

        success = panel.render.await_args.args[2]
        self.assertTrue(success.plain.startswith("✅ Оплачено!"))
        self.assertIn("продлен на 2 недели", success.plain)
        send_config.assert_not_awaited()
        message.answer.assert_not_awaited()

    def test_malformed_legacy_payment_callbacks_are_rejected(self):
        manager = SimpleNamespace(is_tariff_enabled=lambda tariff: tariff == "14_days")
        self.assertIsNone(_parse_legacy_method("pay_stars_bad", "stars", manager))
        self.assertIsNone(_parse_legacy_method("pay_stars_14_days_not-a-user", "stars", manager))
        self.assertIsNone(_parse_legacy_method("pay_stars_unknown_10", "stars", manager))

    async def test_payment_callback_is_acknowledged_before_invoice(self):
        events = []

        async def acknowledge(*_args, **_kwargs):
            events.append("ack")

        class FakePaymentManager:
            def is_tariff_enabled(self, _tariff):
                return True

            async def send_stars_payment_request(self, *_args):
                events.append("invoice")
                return True

        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=55, username="alice"),
            message=SimpleNamespace(chat=SimpleNamespace(id=55)),
        )
        await handle_pay_stars_callback(
            callback,
            FakePaymentManager(),
            acknowledge,
            AsyncMock(),
            lambda: None,
            UserActionLocks(),
            SimpleNamespace(telegram_event=lambda _name: None),
            PaymentMethodCallback(method=PaymentMethod.STARS, tariff="14_days", user_id=55),
        )
        self.assertEqual(events, ["ack", "invoice"])

    async def test_start_command_is_exposed_and_admin_override_is_cleared(self):
        fake_bot = SimpleNamespace(delete_my_commands=AsyncMock(), set_my_commands=AsyncMock())
        with (
            patch.object(bot_module, "bot", fake_bot, create=True),
            patch.object(bot_module, "get_admin_telegram_ids", return_value=[99]),
        ):
            await bot_module.register_bot_commands()
        self.assertEqual(fake_bot.delete_my_commands.await_count, 1)
        fake_bot.set_my_commands.assert_awaited_once()
        command = fake_bot.set_my_commands.await_args.args[0][0]
        self.assertEqual(command.command, "start")
        self.assertEqual(command.description, "Перезапустить бота")

    async def test_my_chat_member_transitions_reachability(self):
        database = SimpleNamespace(
            mark_telegram_unreachable=unittest.mock.Mock(),
            mark_telegram_reachable=unittest.mock.Mock(),
        )
        event = SimpleNamespace(
            chat=SimpleNamespace(id=45, type=ChatType.PRIVATE),
            new_chat_member=SimpleNamespace(status=ChatMemberStatus.KICKED),
        )
        await bot_module.handle_bot_chat_member_update(event, database)
        database.mark_telegram_unreachable.assert_called_once()
        event.new_chat_member.status = ChatMemberStatus.MEMBER
        await bot_module.handle_bot_chat_member_update(event, database)
        database.mark_telegram_reachable.assert_called_once_with(45)

    async def test_duplicate_refund_confirmation_calls_telegram_once(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            database = Database(path)
            payment_id = str(uuid.uuid4())
            payload = f"vpn2:{payment_id}:14_days:70"
            database.create_stars_payment_intent(payment_id, 70, 100, "14_days", payload)
            database.apply_verified_payment(
                payment_id,
                70,
                None,
                100,
                "stars",
                "14_days",
                14,
                telegram_payment_charge_id="charge-70",
                invoice_payload=payload,
            )
            telegram_bot = SimpleNamespace(refund_star_payment=AsyncMock())
            callback = SimpleNamespace(
                from_user=SimpleNamespace(id=1),
                message=SimpleNamespace(edit_text=AsyncMock()),
            )
            callback_data = RefundConfirmationCallback(payment_id=payment_id)
            safe_answer = AsyncMock()
            for _ in range(2):
                await confirm_stars_refund(
                    callback,
                    callback_data,
                    telegram_bot,
                    database,
                    safe_answer,
                    lambda _user_id: True,
                )
            self.assertEqual(telegram_bot.refund_star_payment.await_count, 1)
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_refunded_payment_handler_never_shortens_access(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            database = Database(path)
            payment_id = str(uuid.uuid4())
            payload = f"vpn2:{payment_id}:14_days:80"
            database.create_stars_payment_intent(payment_id, 80, 100, "14_days", payload)
            applied = database.apply_verified_payment(
                payment_id,
                80,
                None,
                100,
                "stars",
                "14_days",
                14,
                telegram_payment_charge_id="charge-80",
                invoice_payload=payload,
            )
            message = SimpleNamespace(
                refunded_payment=SimpleNamespace(
                    telegram_payment_charge_id="charge-80",
                    total_amount=100,
                    invoice_payload=payload,
                ),
                date=SimpleNamespace(timestamp=lambda: 1),
                from_user=SimpleNamespace(id=80),
                chat=SimpleNamespace(id=80),
            )
            panel = SimpleNamespace(delete_user_message=AsyncMock(), render=AsyncMock())
            await process_refunded_payment(
                message,
                database,
                AsyncMock(),
                panel,
                lambda _user_id: None,
            )
            self.assertEqual(
                database.get_peer_by_telegram_id(80)["expire_date"],
                applied["expire_date"],
            )
            self.assertEqual(database.get_payment_by_id(payment_id)["status"], "refunded")
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass


class PreCheckoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_precheckout_accepts_valid_invoice_without_config(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            db = Database(path)
            payment_id = str(uuid.uuid4())
            payload = f"vpn2:{payment_id}:14_days:10"
            db.create_stars_payment_intent(payment_id, 10, 100, "14_days", payload)
            manager = PaymentManager(SimpleNamespace(), db=db)
            answers = []

            async def answer(**kwargs):
                answers.append(kwargs)

            query = SimpleNamespace(
                invoice_payload=payload,
                from_user=SimpleNamespace(id=10),
                total_amount=100,
                currency="XTR",
                answer=answer,
            )
            self.assertTrue(await manager.process_payment(query))
            self.assertEqual(answers, [{"ok": True}])
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass


class ReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_periodic_reconciliation_does_not_send_daily_report(self):
        notify_admins = AsyncMock()
        reconciler = StarsReconciler(
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            notify_admins,
            3600,
        )
        reconciler.run_once = AsyncMock()
        with (
            patch("stars.asyncio.sleep", side_effect=asyncio.CancelledError),
            self.assertRaises(asyncio.CancelledError),
        ):
            await reconciler.run()
        reconciler.run_once.assert_awaited_once()
        notify_admins.assert_not_awaited()

    async def test_reconciliation_reads_multiple_pages(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            database = Database(path)
            transactions = [
                SimpleNamespace(
                    id=f"generic-{index}",
                    amount=1,
                    date=index + 1,
                    source=SimpleNamespace(
                        transaction_type="fragment",
                        user=SimpleNamespace(id=500 + index),
                        invoice_payload=None,
                    ),
                    receiver=None,
                )
                for index in range(101)
            ]
            offsets = []

            class FakeBot:
                async def get_star_transactions(self, *, offset, limit):
                    offsets.append(offset)
                    return SimpleNamespace(transactions=transactions[offset : offset + limit])

            reconciler = StarsReconciler(
                FakeBot(),
                database,
                SimpleNamespace(),
                SimpleNamespace(
                    sync_user_access=AsyncMock(
                        return_value={
                            "total": 0,
                            "updated": 0,
                            "missing": 0,
                            "failed": 0,
                        }
                    )
                ),
                AsyncMock(),
                3600,
            )
            result = await reconciler.run_once()
            self.assertEqual(result.observed, 101)
            self.assertEqual(offsets, [0, 100])
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_missing_successful_update_is_applied_from_star_ledger(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            db = Database(path)
            payment_id = str(uuid.uuid4())
            payload = f"vpn2:{payment_id}:14_days:50"
            db.create_stars_payment_intent(
                payment_id,
                50,
                100,
                "14_days",
                payload,
                {"username": "alice"},
            )
            transaction = SimpleNamespace(
                id="tg-charge-50",
                amount=100,
                date=1000,
                source=SimpleNamespace(
                    transaction_type="invoice_payment",
                    user=SimpleNamespace(id=50),
                    invoice_payload=payload,
                ),
                receiver=None,
            )

            class FakeBot:
                async def get_star_transactions(self, **kwargs):
                    return SimpleNamespace(transactions=[transaction])

            async def notify(_text):
                return None

            manager = PaymentManager(SimpleNamespace(), db=db)
            reconciler = StarsReconciler(
                FakeBot(),
                db,
                manager,
                SimpleNamespace(
                    sync_user_access=AsyncMock(
                        return_value={
                            "total": 0,
                            "updated": 0,
                            "missing": 0,
                            "failed": 0,
                        }
                    )
                ),
                notify,
                3600,
            )
            result = await reconciler.run_once()
            self.assertEqual(result.applied, 1)
            payment = db.get_payment_by_id(payment_id)
            self.assertEqual(payment["status"], "succeeded")
            self.assertEqual(payment["telegram_payment_charge_id"], "tg-charge-50")
            self.assertEqual(db.get_pending_provisioning_tasks(), [])
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass
