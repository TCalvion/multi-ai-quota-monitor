#!/usr/bin/env python3
"""Multi-AI Quota Monitor: loeb AI-tööriistade kvoodiseisu ja kirjutab kvoot.json + kvoot.html."""

from __future__ import annotations

__version__ = "4.7"
__doc__ = (
    f"Multi-AI Quota Monitor (v{__version__}): "
    "loeb AI-tööriistade kvoodiseisu ja kirjutab kvoot.json + kvoot.html."
)

import argparse
import concurrent.futures
import html
import json
import math
import os
import re
import subprocess
import sys
import time
import webbrowser
from collections.abc import Callable, Iterator, Sequence
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import antigravity_probe  # noqa: E402
import claude_usage  # noqa: E402
import kvoot_config  # noqa: E402
import kvoot_i18n  # noqa: E402
from kvoot_i18n import t  # noqa: E402

resolve_kodu_dir = kvoot_config.resolve_home_dir
default_antigravity_cache_path = antigravity_probe.default_cache_path
parse_iso_timestamp = kvoot_config.parse_iso_timestamp
_write_atomic_json = kvoot_config.write_atomic_json


def default_codex_sessions_dir() -> Path:
    """Vaikimisi tee Codexi sessioonikaustale."""
    return kvoot_config.resolve_home_dir() / ".codex" / "sessions"


def default_claude_quota_path() -> Path:
    """Vaikimisi tee Claude statusLine kvoodifailile."""
    return kvoot_config.resolve_home_dir() / ".claude" / "kvoot_claude.json"


def manual_claude_quota_path(claude_file: Path | str | None = None) -> Path:
    """Tee Claude'i käsitsi sisestatud kvoodifailile (samas kaustas kui kvoot_claude.json)."""
    base_file = Path(claude_file) if claude_file is not None else default_claude_quota_path()
    return base_file.parent / "kvoot_claude_manual.json"


def _legacy_manual_claude_quota_path(claude_file: Path | str | None = None) -> Path:
    """Vana eestikeelse nimega käsitsi sisestatud kvoodifaili tee (ülemineku jaoks)."""
    base_file = Path(claude_file) if claude_file is not None else default_claude_quota_path()
    return base_file.parent / "kvoot_claude_kasitsi.json"


def usage_claude_quota_path(claude_file: Path | str | None = None) -> Path:
    """Tee Claude'i `claude /usage` kvoodifailile (samas kaustas kui kvoot_claude.json)."""
    base_file = Path(claude_file) if claude_file is not None else default_claude_quota_path()
    return base_file.parent / "kvoot_claude_usage.json"


def _format_ts(dt: datetime, tz: tzinfo) -> str:
    return dt.astimezone(tz).isoformat(timespec="seconds")


def _age_minutes(source_dt: datetime, now_utc: datetime) -> int:
    """Tagastab allika vanuse täisminutites (>= 0)."""
    diff_sec = (now_utc - source_dt).total_seconds()
    return max(0, int(diff_sec // 60))


def _process_window(window: Any, now_ts: float, tz: tzinfo) -> dict[str, Any] | None:
    """Teisendab kvoodiakna (primary/secondary või five_hour/seven_day) kvoot/4 kujule."""
    if not isinstance(window, dict):
        return None
    used = window.get("used_percent")
    if used is None:
        used = window.get("used_percentage")
    if used is None or isinstance(used, bool):
        return None
    try:
        used_val = float(used)
    except (TypeError, ValueError):
        return None
    if math.isnan(used_val) or math.isinf(used_val):
        return None

    resets_at = window.get("resets_at")
    if resets_at is None:
        return {
            "used": round(used_val, 2),
            "resets_at": None,
            "reset": False,
        }

    try:
        if isinstance(resets_at, str):
            dt_r = parse_iso_timestamp(resets_at)
            resets_ts = float(resets_at) if dt_r is None else dt_r.timestamp()
        else:
            resets_ts = float(resets_at)
    except (TypeError, ValueError):
        return None

    is_reset = now_ts >= resets_ts
    used_final = 0.0 if is_reset else round(used_val, 2)
    resets_dt = datetime.fromtimestamp(resets_ts, tz=timezone.utc)
    return {
        "used": used_final,
        "resets_at": _format_ts(resets_dt, tz),
        "reset": is_reset,
    }


def _extract_rate_limits(obj: dict[str, Any]) -> dict[str, Any] | None:
    """Loeb JSON-realt ainult rate_limits objekti (vestluste teksti ei puuduta)."""
    payload = obj.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("rate_limits"), dict):
        return payload["rate_limits"]
    if isinstance(obj.get("rate_limits"), dict):
        return obj["rate_limits"]
    return None


def _infer_codex_location(path: Path) -> str:
    """Tuletab tee põhjal koha sildi (windows / wsl / local)."""
    low = str(path).replace("\\", "/").lower()
    if "wsl.localhost" in low or "/wsl" in low or low.startswith("/home/"):
        return "wsl"
    if "/mnt/" in low or "/users/" in low or re.match(r"^[a-z]:/", low):
        return "windows"
    return "local"


def _parse_codex_dirs_env(raw: str) -> list[tuple[str, Path]]:
    """Parsib KVOOT_CODEX_DIRS semikooloniga eraldatud kaustaloendi."""
    result: list[tuple[str, Path]] = []
    for part in raw.split(";"):
        item = part.strip()
        if not item:
            continue
        loc: str | None = None
        dir_str = item
        m = re.match(r"^([a-zA-Z0-9_-]+)=(.+)$", item)
        if m:
            loc = m.group(1).lower()
            dir_str = m.group(2).strip()
        p = Path(dir_str).expanduser()
        result.append((loc or _infer_codex_location(p), p))
    return result


def _discover_windows_wsl_codex_dirs_parts() -> tuple[list[tuple[str, Path]], list[tuple[str, dict[str, Any]]]]:
    """Leiab Windowsist WSL-i Codexi sessioonikaustad ja tagastab struktureeritud tõrked."""
    found: list[tuple[str, Path]] = []
    unavail: list[tuple[str, dict[str, Any]]] = []
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            ["wsl", "-l", "-q"],
            capture_output=True,
            timeout=3,
            check=False,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        unavail.append(("wsl", {"key": "codex_wsl_exec_failed", "args": {"exc": str(exc)}}))
        return found, unavail

    if proc.returncode != 0:
        unavail.append(("wsl", {"key": "codex_wsl_list_failed", "args": {"code": proc.returncode}}))
        return found, unavail

    raw_bytes = proc.stdout or b""
    encoding = "utf-16-le" if b"\x00" in raw_bytes else "utf-8"
    text = raw_bytes.decode(encoding, errors="replace").replace("\ufeff", "")
    distros = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().lower().startswith("docker-desktop")
    ]
    if not distros:
        unavail.append(("wsl", {"key": "codex_wsl_no_distros"}))
        return found, unavail

    for distro in distros:
        home_root = Path(f"\\\\wsl.localhost\\{distro}\\home")
        try:
            if not home_root.exists() or not home_root.is_dir():
                unavail.append(("wsl", {"key": "codex_wsl_home_missing", "args": {"path": str(home_root)}}))
                continue
            users = [
                p for p in sorted(home_root.iterdir())
                if p.name != "lost+found" and p.is_dir()
            ]
        except OSError as exc:
            unavail.append(("wsl", {"key": "codex_wsl_dir_unavail", "args": {"path": str(home_root), "exc": str(exc)}}))
            continue

        found_in_distro = False
        for user_dir in users:
            cand = user_dir / ".codex" / "sessions"
            try:
                if cand.exists() and cand.is_dir():
                    found.append(("wsl", cand))
                    found_in_distro = True
            except OSError as exc:
                unavail.append(("wsl", {"key": "codex_wsl_dir_unavail", "args": {"path": str(cand), "exc": str(exc)}}))
        if not found_in_distro and users:
            unavail.append(("wsl", {"key": "codex_wsl_no_sessions", "args": {"distro": distro}}))

    return found, unavail


def _discover_windows_wsl_codex_dirs() -> tuple[list[tuple[str, Path]], list[tuple[str, str]]]:
    """Leiab Windowsist WSL-i Codexi sessioonikaustad (\\\\wsl.localhost\\<distro>\\home\\<user>\\.codex\\sessions)."""
    found, unavail_parts = _discover_windows_wsl_codex_dirs_parts()
    return found, [(loc, kvoot_i18n.render_part(part)) for loc, part in unavail_parts]


def _discover_codex_dirs_parts() -> tuple[list[tuple[str, Path]], list[tuple[str, dict[str, Any]]]]:
    """Tagastab (loetavad_allikad, struktureeritud_kättesaamatud_allikad) Codexi sessioonikaustade kohta."""
    env_dirs = os.environ.get("KVOOT_CODEX_DIRS", "").strip()
    if env_dirs:
        return _parse_codex_dirs_env(env_dirs), []

    env_kodu = os.environ.get("KVOOT_KODU", "").strip()
    if env_kodu:
        p = Path(env_kodu).expanduser() / ".codex" / "sessions"
        return [(_infer_codex_location(p), p)], []

    if os.name == "nt":
        win_dir = kvoot_config.resolve_home_dir() / ".codex" / "sessions"
        wsl_dirs, unavail = _discover_windows_wsl_codex_dirs_parts()
        return [("windows", win_dir), *wsl_dirs], unavail

    sources: list[tuple[str, Path]] = []
    win_home = kvoot_config.resolve_win_home_wsl()
    if win_home is not None and win_home.exists():
        sources.append(("windows", win_home / ".codex" / "sessions"))
    wsl_dir = Path.home() / ".codex" / "sessions"
    if not sources or wsl_dir.resolve() != sources[0][1].resolve():
        sources.append(("wsl", wsl_dir))
    return sources, []


def discover_codex_dirs() -> tuple[list[tuple[str, Path]], list[tuple[str, str]]]:
    """Tagastab (loetavad_allikad, kättesaamatud_allikad) Codexi sessioonikaustade kohta."""
    sources, unavail_parts = _discover_codex_dirs_parts()
    return sources, [(loc, kvoot_i18n.render_part(part)) for loc, part in unavail_parts]


def _read_lines_reverse(fp: Path, chunk_size: int = 65536) -> Iterator[bytes]:
    """Genereerib faili read lõpust alguse suunas ilma tervet faili mällu laadimata."""
    with fp.open("rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        buf = b""
        while pos > 0:
            read_len = min(chunk_size, pos)
            pos -= read_len
            f.seek(pos)
            chunk = f.read(read_len) + buf
            lines = chunk.split(b"\n")
            buf = lines[0]
            for line in reversed(lines[1:]):
                if line:
                    yield line
        if buf:
            yield buf


def _collect_codex_files(base: Path, now_local_date: date, days: int) -> list[Path]:
    """Kogub rollout-*.jsonl failid ainult viimase N päeva (AAAA/KK/PP) kaustadest."""
    files: list[Path] = []
    for offset in range(days):
        d = now_local_date - timedelta(days=offset)
        day_dir = base / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.day:02d}"
        try:
            if day_dir.exists() and day_dir.is_dir():
                files.extend(sorted(day_dir.glob("rollout-*.jsonl")))
        except OSError:
            continue
    try:
        files.extend(sorted(base.glob("rollout-*.jsonl")))
    except OSError:
        pass
    return files


def read_codex_quota(
    sessions_dir: Path | str | None = None,
    now: datetime | None = None,
    codex_dirs: Sequence[tuple[str, Path | str]] | None = None,
) -> dict[str, Any]:
    """Loeb kõige värskema Codexi rate_limits kirje kõigist kohalikest allikatest."""
    t0 = time.perf_counter()
    tz = kvoot_config.resolve_tz()
    codex_days = kvoot_config.get_codex_days()
    worker_limit, lead_limit = kvoot_config.get_limits()
    stale_limit = kvoot_config.get_stale_minutes()

    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_ts = now_utc.timestamp()
    now_local_date = now_utc.astimezone(tz).date()

    if sessions_dir is not None:
        p = Path(sessions_dir)
        sources: list[tuple[str, Path]] = [(_infer_codex_location(p), p)]
        unreadable: list[tuple[str, dict[str, Any]]] = []
    elif codex_dirs is not None:
        sources = [(loc, Path(p)) for loc, p in codex_dirs]
        unreadable = []
    else:
        sources, unreadable = _discover_codex_dirs_parts()

    read_locations: list[str] = []
    freshest: tuple[datetime, str, str, dict[str, Any]] | None = None
    found_files = False

    for loc, base in sources:
        try:
            if not base.exists() or not base.is_dir():
                unreadable.append((loc, {"key": "codex_dir_missing", "args": {"path": str(base)}}))
                continue
        except OSError as exc:
            unreadable.append((loc, {"key": "codex_dir_unavail", "args": {"path": str(base), "exc": str(exc)}}))
            continue

        read_locations.append(loc)
        files = _collect_codex_files(base, now_local_date, codex_days)
        if not files:
            continue
        found_files = True

        for fp in files:
            try:
                for raw_line in _read_lines_reverse(fp):
                    if b"rate_limits" not in raw_line:
                        continue
                    line_txt = raw_line.decode("utf-8", errors="replace").strip()
                    if not line_txt:
                        continue
                    try:
                        obj = json.loads(line_txt)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue

                    rl = _extract_rate_limits(obj)
                    if rl is None:
                        continue

                    ts_dt = parse_iso_timestamp(obj.get("timestamp", ""))
                    if ts_dt is None:
                        continue

                    if (
                        _process_window(rl.get("primary"), now_ts, tz) is None
                        and _process_window(rl.get("secondary"), now_ts, tz) is None
                    ):
                        continue

                    cand_key = (ts_dt, str(fp))
                    if freshest is None or cand_key > (freshest[0], freshest[1]):
                        freshest = (ts_dt, str(fp), loc, rl)
                    # Failis loeme ainult viimast kehtivat rate_limits rida
                    break
            except OSError:
                continue

    read_time_ms = round((time.perf_counter() - t0) * 1000.0, 1)

    if freshest is None:
        if not read_locations and unreadable:
            reason_code = "codex_dir_not_found"
            reason_parts = [
                {
                    "key": "codex_reason_dir_not_found",
                    "args": {"details": {"parts": [p for _, p in unreadable], "sep": "; "}},
                }
            ]
        elif not found_files:
            reason_code = "codex_no_session_files"
            if unreadable:
                details_part = {
                    "parts": [{"prefix": f"{k}: ", **p} for k, p in unreadable],
                    "sep": "; ",
                }
                reason_parts = [
                    {
                        "key": "codex_reason_no_files_unavail",
                        "args": {"days": codex_days, "details": details_part},
                    }
                ]
            else:
                reason_parts = [{"key": "codex_reason_no_files", "args": {"days": codex_days}}]
        else:
            reason_code = "codex_no_rate_limits"
            reason_parts = [{"key": "codex_reason_no_rate_limits"}]
        return {
            "available": False,
            "reason_code": reason_code,
            "reason": kvoot_i18n.render_parts(reason_parts),
            "_reason_parts": reason_parts,
            "read_time_ms": read_time_ms,
        }

    ts_dt, _, winner_loc, rl = freshest
    win_5h = _process_window(rl.get("primary"), now_ts, tz)
    win_wk = _process_window(rl.get("secondary"), now_ts, tz)

    if win_5h is None and win_wk is None:
        reason_parts = [{"key": "codex_reason_missing_windows"}]
        return {
            "available": False,
            "reason_code": "codex_missing_windows",
            "reason": kvoot_i18n.render_parts(reason_parts),
            "_reason_parts": reason_parts,
            "read_time_ms": read_time_ms,
        }

    windows = [w for w in (win_5h, win_wk) if w is not None]
    max_val = max(w["used"] for w in windows)
    age_min = _age_minutes(ts_dt, now_utc)
    status = "stale" if age_min > stale_limit else kvoot_config.compute_status(max_val, worker_limit, lead_limit)

    note_parts: list[dict[str, Any]] = [{"key": "codex_note_local"}]
    if read_locations:
        note_parts.append({"key": "codex_note_sources", "args": {"sources": ", ".join(dict.fromkeys(read_locations))}})
    if unreadable:
        details_arg = {
            "parts": [{"prefix": f"{k} (", **p, "suffix": ")"} for k, p in unreadable],
            "sep": "; ",
        }
        note_parts.append({"key": "codex_note_unreadable", "args": {"details": details_arg}})
    note_code = "partial_sources" if unreadable else "local_sessions"

    return {
        "available": True,
        "source_time": _format_ts(ts_dt, tz),
        "source_location": winner_loc,
        "age_min": age_min,
        "window_5h": win_5h,
        "window_weekly": win_wk,
        "max": max_val,
        "status": status,
        "read_time_ms": read_time_ms,
        "note_code": note_code,
        "note": kvoot_i18n.render_parts(note_parts),
        "_note_parts": note_parts,
    }


def parse_percent(raw: Any, name: str = "percent") -> float:
    """Valideerib kvoodiprotsendi (0–100)."""
    if isinstance(raw, bool) or raw is None:
        raise ValueError(t("err_percent_type", name=name))
    try:
        val = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(t("err_percent_parse", name=name, raw=raw)) from exc
    if math.isnan(val) or math.isinf(val) or val < 0.0 or val > 100.0:
        raise ValueError(t("err_percent_range", name=name, val=val))
    return round(val, 2)


def parse_reset_time(
    raw: str | None,
    now_utc: datetime,
    tz: tzinfo,
) -> float | None:
    """Parsib valikulise lähtestumisaja (HH:MM kohalikus ajas või ISO 8601) epoch-sekunditeks."""
    if raw is None or not str(raw).strip():
        return None
    val = str(raw).strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", val)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError(t("err_time_hhmm", val=val))
        now_local = now_utc.astimezone(tz)
        candidate = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= now_local:
            candidate = candidate + timedelta(days=1)
        return candidate.astimezone(timezone.utc).timestamp()

    dt = parse_iso_timestamp(val)
    if dt is not None:
        return dt.timestamp()
    raise ValueError(t("err_reset_time", val=val))


parse_lahtestub = parse_reset_time


def save_claude_manual_quota(
    pct_5h: Any,
    pct_7d: Any,
    reset_5h: str | None = None,
    reset_7d: str | None = None,
    out_path: Path | str | None = None,
    now: datetime | None = None,
) -> Path:
    """Valideerib ja salvestab Claude'i käsitsi sisestatud kvoodi v4 skeemis."""
    tz = kvoot_config.resolve_tz()
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    val_5h = parse_percent(pct_5h, "--5h")
    val_7d = parse_percent(pct_7d, "--7d")
    ts_5h = parse_reset_time(reset_5h, now_utc, tz)
    ts_7d = parse_reset_time(reset_7d, now_utc, tz)

    target = Path(out_path) if out_path is not None else manual_claude_quota_path()
    payload = {
        "written_at": now_utc.isoformat(timespec="seconds"),
        "five_hour": {
            "used_percentage": val_5h,
            "resets_at": ts_5h,
        },
        "seven_day": {
            "used_percentage": val_7d,
            "resets_at": ts_7d,
        },
    }
    _write_atomic_json(target, payload)
    return target


def _read_one_claude_file(
    fp: Path,
    loc: str,
    now_utc: datetime,
    tz: tzinfo,
) -> tuple[datetime, str, dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None, str | None]:
    """Loeb ühe Claude kvoodifaili (toetab v3/v4 võtmeid). Tagastab (ts_dt, loc, w5, w7, file_note_part, err)."""
    if not fp.exists() or not fp.is_file():
        return now_utc, loc, None, None, None, f"missing: {fp}"

    try:
        raw = json.loads(fp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return now_utc, loc, None, None, None, f"read_error ({fp}): {exc}"

    if not isinstance(raw, dict):
        return now_utc, loc, None, None, None, f"not_object: {fp}"

    now_ts = now_utc.timestamp()
    win_5h = _process_window(raw.get("five_hour"), now_ts, tz)
    win_wk = _process_window(raw.get("seven_day"), now_ts, tz)
    if win_5h is None and win_wk is None:
        return now_utc, loc, None, None, None, f"no_windows: {fp}"

    ts_raw = raw.get("written_at") or raw.get("kirjutatud") or raw.get("updated_at") or ""
    ts_dt = parse_iso_timestamp(str(ts_raw))
    if ts_dt is None:
        ts_dt = datetime.fromtimestamp(fp.stat().st_mtime, tz=timezone.utc)

    file_note_part: dict[str, Any] | None = None
    if raw.get("reason_code") == "missing_window" and isinstance(raw.get("missing_windows"), list):
        label_map = {
            "five_hour": "Current session",
            "seven_day": "Current week (all models)",
        }
        labels = [label_map.get(str(w), str(w)) for w in raw["missing_windows"]]
        if labels:
            file_note_part = {"key": "claude_reason_missing_window", "args": {"windows": ", ".join(labels)}}
    if file_note_part is None:
        raw_reason = raw.get("reason") or raw.get("pohjus")
        if isinstance(raw_reason, str) and raw_reason.strip():
            file_note_part = {"text": raw_reason.strip()}
    return ts_dt, loc, win_5h, win_wk, file_note_part, None


def _select_claude_window(
    win_key: str,
    win_idx: int,
    candidates: list[tuple[datetime, int, str, dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]],
    now_utc: datetime,
    stale_limit: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, bool]:
    """Valib akna värskeimast allikast või selle puudumisel värskest varuallikast."""
    winner = candidates[0]
    winner_loc = winner[2]
    winner_win = winner[win_idx]
    if winner_win is not None:
        return {**winner_win, "source_location": winner_loc}, None, False

    win_part = {"key": win_key}
    for cand in candidates[1:]:
        cand_win = cand[win_idx]
        if cand_win is None:
            continue
        cand_age = _age_minutes(cand[0], now_utc)
        if cand_age <= stale_limit:
            cand_loc = cand[2]
            part = {
                "key": "claude_note_fallback_used",
                "args": {
                    "win": win_part,
                    "primary": winner_loc,
                    "fallback": cand_loc,
                    "age": cand_age,
                },
            }
            return {**cand_win, "source_location": cand_loc, "age_min": cand_age}, part, True

    return (
        None,
        {
            "key": "claude_note_fallback_missing",
            "args": {"win": win_part, "primary": winner_loc},
        },
        True,
    )


def refresh_claude_usage(
    claude_file: Path | str | None = None,
    claude_usage_file: Path | str | None = None,
    now: datetime | None = None,
    timeout_s: float = claude_usage.USAGE_TIMEOUT_S,
) -> dict[str, Any]:
    """Küsib Claude kvoodi `claude /usage` kaudu ja salvestab kvoot_claude_usage.json faili."""
    out_path = (
        Path(claude_usage_file)
        if claude_usage_file is not None
        else usage_claude_quota_path(claude_file)
    )
    return claude_usage.probe_claude_usage(out_path=out_path, timeout_s=timeout_s, now=now)


def read_claude_quota(
    claude_file: Path | str | None = None,
    now: datetime | None = None,
    claude_manual_file: Path | str | None = None,
    claude_usage_file: Path | str | None = None,
    usage_probe_error: str | None = None,
    usage_probe_error_code: str | None = None,
    usage_probe_error_part: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Loeb Claude'i kvoodi värskeima allika (usage_cli, statusline või manual)."""
    fp_status = Path(claude_file) if claude_file is not None else default_claude_quota_path()
    fp_usage = (
        Path(claude_usage_file)
        if claude_usage_file is not None
        else usage_claude_quota_path(fp_status)
    )
    if claude_manual_file is not None:
        fp_manual = Path(claude_manual_file)
    else:
        fp_manual = manual_claude_quota_path(fp_status)
        if not fp_manual.exists():
            legacy_manual = _legacy_manual_claude_quota_path(fp_status)
            if legacy_manual.exists():
                fp_manual = legacy_manual

    tz = kvoot_config.resolve_tz()
    worker_limit, lead_limit = kvoot_config.get_limits()
    stale_limit = kvoot_config.get_stale_minutes()
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    candidates: list[
        tuple[datetime, int, str, dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]
    ] = []

    for priority, (loc, fp) in enumerate(
        [("statusline", fp_status), ("usage_cli", fp_usage), ("manual", fp_manual)]
    ):
        ts_dt, _, win_5h, win_wk, file_note_part, err = _read_one_claude_file(fp, loc, now_utc, tz)
        if err is None:
            candidates.append((ts_dt, priority, loc, win_5h, win_wk, file_note_part))

    if not candidates:
        if usage_probe_error or usage_probe_error_part:
            reason_code = usage_probe_error_code or "claude_probe_failed"
            probe_part = usage_probe_error_part or {"text": str(usage_probe_error)}
            reason_parts = [
                {
                    "key": "claude_reason_probe_and_no_fallback",
                    "args": {
                        "probe_err": probe_part,
                        "status": str(fp_status),
                        "manual": str(fp_manual),
                    },
                }
            ]
        else:
            reason_code = "claude_file_not_found"
            reason_parts = [
                {
                    "key": "claude_reason_not_found",
                    "args": {
                        "usage": str(fp_usage),
                        "status": str(fp_status),
                        "manual": str(fp_manual),
                    },
                }
            ]
        return {
            "available": False,
            "reason_code": reason_code,
            "reason": kvoot_i18n.render_parts(reason_parts),
            "_reason_parts": reason_parts,
        }

    # Võrdse ajatempli korral eelistab uuemat allikat (manual > usage_cli > statusline)
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    ts_dt, _, winner_loc, _, _, winner_note_part = candidates[0]
    age_min = _age_minutes(ts_dt, now_utc)

    win_5h, fb_5h_part, had_missing_5h = _select_claude_window("win_5h_short", 3, candidates, now_utc, stale_limit)
    win_wk, fb_7d_part, had_missing_7d = _select_claude_window("win_7d_short", 4, candidates, now_utc, stale_limit)

    windows = [w for w in (win_5h, win_wk) if w is not None]
    max_val = max(w["used"] for w in windows)
    missing_window = win_5h is None or win_wk is None
    if age_min > stale_limit:
        status = "stale"
    elif missing_window:
        status = "unknown"
    else:
        status = kvoot_config.compute_status(max_val, worker_limit, lead_limit)

    if winner_loc == "usage_cli":
        note_parts: list[dict[str, Any]] = [{"key": "claude_note_usage_cli"}]
        base_code = "usage_cli"
    elif winner_loc == "statusline":
        note_parts = [{"key": "claude_note_statusline"}]
        base_code = "statusline"
    else:
        note_parts = [{"key": "claude_note_manual"}]
        base_code = "manual_entry"

    for fb_part in (fb_5h_part, fb_7d_part):
        if fb_part:
            note_parts.append(fb_part)

    if winner_note_part and kvoot_i18n.render_part(winner_note_part) != usage_probe_error:
        note_parts.append(winner_note_part)

    if usage_probe_error or usage_probe_error_part:
        probe_part = usage_probe_error_part or {"text": str(usage_probe_error)}
        if winner_loc != "usage_cli":
            note_parts.append({"key": "claude_note_probe_failed_fallback", "args": {"err": probe_part}})
        else:
            note_parts.append(probe_part)

    if usage_probe_error or usage_probe_error_part:
        note_code = "usage_probe_failed"
    elif had_missing_5h or had_missing_7d:
        note_code = "missing_window"
    else:
        note_code = base_code

    return {
        "available": True,
        "source_time": _format_ts(ts_dt, tz),
        "source_location": winner_loc,
        "age_min": age_min,
        "window_5h": win_5h,
        "window_weekly": win_wk,
        "max": max_val,
        "status": status,
        "note_code": note_code,
        "note": kvoot_i18n.render_parts(note_parts),
        "_note_parts": note_parts,
    }


def read_antigravity_quota(now: datetime | None = None) -> dict[str, Any]:
    """Loeb töötava Antigravity keeleserveri kvoodi ainult 127.0.0.1 kaudu."""
    worker_limit, lead_limit = kvoot_config.get_limits()
    return antigravity_probe.read_antigravity_quota(
        now=now,
        tz=kvoot_config.resolve_tz(),
        cache_path=default_antigravity_cache_path(for_write=True),
        stale_min=kvoot_config.get_stale_minutes(),
        worker_limit=worker_limit,
        lead_limit=lead_limit,
    )


def _safe_source(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Käivitab ühe allika lugeja nii, et tõrge ei katkesta teisi allikaid."""
    try:
        return fn()
    except Exception as exc:
        reason_parts = [{"key": "err_source_probe", "args": {"exc": antigravity_probe.mask_token(str(exc))}}]
        return {
            "available": False,
            "reason_code": "probe_error",
            "reason": kvoot_i18n.render_parts(reason_parts),
            "_reason_parts": reason_parts,
        }


def build_quota_report(
    codex_sessions_dir: Path | str | None = None,
    claude_file: Path | str | None = None,
    now: datetime | None = None,
    codex_dirs: Sequence[tuple[str, Path | str]] | None = None,
    claude_manual_file: Path | str | None = None,
    claude_usage_file: Path | str | None = None,
    refresh_all: bool = False,
    antigravity_reader: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Koostab kogu kvoodiraporti sõnastiku (--all/--koik korral loeb kõik 3 allikat paralleelselt)."""
    kvoot_config.clear_config_notes()
    tz = kvoot_config.resolve_tz()
    worker_limit, lead_limit = kvoot_config.get_limits()
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    def _read_codex() -> dict[str, Any]:
        return read_codex_quota(
            sessions_dir=codex_sessions_dir,
            now=now_utc,
            codex_dirs=codex_dirs,
        )

    def _read_claude() -> dict[str, Any]:
        probe_err: str | None = None
        probe_code: str | None = None
        probe_err_part: dict[str, Any] | None = None
        if refresh_all:
            try:
                res = refresh_claude_usage(
                    claude_file=claude_file,
                    claude_usage_file=claude_usage_file,
                    now=now_utc,
                )
                if not res.get("ok"):
                    probe_code = str(res.get("error_code") or "claude_probe_failed")
                    if res.get("reason_key"):
                        probe_err_part = {"key": str(res["reason_key"]), "args": dict(res.get("reason_args") or {})}
                        probe_err = kvoot_i18n.render_part(probe_err_part)
                    elif res.get("reason") or res.get("pohjus"):
                        probe_err = str(res.get("reason") or res.get("pohjus"))
                        probe_err_part = {"text": probe_err}
                    else:
                        probe_err_part = {"key": "claude_probe_failed_generic"}
                        probe_err = kvoot_i18n.render_part(probe_err_part)
                elif res.get("reason_key"):
                    probe_err_part = {"key": str(res["reason_key"]), "args": dict(res.get("reason_args") or {})}
                    probe_err = kvoot_i18n.render_part(probe_err_part)
                elif res.get("reason") or res.get("pohjus"):
                    probe_err = str(res.get("reason") or res.get("pohjus"))
                    probe_err_part = {"text": probe_err}
            except Exception as exc:
                probe_code = "probe_error"
                probe_err_part = {
                    "key": "claude_probe_exception",
                    "args": {"exc": antigravity_probe.mask_token(str(exc))},
                }
                probe_err = kvoot_i18n.render_part(probe_err_part)
        return read_claude_quota(
            claude_file=claude_file,
            now=now_utc,
            claude_manual_file=claude_manual_file,
            claude_usage_file=claude_usage_file,
            usage_probe_error=probe_err,
            usage_probe_error_code=probe_code,
            usage_probe_error_part=probe_err_part,
        )

    def _read_anti() -> dict[str, Any]:
        if antigravity_reader is not None:
            return antigravity_reader()
        return read_antigravity_quota(now=now_utc)

    if refresh_all:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            f_codex = pool.submit(_safe_source, _read_codex)
            f_claude = pool.submit(_safe_source, _read_claude)
            f_anti = pool.submit(_safe_source, _read_anti)
            sources = {
                "codex": f_codex.result(),
                "claude": f_claude.result(),
                "antigravity": f_anti.result(),
            }
    else:
        sources = {
            "codex": _safe_source(_read_codex),
            "claude": _safe_source(_read_claude),
            "antigravity": _safe_source(_read_anti),
        }

    workers_allowed: dict[str, bool | None] = {}
    lead_allowed: dict[str, bool | None] = {}

    for name, info in sources.items():
        missing_claude_win = name == "claude" and (
            info.get("window_5h") is None or info.get("window_weekly") is None
        )
        if (
            info.get("available")
            and info.get("max") is not None
            and info.get("status") != "stale"
            and not missing_claude_win
        ):
            m = float(info["max"])
            workers_allowed[name] = m < worker_limit
            lead_allowed[name] = m < lead_limit
        else:
            workers_allowed[name] = None
            lead_allowed[name] = None

    report: dict[str, Any] = {
        "schema": "kvoot/4",
        "read_at": _format_ts(now_utc, tz),
        "limits": {
            "worker": worker_limit,
            "lead": lead_limit,
        },
        "sources": sources,
        "workers_allowed": workers_allowed,
        "lead_allowed": lead_allowed,
    }
    config_notes = kvoot_config.get_config_notes()
    if config_notes:
        cfg_parts = [{"key": f"cfg_note_{n['code']}", "args": n["args"]} for n in config_notes]
        report["note_code"] = config_notes[0]["code"]
        report["note"] = kvoot_i18n.render_parts(cfg_parts)
        report["_note_parts"] = cfg_parts
    return report


def _i18n_attrs(key: str, args: dict[str, Any] | None = None) -> str:
    if args:
        payload = html.escape(json.dumps(args, ensure_ascii=False), quote=True)
        return f'data-i18n="{html.escape(key, quote=True)}" data-i18n-args="{payload}"'
    return f'data-i18n="{html.escape(key, quote=True)}"'


def _i18n_parts_attr(parts: Any) -> str:
    if not parts:
        return ""
    payload = html.escape(json.dumps(parts, ensure_ascii=False), quote=True)
    return f'data-i18n-parts="{payload}"'


def _render_legend_html(text: str) -> str:
    """Renderdab legendi turvaliselt: lubab ainult <strong>...</strong> märgendid, kõik muu põgeneb."""
    parts: list[str] = []
    last = 0
    for m in re.finditer(r"<strong>(.*?)</strong>", text, flags=re.DOTALL):
        if m.start() > last:
            parts.append(html.escape(text[last : m.start()]))
        parts.append(f"<strong>{html.escape(m.group(1))}</strong>")
        last = m.end()
    if last < len(text):
        parts.append(html.escape(text[last:]))
    return "".join(parts)


def _time_to_reset_parts(
    resets_iso: str | None,
    is_reset: bool,
    now_utc: datetime,
    lang: str,
) -> tuple[str, str, dict[str, Any] | None]:
    if is_reset:
        return t("html_reset_done", lang), "html_reset_done", None
    if not resets_iso:
        return t("html_unknown", lang), "html_unknown", None
    dt = parse_iso_timestamp(resets_iso)
    if dt is None:
        return resets_iso, "", None
    diff = int((dt - now_utc).total_seconds())
    if diff <= 0:
        return t("html_reset_done", lang), "html_reset_done", None
    days, rem = divmod(diff, 86400)
    hours, rem = divmod(rem, 3600)
    mins, _ = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} {t('html_unit_d', lang)}")
    if hours:
        parts.append(f"{hours} {t('html_unit_h', lang)}")
    if mins or not parts:
        parts.append(f"{mins} {t('html_unit_min', lang)}")
    return " ".join(parts), "__duration__", {"d": days, "h": hours, "m": mins}


def _stale_badge_parts(age_min: int | None, lang: str) -> tuple[str, str, dict[str, Any] | None]:
    if age_min is None:
        return t("html_stale_short", lang), "html_stale_short", None
    hours = int(age_min) // 60
    if hours >= 1:
        return t("html_stale_hours", lang, hours=hours), "html_stale_hours", {"hours": hours}
    return t("html_stale_mins", lang, mins=int(age_min)), "html_stale_mins", {"mins": int(age_min)}


def _allowed_badge_parts(val: bool | None, lang: str) -> tuple[str, str]:
    if val is True:
        return t("html_allowed_yes", lang), "html_allowed_yes"
    if val is False:
        return t("html_allowed_no", lang), "html_allowed_no"
    return t("html_allowed_unknown", lang), "html_allowed_unknown"


def _window_fallback_label(win: dict[str, Any] | None, parent_loc: str | None) -> str | None:
    if not isinstance(win, dict) or not parent_loc:
        return None
    win_loc = str(win.get("source_location") or "").strip()
    if not win_loc or win_loc == parent_loc:
        return None
    win_age = win.get("age_min")
    if isinstance(win_age, (int, float)) and not isinstance(win_age, bool):
        return f"{win_loc}, {int(win_age)} min"
    return win_loc


def _render_bar(
    label_key: str,
    win: dict[str, Any] | None,
    now_utc: datetime,
    lang: str,
    worker_limit: float,
    lead_limit: float,
    is_stale: bool = False,
    parent_loc: str | None = None,
) -> str:
    label_txt = t(label_key, lang)
    if win is None:
        return f"""
        <div class="bar-row">
          <div class="bar-head"><span {_i18n_attrs(label_key)}>{html.escape(label_txt)}</span><span {_i18n_attrs('html_window_missing')}>{html.escape(t('html_window_missing', lang))}</span></div>
        </div>"""

    val = float(win["used"])
    width = max(0.0, min(100.0, val))
    is_reset = bool(win.get("reset"))
    resets_raw = win.get("resets_at")
    resets_str = str(resets_raw) if resets_raw else ""
    rem_txt, rem_key, rem_args = _time_to_reset_parts(resets_str or None, is_reset, now_utc, lang)
    status = "stale" if is_stale else kvoot_config.compute_status(val, worker_limit, lead_limit)
    resets_utc = _utc_iso(resets_str) if resets_str else ""
    if resets_str and resets_utc:
        resets_span = f'<span class="muted">(<span data-utc-iso="{html.escape(resets_utc, quote=True)}">{html.escape(resets_str)}</span>)</span>'
    elif resets_str:
        resets_span = f'<span class="muted">({html.escape(resets_str)})</span>'
    else:
        resets_span = ""
    fb_label = _window_fallback_label(win, parent_loc)
    fb_span = (
        f'<span><span {_i18n_attrs("html_fallback_label")}>{html.escape(t("html_fallback_label", lang))}</span> <strong>{html.escape(fb_label)}</strong></span>'
        if fb_label
        else ""
    )

    if rem_key == "__duration__" and rem_args is not None:
        rem_attrs = f'data-i18n-duration="{html.escape(json.dumps(rem_args), quote=True)}"'
    elif rem_key:
        rem_attrs = _i18n_attrs(rem_key)
    else:
        rem_attrs = ""

    w_title = html.escape(t("html_marker_worker", lang, limit=f"{worker_limit:g}"), quote=True)
    l_title = html.escape(t("html_marker_lead", lang, limit=f"{lead_limit:g}"), quote=True)
    w_title_args = html.escape(json.dumps({"limit": f"{worker_limit:g}"}), quote=True)
    l_title_args = html.escape(json.dumps({"limit": f"{lead_limit:g}"}), quote=True)

    return f"""
        <div class="bar-row">
          <div class="bar-head">
            <span class="bar-label" {_i18n_attrs(label_key)}>{html.escape(label_txt)}</span>
            <span class="bar-val">{val:.1f}%</span>
          </div>
          <div class="track" role="progressbar" aria-valuenow="{val:.1f}" aria-valuemin="0" aria-valuemax="100">
            <div class="fill fill-{html.escape(status)}" style="width: {width:.2f}%"></div>
            <div class="marker marker-worker" style="left: {worker_limit:g}%" title="{w_title}" data-i18n-title="html_marker_worker" data-i18n-title-args="{w_title_args}"></div>
            <div class="marker marker-lead" style="left: {lead_limit:g}%" title="{l_title}" data-i18n-title="html_marker_lead" data-i18n-title-args="{l_title_args}"></div>
          </div>
          <div class="bar-meta">
            <span><span {_i18n_attrs('html_resets_in')}>{html.escape(t('html_resets_in', lang))}</span> <strong {rem_attrs}>{html.escape(rem_txt)}</strong></span>
            {fb_span}
            {resets_span}
          </div>
        </div>"""


def _utc_iso(ts_str: str | None) -> str:
    """Teisendab ajatempli UTC ISO-8601 stringiks (lõpuga Z) HTML data-* atribuutide jaoks."""
    if not ts_str:
        return ""
    dt = parse_iso_timestamp(str(ts_str))
    if dt is None:
        return ""
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _format_hh_mm(read_at_iso: str, now_utc: datetime, tz: tzinfo) -> str:
    dt = parse_iso_timestamp(read_at_iso)
    if dt is not None:
        return dt.astimezone(tz).strftime("%H:%M")
    return now_utc.astimezone(tz).strftime("%H:%M")


def _render_pools(
    pools: dict[str, Any],
    now_utc: datetime,
    lang: str,
    worker_limit: float,
    lead_limit: float,
    is_stale: bool,
) -> str:
    """Renderdab allika alamkogumid (nt Antigravity Gemini, Claude & GPT, Argon)."""
    parts: list[str] = []
    for pool in pools.values():
        if not isinstance(pool, dict):
            continue
        name = html.escape(str(pool.get("name") or t("html_pool_default", lang)))
        p_max = float(pool.get("max", 0.0))
        p_status = "stale" if is_stale else str(pool.get("status", kvoot_config.compute_status(p_max, worker_limit, lead_limit)))
        bars: list[str] = []
        if isinstance(pool.get("window_5h"), dict):
            bars.append(_render_bar("html_window_5h", pool["window_5h"], now_utc, lang, worker_limit, lead_limit, is_stale=is_stale))
        if isinstance(pool.get("window_weekly"), dict):
            bars.append(_render_bar("html_window_weekly", pool["window_weekly"], now_utc, lang, worker_limit, lead_limit, is_stale=is_stale))
        parts.append(f"""
        <div class="pool-block">
          <div class="pool-head">
            <span class="pool-title">{name}</span>
            <span class="badge badge-{html.escape(p_status)}">{p_max:.1f}%</span>
          </div>
          {''.join(bars)}
        </div>""")
    return "\n".join(parts)


def render_html(data: dict[str, Any], now: datetime | None = None) -> str:
    """Genereerib staatilise iseseisva kvoot.html vaate koos keelelülitiga."""
    lang = kvoot_i18n.resolve_lang()
    tz = kvoot_config.resolve_tz()
    bundles = kvoot_i18n.get_all_bundles()
    custom_title = kvoot_config.resolve_title_override()
    page_title = custom_title or t("app_title", lang)
    title_i18n = "" if custom_title else _i18n_attrs("app_title")

    limits = data.get("limits") if isinstance(data.get("limits"), dict) else {}
    worker_limit = float(limits.get("worker", kvoot_config.get_worker_limit()))
    lead_limit = float(limits.get("lead", kvoot_config.get_lead_limit()))

    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    read_at_raw = str(data.get("read_at", ""))
    read_at = html.escape(read_at_raw)
    read_at_utc = _utc_iso(read_at_raw) or now_utc.isoformat(timespec="seconds").replace("+00:00", "Z")
    hh_mm = html.escape(_format_hh_mm(read_at_raw, now_utc, tz))
    cards: list[str] = []

    w_str = f"{worker_limit:g}"
    l_str = f"{lead_limit:g}"
    names = {"codex": "Codex", "claude": "Claude", "antigravity": "Antigravity"}

    for key, info in data.get("sources", {}).items():
        heading = names.get(key, key.capitalize())
        if info.get("available"):
            status = str(info.get("status", "green"))
            is_stale = status == "stale"
            age_min = info.get("age_min")
            if is_stale:
                status_label, status_key, status_args = _stale_badge_parts(age_min, lang)
            else:
                status_key_map = {
                    "green": "html_status_green",
                    "yellow": "html_status_yellow",
                    "red": "html_status_red",
                    "unknown": "html_status_unknown",
                }
                status_key = status_key_map.get(status, "")
                status_args = {"worker": w_str, "lead": l_str} if status in ("green", "yellow", "red") else None
                status_label = t(status_key, lang, **(status_args or {})) if status_key else status

            source_time_raw = str(info.get("source_time", ""))
            source_time = html.escape(source_time_raw)
            source_time_utc = _utc_iso(source_time_raw)
            source_time_html = (
                f'<span data-utc-iso="{html.escape(source_time_utc, quote=True)}">{source_time}</span>'
                if source_time_utc
                else source_time
            )
            source_loc = str(info.get("source_location", "")).strip() or "local"
            age_silt = f"{int(age_min)} min" if age_min is not None else "-"
            if age_min is not None:
                upd_args: dict[str, Any] | None = {"mins": int(age_min)}
                upd_key = "html_updated_mins_ago"
                upd_label = t(upd_key, lang, mins=int(age_min))
            else:
                upd_args = None
                upd_key = "html_age_unknown"
                upd_label = t(upd_key, lang)

            note_raw = str(info.get("note", ""))
            note = html.escape(note_raw)
            note_code = str(info.get("note_code", ""))
            note_parts = info.get("_note_parts")
            if not note_parts:
                note_i18n_map = {
                    "last_known_cache": "ag_note_last_known",
                    "statusline": "claude_note_statusline",
                    "usage_cli": "claude_note_usage_cli",
                    "manual_entry": "claude_note_manual",
                    "language_server": "ag_note_live",
                }
                if note_code in note_i18n_map and note_raw == t(note_i18n_map[note_code], lang):
                    note_parts = [{"key": note_i18n_map[note_code]}]
            note_i18n_attr = _i18n_parts_attr(note_parts)

            max_v = float(info.get("max", 0.0))
            w_ok = data.get("workers_allowed", {}).get(key)
            l_ok = data.get("lead_allowed", {}).get(key)
            w_txt, w_key = _allowed_badge_parts(w_ok, lang)
            l_txt, l_key = _allowed_badge_parts(l_ok, lang)

            pools = info.get("pools")
            fb_parts: list[str] = []
            if isinstance(pools, dict) and pools:
                body_html = _render_pools(pools, now_utc, lang, worker_limit, lead_limit, is_stale=is_stale)
            else:
                win_5h = info.get("window_5h")
                win_wk = info.get("window_weekly")
                bar_5h = _render_bar("html_window_5h", win_5h, now_utc, lang, worker_limit, lead_limit, is_stale=is_stale, parent_loc=source_loc)
                bar_wk = _render_bar("html_window_weekly", win_wk, now_utc, lang, worker_limit, lead_limit, is_stale=is_stale, parent_loc=source_loc)
                body_html = f"{bar_5h}\n{bar_wk}"
                for tag_key, win_obj in (("win_5h_short", win_5h), ("win_7d_short", win_wk)):
                    silt = _window_fallback_label(win_obj, source_loc)
                    if silt:
                        fb_parts.append(
                            f'<span {_i18n_attrs(tag_key)}>{html.escape(t(tag_key, lang))}</span>: {html.escape(silt)}'
                        )

            card_cls = "card card-stale" if is_stale else "card"
            status_attrs = _i18n_attrs(status_key, status_args) if status_key else ""
            w_lbl_attrs = _i18n_attrs("html_workers_label", {"limit": w_str})
            l_lbl_attrs = _i18n_attrs("html_lead_label", {"limit": l_str})

            fb_foot = (
                f'<span class="muted"><span {_i18n_attrs("html_fallback_label")}>{html.escape(t("html_fallback_label", lang))}</span> <strong>{"; ".join(fb_parts)}</strong></span>'
                if fb_parts
                else ""
            )
            note_foot = (
                f'<span class="muted"><span {_i18n_attrs("html_note_label")}>{html.escape(t("html_note_label", lang))}</span> <span {note_i18n_attr}>{note}</span></span>'
                if note
                else ""
            )

            cards.append(f"""
      <section class="{card_cls}">
        <div class="card-head">
          <h2>{html.escape(heading)}</h2>
          <span class="badge badge-{html.escape(status)}"><span {status_attrs}>{html.escape(status_label)}</span> · max {max_v:.1f}%</span>
        </div>
        {body_html}
        <div class="card-foot">
          <span><span {w_lbl_attrs}>{html.escape(t('html_workers_label', lang, limit=w_str))}</span> <strong {_i18n_attrs(w_key)}>{html.escape(w_txt)}</strong> · <span {l_lbl_attrs}>{html.escape(t('html_lead_label', lang, limit=l_str))}</span> <strong {_i18n_attrs(l_key)}>{html.escape(l_txt)}</strong></span>
          <span class="muted"><span {_i18n_attrs('html_source_label')}>{html.escape(t('html_source_label', lang))}</span> <strong>{html.escape(source_loc)}</strong> · <span {_i18n_attrs('html_age_label')}>{html.escape(t('html_age_label', lang))}</span> <strong>{html.escape(age_silt)}</strong> (<span {_i18n_attrs(upd_key, upd_args)}>{html.escape(upd_label)}</span>) · <span {_i18n_attrs('html_source_time_label')}>{html.escape(t('html_source_time_label', lang))}</span> {source_time_html}</span>
          {fb_foot}
          {note_foot}
        </div>
      </section>""")
        else:
            reason_raw = str(info.get("reason") or "")
            reason_txt = html.escape(reason_raw or t("html_source_missing_default", lang))
            reason_parts = info.get("_reason_parts")
            if reason_parts:
                reason_attrs = _i18n_parts_attr(reason_parts)
            elif not reason_raw:
                reason_attrs = _i18n_attrs("html_source_missing_default")
            else:
                reason_attrs = ""
            cards.append(f"""
      <section class="card card-missing">
        <div class="card-head">
          <h2>{html.escape(heading)}</h2>
          <span class="badge badge-null" {_i18n_attrs('html_unknown_null')}>{html.escape(t('html_unknown_null', lang))}</span>
        </div>
        <p class="missing-reason" {reason_attrs}>{reason_txt}</p>
        <div class="card-foot">
          <span class="muted" {_i18n_attrs('html_source_missing_foot')}>{html.escape(t('html_source_missing_foot', lang))}</span>
        </div>
      </section>""")

    lang_options = "\n".join(
        f'          <option value="{html.escape(code, quote=True)}"{" selected" if code == lang else ""}>{html.escape(str(bundle.get("_lang_name") or code.upper()))}</option>'
        for code, bundle in bundles.items()
    )
    bundles_json = json.dumps(bundles, ensure_ascii=False).replace("<", "\\u003c")
    cards_html = "\n".join(cards)
    legend_html = _render_legend_html(t("html_legend", lang, worker=w_str, lead=l_str))
    legend_args = html.escape(json.dumps({"worker": w_str, "lead": l_str}), quote=True)
    cfg_note = str(data.get("note") or "")
    raw_note_parts = data.get("_note_parts")
    if isinstance(raw_note_parts, list) and raw_note_parts:
        notice_lines: list[str] = []
        for np in raw_note_parts:
            tz_attr = (
                ' data-tz-fixed-notice="1"'
                if isinstance(np, dict) and np.get("key") == "cfg_note_timezone_windows_fixed"
                else ""
            )
            rendered_np = html.escape(kvoot_i18n.render_part(np, lang=lang))
            notice_lines.append(
                f'    <div class="notice"{tz_attr}><span {_i18n_parts_attr([np])}>{rendered_np}</span></div>\n'
            )
        cfg_notice_html = "".join(notice_lines)
    elif cfg_note:
        tz_attr = ' data-tz-fixed-notice="1"' if data.get("note_code") == "timezone_windows_fixed" else ""
        cfg_notice_html = (
            f'    <div class="notice"{tz_attr}><span {_i18n_parts_attr(raw_note_parts)}>{html.escape(cfg_note)}</span></div>\n'
        )
    else:
        cfg_notice_html = ""

    return f"""<!DOCTYPE html>
<html lang="{html.escape(lang, quote=True)}" data-theme="dark">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title {title_i18n}>{html.escape(page_title)}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0d1117;
      --card: #151b23;
      --subcard: #11161d;
      --text: #e6edf3;
      --muted: #8b949e;
      --border: #2a323d;
      --track: #222a35;
      --green: #2ee59d;
      --green-text: #06281a;
      --green-glow: 0 0 12px rgba(46, 229, 157, 0.36);
      --yellow: #e39a3b;
      --red: #f15a5a;
      --stale: #6e7681;
      --null-bg: #1e252e;
      --marker: #c9d1d9;
    }}
    :root[data-theme="light"] {{
      color-scheme: light;
      --bg: #f4f6f8;
      --card: #ffffff;
      --subcard: #f8fafc;
      --text: #182026;
      --muted: #5c7080;
      --border: #d8e1e8;
      --track: #e1e8ed;
      --green: #0f9960;
      --green-text: #ffffff;
      --green-glow: 0 0 8px rgba(15, 153, 96, 0.25);
      --yellow: #d9822b;
      --red: #db3737;
      --stale: #7a8694;
      --null-bg: #edf2f7;
      --marker: #182026;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 24px 16px;
      font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.45;
    }}
    .wrap {{ max-width: 780px; margin: 0 auto; }}
    header {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      margin-bottom: 20px;
      flex-wrap: wrap;
    }}
    h1 {{ font-size: 1.35rem; margin: 0; font-weight: 600; }}
    .sub {{ font-size: 0.88rem; color: var(--muted); margin-top: 3px; }}
    .sub strong {{ color: var(--text); font-weight: 600; }}
    .controls {{ display: flex; align-items: flex-start; gap: 8px; flex-wrap: wrap; max-width: 100%; min-width: 0; }}
    .tz-control {{ display: flex; flex-direction: column; align-items: flex-start; gap: 3px; min-width: 0; max-width: 100%; }}
    button.theme-btn, select.lang-select, select.tz-select {{
      background: var(--card);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 6px 10px;
      cursor: pointer;
      font-size: 0.85rem;
    }}
    select.tz-select {{ width: auto; min-width: 0; max-width: 100%; }}
    .tz-current {{
      display: block;
      font-size: 0.78rem;
      color: var(--muted);
      line-height: 1.3;
      max-width: 100%;
      white-space: normal;
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    @media (min-width: 40em) {{
      .tz-current {{ display: none; }}
    }}
    .legend {{
      font-size: 0.82rem;
      color: var(--muted);
      margin-bottom: 14px;
    }}
    .notice {{
      font-size: 0.84rem;
      color: var(--yellow);
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 8px 12px;
      margin-bottom: 14px;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 16px 18px;
      margin-bottom: 14px;
    }}
    .card-stale {{ opacity: 0.85; }}
    .card-head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      margin-bottom: 12px;
      flex-wrap: wrap;
    }}
    .card-head h2 {{ font-size: 1.1rem; margin: 0; font-weight: 600; }}
    .badge {{
      font-size: 0.78rem;
      font-weight: 600;
      padding: 3px 10px;
      border-radius: 999px;
      color: #fff;
    }}
    .badge-green {{
      background: var(--green);
      color: var(--green-text);
      box-shadow: var(--green-glow);
    }}
    .badge-yellow {{ background: var(--yellow); color: #1a1204; }}
    .badge-red {{ background: var(--red); }}
    .badge-stale {{ background: var(--stale); color: #fff; }}
    .badge-null, .badge-unknown {{ background: var(--null-bg); color: var(--muted); border: 1px solid var(--border); }}
    .pool-block {{
      background: var(--subcard);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 12px;
      margin-bottom: 10px;
    }}
    .pool-head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
      gap: 8px;
    }}
    .pool-title {{ font-size: 0.92rem; font-weight: 600; }}
    .bar-row {{ margin-bottom: 12px; }}
    .pool-block .bar-row:last-child {{ margin-bottom: 0; }}
    .bar-head {{
      display: flex;
      justify-content: space-between;
      font-size: 0.9rem;
      margin-bottom: 5px;
    }}
    .bar-val {{ font-weight: 600; font-variant-numeric: tabular-nums; }}
    .track {{
      position: relative;
      height: 16px;
      background: var(--track);
      border-radius: 8px;
      overflow: hidden;
    }}
    .fill {{ height: 100%; transition: width 0.2s ease; }}
    .fill-green {{
      background: var(--green);
      box-shadow: var(--green-glow);
    }}
    .fill-yellow {{ background: var(--yellow); }}
    .fill-red {{ background: var(--red); }}
    .fill-stale {{ background: var(--stale); }}
    .marker {{
      position: absolute;
      top: 0;
      bottom: 0;
      width: 2px;
      background: var(--marker);
      opacity: 0.5;
    }}
    .bar-meta {{
      display: flex;
      justify-content: space-between;
      gap: 8px;
      font-size: 0.8rem;
      margin-top: 4px;
      flex-wrap: wrap;
      color: var(--muted);
    }}
    .bar-meta strong {{ color: var(--text); font-weight: 500; }}
    .card-foot {{
      display: flex;
      flex-direction: column;
      gap: 3px;
      font-size: 0.82rem;
      border-top: 1px solid var(--border);
      padding-top: 10px;
      margin-top: 6px;
    }}
    .muted {{ color: var(--muted); }}
    .missing-reason {{ margin: 4px 0 8px; font-size: 0.9rem; color: var(--muted); }}
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <div>
        <h1 {title_i18n}>{html.escape(page_title)}</h1>
        <div class="sub"><strong><span {_i18n_attrs('html_updated')}>{html.escape(t('html_updated', lang))}</span> <span data-utc-hhmm="{html.escape(read_at_utc, quote=True)}">{hh_mm}</span></strong> · <span class="muted" data-utc-iso="{html.escape(read_at_utc, quote=True)}">{read_at}</span></div>
      </div>
      <div class="controls">
        <div class="tz-control" id="tzControl">
          <select class="tz-select" id="tzSelect" aria-label="{html.escape(t('html_tz_label', lang), quote=True)}" title="{html.escape(t('html_tz_auto', lang), quote=True)}">
            <option value="auto" selected>{html.escape(t('html_tz_auto', lang))}</option>
          </select>
          <span class="tz-current" id="tzCurrent">{html.escape(t('html_tz_auto', lang))}</span>
        </div>
        <select class="lang-select" id="langSelect" aria-label="{html.escape(t('html_lang_label', lang), quote=True)}">
{lang_options}
        </select>
        <button class="theme-btn" id="themeToggle" type="button" aria-label="{html.escape(t('html_theme_to_light', lang), quote=True)}"><span id="themeIcon" aria-hidden="true">☀</span> <span {_i18n_attrs('html_theme_toggle')}>{html.escape(t('html_theme_toggle', lang))}</span></button>
      </div>
    </header>
    <div class="legend" data-i18n-legend="html_legend" data-i18n-args="{legend_args}">
      {legend_html}
    </div>
{cfg_notice_html}    {cards_html}
  </div>
  <script id="i18n-data" type="application/json">{bundles_json}</script>
  <script>
    (function () {{
      var root = document.documentElement;
      var btn = document.getElementById("themeToggle");
      var themeIcon = document.getElementById("themeIcon");
      var langSel = document.getElementById("langSelect");
      var tzControl = document.getElementById("tzControl");
      var tzSel = document.getElementById("tzSelect");
      var tzCurrent = document.getElementById("tzCurrent");
      var bundlesEl = document.getElementById("i18n-data");
      var bundles = {{}};
      try {{
        bundles = JSON.parse(bundlesEl ? bundlesEl.textContent || "{{}}" : "{{}}");
      }} catch (e) {{
        bundles = {{}};
      }}
      var FALLBACK_TZS = ["UTC", "Europe/Tallinn", "Europe/Helsinki", "Europe/London", "America/New_York"];
      var currentLang = bundles[root.getAttribute("lang")] ? root.getAttribute("lang") : (bundles.en ? "en" : "en");
      var currentTz = "auto";
      var autoTzName = "";
      try {{
        autoTzName = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
      }} catch (e) {{
        autoTzName = "";
      }}

      function fmt(tmpl, args) {{
        if (!args) return tmpl;
        return tmpl.replace(/\\{{([a-zA-Z0-9_]+)(?::([^}}]+))?\\}}/g, function (_, k, spec) {{
          if (!Object.prototype.hasOwnProperty.call(args, k)) {{
            return "{{" + k + (spec ? ":" + spec : "") + "}}";
          }}
          var v = args[k];
          if (spec && typeof v === "number" && isFinite(v)) {{
            if (spec === "g") return String(Number(v.toPrecision(6)));
            var m = /^\\.(\\d+)f$/.exec(spec);
            if (m) return v.toFixed(parseInt(m[1], 10));
          }}
          return String(v);
        }});
      }}

      function tr(code, key, args) {{
        var b = bundles[code] || bundles.en || {{}};
        var en = bundles.en || {{}};
        var raw = (typeof b[key] === "string" && b[key]) ? b[key] : (en[key] || key);
        return fmt(raw, args);
      }}

      function renderPart(code, part) {{
        if (typeof part === "string") return part;
        if (!part || typeof part !== "object") return String(part || "");
        var prefix = part.prefix ? String(part.prefix) : "";
        var suffix = part.suffix ? String(part.suffix) : "";
        if (Array.isArray(part.parts)) {{
          var sep = typeof part.sep === "string" ? part.sep : "; ";
          return prefix + part.parts.map(function (p) {{ return renderPart(code, p); }}).join(sep) + suffix;
        }}
        if (!part.key) return prefix + String(part.text || "") + suffix;
        var args = null;
        if (part.args && typeof part.args === "object") {{
          args = {{}};
          Object.keys(part.args).forEach(function (ak) {{
            var av = part.args[ak];
            if (av && typeof av === "object" && (av.key || Array.isArray(av.parts))) {{
              args[ak] = renderPart(code, av);
            }} else {{
              args[ak] = av;
            }}
          }});
        }}
        return prefix + tr(code, part.key, args) + suffix;
      }}

      function isValidTz(tzName) {{
        if (!tzName || typeof tzName !== "string") return false;
        try {{
          new Intl.DateTimeFormat("en", {{ timeZone: tzName }}).format(new Date(0));
          return true;
        }} catch (e) {{
          return false;
        }}
      }}

      function autoTzLabel(code) {{
        return autoTzName ? tr(code, "html_tz_auto_named", {{ tz: autoTzName }}) : tr(code, "html_tz_auto");
      }}

      function activeTzLabel(code, choice) {{
        return choice === "auto" ? autoTzLabel(code) : choice;
      }}

      function updateTzCurrent(code, choice) {{
        var label = activeTzLabel(code, choice);
        if (tzCurrent) {{
          tzCurrent.textContent = label;
        }}
        if (tzSel) {{
          tzSel.setAttribute("title", label);
        }}
      }}

      function populateTzSelect() {{
        if (!tzSel) return;
        var list = [];
        try {{
          if (typeof Intl !== "undefined" && typeof Intl.supportedValuesOf === "function") {{
            list = Intl.supportedValuesOf("timeZone") || [];
          }}
        }} catch (e) {{
          list = [];
        }}
        if (!Array.isArray(list) || !list.length) {{
          list = FALLBACK_TZS.slice();
        }} else {{
          list = list.slice();
        }}
        if (list.indexOf("UTC") === -1) {{
          list.unshift("UTC");
        }}
        if (autoTzName && isValidTz(autoTzName) && list.indexOf(autoTzName) === -1) {{
          list.push(autoTzName);
        }}
        tzSel.textContent = "";
        var autoOpt = document.createElement("option");
        autoOpt.value = "auto";
        autoOpt.textContent = autoTzLabel(currentLang);
        tzSel.appendChild(autoOpt);
        list.forEach(function (tz) {{
          var opt = document.createElement("option");
          opt.value = tz;
          opt.textContent = tz;
          tzSel.appendChild(opt);
        }});
      }}

      function makeFullFormatter(langCode, tzName) {{
        var candidates = ["shortOffset", "short", null];
        for (var i = 0; i < candidates.length; i++) {{
          var opts = {{
            timeZone: tzName,
            year: "numeric",
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
            hour12: false,
            hourCycle: "h23"
          }};
          if (candidates[i]) opts.timeZoneName = candidates[i];
          try {{
            var f = new Intl.DateTimeFormat(langCode, opts);
            f.format(new Date(0));
            return f;
          }} catch (e) {{}}
        }}
        return null;
      }}

      function applyTz(choice) {{
        var nextChoice = choice || "auto";
        if (nextChoice !== "auto" && !isValidTz(nextChoice)) {{
          nextChoice = "auto";
        }}
        currentTz = nextChoice;
        if (tzSel) {{
          tzSel.value = nextChoice;
        }}
        updateTzCurrent(currentLang, nextChoice);
        var effectiveTz = nextChoice === "auto" ? (isValidTz(autoTzName) ? autoTzName : "UTC") : nextChoice;
        var ok = false;
        try {{
          var fmtFull = makeFullFormatter(currentLang, effectiveTz);
          var fmtHm = new Intl.DateTimeFormat(currentLang, {{
            timeZone: effectiveTz,
            hour: "2-digit",
            minute: "2-digit",
            hour12: false,
            hourCycle: "h23"
          }});
          if (fmtFull) {{
            fmtHm.format(new Date(0));
            document.querySelectorAll("[data-utc-hhmm]").forEach(function (el) {{
              var iso = el.getAttribute("data-utc-hhmm");
              if (!iso) return;
              var d = new Date(iso);
              if (!isNaN(d.getTime())) {{
                el.textContent = fmtHm.format(d);
              }}
            }});
            document.querySelectorAll("[data-utc-iso]").forEach(function (el) {{
              var iso = el.getAttribute("data-utc-iso");
              if (!iso) return;
              var d = new Date(iso);
              if (!isNaN(d.getTime())) {{
                el.textContent = fmtFull.format(d);
              }}
            }});
            document.querySelectorAll("[data-tz-fixed-notice]").forEach(function (el) {{
              el.hidden = true;
              if (el.style) el.style.display = "none";
            }});
            ok = true;
          }}
        }} catch (e) {{
          ok = false;
        }}
        if (tzSel) {{
          tzSel.hidden = !ok;
          if (tzSel.style) tzSel.style.display = ok ? "" : "none";
        }}
        if (tzCurrent) {{
          tzCurrent.hidden = !ok;
          if (tzCurrent.style) tzCurrent.style.display = ok ? "" : "none";
        }}
        if (tzControl) {{
          tzControl.hidden = !ok;
          if (tzControl.style) tzControl.style.display = ok ? "" : "none";
        }}
        return nextChoice;
      }}

      function updateThemeBtn(theme, code) {{
        var isDark = theme !== "light";
        if (themeIcon) {{
          themeIcon.textContent = isDark ? "☀" : "☾";
        }}
        if (btn) {{
          btn.setAttribute("aria-label", tr(code, isDark ? "html_theme_to_light" : "html_theme_to_dark"));
        }}
      }}

      function applyLang(code) {{
        if (!bundles[code]) code = bundles.en ? "en" : Object.keys(bundles)[0] || "en";
        currentLang = code;
        root.setAttribute("lang", code);
        if (langSel) {{
          langSel.value = code;
          langSel.setAttribute("aria-label", tr(code, "html_lang_label"));
        }}
        if (tzSel) {{
          tzSel.setAttribute("aria-label", tr(code, "html_tz_label"));
          var autoOpt = tzSel.options && tzSel.options[0];
          if (autoOpt && autoOpt.value === "auto") {{
            autoOpt.textContent = autoTzLabel(code);
          }}
        }}
        document.querySelectorAll("[data-i18n]").forEach(function (el) {{
          var key = el.getAttribute("data-i18n");
          var rawArgs = el.getAttribute("data-i18n-args");
          var args = null;
          if (rawArgs) {{ try {{ args = JSON.parse(rawArgs); }} catch (e) {{}} }}
          el.textContent = tr(code, key, args);
        }});
        document.querySelectorAll("[data-i18n-legend]").forEach(function (el) {{
          var key = el.getAttribute("data-i18n-legend");
          var rawArgs = el.getAttribute("data-i18n-args");
          var args = null;
          if (rawArgs) {{ try {{ args = JSON.parse(rawArgs); }} catch (e) {{}} }}
          var txt = tr(code, key, args);
          el.textContent = "";
          var re = /<strong>([\\s\\S]*?)<\\/strong>/g;
          var last = 0;
          var m;
          while ((m = re.exec(txt)) !== null) {{
            if (m.index > last) {{
              el.appendChild(document.createTextNode(txt.slice(last, m.index)));
            }}
            var st = document.createElement("strong");
            st.textContent = m[1];
            el.appendChild(st);
            last = re.lastIndex;
          }}
          if (last < txt.length) {{
            el.appendChild(document.createTextNode(txt.slice(last)));
          }}
        }});
        document.querySelectorAll("[data-i18n-parts]").forEach(function (el) {{
          try {{
            var spec = JSON.parse(el.getAttribute("data-i18n-parts") || "[]");
            if (Array.isArray(spec)) {{
              el.textContent = spec.map(function (p) {{ return renderPart(code, p); }}).join("; ");
            }} else if (spec && typeof spec === "object") {{
              el.textContent = renderPart(code, spec);
            }}
          }} catch (e) {{}}
        }});
        document.querySelectorAll("[data-i18n-title]").forEach(function (el) {{
          var key = el.getAttribute("data-i18n-title");
          var rawArgs = el.getAttribute("data-i18n-title-args");
          var args = null;
          if (rawArgs) {{ try {{ args = JSON.parse(rawArgs); }} catch (e) {{}} }}
          el.setAttribute("title", tr(code, key, args));
        }});
        document.querySelectorAll("[data-i18n-duration]").forEach(function (el) {{
          try {{
            var p = JSON.parse(el.getAttribute("data-i18n-duration") || "{{}}");
            var parts = [];
            if (p.d) parts.push(p.d + " " + tr(code, "html_unit_d"));
            if (p.h) parts.push(p.h + " " + tr(code, "html_unit_h"));
            if (p.m || !parts.length) parts.push(p.m + " " + tr(code, "html_unit_min"));
            el.textContent = parts.join(" ");
          }} catch (e) {{}}
        }});
        applyTz(currentTz);
        updateThemeBtn(root.getAttribute("data-theme") || "dark", code);
      }}

      populateTzSelect();

      var savedTheme = null;
      try {{ savedTheme = localStorage.getItem("kvoot-theme"); }} catch (e) {{}}
      if (savedTheme === "light" || savedTheme === "dark") {{
        root.setAttribute("data-theme", savedTheme);
      }}
      updateThemeBtn(root.getAttribute("data-theme") || "dark", currentLang);
      if (btn) {{
        btn.addEventListener("click", function () {{
          var cur = root.getAttribute("data-theme") || "dark";
          var next = cur === "dark" ? "light" : "dark";
          root.setAttribute("data-theme", next);
          updateThemeBtn(next, currentLang);
          try {{ localStorage.setItem("kvoot-theme", next); }} catch (e) {{}}
        }});
      }}

      var savedLang = null;
      try {{ savedLang = localStorage.getItem("kvoot-lang"); }} catch (e) {{}}
      if (savedLang && bundles[savedLang]) {{
        applyLang(savedLang);
      }}
      if (langSel) {{
        langSel.addEventListener("change", function () {{
          var nextLang = langSel.value;
          applyLang(nextLang);
          try {{ localStorage.setItem("kvoot-lang", nextLang); }} catch (e) {{}}
        }});
      }}

      var savedTz = null;
      try {{ savedTz = localStorage.getItem("kvoot-tz"); }} catch (e) {{}}
      var initialTz = applyTz(savedTz || "auto");
      if (savedTz && initialTz !== savedTz) {{
        try {{ localStorage.setItem("kvoot-tz", initialTz); }} catch (e) {{}}
      }}
      if (tzSel) {{
        tzSel.addEventListener("change", function () {{
          var chosen = applyTz(tzSel.value);
          try {{ localStorage.setItem("kvoot-tz", chosen); }} catch (e) {{}}
        }});
      }}
    }})();
  </script>
</body>
</html>
"""


def _strip_private_keys(obj: Any) -> Any:
    """Eemaldab JSON-väljundist sisemised '_' algusega abiväljad, jättes kvoot/4 skeemi puhtaks."""
    if isinstance(obj, dict):
        return {k: _strip_private_keys(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [_strip_private_keys(item) for item in obj]
    return obj


def write_outputs(data: dict[str, Any], out_dir: Path | None = None, now: datetime | None = None) -> tuple[Path, Path]:
    """Kirjutab kvoot.json ja kvoot.html failid."""
    target_dir = out_dir or BASE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    json_path = target_dir / "kvoot.json"
    html_path = target_dir / "kvoot.html"

    json_path.write_text(json.dumps(_strip_private_keys(data), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    html_path.write_text(render_html(data, now=now), encoding="utf-8")
    return json_path, html_path


def _normalize_role(role: str) -> str:
    return "worker" if role in ("worker", "tooline") else "lead"


def check_permission(data: dict[str, Any], role: str, source: str) -> int:
    """Tagastab väljumiskoodi: 0=lubatud, 1=keelatud, 2=allikas puudub, 3=vana või puudulik näit."""
    norm_role = _normalize_role(role)
    key = "workers_allowed" if norm_role == "worker" else "lead_allowed"
    allowed = data.get(key, {}).get(source)
    if allowed is True:
        return 0
    if allowed is False:
        return 1
    info = data.get("sources", {}).get(source)
    if isinstance(info, dict) and info.get("available"):
        if info.get("status") in ("stale", "unknown") or (
            source == "claude" and (info.get("window_5h") is None or info.get("window_weekly") is None)
        ):
            return 3
    return 2


def _open_in_browser(html_path: Path) -> None:
    """Avab genereeritud kvoot.html vaate vaikebrauseris."""
    resolved = html_path.resolve()
    if os.name == "nt" and hasattr(os, "startfile"):
        os.startfile(str(resolved))  # type: ignore[attr-defined]
        return
    webbrowser.open(resolved.as_uri())


def _safe_print(msg: str, stderr: bool = False) -> None:
    stream = sys.stderr if stderr else sys.stdout
    if stream is not None:
        print(msg, file=stream)


def main(argv: list[str] | None = None, now: datetime | None = None) -> int:
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--lang", dest="lang", default=None)
    pre_args, _ = pre_parser.parse_known_args(argv)

    had_env_lang = "KVOOT_LANG" in os.environ
    prev_env_lang = os.environ.get("KVOOT_LANG")
    lang_override = str(pre_args.lang).strip() if pre_args.lang is not None else ""
    if lang_override:
        os.environ["KVOOT_LANG"] = lang_override

    try:
        return _main_with_env(argv=argv, now=now)
    finally:
        if lang_override:
            if had_env_lang and prev_env_lang is not None:
                os.environ["KVOOT_LANG"] = prev_env_lang
            else:
                os.environ.pop("KVOOT_LANG", None)


def _main_with_env(argv: list[str] | None = None, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description=t("cli_desc", version=__version__))
    parser.add_argument(
        "--lang",
        dest="lang",
        default=None,
        help=t("cli_help_lang"),
    )
    parser.add_argument(
        "--check",
        "--kontroll",
        dest="check_role",
        choices=["worker", "lead", "tooline", "juht"],
        help=t("cli_help_check"),
    )
    parser.add_argument(
        "--source",
        "--allikas",
        dest="source",
        default="codex",
        help=t("cli_help_source"),
    )
    parser.add_argument(
        "--codex-dir",
        type=Path,
        default=None,
        help=t("cli_help_codex_dir"),
    )
    parser.add_argument(
        "--claude-file",
        type=Path,
        default=None,
        help=t("cli_help_claude_file"),
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help=t("cli_help_out_dir"),
    )
    parser.add_argument(
        "--all",
        "--koik",
        dest="refresh_all",
        action="store_true",
        help=t("cli_help_all"),
    )
    parser.add_argument(
        "--open",
        "--ava",
        dest="open_browser",
        action="store_true",
        help=t("cli_help_open"),
    )
    parser.add_argument(
        "--set",
        "--sisesta",
        dest="set_source",
        choices=["claude"],
        default=None,
        help=t("cli_help_set"),
    )
    parser.add_argument(
        "--5h",
        dest="pct_5h",
        type=str,
        default=None,
        help=t("cli_help_5h"),
    )
    parser.add_argument(
        "--7d",
        "--7p",
        dest="pct_7d",
        type=str,
        default=None,
        help=t("cli_help_7d"),
    )
    parser.add_argument(
        "--5h-reset",
        "--5h-lahtestub",
        dest="reset_5h",
        type=str,
        default=None,
        help=t("cli_help_5h_reset"),
    )
    parser.add_argument(
        "--7d-reset",
        "--7p-lahtestub",
        dest="reset_7d",
        type=str,
        default=None,
        help=t("cli_help_7d_reset"),
    )
    args = parser.parse_args(argv)

    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    if args.set_source == "claude":
        if args.pct_5h is None or args.pct_7d is None:
            _safe_print(t("cli_err_prefix", msg=t("cli_err_set_requires_5h_7d")), stderr=True)
            return 2
        target = manual_claude_quota_path(args.claude_file)
        try:
            saved_path = save_claude_manual_quota(
                pct_5h=args.pct_5h,
                pct_7d=args.pct_7d,
                reset_5h=args.reset_5h,
                reset_7d=args.reset_7d,
                out_path=target,
                now=now_utc,
            )
        except ValueError as exc:
            _safe_print(t("cli_err_prefix", msg=exc), stderr=True)
            return 2
        _safe_print(
            t(
                "cli_saved_manual",
                path=saved_path,
                pct_5h=float(args.pct_5h),
                pct_7d=float(args.pct_7d),
            )
        )
        return 0

    data = build_quota_report(
        codex_sessions_dir=args.codex_dir,
        claude_file=args.claude_file,
        now=now_utc,
        refresh_all=args.refresh_all,
    )

    if args.check_role:
        code = check_permission(data, args.check_role, args.source)
        info = data.get("sources", {}).get(args.source)
        norm_role = _normalize_role(args.check_role)
        role_txt = t("role_worker") if norm_role == "worker" else t("role_lead")
        if code == 0:
            st_txt = t(f"status_{info['status']}")
            _safe_print(t("cli_check_allowed", role=role_txt, source=args.source, max=info["max"], status=st_txt))
        elif code == 1:
            st_txt = t(f"status_{info['status']}")
            _safe_print(t("cli_check_denied", role=role_txt, source=args.source, max=info["max"], status=st_txt))
        elif code == 3:
            if info.get("status") == "stale":
                _safe_print(
                    t(
                        "cli_check_stale",
                        role=role_txt,
                        source=args.source,
                        age=info.get("age_min"),
                        max=info["max"],
                    )
                )
            else:
                _safe_print(
                    t(
                        "cli_check_missing_window",
                        role=role_txt,
                        source=args.source,
                        max=info["max"],
                    )
                )
        else:
            reason = (
                info.get("reason")
                if isinstance(info, dict)
                else t("cli_check_unknown_source", source=args.source)
            )
            _safe_print(t("cli_check_unknown", role=role_txt, source=args.source, reason=reason))
        return code

    json_path, html_path = write_outputs(data, out_dir=args.out_dir, now=now_utc)
    if args.open_browser:
        _open_in_browser(html_path)
    summary_parts = []
    for name in ("codex", "claude", "antigravity"):
        info = data["sources"][name]
        if info.get("available"):
            st_txt = t(f"status_{info['status']}")
            summary_parts.append(f"{name}: {info['max']:.1f}%, {st_txt}")
        else:
            summary_parts.append(t("cli_summary_missing", source=name))
    _safe_print(
        t(
            "cli_updated_summary",
            json_name=json_path.name,
            html_name=html_path.name,
            summary="; ".join(summary_parts),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
