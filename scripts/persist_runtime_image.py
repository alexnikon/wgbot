#!/usr/bin/env python3
"""Atomically persist the last healthy container image in a runtime env file."""

import argparse
import os
import re
import stat
import tempfile
from pathlib import Path

IMAGE_LINE_RE = re.compile(r"^\s*WGBOT_IMAGE\s*=")


def validate_image(image: str) -> str:
    """Validate a Docker image reference before storing it."""
    normalized = image.strip()
    if (
        not normalized
        or len(normalized) > 512
        or any(character.isspace() or ord(character) < 32 for character in normalized)
    ):
        raise ValueError("Image reference must be non-empty and contain no whitespace")
    return normalized


def persist_runtime_image(env_path: Path, image: str) -> None:
    """Replace WGBOT_IMAGE while preserving unrelated env content and file mode."""
    image = validate_image(image)
    original = env_path.read_text(encoding="utf-8")
    mode = stat.S_IMODE(env_path.stat().st_mode)
    lines = original.splitlines()
    updated: list[str] = []
    replaced = False
    for line in lines:
        if IMAGE_LINE_RE.match(line) and not line.lstrip().startswith("#"):
            if not replaced:
                updated.append(f"WGBOT_IMAGE={image}")
                replaced = True
            continue
        updated.append(line)
    if not replaced:
        updated.append(f"WGBOT_IMAGE={image}")
    content = "\n".join(updated) + "\n"

    descriptor, temporary_name = tempfile.mkstemp(
        dir=env_path.parent, prefix=f".{env_path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        temporary.replace(env_path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    persist_runtime_image(args.env_file.resolve(), args.image)


if __name__ == "__main__":
    main()
