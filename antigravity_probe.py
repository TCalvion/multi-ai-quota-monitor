#!/usr/bin/env python3
"""Antigravity kohaliku keeleserveri kvoodimõõdik (v4).

Loeb töötava Antigravity keeleserveri protsessi käsurealt `--csrf_token`
väärtuse AINULT mällu ja küsib kvooti AINULT aadressilt 127.0.0.1.
Tokenit ei kirjutata faili, logisse, raportisse, testi ega veateatesse.
Keelatud on Google'i sisselogimise varutee, Keychain/Credential Manager,
`agy` sisselogimine ja mis tahes võrgupäring väljapoole 127.0.0.1.

Osaliselt porditud ja kohandatud ClaudeBarist (Apache-2.0, vt NOTICE ja
LICENSES/Apache-2.0.txt): https://github.com/tddworks/ClaudeBar
(`docs/providers/antigravity`, `antigravity.json` / `antigravity-usage.js`
endpointide nimed `RetrieveUserQuotaSummary`, `GetUserStatus`,
`GetCommandModelConfigs` ja `bucketId` vastendused).
"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any

try:
    import kvoot_config as _kvoot_config
    import kvoot_i18n as _kvoot_i18n
except ImportError:
    _kvoot_config = None  # type: ignore[assignment]
    _kvoot_i18n = None  # type: ignore[assignment]

_FALLBACK_TEXTS = {
    "ag_reason_not_running": "Antigravity language server is not running (process not found)",
    "ag_reason_no_ports": "No listening ports found for Antigravity language server",
    "ag_reason_no_response": "Antigravity language server did not respond",
    "ag_reason_query_failed": "Antigravity query failed: {err}",
    "ag_reason_wsl_failed": "Antigravity WSL query failed: {err}",
    "ag_reason_wsl_bad_response": "WSL Antigravity query returned an invalid response",
    "ag_reason_wsl_no_data": "Antigravity language server returned no quota data",
    "ag_reason_parse_error": "Failed to parse Antigravity response: {err}",
    "ag_reason_no_quota_data": "No quota data found in Antigravity language server response",
    "ag_note_live": "local language server",
    "ag_note_live_ep": "local language server ({endpoint})",
    "ag_note_pools": "pools: {pools}",
    "ag_note_last_known": "language server is not running, showing last known reading",
    "ag_note_cache_write_failed": "could not save last reading ({err_type})",
}


def _msg(key: str, **kwargs: Any) -> str:
    if _kvoot_i18n is not None:
        return _kvoot_i18n.t(key, **kwargs)
    tmpl = _FALLBACK_TEXTS.get(key, key)
    return tmpl.format(**kwargs) if kwargs else tmpl


def _render_parts(parts: list[dict[str, Any]]) -> str:
    if _kvoot_i18n is not None:
        return _kvoot_i18n.render_parts(parts)
    rendered: list[str] = []
    for p in parts:
        key = p.get("key")
        if key:
            rendered.append(_msg(str(key), **(p.get("args") or {})))
        elif p.get("text"):
            rendered.append(str(p["text"]))
    return "; ".join(rendered)

ENDPOINTS = (
    "/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary",
    "/exa.language_server_pb.LanguageServerService/GetUserStatus",
    "/exa.language_server_pb.LanguageServerService/GetCommandModelConfigs",
)
REQUEST_BODY = json.dumps(
    {
        "metadata": {
            "ideName": "antigravity",
            "extensionName": "antigravity",
            "ideVersion": "unknown",
            "locale": "en",
        }
    }
).encode("utf-8")


@dataclass(frozen=True)
class AntigravityProcess:
    pid: int
    csrf_token: str
    extension_port: int | None = None
    koht: str = "kohalik"


def mask_token(text: str, token: str | None = None) -> str:
    """Asendab tokeni kõik esinemised tekstis sümboliga '***'."""
    out = str(text)
    if token:
        out = out.replace(token, "***")
    out = re.sub(r"(--csrf_token(?:=|\s+))([^\s\"']+)", r"\1***", out)
    out = re.sub(r"(--extension_server_csrf_token(?:=|\s+))([^\s\"']+)", r"\1***", out)
    out = re.sub(r"(X-Codeium-Csrf-Token['\":\s]+)([^\s\"',}]+)", r"\1***", out, flags=re.IGNORECASE)
    return out


def _split_cmdline(cmdline: str | Sequence[str]) -> list[str]:
    if isinstance(cmdline, str):
        try:
            return shlex.split(cmdline, posix=(os.name != "nt"))
        except ValueError:
            return cmdline.split()
    return [str(a) for a in cmdline]


def parse_antigravity_cmdline(cmdline: str | Sequence[str]) -> tuple[str, int | None] | None:
    """Kontrollib, kas käsurida kuulub Antigravity keeleserverile ja eraldab (csrf_token, extension_port)."""
    args = _split_cmdline(cmdline)
    if not args:
        return None
    joined = " ".join(args)
    low = joined.lower().replace("\\", "/")

    is_ls = "language_server" in low or re.search(r"(^|/|\s)agy(\s|$)", low) is not None
    is_anti = (
        "antigravity" in low
        or "/.antigravity" in low
        or "/antigravity/" in low
        or re.search(r"(^|/|\s)agy(\s|$)", low) is not None
    )
    if not (is_ls and is_anti):
        return None

    token: str | None = None
    ext_port: int | None = None
    for i, arg in enumerate(args):
        if arg == "--csrf_token" and i + 1 < len(args):
            token = args[i + 1].strip()
        elif arg.startswith("--csrf_token="):
            token = arg.split("=", 1)[1].strip()
        elif arg == "--extension_server_port" and i + 1 < len(args):
            try:
                ext_port = int(args[i + 1])
            except ValueError:
                pass
        elif arg.startswith("--extension_server_port="):
            try:
                ext_port = int(arg.split("=", 1)[1])
            except ValueError:
                pass

    if not token:
        return None
    return token, ext_port


def find_antigravity_process_linux(proc_dir: Path | str = "/proc") -> AntigravityProcess | None:
    """Leiab Linux/WSL /proc/*/cmdline alt töötava Antigravity keeleserveri."""
    root = Path(proc_dir)
    if not root.exists():
        return None
    candidates: list[AntigravityProcess] = []
    for entry in root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        args = [a.decode("utf-8", errors="replace") for a in raw.split(b"\x00") if a]
        parsed = parse_antigravity_cmdline(args)
        if parsed is not None:
            token, ext_port = parsed
            candidates.append(
                AntigravityProcess(
                    pid=int(entry.name),
                    csrf_token=token,
                    extension_port=ext_port,
                    koht="wsl",
                )
            )
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.pid)
    return candidates[0]


def find_listening_ports_linux(pid: int, proc_dir: Path | str = "/proc") -> list[int]:
    """Leiab protsessi kuulatavad TCP-pordid /proc/<pid>/fd ja /proc/net/tcp kaudu."""
    root = Path(proc_dir)
    inodes: set[str] = set()
    fd_dir = root / str(pid) / "fd"
    try:
        for fd in fd_dir.iterdir():
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith("socket:[") and target.endswith("]"):
                inodes.add(target[8:-1])
    except OSError:
        return []

    if not inodes:
        return []

    ports: list[int] = []
    for tcp_name in ("net/tcp", "net/tcp6"):
        tcp_file = root / tcp_name
        try:
            lines = tcp_file.read_text(encoding="utf-8", errors="replace").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 10:
                continue
            state = parts[3]
            inode = parts[9]
            if state == "0A" and inode in inodes:
                try:
                    port = int(parts[1].split(":")[1], 16)
                except (IndexError, ValueError):
                    continue
                if port not in ports:
                    ports.append(port)
    return ports


def find_antigravity_process_windows() -> AntigravityProcess | None:
    """Leiab Windowsist töötava Antigravity keeleserveri protsessi (ainult mällu)."""
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    ps_cmd = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -and ($_.CommandLine -match 'csrf_token') -and "
        "($_.Name -match 'language_server|agy' -or $_.CommandLine -match 'language_server|antigravity') } | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-InputFormat", "None", "-Command", ps_cmd],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=6,
            check=False,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    stdout_text = (proc.stdout or "").strip()
    if proc.returncode != 0 or not stdout_text:
        return None

    try:
        raw = json.loads(stdout_text)
    except json.JSONDecodeError:
        return None

    rows = raw if isinstance(raw, list) else [raw]
    candidates: list[AntigravityProcess] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cmdline = row.get("CommandLine") or ""
        pid = row.get("ProcessId")
        if not isinstance(pid, int):
            continue
        parsed = parse_antigravity_cmdline(cmdline)
        if parsed is not None:
            token, ext_port = parsed
            candidates.append(
                AntigravityProcess(
                    pid=pid,
                    csrf_token=token,
                    extension_port=ext_port,
                    koht="windows",
                )
            )
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.pid)
    return candidates[0]


def find_listening_ports_windows(pid: int) -> list[int]:
    """Leiab Windowsis protsessi kuulatavad TCP-pordid netstat -ano abil."""
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    ports: list[int] = []
    pid_str = str(pid)
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING" and parts[4] == pid_str:
            local_addr = parts[1]
            if ":" in local_addr:
                try:
                    port = int(local_addr.rsplit(":", 1)[1])
                except ValueError:
                    continue
                if port not in ports:
                    ports.append(port)
    return ports


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keelab HTTP-suunamised (3xx), et token ei lahkuks aadressilt 127.0.0.1."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            f"HTTP redirect forbidden ({code})",
            headers,
            fp,
        )


def _localhost_opener(scheme: str) -> urllib.request.OpenerDirector:
    """Loob openeri ainult 127.0.0.1 jaoks: ilma proxyta, ilma suunamisteta, CERT_NONE ainult HTTPS-il."""
    if scheme not in ("http", "https"):
        raise ValueError("Only http/https allowed")
    handlers: list[Any] = [
        urllib.request.ProxyHandler({}),
        _NoRedirectHandler(),
    ]
    if scheme == "https":
        # Kohalik keeleserver kasutab iseallkirjastatud sertifikaati ainult 127.0.0.1 peal
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def _post_localhost(
    scheme: str,
    port: int,
    endpoint: str,
    token: str,
    timeout_s: float = 3.0,
) -> dict[str, Any] | None:
    """Saadab POST päringu AINULT aadressile 127.0.0.1."""
    if scheme not in ("https", "http"):
        raise ValueError("Only http/https allowed")
    url = f"{scheme}://127.0.0.1:{int(port)}{endpoint}"
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or parsed.hostname != "127.0.0.1":
        raise ValueError("Only 127.0.0.1 allowed")

    headers = {
        "Content-Type": "application/json",
        "X-Codeium-Csrf-Token": token,
        "Connect-Protocol-Version": "1",
    }
    opener = _localhost_opener(scheme)
    req = urllib.request.Request(url, data=REQUEST_BODY, headers=headers, method="POST")
    try:
        with opener.open(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            return data if isinstance(data, dict) else None
    except urllib.error.HTTPError as exc:
        raise urllib.error.HTTPError(
            url,
            exc.code,
            mask_token(str(exc.reason or exc.msg), token),
            exc.headers,
            None,
        ) from None
    except Exception as exc:
        raise RuntimeError(mask_token(f"{type(exc).__name__}: {exc}", token)) from None


def _has_quota_payload(data: dict[str, Any]) -> bool:
    resp = data.get("response")
    if isinstance(resp, dict) and isinstance(resp.get("groups"), list):
        return True
    if isinstance(data.get("groups"), list):
        return True
    us = data.get("userStatus")
    if isinstance(us, dict) and isinstance(us.get("cascadeModelConfigData"), dict):
        return True
    if isinstance(data.get("clientModelConfigs"), list):
        return True
    return False


def _query_process(proc: AntigravityProcess, ports: Sequence[int]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Küsib keeleserverilt kvooti 127.0.0.1 kaudu. Tagastab (json_data, endpoint, error)."""
    last_err: str | None = None
    for port in ports:
        for endpoint in ENDPOINTS:
            try:
                data = _post_localhost("https", port, endpoint, proc.csrf_token)
                if data is not None and _has_quota_payload(data):
                    return data, endpoint, None
            except urllib.error.HTTPError as exc:
                last_err = mask_token(f"HTTP {exc.code} ({endpoint})", proc.csrf_token)
            except Exception as exc:
                last_err = mask_token(f"{type(exc).__name__}: {exc}", proc.csrf_token)

    if proc.extension_port:
        for endpoint in ENDPOINTS:
            try:
                data = _post_localhost("http", proc.extension_port, endpoint, proc.csrf_token)
                if data is not None and _has_quota_payload(data):
                    return data, endpoint, None
            except Exception as exc:
                last_err = mask_token(f"{type(exc).__name__}: {exc}", proc.csrf_token)

    return None, None, last_err or _msg("ag_reason_no_response")


def _mask_part(part: dict[str, Any], token: str | None = None) -> dict[str, Any]:
    out = dict(part)
    args = out.get("args")
    if isinstance(args, dict):
        out["args"] = {
            str(k): (mask_token(v, token) if isinstance(v, str) else v)
            for k, v in args.items()
        }
    return out


def _query_wsl_from_windows() -> tuple[dict[str, Any] | None, str | None, str | None, dict[str, Any] | str | None]:
    """Küsib Windowsist WSL-is töötavat keeleserverit; token jääb ainult WSL protsessi mällu."""
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    script_src = Path(__file__).read_text(encoding="utf-8")
    try:
        proc = subprocess.run(
            ["wsl.exe", "-e", "python3", "-", "--probe-raw"],
            input=script_src,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=8,
            check=False,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, None, "server_not_running", {"key": "ag_reason_wsl_failed", "args": {"err": mask_token(str(exc))}}

    if not proc.stdout.strip():
        return None, None, "server_not_running", {"key": "ag_reason_not_running"}

    try:
        res = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return None, None, "server_no_response", {"key": "ag_reason_wsl_bad_response"}

    if not isinstance(res, dict) or not res.get("ok"):
        rcode = str(res.get("reason_code") or "server_not_running") if isinstance(res, dict) else "server_not_running"
        if isinstance(res, dict) and isinstance(res.get("reason_key"), str) and res.get("reason_key"):
            part: dict[str, Any] = {"key": str(res["reason_key"])}
            if isinstance(res.get("reason_args"), dict) and res["reason_args"]:
                part["args"] = dict(res["reason_args"])
            return None, None, rcode, _mask_part(part)
        code_to_key = {
            "server_not_running": "ag_reason_not_running",
            "no_listening_ports": "ag_reason_no_ports",
            "server_no_response": "ag_reason_no_response",
            "no_quota_data": "ag_reason_wsl_no_data",
        }
        if rcode in code_to_key:
            return None, None, rcode, {"key": code_to_key[rcode]}
        if rcode == "parse_error":
            raw_err = mask_token(str(res.get("reason") or "")) if isinstance(res, dict) else ""
            return None, None, rcode, {"key": "ag_reason_parse_error", "args": {"err": raw_err}}
        if isinstance(res, dict) and res.get("reason"):
            return None, None, rcode, {"key": "ag_reason_wsl_failed", "args": {"err": mask_token(str(res["reason"]))}}
        return None, None, rcode, {"key": "ag_reason_not_running"}

    payload = res.get("payload")
    endpoint = res.get("endpoint")
    if isinstance(payload, dict):
        # Eemaldame isikuandmed igaks juhuks
        us = payload.get("userStatus")
        if isinstance(us, dict):
            us.pop("email", None)
            us.pop("name", None)
        return payload, str(endpoint) if endpoint else None, None, None
    return None, None, "no_quota_data", {"key": "ag_reason_wsl_no_data"}


def default_cache_path(for_write: bool = True) -> Path:
    """Vaikimisi tee Antigravity viimase teadaoleva näidu vahemälufailile."""
    assert _kvoot_config is not None
    return _kvoot_config.resolve_state_dir(for_write=for_write) / "kvoot_antigravity_last.json"


def _legacy_cache_candidates(primary_path: Path) -> list[Path]:
    """Tagastab vahemälufaili kandidaadid uue ja legacy state dir koha jaoks."""
    assert _kvoot_config is not None
    candidates = [primary_path]
    if primary_path != default_cache_path(for_write=True):
        return candidates
    candidates.append(primary_path.parent / "kvoot_antigravity_viimane.json")
    has_state_override = bool(
        os.environ.get("KVOOT_STATE_DIR", "").strip()
        or _kvoot_config.load_config().get("state_dir")
    )
    if not has_state_override:
        old_dir = _kvoot_config.resolve_home_dir() / ".tc-kvoot"  # legacy state dir
        candidates.append(old_dir / "kvoot_antigravity_last.json")
        candidates.append(old_dir / "kvoot_antigravity_viimane.json")
    return candidates


def _format_window(
    remaining_fraction: Any,
    reset_time: Any,
    now_utc: datetime,
    tz: tzinfo,
    missing_is_full: bool = False,
) -> dict[str, Any] | None:
    if remaining_fraction is None or isinstance(remaining_fraction, bool):
        if not missing_is_full:
            return None
        rem = 0.0
    else:
        try:
            rem = float(remaining_fraction)
        except (TypeError, ValueError):
            return None
        if math.isnan(rem) or math.isinf(rem):
            return None

    used = round(max(0.0, min(100.0, (1.0 - rem) * 100.0)), 2)

    assert _kvoot_config is not None
    dt_r = _kvoot_config.parse_iso_timestamp(str(reset_time)) if reset_time else None
    if dt_r is None:
        return {
            "used": used,
            "resets_at": None,
            "reset": False,
        }
    is_reset = now_utc >= dt_r
    return {
        "used": 0.0 if is_reset else used,
        "resets_at": dt_r.astimezone(tz).isoformat(timespec="seconds"),
        "reset": is_reset,
    }


def _slug_pool_key(bucket_id: str, group_name: str) -> str:
    low_id = bucket_id.lower()
    low_grp = group_name.lower()
    if low_id.startswith("gemini-argon") or "argon" in low_id or "argon" in low_grp:
        return "argon"
    if low_id.startswith("gemini-") or "gemini" in low_grp:
        return "gemini"
    if low_id.startswith("3p-") or "claude" in low_grp:
        return "claude_gpt"
    slug = re.sub(r"[^a-z0-9]+", "_", low_grp).strip("_")
    return slug or "other"


def _window_slot(bucket_id: str, window_name: str) -> str:
    low_id = bucket_id.lower()
    low_w = window_name.lower()
    if "5h" in low_id or low_w == "5h" or "session" in low_w:
        return "window_5h"
    return "window_weekly"


def _summarize_pools(
    pools: dict[str, dict[str, Any]],
    is_stale: bool = False,
    worker_limit: float | None = None,
    lead_limit: float | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, float]:
    """Arvutab kogumite ja kogu Antigravity koondmax-i ning 5h/nädala tipud."""
    assert _kvoot_config is not None
    all_5h: list[dict[str, Any]] = []
    all_wk: list[dict[str, Any]] = []
    for pool in pools.values():
        vals: list[float] = []
        if isinstance(pool.get("window_5h"), dict):
            all_5h.append(pool["window_5h"])
            vals.append(float(pool["window_5h"]["used"]))
        if isinstance(pool.get("window_weekly"), dict):
            all_wk.append(pool["window_weekly"])
            vals.append(float(pool["window_weekly"]["used"]))
        pool_max = max(vals) if vals else 0.0
        pool["max"] = pool_max
        pool["status"] = (
            "stale"
            if is_stale
            else _kvoot_config.compute_status(pool_max, worker_limit, lead_limit)
        )

    win_5h = max(all_5h, key=lambda a: float(a["used"])) if all_5h else None
    win_wk = max(all_wk, key=lambda a: float(a["used"])) if all_wk else None
    max_val = max(float(p["max"]) for p in pools.values())
    return win_5h, win_wk, max_val


def _sanitize_cached_window(raw: Any) -> dict[str, Any] | None:
    """Eraldab vahemälufaili jaoks ainult akna protsendi ja lähtestumisaja (toetab v3 ja v4 võtmeid)."""
    if not isinstance(raw, dict):
        return None
    used = raw.get("used") if "used" in raw else raw.get("kasutatud")
    if used is None or isinstance(used, bool):
        return None
    try:
        used_val = round(max(0.0, min(100.0, float(used))), 2)
    except (TypeError, ValueError):
        return None
    if math.isnan(used_val) or math.isinf(used_val):
        return None
    resets_at = raw.get("resets_at") if "resets_at" in raw else raw.get("lahtestub")
    is_reset = raw.get("reset") if "reset" in raw else raw.get("lahtestunud", False)
    return {
        "used": used_val,
        "resets_at": resets_at,
        "reset": bool(is_reset),
    }


def _refresh_cached_window(raw: Any, now_utc: datetime, tz: tzinfo) -> dict[str, Any] | None:
    """Valideerib ja värskendab vahemällu salvestatud akna lähtestumisstaatust."""
    assert _kvoot_config is not None
    win = _sanitize_cached_window(raw)
    if win is None:
        return None
    resets_raw = win.get("resets_at")
    if isinstance(resets_raw, str) and resets_raw.strip():
        dt_r = _kvoot_config.parse_iso_timestamp(resets_raw)
        if dt_r is not None:
            win["resets_at"] = dt_r.astimezone(tz).isoformat(timespec="seconds")
            is_reset = now_utc >= dt_r
            win["reset"] = is_reset
            if is_reset:
                win["used"] = 0.0
        else:
            win["resets_at"] = resets_raw.strip()
    else:
        win["resets_at"] = None
    return win


def save_antigravity_cache(
    parsed: dict[str, Any],
    now_utc: datetime,
    cache_path: Path,
) -> None:
    """Salvestab kogumid koos ajaga atomaarselt v4 skeemis (ilma tokeni ja portideta)."""
    assert _kvoot_config is not None
    pools_raw = parsed.get("pools")
    if not isinstance(pools_raw, dict) or not pools_raw:
        return
    pools_out: dict[str, dict[str, Any]] = {}
    for slug, pool in pools_raw.items():
        if not isinstance(pool, dict):
            continue
        pools_out[str(slug)] = {
            "name": str(pool.get("name") or slug),
            "window_5h": _sanitize_cached_window(pool.get("window_5h")),
            "window_weekly": _sanitize_cached_window(pool.get("window_weekly")),
        }
    if not pools_out:
        return
    payload = {
        "written_at": now_utc.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "pools": pools_out,
    }
    _kvoot_config.write_atomic_json(cache_path, payload, prefix=".kvoot_tmp_")


def load_antigravity_cache(
    cache_path: Path,
    now_utc: datetime,
    tz: tzinfo,
    stale_min: int | None = None,
    worker_limit: float | None = None,
    lead_limit: float | None = None,
) -> dict[str, Any] | None:
    """Loeb viimase teadaoleva Antigravity näidu failist (toetab v3/v4 ja legacy state dir üleminekut)."""
    assert _kvoot_config is not None
    target_path: Path | None = None
    for cand in _legacy_cache_candidates(cache_path):
        if cand.exists() and cand.is_file():
            target_path = cand
            break
    if target_path is None:
        return None

    try:
        raw = json.loads(target_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None

    pools_raw = raw.get("pools") if isinstance(raw.get("pools"), dict) else raw.get("kogumid")
    if not isinstance(pools_raw, dict) or not pools_raw:
        return None

    rebuilt: dict[str, dict[str, Any]] = {}
    for slug, pool in pools_raw.items():
        if not isinstance(pool, dict):
            continue
        raw_5h = pool.get("window_5h") if "window_5h" in pool else pool.get("aken_5h")
        raw_wk = pool.get("window_weekly") if "window_weekly" in pool else pool.get("aken_nadal")
        w_5h = _refresh_cached_window(raw_5h, now_utc, tz)
        w_wk = _refresh_cached_window(raw_wk, now_utc, tz)
        if w_5h is None and w_wk is None:
            continue
        name = pool.get("name") or pool.get("nimi") or slug
        rebuilt[str(slug)] = {
            "name": str(name),
            "window_5h": w_5h,
            "window_weekly": w_wk,
            "max": 0.0,
            "status": "green",
        }
    if not rebuilt:
        return None

    ts_raw = raw.get("written_at") or raw.get("kirjutatud") or ""
    ts_dt = _kvoot_config.parse_iso_timestamp(str(ts_raw))
    if ts_dt is None:
        try:
            ts_dt = datetime.fromtimestamp(target_path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            return None

    age_min = max(0, int((now_utc - ts_dt).total_seconds() // 60))
    stale_limit = _kvoot_config.get_stale_minutes() if stale_min is None else stale_min
    is_stale = age_min > stale_limit
    win_5h, win_wk, max_val = _summarize_pools(
        rebuilt,
        is_stale=is_stale,
        worker_limit=worker_limit,
        lead_limit=lead_limit,
    )
    status = (
        "stale"
        if is_stale
        else _kvoot_config.compute_status(max_val, worker_limit, lead_limit)
    )

    note_parts = [{"key": "ag_note_last_known"}]
    return {
        "available": True,
        "source_time": ts_dt.astimezone(tz).isoformat(timespec="seconds"),
        "source_location": "last_known",
        "age_min": age_min,
        "window_5h": win_5h,
        "window_weekly": win_wk,
        "pools": rebuilt,
        "max": max_val,
        "status": status,
        "note_code": "last_known_cache",
        "note": _render_parts(note_parts),
        "_note_parts": note_parts,
    }


def parse_antigravity_response(
    data: dict[str, Any],
    now_utc: datetime,
    tz: tzinfo,
    endpoint: str | None = None,
    worker_limit: float | None = None,
    lead_limit: float | None = None,
) -> dict[str, Any] | None:
    """Teisendab Antigravity keeleserveri vastuse standardsele kvoot/4 kujule."""
    if not isinstance(data, dict):
        return None
    assert _kvoot_config is not None

    pools: dict[str, dict[str, Any]] = {}

    # 1) RetrieveUserQuotaSummary: response.groups või groups
    resp = data.get("response") if isinstance(data.get("response"), dict) else data
    groups = resp.get("groups") if isinstance(resp, dict) else None
    if isinstance(groups, list):
        for grp in groups:
            if not isinstance(grp, dict):
                continue
            grp_name = str(grp.get("displayName") or "Other")
            buckets = grp.get("buckets")
            if not isinstance(buckets, list):
                continue
            for b in buckets:
                if not isinstance(b, dict):
                    continue
                bid = str(b.get("bucketId") or b.get("id") or "")
                w_kind = str(b.get("window") or "")
                rem = b.get("remainingFraction")
                if rem is None:
                    rem = b.get("remaining_fraction")
                r_time = b.get("resetTime") or b.get("reset_time")
                win = _format_window(rem, r_time, now_utc, tz, missing_is_full=False)
                if win is None:
                    continue
                pool_key = _slug_pool_key(bid, grp_name)
                slot = _window_slot(bid, w_kind)
                pool = pools.setdefault(
                    pool_key,
                    {
                        "name": grp_name,
                        "window_5h": None,
                        "window_weekly": None,
                        "max": 0.0,
                        "status": "green",
                    },
                )
                pool[slot] = win

    # 2) Legacy fallback: GetUserStatus / GetCommandModelConfigs
    if not pools:
        us = data.get("userStatus") if isinstance(data.get("userStatus"), dict) else None
        cm = us.get("cascadeModelConfigData") if us and isinstance(us.get("cascadeModelConfigData"), dict) else None
        configs = cm.get("clientModelConfigs") if cm else data.get("clientModelConfigs")
        if isinstance(configs, list):
            for idx, cfg in enumerate(configs):
                if not isinstance(cfg, dict):
                    continue
                label = str(cfg.get("label") or f"model_{idx}").strip()
                qinfo = cfg.get("quotaInfo")
                if not isinstance(qinfo, dict):
                    continue
                win = _format_window(
                    qinfo.get("remainingFraction"),
                    qinfo.get("resetTime"),
                    now_utc,
                    tz,
                    missing_is_full=True,
                )
                if win is None:
                    continue
                slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or f"model_{idx}"
                pools[slug] = {
                    "name": label,
                    "window_5h": win,
                    "window_weekly": None,
                    "max": win["used"],
                    "status": _kvoot_config.compute_status(win["used"], worker_limit, lead_limit),
                }

    if not pools:
        return None

    win_5h, win_wk, max_val = _summarize_pools(
        pools,
        is_stale=False,
        worker_limit=worker_limit,
        lead_limit=lead_limit,
    )
    status = _kvoot_config.compute_status(max_val, worker_limit, lead_limit)

    pool_labels = [f"{p['name']} ({p['max']:.1f}%)" for p in pools.values()]
    note_parts: list[dict[str, Any]] = []
    if endpoint:
        ep_short = endpoint.rsplit("/", 1)[-1]
        note_parts.append({"key": "ag_note_live_ep", "args": {"endpoint": ep_short}})
    else:
        note_parts.append({"key": "ag_note_live"})
    note_parts.append({"key": "ag_note_pools", "args": {"pools": ", ".join(pool_labels)}})

    return {
        "available": True,
        "source_time": now_utc.astimezone(tz).isoformat(timespec="seconds"),
        "source_location": "language_server",
        "age_min": 0,
        "window_5h": win_5h,
        "window_weekly": win_wk,
        "pools": pools,
        "max": max_val,
        "status": status,
        "note_code": "language_server",
        "note": _render_parts(note_parts),
        "_note_parts": note_parts,
    }


def _probe_raw_posix() -> dict[str, Any]:
    proc = find_antigravity_process_linux()
    if proc is None:
        return {
            "ok": False,
            "reason_code": "server_not_running",
            "reason_key": "ag_reason_not_running",
            "reason": _msg("ag_reason_not_running"),
        }
    try:
        ports = find_listening_ports_linux(proc.pid)
        if not ports and not proc.extension_port:
            return {
                "ok": False,
                "reason_code": "no_listening_ports",
                "reason_key": "ag_reason_no_ports",
                "reason": _msg("ag_reason_no_ports"),
            }
        payload, endpoint, err = _query_process(proc, ports)
        if payload is None:
            if err and err != _msg("ag_reason_no_response"):
                clean_err = mask_token(err, proc.csrf_token)
                return {
                    "ok": False,
                    "reason_code": "server_no_response",
                    "reason_key": "ag_reason_query_failed",
                    "reason_args": {"err": clean_err},
                    "reason": _msg("ag_reason_query_failed", err=clean_err),
                }
            return {
                "ok": False,
                "reason_code": "server_no_response",
                "reason_key": "ag_reason_no_response",
                "reason": _msg("ag_reason_no_response"),
            }
        us = payload.get("userStatus")
        if isinstance(us, dict):
            us.pop("email", None)
            us.pop("name", None)
        return {"ok": True, "payload": payload, "endpoint": endpoint}
    except Exception as exc:
        clean_err = mask_token(str(exc), proc.csrf_token)
        return {
            "ok": False,
            "reason_code": "server_no_response",
            "reason_key": "ag_reason_query_failed",
            "reason_args": {"err": clean_err},
            "reason": _msg("ag_reason_query_failed", err=clean_err),
        }


def _is_windows() -> bool:
    return os.name == "nt"


def read_antigravity_quota(
    now: datetime | None = None,
    tz: tzinfo | None = None,
    cache_path: Path | str | None = None,
    stale_min: int | None = None,
    worker_limit: float | None = None,
    lead_limit: float | None = None,
) -> dict[str, Any]:
    """Loeb töötava Antigravity keeleserveri kvoodi ainult 127.0.0.1 kaudu (või viimase teadaoleva)."""
    assert _kvoot_config is not None
    t0 = time.perf_counter()
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    target_tz = tz or _kvoot_config.resolve_tz()
    target_cache = Path(cache_path) if cache_path is not None else default_cache_path(for_write=True)

    payload: dict[str, Any] | None = None
    endpoint: str | None = None
    err: str | None = None
    reason_code: str = "server_not_running"
    reason_part: dict[str, Any] | None = None
    token_to_mask: str | None = None

    if _is_windows():
        try:
            proc = find_antigravity_process_windows()
            if proc is not None:
                token_to_mask = proc.csrf_token
                ports = find_listening_ports_windows(proc.pid)
                if ports or proc.extension_port:
                    payload, endpoint, err = _query_process(proc, ports)
                    if payload is None:
                        reason_code = "server_no_response"
                        if err and err != _msg("ag_reason_no_response"):
                            reason_part = {"key": "ag_reason_query_failed", "args": {"err": mask_token(err, token_to_mask)}}
                            err = _render_parts([reason_part])
                        else:
                            reason_part = {"key": "ag_reason_no_response"}
                else:
                    reason_code = "no_listening_ports"
                    reason_part = {"key": "ag_reason_no_ports"}
                    err = _msg("ag_reason_no_ports")
        except Exception as exc:
            payload = None
            reason_code = "server_no_response"
            reason_part = {"key": "ag_reason_query_failed", "args": {"err": mask_token(str(exc), token_to_mask)}}
            err = _render_parts([reason_part])

        if payload is None:
            try:
                wsl_payload, wsl_ep, wsl_code, wsl_err = _query_wsl_from_windows()
                if wsl_payload is not None:
                    payload, endpoint, err = wsl_payload, wsl_ep, None
                    reason_part = None
                elif err is None:
                    reason_code = wsl_code or "server_not_running"
                    if isinstance(wsl_err, dict) and wsl_err.get("key"):
                        reason_part = _mask_part(wsl_err, token_to_mask)
                        err = _render_parts([reason_part])
                    elif reason_code == "no_listening_ports":
                        reason_part = {"key": "ag_reason_no_ports"}
                        err = _render_parts([reason_part])
                    elif reason_code == "no_quota_data":
                        reason_part = {"key": "ag_reason_wsl_no_data"}
                        err = _render_parts([reason_part])
                    elif reason_code == "server_no_response":
                        if wsl_err == _msg("ag_reason_wsl_bad_response"):
                            reason_part = {"key": "ag_reason_wsl_bad_response"}
                        else:
                            reason_part = {"key": "ag_reason_no_response"}
                        err = _render_parts([reason_part])
                    elif wsl_err and str(wsl_err) != _msg("ag_reason_not_running"):
                        reason_part = {"key": "ag_reason_wsl_failed", "args": {"err": mask_token(str(wsl_err), token_to_mask)}}
                        err = _render_parts([reason_part])
                    else:
                        reason_part = {"key": "ag_reason_not_running"}
                        err = _render_parts([reason_part])
            except Exception as exc:
                payload = None
                if err is None:
                    reason_code = "server_not_running"
                    reason_part = {"key": "ag_reason_wsl_failed", "args": {"err": mask_token(str(exc), token_to_mask)}}
                    err = _render_parts([reason_part])
    else:
        try:
            proc = find_antigravity_process_linux()
            if proc is None:
                reason_code = "server_not_running"
                reason_part = {"key": "ag_reason_not_running"}
                err = _msg("ag_reason_not_running")
            else:
                token_to_mask = proc.csrf_token
                ports = find_listening_ports_linux(proc.pid)
                if not ports and not proc.extension_port:
                    reason_code = "no_listening_ports"
                    reason_part = {"key": "ag_reason_no_ports"}
                    err = _msg("ag_reason_no_ports")
                else:
                    payload, endpoint, err = _query_process(proc, ports)
                    if payload is None:
                        reason_code = "server_no_response"
                        if err and err != _msg("ag_reason_no_response"):
                            reason_part = {"key": "ag_reason_query_failed", "args": {"err": mask_token(err, token_to_mask)}}
                            err = _render_parts([reason_part])
                        else:
                            reason_part = {"key": "ag_reason_no_response"}
        except Exception as exc:
            payload = None
            reason_code = "server_no_response"
            reason_part = {"key": "ag_reason_query_failed", "args": {"err": mask_token(str(exc), token_to_mask)}}
            err = _render_parts([reason_part])

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)

    if payload is None:
        cached = load_antigravity_cache(
            target_cache,
            now_utc,
            target_tz,
            stale_min=stale_min,
            worker_limit=worker_limit,
            lead_limit=lead_limit,
        )
        if cached is not None:
            cached["read_time_ms"] = elapsed_ms
            return cached
        if reason_part is None:
            reason_part = {"key": "ag_reason_no_response"}
        return {
            "available": False,
            "reason_code": reason_code,
            "reason": mask_token(err or _render_parts([reason_part]), token_to_mask),
            "_reason_parts": [reason_part],
            "read_time_ms": elapsed_ms,
        }

    try:
        parsed = parse_antigravity_response(
            payload,
            now_utc,
            target_tz,
            endpoint=endpoint,
            worker_limit=worker_limit,
            lead_limit=lead_limit,
        )
    except Exception as exc:
        r_part = {"key": "ag_reason_parse_error", "args": {"err": mask_token(str(exc), token_to_mask)}}
        return {
            "available": False,
            "reason_code": "parse_error",
            "reason": mask_token(_render_parts([r_part]), token_to_mask),
            "_reason_parts": [r_part],
            "read_time_ms": elapsed_ms,
        }
    if parsed is None:
        r_part = {"key": "ag_reason_no_quota_data"}
        return {
            "available": False,
            "reason_code": "no_quota_data",
            "reason": _render_parts([r_part]),
            "_reason_parts": [r_part],
            "read_time_ms": elapsed_ms,
        }
    try:
        save_antigravity_cache(parsed, now_utc, target_cache)
    except OSError as exc:
        err_type = type(exc).__name__
        save_part = {"key": "ag_note_cache_write_failed", "args": {"err_type": err_type}}
        note_parts = list(parsed.get("_note_parts") or [])
        note_parts.append(save_part)
        parsed["note_code"] = "cache_write_failed"
        parsed["_note_parts"] = note_parts
        parsed["note"] = _render_parts(note_parts)
    parsed["read_time_ms"] = elapsed_ms
    return parsed


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--probe-raw":
        res = _probe_raw_posix()
        print(json.dumps(res, ensure_ascii=False))
        sys.exit(0 if res.get("ok") else 1)
