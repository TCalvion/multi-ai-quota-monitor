#!/usr/bin/env python3
"""Ühine seadistus, teede tuvastus ja atomaarne JSON-kirjutus kvoodimõõdiku jaoks."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
import threading
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BASE_DIR = Path(__file__).resolve().parent

DEFAULT_WORKER_LIMIT = 50.0
DEFAULT_LEAD_LIMIT = 70.0
DEFAULT_STALE_MINUTES = 180
DEFAULT_CODEX_DAYS = 3

_WIN_SYSTEM_USER_DIRS = frozenset(
    {"public", "default", "default user", "all users", "defaultapppool"}
)

_notes_lock = threading.Lock()
_config_notes: list[dict[str, Any]] = []


def _record_note(code: str, args: dict[str, Any] | None = None) -> None:
    """Salvestab seadistuse või keskkonna tuvastuse hoiatuskoodi koos argumentidega."""
    clean_args = dict(args) if args else {}
    sig = (code, tuple(sorted((k, str(v)) for k, v in clean_args.items())))
    with _notes_lock:
        for existing in _config_notes:
            ex_sig = (
                existing["code"],
                tuple(sorted((k, str(v)) for k, v in existing["args"].items())),
            )
            if ex_sig == sig:
                return
        _config_notes.append({"code": code, "args": clean_args})


def clear_config_notes() -> None:
    """Tühjendab kogutud seadistuse hoiatuskoodid."""
    with _notes_lock:
        _config_notes.clear()


def get_config_notes() -> list[dict[str, Any]]:
    """Tagastab kogutud seadistuse hoiatuskoodide koopia."""
    with _notes_lock:
        return [{"code": n["code"], "args": dict(n["args"])} for n in _config_notes]


def _is_windows() -> bool:
    """Tagastab True Windowsi keskkonnas (testides eraldi mockitav ilma os.name muutmata)."""
    return os.name == "nt"


def load_config(config_path: Path | str | None = None) -> dict[str, Any]:
    """Laeb valikulise kvoot.config.json faili (KVOOT_CONFIG ülekirjutus või skripti kaust)."""
    if config_path is not None:
        target = Path(config_path).expanduser()
        explicit = True
    else:
        env_cfg = os.environ.get("KVOOT_CONFIG", "").strip()
        if env_cfg:
            target = Path(env_cfg).expanduser()
            explicit = True
        else:
            target = BASE_DIR / "kvoot.config.json"
            explicit = False

    if not target.exists() or not target.is_file():
        if explicit:
            _record_note("config_not_found", {"path": str(target)})
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            return data
        _record_note("config_invalid", {"path": str(target)})
    except (OSError, json.JSONDecodeError):
        _record_note("config_invalid", {"path": str(target)})
    return {}


def _get_setting(env_names: tuple[str, ...], config_key: str) -> str:
    """Tagastab seadistuse väärtuse järjekorras: keskkonnamuutuja -> kvoot.config.json -> ''."""
    for env_name in env_names:
        val = os.environ.get(env_name, "").strip()
        if val:
            return val
    cfg = load_config()
    if config_key in cfg and cfg[config_key] is not None:
        val = str(cfg[config_key]).strip()
        if val:
            return val
    return ""


def _win_path_to_wsl(raw_path: str) -> Path | None:
    """Teisendab Windowsi tee WSL-i teeks (wslpath või draivi-eesliite abil)."""
    cleaned = raw_path.strip().strip('"').strip("'")
    if not cleaned:
        return None
    if cleaned.startswith("/"):
        return Path(cleaned)

    m = re.match(r"^([A-Za-z]):[\\/](.*)$", cleaned)
    try:
        proc = subprocess.run(
            ["wslpath", "-u", cleaned],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
            check=False,
        )
        out = (proc.stdout or "").strip()
        if proc.returncode == 0 and out.startswith("/"):
            return Path(out)
    except (OSError, subprocess.SubprocessError):
        pass

    if m:
        drive = m.group(1).lower()
        rest = m.group(2).replace("\\", "/").lstrip("/")
        return Path(f"/mnt/{drive}/{rest}" if rest else f"/mnt/{drive}")
    return None


def resolve_win_home_wsl(users_root: Path | None = None) -> Path | None:
    """Tuvastab WSL-is Windowsi kasutajaprofiili ilma kasutajanime kõvakodeerimata."""
    if _is_windows():
        return None

    env_win_home = os.environ.get("KVOOT_WIN_HOME", "").strip()
    if env_win_home:
        converted = _win_path_to_wsl(env_win_home)
        return converted if converted is not None else Path(env_win_home).expanduser()

    root = users_root if users_root is not None else Path("/mnt/c/Users")
    is_wsl = bool(os.environ.get("WSL_DISTRO_NAME")) or root.is_dir()
    if not is_wsl:
        return None

    if users_root is None:
        cwd = "/mnt/c" if Path("/mnt/c").is_dir() else None
        try:
            proc = subprocess.run(
                ["cmd.exe", "/c", "echo", "%USERPROFILE%"],
                stdin=subprocess.DEVNULL,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=2,
                check=False,
            )
            out = (proc.stdout or "").strip()
            if proc.returncode == 0 and out and "%USERPROFILE%" not in out:
                first_line = out.splitlines()[0].strip()
                cand = _win_path_to_wsl(first_line)
                if cand is not None and cand.exists() and cand.is_dir():
                    return cand
        except (OSError, subprocess.SubprocessError):
            pass

    if not root.exists() or not root.is_dir():
        return None

    try:
        candidates = [
            p
            for p in sorted(root.iterdir(), key=lambda item: item.name.lower())
            if p.is_dir()
            and not p.name.startswith(".")
            and p.name.lower() not in _WIN_SYSTEM_USER_DIRS
        ]
    except OSError:
        return None

    if len(candidates) == 1:
        return candidates[0]

    ai_markers = (".codex", ".claude", ".kvoot", ".tc-kvoot")
    with_ai = [
        p for p in candidates if any((p / marker).exists() for marker in ai_markers)
    ]
    if len(with_ai) == 1:
        return with_ai[0]
    if len(with_ai) > 1:
        _record_note("win_home_ambiguous", {"profiles": ", ".join(p.name for p in with_ai)})
        return None
    return None


def resolve_home_dir() -> Path:
    """Tagastab kasutaja kodukausta (KVOOT_KODU, Windows %USERPROFILE%, või WSL dünaamiline profiil)."""
    env_kodu = os.environ.get("KVOOT_KODU", "").strip()
    if env_kodu:
        return Path(env_kodu).expanduser()
    if _is_windows():
        up = os.environ.get("USERPROFILE", "").strip() or os.path.expandvars("%USERPROFILE%")
        if up and up != "%USERPROFILE%":
            return Path(up)
        return Path.home()
    win_home = resolve_win_home_wsl()
    if win_home is not None and win_home.exists():
        return win_home
    return Path.home()


resolve_kodu_dir = resolve_home_dir


def resolve_state_dir(for_write: bool = True) -> Path:
    """Tagastab olekukausta (~/.kvoot, KVOOT_STATE_DIR või üleminekul legacy state dir)."""
    override = _get_setting(("KVOOT_STATE_DIR",), "state_dir")
    if override:
        return Path(override).expanduser()
    home = resolve_home_dir()
    new_dir = home / ".kvoot"
    old_dir = home / ".tc-kvoot"  # legacy state dir
    if not for_write and not new_dir.exists() and old_dir.exists() and old_dir.is_dir():
        return old_dir
    return new_dir


def _parse_limit(raw: str, default: float) -> float:
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        return default
    if math.isnan(val) or math.isinf(val) or val <= 0.0 or val > 100.0:
        return default
    return val


def get_limits() -> tuple[float, float]:
    """Tagastab (worker_limit, lead_limit); kui worker_limit > lead_limit, taastab vaikeväärtused."""
    w_lim = _parse_limit(_get_setting(("KVOOT_WORKER_LIMIT",), "worker_limit"), DEFAULT_WORKER_LIMIT)
    l_lim = _parse_limit(_get_setting(("KVOOT_LEAD_LIMIT",), "lead_limit"), DEFAULT_LEAD_LIMIT)
    if w_lim > l_lim:
        _record_note("limits_inverted", {"worker": f"{w_lim:g}", "lead": f"{l_lim:g}"})
        return DEFAULT_WORKER_LIMIT, DEFAULT_LEAD_LIMIT
    return w_lim, l_lim


def get_worker_limit() -> float:
    """Tagastab tööliste kvoodipiiri protsentides (vaikimisi 50.0)."""
    return get_limits()[0]


def get_lead_limit() -> float:
    """Tagastab juhi kvoodipiiri protsentides (vaikimisi 70.0)."""
    return get_limits()[1]


def get_stale_minutes() -> int:
    """Tagastab vana näidu piiri minutites (KVOOT_STALE_MIN / KVOOT_VANA_MIN, vaikimisi 180)."""
    raw = _get_setting(("KVOOT_STALE_MIN", "KVOOT_VANA_MIN"), "stale_minutes")
    if not raw:
        return DEFAULT_STALE_MINUTES
    try:
        val = int(raw)
        return val if val >= 0 else DEFAULT_STALE_MINUTES
    except ValueError:
        return DEFAULT_STALE_MINUTES


def get_codex_days() -> int:
    """Tagastab Codexi sessioonifailide päevade akna (vaikimisi 3)."""
    raw = _get_setting(("KVOOT_CODEX_DAYS",), "codex_days")
    if not raw:
        return DEFAULT_CODEX_DAYS
    try:
        val = int(raw)
        return val if val >= 1 else DEFAULT_CODEX_DAYS
    except ValueError:
        return DEFAULT_CODEX_DAYS


def resolve_title_override() -> str | None:
    """Tagastab pealkirja ülekirjutuse (KVOOT_TITLE või config), kui see on määratud."""
    val = _get_setting(("KVOOT_TITLE",), "title")
    return val or None


def compute_status(
    usage: float,
    worker_limit: float | None = None,
    lead_limit: float | None = None,
) -> str:
    """Arvutab kvoodiseisu (green / yellow / red) seadistatud piiride põhjal."""
    w_lim = get_worker_limit() if worker_limit is None else worker_limit
    l_lim = get_lead_limit() if lead_limit is None else lead_limit
    if usage < w_lim:
        return "green"
    if usage < l_lim:
        return "yellow"
    return "red"


class _TallinnTZ(tzinfo):
    """Standardteegil põhinev Europe/Tallinn varuajavöönd (EET +02:00 / EEST +03:00)."""

    @staticmethod
    def _last_sunday(year: int, month: int) -> datetime:
        if month == 12:
            nxt = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            nxt = datetime(year, month + 1, 1, tzinfo=timezone.utc)
        day = nxt - timedelta(days=1)
        shift = (day.weekday() + 1) % 7
        return day - timedelta(days=shift)

    def _is_dst(self, dt: datetime) -> bool:
        naive = dt.replace(tzinfo=None)
        utc_approx = (naive - timedelta(hours=2)).replace(tzinfo=timezone.utc)
        start = self._last_sunday(utc_approx.year, 3).replace(hour=1, minute=0, second=0)
        end = self._last_sunday(utc_approx.year, 10).replace(hour=1, minute=0, second=0)
        return start <= utc_approx < end

    def utcoffset(self, dt: datetime | None) -> timedelta:
        return timedelta(hours=2) + self.dst(dt)

    def dst(self, dt: datetime | None) -> timedelta:
        if dt is not None and self._is_dst(dt):
            return timedelta(hours=1)
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        if dt is not None and self._is_dst(dt):
            return "EEST"
        return "EET"


def tallinn_tz() -> tzinfo:
    """Tagastab Europe/Tallinn ajavööndi objekti."""
    try:
        return ZoneInfo("Europe/Tallinn")
    except ZoneInfoNotFoundError:
        return _TallinnTZ()


def _resolve_named_tz(tz_name: str) -> tzinfo | None:
    """Proovib lahendada IANA ajavööndi nime (UTC ja Europe/Tallinn töötavad ka ilma tzdata-ta)."""
    if tz_name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        if tz_name == "Europe/Tallinn":
            return _TallinnTZ()
        return None


def _detect_posix_system_tz() -> tzinfo | None:
    """Loeb POSIX süsteemi IANA ajavööndi /etc/localtime või /etc/timezone kaudu."""
    try:
        localtime = Path("/etc/localtime")
        if localtime.is_symlink():
            resolved = str(localtime.resolve())
            marker = "/zoneinfo/"
            if marker in resolved:
                iana_name = resolved.split(marker, 1)[1].strip()
                if iana_name:
                    return ZoneInfo(iana_name)
    except (OSError, ZoneInfoNotFoundError, ValueError, KeyError):
        pass
    try:
        etc_tz = Path("/etc/timezone")
        if etc_tz.is_file():
            iana_name = etc_tz.read_text(encoding="utf-8", errors="replace").strip()
            if iana_name:
                return ZoneInfo(iana_name)
    except (OSError, ZoneInfoNotFoundError, ValueError, KeyError):
        pass
    return None


_POSIX_NAME_RE = re.compile(r"^(?:<([A-Za-z0-9+\-]{3,})>|([A-Za-z]{3,}))")
_POSIX_OFFSET_RE = re.compile(r"^([+-]?)(\d{1,3})(?::(\d{1,2})(?::(\d{1,2}))?)?")
_POSIX_DATE_M_RE = re.compile(r"^M(\d{1,2})\.(\d)\.(\d)$")
_POSIX_DATE_J_RE = re.compile(r"^J(\d{1,3})$")
_POSIX_DATE_N_RE = re.compile(r"^(\d{1,3})$")


class _TZState(NamedTuple):
    offset: int
    dst: int
    name: str


def _parse_posix_name(s: str, pos: int) -> tuple[str, int] | None:
    m = _POSIX_NAME_RE.match(s[pos:])
    if not m:
        return None
    name = m.group(1) if m.group(1) is not None else m.group(2)
    return name, pos + m.end()


def _parse_posix_hms(
    s: str,
    pos: int,
    *,
    max_hours: int,
    invert_sign: bool,
    strict_under_24h: bool,
) -> tuple[int, int] | None:
    m = _POSIX_OFFSET_RE.match(s[pos:])
    if not m:
        return None
    sign_str, h_str, m_str, sec_str = m.groups()
    hh = int(h_str)
    mm = int(m_str) if m_str is not None else 0
    ss = int(sec_str) if sec_str is not None else 0
    if hh > max_hours or mm > 59 or ss > 59:
        return None
    total = hh * 3600 + mm * 60 + ss
    if strict_under_24h and total >= 86400:
        return None
    raw_sign = -1 if sign_str == "-" else 1
    signed = (-raw_sign if invert_sign else raw_sign) * total
    return signed, pos + m.end()


def _parse_posix_transition(spec: str) -> tuple[tuple[str, int, int, int], int] | None:
    if not spec:
        return None
    if "/" in spec:
        date_part, time_part = spec.split("/", 1)
        if not time_part:
            return None
    else:
        date_part, time_part = spec, None

    m_m = _POSIX_DATE_M_RE.match(date_part)
    if m_m:
        month, week, weekday = int(m_m.group(1)), int(m_m.group(2)), int(m_m.group(3))
        if not (1 <= month <= 12 and 1 <= week <= 5 and 0 <= weekday <= 6):
            return None
        date_rule: tuple[str, int, int, int] = ("M", month, week, weekday)
    else:
        m_j = _POSIX_DATE_J_RE.match(date_part)
        if m_j:
            jday = int(m_j.group(1))
            if not (1 <= jday <= 365):
                return None
            date_rule = ("J", jday, 0, 0)
        else:
            m_n = _POSIX_DATE_N_RE.match(date_part)
            if not m_n:
                return None
            nday = int(m_n.group(1))
            if not (0 <= nday <= 365):
                return None
            date_rule = ("N", nday, 0, 0)

    if time_part is None:
        time_sec = 7200
    else:
        parsed_time = _parse_posix_hms(
            time_part,
            0,
            max_hours=167,
            invert_sign=False,
            strict_under_24h=False,
        )
        if parsed_time is None or parsed_time[1] != len(time_part):
            return None
        time_sec = parsed_time[0]
    return date_rule, time_sec


class _PosixRuleTZ(tzinfo):
    """Puhta Pythoni POSIX TZ-reegli ajavöönd (PEP 495; ei muuda protsessi globaalset TZ-d)."""

    def __init__(
        self,
        tz_rule: str,
        std_name: str,
        std_offset: int,
        dst_name: str | None = None,
        dst_offset: int | None = None,
        start_rule: tuple[tuple[str, int, int, int], int] | None = None,
        end_rule: tuple[tuple[str, int, int, int], int] | None = None,
    ) -> None:
        self._tz_rule = tz_rule
        self._std = _TZState(offset=std_offset, dst=0, name=std_name)
        if dst_name is not None and dst_offset is not None and start_rule is not None and end_rule is not None:
            self._dst: _TZState | None = _TZState(
                offset=dst_offset,
                dst=dst_offset - std_offset,
                name=dst_name,
            )
            self._start_rule: tuple[tuple[str, int, int, int], int] | None = start_rule
            self._end_rule: tuple[tuple[str, int, int, int], int] | None = end_rule
        else:
            self._dst = None
            self._start_rule = None
            self._end_rule = None

    def __repr__(self) -> str:
        return f"_PosixRuleTZ({self._tz_rule!r})"

    @staticmethod
    def _eval_transition_sec(
        rule: tuple[tuple[str, int, int, int], int],
        year: int,
        active_offset: int,
    ) -> int:
        """Arvutab ülemineku sekundi aasta 1. jaanuari 00:00:00 UTC suhtes (ilma ületäitumiseta)."""
        date_spec, time_sec = rule
        kind, a, b, c = date_spec
        jan1 = date(year, 1, 1)
        if kind == "M":
            first = date(year, a, 1)
            py_wday = (c - 1) % 7
            delta = (py_wday - first.weekday()) % 7
            day = 1 + delta + (b - 1) * 7
            if b == 5:
                last_day = 31 if a == 12 else (date(year, a + 1, 1) - timedelta(days=1)).day
                if day > last_day:
                    day -= 7
            day_offset = (date(year, a, day) - jan1).days
        elif kind == "J":
            ref = date(2001, 1, 1) + timedelta(days=a - 1)
            day_offset = (date(year, ref.month, ref.day) - jan1).days
        else:
            day_offset = a
        return day_offset * 86400 + time_sec - active_offset

    @staticmethod
    def _shift_naive(dt_naive: datetime, delta_sec: int) -> datetime | None:
        try:
            return dt_naive + timedelta(seconds=delta_sec)
        except OverflowError:
            return None

    def _state_at_utc(self, u_naive: datetime) -> _TZState:
        """Ühtne UTC-olekufunktsioon (sama allikas nii fromutc() kui kohaliku aja päringule)."""
        if self._dst is None:
            return self._std
        assert self._start_rule is not None and self._end_rule is not None
        year = u_naive.year
        s_us = self._eval_transition_sec(self._start_rule, year, self._std.offset) * 1_000_000
        e_us = self._eval_transition_sec(self._end_rule, year, self._dst.offset) * 1_000_000
        delta = u_naive - datetime(year, 1, 1)
        u_us = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
        if s_us <= e_us:
            return self._dst if (s_us <= u_us < e_us) else self._std
        return self._dst if (u_us >= s_us or u_us < e_us) else self._std

    def _state_for_local(self, dt: datetime) -> _TZState:
        if self._dst is None:
            return self._std
        local = dt.replace(tzinfo=None)
        if self._std.offset == self._dst.offset:
            u_cand = self._shift_naive(local, -self._std.offset)
            return self._std if u_cand is None else self._state_at_utc(u_cand)

        u_std = self._shift_naive(local, -self._std.offset)
        u_dst = self._shift_naive(local, -self._dst.offset)
        valid_std = u_std is not None and self._state_at_utc(u_std) == self._std
        valid_dst = u_dst is not None and self._state_at_utc(u_dst) == self._dst

        if valid_std and not valid_dst:
            return self._std
        if valid_dst and not valid_std:
            return self._dst
        if valid_std and valid_dst:
            assert u_std is not None and u_dst is not None
            # Korduv kohalik aeg (PEP 495 fold): fold=0 on varasem UTC-hetk, fold=1 hilisem.
            if u_std < u_dst:
                return self._std if dt.fold == 0 else self._dst
            return self._dst if dt.fold == 0 else self._std

        # Olematu kohalik aeg (PEP 495 gap): fold=0 kasutab eelnevat, fold=1 järgnevat olekut.
        if u_std is not None and u_dst is not None:
            u_early, u_late = (u_std, u_dst) if u_std < u_dst else (u_dst, u_std)
            return self._state_at_utc(u_early) if dt.fold == 0 else self._state_at_utc(u_late)
        if u_std is not None:
            return self._state_at_utc(u_std)
        if u_dst is not None:
            return self._state_at_utc(u_dst)
        return self._std

    def fromutc(self, dt: datetime) -> datetime:
        if not isinstance(dt, datetime):
            raise TypeError("fromutc() requires a datetime argument")
        if dt.tzinfo is not self:
            raise ValueError("fromutc: dt.tzinfo is not self")
        u_naive = dt.replace(tzinfo=None)
        if self._dst is None:
            local = u_naive + timedelta(seconds=self._std.offset)
            return local.replace(tzinfo=self, fold=0)

        state = self._state_at_utc(u_naive)
        local = u_naive + timedelta(seconds=state.offset)
        fold = 0
        if self._std.offset != self._dst.offset:
            other = self._dst if state == self._std else self._std
            u_other = self._shift_naive(local, -other.offset)
            if u_other is not None and u_other < u_naive and self._state_at_utc(u_other) == other:
                fold = 1
        return local.replace(tzinfo=self, fold=fold)

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        if dt is None:
            return None
        return timedelta(seconds=self._state_for_local(dt).offset)

    def dst(self, dt: datetime | None) -> timedelta | None:
        if dt is None:
            return None
        return timedelta(seconds=self._state_for_local(dt).dst)

    def tzname(self, dt: datetime | None) -> str | None:
        if dt is None:
            return None
        return self._state_for_local(dt).name


def _parse_posix_tz(tz_rule: str) -> _PosixRuleTZ | None:
    """Parsib POSIX TZ-reegli (std offset [dst [offset],start[/time],end[/time]])."""
    if not tz_rule or tz_rule.startswith(":") or any(ch.isspace() for ch in tz_rule):
        return None
    parsed_std_name = _parse_posix_name(tz_rule, 0)
    if parsed_std_name is None:
        return None
    std_name, pos = parsed_std_name

    parsed_std_off = _parse_posix_hms(
        tz_rule,
        pos,
        max_hours=23,
        invert_sign=True,
        strict_under_24h=True,
    )
    if parsed_std_off is None:
        return None
    std_off, pos = parsed_std_off

    if pos == len(tz_rule):
        return _PosixRuleTZ(tz_rule, std_name, std_off)

    parsed_dst_name = _parse_posix_name(tz_rule, pos)
    if parsed_dst_name is None:
        return None
    dst_name, pos = parsed_dst_name

    if pos == len(tz_rule):
        # DST ilma üleminekureegliteta on süsteemispetsiifiline -> ära arva.
        return None

    if tz_rule[pos] == ",":
        dst_off = std_off + 3600
        if abs(dst_off) >= 86400:
            return None
    else:
        parsed_dst_off = _parse_posix_hms(
            tz_rule,
            pos,
            max_hours=23,
            invert_sign=True,
            strict_under_24h=True,
        )
        if parsed_dst_off is None:
            return None
        dst_off, pos = parsed_dst_off
        if pos >= len(tz_rule) or tz_rule[pos] != ",":
            return None

    rule_parts = tz_rule[pos + 1 :].split(",")
    if len(rule_parts) != 2:
        return None
    start_rule = _parse_posix_transition(rule_parts[0])
    end_rule = _parse_posix_transition(rule_parts[1])
    if start_rule is None or end_rule is None:
        return None

    return _PosixRuleTZ(
        tz_rule,
        std_name,
        std_off,
        dst_name,
        dst_off,
        start_rule,
        end_rule,
    )


def resolve_tz() -> tzinfo:
    """Tagastab ajavööndi: KVOOT_TZ / config -> TZ -> süsteemi ajavöönd (ilma vaikse vale nihketa)."""
    tz_name = _get_setting(("KVOOT_TZ",), "timezone")
    if tz_name:
        resolved = _resolve_named_tz(tz_name)
        if resolved is not None:
            return resolved
        _record_note("timezone_unavailable", {"tz": tz_name})
        return timezone.utc

    env_tz = os.environ.get("TZ", "").strip()
    if env_tz:
        has_colon = env_tz.startswith(":")
        clean_tz = env_tz.lstrip(":")
        if clean_tz:
            resolved = _resolve_named_tz(clean_tz)
            if resolved is not None:
                return resolved
            if not _is_windows() and clean_tz == "/etc/localtime":
                posix_tz = _detect_posix_system_tz()
                if posix_tz is not None:
                    return posix_tz
            if not has_colon:
                posix_rule = _parse_posix_tz(clean_tz)
                if posix_rule is not None:
                    return posix_rule
            _record_note("timezone_unavailable", {"tz": env_tz})
            return timezone.utc

    if not _is_windows():
        posix_tz = _detect_posix_system_tz()
        if posix_tz is not None:
            return posix_tz
    else:
        _record_note("timezone_windows_fixed", {})

    local_tz = datetime.now().astimezone().tzinfo
    return local_tz or timezone.utc


def parse_iso_timestamp(ts_str: str | None) -> datetime | None:
    """Parsib ISO 8601 ajatempli UTC-teadlikuks datetime objektiks."""
    if not isinstance(ts_str, str) or not ts_str.strip():
        return None
    val = ts_str.strip()
    if val.endswith("Z"):
        val = val[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(val)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def write_atomic_json(target: Path, data: dict[str, Any], prefix: str = ".kvoot_tmp_") -> None:
    """Kirjutab JSON-faili atomaarselt (ajutine fail samas kaustas + os.replace)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=prefix,
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, target)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
