import argparse
import asyncio
from datetime import UTC, datetime, timedelta

from cascade_api import CascadeAPI, load_cascade_servers


async def check_server(server, exercise_peer: bool) -> None:
    api = CascadeAPI(server)
    peer_id: str | None = None
    try:
        health = await api.health()
        interface = await api.get_interface()
        peers = await api.list_peers()
        print(
            f"OK server={server.server_key} status={health.get('status')} "
            f"interface={interface.get('id')} peers={len(peers)}/{server.max_peers}"
        )
        if not exercise_peer:
            return

        expiry = datetime.now(UTC) + timedelta(hours=1)
        peer = await api.create_peer(
            f"wgbot-smoke-{int(datetime.now().timestamp())}",
            expiry.isoformat(),
        )
        peer_id = str(peer["id"])
        await api.get_peer(peer_id)
        await api.update_expiry(peer_id, expiry.isoformat())
        await api.disable_peer(peer_id)
        await api.enable_peer(peer_id)
        config = await api.download_config(peer_id)
        if not config:
            raise RuntimeError("Cascade returned an empty peer config")
        print(f"API exercise passed server={server.server_key} peer={peer_id}")
    finally:
        try:
            if peer_id:
                await api.delete_peer(peer_id)
                print(f"Deleted smoke peer server={server.server_key} peer={peer_id}")
        finally:
            await api.close()


async def main(args: argparse.Namespace) -> int:
    servers = load_cascade_servers()
    if args.server_key:
        servers = [server for server in servers if server.server_key == args.server_key]
    if not servers:
        raise RuntimeError("No matching Cascade server was found")
    for server in servers:
        exercise_peer = args.exercise_peer and (
            server.enabled or args.server_key == server.server_key
        )
        await check_server(server, exercise_peer)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate configured Cascade APIs")
    parser.add_argument("--server-key")
    parser.add_argument(
        "--exercise-peer",
        action="store_true",
        help="Create and delete a temporary peer after exercising all peer operations.",
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(build_parser().parse_args())))
