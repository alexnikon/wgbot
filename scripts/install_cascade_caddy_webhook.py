import argparse
import os
import shutil
import stat
from pathlib import Path

START_MARKER = "# BEGIN WGBOT YOOKASSA WEBHOOK"
END_MARKER = "# END WGBOT YOOKASSA WEBHOOK"
INSERT_BEFORE = "    # ── Decoy site"
MANAGED_BLOCK = f"""    {START_MARKER}
    handle /webhook/yookassa* {{
        reverse_proxy 127.0.0.1:8001
    }}
    {END_MARKER}

"""
WEBHOOK_PATH = "/webhook/yookassa"
WEBHOOK_UPSTREAM = "reverse_proxy 127.0.0.1:8001"


class CaddyPatchError(RuntimeError):
    """Raised when the Cascade Caddyfile cannot be patched safely."""


def patch_caddyfile(content: str) -> tuple[str, bool]:
    if START_MARKER in content and END_MARKER in content:
        return content, False
    if START_MARKER in content or END_MARKER in content:
        raise CaddyPatchError("Caddyfile contains an incomplete managed webhook block")
    webhook_index = content.find(WEBHOOK_PATH)
    upstream_index = content.find(WEBHOOK_UPSTREAM, webhook_index)
    if webhook_index >= 0 and 0 <= upstream_index - webhook_index <= 800:
        return content, False
    index = content.find(INSERT_BEFORE)
    if index < 0:
        raise CaddyPatchError(
            "Cascade decoy-site marker was not found; upstream Caddyfile changed"
        )
    return content[:index] + MANAGED_BLOCK + content[index:], True


def atomic_write(path: Path, content: str) -> None:
    original_mode = stat.S_IMODE(path.stat().st_mode)
    temporary = path.with_name(f".{path.name}.wgbot.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        original_mode,
    )
    try:
        os.fchmod(descriptor, original_mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install or verify the wgbot YooKassa route in Cascade Caddy"
    )
    parser.add_argument("caddyfile", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    if not args.caddyfile.is_file():
        raise CaddyPatchError(f"Caddyfile not found: {args.caddyfile}")
    original = args.caddyfile.read_text(encoding="utf-8")
    patched, changed = patch_caddyfile(original)
    if args.check:
        if changed:
            raise CaddyPatchError("Managed YooKassa webhook route is not installed")
        print("Cascade Caddy webhook route is installed")
        return 0
    if not changed:
        print("Cascade Caddy webhook route is already installed")
        return 0

    backup = args.caddyfile.with_suffix(args.caddyfile.suffix + ".pre-wgbot")
    if not backup.exists():
        shutil.copy2(args.caddyfile, backup)
    atomic_write(args.caddyfile, patched)
    print(f"Installed webhook route; backup={backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
