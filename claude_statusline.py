#!/usr/bin/env python3
"""Claude Code statusLine skript: salvestab ainult rate_limits väljad faili kvoot_claude.json."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from kvoot_config import resolve_home_dir, write_atomic_json  # noqa: E402
from kvoot_i18n import t  # noqa: E402


def default_claude_quota_path() -> Path:
    """Vaikimisi tee kvoot_claude.json failile (ühine Windowsi ja WSL-i vahel)."""
    return resolve_home_dir() / ".claude" / "kvoot_claude.json"


def _clean_window(window: Any) -> dict[str, Any] | None:
    """Eraldab ainult used_percentage ja resets_at väljad."""
    if not isinstance(window, dict):
        return None
    used = window.get("used_percentage")
    resets_at = window.get("resets_at")
    if used is None or resets_at is None:
        return None
    try:
        used_val = round(float(used), 2)
        resets_val = float(resets_at)
    except (TypeError, ValueError):
        return None
    return {
        "used_percentage": used_val,
        "resets_at": resets_val,
    }


def extract_rate_limits(payload: dict[str, Any], now_utc: datetime | None = None) -> dict[str, Any] | None:
    """Tagastab ainult rate_limits väljad + kirjutamise aja, või None kui rate_limits puudub."""
    rl = payload.get("rate_limits")
    if not isinstance(rl, dict):
        return None

    five_hour = _clean_window(rl.get("five_hour"))
    seven_day = _clean_window(rl.get("seven_day"))
    if five_hour is None and seven_day is None:
        return None

    ts = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
    return {
        "written_at": ts,
        "five_hour": five_hour,
        "seven_day": seven_day,
    }


def format_status_line(payload: dict[str, Any]) -> str:
    """Vormindab lühikese staatusrea Claude Code'i jaoks aktiivses keeles."""
    rl = payload.get("rate_limits") if isinstance(payload, dict) else None
    five = _clean_window(rl.get("five_hour")) if isinstance(rl, dict) else None
    seven = _clean_window(rl.get("seven_day")) if isinstance(rl, dict) else None

    lbl_5h = t("statusline_5h")
    lbl_7d = t("statusline_7d")
    lbl_ctx = t("statusline_ctx")

    parts: list[str] = []
    parts.append(f"{lbl_5h} {five['used_percentage']:.0f}%" if five else f"{lbl_5h} -")
    parts.append(f"{lbl_7d} {seven['used_percentage']:.0f}%" if seven else f"{lbl_7d} -")

    ctx = payload.get("context_window") if isinstance(payload, dict) else None
    ctx_pct = ctx.get("used_percentage") if isinstance(ctx, dict) else None
    if ctx_pct is not None:
        try:
            parts.append(f"{lbl_ctx} {float(ctx_pct):.0f}%")
        except (TypeError, ValueError):
            pass

    return " · ".join(parts)


def process_stdin_text(
    raw_text: str,
    out_path: Path | None = None,
    now_utc: datetime | None = None,
) -> str:
    """Töötleb statusLine JSON-sisendi, salvestab vajadusel kvoot_claude.json ja tagastab staatusrea."""
    try:
        payload = json.loads(raw_text) if raw_text.strip() else {}
    except json.JSONDecodeError:
        payload = {}

    if not isinstance(payload, dict):
        payload = {}

    extracted = extract_rate_limits(payload, now_utc=now_utc)
    if extracted is not None:
        target = out_path or default_claude_quota_path()
        write_atomic_json(target, extracted, prefix=".kvoot_claude_")

    return format_status_line(payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=t("statusline_cli_desc"))
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=t("statusline_cli_out"),
    )
    args = parser.parse_args(argv)

    raw_text = sys.stdin.read()
    line = process_stdin_text(raw_text, out_path=args.out)
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
