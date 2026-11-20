#!/usr/bin/env python3
"""Claude Code `/usage` PTY-mõõdik (v4).

Käivitatakse tühjast proovikaustast (~/.kvoot-probe), ei muuda ~/.claude.json
ega settings.json faile, ei loe ~/.claude/.credentials.json faili ning ei salvesta
ekraaniteksti, e-posti ega organisatsiooni nime.

Osaliselt porditud ClaudeBarist (Apache-2.0, vt NOTICE ja LICENSES/Apache-2.0.txt):
https://github.com/tddworks/ClaudeBar (`docs/providers/claude`,
`claude-usage-screen.js`: sektsioonipealkirjad, `isRateLimitRow`,
`onlyBreakdownRateLimited` ning "% used"/"% left" parsimine).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if os.name != "nt":
    import fcntl
    import pty
    import select
    import struct
    import termios

try:
    import kvoot_config as _kvoot_config
    import kvoot_i18n as _kvoot_i18n
except ImportError:
    _kvoot_config = None  # type: ignore[assignment]
    _kvoot_i18n = None  # type: ignore[assignment]

_FALLBACK_TEXTS = {
    "claude_reason_folder_trust": "Claude Code prompted for folder trust",
    "claude_reason_auth_required": "Claude Code is not logged in",
    "claude_reason_subscription_required": "Claude /usage is only available for subscription plans",
    "claude_reason_rate_limited": "Claude /usage rate limited",
    "claude_reason_loading_timeout": "Claude /usage loading did not complete",
    "claude_reason_parse_failed": "Could not find Current session or Current week (all models) windows on Claude /usage screen",
    "claude_reason_timeout": "Claude /usage screen did not open within {timeout:g} s",
    "claude_reason_cli_not_found": "Claude Code CLI (claude) is not installed in WSL",
    "claude_reason_cli_exec_failed": "Failed to start Claude CLI: {exc}",
    "claude_reason_pty_unsupported": "POSIX PTY is not supported on Windows",
    "claude_reason_empty_screen": "Claude /usage returned no screen output",
    "claude_reason_wsl_exec_failed": "Failed to launch WSL Claude /usage: {exc}",
    "claude_reason_wsl_exit": "WSL Claude /usage exited with code {code}",
    "claude_reason_wsl_invalid": "WSL Claude /usage returned an invalid response",
    "claude_reason_missing_window": "Missing window: {windows}",
}


def _msg(key: str, **kwargs: Any) -> str:
    if _kvoot_i18n is not None:
        return _kvoot_i18n.t(key, **kwargs)
    tmpl = _FALLBACK_TEXTS.get(key, key)
    return tmpl.format(**kwargs) if kwargs else tmpl


def _fail(error_code: str, reason_key: str, **reason_args: Any) -> dict[str, Any]:
    """Tagastab struktureeritud tõrkeväärtuse koos koodi, võtme ja argumentidega."""
    clean_args: dict[str, Any] = {}
    for k, v in reason_args.items():
        clean_args[k] = v if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)
    return {
        "ok": False,
        "error_code": error_code,
        "reason_key": reason_key,
        "reason_args": clean_args,
        "reason": _msg(reason_key, **clean_args),
    }


def _default_tz() -> tzinfo:
    if _kvoot_config is not None:
        return _kvoot_config.resolve_tz()
    tz_name = os.environ.get("KVOOT_TZ", "").strip()
    if tz_name:
        if tz_name.upper() == "UTC":
            return timezone.utc
        try:
            return ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc

USAGE_TIMEOUT_S = 20.0
SESSION_LABELS = ("Current session", "Current session (all models)")
WEEK_LABELS = ("Current week (all models)",)
SECTION_STARTS = (
    "current session",
    "current week",
    "extra usage",
    "what's contributing to your",
)

_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def render_vt_screen(raw_bytes: bytes, rows: int = 40, cols: int = 120) -> list[str]:
    """Renderdab ANSI/VT100 baitvoo fikseeritud ekraaniridadeks."""
    text = raw_bytes.decode("utf-8", errors="replace")
    grid = [[" "] * cols for _ in range(rows)]
    r, c = 0, 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\x1b":
            if i + 1 < n and text[i + 1] == "[":
                j = i + 2
                while j < n and (text[j].isdigit() or text[j] in ";?"):
                    j += 1
                if j < n:
                    cmd = text[j]
                    param_str = text[i + 2 : j].lstrip("?")
                    parts = [int(p) if p.isdigit() else 0 for p in param_str.split(";")] if param_str else []
                    if cmd in ("H", "f"):
                        row_v = parts[0] if len(parts) >= 1 and parts[0] else 1
                        col_v = parts[1] if len(parts) >= 2 and parts[1] else 1
                        r = max(0, min(rows - 1, row_v - 1))
                        c = max(0, min(cols - 1, col_v - 1))
                    elif cmd == "A":
                        r = max(0, r - (parts[0] if parts and parts[0] else 1))
                    elif cmd == "B":
                        r = min(rows - 1, r + (parts[0] if parts and parts[0] else 1))
                    elif cmd == "C":
                        c = min(cols - 1, c + (parts[0] if parts and parts[0] else 1))
                    elif cmd == "D":
                        c = max(0, c - (parts[0] if parts and parts[0] else 1))
                    elif cmd == "G":
                        col_v = parts[0] if parts and parts[0] else 1
                        c = max(0, min(cols - 1, col_v - 1))
                    elif cmd == "K":
                        mode = parts[0] if parts else 0
                        if mode == 0:
                            for x in range(c, cols):
                                grid[r][x] = " "
                        elif mode == 1:
                            for x in range(0, c + 1):
                                grid[r][x] = " "
                        elif mode == 2:
                            grid[r] = [" "] * cols
                    elif cmd == "J":
                        mode = parts[0] if parts else 0
                        if mode == 2:
                            grid = [[" "] * cols for _ in range(rows)]
                            r, c = 0, 0
                        elif mode == 0:
                            for x in range(c, cols):
                                grid[r][x] = " "
                            for y in range(r + 1, rows):
                                grid[y] = [" "] * cols
                    i = j + 1
                    continue
            elif i + 1 < n and text[i + 1] == "]":
                end = text.find("\x07", i + 2)
                end2 = text.find("\x1b\\", i + 2)
                if end != -1 and (end2 == -1 or end < end2):
                    i = end + 1
                    continue
                if end2 != -1:
                    i = end2 + 2
                    continue
            i += 2
            continue
        elif ch == "\r":
            c = 0
        elif ch == "\n":
            if r < rows - 1:
                r += 1
            else:
                grid.pop(0)
                grid.append([" "] * cols)
            c = 0
        elif ch == "\b":
            c = max(0, c - 1)
        elif ch >= " ":
            if c >= cols:
                c = 0
                if r < rows - 1:
                    r += 1
            grid[r][c] = ch
            c += 1
        i += 1
    return ["".join(row).rstrip() for row in grid]


_PURE_PERCENT_RE = re.compile(
    r"^[\s█▉▊▋▌▍▎▏░▓▒─━│|·]*(\d+(?:\.\d+)?)\s*%\s*(used|left)\b",
    re.IGNORECASE,
)


def _is_section_start(line: str) -> bool:
    row = line.strip().lower()
    return any(row.startswith(prefix) for prefix in SECTION_STARTS)


def _line_matches_label(line: str, label: str) -> bool:
    """Kontrollib, kas rida on täpne sektsioonisilt (ilma lubamatu sulgudes täpsustuseta)."""
    row = line.strip().lower()
    target = label.lower()
    if row == target:
        return True
    if row.startswith(target):
        rest = row[len(target) :]
        if rest[0].isalnum():
            return False
        stripped = rest.lstrip(" :·\t")
        if not stripped or stripped.startswith("("):
            return False
        return bool(_PURE_PERCENT_RE.match(stripped))
    return False


def _is_any_heading_line(line: str) -> bool:
    """Tuvastab suvalise uue sektsiooni/pealkirjalaadse rea, et lugemine ei valguks järgmisse plokki."""
    row = line.strip()
    if not row:
        return False
    low = row.lower()
    if _is_section_start(row) or low == "session":
        return True
    if low.startswith(("resets", "reset ", "loading ")):
        return False
    if _PURE_PERCENT_RE.match(row):
        return False
    if re.search(r"\d+(?:\.\d+)?\s*%\s*(used|left)\b", row, re.IGNORECASE):
        return True
    if row.endswith(":") or "(" in row:
        return True
    if len(row) <= 50 and row[0].isupper() and not row.endswith(".") and "%" not in row:
        return True
    return False


def _is_rate_limit_row(line: str) -> bool:
    low = line.lower()
    return (
        ("rate limited" in low or "rate limit exceeded" in low or "too many requests" in low)
        and "rate limits are" not in low
    )


def _only_breakdown_rate_limited(lines: list[str]) -> bool:
    """Kontrollib, kas rate-limit esineb ainult mudelite jaotuse all (ClaudeBar eeskuju, Apache-2.0)."""
    in_advice = False
    for line in lines:
        row = line.strip().lower()
        is_label = _is_section_start(line) or row == "session"
        is_breakdown = in_advice and not is_label and "breakdown" in row
        if _is_rate_limit_row(line) and not is_breakdown:
            return False
        if is_label:
            in_advice = row.startswith("what's contributing to your")
    return True


def _deduplicate_resets(text: str) -> str:
    low = text.lower()
    positions: list[int] = []
    pos = 0
    while True:
        idx = low.find("resets", pos)
        if idx == -1:
            break
        positions.append(idx)
        pos = idx + 1
    if len(positions) > 1:
        return text[positions[-1] :].strip()
    return text.strip()


def _extract_percent(lines: list[str], labels: tuple[str, ...]) -> float | None:
    """Leiab esimese 'NN% used|left' väärtuse kuni järgmise pealkirjataolise reani."""
    for label in labels:
        for i, line in enumerate(lines):
            if not _line_matches_label(line, label):
                continue
            for j in range(i, min(len(lines), i + 12)):
                if j > i and _is_any_heading_line(lines[j]):
                    break
                if j > i and "no limit" in lines[j].lower():
                    break
                if j == i:
                    # Samal real tohib enne numbrit olla ainult silt ise (+ eraldajad)
                    after = lines[j].strip()[len(label) :].lstrip(" :·\t")
                    m = _PURE_PERCENT_RE.match(after)
                else:
                    m = _PURE_PERCENT_RE.match(lines[j].strip())
                if m:
                    val = float(m.group(1))
                    if val > 100.0:
                        return None
                    if m.group(2).lower() == "left":
                        val = 100.0 - val
                    return round(max(0.0, min(100.0, val)), 2)
    return None


def _extract_reset_text(lines: list[str], labels: tuple[str, ...]) -> str | None:
    for label in labels:
        for i, line in enumerate(lines):
            if not _line_matches_label(line, label):
                continue
            for j in range(i, min(len(lines), i + 14)):
                if j > i and _is_any_heading_line(lines[j]):
                    break
                low = lines[j].lower()
                if "reset" in low or ("in" in low and ("h" in low or "m" in low)):
                    return _deduplicate_resets(lines[j])
    return None


def _resolve_tz(tz_name: str | None, fallback_tz: tzinfo) -> tzinfo:
    if not tz_name:
        return fallback_tz
    try:
        return ZoneInfo(tz_name.strip())
    except ZoneInfoNotFoundError:
        if tz_name.strip().upper() == "UTC":
            return timezone.utc
        return fallback_tz


def _parse_time_12_24(hour_s: str, min_s: str | None, ampm: str | None) -> tuple[int, int] | None:
    hour = int(hour_s)
    minute = int(min_s) if min_s else 0
    if ampm:
        ap = ampm.lower()
        if not (1 <= hour <= 12) or not (0 <= minute <= 59):
            return None
        if ap == "am":
            hour = 0 if hour == 12 else hour
        elif ap == "pm":
            hour = 12 if hour == 12 else hour + 12
    else:
        if not (0 <= hour <= 23) or not (0 <= minute <= 59):
            return None
    return hour, minute


def parse_claude_reset_text(
    text: str | None,
    now_utc: datetime,
    default_tz: tzinfo,
) -> float | None:
    """Teisendab Claude /usage lähtestumisaja teksti (nt 'Resets 8:50am (Europe/Tallinn)') epoch-sekunditeks."""
    if not text or not isinstance(text, str):
        return None
    cleaned = _deduplicate_resets(text)
    cleaned = re.sub(r"^resets?\s*:?\s*", "", cleaned, flags=re.IGNORECASE).strip()
    if not cleaned:
        return None

    tz_match = re.search(r"\(([^)]+)\)\s*$", cleaned)
    target_tz = default_tz
    if tz_match:
        target_tz = _resolve_tz(tz_match.group(1), default_tz)
        cleaned = cleaned[: tz_match.start()].strip()

    # Suhteline aeg: "in 2h 15m", "in 45m", "in 1d 3h"
    rel_m = re.match(r"^in\s+(.+)$", cleaned, re.IGNORECASE)
    if rel_m:
        body = rel_m.group(1)
        days = sum(int(x) for x in re.findall(r"(\d+)\s*d\b", body, re.IGNORECASE))
        hours = sum(int(x) for x in re.findall(r"(\d+)\s*h\b", body, re.IGNORECASE))
        mins = sum(int(x) for x in re.findall(r"(\d+)\s*m\b", body, re.IGNORECASE))
        if days or hours or mins:
            return (now_utc + timedelta(days=days, hours=hours, minutes=mins)).timestamp()

    now_local = now_utc.astimezone(target_tz)

    # Kuu + päev [+ aeg], nt "Nov 18, 7pm" või "Nov 18 at 7:30am" või "Nov 18"
    md_m = re.match(
        r"^([A-Za-z]+)\s+(\d{1,2})(?:\s*(?:,|at)?\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?$",
        cleaned,
        re.IGNORECASE,
    )
    if md_m:
        month = _MONTHS.get(md_m.group(1).lower())
        day = int(md_m.group(2))
        if month is not None and 1 <= day <= 31:
            if md_m.group(3):
                hm = _parse_time_12_24(md_m.group(3), md_m.group(4), md_m.group(5))
                if hm is None:
                    return None
                hour, minute = hm
            else:
                hour, minute = 0, 0
            year = now_local.year
            try:
                cand = datetime(year, month, day, hour, minute, tzinfo=target_tz)
            except ValueError:
                return None
            if (now_local - cand).total_seconds() > 86400:
                try:
                    cand = datetime(year + 1, month, day, hour, minute, tzinfo=target_tz)
                except ValueError:
                    return None
            return cand.astimezone(timezone.utc).timestamp()

    # Ainult kellaaeg: "8:50am", "9pm", "14:30"
    tm_m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", cleaned, re.IGNORECASE)
    if tm_m and (tm_m.group(2) is not None or tm_m.group(3) is not None):
        hm = _parse_time_12_24(tm_m.group(1), tm_m.group(2), tm_m.group(3))
        if hm is not None:
            hour, minute = hm
            cand = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if cand <= now_local:
                cand = cand + timedelta(days=1)
            return cand.astimezone(timezone.utc).timestamp()

    return None


def parse_claude_usage_screen(
    screen_text: str,
    now_utc: datetime,
    tz: tzinfo,
) -> dict[str, Any]:
    """Parsib Claude /usage ekraaniteksti; tagastab ainult aknad või puhastatud vea."""
    lines = [ln.strip() for ln in screen_text.splitlines() if ln.strip()]
    low = screen_text.lower()

    if (
        "do you trust the files in this folder?" in low
        or "is this a project you created or one you trust" in low
        or "yes, i trust this folder" in low
    ) and not any(_line_matches_label(ln, lbl) for ln in lines for lbl in SESSION_LABELS):
        return _fail("folder_trust", "claude_reason_folder_trust")

    if any(
        tok in low
        for tok in (
            "token_expired",
            "token has expired",
            "authentication_error",
            "not logged in",
            "please log in",
            "please run claude login",
        )
    ):
        return _fail("auth_required", "claude_reason_auth_required")

    if "/usage is only available for subscription plans" in low or (
        "api usage billing" in low and "total cost:" in low
    ):
        return _fail("subscription_required", "claude_reason_subscription_required")

    pct_5h = _extract_percent(lines, SESSION_LABELS)
    pct_7d = _extract_percent(lines, WEEK_LABELS)

    if any(_is_rate_limit_row(ln) for ln in lines):
        if not ((pct_5h is not None or pct_7d is not None) and _only_breakdown_rate_limited(lines)):
            return _fail("rate_limited", "claude_reason_rate_limited")

    if pct_5h is None and pct_7d is None:
        if "loading usage data" in low:
            return _fail("timeout", "claude_reason_loading_timeout")
        return _fail("parse_failed", "claude_reason_parse_failed")

    reset_5h_txt = _extract_reset_text(lines, SESSION_LABELS) if pct_5h is not None else None
    reset_7d_txt = (
        _extract_reset_text(lines, WEEK_LABELS)
        if pct_7d is not None
        else None
    )

    five_hour = (
        {
            "used_percentage": pct_5h,
            "resets_at": parse_claude_reset_text(reset_5h_txt, now_utc, tz),
        }
        if pct_5h is not None
        else None
    )
    seven_day = (
        {
            "used_percentage": pct_7d,
            "resets_at": parse_claude_reset_text(reset_7d_txt, now_utc, tz),
        }
        if pct_7d is not None
        else None
    )

    out: dict[str, Any] = {
        "ok": True,
        "five_hour": five_hour,
        "seven_day": seven_day,
    }
    missing: list[str] = []
    missing_labels: list[str] = []
    if five_hour is None:
        missing.append("five_hour")
        missing_labels.append("Current session")
    if seven_day is None:
        missing.append("seven_day")
        missing_labels.append("Current week (all models)")
    if missing:
        out["reason_code"] = "missing_window"
        out["missing_windows"] = missing
        out["reason_key"] = "claude_reason_missing_window"
        out["reason_args"] = {"windows": ", ".join(missing_labels)}
        out["reason"] = _msg("claude_reason_missing_window", windows=", ".join(missing_labels))
    return out


def _find_wsl_claude_bin() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    for cand in (
        Path.home() / ".local" / "bin" / "claude",
        Path("/usr/local/bin/claude"),
    ):
        if cand.exists() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def _run_claude_usage_posix(timeout_s: float = USAGE_TIMEOUT_S) -> dict[str, Any]:
    """Käivitab `claude /usage` pseudoterminalis tühjas proovikaustas (WSL/Linux)."""
    if os.name == "nt":
        return _fail("pty_unsupported", "claude_reason_pty_unsupported")

    claude_bin = _find_wsl_claude_bin()
    if not claude_bin:
        return _fail("cli_not_found", "claude_reason_cli_not_found")

    probe_dir = Path.home() / ".kvoot-probe"
    probe_dir.mkdir(parents=True, exist_ok=True)

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))

    env = os.environ.copy()
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    env["TERM"] = "xterm-256color"
    env["COLUMNS"] = "120"
    env["LINES"] = "40"

    try:
        proc = subprocess.Popen(
            [claude_bin, "/usage", "--allowed-tools", ""],
            cwd=str(probe_dir),
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        os.close(slave)
        os.close(master)
        return _fail("cli_exec_failed", "claude_reason_cli_exec_failed", exc=exc)

    os.close(slave)
    buf = b""
    sent_trust = False
    usage_screen_seen = False
    bars_first_seen: float | None = None

    try:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            r, _, _ = select.select([master], [], [], 0.2)
            if r:
                try:
                    chunk = os.read(master, 8192)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                lines = render_vt_screen(buf)
                screen_text = "\n".join(lines)
                low = screen_text.lower()

                if not sent_trust and "yes, i trust this folder" in low:
                    time.sleep(0.15)
                    before_yes = low.split("yes, i trust this folder", 1)[0]
                    if "no, exit" in before_yes:
                        os.write(master, b"\x1b[B")
                        time.sleep(0.1)
                    os.write(master, b"\r")
                    sent_trust = True
                    continue

                if any(
                    tok in low
                    for tok in (
                        "not logged in",
                        "please log in",
                        "token_expired",
                        "authentication_error",
                        "/usage is only available for subscription plans",
                    )
                ):
                    break

                has_bar = ("% used" in low or "% left" in low) and (
                    any(_line_matches_label(ln, lbl) for ln in lines for lbl in SESSION_LABELS)
                    or any(_line_matches_label(ln, lbl) for ln in lines for lbl in WEEK_LABELS)
                )
                if has_bar:
                    usage_screen_seen = True
                    if bars_first_seen is None:
                        bars_first_seen = time.monotonic()
                    if "resets" in low or (time.monotonic() - bars_first_seen) >= 1.5:
                        # Oota lühike hetk, et teine aken ja lähtestusaeg jõuaksid joonistuda
                        time.sleep(0.6)
                        while select.select([master], [], [], 0.1)[0]:
                            try:
                                extra = os.read(master, 8192)
                                if not extra:
                                    break
                                buf += extra
                            except OSError:
                                break
                        break
            elif proc.poll() is not None:
                break
        else:
            return _fail("timeout", "claude_reason_timeout", timeout=timeout_s)
    finally:
        if usage_screen_seen:
            try:
                os.write(master, b"\x1b")
                time.sleep(0.05)
                os.write(master, b"/exit\r")
            except OSError:
                pass
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        try:
            os.close(master)
        except OSError:
            pass

    screen_text = "\n".join(render_vt_screen(buf))
    if not screen_text.strip():
        return _fail("parse_failed", "claude_reason_empty_screen")

    # Parsime mälus; ekraaniteksti ei salvestata ega tagastata
    return parse_claude_usage_screen(screen_text, datetime.now(timezone.utc), _default_tz())


def _run_claude_usage_windows(timeout_s: float = USAGE_TIMEOUT_S) -> dict[str, Any]:
    """Käivitab Windowsist WSL-i kaudu `claude /usage` (sest claude CLI on WSL-is)."""
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    script_src = Path(__file__).read_text(encoding="utf-8")
    try:
        proc = subprocess.run(
            ["wsl.exe", "-e", "python3", "-", "--probe-json", f"{timeout_s:g}"],
            input=script_src,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_s + 8,
            check=False,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _fail("wsl_exec_failed", "claude_reason_wsl_exec_failed", exc=exc)

    if proc.returncode != 0 and not proc.stdout.strip():
        return _fail("wsl_exit", "claude_reason_wsl_exit", code=proc.returncode)

    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
        if isinstance(data, dict):
            # Tõlgime WSL alamprotsessist saadud veakoodi aktiivsesse keelde
            code = data.get("error_code")
            if not data.get("ok") and code:
                code_map = {
                    "folder_trust": "claude_reason_folder_trust",
                    "auth_required": "claude_reason_auth_required",
                    "subscription_required": "claude_reason_subscription_required",
                    "rate_limited": "claude_reason_rate_limited",
                    "parse_failed": "claude_reason_parse_failed",
                    "cli_not_found": "claude_reason_cli_not_found",
                    "cli_exec_failed": "claude_reason_cli_exec_failed",
                    "pty_unsupported": "claude_reason_pty_unsupported",
                    "wsl_exec_failed": "claude_reason_wsl_exec_failed",
                    "wsl_exit": "claude_reason_wsl_exit",
                    "timeout": "claude_reason_timeout",
                }
                msg_key = str(data.get("reason_key") or code_map.get(str(code)) or "")
                if msg_key:
                    args = data.get("reason_args") if isinstance(data.get("reason_args"), dict) else {}
                    if code == "timeout" and "timeout" not in args:
                        msg_key = "claude_reason_loading_timeout"
                    data["reason_key"] = msg_key
                    data["reason_args"] = args
                    data["reason"] = _msg(msg_key, **args)
            return data
    except (json.JSONDecodeError, IndexError):
        pass
    return _fail("parse_failed", "claude_reason_wsl_invalid")


def probe_claude_usage(
    out_path: Path,
    timeout_s: float = USAGE_TIMEOUT_S,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Küsib värske Claude kvoodi `/usage` kaudu ja kirjutab kvoot_claude_usage.json faili."""
    res = _run_claude_usage_windows(timeout_s) if os.name == "nt" else _run_claude_usage_posix(timeout_s)
    if not res.get("ok"):
        return res

    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    payload: dict[str, Any] = {
        "written_at": now_utc.isoformat(timespec="seconds"),
        "five_hour": res.get("five_hour"),
        "seven_day": res.get("seven_day"),
    }
    if res.get("reason_code"):
        payload["reason_code"] = res["reason_code"]
    if res.get("missing_windows"):
        payload["missing_windows"] = res["missing_windows"]
    assert _kvoot_config is not None
    _kvoot_config.write_atomic_json(out_path, payload, prefix=".kvoot_usage_")
    out = {"ok": True, "path": str(out_path), **payload}
    if res.get("reason"):
        out["reason"] = res["reason"]
    return out


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--probe-json":
        t_out = float(sys.argv[2]) if len(sys.argv) >= 3 else USAGE_TIMEOUT_S
        result = _run_claude_usage_posix(timeout_s=t_out)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0 if result.get("ok") else 1)
