"""Small local CLI entrypoint; it never calls an external model."""

from __future__ import annotations

import argparse
import json

from jarvis.memory import SQLiteMemoryStore
from jarvis.settings import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis")
    parser.add_argument(
        "command", nargs="?", default="status", choices=("status",)
    )
    args = parser.parse_args(argv)
    if args.command == "status":
        settings = Settings()
        memory = SQLiteMemoryStore(settings.memory_database)
        print(
            json.dumps(
                {
                    "mode": "local",
                    "schema_version": memory.schema_version(),
                    "permission_mode": settings.permission_mode.value,
                    "monthly_target_usd": settings.monthly_target_usd,
                    "monthly_hard_limit_usd": settings.monthly_hard_limit_usd,
                    "paid_api_enabled": settings.paid_api_enabled,
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
