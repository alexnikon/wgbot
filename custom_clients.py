import logging
import json
import os
import re
import uuid
from typing import Any, Dict, List, Optional, Set


logger = logging.getLogger(__name__)


class CustomClientsManager:
    """
    Manager for manual bindings: Telegram ID -> peer public keys from WGDashboard.

    Line formats in custom_clients.txt:
    - 123456789=peer_key_1,peer_key_2
    - 123456789:peer_key_1 peer_key_2
    Comments and empty lines are ignored.
    """

    def __init__(self, file_path: str, clients_json_path: str | None = None):
        self.file_path = file_path
        self.clients_json_path = clients_json_path

    def _parse_line(self, raw_line: str) -> Optional[tuple[str, List[str]]]:
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            return None

        if "=" in line:
            user_id, peers_raw = line.split("=", 1)
        elif ":" in line:
            user_id, peers_raw = line.split(":", 1)
        else:
            return None

        user_id = user_id.strip()
        if not user_id.isdigit():
            return None

        peers = [p.strip() for p in re.split(r"[,\s;]+", peers_raw.strip()) if p.strip()]
        if not peers:
            return None

        deduped: List[str] = []
        seen: Set[str] = set()
        for peer in peers:
            if peer in seen:
                continue
            seen.add(peer)
            deduped.append(peer)

        return user_id, deduped

    def get_peers_for_user(self, user_id: int) -> List[str]:
        unified_peers = self._get_peers_from_clients_json(user_id)
        if unified_peers:
            return unified_peers

        if not os.path.exists(self.file_path):
            return []

        result: List[str] = []
        seen: Set[str] = set()
        uid = str(user_id)

        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                for line in f:
                    parsed = self._parse_line(line)
                    if not parsed:
                        continue
                    parsed_uid, peers = parsed
                    if parsed_uid != uid:
                        continue
                    for peer in peers:
                        if peer in seen:
                            continue
                        seen.add(peer)
                        result.append(peer)
        except Exception as e:
            logger.error(f"Failed to read custom_clients file {self.file_path}: {e}")
            return []

        return result

    def _get_peers_from_clients_json(self, user_id: int) -> List[str]:
        if not self.clients_json_path or not os.path.exists(self.clients_json_path):
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
            logger.error(f"Failed to read custom peers from clients.json: {e}")

        return []


def build_custom_job_id(user_id: int, peer_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"wgbot-custom-job:{user_id}:{peer_id}"))


def sync_custom_peers_access(
    wg_api: Any,
    custom_clients_manager: CustomClientsManager,
    db: Any,
    user_id: int,
    expire_date: str,
    allow_access: bool = True,
    exclude_peer_ids: Optional[Set[str]] = None,
    primary_peer_id: Optional[str] = None,
    primary_job_id: Optional[str] = None,
) -> Dict[str, int]:
    peers = custom_clients_manager.get_peers_for_user(user_id)
    if primary_peer_id and primary_peer_id not in peers:
        logger.warning(
            f"Primary peer {primary_peer_id} is not listed in custom_clients for user_id={user_id}; adding it to sync set"
        )
        peers.append(primary_peer_id)

    if not peers:
        logger.info(f"No custom peers configured for user_id={user_id}")
        return {"total": 0, "updated": 0, "failed": 0}

    excluded = exclude_peer_ids or set()
    total = 0
    updated = 0
    failed = 0

    logger.info(
        f"Starting custom peer sync for user_id={user_id}: peers={peers}, primary_peer_id={primary_peer_id}, primary_job_id={primary_job_id}, allow_access={allow_access}"
    )

    for peer_id in peers:
        if peer_id in excluded:
            logger.info(
                f"Skipping excluded custom peer for user_id={user_id}: peer={peer_id}"
            )
            continue
        total += 1

        try:
            is_primary_peer = primary_peer_id == peer_id
            if allow_access:
                logger.info(
                    f"Calling allow_access_peer for user_id={user_id}, peer={peer_id}, is_primary={is_primary_peer}"
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
                primary_job_id
                if primary_peer_id == peer_id and primary_job_id
                else build_custom_job_id(user_id, peer_id)
            )
            logger.info(
                f"Ensuring schedule job for user_id={user_id}, peer={peer_id}, is_primary={is_primary_peer}, job_id={job_id}, expire_date={expire_date}"
            )
            update_result, resolved_job_id, resolved_expire_date, created_new_job = (
                wg_api.ensure_restrict_job(peer_id, expire_date, job_id=job_id)
            )
            logger.info(
                f"ensure_restrict_job result for user_id={user_id}, peer={peer_id}, requested_job_id={job_id}, resolved_job_id={resolved_job_id}, created_new_job={created_new_job}: {update_result}"
            )
            if update_result and isinstance(update_result, dict) and update_result.get("status") is False:
                raise Exception(f"ensure_restrict_job error: {update_result}")

            if is_primary_peer and db and resolved_job_id != primary_job_id:
                primary_peer = db.get_peer_by_telegram_id(user_id)
                if primary_peer and primary_peer.get("peer_name") and primary_peer.get("peer_id") == peer_id:
                    persisted = db.update_peer_info(
                        primary_peer["peer_name"],
                        peer_id,
                        resolved_job_id,
                        resolved_expire_date,
                    )
                    logger.info(
                        f"Primary peer job persistence for user_id={user_id}, peer={peer_id}, resolved_job_id={resolved_job_id}, persisted={persisted}"
                    )
                else:
                    logger.warning(
                        f"Primary peer DB record not found or mismatched for user_id={user_id}, peer={peer_id}; resolved_job_id={resolved_job_id} was not persisted"
                    )

            updated += 1
            logger.info(
                f"Custom peer sync succeeded for user_id={user_id}, peer={peer_id}, job_id={resolved_job_id}"
            )
        except Exception as e:
            failed += 1
            logger.error(
                f"Failed to sync custom peer user_id={user_id}, peer={peer_id}: {e}"
            )

    logger.info(
        f"Completed custom peer sync for user_id={user_id}: total={total}, updated={updated}, failed={failed}"
    )
    return {"total": total, "updated": updated, "failed": failed}
