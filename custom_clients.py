import logging
import json
import os
import uuid
from typing import Any, Dict, List, Optional, Set


logger = logging.getLogger(__name__)


class CustomClientsManager:
    """Manager for client peer bindings stored in clients.json."""

    def __init__(self, clients_json_path: str):
        self.clients_json_path = clients_json_path

    def get_peers_for_user(self, user_id: int) -> List[str]:
        return self._get_peers_from_clients_json(user_id)

    def get_job_id_for_peer(self, user_id: int, peer_id: str) -> Optional[str]:
        if not os.path.exists(self.clients_json_path):
            return None

        try:
            with open(self.clients_json_path, "r", encoding="utf-8") as file:
                data = json.load(file)

            if not isinstance(data, dict) or not isinstance(data.get("clients"), list):
                return None

            for client in data["clients"]:
                if not isinstance(client, dict):
                    continue
                if str(client.get("telegramId")) != str(user_id):
                    continue

                for peer in client.get("peers") or []:
                    if not isinstance(peer, dict):
                        continue
                    if str(peer.get("publicKey") or "").strip() == peer_id:
                        job_id = str(peer.get("jobId") or "").strip()
                        return job_id or None
        except Exception as e:
            logger.error(f"Failed to read jobId from clients.json: {e}")

        return None

    def update_job_id_for_peer(self, user_id: int, peer_id: str, job_id: str) -> bool:
        if not os.path.exists(self.clients_json_path):
            return False

        try:
            with open(self.clients_json_path, "r", encoding="utf-8") as file:
                data = json.load(file)

            if not isinstance(data, dict) or not isinstance(data.get("clients"), list):
                return False

            for client in data["clients"]:
                if not isinstance(client, dict):
                    continue
                if str(client.get("telegramId")) != str(user_id):
                    continue

                for peer in client.get("peers") or []:
                    if not isinstance(peer, dict):
                        continue
                    if str(peer.get("publicKey") or "").strip() == peer_id:
                        peer["jobId"] = job_id
                        with open(self.clients_json_path, "w", encoding="utf-8") as file:
                            json.dump(data, file, indent=2, ensure_ascii=False)
                        return True
        except Exception as e:
            logger.error(f"Failed to write jobId to clients.json: {e}")

        return False

    def _get_peers_from_clients_json(self, user_id: int) -> List[str]:
        if not os.path.exists(self.clients_json_path):
            return []

        try:
            with open(self.clients_json_path, "r", encoding="utf-8") as file:
                data = json.load(file)

            if not isinstance(data, dict) or not isinstance(data.get("clients"), list):
                return []

            result: List[str] = []
            seen: Set[str] = set()
            for client in data["clients"]:
                if not isinstance(client, dict):
                    continue
                if str(client.get("telegramId")) != str(user_id):
                    continue

                peers = client.get("peers") or []
                if not isinstance(peers, list):
                    return []

                for peer in peers:
                    if not isinstance(peer, dict):
                        continue
                    public_key = str(peer.get("publicKey") or "").strip()
                    if not public_key or public_key in seen:
                        continue
                    seen.add(public_key)
                    result.append(public_key)
                return result
        except Exception as e:
            logger.error(f"Failed to read peers from clients.json: {e}")

        return []


def build_custom_job_id(user_id: int, peer_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"wgbot-custom-job:{user_id}:{peer_id}"))


def sync_custom_peers_access(
    wg_api: Any,
    custom_clients_manager: CustomClientsManager,
    user_id: int,
    expire_date: str,
    allow_access: bool = True,
    exclude_peer_ids: Optional[Set[str]] = None,
    primary_peer_id: Optional[str] = None,
) -> Dict[str, int]:
    peers = custom_clients_manager.get_peers_for_user(user_id)
    if not peers:
        logger.info(f"No peers configured in clients.json for user_id={user_id}")
        return {"total": 0, "updated": 0, "failed": 0}

    excluded = set(exclude_peer_ids or set())
    if primary_peer_id:
        excluded.add(primary_peer_id)

    total = 0
    updated = 0
    failed = 0

    logger.info(
        f"Starting manual peer sync for user_id={user_id}: peers={peers}, primary_peer_id={primary_peer_id}, allow_access={allow_access}"
    )

    for peer_id in peers:
        if peer_id in excluded:
            logger.info(
                f"Skipping primary/excluded peer for user_id={user_id}: peer={peer_id}"
            )
            continue
        total += 1

        try:
            if allow_access:
                logger.info(
                    f"Calling allow_access_peer for manual peer user_id={user_id}, peer={peer_id}"
                )
                allow_result = wg_api.allow_access_peer(peer_id)
                logger.info(
                    f"allow_access_peer result for user_id={user_id}, peer={peer_id}: {allow_result}"
                )
                if allow_result and isinstance(allow_result, dict) and allow_result.get("status") is False:
                    logger.warning(
                        f"allowAccessPeers returned an error for user_id={user_id}, peer={peer_id}: {allow_result}"
                    )

            job_id = (
                custom_clients_manager.get_job_id_for_peer(user_id, peer_id)
                or build_custom_job_id(user_id, peer_id)
            )
            logger.info(
                f"Ensuring schedule job for manual peer user_id={user_id}, peer={peer_id}, job_id={job_id}, expire_date={expire_date}"
            )
            update_result, resolved_job_id, resolved_expire_date, created_new_job = (
                wg_api.ensure_restrict_job(peer_id, expire_date, job_id=job_id)
            )
            logger.info(
                f"ensure_restrict_job result for user_id={user_id}, peer={peer_id}, requested_job_id={job_id}, resolved_job_id={resolved_job_id}, created_new_job={created_new_job}: {update_result}"
            )
            if update_result and isinstance(update_result, dict) and update_result.get("status") is False:
                raise Exception(f"ensure_restrict_job error: {update_result}")

            if resolved_job_id != job_id:
                persisted = custom_clients_manager.update_job_id_for_peer(
                    user_id,
                    peer_id,
                    resolved_job_id,
                )
                logger.info(
                    f"Manual peer jobId persistence for user_id={user_id}, peer={peer_id}, resolved_job_id={resolved_job_id}, persisted={persisted}"
                )

            updated += 1
            logger.info(
                f"Manual peer sync succeeded for user_id={user_id}, peer={peer_id}, job_id={resolved_job_id}"
            )
        except Exception as e:
            failed += 1
            logger.error(
                f"Failed to sync custom peer user_id={user_id}, peer={peer_id}: {e}"
            )

    logger.info(
        f"Completed manual peer sync for user_id={user_id}: total={total}, updated={updated}, failed={failed}"
    )
    return {"total": total, "updated": updated, "failed": failed}
