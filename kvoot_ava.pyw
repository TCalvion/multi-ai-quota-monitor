#!/usr/bin/env python3
"""Töölaua ikooni käivitaja (ilma konsooliaknata): küsib kõik 3 allikat paralleelselt ja avab kvoot.html."""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import antigravity_probe  # noqa: E402
import kvoot  # noqa: E402
import kvoot_config  # noqa: E402
from kvoot_i18n import t  # noqa: E402


def _show_error(message: str) -> None:
    """Näitab Windowsis lühikest turvalist veateadet (ilma tokenita ja ekraanitekstita)."""
    clean = antigravity_probe.mask_token(message)[:240]
    title = kvoot_config.resolve_title_override() or t("app_title")
    if os.name == "nt" and hasattr(ctypes, "windll"):
        try:
            ctypes.windll.user32.MessageBoxW(0, clean, title, 0x10)
            return
        except Exception:
            pass
    if sys.stderr is not None:
        print(t("cli_err_prefix", msg=clean), file=sys.stderr)


def main() -> int:
    try:
        code = kvoot.main(["--all", "--open"])
        if code != 0:
            _show_error(t("ava_nonzero_exit", code=code))
        return code
    except Exception as exc:
        _show_error(t("ava_exception", exc_type=type(exc).__name__, exc=exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
