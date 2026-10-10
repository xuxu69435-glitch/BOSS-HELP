"""Seed empty local templates without overwriting existing configuration."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def initialize(root: Path) -> None:
    root = root.resolve()
    for template, relative in (
        ("preferences.example.json", "preferences/settings.json"),
        ("profile.example.json", "resumes/user.json"),
    ):
        source = root / "config" / template
        target = root / "data" / "boss-agent" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        content = source.read_bytes()
        if template == "profile.example.json":
            profile = json.loads(content)
            now = datetime.now(timezone.utc).isoformat()
            profile["created_at"] = profile["updated_at"] = now
            content = (json.dumps(profile, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        try:
            with target.open("xb") as handle:
                handle.write(content)
        except FileExistsError:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Initialize empty local configuration")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    initialize(parser.parse_args().root)
