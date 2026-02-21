import logging
import os
import re
import uuid
from typing import Any, Dict, List, Optional, Set


logger = logging.getLogger(__name__)


class CustomClientsManager:
    """
    Менеджер ручных привязок Telegram ID -> peer public keys из WGDashboard.

    Форматы строк в custom_clients.txt:
    - 123456789=peer_key_1,peer_key_2
    - 123456789:peer_key_1 peer_key_2
    Комментарии и пустые строки игнорируются.
    """

    def __init__(self, file_path: str):
        self.file_path = file_path

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
            logger.error(f"Ошибка чтения custom_clients файла {self.file_path}: {e}")
            return []

        return result


def build_custom_job_id(user_id: int, peer_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"wgbot-custom-job:{user_id}:{peer_id}"))


def sync_custom_peers_access(
    wg_api: Any,
    custom_clients_manager: CustomClientsManager,
    user_id: int,
    expire_date: str,
    allow_access: bool = True,
    exclude_peer_ids: Optional[Set[str]] = None,
) -> Dict[str, int]:
    peers = custom_clients_manager.get_peers_for_user(user_id)
    if not peers:
        return {"total": 0, "updated": 0, "failed": 0}

    excluded = exclude_peer_ids or set()
    total = 0
    updated = 0
    failed = 0

    for peer_id in peers:
        if peer_id in excluded:
            continue
        total += 1

        try:
            if allow_access:
                allow_result = wg_api.allow_access_peer(peer_id)
                if allow_result and isinstance(allow_result, dict) and allow_result.get("status") is False:
                    logger.warning(
                        f"allowAccessPeers вернул ошибку для user_id={user_id}, peer={peer_id}: {allow_result}"
                    )

            job_id = build_custom_job_id(user_id, peer_id)
            update_result = wg_api.update_job_expire_date(job_id, peer_id, expire_date)
            if update_result and isinstance(update_result, dict) and update_result.get("status") is False:
                raise Exception(f"savePeerScheduleJob error: {update_result}")

            updated += 1
        except Exception as e:
            failed += 1
            logger.error(
                f"Не удалось синхронизировать custom peer user_id={user_id}, peer={peer_id}: {e}"
            )

    return {"total": total, "updated": updated, "failed": failed}
