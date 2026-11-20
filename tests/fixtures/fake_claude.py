#!/usr/bin/env python3
"""Võlts-claude skript PTY-raja ühiktestide jaoks (päris Claude'i ega saladusi ei kasuta)."""

from __future__ import annotations

import os
import select
import signal
import sys
import time
from pathlib import Path


def _append_log(path_env: str, data: bytes) -> None:
    p = os.environ.get(path_env)
    if not p:
        return
    with Path(p).open("ab") as f:
        f.write(data)


def main() -> int:
    mode = os.environ.get("FAKE_CLAUDE_MODE", "trust_and_usage")
    env_log = os.environ.get("FAKE_CLAUDE_ENV_LOG")
    if env_log:
        has_oauth = "CLAUDE_CODE_OAUTH_TOKEN" in os.environ
        Path(env_log).write_text(f"oauth_env={has_oauth}\n", encoding="utf-8")

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    if mode == "timeout_other_dialog":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        import tty

        tty.setraw(sys.stdin.fileno())
        sys.stdout.write(
            "\x1b[2J\x1b[HDo you want to use this API key?\r\n"
            "\u276f 1. No (recommended)\r\n"
            "  2. Yes\r\n"
        )
        sys.stdout.flush()
        while True:
            r, _, _ = select.select([sys.stdin], [], [], 0.05)
            if r:
                chunk = os.read(sys.stdin.fileno(), 1024)
                if not chunk:
                    break
                _append_log("FAKE_CLAUDE_STDIN_LOG", chunk)
            time.sleep(0.05)
        return 0

    # trust_and_usage
    sys.stdout.write(
        "\x1b[2J\x1b[HQuick safety check: Is this a project you created or one you trust?\r\n"
        "\u276f No, exit\r\n"
        "  Yes, I trust this folder\r\n"
        "Enter to confirm \u00b7 Esc to cancel\r\n"
    )
    sys.stdout.flush()

    received = b""
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        r, _, _ = select.select([sys.stdin], [], [], 0.05)
        if r:
            chunk = os.read(sys.stdin.fileno(), 1024)
            if not chunk:
                break
            received += chunk
            _append_log("FAKE_CLAUDE_STDIN_LOG", chunk)
            if b"\r" in received or b"\n" in received:
                break

    if b"\x1b[B" not in received:
        return 1

    sys.stdout.write(
        "\x1b[2J\x1b[HSettings: Usage \u00b7 Claude Max \u00b7 salanimi@example.com \u00b7 Salajane Org O\u00dc\r\n"
        "\x1b[3;1HCurrent session\r\n"
        "\x1b[4;1H\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2588\x1b[52G28% used\r\n"
        "\x1b[5;1HResets 8:50am (Europe/Tallinn)\r\n"
        "\x1b[7;1HCurrent week (all models)\r\n"
        "\x1b[8;1H\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2588\x1b[52G29% used\r\n"
        "\x1b[9;1HResets Nov 18, 7pm (Europe/Tallinn)\r\n"
    )
    sys.stdout.flush()

    end_wait = time.monotonic() + 5.0
    while time.monotonic() < end_wait:
        r, _, _ = select.select([sys.stdin], [], [], 0.05)
        if r:
            chunk = os.read(sys.stdin.fileno(), 1024)
            if not chunk or b"/exit" in chunk:
                break
        time.sleep(0.05)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
