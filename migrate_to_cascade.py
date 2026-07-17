import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from cascade_api import CascadeRouter
from database import Database


logger = logging.getLogger(__name__)


def load_client_registry(path: Path | None) -> list[dict[str, Any]]:
    """Read both the unified and legacy clients.json formats."""
    if path is None or not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid clients.json at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    if isinstance(data, dict) and isinstance(data.get("clients"), list):
        result: list[dict[str, Any]] = []
        for client in data["clients"]:
            try:
                user_id = int(client.get("telegramId"))
            except (TypeError, ValueError):
                continue
            for peer in client.get("peers") or []:
                public_key = str(peer.get("publicKey") or "").strip()
                if not public_key:
                    continue
                result.append(
                    {
                        "telegram_user_id": user_id,
                        "telegram_username": str(client.get("username") or "").strip(),
                        "promo": int(client.get("promo") or 0),
                        "public_key": public_key,
                        "peer_name": str(peer.get("clientId") or "").strip(),
                        "role": "primary" if peer.get("role") == "bot" else "manual",
                    }
                )
        return result
    if isinstance(data, list):
        return [
            {
                "public_key": str(item.get("publicKey") or "").strip(),
                "peer_name": str(item.get("clientId") or "").strip(),
                "role": "manual",
            }
            for item in data
            if isinstance(item, dict) and item.get("publicKey")
        ]
    raise ValueError("Unsupported clients.json format")


async def migrate(args: argparse.Namespace) -> int:
    db = Database(args.database)
    router = CascadeRouter(db)
    server = router.get_server(args.server_key)
    interface_id = args.interface_id or server.interface_id
    cascade_peers = await router.get_api(server.server_key).list_peers(interface_id)
    cascade_by_key: dict[str, dict[str, Any]] = {}
    duplicate_cascade_keys: set[str] = set()
    for peer in cascade_peers:
        public_key = str(peer.get("publicKey") or "").strip()
        if not public_key:
            continue
        if public_key in cascade_by_key:
            duplicate_cascade_keys.add(public_key)
            continue
        cascade_by_key[public_key] = peer

    candidates = {
        item["legacy_public_key"]: {
            "telegram_user_id": item["telegram_user_id"],
            "telegram_username": item.get("telegram_username") or "",
            "promo": 0,
            "public_key": item["legacy_public_key"],
            "peer_name": item.get("legacy_peer_name") or "",
            "role": "primary",
        }
        for item in db.get_legacy_migration_candidates()
    }
    unresolved_registry: list[dict[str, Any]] = []
    conflicting_keys: set[str] = set(duplicate_cascade_keys)
    for item in load_client_registry(args.clients_json):
        public_key = item["public_key"]
        if item.get("telegram_user_id"):
            existing = candidates.get(public_key)
            if existing and existing["telegram_user_id"] != item["telegram_user_id"]:
                conflicting_keys.add(public_key)
                print(
                    f"CONFLICT public_key={public_key} "
                    f"telegram_ids={existing['telegram_user_id']},{item['telegram_user_id']}"
                )
                continue
            candidates[public_key] = item
        elif public_key not in candidates:
            unresolved_registry.append(item)

    matched = 0
    missing = 0
    for public_key, candidate in candidates.items():
        if public_key in conflicting_keys:
            continue
        cascade_peer = cascade_by_key.get(public_key)
        if not cascade_peer:
            missing += 1
            print(
                f"MISSING telegram_id={candidate['telegram_user_id']} "
                f"public_key={public_key} name={candidate['peer_name']}"
            )
            continue
        matched += 1
        print(
            f"MATCH telegram_id={candidate['telegram_user_id']} "
            f"cascade_peer_id={cascade_peer['id']} public_key={public_key}"
        )
        if args.apply:
            db.upsert_client(
                candidate["telegram_user_id"], candidate.get("telegram_username")
            )
            db.set_client_promo(candidate["telegram_user_id"], candidate.get("promo", 0))
            saved = db.save_client_peer(
                candidate["telegram_user_id"],
                server.server_key,
                interface_id,
                str(cascade_peer["id"]),
                public_key,
                str(cascade_peer.get("name") or candidate["peer_name"]),
                candidate.get("role", "manual"),
                bool(cascade_peer.get("enabled", True)),
            )
            if not saved:
                conflicting_keys.add(public_key)
                print(
                    f"CONFLICT telegram_id={candidate['telegram_user_id']} "
                    f"public_key={public_key} reason=database constraint"
                )

    for item in unresolved_registry:
        print(
            f"UNRESOLVED clients.json entry public_key={item['public_key']} "
            f"name={item['peer_name']} reason=no telegramId or legacy DB match"
        )

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(
        f"{mode} complete: matched={matched} missing={missing} "
        f"conflicts={len(conflicting_keys)} unresolved={len(unresolved_registry)} "
        f"cascade_total={len(cascade_peers)}"
    )
    await router.close()
    return 0 if not (missing or conflicting_keys or unresolved_registry) else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bind existing bot users to manually imported Cascade peers"
    )
    parser.add_argument("--server-key", required=True)
    parser.add_argument("--interface-id")
    parser.add_argument("--database", default="data/wgbot.db")
    parser.add_argument("--clients-json", type=Path)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist matches. Without this flag the command is a dry-run.",
    )
    return parser


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(asyncio.run(migrate(build_parser().parse_args())))
