import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable

from cascade_api import CascadeRouter
from database import Database
from runtime_metrics import RuntimeMetrics

logger = logging.getLogger(__name__)

ConfigSender = Callable[[int, bytes], Awaitable[bool]]
AdminNotifier = Callable[[str], Awaitable[None]]


class ProvisioningWorker:
    """Execute durable Cascade provisioning tasks under database leases."""

    def __init__(
        self,
        db: Database,
        cascade_router: CascadeRouter,
        send_config: ConfigSender,
        notify_admins: AdminNotifier,
        interval_seconds: int,
        lease_seconds: int,
        metrics: RuntimeMetrics | None = None,
    ) -> None:
        self.db = db
        self.cascade_router = cascade_router
        self.send_config = send_config
        self.notify_admins = notify_admins
        self.interval_seconds = interval_seconds
        self.lease_seconds = lease_seconds
        self.metrics = metrics
        self.worker_id = f"bot-{uuid.uuid4()}"

    async def run(self) -> None:
        """Continuously claim and process due tasks until cancelled."""
        while True:
            tasks = await asyncio.to_thread(
                self.db.claim_provisioning_tasks,
                self.worker_id,
                self.lease_seconds,
            )
            if self.metrics:
                self.metrics.provisioning_claimed(len(tasks))
            for task in tasks:
                await self._process(task)
            await asyncio.sleep(self.interval_seconds)

    async def _process(self, task: dict) -> None:
        config_sent = True
        heartbeat = asyncio.create_task(
            self._renew_lease(task["id"]), name=f"lease-{task['id']}"
        )
        try:
            payload = task["payload"]
            user_id = int(task["telegram_user_id"])
            if task["operation"] == "create_peer":
                primary = await asyncio.to_thread(
                    self.db.get_primary_client_peer, user_id
                )
                if primary:
                    config = await self.cascade_router.get_primary_config(user_id)
                else:
                    _, config = await self.cascade_router.create_user_peer(
                        user_id,
                        payload.get("username"),
                        payload["peer_name"],
                        payload["expire_date"],
                    )
                config_sent = await self.send_config(user_id, config)
            elif task["operation"] == "sync_access":
                result = await self.cascade_router.sync_user_access(
                    user_id, payload["expire_date"]
                )
                if result["failed"]:
                    raise RuntimeError(f"Failed peers: {result['failed']}")
            else:
                raise RuntimeError(f"Unknown provisioning operation: {task['operation']}")

            completed = await asyncio.to_thread(
                self.db.complete_provisioning_task, task["id"], self.worker_id
            )
            if not completed:
                raise RuntimeError("Provisioning task lease ownership was lost")
            if self.metrics:
                self.metrics.provisioning_completed()
            delivery_note = (
                ""
                if task["operation"] != "create_peer" or config_sent
                else "\nКонфиг не доставлен; пользователь может запросить его повторно."
            )
            await self.notify_admins(
                "✅ Отложенная операция выполнена\n\n"
                f"Telegram ID: {user_id}\n"
                f"Операция: {task['operation']}{delivery_note}"
            )
        except Exception as exc:
            await asyncio.to_thread(
                self.db.fail_provisioning_task,
                task["id"],
                str(exc),
                self.worker_id,
            )
            logger.error(
                "Provisioning task %s failed (%s)", task["id"], type(exc).__name__
            )
            if self.metrics:
                self.metrics.provisioning_failed(task["id"], exc)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _renew_lease(self, task_id: str) -> None:
        interval = max(10, self.lease_seconds // 3)
        while True:
            await asyncio.sleep(interval)
            renewed = await asyncio.to_thread(
                self.db.renew_provisioning_lease,
                task_id,
                self.worker_id,
                self.lease_seconds,
            )
            if not renewed:
                return
