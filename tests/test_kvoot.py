"""Sünteetilised ühiktestid kvoot.py jaoks (päris sessioonifaile ei kasutata)."""

from __future__ import annotations

import io
import json
import os
import py_compile
import re
import shutil
import stat
import string
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import antigravity_probe  # noqa: E402
import claude_statusline  # noqa: E402
import claude_usage  # noqa: E402
import kvoot  # noqa: E402
import kvoot_config  # noqa: E402
import kvoot_i18n  # noqa: E402
import importlib.machinery  # noqa: E402
import importlib.util  # noqa: E402

_ava_loader = importlib.machinery.SourceFileLoader("kvoot_ava", str(ROOT / "kvoot_ava.pyw"))
_ava_spec = importlib.util.spec_from_loader("kvoot_ava", _ava_loader)
assert _ava_spec is not None
kvoot_ava = importlib.util.module_from_spec(_ava_spec)
_ava_loader.exec_module(kvoot_ava)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _find_node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    nvm_dir = Path.home() / ".nvm" / "versions" / "node"
    if nvm_dir.is_dir():
        for cand in sorted(nvm_dir.iterdir(), reverse=True):
            bin_node = cand / "bin" / "node"
            if bin_node.is_file():
                return str(bin_node)
    return None


def _write_rollout(base_dir: Path, rel_path: str, lines: list[str]) -> Path:
    fp = base_dir / rel_path
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return fp


class TestKvoodimootur(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_kodu = tempfile.TemporaryDirectory()
        self._empty_cfg = Path(self._tmp_kodu.name) / "empty_config.json"
        self._empty_cfg.write_text("{}", encoding="utf-8")
        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "KVOOT_KODU": self._tmp_kodu.name,
                "KVOOT_TZ": "Europe/Tallinn",
                "KVOOT_LANG": "en",
                "KVOOT_TITLE": "",
                "KVOOT_WORKER_LIMIT": "",
                "KVOOT_LEAD_LIMIT": "",
                "KVOOT_STALE_MIN": "",
                "KVOOT_VANA_MIN": "",
                "KVOOT_STATE_DIR": "",
                "KVOOT_WIN_HOME": "",
                "KVOOT_CONFIG": str(self._empty_cfg),
            },
        )
        self._env_patch.start()
        self._anti_linux_patch = mock.patch.object(
            antigravity_probe, "find_antigravity_process_linux", return_value=None
        )
        self._anti_win_patch = mock.patch.object(
            antigravity_probe, "find_antigravity_process_windows", return_value=None
        )
        self._anti_wsl_patch = mock.patch.object(
            antigravity_probe,
            "_query_wsl_from_windows",
            return_value=(None, None, "server_not_running", {"key": "ag_reason_not_running"}),
        )
        self._anti_linux_patch.start()
        self._anti_win_patch.start()
        self._anti_wsl_patch.start()

    def tearDown(self) -> None:
        self._anti_wsl_patch.stop()
        self._anti_win_patch.stop()
        self._anti_linux_patch.stop()
        self._env_patch.stop()
        self._tmp_kodu.cleanup()

    def test_freshest_across_files_and_broken_line(self) -> None:
        # 2026-11-15T08:30:00Z -> resets_at 1794744000 is 2026-11-15T12:00:00Z (future)
        now = datetime(2026, 11, 15, 8, 30, 0, tzinfo=timezone.utc)
        res = kvoot.read_codex_quota(sessions_dir=FIXTURES_DIR, now=now)
        self.assertTrue(res["available"])
        self.assertEqual(res["source_time"], "2026-11-15T10:00:00+02:00")
        self.assertEqual(res["window_5h"]["used"], 42.5)
        self.assertFalse(res["window_5h"]["reset"])
        self.assertEqual(res["window_weekly"]["used"], 18.0)
        self.assertEqual(res["max"], 42.5)
        self.assertEqual(res["status"], "green")
        self.assertEqual(res["note_code"], "local_sessions")

    def test_freshest_by_timestamp_not_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_rollout(
                d,
                "2026/11/14/rollout-a.jsonl",
                [
                    json.dumps(
                        {
                            "timestamp": "2026-11-15T09:00:00Z",
                            "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "rate_limits": {
                                    "primary": {"used_percent": 33.0, "resets_at": 1794750000},
                                    "secondary": {"used_percent": 10.0, "resets_at": 1795186800},
                                },
                            },
                        }
                    )
                ],
            )
            _write_rollout(
                d,
                "2026/11/15/rollout-z.jsonl",
                [
                    json.dumps(
                        {
                            "timestamp": "2026-11-15T08:00:00Z",
                            "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "rate_limits": {
                                    "primary": {"used_percent": 99.0, "resets_at": 1794750000},
                                    "secondary": {"used_percent": 99.0, "resets_at": 1795186800},
                                },
                            },
                        }
                    )
                ],
            )
            now = datetime(2026, 11, 15, 9, 1, 0, tzinfo=timezone.utc)
            res = kvoot.read_codex_quota(sessions_dir=d, now=now)
            self.assertEqual(res["window_5h"]["used"], 33.0)

    def test_reset_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_rollout(
                d,
                "2026/11/15/rollout-reset.jsonl",
                [
                    json.dumps(
                        {
                            "timestamp": "2026-11-15T06:00:00Z",
                            "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "rate_limits": {
                                    "primary": {"used_percent": 85.0, "resets_at": 1794729600},
                                    "secondary": {"used_percent": 12.0, "resets_at": 1795186800},
                                },
                            },
                        }
                    )
                ],
            )
            now = datetime(2026, 11, 15, 8, 30, 0, tzinfo=timezone.utc)
            res = kvoot.read_codex_quota(sessions_dir=d, now=now)
            self.assertTrue(res["available"])
            self.assertTrue(res["window_5h"]["reset"])
            self.assertEqual(res["window_5h"]["used"], 0.0)
            self.assertFalse(res["window_weekly"]["reset"])
            self.assertEqual(res["window_weekly"]["used"], 12.0)
            self.assertEqual(res["max"], 12.0)
            self.assertEqual(res["status"], "green")

    def test_missing_sources(self) -> None:
        missing = Path(tempfile.gettempdir()) / "nonexistent_kvoot_dir_12345"
        res = kvoot.read_codex_quota(sessions_dir=missing)
        self.assertFalse(res["available"])
        self.assertEqual(res["reason_code"], "codex_dir_not_found")
        self.assertTrue(bool(res["reason"]))

        claude = kvoot.read_claude_quota()
        self.assertFalse(claude["available"])
        self.assertEqual(claude["reason_code"], "claude_file_not_found")
        self.assertTrue(bool(claude["reason"]))

        agy = kvoot.read_antigravity_quota()
        self.assertFalse(agy["available"])
        self.assertEqual(agy["reason_code"], "server_not_running")
        self.assertTrue(bool(agy["reason"]))

        report = kvoot.build_quota_report(codex_sessions_dir=missing)
        self.assertEqual(report["schema"], "kvoot/4")
        self.assertIsNone(report["workers_allowed"]["codex"])
        self.assertIsNone(report["workers_allowed"]["claude"])
        self.assertEqual(kvoot.check_permission(report, "worker", "codex"), 2)
        self.assertEqual(kvoot.check_permission(report, "lead", "claude"), 2)
        self.assertEqual(kvoot.check_permission(report, "worker", "unknown_src"), 2)

    def test_thresholds_49_9_to_70(self) -> None:
        cases = [
            (49.9, "green", 0, 0),
            (50.0, "yellow", 1, 0),
            (69.9, "yellow", 1, 0),
            (70.0, "red", 1, 1),
        ]
        now = datetime(2026, 11, 15, 8, 30, 0, tzinfo=timezone.utc)
        for pct, exp_status, exp_worker, exp_lead in cases:
            with self.subTest(pct=pct):
                with tempfile.TemporaryDirectory() as tmp:
                    d = Path(tmp)
                    _write_rollout(
                        d,
                        "2026/11/15/rollout-limit.jsonl",
                        [
                            json.dumps(
                                {
                                    "timestamp": "2026-11-15T08:00:00Z",
                                    "type": "event_msg",
                                    "payload": {
                                        "type": "token_count",
                                        "rate_limits": {
                                            "primary": {"used_percent": pct, "resets_at": 1794750000},
                                            "secondary": {"used_percent": 5.0, "resets_at": 1795186800},
                                        },
                                    },
                                }
                            )
                        ],
                    )
                    report = kvoot.build_quota_report(codex_sessions_dir=d, now=now)
                    self.assertEqual(report["sources"]["codex"]["status"], exp_status)
                    self.assertEqual(kvoot.check_permission(report, "worker", "codex"), exp_worker)
                    self.assertEqual(kvoot.check_permission(report, "lead", "codex"), exp_lead)

                    code_w = kvoot.main(["--check", "worker", "--source", "codex", "--codex-dir", str(d)], now=now)
                    code_l = kvoot.main(["--check", "lead", "--source", "codex", "--codex-dir", str(d)], now=now)
                    self.assertEqual(code_w, exp_worker)
                    self.assertEqual(code_l, exp_lead)

    def test_html_and_tallinn_tz(self) -> None:
        now = datetime(2026, 11, 15, 8, 30, 0, tzinfo=timezone.utc)
        report = kvoot.build_quota_report(codex_sessions_dir=FIXTURES_DIR, now=now)
        html_str = kvoot.render_html(report, now=now)
        self.assertIn("Multi-AI Quota Monitor", html_str)
        self.assertIn("marker-worker", html_str)
        self.assertIn("marker-lead", html_str)
        self.assertIn("left: 50%", html_str)
        self.assertIn("left: 70%", html_str)
        self.assertIn("data-theme", html_str)
        self.assertIn('id="langSelect"', html_str)
        self.assertNotIn("http://", html_str)
        self.assertNotIn("https://", html_str)

        tz = kvoot_config.tallinn_tz()
        winter = datetime(2026, 11, 15, 8, 0, tzinfo=timezone.utc).astimezone(tz)
        summer = datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc).astimezone(tz)
        self.assertEqual(winter.utcoffset().total_seconds(), 7200)
        self.assertEqual(summer.utcoffset().total_seconds(), 10800)

    def test_statusline_both_windows_and_privacy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_file = Path(tmp) / "kvoot_claude.json"
            payload = {
                "cwd": "/secret/path",
                "session_id": "sess-123",
                "transcript_path": "/secret/transcript.jsonl",
                "model": {"id": "claude-opus-4-6", "display_name": "Opus 4.6"},
                "cost": {"total_cost_usd": 1.23},
                "context_window": {"used_percentage": 8.4},
                "rate_limits": {
                    "five_hour": {"used_percentage": 23.0, "resets_at": 1794750000},
                    "seven_day": {"used_percentage": 41.0, "resets_at": 1795186800},
                },
            }
            now = datetime(2026, 11, 15, 10, 0, 0, tzinfo=timezone.utc)
            line_en = claude_statusline.process_stdin_text(
                json.dumps(payload), out_path=out_file, now_utc=now
            )
            self.assertEqual(line_en, "5h 23% · 7d 41% · ctx 8%")
            with mock.patch.dict(os.environ, {"KVOOT_LANG": "et"}):
                line_et = claude_statusline.format_status_line(payload)
            self.assertEqual(line_et, "5h 23% · 7p 41% · kontekst 8%")

            self.assertTrue(out_file.exists())
            saved = json.loads(out_file.read_text(encoding="utf-8"))
            self.assertEqual(set(saved.keys()), {"written_at", "five_hour", "seven_day"})
            self.assertEqual(saved["five_hour"], {"used_percentage": 23.0, "resets_at": 1794750000.0})
            self.assertEqual(saved["seven_day"], {"used_percentage": 41.0, "resets_at": 1795186800.0})
            raw_saved = out_file.read_text(encoding="utf-8")
            self.assertNotIn("secret", raw_saved)
            self.assertNotIn("opus", raw_saved)
            self.assertNotIn("1.23", raw_saved)

    def test_statusline_one_window_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_file = Path(tmp) / "kvoot_claude.json"
            payload = {
                "rate_limits": {
                    "five_hour": {"used_percentage": 15.5, "resets_at": 1800000000},
                }
            }
            now = datetime(2026, 11, 15, 10, 0, 0, tzinfo=timezone.utc)
            line = claude_statusline.process_stdin_text(
                json.dumps(payload), out_path=out_file, now_utc=now
            )
            self.assertEqual(line, "5h 16% · 7d -")
            saved = json.loads(out_file.read_text(encoding="utf-8"))
            self.assertEqual(saved["five_hour"]["used_percentage"], 15.5)
            self.assertIsNone(saved["seven_day"])

            # O3: one-window statusline without fallback -> UNKNOWN (allowed=None, --check code 3)
            rep_missing = kvoot.build_quota_report(
                codex_sessions_dir=FIXTURES_DIR,
                claude_file=out_file,
                now=now + timedelta(minutes=5),
            )
            c_missing = rep_missing["sources"]["claude"]
            self.assertTrue(c_missing["available"])
            self.assertEqual(c_missing["window_5h"]["source_location"], "statusline")
            self.assertIsNone(c_missing["window_weekly"])
            self.assertEqual(c_missing["status"], "unknown")
            self.assertEqual(c_missing["note_code"], "missing_window")
            self.assertIsNone(rep_missing["workers_allowed"]["claude"])
            self.assertIsNone(rep_missing["lead_allowed"]["claude"])
            self.assertEqual(kvoot.check_permission(rep_missing, "worker", "claude"), 3)
            self.assertEqual(kvoot.check_permission(rep_missing, "lead", "claude"), 3)

            # Fresh fallback (manual) provides missing 7d window -> taken from fallback
            manual_file = Path(tmp) / "kvoot_claude_manual.json"
            kvoot.save_claude_manual_quota(
                10.0,
                55.0,
                out_path=manual_file,
                now=now - timedelta(minutes=10),
            )
            rep_fb = kvoot.build_quota_report(
                codex_sessions_dir=FIXTURES_DIR,
                claude_file=out_file,
                claude_manual_file=manual_file,
                now=now + timedelta(minutes=5),
            )
            c_fb = rep_fb["sources"]["claude"]
            self.assertEqual(c_fb["source_location"], "statusline")
            self.assertEqual(c_fb["window_5h"]["source_location"], "statusline")
            self.assertEqual(c_fb["window_weekly"]["used"], 55.0)
            self.assertEqual(c_fb["window_weekly"]["source_location"], "manual")
            self.assertEqual(c_fb["max"], 55.0)
            self.assertFalse(rep_fb["workers_allowed"]["claude"])
            self.assertEqual(kvoot.check_permission(rep_fb, "worker", "claude"), 1)

    def test_statusline_no_rate_limits_preserves_old(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_file = Path(tmp) / "kvoot_claude.json"
            old_data = {
                "written_at": "2026-11-15T09:00:00+00:00",
                "five_hour": {"used_percentage": 12.0, "resets_at": 1794750000},
                "seven_day": None,
            }
            out_file.write_text(json.dumps(old_data), encoding="utf-8")

            line = claude_statusline.process_stdin_text(
                json.dumps({"context_window": {"used_percentage": 5.0}}),
                out_path=out_file,
            )
            self.assertEqual(line, "5h - · 7d - · ctx 5%")
            self.assertEqual(json.loads(out_file.read_text(encoding="utf-8")), old_data)

    def test_claude_status_limits_and_age(self) -> None:
        now = datetime(2026, 11, 15, 8, 42, 0, tzinfo=timezone.utc)
        cases = [
            (49.9, "green", 0, 0),
            (50.0, "yellow", 1, 0),
            (70.0, "red", 1, 1),
        ]
        for pct, exp_status, exp_w, exp_l in cases:
            with self.subTest(pct=pct):
                with tempfile.TemporaryDirectory() as tmp:
                    c_file = Path(tmp) / "kvoot_claude.json"
                    c_file.write_text(
                        json.dumps(
                            {
                                "written_at": "2026-11-15T08:30:00+00:00",
                                "five_hour": {"used_percentage": pct, "resets_at": 1794750000},
                                "seven_day": {"used_percentage": 10.0, "resets_at": 1795186800},
                            }
                        ),
                        encoding="utf-8",
                    )
                    report = kvoot.build_quota_report(
                        codex_sessions_dir=FIXTURES_DIR,
                        claude_file=c_file,
                        now=now,
                    )
                    c_info = report["sources"]["claude"]
                    self.assertTrue(c_info["available"])
                    self.assertEqual(c_info["status"], exp_status)
                    self.assertEqual(c_info["age_min"], 12)
                    self.assertEqual(c_info["note_code"], "statusline")
                    self.assertIn("claude.ai", c_info["note"])
                    self.assertEqual(kvoot.check_permission(report, "worker", "claude"), exp_w)
                    self.assertEqual(kvoot.check_permission(report, "lead", "claude"), exp_l)

                    html_str = kvoot.render_html(report, now=now)
                    self.assertIn("updated 12 min ago", html_str)

    def test_home_dir_and_dynamic_wsl_profile_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"KVOOT_KODU": tmp}):
                self.assertEqual(kvoot_config.resolve_home_dir(), Path(tmp))
                self.assertEqual(kvoot.resolve_kodu_dir(), Path(tmp))
                self.assertEqual(kvoot.default_codex_sessions_dir(), Path(tmp) / ".codex" / "sessions")
                self.assertEqual(kvoot.default_claude_quota_path(), Path(tmp) / ".claude" / "kvoot_claude.json")
                self.assertEqual(claude_statusline.default_claude_quota_path(), Path(tmp) / ".claude" / "kvoot_claude.json")

        # KVOOT_WIN_HOME override with Windows path
        with mock.patch.dict(os.environ, {"KVOOT_KODU": "", "KVOOT_WIN_HOME": "C:\\Users\\alice"}):
            with mock.patch.object(kvoot_config, "_is_windows", return_value=False):
                with mock.patch.object(kvoot_config.subprocess, "run", side_effect=OSError("no wslpath")):
                    with mock.patch.object(Path, "exists", return_value=True):
                        self.assertEqual(kvoot_config.resolve_home_dir(), Path("/mnt/c/Users/alice"))

        # Dynamic detection via cmd.exe /c echo %USERPROFILE% + wslpath
        is_win = os.name == "nt"
        with tempfile.TemporaryDirectory() as tmp:
            if is_win:
                expected_alice = Path("/mnt/c/Users/alice")
                wslpath_out = "/mnt/c/Users/alice\n"
            else:
                expected_alice = Path(tmp) / "mnt" / "c" / "Users" / "alice"
                expected_alice.mkdir(parents=True)
                wslpath_out = str(expected_alice) + "\n"

            def _fake_run(cmd, **_kwargs):
                if cmd[0] == "cmd.exe":
                    return subprocess.CompletedProcess(cmd, 0, stdout="C:\\Users\\alice\r\n", stderr="")
                if cmd[0] == "wslpath":
                    return subprocess.CompletedProcess(cmd, 0, stdout=wslpath_out, stderr="")
                raise AssertionError(f"unexpected cmd: {cmd}")

            with mock.patch.dict(os.environ, {"KVOOT_KODU": "", "KVOOT_WIN_HOME": "", "WSL_DISTRO_NAME": "Ubuntu"}):
                with mock.patch.object(kvoot_config, "_is_windows", return_value=False):
                    with mock.patch.object(kvoot_config.subprocess, "run", side_effect=_fake_run):
                        if is_win:
                            expected_win_alice = Path("/mnt/c/Users/alice")
                            with mock.patch.object(Path, "exists", return_value=True):
                                with mock.patch.object(Path, "is_dir", return_value=True):
                                    self.assertEqual(kvoot_config.resolve_win_home_wsl(), expected_win_alice)
                                    self.assertEqual(kvoot_config.resolve_home_dir(), expected_win_alice)
                        else:
                            self.assertEqual(kvoot_config.resolve_win_home_wsl(), expected_alice)
                            self.assertEqual(kvoot_config.resolve_home_dir(), expected_alice)

        # Fallback scanning /mnt/c/Users/* when cmd.exe fails
        with tempfile.TemporaryDirectory() as tmp:
            users_root = Path(tmp) / "Users"
            (users_root / "Public").mkdir(parents=True)
            (users_root / "Default").mkdir(parents=True)
            alice_profile = users_root / "alice"
            (alice_profile / ".codex").mkdir(parents=True)
            with mock.patch.dict(os.environ, {"KVOOT_WIN_HOME": "", "WSL_DISTRO_NAME": "Ubuntu"}):
                with mock.patch.object(kvoot_config, "_is_windows", return_value=False):
                    self.assertEqual(kvoot_config.resolve_win_home_wsl(users_root=users_root), alice_profile)

        # Single implementation check: claude_statusline and kvoot share kvoot_config.resolve_home_dir
        self.assertIs(kvoot.resolve_kodu_dir, kvoot_config.resolve_home_dir)

    @staticmethod
    def _codex_line(ts: str, p5: float, p7: float, r5: int = 1800000000, r7: int = 1800500000) -> str:
        return json.dumps(
            {
                "timestamp": ts,
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "primary": {"used_percent": p5, "resets_at": r5},
                        "secondary": {"used_percent": p7, "resets_at": r7},
                    },
                },
            }
        )

    def test_codex_multiple_sources_freshest_wins_both_ways(self) -> None:
        now = datetime(2026, 11, 17, 6, 0, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            win_dir = Path(tmp) / "win"
            wsl_dir = Path(tmp) / "wsl"

            _write_rollout(win_dir, "2026/11/17/rollout-1.jsonl", [self._codex_line("2026-11-17T04:00:00Z", 12.0, 5.0)])
            _write_rollout(wsl_dir, "2026/11/17/rollout-2.jsonl", [self._codex_line("2026-11-17T05:30:00Z", 44.0, 20.0)])

            with mock.patch.dict(os.environ, {"KVOOT_CODEX_DIRS": f"windows={win_dir};wsl={wsl_dir}"}):
                res_wsl = kvoot.read_codex_quota(now=now)
            self.assertTrue(res_wsl["available"])
            self.assertEqual(res_wsl["source_location"], "wsl")
            self.assertEqual(res_wsl["max"], 44.0)

            _write_rollout(win_dir, "2026/11/17/rollout-3.jsonl", [self._codex_line("2026-11-17T05:50:00Z", 55.0, 22.0)])
            with mock.patch.dict(os.environ, {"KVOOT_CODEX_DIRS": f"windows={win_dir};wsl={wsl_dir}"}):
                res_win = kvoot.read_codex_quota(now=now)
            self.assertTrue(res_win["available"])
            self.assertEqual(res_win["source_location"], "windows")
            self.assertEqual(res_win["max"], 55.0)

    def test_codex_one_dir_missing_and_three_day_window(self) -> None:
        now = datetime(2026, 11, 17, 6, 0, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            win_dir = Path(tmp) / "win"
            missing_wsl = Path(tmp) / "missing_wsl"

            _write_rollout(win_dir, "2026/11/10/rollout-old.jsonl", [self._codex_line("2026-11-17T05:59:00Z", 99.0, 99.0)])
            _write_rollout(win_dir, "2026/11/14/rollout-4d.jsonl", [self._codex_line("2026-11-17T05:59:00Z", 88.0, 88.0)])
            _write_rollout(
                win_dir,
                "2026/11/16/rollout-active.jsonl",
                [
                    self._codex_line("2026-11-16T10:00:00Z", 5.0, 2.0),
                    json.dumps({"type": "message", "text": "conversation"}),
                    self._codex_line("2026-11-17T05:45:00Z", 31.0, 14.0),
                ],
            )

            with mock.patch.dict(os.environ, {"KVOOT_CODEX_DIRS": f"windows={win_dir};wsl={missing_wsl}"}):
                res = kvoot.read_codex_quota(now=now)
            self.assertTrue(res["available"])
            self.assertEqual(res["source_location"], "windows")
            self.assertEqual(res["max"], 31.0)
            self.assertEqual(res["note_code"], "partial_sources")
            self.assertIn("unreadable: wsl", res["note"])

            only_old = Path(tmp) / "only_old"
            _write_rollout(only_old, "2026/11/13/rollout-old.jsonl", [self._codex_line("2026-11-13T12:00:00Z", 10.0, 5.0)])
            res_empty = kvoot.read_codex_quota(sessions_dir=only_old, now=now)
            self.assertFalse(res_empty["available"])
            self.assertEqual(res_empty["reason_code"], "codex_no_session_files")

    def test_stale_reading_179_vs_181_min_and_html_and_check(self) -> None:
        src_time = datetime(2026, 11, 17, 6, 0, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_rollout(
                d,
                "2026/11/17/rollout-time.jsonl",
                [self._codex_line(src_time.isoformat().replace("+00:00", "Z"), 24.0, 11.0)],
            )

            now_179 = src_time + timedelta(minutes=179)
            rep_179 = kvoot.build_quota_report(codex_sessions_dir=d, now=now_179)
            self.assertEqual(rep_179["sources"]["codex"]["age_min"], 179)
            self.assertEqual(rep_179["sources"]["codex"]["status"], "green")
            self.assertTrue(rep_179["workers_allowed"]["codex"])
            self.assertTrue(rep_179["lead_allowed"]["codex"])
            self.assertEqual(kvoot.check_permission(rep_179, "worker", "codex"), 0)

            now_181 = src_time + timedelta(minutes=181)
            rep_181 = kvoot.build_quota_report(codex_sessions_dir=d, now=now_181)
            self.assertEqual(rep_181["sources"]["codex"]["age_min"], 181)
            self.assertEqual(rep_181["sources"]["codex"]["status"], "stale")
            self.assertIsNone(rep_181["workers_allowed"]["codex"])
            self.assertIsNone(rep_181["lead_allowed"]["codex"])
            self.assertEqual(kvoot.check_permission(rep_181, "worker", "codex"), 3)
            self.assertEqual(kvoot.check_permission(rep_181, "lead", "codex"), 3)
            self.assertEqual(kvoot.main(["--check", "worker", "--source", "codex", "--codex-dir", str(d)], now=now_181), 3)

            html_181 = kvoot.render_html(rep_181, now=now_181)
            html_body = html_181.split("</style>", 1)[1]
            self.assertIn("stale, 3 h ago", html_body)
            self.assertIn("badge-stale", html_body)
            self.assertIn("fill-stale", html_body)
            self.assertIn("24.0%", html_body)
            self.assertNotIn("fill-green", html_body)
            self.assertNotIn("badge-green", html_body)

            with mock.patch.dict(os.environ, {"KVOOT_STALE_MIN": "60"}):
                rep_61 = kvoot.build_quota_report(codex_sessions_dir=d, now=src_time + timedelta(minutes=61))
                self.assertEqual(rep_61["sources"]["codex"]["status"], "stale")

    def test_claude_manual_entry_validation_and_competition(self) -> None:
        now = datetime(2026, 11, 17, 6, 0, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            claude_status = Path(tmp) / "kvoot_claude.json"
            claude_manual = Path(tmp) / "kvoot_claude_manual.json"

            for argid in [
                ["--set", "claude", "--5h", "101", "--7d", "50", "--claude-file", str(claude_status)],
                ["--set", "claude", "--5h", "-1", "--7d", "50", "--claude-file", str(claude_status)],
                ["--set", "claude", "--5h", "text", "--7d", "50", "--claude-file", str(claude_status)],
                ["--set", "claude", "--5h", "40", "--7d", "nan", "--claude-file", str(claude_status)],
                ["--set", "claude", "--5h", "40", "--claude-file", str(claude_status)],
            ]:
                with self.subTest(argid=argid):
                    code = kvoot.main(argid, now=now)
                    self.assertNotEqual(code, 0)
                    self.assertFalse(claude_manual.exists())

            code_ok = kvoot.main(
                ["--set", "claude", "--5h", "40", "--7d", "62", "--claude-file", str(claude_status)],
                now=now,
            )
            self.assertEqual(code_ok, 0)
            self.assertTrue(claude_manual.exists())
            c_info = kvoot.read_claude_quota(claude_file=claude_status, now=now + timedelta(minutes=5))
            self.assertTrue(c_info["available"])
            self.assertEqual(c_info["source_location"], "manual")
            self.assertEqual(c_info["window_5h"]["used"], 40.0)
            self.assertIsNone(c_info["window_5h"]["resets_at"])
            self.assertEqual(c_info["window_weekly"]["used"], 62.0)
            self.assertEqual(c_info["max"], 62.0)
            self.assertEqual(c_info["status"], "yellow")

            code_reset = kvoot.main(
                [
                    "--set",
                    "claude",
                    "--5h",
                    "25",
                    "--7d",
                    "30",
                    "--5h-reset",
                    "14:30",
                    "--7d-reset",
                    "2026-11-20T15:00:00+02:00",
                    "--claude-file",
                    str(claude_status),
                ],
                now=now,
            )
            self.assertEqual(code_reset, 0)
            c_reset = kvoot.read_claude_quota(claude_file=claude_status, now=now)
            self.assertEqual(c_reset["window_5h"]["resets_at"], "2026-11-17T14:30:00+02:00")
            self.assertEqual(c_reset["window_weekly"]["resets_at"], "2026-11-20T15:00:00+02:00")

            claude_status.write_text(
                json.dumps(
                    {
                        "written_at": (now + timedelta(minutes=10)).isoformat(),
                        "five_hour": {"used_percentage": 15.0, "resets_at": 1800000000},
                        "seven_day": {"used_percentage": 19.0, "resets_at": 1800500000},
                    }
                ),
                encoding="utf-8",
            )
            c_st = kvoot.read_claude_quota(claude_file=claude_status, now=now + timedelta(minutes=15))
            self.assertEqual(c_st["source_location"], "statusline")
            self.assertEqual(c_st["max"], 19.0)

            kvoot.save_claude_manual_quota(
                48.0,
                51.0,
                out_path=claude_manual,
                now=now + timedelta(minutes=20),
            )
            c_man = kvoot.read_claude_quota(claude_file=claude_status, now=now + timedelta(minutes=25))
            self.assertEqual(c_man["source_location"], "manual")
            self.assertEqual(c_man["max"], 51.0)

            c_stale = kvoot.read_claude_quota(claude_file=claude_status, now=now + timedelta(minutes=202))
            self.assertEqual(c_stale["status"], "stale")

    def test_windows_wsl_discovery_utf16le_and_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            sessions = fake_home / "alice" / ".codex" / "sessions"
            sessions.mkdir(parents=True)

            fake_proc = mock.Mock(returncode=0, stdout="Ubuntu\r\n".encode("utf-16-le"))
            orig_path = kvoot.Path

            def fake_path_factory(arg: object, *args: object) -> Path:
                s = str(arg)
                if s.startswith("\\\\wsl.localhost\\Ubuntu\\home"):
                    return fake_home
                return orig_path(arg, *args)

            with mock.patch.object(kvoot.subprocess, "run", return_value=fake_proc):
                with mock.patch.object(kvoot, "Path", side_effect=fake_path_factory):
                    found, unavail = kvoot._discover_windows_wsl_codex_dirs()
            self.assertEqual(found, [("wsl", sessions)])
            self.assertEqual(unavail, [])

            with mock.patch.object(kvoot.subprocess, "run", side_effect=OSError("wsl missing")):
                found_err, unavail_err = kvoot._discover_windows_wsl_codex_dirs()
            self.assertEqual(found_err, [])
            self.assertEqual(len(unavail_err), 1)
            self.assertEqual(unavail_err[0][0], "wsl")

    def test_claude_usage_synthetic_screen_and_missing_window(self) -> None:
        tz = kvoot_config.tallinn_tz()
        now = datetime(2026, 11, 17, 6, 30, 0, tzinfo=timezone.utc)
        screen = "\n".join(
            [
                "Settings: Usage · Claude Max · hidden@example.com · Hidden Org",
                "Current session",
                "██████████████          28% used",
                "Resets 8:50am (Europe/Tallinn)",
                "Current week (all models)",
                "██████████████▍         29% used",
                "Resets Nov 18, 7pm (Europe/Tallinn)",
                "Current week (Fable)",
                "0% used",
            ]
        )
        parsed = claude_usage.parse_claude_usage_screen(screen, now, tz)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["five_hour"]["used_percentage"], 28.0)
        self.assertEqual(parsed["seven_day"]["used_percentage"], 29.0)
        self.assertEqual(
            datetime.fromtimestamp(parsed["five_hour"]["resets_at"], tz=timezone.utc).astimezone(tz).isoformat(timespec="seconds"),
            "2026-11-17T08:50:00+02:00",
        )
        self.assertEqual(
            datetime.fromtimestamp(parsed["seven_day"]["resets_at"], tz=timezone.utc).astimezone(tz).isoformat(timespec="seconds"),
            "2026-11-18T19:00:00+02:00",
        )

        left_screen = "Current session\n65% left\nResets in 2h 15m\nCurrent week (all models)\n80% left\n"
        parsed_left = claude_usage.parse_claude_usage_screen(left_screen, now, tz)
        self.assertTrue(parsed_left["ok"])
        self.assertEqual(parsed_left["five_hour"]["used_percentage"], 35.0)
        self.assertEqual(parsed_left["seven_day"]["used_percentage"], 20.0)

        only_5h = "Current session\n19% used\nResets 10:00am (Europe/Tallinn)\n"
        parsed_5h = claude_usage.parse_claude_usage_screen(only_5h, now, tz)
        self.assertTrue(parsed_5h["ok"])
        self.assertEqual(parsed_5h["five_hour"]["used_percentage"], 19.0)
        self.assertIsNone(parsed_5h["seven_day"])
        self.assertEqual(parsed_5h["reason_code"], "missing_window")
        self.assertEqual(parsed_5h["missing_windows"], ["seven_day"])

    def test_claude_usage_o2_screen_change_returns_null(self) -> None:
        tz = kvoot_config.tallinn_tz()
        now = datetime(2026, 11, 17, 6, 30, 0, tzinfo=timezone.utc)

        screen_1 = (
            "Current session\n10% used\n"
            "Weekly limit (all models)\n55% used\n"
            "Current week (Sonnet only)\n3% used\n"
        )
        p1 = claude_usage.parse_claude_usage_screen(screen_1, now, tz)
        self.assertTrue(p1["ok"])
        self.assertEqual(p1["five_hour"]["used_percentage"], 10.0)
        self.assertIsNone(p1["seven_day"])
        self.assertIn("Current week (all models)", p1["reason"])

        for screen_2 in (
            "Current session\nNo limit for your plan\nDaily usage (new)\n77% used\n",
            "Current session\nNo limit for your plan\nDaily usage (new) 77% used\n",
        ):
            with self.subTest(screen_2=screen_2):
                p2 = claude_usage.parse_claude_usage_screen(screen_2, now, tz)
                self.assertFalse(p2["ok"])
                self.assertEqual(p2["error_code"], "parse_failed")
                self.assertTrue(bool(p2["reason"]))

        p3 = claude_usage.parse_claude_usage_screen("Used: 28 percent\n", now, tz)
        self.assertFalse(p3["ok"])
        self.assertEqual(p3["error_code"], "parse_failed")
        self.assertTrue(bool(p3["reason"]))

        v12_screen = (
            "Previous Current session\n90% used\n"
            "Current session\n10% used\n"
            "Current week (all models)\n20% used\n"
        )
        p_v12 = claude_usage.parse_claude_usage_screen(v12_screen, now, tz)
        self.assertTrue(p_v12["ok"])
        self.assertEqual(p_v12["five_hour"]["used_percentage"], 10.0)
        self.assertEqual(p_v12["seven_day"]["used_percentage"], 20.0)

        p_v12_single = claude_usage.parse_claude_usage_screen("Last Current session 90% used\n", now, tz)
        self.assertFalse(p_v12_single["ok"])
        self.assertEqual(p_v12_single["error_code"], "parse_failed")

        u1_screen = (
            "Current session (Sonnet only)\n3% used\n"
            "Current session (all models)\n80% used\n"
            "Current week (all models)\n40% used\n"
        )
        p_u1 = claude_usage.parse_claude_usage_screen(u1_screen, now, tz)
        self.assertTrue(p_u1["ok"])
        self.assertEqual(p_u1["five_hour"]["used_percentage"], 80.0)
        self.assertEqual(p_u1["seven_day"]["used_percentage"], 40.0)

        u1_no_all = (
            "Current session (Sonnet only)\n3% used\n"
            "Current week (all models)\n40% used\n"
        )
        p_u1_null = claude_usage.parse_claude_usage_screen(u1_no_all, now, tz)
        self.assertTrue(p_u1_null["ok"])
        self.assertIsNone(p_u1_null["five_hour"])

        u2_u4 = [
            "Current session\ncontext window\n45% used\n",
            "Current session\nSession limit not tracked on this plan.\n███ 66% used\n",
            "Current session\n5-hour extra credits\n90% used\n",
        ]
        for idx, screen_u in enumerate(u2_u4, start=2):
            with self.subTest(screen=f"U{idx}"):
                p_u = claude_usage.parse_claude_usage_screen(screen_u, now, tz)
                self.assertTrue(p_u["ok"])
                self.assertIsNone(p_u["seven_day"])
                with tempfile.TemporaryDirectory() as tmp:
                    fp_status = Path(tmp) / "kvoot_claude.json"
                    fp_usage = Path(tmp) / "kvoot_claude_usage.json"
                    fp_usage.write_text(
                        json.dumps(
                            {
                                "written_at": now.isoformat(timespec="seconds"),
                                "five_hour": p_u["five_hour"],
                                "seven_day": p_u["seven_day"],
                                "reason_code": p_u["reason_code"],
                                "missing_windows": p_u["missing_windows"],
                            }
                        ),
                        encoding="utf-8",
                    )
                    rep_u = kvoot.build_quota_report(
                        codex_sessions_dir=FIXTURES_DIR,
                        claude_file=fp_status,
                        claude_usage_file=fp_usage,
                        now=now,
                    )
                    self.assertIsNone(rep_u["workers_allowed"]["claude"])
                    self.assertIsNone(rep_u["lead_allowed"]["claude"])
                    self.assertEqual(kvoot.check_permission(rep_u, "worker", "claude"), 3)

    def test_claude_o3_missing_window_fallback_and_unknown(self) -> None:
        tz = kvoot_config.tallinn_tz()
        now = datetime(2026, 11, 17, 6, 30, 0, tzinfo=timezone.utc)
        screen_1 = (
            "Current session\n10% used\n"
            "Weekly limit (all models)\n55% used\n"
            "Current week (Sonnet only)\n3% used\n"
        )
        parsed_1 = claude_usage.parse_claude_usage_screen(screen_1, now, tz)
        self.assertTrue(parsed_1["ok"])

        with tempfile.TemporaryDirectory() as tmp:
            fp_status = Path(tmp) / "kvoot_claude.json"
            fp_usage = Path(tmp) / "kvoot_claude_usage.json"

            with mock.patch.object(claude_usage, "_run_claude_usage_posix", return_value=parsed_1):
                with mock.patch.object(claude_usage, "_run_claude_usage_windows", return_value=parsed_1):
                    saved_res = claude_usage.probe_claude_usage(out_path=fp_usage, now=now)
            self.assertTrue(saved_res["ok"])
            usage_json = json.loads(fp_usage.read_text(encoding="utf-8"))
            self.assertEqual(usage_json.get("reason_code"), "missing_window")
            self.assertEqual(usage_json.get("missing_windows"), ["seven_day"])

            # (a) 10 min older statusline has 7d 55% -> taken from statusline, workers DENIED
            fp_status.write_text(
                json.dumps(
                    {
                        "written_at": (now - timedelta(minutes=10)).isoformat(timespec="seconds"),
                        "five_hour": {"used_percentage": 9.0, "resets_at": 1800000000},
                        "seven_day": {"used_percentage": 55.0, "resets_at": 1800500000},
                    }
                ),
                encoding="utf-8",
            )
            rep_a = kvoot.build_quota_report(
                codex_sessions_dir=FIXTURES_DIR,
                claude_file=fp_status,
                claude_usage_file=fp_usage,
                now=now,
            )
            c_a = rep_a["sources"]["claude"]
            self.assertTrue(c_a["available"])
            self.assertEqual(c_a["source_location"], "usage_cli")
            self.assertEqual(c_a["window_5h"]["used"], 10.0)
            self.assertEqual(c_a["window_5h"]["source_location"], "usage_cli")
            self.assertEqual(c_a["window_weekly"]["used"], 55.0)
            self.assertEqual(c_a["window_weekly"]["source_location"], "statusline")
            self.assertEqual(c_a["max"], 55.0)
            self.assertEqual(c_a["status"], "yellow")
            self.assertFalse(rep_a["workers_allowed"]["claude"])
            self.assertTrue(rep_a["lead_allowed"]["claude"])
            self.assertEqual(kvoot.check_permission(rep_a, "worker", "claude"), 1)
            self.assertEqual(c_a["note_code"], "missing_window")
            self.assertIn("Current week (all models)", c_a["note"])
            html_a = kvoot.render_html(rep_a, now=now).split("</style>", 1)[1].split('<script id="i18n-data"', 1)[0]
            self.assertIn("statusline, 10 min", html_a)
            self.assertIn("Fallback:", html_a)

            # (b) statusline 200 min old (> stale_minutes) -> 7d null, UNKNOWN, code 3
            fp_status.write_text(
                json.dumps(
                    {
                        "written_at": (now - timedelta(minutes=200)).isoformat(timespec="seconds"),
                        "five_hour": {"used_percentage": 9.0, "resets_at": 1800000000},
                        "seven_day": {"used_percentage": 55.0, "resets_at": 1800500000},
                    }
                ),
                encoding="utf-8",
            )
            rep_b = kvoot.build_quota_report(
                codex_sessions_dir=FIXTURES_DIR,
                claude_file=fp_status,
                claude_usage_file=fp_usage,
                now=now,
            )
            c_b = rep_b["sources"]["claude"]
            self.assertTrue(c_b["available"])
            self.assertIsNone(c_b["window_weekly"])
            self.assertEqual(c_b["status"], "unknown")
            self.assertIsNone(rep_b["workers_allowed"]["claude"])
            self.assertIsNone(rep_b["lead_allowed"]["claude"])
            self.assertEqual(kvoot.check_permission(rep_b, "worker", "claude"), 3)
            self.assertEqual(kvoot.check_permission(rep_b, "lead", "claude"), 3)
            self.assertEqual(
                kvoot.main(
                    ["--check", "worker", "--source", "claude", "--claude-file", str(fp_status)],
                    now=now,
                ),
                3,
            )

            # (c) both windows present -> no Fallback label
            fp_usage.write_text(
                json.dumps(
                    {
                        "written_at": now.isoformat(timespec="seconds"),
                        "five_hour": {"used_percentage": 10.0, "resets_at": 1800000000},
                        "seven_day": {"used_percentage": 22.0, "resets_at": 1800500000},
                    }
                ),
                encoding="utf-8",
            )
            rep_c = kvoot.build_quota_report(
                codex_sessions_dir=FIXTURES_DIR,
                claude_file=fp_status,
                claude_usage_file=fp_usage,
                now=now,
            )
            c_c = rep_c["sources"]["claude"]
            self.assertEqual(c_c["window_5h"]["source_location"], "usage_cli")
            self.assertEqual(c_c["window_weekly"]["source_location"], "usage_cli")
            self.assertEqual(c_c["max"], 22.0)
            self.assertEqual(c_c["status"], "green")
            self.assertTrue(rep_c["workers_allowed"]["claude"])
            self.assertEqual(kvoot.check_permission(rep_c, "worker", "claude"), 0)
            self.assertEqual(c_c["note_code"], "usage_cli")
            html_c = kvoot.render_html(rep_c, now=now).split("</style>", 1)[1].split('<script id="i18n-data"', 1)[0]
            self.assertNotIn("Fallback:", html_c)

    @unittest.skipIf(os.name == "nt", "POSIX PTY test runs on WSL/Linux")
    def test_claude_usage_pty_fake_binary_trust_and_privacy(self) -> None:
        fake_script = FIXTURES_DIR / "fake_claude.py"
        now = datetime(2026, 11, 17, 6, 30, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            wrapper = bin_dir / "claude"
            wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake_script}" "$@"\n', encoding="utf-8")
            wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

            stdin_log = tmp_path / "stdin.bin"
            env_log = tmp_path / "env.txt"
            out_usage = tmp_path / "kvoot_claude_usage.json"
            fake_home = tmp_path / "home"
            fake_home.mkdir()

            env_patch = {
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "HOME": str(fake_home),
                "CLAUDE_CODE_OAUTH_TOKEN": "secret-oauth-token-must-not-leak",
                "FAKE_CLAUDE_MODE": "trust_and_usage",
                "FAKE_CLAUDE_STDIN_LOG": str(stdin_log),
                "FAKE_CLAUDE_ENV_LOG": str(env_log),
            }
            with mock.patch.dict(os.environ, env_patch):
                res = claude_usage.probe_claude_usage(out_path=out_usage, timeout_s=8.0, now=now)

            self.assertTrue(res["ok"])
            self.assertTrue(out_usage.exists())
            saved = json.loads(out_usage.read_text(encoding="utf-8"))
            self.assertEqual(set(saved.keys()), {"written_at", "five_hour", "seven_day"})
            self.assertEqual(saved["five_hour"]["used_percentage"], 28.0)
            self.assertEqual(saved["seven_day"]["used_percentage"], 29.0)

            sent_bytes = stdin_log.read_bytes()
            self.assertIn(b"\x1b[B", sent_bytes)
            self.assertTrue(b"\r" in sent_bytes or b"\n" in sent_bytes)
            self.assertEqual(env_log.read_text(encoding="utf-8").strip(), "oauth_env=False")

            raw_saved = out_usage.read_text(encoding="utf-8")
            raw_res = json.dumps(res, ensure_ascii=False)
            for text in (raw_saved, raw_res):
                self.assertNotIn("salanimi@example.com", text)
                self.assertNotIn("Salajane Org", text)
                self.assertNotIn("Current session", text)

    @unittest.skipIf(os.name == "nt", "POSIX PTY test runs on WSL/Linux")
    def test_claude_usage_pty_timeout_and_v2_close(self) -> None:
        fake_script = FIXTURES_DIR / "fake_claude.py"
        now = datetime(2026, 11, 17, 6, 30, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            wrapper = bin_dir / "claude"
            wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake_script}" "$@"\n', encoding="utf-8")
            wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

            stdin_log = tmp_path / "stdin_timeout.bin"
            out_usage = tmp_path / "kvoot_claude_usage.json"
            fake_home = tmp_path / "home"
            fake_home.mkdir()

            env_patch = {
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "HOME": str(fake_home),
                "FAKE_CLAUDE_MODE": "timeout_other_dialog",
                "FAKE_CLAUDE_STDIN_LOG": str(stdin_log),
            }
            t0 = time.monotonic()
            with mock.patch.dict(os.environ, env_patch):
                res = claude_usage.probe_claude_usage(out_path=out_usage, timeout_s=1.2, now=now)
            elapsed = time.monotonic() - t0

            self.assertFalse(res["ok"])
            self.assertEqual(res.get("error_code"), "timeout")
            self.assertIn("1.2 s", res["reason"])
            self.assertGreaterEqual(elapsed, 1.1)
            self.assertLess(elapsed, 4.5)
            self.assertFalse(out_usage.exists())
            sent_bytes = stdin_log.read_bytes() if stdin_log.exists() else b""
            self.assertEqual(sent_bytes, b"")

    def test_claude_three_sources_freshest_wins(self) -> None:
        base = datetime(2026, 11, 17, 6, 0, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            fp_status = Path(tmp) / "kvoot_claude.json"
            fp_usage = Path(tmp) / "kvoot_claude_usage.json"
            fp_manual = Path(tmp) / "kvoot_claude_manual.json"

            def _write_c(fp: Path, dt: datetime, p5: float, p7: float) -> None:
                fp.write_text(
                    json.dumps(
                        {
                            "written_at": dt.isoformat(timespec="seconds"),
                            "five_hour": {"used_percentage": p5, "resets_at": 1800000000},
                            "seven_day": {"used_percentage": p7, "resets_at": 1800500000},
                        }
                    ),
                    encoding="utf-8",
                )

            _write_c(fp_status, base + timedelta(minutes=1), 10.0, 10.0)
            _write_c(fp_manual, base + timedelta(minutes=2), 20.0, 20.0)
            _write_c(fp_usage, base + timedelta(minutes=3), 33.0, 15.0)
            c1 = kvoot.read_claude_quota(
                claude_file=fp_status,
                claude_usage_file=fp_usage,
                claude_manual_file=fp_manual,
                now=base + timedelta(minutes=5),
            )
            self.assertEqual(c1["source_location"], "usage_cli")
            self.assertEqual(c1["max"], 33.0)

            _write_c(fp_status, base + timedelta(minutes=4), 44.0, 12.0)
            c2 = kvoot.read_claude_quota(
                claude_file=fp_status,
                claude_usage_file=fp_usage,
                claude_manual_file=fp_manual,
                now=base + timedelta(minutes=5),
            )
            self.assertEqual(c2["source_location"], "statusline")
            self.assertEqual(c2["max"], 44.0)

            _write_c(fp_manual, base + timedelta(minutes=5), 55.0, 18.0)
            c3 = kvoot.read_claude_quota(
                claude_file=fp_status,
                claude_usage_file=fp_usage,
                claude_manual_file=fp_manual,
                now=base + timedelta(minutes=5),
            )
            self.assertEqual(c3["source_location"], "manual")
            self.assertEqual(c3["max"], 55.0)

    def test_antigravity_cmdline_token_and_masking(self) -> None:
        tok = "secret_csrf_token_987654"
        parsed1 = antigravity_probe.parse_antigravity_cmdline(
            f"/home/alice/.antigravity-server/bin/2.32.1/language_server --csrf_token {tok} --extension_server_port 4444"
        )
        self.assertEqual(parsed1, (tok, 4444))

        parsed2 = antigravity_probe.parse_antigravity_cmdline(
            [
                "C:\\Program Files\\Antigravity\\language_server_windows_x64.exe",
                f"--csrf_token={tok}",
            ]
        )
        self.assertEqual(parsed2, (tok, None))

        self.assertIsNone(
            antigravity_probe.parse_antigravity_cmdline(
                "/home/alice/.antigravity-server/bin/2.32.1/language_server --app_data_dir antigravity"
            )
        )
        self.assertIsNone(
            antigravity_probe.parse_antigravity_cmdline(f"/usr/bin/python3 --csrf_token {tok}")
        )

        fake_proc = antigravity_probe.AntigravityProcess(pid=999, csrf_token=tok, koht="wsl")
        with mock.patch.object(antigravity_probe, "find_antigravity_process_linux", return_value=fake_proc):
            with mock.patch.object(antigravity_probe, "find_antigravity_process_windows", return_value=fake_proc):
                with mock.patch.object(antigravity_probe, "find_listening_ports_linux", return_value=[41633]):
                    with mock.patch.object(antigravity_probe, "find_listening_ports_windows", return_value=[41633]):
                        with mock.patch.object(
                            antigravity_probe,
                            "_post_localhost",
                            side_effect=RuntimeError(f"Connection failed with token {tok}"),
                        ):
                            res = kvoot.read_antigravity_quota()
        self.assertFalse(res["available"])
        self.assertNotIn(tok, res["reason"])
        self.assertIn("***", res["reason"])

        rep = kvoot.build_quota_report(antigravity_reader=lambda: res)
        dumped = json.dumps(rep, ensure_ascii=False)
        self.assertNotIn(tok, dumped)

        with mock.patch.object(antigravity_probe, "find_antigravity_process_linux", return_value=fake_proc):
            with mock.patch.object(antigravity_probe, "find_antigravity_process_windows", return_value=fake_proc):
                with mock.patch.object(
                    antigravity_probe,
                    "find_listening_ports_linux",
                    side_effect=RuntimeError(f"port error {tok}"),
                ):
                    with mock.patch.object(
                        antigravity_probe,
                        "find_listening_ports_windows",
                        side_effect=RuntimeError(f"port error {tok}"),
                    ):
                        res_v6 = kvoot.read_antigravity_quota()
        self.assertFalse(res_v6["available"])
        self.assertNotIn(tok, res_v6["reason"])
        self.assertIn("***", res_v6["reason"])

    def test_antigravity_o1_localhost_redirect_forbidden(self) -> None:
        tok = "SYNTH-REDIRECT-TOKEN-123"
        got_tokens: list[str | None] = []

        class TargetHandler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                got_tokens.append(self.headers.get("X-Codeium-Csrf-Token"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def do_POST(self) -> None:
                got_tokens.append(self.headers.get("X-Codeium-Csrf-Token"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

        target_srv = HTTPServer(("127.0.0.2", 0), TargetHandler)
        target_port = target_srv.server_address[1]

        class RedirectHandler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or "0")
                if length:
                    self.rfile.read(length)
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.2:{target_port}/hijacked")
                self.end_headers()

        redir_srv = HTTPServer(("127.0.0.1", 0), RedirectHandler)
        redir_port = redir_srv.server_address[1]

        for srv in (target_srv, redir_srv):
            threading.Thread(target=srv.serve_forever, daemon=True).start()

        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                antigravity_probe._post_localhost("http", redir_port, antigravity_probe.ENDPOINTS[0], tok)
            self.assertEqual(ctx.exception.code, 302)
            self.assertEqual(got_tokens, [])

            fake_proc = antigravity_probe.AntigravityProcess(
                pid=999,
                csrf_token=tok,
                extension_port=redir_port,
                koht="wsl",
            )
            with mock.patch.object(antigravity_probe, "find_antigravity_process_linux", return_value=fake_proc):
                with mock.patch.object(antigravity_probe, "find_antigravity_process_windows", return_value=fake_proc):
                    with mock.patch.object(antigravity_probe, "find_listening_ports_linux", return_value=[]):
                        with mock.patch.object(antigravity_probe, "find_listening_ports_windows", return_value=[]):
                            res = antigravity_probe.read_antigravity_quota()

            self.assertFalse(res["available"])
            self.assertIn("302", res["reason"])
            self.assertNotIn(tok, res["reason"])
            self.assertEqual(got_tokens, [])
        finally:
            redir_srv.shutdown()
            redir_srv.server_close()
            target_srv.shutdown()
            target_srv.server_close()

    def test_antigravity_synthetic_response_pools_and_max(self) -> None:
        now = datetime(2026, 11, 17, 8, 30, 0, tzinfo=timezone.utc)
        tz = kvoot_config.tallinn_tz()
        synthetic = {
            "response": {
                "groups": [
                    {
                        "displayName": "Gemini Models",
                        "buckets": [
                            {
                                "bucketId": "gemini-5h",
                                "window": "5h",
                                "remainingFraction": 0.90,
                                "resetTime": "2026-11-17T10:30:00Z",
                            },
                            {
                                "bucketId": "gemini-weekly",
                                "window": "weekly",
                                "remainingFraction": 0.95,
                                "resetTime": "2026-11-22T08:00:00Z",
                            },
                        ],
                    },
                    {
                        "displayName": "Claude and GPT models",
                        "buckets": [
                            {
                                "bucketId": "3p-5h",
                                "window": "5h",
                                "remainingFraction": 0.80,
                                "resetTime": "2026-11-17T11:30:00Z",
                            },
                            {
                                "bucketId": "3p-weekly",
                                "window": "weekly",
                                "remainingFraction": 1.0,
                                "resetTime": "2026-11-24T06:00:00Z",
                            },
                        ],
                    },
                    {
                        "displayName": "Gemini 4 Argon",
                        "buckets": [
                            {
                                "bucketId": "gemini-argon-wtu-weekly",
                                "window": "weekly",
                                "remainingFraction": 0.3866,
                                "resetTime": "2026-11-22T12:00:00Z",
                            }
                        ],
                    },
                ]
            }
        }
        parsed = antigravity_probe.parse_antigravity_response(
            synthetic,
            now,
            tz,
            endpoint="/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary",
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertTrue(parsed["available"])
        self.assertEqual(parsed["source_location"], "language_server")
        self.assertEqual(parsed["age_min"], 0)
        self.assertEqual(set(parsed["pools"].keys()), {"gemini", "claude_gpt", "argon"})
        self.assertEqual(parsed["pools"]["gemini"]["window_5h"]["used"], 10.0)
        self.assertEqual(parsed["pools"]["claude_gpt"]["window_5h"]["used"], 20.0)
        self.assertEqual(parsed["pools"]["argon"]["window_weekly"]["used"], 61.34)
        self.assertEqual(parsed["max"], 61.34)
        self.assertEqual(parsed["status"], "yellow")

    def test_antigravity_last_known_and_stale(self) -> None:
        t0 = datetime(2026, 11, 17, 8, 30, 0, tzinfo=timezone.utc)
        tok = "SECRET-AGY-CACHE-TOKEN-777"
        fake_port = 41633
        synthetic = {
            "response": {
                "groups": [
                    {
                        "displayName": "Gemini Models",
                        "buckets": [
                            {
                                "bucketId": "gemini-5h",
                                "window": "5h",
                                "remainingFraction": 0.90,
                                "resetTime": "2026-11-17T15:30:00Z",
                            },
                            {
                                "bucketId": "gemini-weekly",
                                "window": "weekly",
                                "remainingFraction": 0.95,
                                "resetTime": "2026-11-22T08:00:00Z",
                            },
                        ],
                    },
                    {
                        "displayName": "Gemini 4 Argon",
                        "buckets": [
                            {
                                "bucketId": "gemini-argon-wtu-weekly",
                                "window": "weekly",
                                "remainingFraction": 0.3866,
                                "resetTime": "2026-11-22T12:00:00Z",
                            }
                        ],
                    },
                ]
            }
        }

        with tempfile.TemporaryDirectory() as tmp:
            cache_file = Path(tmp) / "kvoot_antigravity_last.json"

            res_missing = antigravity_probe.read_antigravity_quota(
                now=t0,
                tz=kvoot_config.tallinn_tz(),
                cache_path=cache_file,
            )
            self.assertFalse(res_missing["available"])
            self.assertFalse(cache_file.exists())

            fake_proc = antigravity_probe.AntigravityProcess(
                pid=999,
                csrf_token=tok,
                extension_port=fake_port,
                koht="wsl",
            )
            with mock.patch.object(antigravity_probe, "find_antigravity_process_linux", return_value=fake_proc):
                with mock.patch.object(antigravity_probe, "find_antigravity_process_windows", return_value=fake_proc):
                    with mock.patch.object(antigravity_probe, "find_listening_ports_linux", return_value=[fake_port]):
                        with mock.patch.object(antigravity_probe, "find_listening_ports_windows", return_value=[fake_port]):
                            with mock.patch.object(
                                antigravity_probe,
                                "_query_process",
                                return_value=(synthetic, antigravity_probe.ENDPOINTS[0], None),
                            ):
                                res_live = antigravity_probe.read_antigravity_quota(
                                    now=t0,
                                    tz=kvoot_config.tallinn_tz(),
                                    cache_path=cache_file,
                                )
            self.assertTrue(res_live["available"])
            self.assertEqual(res_live["source_location"], "language_server")
            self.assertTrue(cache_file.exists())
            raw_cache = cache_file.read_text(encoding="utf-8")
            self.assertNotIn(tok, raw_cache)
            self.assertNotIn(str(fake_port), raw_cache)
            cached_json = json.loads(raw_cache)
            # I3: strict key check on cache JSON
            self.assertEqual(set(cached_json), {"written_at", "pools"})
            self.assertTrue(cached_json["pools"])
            for pool in cached_json["pools"].values():
                self.assertEqual(set(pool), {"name", "window_5h", "window_weekly"})
                for win_key in ("window_5h", "window_weekly"):
                    win = pool[win_key]
                    if win is not None:
                        self.assertTrue(set(win).issubset({"used", "resets_at", "reset"}))
                        self.assertIn("used", win)

            t_30 = t0 + timedelta(minutes=30)
            res_30 = antigravity_probe.read_antigravity_quota(
                now=t_30,
                tz=kvoot_config.tallinn_tz(),
                cache_path=cache_file,
            )
            self.assertTrue(res_30["available"])
            self.assertEqual(res_30["source_location"], "last_known")
            self.assertEqual(res_30["age_min"], 30)
            self.assertEqual(res_30["status"], "yellow")
            self.assertEqual(res_30["max"], 61.34)
            self.assertEqual(res_30["note_code"], "last_known_cache")
            rep_30 = kvoot.build_quota_report(
                codex_sessions_dir=FIXTURES_DIR,
                now=t_30,
                antigravity_reader=lambda: res_30,
            )
            self.assertFalse(rep_30["workers_allowed"]["antigravity"])
            self.assertTrue(rep_30["lead_allowed"]["antigravity"])
            self.assertEqual(kvoot.check_permission(rep_30, "worker", "antigravity"), 1)
            self.assertEqual(kvoot.check_permission(rep_30, "lead", "antigravity"), 0)

            t_200 = t0 + timedelta(minutes=200)
            res_200 = antigravity_probe.read_antigravity_quota(
                now=t_200,
                tz=kvoot_config.tallinn_tz(),
                cache_path=cache_file,
            )
            self.assertTrue(res_200["available"])
            self.assertEqual(res_200["source_location"], "last_known")
            self.assertEqual(res_200["age_min"], 200)
            self.assertEqual(res_200["status"], "stale")
            rep_200 = kvoot.build_quota_report(
                codex_sessions_dir=FIXTURES_DIR,
                now=t_200,
                antigravity_reader=lambda: res_200,
            )
            self.assertIsNone(rep_200["workers_allowed"]["antigravity"])
            self.assertIsNone(rep_200["lead_allowed"]["antigravity"])
            self.assertEqual(kvoot.check_permission(rep_200, "worker", "antigravity"), 3)
            self.assertEqual(kvoot.check_permission(rep_200, "lead", "antigravity"), 3)

            html_body = kvoot.render_html(rep_200, now=t_200).split("</style>", 1)[1]
            self.assertIn("stale, 3 h ago", html_body)
            self.assertIn("badge-stale", html_body)
            self.assertIn("fill-stale", html_body)
            self.assertIn("Gemini Models", html_body)
            self.assertIn("Gemini 4 Argon", html_body)
            self.assertIn("61.34%", html_body)
            self.assertIn("last_known", html_body)

    def test_all_one_source_fails_and_html_v4(self) -> None:
        now = datetime(2026, 11, 17, 6, 45, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            codex_dir = tmp_path / "codex"
            out_dir = tmp_path / "out"
            claude_status = tmp_path / "claude" / "kvoot_claude.json"
            claude_status.parent.mkdir(parents=True, exist_ok=True)
            _write_rollout(
                codex_dir,
                "2026/11/17/rollout-ok.jsonl",
                [self._codex_line("2026-11-17T06:40:00Z", 18.0, 9.0)],
            )

            anti_ok = {
                "available": True,
                "source_time": "2026-11-17T08:45:00+02:00",
                "source_location": "language_server",
                "age_min": 0,
                "window_5h": {"used": 5.0, "resets_at": "2026-11-17T11:00:00+02:00", "reset": False},
                "window_weekly": {"used": 12.0, "resets_at": "2026-11-22T11:00:00+02:00", "reset": False},
                "pools": {
                    "gemini": {
                        "name": "Gemini Models",
                        "window_5h": {"used": 5.0, "resets_at": "2026-11-17T11:00:00+02:00", "reset": False},
                        "window_weekly": None,
                        "max": 5.0,
                        "status": "green",
                    }
                },
                "max": 12.0,
                "status": "green",
                "note_code": "language_server",
                "note": "local language server",
            }

            with mock.patch.object(kvoot, "refresh_claude_usage", side_effect=RuntimeError("Claude probe broken")):
                with mock.patch.object(kvoot, "read_antigravity_quota", return_value=anti_ok):
                    code = kvoot.main(
                        [
                            "--all",
                            "--codex-dir",
                            str(codex_dir),
                            "--claude-file",
                            str(claude_status),
                            "--out-dir",
                            str(out_dir),
                        ],
                        now=now,
                    )
            self.assertEqual(code, 0)
            rep = json.loads((out_dir / "kvoot.json").read_text(encoding="utf-8"))
            self.assertEqual(rep["schema"], "kvoot/4")
            self.assertTrue(rep["sources"]["codex"]["available"])
            self.assertFalse(rep["sources"]["claude"]["available"])
            self.assertIn("Claude probe broken", rep["sources"]["claude"]["reason"])
            self.assertTrue(rep["sources"]["antigravity"]["available"])
            self.assertTrue(rep["workers_allowed"]["codex"])
            self.assertIsNone(rep["workers_allowed"]["claude"])
            self.assertTrue(rep["workers_allowed"]["antigravity"])

            claude_status.write_text(
                json.dumps(
                    {
                        "written_at": (now - timedelta(minutes=10)).isoformat(timespec="seconds"),
                        "five_hour": {"used_percentage": 21.0, "resets_at": 1800000000},
                        "seven_day": {"used_percentage": 14.0, "resets_at": 1800500000},
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(kvoot, "refresh_claude_usage", side_effect=RuntimeError("Claude probe broken")):
                with mock.patch.object(kvoot, "read_antigravity_quota", return_value=anti_ok):
                    code_v5 = kvoot.main(
                        [
                            "--all",
                            "--codex-dir",
                            str(codex_dir),
                            "--claude-file",
                            str(claude_status),
                            "--out-dir",
                            str(out_dir),
                        ],
                        now=now,
                    )
            self.assertEqual(code_v5, 0)
            rep_v5 = json.loads((out_dir / "kvoot.json").read_text(encoding="utf-8"))
            self.assertTrue(rep_v5["sources"]["claude"]["available"])
            self.assertEqual(rep_v5["sources"]["claude"]["source_location"], "statusline")
            self.assertEqual(rep_v5["sources"]["claude"]["max"], 21.0)
            self.assertEqual(rep_v5["sources"]["claude"]["note_code"], "usage_probe_failed")
            self.assertIn("Claude probe broken", rep_v5["sources"]["claude"]["note"])
            self.assertTrue(rep_v5["workers_allowed"]["claude"])

            html_txt = (out_dir / "kvoot.html").read_text(encoding="utf-8")
            self.assertIn('>updated</span> <span data-utc-hhmm="2026-11-17T06:45:00Z">08:45</span>', html_txt)
            self.assertIn("Source:", html_txt)
            self.assertIn("Age:", html_txt)
            self.assertIn("Gemini Models", html_txt)
            self.assertIn('data-theme="dark"', html_txt)

    def test_antigravity_cache_write_error_keeps_live_reading(self) -> None:
        t0 = datetime(2026, 11, 18, 6, 30, 0, tzinfo=timezone.utc)
        tok = "tok_v14_secret_xyz"
        fake_port = 43210
        synthetic = {
            "response": {
                "groups": [
                    {
                        "displayName": "Gemini 4 Argon",
                        "buckets": [
                            {
                                "bucketId": "gemini-argon-wtu-weekly",
                                "window": "weekly",
                                "remainingFraction": 0.4,
                                "resetTime": "2026-11-22T12:00:00Z",
                            }
                        ],
                    }
                ]
            }
        }
        fake_proc = antigravity_probe.AntigravityProcess(
            pid=998,
            csrf_token=tok,
            extension_port=fake_port,
            koht="wsl",
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            kodu_file = tmp_path / "home_is_file"
            kodu_file.write_text("not a dir\n", encoding="utf-8")
            codex_dir = tmp_path / "codex"
            _write_rollout(
                codex_dir,
                "2026/11/18/rollout-ok.jsonl",
                [self._codex_line("2026-11-18T06:25:00Z", 12.0, 8.0)],
            )
            claude_status = tmp_path / "kvoot_claude.json"
            claude_status.write_text(
                json.dumps(
                    {
                        "written_at": (t0 - timedelta(minutes=2)).isoformat(timespec="seconds"),
                        "five_hour": {"used_percentage": 10.0, "resets_at": 1800000000},
                        "seven_day": {"used_percentage": 20.0, "resets_at": 1800500000},
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.dict(os.environ, {"KVOOT_KODU": str(kodu_file)}):
                with mock.patch.object(antigravity_probe, "find_antigravity_process_linux", return_value=fake_proc):
                    with mock.patch.object(antigravity_probe, "find_antigravity_process_windows", return_value=fake_proc):
                        with mock.patch.object(antigravity_probe, "find_listening_ports_linux", return_value=[fake_port]):
                            with mock.patch.object(antigravity_probe, "find_listening_ports_windows", return_value=[fake_port]):
                                with mock.patch.object(
                                    antigravity_probe,
                                    "_query_process",
                                    return_value=(synthetic, antigravity_probe.ENDPOINTS[0], None),
                                ):
                                    res = antigravity_probe.read_antigravity_quota(
                                        now=t0,
                                        tz=kvoot_config.tallinn_tz(),
                                        cache_path=antigravity_probe.default_cache_path(),
                                    )
                                    rep = kvoot.build_quota_report(
                                        codex_sessions_dir=codex_dir,
                                        claude_file=claude_status,
                                        now=t0,
                                    )

            for item in (res, rep["sources"]["antigravity"]):
                self.assertTrue(item["available"])
                self.assertEqual(item["source_location"], "language_server")
                self.assertEqual(item["max"], 60.0)
                self.assertEqual(item["pools"]["argon"]["window_weekly"]["used"], 60.0)
                self.assertEqual(item["note_code"], "cache_write_failed")
                self.assertIn("could not save last reading", item["note"])
                self.assertNotIn("reason", item)
                serialized = json.dumps(item, ensure_ascii=False)
                self.assertNotIn(str(tmp_path), serialized)
                self.assertNotIn(kodu_file.name, serialized)
                self.assertNotIn(tok, serialized)

    def test_antigravity_windows_oem_bytes_and_wsl_fallback(self) -> None:
        tok = "tok_i5_oem_12345"
        raw_ps_bytes = (
            b'{"ProcessId":4321,"CommandLine":"C:\\\\Users\\\\alice\\\\J\x84rgen\\\\J\x81ri\\\\'
            b'antigravity\\\\language_server_windows_x64.exe --csrf_token '
            + tok.encode("ascii")
            + b' --extension_server_port 42111"}\r\n'
        )

        def _fake_ps_run(*_args, **kwargs):
            enc = kwargs.get("encoding") or "utf-8"
            err_mode = kwargs.get("errors") or "strict"
            decoded = raw_ps_bytes.decode(enc, errors=err_mode)
            return subprocess.CompletedProcess(args=["powershell.exe"], returncode=0, stdout=decoded, stderr="")

        self._anti_win_patch.stop()
        try:
            with mock.patch.object(antigravity_probe.subprocess, "run", side_effect=_fake_ps_run):
                proc = antigravity_probe.find_antigravity_process_windows()
        finally:
            self._anti_win_patch.start()
        self.assertIsNotNone(proc)
        assert proc is not None
        self.assertEqual(proc.pid, 4321)
        self.assertEqual(proc.csrf_token, tok)
        self.assertEqual(proc.extension_port, 42111)

        t0 = datetime(2026, 11, 18, 6, 30, 0, tzinfo=timezone.utc)
        synthetic = {
            "response": {
                "groups": [
                    {
                        "displayName": "Gemini 4 Argon",
                        "buckets": [
                            {
                                "bucketId": "gemini-argon-wtu-weekly",
                                "window": "weekly",
                                "remainingFraction": 0.3866,
                                "resetTime": "2026-11-22T12:00:00Z",
                            }
                        ],
                    }
                ]
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            cache_file = Path(tmp) / "kvoot_antigravity_last.json"
            with mock.patch.object(antigravity_probe, "_is_windows", return_value=True):
                with mock.patch.object(
                    antigravity_probe,
                    "find_antigravity_process_windows",
                    side_effect=RuntimeError("windows lookup failed"),
                ):
                    with mock.patch.object(
                        antigravity_probe,
                        "_query_wsl_from_windows",
                        return_value=(synthetic, antigravity_probe.ENDPOINTS[0], None, None),
                    ) as wsl_mock:
                        res = antigravity_probe.read_antigravity_quota(
                            now=t0,
                            tz=kvoot_config.tallinn_tz(),
                            cache_path=cache_file,
                        )
            wsl_mock.assert_called_once()
            self.assertTrue(res["available"])
            self.assertEqual(res["source_location"], "language_server")
            self.assertEqual(res["max"], 61.34)

    def test_v3_legacy_cache_and_state_dir_migration(self) -> None:
        t0 = datetime(2026, 11, 18, 6, 30, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            old_dir = home / ".tc-kvoot"
            old_dir.mkdir(parents=True)
            old_cache = old_dir / "kvoot_antigravity_viimane.json"
            old_cache.write_text(
                json.dumps(
                    {
                        "kirjutatud": (t0 - timedelta(minutes=15)).isoformat(),
                        "kogumid": {
                            "argon": {
                                "nimi": "Gemini 4 Argon",
                                "aken_5h": None,
                                "aken_nadal": {
                                    "kasutatud": 45.0,
                                    "lahtestub": "2026-11-22T12:00:00+02:00",
                                    "lahtestunud": False,
                                },
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            # Legacy state dir & Estonian-keyed cache is read when .kvoot does not exist yet
            with mock.patch.dict(os.environ, {"KVOOT_KODU": str(home), "KVOOT_STATE_DIR": ""}):
                self.assertEqual(kvoot_config.resolve_state_dir(for_write=False), old_dir)
                res_legacy = kvoot.read_antigravity_quota(now=t0)
            self.assertTrue(res_legacy["available"])
            self.assertEqual(res_legacy["source_location"], "last_known")
            self.assertEqual(res_legacy["max"], 45.0)
            self.assertEqual(res_legacy["status"], "green")
            self.assertEqual(res_legacy["pools"]["argon"]["window_weekly"]["used"], 45.0)

            # Next live write writes to ~/.kvoot/kvoot_antigravity_last.json in v4 keys
            synthetic = {
                "response": {
                    "groups": [
                        {
                            "displayName": "Gemini 4 Argon",
                            "buckets": [
                                {
                                    "bucketId": "gemini-argon-wtu-weekly",
                                    "window": "weekly",
                                    "remainingFraction": 0.4,
                                    "resetTime": "2026-11-22T12:00:00Z",
                                }
                            ],
                        }
                    ]
                }
            }
            fake_proc = antigravity_probe.AntigravityProcess(pid=111, csrf_token="tok_mig", extension_port=41111, koht="wsl")
            with mock.patch.dict(os.environ, {"KVOOT_KODU": str(home), "KVOOT_STATE_DIR": ""}):
                with mock.patch.object(antigravity_probe, "find_antigravity_process_linux", return_value=fake_proc):
                    with mock.patch.object(antigravity_probe, "find_antigravity_process_windows", return_value=fake_proc):
                        with mock.patch.object(antigravity_probe, "find_listening_ports_linux", return_value=[41111]):
                            with mock.patch.object(antigravity_probe, "find_listening_ports_windows", return_value=[41111]):
                                with mock.patch.object(
                                    antigravity_probe,
                                    "_query_process",
                                    return_value=(synthetic, antigravity_probe.ENDPOINTS[0], None),
                                ):
                                    res_new = kvoot.read_antigravity_quota(now=t0)
            self.assertTrue(res_new["available"])
            new_cache = home / ".kvoot" / "kvoot_antigravity_last.json"
            self.assertTrue(new_cache.exists())
            new_json = json.loads(new_cache.read_text(encoding="utf-8"))
            self.assertEqual(set(new_json), {"written_at", "pools"})
            self.assertEqual(set(new_json["pools"]["argon"]), {"name", "window_5h", "window_weekly"})

            # Legacy v3 Claude files (kirjutatud + kvoot_claude_kasitsi.json) are read seamlessly
            claude_dir = home / ".claude"
            claude_dir.mkdir(parents=True)
            fp_status = claude_dir / "kvoot_claude.json"
            fp_kasitsi = claude_dir / "kvoot_claude_kasitsi.json"
            fp_kasitsi.write_text(
                json.dumps(
                    {
                        "kirjutatud": (t0 - timedelta(minutes=5)).isoformat(),
                        "five_hour": {"used_percentage": 22.0, "resets_at": 1800000000},
                        "seven_day": {"used_percentage": 33.0, "resets_at": 1800500000},
                    }
                ),
                encoding="utf-8",
            )
            c_old = kvoot.read_claude_quota(claude_file=fp_status, now=t0)
            self.assertTrue(c_old["available"])
            self.assertEqual(c_old["source_location"], "manual")
            self.assertEqual(c_old["max"], 33.0)

    def test_i18n_completeness_fallback_and_new_lang(self) -> None:
        bundles = kvoot_i18n.available_languages()
        self.assertIn("en", bundles)
        self.assertIn("et", bundles)
        self.assertIn("fi", bundles)
        self.assertEqual(bundles["fi"]["_lang_name"], "Suomi")
        self.assertEqual(bundles["fi"]["app_title"], "Multi-AI käyttömittari")

        def _placeholders(tmpl: str) -> set[tuple[str, str, str]]:
            return {
                (fname, fspec or "", conv or "")
                for _, fname, fspec, conv in string.Formatter().parse(tmpl)
                if fname is not None
            }

        en_bundle = bundles["en"]
        en_keys = set(en_bundle.keys())
        for lang_code, bundle in bundles.items():
            self.assertEqual(set(bundle.keys()), en_keys, msg=f"Key mismatch in lang/{lang_code}.json")
            for k, en_val in en_bundle.items():
                self.assertEqual(
                    _placeholders(bundle[k]),
                    _placeholders(en_val),
                    msg=f"Placeholder mismatch in lang/{lang_code}.json for key {k!r}",
                )

        # CLI --lang fi selects Finnish for output without mutating global env (both when KVOOT_LANG is set and unset)
        now = datetime(2026, 11, 18, 8, 0, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out_fi"
            buf = io.StringIO()
            with mock.patch.dict(os.environ, {"KVOOT_LANG": "et"}):
                with mock.patch("sys.stdout", buf):
                    code = kvoot.main(
                        ["--lang", "fi", "--codex-dir", str(FIXTURES_DIR), "--out-dir", str(out_dir)],
                        now=now,
                    )
                self.assertEqual(os.environ.get("KVOOT_LANG"), "et")
            self.assertEqual(code, 0)
            self.assertIn("Päivitetty:", buf.getvalue())
            html_fi = (out_dir / "kvoot.html").read_text(encoding="utf-8")
            self.assertIn('<html lang="fi"', html_fi)
            self.assertIn("Multi-AI käyttömittari", html_fi)
            self.assertIn('"Suomi"', html_fi)

            # When KVOOT_LANG was initially unset, --lang must leave it unset afterwards
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("KVOOT_LANG", None)
                with mock.patch("sys.stdout", io.StringIO()):
                    code2 = kvoot.main(
                        ["--lang", "fi", "--codex-dir", str(FIXTURES_DIR), "--out-dir", str(out_dir)],
                        now=now,
                    )
                self.assertEqual(code2, 0)
                self.assertNotIn("KVOOT_LANG", os.environ)

        # Missing key in a partial language falls back to English without error
        with tempfile.TemporaryDirectory() as tmp:
            lang_dir = Path(tmp) / "lang"
            lang_dir.mkdir()
            (lang_dir / "en.json").write_text(
                json.dumps({"_lang_name": "English", "app_title": "Multi-AI Quota Monitor", "hello": "Hello {name}"}),
                encoding="utf-8",
            )
            (lang_dir / "et.json").write_text(
                json.dumps({"_lang_name": "Eesti", "app_title": "Mitme AI kvoodimõõdik", "hello": "Tere {name}"}),
                encoding="utf-8",
            )
            (lang_dir / "de.json").write_text(
                json.dumps({"_lang_name": "Deutsch", "app_title": "KI-Quotenmonitor"}),
                encoding="utf-8",
            )
            loaded = kvoot_i18n.available_languages(lang_dir)
            self.assertIn("de", loaded)
            self.assertEqual(kvoot_i18n.t("app_title", lang="de", lang_dir=lang_dir), "KI-Quotenmonitor")
            self.assertEqual(kvoot_i18n.t("hello", lang="de", lang_dir=lang_dir, name="Alice"), "Hello Alice")

    def test_i18n_html_and_json_key_invariance(self) -> None:
        now = datetime(2026, 11, 15, 8, 30, 0, tzinfo=timezone.utc)
        with mock.patch.dict(os.environ, {"KVOOT_LANG": "en"}):
            rep_en = kvoot.build_quota_report(codex_sessions_dir=FIXTURES_DIR, now=now)
            html_en = kvoot.render_html(rep_en, now=now)
        with mock.patch.dict(os.environ, {"KVOOT_LANG": "et"}):
            rep_et = kvoot.build_quota_report(codex_sessions_dir=FIXTURES_DIR, now=now)
            html_et = kvoot.render_html(rep_et, now=now)

        # JSON keys do not change with language
        self.assertEqual(list(rep_en.keys()), list(rep_et.keys()))
        self.assertEqual(list(rep_en["sources"]["codex"].keys()), list(rep_et["sources"]["codex"].keys()))
        self.assertEqual(
            list(rep_en["sources"]["codex"]["window_5h"].keys()),
            list(rep_et["sources"]["codex"]["window_5h"].keys()),
        )
        self.assertEqual(rep_en["sources"]["codex"]["status"], rep_et["sources"]["codex"]["status"])
        self.assertEqual(rep_en["sources"]["codex"]["note_code"], rep_et["sources"]["codex"]["note_code"])
        self.assertNotEqual(rep_en["sources"]["codex"]["note"], rep_et["sources"]["codex"]["note"])

        # HTML reflects active language and includes all languages + try-wrapped localStorage
        self.assertIn('<html lang="en"', html_en)
        self.assertIn("Multi-AI Quota Monitor", html_en)
        self.assertIn("Toggle theme", html_en)
        self.assertIn('<html lang="et"', html_et)
        self.assertIn("Mitme AI kvoodimõõdik", html_et)
        self.assertIn("Vaheta teemat", html_et)
        self.assertIn("try { localStorage", html_et)
        self.assertIn('"English"', html_et)
        self.assertIn('"Eesti"', html_et)

    def test_cli_english_and_estonian_aliases(self) -> None:
        now = datetime(2026, 11, 18, 8, 0, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            codex_dir = tmp_path / "codex"
            _write_rollout(
                codex_dir,
                "2026/11/18/rollout-1.jsonl",
                [self._codex_line((now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"), 55.0, 20.0)],
            )
            claude_status = tmp_path / "kvoot_claude.json"
            claude_status.write_text(
                json.dumps(
                    {
                        "written_at": (now - timedelta(minutes=5)).isoformat(),
                        "five_hour": {"used_percentage": 30.0, "resets_at": 1800000000},
                        "seven_day": {"used_percentage": 20.0, "resets_at": 1800500000},
                    }
                ),
                encoding="utf-8",
            )

            # Estonian CLI aliases & exit codes 0/1/2/3
            self.assertEqual(
                kvoot.main(["--kontroll", "tooline", "--allikas", "codex", "--codex-dir", str(codex_dir)], now=now),
                1,
            )
            self.assertEqual(
                kvoot.main(["--kontroll", "juht", "--allikas", "codex", "--codex-dir", str(codex_dir)], now=now),
                0,
            )
            self.assertEqual(
                kvoot.main(["--kontroll", "tooline", "--allikas", "claude", "--claude-file", str(claude_status)], now=now),
                0,
            )
            self.assertEqual(
                kvoot.main(["--kontroll", "tooline", "--allikas", "antigravity"], now=now),
                2,
            )
            self.assertEqual(
                kvoot.main(
                    ["--kontroll", "tooline", "--allikas", "codex", "--codex-dir", str(codex_dir)],
                    now=now + timedelta(minutes=200),
                ),
                3,
            )

            # Estonian --sisesta / --7p / --5h-lahtestub / --7p-lahtestub aliases
            code_set = kvoot.main(
                [
                    "--sisesta",
                    "claude",
                    "--5h",
                    "12",
                    "--7p",
                    "34",
                    "--5h-lahtestub",
                    "14:00",
                    "--7p-lahtestub",
                    "2026-11-20T15:00:00+02:00",
                    "--claude-file",
                    str(claude_status),
                ],
                now=now,
            )
            self.assertEqual(code_set, 0)

    def test_config_limits_tz_title_and_state_dir(self) -> None:
        now = datetime(2026, 11, 15, 8, 30, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg_file = tmp_path / "kvoot.config.json"
            state_dir = tmp_path / "custom_state"
            cfg_file.write_text(
                json.dumps(
                    {
                        "worker_limit": 30,
                        "lead_limit": 60,
                        "timezone": "UTC",
                        "title": "My Custom Monitor",
                        "state_dir": str(state_dir),
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"KVOOT_CONFIG": str(cfg_file), "KVOOT_TZ": ""}):
                rep = kvoot.build_quota_report(codex_sessions_dir=FIXTURES_DIR, now=now)
                self.assertEqual(rep["limits"], {"worker": 30.0, "lead": 60.0})
                self.assertTrue(rep["read_at"].endswith("+00:00"))
                self.assertEqual(kvoot_config.resolve_state_dir(), state_dir)
                html_str = kvoot.render_html(rep, now=now)
                self.assertIn("My Custom Monitor", html_str)
                self.assertIn("left: 30%", html_str)
                self.assertIn("left: 60%", html_str)

                # KVOOT_TITLE env overrides config title
                with mock.patch.dict(os.environ, {"KVOOT_TITLE": "Env Monitor", "KVOOT_WORKER_LIMIT": "45"}):
                    rep2 = kvoot.build_quota_report(codex_sessions_dir=FIXTURES_DIR, now=now)
                    self.assertEqual(rep2["limits"]["worker"], 45.0)
                    self.assertIn("Env Monitor", kvoot.render_html(rep2, now=now))
                    self.assertEqual(kvoot_config.resolve_title_override(), "Env Monitor")
                    self.assertTrue(callable(kvoot_ava._show_error))

    def test_public_export_clean(self) -> None:
        export_script = ROOT / "tools" / "export_public.py"
        self.assertTrue(export_script.exists())

        forbidden_words = [
            "st" + "eve",
            "leno" + "vog",
            "tc-" + "hq",
            "leg" + "ion",
            "night" + "owl",
            "tel" + "os",
            "jää" + "rion",
            "jaar" + "ion",
            "calv" + "ion",
        ]
        forbidden_code_re = re.compile(r"\bd-0\d{2}\b", re.IGNORECASE)
        personal_win_path_re = re.compile(
            r"(?:/mnt/[a-z]/users/|[a-z]:\\+users\\+)([a-z0-9._-]+)",
            re.IGNORECASE,
        )

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "public_repo"
            proc = subprocess.run(
                [sys.executable, str(export_script), "--out", str(out_dir)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)

            # Required public files and export marker are present
            for req in (
                "kvoot.py",
                "kvoot_config.py",
                "kvoot_i18n.py",
                "antigravity_probe.py",
                "claude_usage.py",
                "claude_statusline.py",
                "kvoot_ava.pyw",
                "paigalda_ikoon.ps1",
                "lang/en.json",
                "lang/et.json",
                "lang/fi.json",
                "tests/test_kvoot.py",
                "README.md",
                "README.et.md",
                "LICENSE",
                "NOTICE",
                "LICENSES/Apache-2.0.txt",
                ".gitignore",
                ".kvoot-export",
            ):
                self.assertTrue((out_dir / req).exists(), msg=f"Missing exported file: {req}")

            self.assertEqual(
                (out_dir / "LICENSE").read_text(encoding="utf-8"),
                (ROOT / "LICENSES" / "Apache-2.0.txt").read_text(encoding="utf-8"),
            )

            # Only allowlisted top-level entries are exported (no internal docs/logs/configs leak)
            allowed_top = {
                ".gitignore",
                ".kvoot-export",
                "LICENSE",
                "NOTICE",
                "README.md",
                "README.et.md",
                "antigravity_probe.py",
                "claude_statusline.py",
                "claude_usage.py",
                "gen_ico.py",
                "kvoot.ico",
                "kvoot.py",
                "kvoot_ava.pyw",
                "kvoot_config.py",
                "kvoot_i18n.py",
                "paigalda_ikoon.ps1",
                "LICENSES",
                "tools",
                "lang",
                "tests",
            }
            exported_top = {p.name for p in out_dir.iterdir()}
            self.assertEqual(exported_top, allowed_top)
            md_files = {p.relative_to(out_dir).as_posix() for p in out_dir.rglob("*.md")}
            self.assertEqual(md_files, {"README.md", "README.et.md"})

            # No forbidden words or personal Windows paths anywhere in export
            # (allow only the exact public GitHub org repository clone URL)
            public_repo_url_low = "https://github.com/t" + "calv" + "ion/multi-ai-quota-monitor.git"
            for fp in sorted(out_dir.rglob("*")):
                if not fp.is_file():
                    continue
                if fp.suffix in (".ico", ".png"):
                    continue
                text = fp.read_text(encoding="utf-8", errors="replace")
                low = text.lower().replace(public_repo_url_low, "")
                for word in forbidden_words:
                    self.assertNotIn(word, low, msg=f"Forbidden token {word!r} in {fp.relative_to(out_dir)}")
                self.assertIsNone(
                    forbidden_code_re.search(text),
                    msg=f"Forbidden decision id in {fp.relative_to(out_dir)}",
                )
                for m in personal_win_path_re.finditer(text):
                    self.assertEqual(
                        m.group(1).lower(),
                        "alice",
                        msg=f"Personal user path {m.group(0)!r} in {fp.relative_to(out_dir)}",
                    )
                if fp.suffix in (".py", ".pyw"):
                    py_compile.compile(str(fp), doraise=True)

    def test_v18_i18n_xss_and_json_escape(self) -> None:
        now = datetime(2026, 11, 18, 9, 0, 0, tzinfo=timezone.utc)
        payloads = [
            "<img src=x onerror=alert(1)>",
            "</script><script>alert(1)</script>",
            "<!--<script>",
            "<strong><img src=x onerror=alert(2)></strong>",
        ]
        en_bundle = json.loads((ROOT / "lang" / "en.json").read_text(encoding="utf-8"))
        for payload in payloads:
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as tmp:
                    lang_dir = Path(tmp) / "lang"
                    lang_dir.mkdir()
                    (lang_dir / "en.json").write_text(
                        json.dumps(en_bundle, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    malicious = {k: payload for k in en_bundle}
                    (lang_dir / "xx.json").write_text(
                        json.dumps(malicious, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    with mock.patch.object(kvoot_i18n, "LANG_DIR", lang_dir):
                        with mock.patch.dict(os.environ, {"KVOOT_LANG": "xx"}):
                            rep = kvoot.build_quota_report(codex_sessions_dir=FIXTURES_DIR, now=now)
                            rendered = kvoot.render_html(rep, now=now)

                    self.assertNotIn("innerHTML", rendered)
                    self.assertNotIn("data-i18n-html", rendered)

                    # Extract JSON script block and verify zero literal '<' inside it
                    m_json = re.search(
                        r'<script id="i18n-data" type="application/json">(.*?)</script>',
                        rendered,
                        flags=re.DOTALL,
                    )
                    self.assertIsNotNone(m_json)
                    assert m_json is not None
                    json_block = m_json.group(1)
                    self.assertNotIn("<", json_block)
                    parsed_bundles = json.loads(json_block)
                    self.assertEqual(parsed_bundles["xx"]["html_legend"], payload)

                    # Check rendered body outside script/style contains no executable tags from payload
                    body_part = rendered.split("</style>", 1)[1].split('<script id="i18n-data"', 1)[0]
                    self.assertNotIn("<img", body_part.lower())
                    self.assertNotIn("<script", body_part.lower())
                    self.assertNotIn("<!--<script", body_part.lower())

    def test_v16_export_public_force_requires_marker(self) -> None:
        export_script = ROOT / "tools" / "export_public.py"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            unmarked_dir = tmp_path / "important_user_dir"
            unmarked_dir.mkdir()
            important = unmarked_dir / "oluline.txt"
            important.write_text("do not delete\n", encoding="utf-8")

            proc_fail = subprocess.run(
                [sys.executable, str(export_script), "--out", str(unmarked_dir), "--force"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(proc_fail.returncode, 0)
            self.assertIn(".kvoot-export", proc_fail.stderr)
            self.assertTrue(important.exists())
            self.assertEqual(important.read_text(encoding="utf-8"), "do not delete\n")

            # Forged empty marker must NOT allow wiping directory
            forged_dir = tmp_path / "forged_marker_dir"
            forged_dir.mkdir()
            (forged_dir / ".kvoot-export").write_text("", encoding="utf-8")
            forged_keep = forged_dir / "oluline.txt"
            forged_keep.write_text("keep\n", encoding="utf-8")
            proc_forged = subprocess.run(
                [sys.executable, str(export_script), "--out", str(forged_dir), "--force"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(proc_forged.returncode, 0)
            self.assertTrue(forged_keep.exists())

            # Manifest-based --force deletes only files listed in marker manifest and keeps foreign files with warning
            marked_dir = tmp_path / "previous_export"
            proc_init = subprocess.run(
                [sys.executable, str(export_script), "--out", str(marked_dir)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_init.returncode, 0, msg=proc_init.stderr)
            marker_file = marked_dir / ".kvoot-export"
            self.assertTrue(marker_file.is_file())
            manifest = json.loads(marker_file.read_text(encoding="utf-8"))
            self.assertIn("kvoot.py", manifest["files"])

            # Simulate an obsolete exported file listed in old manifest + a foreign file NOT in manifest
            obsolete_manifest_file = marked_dir / "old_dir" / "old_exported.txt"
            obsolete_manifest_file.parent.mkdir(parents=True)
            obsolete_manifest_file.write_text("old export artifact\n", encoding="utf-8")
            manifest["files"].append("old_dir/old_exported.txt")
            marker_file.write_text(json.dumps(manifest), encoding="utf-8")

            foreign_extra = marked_dir / "foreign_extra.txt"
            foreign_extra.write_text("user file\n", encoding="utf-8")

            proc_force = subprocess.run(
                [sys.executable, str(export_script), "--out", str(marked_dir), "--force"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_force.returncode, 0, msg=proc_force.stderr)
            self.assertFalse(obsolete_manifest_file.exists())
            self.assertTrue(foreign_extra.exists())
            self.assertIn("foreign_extra.txt", proc_force.stderr)
            self.assertTrue((marked_dir / ".kvoot-export").is_file())
            self.assertTrue((marked_dir / "kvoot.py").is_file())

            # V20: --force MUST refuse if target is a git repository (.git intact, exit != 0)
            git_dir = tmp_path / "git_export"
            proc_ginit = subprocess.run(
                [sys.executable, str(export_script), "--out", str(git_dir)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_ginit.returncode, 0, msg=proc_ginit.stderr)
            subprocess.run(["git", "init", "-q"], cwd=git_dir, check=True)
            subprocess.run(["git", "add", "-A"], cwd=git_dir, check=True)
            subprocess.run(
                ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-qm", "init"],
                cwd=git_dir,
                check=True,
            )
            proc_git_force = subprocess.run(
                [sys.executable, str(export_script), "--out", str(git_dir), "--force"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(proc_git_force.returncode, 0)
            self.assertIn("Target is a git repository. Export to a fresh folder and copy the files.", proc_git_force.stderr)
            self.assertTrue((git_dir / ".git").is_dir())
            self.assertTrue((git_dir / "kvoot.py").is_file())

            # V20 + I18: .git as a FILE (worktree/submodule) must also be refused
            git_file_dir = tmp_path / "git_file_export"
            proc_gfinit = subprocess.run(
                [sys.executable, str(export_script), "--out", str(git_file_dir)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_gfinit.returncode, 0, msg=proc_gfinit.stderr)
            (git_file_dir / ".git").write_text("gitdir: ../outside_git\n", encoding="utf-8")
            proc_gf_force = subprocess.run(
                [sys.executable, str(export_script), "--out", str(git_file_dir), "--force"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(proc_gf_force.returncode, 0)
            self.assertIn("Target is a git repository. Export to a fresh folder and copy the files.", proc_gf_force.stderr)
            self.assertTrue((git_file_dir / ".git").is_file())

            # V20 + I18 + I21: manifest path attacks ('../', absolute path, 'sub/.Git/HEAD') must fail without deleting files
            outside_secret = tmp_path / "outside_secret.txt"
            outside_secret.write_text("secret\n", encoding="utf-8")
            abs_secret = tmp_path / "abs_secret.txt"
            abs_secret.write_text("abs secret\n", encoding="utf-8")

            bad_manifest_cases = [
                ("traversal", "../outside_secret.txt", "escapes target"),
                ("absolute", str(abs_secret), "Unsafe manifest path"),
                ("nested_git_case", "sub/.Git/HEAD", "Unsafe manifest path"),
            ]
            for label, bad_entry, exp_err in bad_manifest_cases:
                with self.subTest(manifest_attack=label):
                    atk_dir = tmp_path / f"atk_{label}"
                    proc_atk_init = subprocess.run(
                        [sys.executable, str(export_script), "--out", str(atk_dir)],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(proc_atk_init.returncode, 0, msg=proc_atk_init.stderr)
                    if label == "nested_git_case":
                        nested_head = atk_dir / "sub" / ".Git" / "HEAD"
                        nested_head.parent.mkdir(parents=True, exist_ok=True)
                        nested_head.write_text("ref: refs/heads/main\n", encoding="utf-8")
                    atk_marker = atk_dir / ".kvoot-export"
                    atk_manifest = json.loads(atk_marker.read_text(encoding="utf-8"))
                    atk_manifest["files"].append(bad_entry)
                    atk_marker.write_text(json.dumps(atk_manifest), encoding="utf-8")

                    proc_atk = subprocess.run(
                        [sys.executable, str(export_script), "--out", str(atk_dir), "--force"],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotEqual(proc_atk.returncode, 0)
                    self.assertIn(exp_err, proc_atk.stderr)
                    self.assertTrue(outside_secret.exists())
                    self.assertTrue(abs_secret.exists())
                    if label == "nested_git_case":
                        self.assertTrue((atk_dir / "sub" / ".Git" / "HEAD").is_file())

    def test_v17_win_home_ambiguous_returns_none_with_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            users_root = Path(tmp) / "Users"
            (users_root / "alice" / ".claude").mkdir(parents=True)
            (users_root / "bob" / ".codex").mkdir(parents=True)

            kvoot_config.clear_config_notes()
            with mock.patch.dict(os.environ, {"KVOOT_WIN_HOME": "", "WSL_DISTRO_NAME": "Ubuntu"}):
                with mock.patch.object(kvoot_config, "_is_windows", return_value=False):
                    res = kvoot_config.resolve_win_home_wsl(users_root=users_root)
            self.assertIsNone(res)
            notes = kvoot_config.get_config_notes()
            self.assertTrue(any(n["code"] == "win_home_ambiguous" for n in notes))
            rendered_note = kvoot_i18n.t(
                "cfg_note_win_home_ambiguous",
                **next(n["args"] for n in notes if n["code"] == "win_home_ambiguous"),
            )
            self.assertIn("KVOOT_WIN_HOME", rendered_note)

            # Single AI-marked profile still resolves normally without ambiguous note
            single_root = Path(tmp) / "UsersSingle"
            (single_root / "alice" / ".codex").mkdir(parents=True)
            (single_root / "carol").mkdir(parents=True)
            kvoot_config.clear_config_notes()
            with mock.patch.dict(os.environ, {"KVOOT_WIN_HOME": "", "WSL_DISTRO_NAME": "Ubuntu"}):
                with mock.patch.object(kvoot_config, "_is_windows", return_value=False):
                    res_single = kvoot_config.resolve_win_home_wsl(users_root=single_root)
            self.assertEqual(res_single, single_root / "alice")
            self.assertEqual(kvoot_config.get_config_notes(), [])

    def test_v19_timezone_unavailable_fallback_and_bogus(self) -> None:
        from zoneinfo import ZoneInfoNotFoundError

        # 1) KVOOT_TZ=America/New_York without tzdata -> timezone_unavailable + UTC (not +02:00)
        kvoot_config.clear_config_notes()
        with mock.patch.dict(os.environ, {"KVOOT_TZ": "America/New_York"}):
            with mock.patch.object(kvoot_config, "ZoneInfo", side_effect=ZoneInfoNotFoundError("no tzdata")):
                tz = kvoot_config.resolve_tz()
        self.assertEqual(tz, timezone.utc)
        notes = kvoot_config.get_config_notes()
        self.assertTrue(any(n["code"] == "timezone_unavailable" for n in notes))

        # 2) KVOOT_TZ=Bogus/Zone -> timezone_unavailable + UTC
        kvoot_config.clear_config_notes()
        with mock.patch.dict(os.environ, {"KVOOT_TZ": "Bogus/Zone"}):
            tz_bogus = kvoot_config.resolve_tz()
        self.assertEqual(tz_bogus, timezone.utc)
        self.assertTrue(any(n["code"] == "timezone_unavailable" for n in kvoot_config.get_config_notes()))

        # 2b) V19-c: TZ=Bogus/Zone (not KVOOT_TZ) -> timezone_unavailable + UTC (not silent local offset)
        kvoot_config.clear_config_notes()
        with mock.patch.dict(os.environ, {"KVOOT_TZ": "", "TZ": "Bogus/Zone"}):
            tz_env_bogus = kvoot_config.resolve_tz()
        self.assertEqual(tz_env_bogus, timezone.utc)
        self.assertTrue(any(n["code"] == "timezone_unavailable" for n in kvoot_config.get_config_notes()))

        # 3) Europe/Tallinn without tzdata -> _TallinnTZ without warning
        kvoot_config.clear_config_notes()
        with mock.patch.dict(os.environ, {"KVOOT_TZ": "Europe/Tallinn"}):
            with mock.patch.object(kvoot_config, "ZoneInfo", side_effect=ZoneInfoNotFoundError("no tzdata")):
                tz_tln = kvoot_config.resolve_tz()
        self.assertIsInstance(tz_tln, kvoot_config._TallinnTZ)
        self.assertEqual(kvoot_config.get_config_notes(), [])

        # 4) Windows without KVOOT_TZ/config/TZ -> timezone_windows_fixed warning (safe on Python 3.10)
        kvoot_config.clear_config_notes()
        with mock.patch.dict(os.environ, {"KVOOT_TZ": "", "TZ": ""}):
            with mock.patch.object(kvoot_config, "_is_windows", return_value=True):
                kvoot_config.resolve_tz()
        self.assertTrue(any(n["code"] == "timezone_windows_fixed" for n in kvoot_config.get_config_notes()))

    def test_i14_tz_colon_and_posix_rule(self) -> None:
        # 1) Leading ':' in TZ (e.g. TZ=":Europe/Tallinn") strips ':' and preserves DST
        kvoot_config.clear_config_notes()
        with mock.patch.dict(os.environ, {"KVOOT_TZ": "", "TZ": ":Europe/Tallinn"}):
            tz_colon = kvoot_config.resolve_tz()
        self.assertEqual(kvoot_config.get_config_notes(), [])
        self.assertEqual(datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc).astimezone(tz_colon).utcoffset(), timedelta(hours=3))
        self.assertEqual(datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc).astimezone(tz_colon).utcoffset(), timedelta(hours=2))

        # 2) POSIX rule TZ="EET-2EEST,M3.5.0/3,M10.5.0/4" resolves via pure-Python parser on both POSIX and Windows
        #    (never falls back to /etc/localtime and never touches process-global libc TZ)
        kvoot_config.clear_config_notes()
        with mock.patch.dict(os.environ, {"KVOOT_TZ": "", "TZ": "EET-2EEST,M3.5.0/3,M10.5.0/4"}):
            with mock.patch.object(kvoot_config, "_detect_posix_system_tz", return_value=None):
                tz_posix = kvoot_config.resolve_tz()
        self.assertEqual(kvoot_config.get_config_notes(), [])
        self.assertEqual(datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc).astimezone(tz_posix).utcoffset(), timedelta(hours=3))
        self.assertEqual(datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc).astimezone(tz_posix).utcoffset(), timedelta(hours=2))

        # 3) Unparseable POSIX-like TZ fails closed to UTC + timezone_unavailable (never guesses)
        for bad_tz in (
            "EET-2EEST",
            "EET-2EEST,M13.5.0/3,M10.5.0/4",
            "EET-2EEST,J0/2,J300/2",
            "EET-24",
            "UT0",
            "<+3>-3",
            ":EET-2EEST,M3.5.0/3,M10.5.0/4",
        ):
            with self.subTest(bad_tz=bad_tz):
                kvoot_config.clear_config_notes()
                with mock.patch.dict(os.environ, {"KVOOT_TZ": "", "TZ": bad_tz}):
                    tz_bad = kvoot_config.resolve_tz()
                self.assertEqual(tz_bad, timezone.utc)
                self.assertTrue(any(n["code"] == "timezone_unavailable" for n in kvoot_config.get_config_notes()))

    def test_i7_readme_codes_match_code_and_wsl_probe_code(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        def _extract_table_codes(heading: str) -> set[str]:
            section = readme.split(heading, 1)[1].split("\n### ", 1)[0]
            return set(re.findall(r"^\|\s*`([a-z0-9_]+)`\s*\|", section, flags=re.MULTILINE))

        readme_note_codes = _extract_table_codes("### `note_code` Values")
        readme_reason_codes = _extract_table_codes("### `reason_code` Values")

        # Expected note codes emitted in code
        code_note_codes = {
            "local_sessions",
            "partial_sources",
            "usage_cli",
            "statusline",
            "manual_entry",
            "missing_window",
            "usage_probe_failed",
            "language_server",
            "last_known_cache",
            "cache_write_failed",
        }
        # Config warning codes recorded via _record_note(...)
        cfg_src = (ROOT / "kvoot_config.py").read_text(encoding="utf-8")
        code_note_codes.update(re.findall(r'_record_note\(\s*"([a-z0-9_]+)"', cfg_src))
        self.assertEqual(readme_note_codes, code_note_codes)

        # Both English and Estonian READMEs must note Windows CRT independence for POSIX TZ parsing
        readme_et = (ROOT / "README.et.md").read_text(encoding="utf-8")
        self.assertIn("Windows CRT", readme)
        self.assertIn("US DST", readme)
        self.assertIn("Windowsi CRT", readme_et)
        self.assertIn("USA reegleid", readme_et)

        # Reason / error codes emitted across kvoot.py, claude_usage.py, antigravity_probe.py
        kvoot_src = (ROOT / "kvoot.py").read_text(encoding="utf-8")
        claude_src = (ROOT / "claude_usage.py").read_text(encoding="utf-8")
        anti_src = (ROOT / "antigravity_probe.py").read_text(encoding="utf-8")

        code_reason_codes = set()
        code_reason_codes.update(re.findall(r'reason_code\s*=\s*"([a-z0-9_]+)"', kvoot_src))
        code_reason_codes.update(re.findall(r'"reason_code":\s*"([a-z0-9_]+)"', kvoot_src))
        code_reason_codes.update(re.findall(r'usage_probe_error_code\s*or\s*"([a-z0-9_]+)"', kvoot_src))
        code_reason_codes.update(re.findall(r'error_code"\)\s*or\s*"([a-z0-9_]+)"', kvoot_src))
        code_reason_codes.update(re.findall(r'_fail\(\s*"([a-z0-9_]+)"', claude_src))
        code_reason_codes.update(re.findall(r'reason_code\s*=\s*"([a-z0-9_]+)"', anti_src))
        code_reason_codes.update(re.findall(r'"reason_code":\s*"([a-z0-9_]+)"', anti_src))
        # missing_window is a note_code in kvoot/4 (reason_code only inside intermediate claude_usage cache)
        code_reason_codes.discard("missing_window")

        self.assertEqual(readme_reason_codes, code_reason_codes)

        # WSL spawn failure & non-zero exit must NOT report pty_unsupported
        with mock.patch.object(claude_usage.subprocess, "run", side_effect=OSError("no wsl")):
            res_exec = claude_usage._run_claude_usage_windows(timeout_s=2.0)
        self.assertFalse(res_exec["ok"])
        self.assertEqual(res_exec["error_code"], "wsl_exec_failed")

        with mock.patch.object(
            claude_usage.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(["wsl.exe"], 1, stdout="", stderr="err"),
        ):
            res_exit = claude_usage._run_claude_usage_windows(timeout_s=2.0)
        self.assertFalse(res_exit["ok"])
        self.assertEqual(res_exit["error_code"], "wsl_exit")

    def test_i8_limits_inverted_and_i9_config_override(self) -> None:
        now = datetime(2026, 11, 15, 8, 30, 0, tzinfo=timezone.utc)
        # I8: worker_limit > lead_limit -> defaults (50, 70) + limits_inverted note_code
        with mock.patch.dict(os.environ, {"KVOOT_WORKER_LIMIT": "80", "KVOOT_LEAD_LIMIT": "60"}):
            rep = kvoot.build_quota_report(codex_sessions_dir=FIXTURES_DIR, now=now)
        self.assertEqual(rep["limits"], {"worker": 50.0, "lead": 70.0})
        self.assertEqual(rep.get("note_code"), "limits_inverted")
        self.assertTrue(rep["workers_allowed"]["codex"])  # 42.5% < 50%

        # I8-a: worker_limit == lead_limit is allowed (strict '>' only is inverted)
        with mock.patch.dict(os.environ, {"KVOOT_WORKER_LIMIT": "60", "KVOOT_LEAD_LIMIT": "60"}):
            rep_eq = kvoot.build_quota_report(codex_sessions_dir=FIXTURES_DIR, now=now)
        self.assertEqual(rep_eq["limits"], {"worker": 60.0, "lead": 60.0})
        self.assertNotEqual(rep_eq.get("note_code"), "limits_inverted")
        self.assertEqual(kvoot_config.compute_status(59.9, 60.0, 60.0), "green")
        self.assertEqual(kvoot_config.compute_status(60.0, 60.0, 60.0), "red")

        # I9: KVOOT_CONFIG points to nonexistent file -> config_not_found, does NOT fall back to BASE_DIR config
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_base_cfg = tmp_path / "kvoot.config.json"
            fake_base_cfg.write_text(json.dumps({"title": "Fallback Leak", "worker_limit": 25}), encoding="utf-8")
            missing_cfg = tmp_path / "does_not_exist.json"
            with mock.patch.object(kvoot_config, "BASE_DIR", tmp_path):
                with mock.patch.dict(os.environ, {"KVOOT_CONFIG": str(missing_cfg)}):
                    kvoot_config.clear_config_notes()
                    cfg = kvoot_config.load_config()
                    notes = kvoot_config.get_config_notes()
                    rep_cfg = kvoot.build_quota_report(codex_sessions_dir=FIXTURES_DIR, now=now)
            self.assertEqual(cfg, {})
            self.assertTrue(any(n["code"] == "config_not_found" for n in notes))
            self.assertEqual(rep_cfg["limits"], {"worker": 50.0, "lead": 70.0})
            self.assertEqual(rep_cfg.get("note_code"), "config_not_found")

        # I16: malformed JSON -> config_invalid (both explicit KVOOT_CONFIG and default BASE_DIR/kvoot.config.json)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bad_default_cfg = tmp_path / "kvoot.config.json"
            bad_default_cfg.write_text("{not valid json", encoding="utf-8")
            with mock.patch.object(kvoot_config, "BASE_DIR", tmp_path):
                with mock.patch.dict(os.environ, {"KVOOT_CONFIG": ""}):
                    kvoot_config.clear_config_notes()
                    cfg_def = kvoot_config.load_config()
                    notes_def = kvoot_config.get_config_notes()
                    rep_def = kvoot.build_quota_report(codex_sessions_dir=FIXTURES_DIR, now=now)
            self.assertEqual(cfg_def, {})
            self.assertTrue(any(n["code"] == "config_invalid" for n in notes_def))
            self.assertFalse(any(n["code"] == "config_not_found" for n in notes_def))
            self.assertEqual(rep_def.get("note_code"), "config_invalid")

            bad_explicit_cfg = tmp_path / "explicit_bad.json"
            bad_explicit_cfg.write_text("[1, 2, 3]", encoding="utf-8")
            with mock.patch.dict(os.environ, {"KVOOT_CONFIG": str(bad_explicit_cfg)}):
                kvoot_config.clear_config_notes()
                cfg_exp = kvoot_config.load_config()
                notes_exp = kvoot_config.get_config_notes()
            self.assertEqual(cfg_exp, {})
            self.assertTrue(any(n["code"] == "config_invalid" for n in notes_exp))
            self.assertFalse(any(n["code"] == "config_not_found" for n in notes_exp))

            # I16b: missing default config (KVOOT_CONFIG="" and no BASE_DIR/kvoot.config.json) -> no note
            empty_base = tmp_path / "empty_base"
            empty_base.mkdir()
            with mock.patch.object(kvoot_config, "BASE_DIR", empty_base):
                with mock.patch.dict(os.environ, {"KVOOT_CONFIG": ""}):
                    kvoot_config.clear_config_notes()
                    cfg_missing_def = kvoot_config.load_config()
                    notes_missing_def = kvoot_config.get_config_notes()
                    rep_missing_def = kvoot.build_quota_report(codex_sessions_dir=FIXTURES_DIR, now=now)
            self.assertEqual(cfg_missing_def, {})
            self.assertEqual(notes_missing_def, [])
            self.assertIsNone(rep_missing_def.get("note_code"))

    def test_item7_note_and_reason_live_language_switch(self) -> None:
        now = datetime(2026, 11, 18, 9, 0, 0, tzinfo=timezone.utc)
        bundles = kvoot_i18n.available_languages()

        def _sim_tr(code: str, key: str, args: dict | None = None) -> str:
            b = bundles.get(code) or bundles["en"]
            en = bundles["en"]
            raw = b.get(key) if isinstance(b.get(key), str) and b.get(key) else en.get(key, key)
            if not args:
                return raw
            return kvoot_i18n.t(key, lang=code, **args)

        def _sim_render_part(code: str, part: dict | str) -> str:
            if isinstance(part, str):
                return part
            prefix = str(part.get("prefix") or "")
            suffix = str(part.get("suffix") or "")
            if isinstance(part.get("parts"), list):
                sep = str(part.get("sep") if part.get("sep") is not None else "; ")
                return prefix + sep.join(_sim_render_part(code, p) for p in part["parts"]) + suffix
            key = part.get("key")
            if not key:
                return prefix + str(part.get("text") or "") + suffix
            raw_args = part.get("args") or {}
            resolved = {
                ak: (_sim_render_part(code, av) if isinstance(av, dict) and ("key" in av or "parts" in av) else av)
                for ak, av in raw_args.items()
            }
            return prefix + _sim_tr(code, key, resolved) + suffix

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            win_dir = tmp_path / "win_codex"
            missing_wsl = tmp_path / "missing_wsl_codex"
            _write_rollout(
                win_dir,
                "2026/11/18/rollout-1.jsonl",
                [self._codex_line("2026-11-18T08:55:00Z", 20.0, 10.0)],
            )
            fp_status = tmp_path / "kvoot_claude.json"
            fp_status.write_text(
                json.dumps(
                    {
                        "written_at": (now - timedelta(minutes=3)).isoformat(timespec="seconds"),
                        "five_hour": {"used_percentage": 15.0, "resets_at": 1800000000},
                        "seven_day": None,
                    }
                ),
                encoding="utf-8",
            )
            fp_manual = tmp_path / "kvoot_claude_manual.json"
            fp_manual.write_text(
                json.dumps(
                    {
                        "written_at": (now - timedelta(minutes=8)).isoformat(timespec="seconds"),
                        "five_hour": {"used_percentage": 10.0, "resets_at": 1800000000},
                        "seven_day": {"used_percentage": 25.0, "resets_at": 1800500000},
                    }
                ),
                encoding="utf-8",
            )
            anti_payload = {
                "response": {
                    "groups": [
                        {
                            "displayName": "Gemini Models",
                            "buckets": [
                                {
                                    "bucketId": "gemini-5h",
                                    "window": "5h",
                                    "remainingFraction": 0.9,
                                    "resetTime": "2026-11-18T11:00:00Z",
                                }
                            ],
                        }
                    ]
                }
            }
            fake_proc = antigravity_probe.AntigravityProcess(pid=77, csrf_token="tok_i7", extension_port=41234, koht="wsl")

            def _build_full(lang_code: str) -> tuple[dict, str]:
                with mock.patch.dict(os.environ, {"KVOOT_LANG": lang_code, "KVOOT_WORKER_LIMIT": "80", "KVOOT_LEAD_LIMIT": "60"}):
                    with mock.patch.object(
                        kvoot,
                        "refresh_claude_usage",
                        return_value={
                            "ok": False,
                            "error_code": "timeout",
                            "reason_key": "claude_reason_timeout",
                            "reason_args": {"timeout": 20.0},
                            "reason": "Claude /usage ekraan ei avanenud 20 s jooksul"
                            if lang_code == "et"
                            else "Claude /usage screen did not open within 20 s",
                        },
                    ):
                        with mock.patch.object(antigravity_probe, "find_antigravity_process_linux", return_value=fake_proc):
                            with mock.patch.object(antigravity_probe, "find_antigravity_process_windows", return_value=fake_proc):
                                with mock.patch.object(antigravity_probe, "find_listening_ports_linux", return_value=[41234]):
                                    with mock.patch.object(antigravity_probe, "find_listening_ports_windows", return_value=[41234]):
                                        with mock.patch.object(
                                            antigravity_probe,
                                            "_query_process",
                                            return_value=(anti_payload, antigravity_probe.ENDPOINTS[0], None),
                                        ):
                                            rep = kvoot.build_quota_report(
                                                codex_dirs=[("windows", win_dir), ("wsl", missing_wsl)],
                                                claude_file=fp_status,
                                                claude_manual_file=fp_manual,
                                                refresh_all=True,
                                                now=now,
                                            )
                                            out_dir = tmp_path / f"out_{lang_code}"
                                            json_p, html_p = kvoot.write_outputs(rep, out_dir=out_dir, now=now)
                                            written_json = json.loads(json_p.read_text(encoding="utf-8"))
                                            return written_json, html_p.read_text(encoding="utf-8")

            json_et, html_et = _build_full("et")
            json_en, html_en = _build_full("en")

            # kvoot.json must contain no internal '_' keys
            self.assertFalse(any(k.startswith("_") for k in json_et))
            for src_obj in json_et["sources"].values():
                self.assertFalse(any(k.startswith("_") for k in src_obj))

            # Also check unavailable cards have data-i18n-parts
            empty_kodu = tmp_path / "empty_kodu"
            empty_kodu.mkdir()
            with mock.patch.dict(os.environ, {"KVOOT_LANG": "et", "KVOOT_KODU": str(empty_kodu)}):
                rep_missing = kvoot.build_quota_report(codex_sessions_dir=tmp_path / "no_codex", now=now)
                html_missing_et = kvoot.render_html(rep_missing, now=now)

            for html_src, target_lang, forbidden_phrases in (
                (
                    html_et,
                    "en",
                    [
                        "kohalikud Codexi sessioonid",
                        "allikad:",
                        "lugemata:",
                        "kausta ei leitud",
                        "kohalik keeleserver",
                        "kogumid:",
                        "viimase kohaliku Claude",
                        "värske /usage ebaõnnestus",
                        "ekraan ei avanenud",
                        "aken puudus allikast",
                        "7p aken",
                        "Seadistatud worker_limit",
                    ],
                ),
                (
                    html_en,
                    "et",
                    [
                        "local Codex session files",
                        "sources:",
                        "unreadable:",
                        "directory not found",
                        "local language server",
                        "pools:",
                        "shows Claude account quota",
                        "fresh /usage failed",
                        "screen did not open",
                        "window was missing from",
                        "7d window",
                        "Configured worker_limit",
                    ],
                ),
            ):
                body = html_src.split("</style>", 1)[1].split('<script id="i18n-data"', 1)[0]
                parts_attrs = re.findall(r'data-i18n-parts="([^"]+)"', body)
                # Notice banner + Codex note + Claude note + Antigravity note = 4 elements
                self.assertEqual(len(parts_attrs), 4)
                rendered_notes = []
                for raw_attr in parts_attrs:
                    spec = json.loads(kvoot.html.unescape(raw_attr))
                    rendered_notes.append("; ".join(_sim_render_part(target_lang, p) for p in spec))
                joined = "\n".join(rendered_notes)
                for phrase in forbidden_phrases:
                    self.assertNotIn(phrase, joined)
                if target_lang == "en":
                    self.assertIn("screen did not open within 20 s", joined)
                else:
                    self.assertIn("ekraan ei avanenud 20 s jooksul", joined)
                # Technical identifiers remain untouched
                self.assertIn("RetrieveUserQuotaSummary", joined)
                self.assertIn("Gemini Models", joined)
                self.assertIn("windows", joined)
                self.assertIn("wsl", joined)

            # Unavailable card reasons also switch cleanly via data-i18n-parts
            missing_body = html_missing_et.split("</style>", 1)[1].split('<script id="i18n-data"', 1)[0]
            missing_parts = re.findall(r'class="missing-reason"[^>]*data-i18n-parts="([^"]+)"', missing_body)
            self.assertEqual(len(missing_parts), 3)
            missing_en = "\n".join(
                "; ".join(_sim_render_part("en", p) for p in json.loads(kvoot.html.unescape(raw)))
                for raw in missing_parts
            )
            self.assertIn("Codex sessions directory not found", missing_en)
            self.assertIn("No Claude quota file found", missing_en)
            self.assertIn("Antigravity language server is not running", missing_en)
            self.assertNotIn("ei leitud", missing_en)
            self.assertNotIn("ei tööta", missing_en)

    def test_v21_antigravity_wsl_error_structured_parts(self) -> None:
        now = datetime(2026, 11, 18, 9, 0, 0, tzinfo=timezone.utc)
        self._anti_wsl_patch.stop()
        try:
            cases = [
                ("missing", FileNotFoundError("wsl.exe not found --csrf_token secret_wsl_token_123"), "ag_reason_wsl_failed"),
                ("timeout", subprocess.TimeoutExpired(["wsl.exe"], 8), "ag_reason_wsl_failed"),
                (
                    "parse_error",
                    subprocess.CompletedProcess(
                        ["wsl.exe"],
                        1,
                        stdout=json.dumps(
                            {
                                "ok": False,
                                "reason_code": "parse_error",
                                "reason_key": "ag_reason_parse_error",
                                "reason_args": {"err": "bad json payload --csrf_token secret_wsl_token_123"},
                                "reason": "Antigravity vastuse parsimine ebaõnnestus: bad json payload --csrf_token secret_wsl_token_123",
                            }
                        ),
                        stderr="",
                    ),
                    "ag_reason_parse_error",
                ),
            ]
            for label, effect, exp_key in cases:
                with self.subTest(case=label):
                    with tempfile.TemporaryDirectory() as tmp:
                        cache_p = Path(tmp) / "ag_last.json"
                        with mock.patch.object(antigravity_probe, "_is_windows", return_value=True):
                            with mock.patch.object(antigravity_probe, "find_antigravity_process_windows", return_value=None):
                                with mock.patch.object(antigravity_probe.subprocess, "run", side_effect=effect if isinstance(effect, Exception) else None, return_value=None if isinstance(effect, Exception) else effect):
                                    with mock.patch.dict(os.environ, {"KVOOT_LANG": "et"}):
                                        res = antigravity_probe.read_antigravity_quota(
                                            now=now,
                                            tz=kvoot_config.tallinn_tz(),
                                            cache_path=cache_p,
                                        )
                        self.assertFalse(res["available"])
                        parts = res["_reason_parts"]
                        self.assertEqual(len(parts), 1)
                        self.assertEqual(parts[0]["key"], exp_key)
                        arg_err = str((parts[0].get("args") or {}).get("err", ""))
                        self.assertNotIn("ebaõnnestus", arg_err)
                        self.assertNotIn("Antigravity", arg_err)
                        self.assertNotIn("secret_wsl_token_123", arg_err)
                        self.assertNotIn("secret_wsl_token_123", res["reason"])
                        if label in ("missing", "parse_error"):
                            self.assertIn("***", arg_err)
                            self.assertIn("***", res["reason"])
                        self.assertEqual(res["reason"].count("ebaõnnestus:"), 1)

                        # Render into HTML in 'et' and verify parts switch cleanly to 'en'
                        rep = {
                            "schema": "kvoot/4",
                            "read_at": "2026-11-18T11:00:00+02:00",
                            "limits": {"worker": 50.0, "lead": 70.0},
                            "sources": {
                                "codex": {"available": False, "reason": "none"},
                                "claude": {"available": False, "reason": "none"},
                                "antigravity": res,
                            },
                            "workers_allowed": {"codex": None, "claude": None, "antigravity": None},
                            "lead_allowed": {"codex": None, "claude": None, "antigravity": None},
                        }
                        with mock.patch.dict(os.environ, {"KVOOT_LANG": "en"}):
                            en_reason = kvoot_i18n.render_parts(parts, lang="en")
                            html_en = kvoot.render_html(rep, now=now)
                        self.assertNotIn("ebaõnnestus", en_reason)
                        self.assertNotIn("päring", en_reason)
                        self.assertNotIn("parsimine", en_reason)
                        self.assertIn("Antigravity", en_reason)
                        self.assertIn("data-i18n-parts", html_en)
        finally:
            self._anti_wsl_patch.start()

    def test_i13_and_tz_selector_in_node_js(self) -> None:
        node_bin = _find_node()
        if not node_bin:
            self.skipTest("node not found")

        now_july = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
        now_jan = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

        class _DOMExtractParser(HTMLParser):
            def __init__(self) -> None:
                super().__init__(convert_charrefs=True)
                self.root: dict = {"tag": "body", "attrs": {}, "children": []}
                self.stack: list[dict] = [self.root]
                self.in_body = False
                self.in_script = False
                self.script_id = ""
                self.script_buf: list[str] = []
                self.i18n_json = ""
                self.main_js = ""

            def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                attr_map = {k: ("" if v is None else v) for k, v in attrs}
                if tag == "body":
                    self.in_body = True
                    return
                if tag == "script":
                    self.in_script = True
                    self.script_id = attr_map.get("id", "")
                    self.script_buf = []
                    return
                if self.in_body:
                    node = {"tag": tag, "attrs": attr_map, "children": []}
                    self.stack[-1]["children"].append(node)
                    if tag not in ("meta", "br", "hr", "img", "input"):
                        self.stack.append(node)

            def handle_endtag(self, tag: str) -> None:
                if tag == "script" and self.in_script:
                    content = "".join(self.script_buf)
                    if self.script_id == "i18n-data":
                        self.i18n_json = content
                    else:
                        self.main_js = content
                    self.in_script = False
                    self.script_id = ""
                    self.script_buf = []
                    return
                if tag == "body":
                    self.in_body = False
                    return
                if self.in_body and len(self.stack) > 1 and self.stack[-1]["tag"] == tag:
                    self.stack.pop()

            def handle_data(self, data: str) -> None:
                if self.in_script:
                    self.script_buf.append(data)
                elif self.in_body and data:
                    self.stack[-1]["children"].append({"tag": "#text", "text": data})

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            win_dir = tmp_path / "win_codex"
            missing_wsl = tmp_path / "missing_wsl_codex"
            _write_rollout(
                win_dir,
                "2026/07/01/rollout-1.jsonl",
                [self._codex_line("2026-07-01T12:00:00Z", 20.0, 10.0, r5=1782914400, r7=1783339200)],
            )
            fp_status = tmp_path / "kvoot_claude.json"
            fp_status.write_text(
                json.dumps(
                    {
                        "written_at": "2026-01-15T12:00:00+00:00",
                        "five_hour": {"used_percentage": 15.0, "resets_at": 1768492800},
                        "seven_day": None,
                    }
                ),
                encoding="utf-8",
            )
            fp_manual = tmp_path / "kvoot_claude_manual.json"
            fp_manual.write_text(
                json.dumps(
                    {
                        "written_at": "2026-01-15T11:55:00+00:00",
                        "five_hour": {"used_percentage": 10.0, "resets_at": 1768492800},
                        "seven_day": {"used_percentage": 25.0, "resets_at": 1768992800},
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.dict(os.environ, {"KVOOT_LANG": "et", "KVOOT_TZ": "", "TZ": ""}):
                with mock.patch.object(kvoot_config, "_is_windows", return_value=True):
                    with mock.patch.object(
                        kvoot,
                        "refresh_claude_usage",
                        return_value={
                            "ok": False,
                            "error_code": "timeout",
                            "reason_key": "claude_reason_timeout",
                            "reason_args": {"timeout": 20.0},
                        },
                    ):
                        rep = kvoot.build_quota_report(
                            codex_dirs=[("windows", win_dir), ("wsl", missing_wsl)],
                            claude_file=fp_status,
                            claude_manual_file=fp_manual,
                            refresh_all=True,
                            now=now_july,
                        )
                        html_doc = kvoot.render_html(rep, now=now_july)

            # Ensure winter source_time and summer read_at are both present in the same HTML
            self.assertIn('data-utc-iso="2026-07-01T12:00:00Z"', html_doc)
            self.assertIn('data-utc-iso="2026-01-15T12:00:00Z"', html_doc)
            self.assertIn("data-tz-fixed-notice", html_doc)

            # Verify timezone selector CSS expands with text and provides a wrapping full-name label on narrow screens
            style_block = html_doc.split("<style>", 1)[1].split("</style>", 1)[0]
            self.assertNotIn("max-width: 220px", style_block)
            self.assertRegex(style_block, r"select\.tz-select\s*\{[^}]*width:\s*auto")
            self.assertRegex(style_block, r"select\.tz-select\s*\{[^}]*max-width:\s*100%")
            self.assertRegex(style_block, r"header\s*\{[^}]*flex-wrap:\s*wrap")
            self.assertRegex(style_block, r"\.controls\s*\{[^}]*flex-wrap:\s*wrap")
            self.assertRegex(style_block, r"\.tz-control\s*\{[^}]*max-width:\s*100%")
            self.assertRegex(style_block, r"\.tz-current\s*\{[^}]*white-space:\s*normal")
            self.assertRegex(style_block, r"\.tz-current\s*\{[^}]*overflow-wrap:\s*anywhere")
            self.assertRegex(style_block, r"\.tz-current\s*\{[^}]*max-width:\s*100%")
            self.assertNotIn("min-width: 481px", style_block)
            self.assertRegex(
                style_block,
                r"@media\s*\(\s*min-width:\s*40em\s*\)\s*\{[^}]*\.tz-current\s*\{[^}]*display:\s*none",
            )

            parser = _DOMExtractParser()
            parser.feed(html_doc)
            self.assertTrue(parser.main_js)
            self.assertTrue(parser.i18n_json)

            # Build malicious xx legend bundle for testing DOM safety in JS
            bundles = json.loads(parser.i18n_json)
            bundles["xx"] = dict(bundles["en"])
            bundles["xx"]["html_legend"] = '<img src=x onerror=pwn()><strong>{worker}%</strong>'
            i18n_mal_json = json.dumps(bundles, ensure_ascii=False)

            node_harness = tmp_path / "run_dom_test.js"
            node_harness.write_text(
                r"""
const assert = require("assert");
const vm = require("vm");
const fs = require("fs");

const payload = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));

function createNode(tag, text) {
  const listeners = {};
  const attrs = {};
  const children = [];
  const style = {};
  let _value = "";
  const el = {
    tagName: tag.toUpperCase(),
    nodeType: tag === "#text" ? 3 : 1,
    style,
    hidden: false,
    _text: text || "",
    children,
    setAttribute(k, v) { attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null; },
    hasAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k); },
    appendChild(child) { children.push(child); return child; },
    addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
    dispatchEvent(ev) { (listeners[ev] || []).forEach(fn => fn({ type: ev, target: el })); },
    insertAdjacentHTML() { throw new Error("FORBIDDEN insertAdjacentHTML"); },
    get options() { return children.filter(c => c.tagName === "OPTION"); },
    get value() {
      if (tag === "select" && !_value && children.length) {
        const sel = children.find(c => Object.prototype.hasOwnProperty.call(c._attrs, "selected"));
        return sel ? sel.value : children[0].value;
      }
      return _value;
    },
    set value(v) { _value = String(v); },
    get textContent() {
      if (tag === "#text") return el._text;
      return children.map(c => c.textContent).join("");
    },
    set textContent(v) {
      if (tag === "#text") { el._text = String(v); return; }
      children.length = 0;
      if (v !== "" && v !== null && v !== undefined) {
        children.push(createNode("#text", String(v)));
      }
    },
    _attrs: attrs,
  };
  Object.defineProperty(el, "innerHTML", {
    get() { throw new Error("FORBIDDEN innerHTML get"); },
    set() { throw new Error("FORBIDDEN innerHTML set"); },
  });
  Object.defineProperty(el, "outerHTML", {
    get() { throw new Error("FORBIDDEN outerHTML get"); },
    set() { throw new Error("FORBIDDEN outerHTML set"); },
  });
  return el;
}

function hydrate(spec) {
  if (spec.tag === "#text") return createNode("#text", spec.text);
  const el = createNode(spec.tag);
  for (const [k, v] of Object.entries(spec.attrs || {})) {
    el.setAttribute(k, v);
    if (k === "value") el.value = v;
  }
  for (const ch of spec.children || []) {
    el.appendChild(hydrate(ch));
  }
  return el;
}

function buildScenario(opts) {
  const body = hydrate(payload.dom);
  const docEl = createNode("html");
  docEl.setAttribute("lang", "et");
  const i18nEl = createNode("script");
  i18nEl.setAttribute("id", "i18n-data");
  i18nEl.textContent = opts.i18nJson || payload.i18nJson;

  function findAll(node, pred, out) {
    if (node.nodeType === 1 && pred(node)) out.push(node);
    for (const ch of node.children || []) findAll(ch, pred, out);
  }

  const storageData = Object.assign({}, opts.storage || {});
  const localStorage = {
    getItem(k) { return Object.prototype.hasOwnProperty.call(storageData, k) ? storageData[k] : null; },
    setItem(k, v) { storageData[k] = String(v); },
  };

  const document = {
    documentElement: docEl,
    createElement(tag) { return createNode(tag); },
    createTextNode(txt) { return createNode("#text", txt); },
    getElementById(id) {
      if (id === "i18n-data") return i18nEl;
      const found = [];
      findAll(body, n => n.getAttribute("id") === id, found);
      return found[0] || null;
    },
    querySelectorAll(sel) {
      const m = /^\[([a-zA-Z0-9_-]+)\]$/.exec(sel.trim());
      if (!m) return [];
      const attr = m[1];
      const found = [];
      findAll(body, n => n.hasAttribute(attr), found);
      return found;
    },
    write() { throw new Error("FORBIDDEN document.write"); },
  };

  const dtfFullCalls = [];
  function makeTrackedDTF(baseFn) {
    return function TrackedDTF(loc, options) {
      if (options && options.year) {
        dtfFullCalls.push(Object.prototype.hasOwnProperty.call(options, "timeZoneName") ? options.timeZoneName : null);
      }
      return baseFn(loc, options);
    };
  }

  let baseDTF = (loc, options) => new Intl.DateTimeFormat(loc, options);
  let supportedValuesFn = Intl.supportedValuesOf;
  if (opts.noSupportedValuesOf) {
    supportedValuesFn = () => { throw new Error("unsupported"); };
  } else if (opts.intlMode === "autoNyc" || opts.intlMode === "autoLongTz" || opts.intlMode === "autoTallinn") {
    const forcedAutoTz =
      opts.intlMode === "autoLongTz"
        ? "America/Argentina/ComodRivadavia"
        : (opts.intlMode === "autoTallinn" ? "Europe/Tallinn" : "America/New_York");
    baseDTF = (loc, options) => {
      const real = new Intl.DateTimeFormat(loc, options);
      if (!options || !options.timeZone) {
        return {
          format(d) { return real.format(d); },
          resolvedOptions() { return Object.assign({}, real.resolvedOptions(), { timeZone: forcedAutoTz }); },
        };
      }
      return real;
    };
  } else if (opts.intlMode === "noShortOffset") {
    baseDTF = (loc, options) => {
      if (options && options.timeZoneName === "shortOffset") {
        throw new RangeError("shortOffset unsupported");
      }
      return new Intl.DateTimeFormat(loc, options);
    };
  } else if (opts.intlMode === "noTimeZoneName") {
    baseDTF = (loc, options) => {
      if (options && options.timeZoneName) {
        throw new RangeError("timeZoneName unsupported");
      }
      return new Intl.DateTimeFormat(loc, options);
    };
  } else if (opts.intlMode === "formatBroken") {
    baseDTF = (loc, options) => {
      if (options && (options.year || options.hour)) {
        throw new Error("Intl format broken");
      }
      return new Intl.DateTimeFormat(loc, options);
    };
  }
  const customIntl = {
    DateTimeFormat: makeTrackedDTF(baseDTF),
    supportedValuesOf: supportedValuesFn,
  };

  const sandbox = { document, localStorage, Intl: customIntl, Date, JSON, Object, Array, String, Number, RegExp, isNaN, isFinite, parseInt };
  vm.runInNewContext(payload.mainJs, sandbox);
  return { document, body, localStorage, storageData, docEl, dtfFullCalls };
}

function getTimeTexts(doc) {
  const julyEl = doc.querySelectorAll("[data-utc-iso]").find(e => e.getAttribute("data-utc-iso") === "2026-07-01T12:00:00Z");
  const janEl = doc.querySelectorAll("[data-utc-iso]").find(e => e.getAttribute("data-utc-iso") === "2026-01-15T12:00:00Z");
  const hmEl = doc.querySelectorAll("[data-utc-hhmm]")[0];
  return { july: julyEl.textContent, jan: janEl.textContent, hm: hmEl.textContent };
}

// 1) Normal run: check timezone notice hidden, theme icon + aria-label, lang switch et -> en -> fi, nested args, legend nodes, DST formatting
const s1 = buildScenario({});
const tzNotices = s1.document.querySelectorAll("[data-tz-fixed-notice]");
assert.strictEqual(tzNotices.length, 1);
assert.strictEqual(tzNotices[0].hidden, true);
assert.strictEqual(tzNotices[0].style.display, "none");

const langSel = s1.document.getElementById("langSelect");
const tzSel = s1.document.getElementById("tzSelect");
const themeBtn = s1.document.getElementById("themeToggle");
const themeIcon = s1.document.getElementById("themeIcon");
assert.strictEqual(s1.docEl.getAttribute("data-theme") || "dark", "dark");
assert.strictEqual(themeIcon.textContent, "☀");
assert.strictEqual(themeBtn.getAttribute("aria-label"), "Lülita heledale teemale");

// Click theme toggle: dark -> light (☾), preserves translation on lang switch, light -> dark (☀)
themeBtn.dispatchEvent("click");
assert.strictEqual(s1.docEl.getAttribute("data-theme"), "light");
assert.strictEqual(themeIcon.textContent, "☾");
assert.strictEqual(themeBtn.getAttribute("aria-label"), "Lülita tumedale teemale");
assert.strictEqual(s1.storageData["kvoot-theme"], "light");

assert.ok(tzSel.options.length > 5);
assert.strictEqual(tzSel.options[0].value, "auto");
assert.ok(tzSel.options[0].textContent.includes("·") || tzSel.options[0].textContent.length > 0);

// Switch language et -> en
langSel.value = "en";
langSel.dispatchEvent("change");
assert.strictEqual(themeIcon.textContent, "☾");
assert.strictEqual(themeBtn.getAttribute("aria-label"), "Switch to dark theme");
assert.ok(themeBtn.textContent.includes("Toggle theme"));
const partsEn = s1.document.querySelectorAll("[data-i18n-parts]").map(el => el.textContent).join("\n");
assert.ok(partsEn.includes("local Codex session files"));
assert.ok(partsEn.includes("directory not found"));
assert.ok(partsEn.includes("screen did not open within 20 s"));
assert.ok(!partsEn.includes("kausta ei leitud"));
assert.ok(!partsEn.includes("ekraan ei avanenud"));
assert.ok(!partsEn.includes("värske /usage ebaõnnestus"));

// Switch language en -> fi
langSel.value = "fi";
langSel.dispatchEvent("change");
assert.strictEqual(themeIcon.textContent, "☾");
assert.strictEqual(themeBtn.getAttribute("aria-label"), "Vaihda tummaan teemaan");
assert.ok(themeBtn.textContent.includes("Vaihda teema"));
themeBtn.dispatchEvent("click");
assert.strictEqual(s1.docEl.getAttribute("data-theme"), "dark");
assert.strictEqual(themeIcon.textContent, "☀");
assert.strictEqual(themeBtn.getAttribute("aria-label"), "Vaihda vaaleaan teemaan");
const partsFi = s1.document.querySelectorAll("[data-i18n-parts]").map(el => el.textContent).join("\n");
assert.ok(partsFi.includes("paikalliset Codex-istuntotiedostot"));
assert.ok(partsFi.includes("kansiota ei löytynyt"));
assert.ok(partsFi.includes("näkymä ei avautunut 20 s kuluessa"));
assert.ok(s1.document.querySelectorAll("[data-i18n]").some(el => el.textContent.includes("Multi-AI käyttömittari")));

// Legend contains ONLY #text and STRONG elements, never IMG
const legendEl = s1.document.querySelectorAll("[data-i18n-legend]")[0];
assert.ok(legendEl.children.length >= 2);
for (const ch of legendEl.children) {
  assert.ok(ch.tagName === "#TEXT" || ch.tagName === "STRONG", "Unexpected legend child tag: " + ch.tagName);
}

// Switch to language 'en' and test timezone selector on July (DST) and January (standard) + (b) change saves + (e) format follows page lang
langSel.value = "en";
langSel.dispatchEvent("change");

tzSel.value = "Europe/Tallinn";
tzSel.dispatchEvent("change");
assert.strictEqual(s1.storageData["kvoot-tz"], "Europe/Tallinn");
const tln = getTimeTexts(s1.document);
assert.ok(tln.july.includes("15:00:00") && tln.july.includes("+3"), "Expected July Tallinn +3: " + tln.july);
assert.ok(tln.jan.includes("14:00:00") && tln.jan.includes("+2"), "Expected Jan Tallinn +2: " + tln.jan);
assert.strictEqual(tln.hm, "15:00");

tzSel.value = "America/New_York";
tzSel.dispatchEvent("change");
assert.strictEqual(s1.storageData["kvoot-tz"], "America/New_York");
const nycEn = getTimeTexts(s1.document);
assert.ok(nycEn.july.includes("08:00:00") && nycEn.july.includes("4"), "Expected July NYC -4: " + nycEn.july);
assert.ok(nycEn.july.includes("07/01/2026"), "Expected US date format in en: " + nycEn.july);
assert.ok(nycEn.jan.includes("07:00:00") && nycEn.jan.includes("5"), "Expected Jan NYC -5: " + nycEn.jan);
assert.strictEqual(nycEn.hm, "08:00");
assert.notStrictEqual(nycEn.july, tln.july);

// (e) Formatting language follows page language (en -> et -> fi)
langSel.value = "et";
langSel.dispatchEvent("change");
const nycEt = getTimeTexts(s1.document);
assert.ok(nycEt.july.includes("01.07.2026"), "Expected et date format: " + nycEt.july);
assert.notStrictEqual(nycEt.july, nycEn.july);

langSel.value = "fi";
langSel.dispatchEvent("change");
const nycFi = getTimeTexts(s1.document);
assert.ok(nycFi.july.includes("08.00.00"), "Expected fi time format: " + nycFi.july);
assert.notStrictEqual(nycFi.july, nycEn.july);
assert.notStrictEqual(nycFi.july, nycEt.july);

// 2) Unknown saved timezone falls back to 'auto'
const s2 = buildScenario({ storage: { "kvoot-tz": "Mars/Olympus" } });
assert.strictEqual(s2.document.getElementById("tzSelect").value, "auto");
assert.strictEqual(s2.storageData["kvoot-tz"], "auto");

// 3) Malicious legend translation produces no IMG element (only #text and STRONG)
const s3 = buildScenario({ i18nJson: payload.i18nMalJson, storage: { "kvoot-lang": "xx" } });
const malLegend = s3.document.querySelectorAll("[data-i18n-legend]")[0];
assert.ok(malLegend.textContent.includes("<img src=x onerror=pwn()>"));
for (const ch of malLegend.children) {
  assert.ok(ch.tagName === "#TEXT" || ch.tagName === "STRONG");
}

// 4) Fallback timezone list when Intl.supportedValuesOf is unavailable
const s4 = buildScenario({ noSupportedValuesOf: true });
const opts4 = s4.document.getElementById("tzSelect").options.map(o => o.value);
assert.deepStrictEqual(opts4, ["auto", "UTC", "Europe/Tallinn", "Europe/Helsinki", "Europe/London", "America/New_York"]);

// 5) (a) Valid saved kvoot-tz="America/New_York" and saved kvoot-theme="light" restore cleanly on load
const s5 = buildScenario({ storage: { "kvoot-tz": "America/New_York", "kvoot-theme": "light" } });
assert.strictEqual(s5.document.getElementById("tzSelect").value, "America/New_York");
assert.strictEqual(s5.storageData["kvoot-tz"], "America/New_York");
const t5 = getTimeTexts(s5.document);
assert.ok(t5.july.includes("08:00:00") && t5.july.includes("4"), "Expected restored NYC July -4: " + t5.july);
assert.ok(t5.jan.includes("07:00:00") && t5.jan.includes("5"), "Expected restored NYC Jan -5: " + t5.jan);
assert.strictEqual(s5.docEl.getAttribute("data-theme"), "light");
assert.strictEqual(s5.document.getElementById("themeIcon").textContent, "☾");

// 6) (c) "auto" uses Intl.DateTimeFormat().resolvedOptions().timeZone (not hardcoded UTC)
const s6 = buildScenario({ intlMode: "autoNyc" });
const tz6 = s6.document.getElementById("tzSelect");
assert.strictEqual(tz6.value, "auto");
assert.ok(tz6.options[0].textContent.includes("America/New_York"), "Expected auto label with America/New_York: " + tz6.options[0].textContent);
const t6 = getTimeTexts(s6.document);
assert.ok(t6.july.includes("08:00:00") && t6.july.includes("4"), "Expected auto NYC -4, not UTC: " + t6.july);

// 7) I19 fallback (SOL-03): shortOffset throws -> tries shortOffset then falls back strictly to "short" (not null)
const s7 = buildScenario({ intlMode: "noShortOffset", storage: { "kvoot-tz": "America/New_York", "kvoot-lang": "en" } });
assert.deepStrictEqual(s7.dtfFullCalls.slice(0, 2), ["shortOffset", "short"]);
const n7 = s7.document.querySelectorAll("[data-tz-fixed-notice]")[0];
assert.strictEqual(n7.hidden, true);
assert.strictEqual(s7.document.getElementById("tzSelect").hidden, false);
const t7 = getTimeTexts(s7.document);
const expectedShortJuly = new Intl.DateTimeFormat("en", {
  timeZone: "America/New_York",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
  hourCycle: "h23",
  timeZoneName: "short",
}).format(new Date("2026-07-01T12:00:00Z"));
assert.strictEqual(t7.july, expectedShortJuly);

// 8) I19 fallback (SOL-03): shortOffset and short both throw -> tries shortOffset -> short -> null
const s8 = buildScenario({ intlMode: "noTimeZoneName", storage: { "kvoot-tz": "America/New_York", "kvoot-lang": "en" } });
assert.deepStrictEqual(s8.dtfFullCalls.slice(0, 3), ["shortOffset", "short", null]);
const n8 = s8.document.querySelectorAll("[data-tz-fixed-notice]")[0];
assert.strictEqual(n8.hidden, true);
assert.strictEqual(s8.document.getElementById("tzSelect").hidden, false);
const t8 = getTimeTexts(s8.document);
assert.ok(t8.july.includes("08:00:00"), "Expected no-timeZoneName fallback formatted July 08:00:00: " + t8.july);

// 9) (d) + I19: Intl formatting error keeps data-tz-fixed-notice visible, leaves server text intact, and hides #tzSelect/#tzCurrent
const s9 = buildScenario({ intlMode: "formatBroken" });
assert.deepStrictEqual(s9.dtfFullCalls.slice(0, 3), ["shortOffset", "short", null]);
const n9 = s9.document.querySelectorAll("[data-tz-fixed-notice]")[0];
const tz9 = s9.document.getElementById("tzSelect");
const tzCur9 = s9.document.getElementById("tzCurrent");
assert.strictEqual(n9.hidden, false);
assert.notStrictEqual(n9.style.display, "none");
assert.strictEqual(tz9.hidden, true);
assert.strictEqual(tz9.style.display, "none");
assert.strictEqual(tzCur9.hidden, true);
assert.strictEqual(tzCur9.style.display, "none");
const t9 = getTimeTexts(s9.document);
assert.strictEqual(t9.july, payload.serverJulyText);

// 10) SOL-04: Long auto and selected timezone label appears untruncated in #tzCurrent in et, en, fi
//     and wide viewports (>= 40em, font-size-aware) hide #tzCurrent via CSS @media rule without inline display override
assert.ok(
  /@media\s*\(\s*min-width:\s*40em\s*\)\s*\{[\s\S]*?\.tz-current\s*\{[^}]*display:\s*none/.test(payload.styleBlock),
  "Expected @media (min-width: 40em) rule hiding .tz-current"
);
assert.ok(
  !/min-width:\s*481px/.test(payload.styleBlock),
  "Did not expect pixel-based min-width: 481px breakpoint"
);
const s10 = buildScenario({ intlMode: "autoLongTz" });
const lang10 = s10.document.getElementById("langSelect");
const tz10 = s10.document.getElementById("tzSelect");
const tzCur10 = s10.document.getElementById("tzCurrent");
assert.strictEqual(tzCur10.hidden, false);
assert.strictEqual(tzCur10.style.display, "");
assert.strictEqual(tz10.options[0].textContent, "Automaatne · America/Argentina/ComodRivadavia");
assert.strictEqual(tzCur10.textContent, "Automaatne · America/Argentina/ComodRivadavia");
assert.strictEqual(tz10.getAttribute("title"), "Automaatne · America/Argentina/ComodRivadavia");
lang10.value = "en";
lang10.dispatchEvent("change");
assert.strictEqual(tz10.options[0].textContent, "Auto · America/Argentina/ComodRivadavia");
assert.strictEqual(tzCur10.textContent, "Auto · America/Argentina/ComodRivadavia");
assert.strictEqual(tz10.getAttribute("title"), "Auto · America/Argentina/ComodRivadavia");
lang10.value = "fi";
lang10.dispatchEvent("change");
assert.strictEqual(tz10.options[0].textContent, "Automaattinen · America/Argentina/ComodRivadavia");
assert.strictEqual(tzCur10.textContent, "Automaattinen · America/Argentina/ComodRivadavia");
assert.strictEqual(tz10.getAttribute("title"), "Automaattinen · America/Argentina/ComodRivadavia");
// Explicit timezone selection also updates #tzCurrent and title with full name
tz10.value = "America/Argentina/ComodRivadavia";
tz10.dispatchEvent("change");
assert.strictEqual(tzCur10.textContent, "America/Argentina/ComodRivadavia");
assert.strictEqual(tz10.getAttribute("title"), "America/Argentina/ComodRivadavia");

const s11 = buildScenario({ intlMode: "autoTallinn", storage: { "kvoot-lang": "fi" } });
assert.strictEqual(s11.document.getElementById("tzSelect").options[0].textContent, "Automaattinen · Europe/Tallinn");
assert.strictEqual(s11.document.getElementById("tzCurrent").textContent, "Automaattinen · Europe/Tallinn");
assert.strictEqual(s11.document.getElementById("tzCurrent").style.display, "");
""",
                encoding="utf-8",
            )
            server_parser = _DOMExtractParser()
            server_parser.feed(html_doc)
            def _find_utc_iso_text(node, target_iso):
                if node.get("tag") != "#text" and node.get("attrs", {}).get("data-utc-iso") == target_iso:
                    return "".join(ch.get("text", "") for ch in node.get("children", []))
                for ch in node.get("children", []):
                    res_txt = _find_utc_iso_text(ch, target_iso)
                    if res_txt is not None:
                        return res_txt
                return None
            server_july_text = _find_utc_iso_text(server_parser.root, "2026-07-01T12:00:00Z")
            self.assertTrue(server_july_text)

            payload_file = tmp_path / "dom_payload.json"
            payload_file.write_text(
                json.dumps(
                    {
                        "dom": parser.root,
                        "i18nJson": parser.i18n_json,
                        "i18nMalJson": i18n_mal_json,
                        "mainJs": parser.main_js,
                        "styleBlock": style_block,
                        "serverJulyText": server_july_text,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            proc = subprocess.run(
                [node_bin, str(node_harness), str(payload_file)],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, msg=f"Node JS test failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")

    def test_v22_posix_tz_rule(self) -> None:
        july = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        jan = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        mar15 = datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)
        mar28 = datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc)
        apr02 = datetime(2026, 4, 2, 12, 0, tzinfo=timezone.utc)

        with mock.patch.object(kvoot_config, "_is_windows", return_value=False):
            with mock.patch.object(kvoot_config, "_detect_posix_system_tz", return_value=kvoot_config.tallinn_tz()):
                # 1) Fixed rules: UTC0, JST-9, <+03>-3
                for rule, exp_off, exp_name in (
                    ("UTC0", timedelta(0), "UTC"),
                    ("JST-9", timedelta(hours=9), "JST"),
                    ("<+03>-3", timedelta(hours=3), "+03"),
                ):
                    with self.subTest(fixed_rule=rule):
                        kvoot_config.clear_config_notes()
                        with mock.patch.dict(os.environ, {"KVOOT_TZ": "", "TZ": rule}):
                            tz_obj = kvoot_config.resolve_tz()
                        for u in (jan, july):
                            loc = u.astimezone(tz_obj)
                            self.assertEqual(loc.timestamp(), u.timestamp())
                            self.assertEqual(loc.utcoffset(), exp_off)
                            self.assertEqual(loc.dst(), timedelta(0))
                            self.assertEqual(loc.tzname(), exp_name)
                        self.assertEqual(kvoot_config.get_config_notes(), [])

                # 2) Standard EU DST + Sol v4.3 early/late transition cases
                kvoot_config.clear_config_notes()
                with mock.patch.dict(os.environ, {"KVOOT_TZ": "", "TZ": "EET-2EEST,M3.5.0/3,M10.5.0/4"}):
                    tz_eet = kvoot_config.resolve_tz()
                self.assertEqual(july.astimezone(tz_eet).utcoffset(), timedelta(hours=3))
                self.assertEqual(july.astimezone(tz_eet).dst(), timedelta(hours=1))
                self.assertEqual(jan.astimezone(tz_eet).utcoffset(), timedelta(hours=2))
                self.assertEqual(jan.astimezone(tz_eet).dst(), timedelta(0))
                self.assertEqual(july.astimezone(tz_eet).tzname(), "EEST")
                self.assertEqual(jan.astimezone(tz_eet).tzname(), "EET")

                kvoot_config.clear_config_notes()
                with mock.patch.dict(os.environ, {"KVOOT_TZ": "", "TZ": "EET-2EEST,M3.1.0/3,M10.1.0/4"}):
                    tz_early = kvoot_config.resolve_tz()
                dt_m15 = mar15.astimezone(tz_early)
                self.assertEqual(dt_m15.timestamp(), mar15.timestamp())
                self.assertEqual(dt_m15.utcoffset(), timedelta(hours=3))
                self.assertEqual(dt_m15.hour, 15)
                self.assertEqual(dt_m15.tzname(), "EEST")

                kvoot_config.clear_config_notes()
                with mock.patch.dict(os.environ, {"KVOOT_TZ": "", "TZ": "EET-2EEST,M4.1.0/3,M10.5.0/4"}):
                    tz_late = kvoot_config.resolve_tz()
                dt_m28 = mar28.astimezone(tz_late)
                dt_a02 = apr02.astimezone(tz_late)
                self.assertEqual(dt_m28.utcoffset(), timedelta(hours=2))
                self.assertEqual(dt_a02.utcoffset(), timedelta(hours=2))
                self.assertEqual(dt_a02.hour, 14)
                self.assertEqual(dt_a02.tzname(), "EET")
                self.assertEqual(july.astimezone(tz_late).utcoffset(), timedelta(hours=3))

                # 3) SOL-02 exact audit cases: 2-hour DST and negative DST
                # 3a) EET-2EEST-4,M3.5.0/3,M10.5.0/4 at 2026-10-25T01:30:00Z -> 03:30+02:00 (fold=1)
                kvoot_config.clear_config_notes()
                with mock.patch.dict(os.environ, {"KVOOT_TZ": "", "TZ": "EET-2EEST-4,M3.5.0/3,M10.5.0/4"}):
                    tz_2h = kvoot_config.resolve_tz()
                u_2h = datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)
                v_2h = u_2h.astimezone(tz_2h)
                self.assertEqual(v_2h.isoformat(), "2026-10-25T03:30:00+02:00")
                self.assertEqual(v_2h.fold, 1)
                self.assertEqual(v_2h.timestamp(), u_2h.timestamp())
                self.assertEqual(v_2h.dst(), timedelta(0))
                self.assertEqual(july.astimezone(tz_2h).dst(), timedelta(hours=2))

                # 3b) IST-1GMT0,M10.5.0/2,M3.5.0/1 at 2026-10-25T00:30:00Z -> 01:30+01:00 (fold=0)
                #     and at 01:30:00Z -> 01:30+00:00 (fold=1, dst=-1h)
                kvoot_config.clear_config_notes()
                with mock.patch.dict(os.environ, {"KVOOT_TZ": "", "TZ": "IST-1GMT0,M10.5.0/2,M3.5.0/1"}):
                    tz_neg = kvoot_config.resolve_tz()
                u_neg0 = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)
                v_neg0 = u_neg0.astimezone(tz_neg)
                self.assertEqual(v_neg0.isoformat(), "2026-10-25T01:30:00+01:00")
                self.assertEqual(v_neg0.fold, 0)
                self.assertEqual(v_neg0.timestamp(), u_neg0.timestamp())
                self.assertEqual(v_neg0.dst(), timedelta(0))
                self.assertEqual(v_neg0.tzname(), "IST")

                u_neg1 = datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)
                v_neg1 = u_neg1.astimezone(tz_neg)
                self.assertEqual(v_neg1.isoformat(), "2026-10-25T01:30:00+00:00")
                self.assertEqual(v_neg1.fold, 1)
                self.assertEqual(v_neg1.timestamp(), u_neg1.timestamp())
                self.assertEqual(v_neg1.dst(), timedelta(hours=-1))
                self.assertEqual(v_neg1.tzname(), "GMT")
                self.assertEqual(jan.astimezone(tz_neg).dst(), timedelta(hours=-1))
                self.assertEqual(july.astimezone(tz_neg).dst(), timedelta(0))

                # 3c) 30-min southern-hemisphere DST (<+1030>-10:30<+11>-11,M10.1.0,M4.1.0)
                kvoot_config.clear_config_notes()
                with mock.patch.dict(os.environ, {"KVOOT_TZ": "", "TZ": "<+1030>-10:30<+11>-11,M10.1.0,M4.1.0"}):
                    tz_30m = kvoot_config.resolve_tz()
                self.assertEqual(jan.astimezone(tz_30m).dst(), timedelta(minutes=30))
                self.assertEqual(july.astimezone(tz_30m).dst(), timedelta(0))
                u_lh_fold1 = datetime(2026, 4, 4, 15, 15, tzinfo=timezone.utc)
                v_lh_fold1 = u_lh_fold1.astimezone(tz_30m)
                self.assertEqual(v_lh_fold1.isoformat(), "2026-04-05T01:45:00+10:30")
                self.assertEqual(v_lh_fold1.fold, 1)
                self.assertEqual(v_lh_fold1.timestamp(), u_lh_fold1.timestamp())
                u_lh_after = datetime(2026, 4, 4, 15, 45, tzinfo=timezone.utc)
                v_lh_after = u_lh_after.astimezone(tz_30m)
                self.assertEqual(v_lh_after.isoformat(), "2026-04-05T02:15:00+10:30")
                self.assertEqual(v_lh_after.fold, 0)
                self.assertEqual(v_lh_after.timestamp(), u_lh_after.timestamp())

                # 3d) Rule in DST on BOTH Jan 1 and Jul 1 (catches Jan 1 / Jul 1 probe-point dst() mutant)
                with mock.patch.dict(os.environ, {"KVOOT_TZ": "", "TZ": "FOO-2BAR-4,M4.1.0/2,M3.1.0/2"}):
                    tz_both = kvoot_config.resolve_tz()
                self.assertEqual(jan.astimezone(tz_both).dst(), timedelta(hours=2))
                self.assertEqual(july.astimezone(tz_both).dst(), timedelta(hours=2))
                self.assertEqual(datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc).astimezone(tz_both).dst(), timedelta(0))

                # 4) Invariant u.astimezone(tz).timestamp() == u.timestamp() every 15 min across +/-3h
                #    around transitions + Jn and n rules (leap & non-leap years)
                window_specs = [
                    ("EET-2EEST,M3.5.0/3,M10.5.0/4", [datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc), datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc)]),
                    ("EET-2EEST-4,M3.5.0/3,M10.5.0/4", [datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc), datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc)]),
                    ("<+1030>-10:30<+11>-11,M10.1.0,M4.1.0", [datetime(2026, 4, 4, 15, 0, tzinfo=timezone.utc), datetime(2026, 10, 3, 15, 30, tzinfo=timezone.utc)]),
                    ("IST-1GMT0,M10.5.0/2,M3.5.0/1", [datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc), datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc)]),
                    ("EET-2EEST,M3.1.0/3,M10.1.0/4", [datetime(2026, 3, 1, 1, 0, tzinfo=timezone.utc), datetime(2026, 10, 4, 1, 0, tzinfo=timezone.utc)]),
                    ("EET-2EEST,M4.1.0/3,M10.5.0/4", [datetime(2026, 4, 5, 1, 0, tzinfo=timezone.utc), datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc)]),
                    ("FOO-2BAR-3,J60/2,J300/2", [datetime(2024, 3, 1, 0, 0, tzinfo=timezone.utc), datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)]),
                    ("FOO-2BAR-3,59/2,300/2", [datetime(2024, 2, 29, 0, 0, tzinfo=timezone.utc), datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)]),
                    ("UTC0", [jan, july]),
                    ("JST-9", [jan, july]),
                    ("<+03>-3", [jan, july]),
                ]
                libc_probe_code = (
                    "import json, os, sys, time\n"
                    "os.environ['TZ'] = sys.argv[1]\n"
                    "time.tzset()\n"
                    "out = []\n"
                    "for ts in json.loads(sys.argv[2]):\n"
                    "    tm = time.localtime(ts)\n"
                    "    out.append([tm.tm_year, tm.tm_mon, tm.tm_mday, tm.tm_hour, tm.tm_min, tm.tm_sec, tm.tm_gmtoff, tm.tm_isdst, tm.tm_zone])\n"
                    "print(json.dumps(out))\n"
                )
                for rule, anchors in window_specs:
                    with self.subTest(window_rule=rule):
                        with mock.patch.dict(os.environ, {"KVOOT_TZ": "", "TZ": rule}):
                            tz_obj = kvoot_config.resolve_tz()
                        pts = [anchor + timedelta(minutes=m) for anchor in anchors for m in range(-180, 181, 15)]
                        for u in pts:
                            loc = u.astimezone(tz_obj)
                            self.assertEqual(loc.timestamp(), u.timestamp(), msg=f"{rule} @ {u.isoformat()} -> {loc.isoformat()} fold={loc.fold}")
                        # On POSIX, cross-check every point against libc in a separate subprocess (never mutating our TZ)
                        if hasattr(time, "tzset"):
                            ts_list = [int(u.timestamp()) for u in pts]
                            proc = subprocess.run(
                                [sys.executable, "-c", libc_probe_code, rule, json.dumps(ts_list)],
                                capture_output=True,
                                text=True,
                                timeout=10,
                                check=True,
                            )
                            glibc_rows = json.loads(proc.stdout)
                            for u, grow in zip(pts, glibc_rows):
                                loc = u.astimezone(tz_obj)
                                gy, gmo, gd, gh, gmi, gs, goff, gisdst, gzone = grow
                                self.assertEqual((loc.year, loc.month, loc.day, loc.hour, loc.minute, loc.second), (gy, gmo, gd, gh, gmi, gs))
                                self.assertEqual(int(loc.utcoffset().total_seconds()), goff)
                                self.assertEqual(loc.tzname(), gzone)
                                if gisdst == 0:
                                    self.assertEqual(loc.dst(), timedelta(0))
                                else:
                                    self.assertNotEqual(loc.dst(), timedelta(0))

                # 5) Gap (non-existent) and fold (ambiguous) local datetimes with both fold=0 and fold=1
                # EET/EEST gap 2026-03-29 03:30: fold=0 -> +02, fold=1 -> +03
                self.assertEqual(datetime(2026, 3, 29, 3, 30, tzinfo=tz_eet, fold=0).utcoffset(), timedelta(hours=2))
                self.assertEqual(datetime(2026, 3, 29, 3, 30, tzinfo=tz_eet, fold=1).utcoffset(), timedelta(hours=3))
                # EET/EEST fold 2026-10-25 03:30: fold=0 -> +03, fold=1 -> +02
                self.assertEqual(datetime(2026, 10, 25, 3, 30, tzinfo=tz_eet, fold=0).utcoffset(), timedelta(hours=3))
                self.assertEqual(datetime(2026, 10, 25, 3, 30, tzinfo=tz_eet, fold=1).utcoffset(), timedelta(hours=2))
                # 2h DST gap & fold
                self.assertEqual(datetime(2026, 3, 29, 3, 30, tzinfo=tz_2h, fold=0).utcoffset(), timedelta(hours=2))
                self.assertEqual(datetime(2026, 3, 29, 3, 30, tzinfo=tz_2h, fold=1).utcoffset(), timedelta(hours=4))
                self.assertEqual(datetime(2026, 10, 25, 3, 30, tzinfo=tz_2h, fold=0).utcoffset(), timedelta(hours=4))
                self.assertEqual(datetime(2026, 10, 25, 3, 30, tzinfo=tz_2h, fold=1).utcoffset(), timedelta(hours=2))
                # 30-min DST gap & fold
                self.assertEqual(datetime(2026, 10, 4, 2, 15, tzinfo=tz_30m, fold=0).utcoffset(), timedelta(hours=10, minutes=30))
                self.assertEqual(datetime(2026, 10, 4, 2, 15, tzinfo=tz_30m, fold=1).utcoffset(), timedelta(hours=11))
                self.assertEqual(datetime(2026, 4, 5, 1, 45, tzinfo=tz_30m, fold=0).utcoffset(), timedelta(hours=11))
                self.assertEqual(datetime(2026, 4, 5, 1, 45, tzinfo=tz_30m, fold=1).utcoffset(), timedelta(hours=10, minutes=30))
                # Negative DST gap & fold
                self.assertEqual(datetime(2026, 3, 29, 1, 30, tzinfo=tz_neg, fold=0).utcoffset(), timedelta(0))
                self.assertEqual(datetime(2026, 3, 29, 1, 30, tzinfo=tz_neg, fold=1).utcoffset(), timedelta(hours=1))
                self.assertEqual(datetime(2026, 10, 25, 1, 30, tzinfo=tz_neg, fold=0).utcoffset(), timedelta(hours=1))
                self.assertEqual(datetime(2026, 10, 25, 1, 30, tzinfo=tz_neg, fold=1).utcoffset(), timedelta(0))

                # 6) build_quota_report() preserves exact UTC epoch in resets_at during tricky folds
                with tempfile.TemporaryDirectory() as tmp:
                    codex_dir = Path(tmp) / "codex"
                    reset_2h = datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)
                    reset_neg = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)
                    _write_rollout(
                        codex_dir,
                        "2026/10/25/rollout-1.jsonl",
                        [
                            self._codex_line(
                                "2026-10-25T00:05:00Z",
                                20.0,
                                10.0,
                                r5=int(reset_2h.timestamp()),
                                r7=int(reset_neg.timestamp()),
                            )
                        ],
                    )
                    with mock.patch.dict(os.environ, {"KVOOT_TZ": "", "TZ": "EET-2EEST-4,M3.5.0/3,M10.5.0/4"}):
                        rep_2h = kvoot.build_quota_report(codex_sessions_dir=codex_dir, now=reset_2h)
                    w5_iso = rep_2h["sources"]["codex"]["window_5h"]["resets_at"]
                    self.assertEqual(w5_iso, "2026-10-25T03:30:00+02:00")
                    self.assertEqual(kvoot_config.parse_iso_timestamp(w5_iso), reset_2h)

                    with mock.patch.dict(os.environ, {"KVOOT_TZ": "", "TZ": "IST-1GMT0,M10.5.0/2,M3.5.0/1"}):
                        rep_neg = kvoot.build_quota_report(codex_sessions_dir=codex_dir, now=reset_neg)
                    w7_iso = rep_neg["sources"]["codex"]["window_weekly"]["resets_at"]
                    self.assertEqual(w7_iso, "2026-10-25T01:30:00+01:00")
                    self.assertEqual(kvoot_config.parse_iso_timestamp(w7_iso), reset_neg)

                # 7) Unset TZ uses _detect_posix_system_tz (/etc/localtime)
                kvoot_config.clear_config_notes()
                with mock.patch.dict(os.environ, {"KVOOT_TZ": "", "TZ": ""}):
                    tz_sys = kvoot_config.resolve_tz()
                self.assertEqual(july.astimezone(tz_sys).utcoffset(), timedelta(hours=3))
                self.assertEqual(jan.astimezone(tz_sys).utcoffset(), timedelta(hours=2))
                self.assertEqual(kvoot_config.get_config_notes(), [])

    def test_sol05_posix_tz_no_global_state_mutation_multithread(self) -> None:
        # Verify resolve_tz and _PosixRuleTZ conversions never mutate os.environ["TZ"] or libc localtime in another thread
        orig_tz = os.environ.get("TZ")
        with mock.patch.dict(os.environ, {"KVOOT_TZ": "", "TZ": "UTC0"}):
            tz_utc0 = kvoot_config.resolve_tz()
        with mock.patch.dict(os.environ, {"KVOOT_TZ": "", "TZ": "JST-9"}):
            u = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
            loc = u.astimezone(tz_utc0)
            self.assertEqual(loc.hour, 12)
            self.assertEqual(loc.utcoffset(), timedelta(0))
            self.assertEqual(os.environ.get("TZ"), "JST-9")
        self.assertEqual(os.environ.get("TZ"), orig_tz)

        if hasattr(time, "tzset"):
            # Sol's two-thread test in an isolated subprocess (never mutating unittest process's TZ)
            script = (
                "import os, sys, threading, time\n"
                "from datetime import datetime, timezone\n"
                f"sys.path.insert(0, {str(ROOT)!r})\n"
                "import kvoot_config\n"
                "os.environ['KVOOT_TZ'] = ''\n"
                "os.environ['TZ'] = 'UTC0'\n"
                "tz_old = kvoot_config.resolve_tz()\n"
                "os.environ['TZ'] = 'JST-9'\n"
                "time.tzset()\n"
                "tzset_calls = []\n"
                "orig_tzset = time.tzset\n"
                "def tracked_tzset():\n"
                "    tzset_calls.append(os.environ.get('TZ'))\n"
                "    return orig_tzset()\n"
                "time.tzset = tracked_tzset\n"
                "u = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)\n"
                "ts = u.timestamp()\n"
                "barrier = threading.Barrier(2)\n"
                "done = threading.Event()\n"
                "errors = []\n"
                "def worker_old():\n"
                "    try:\n"
                "        barrier.wait()\n"
                "        for _ in range(200):\n"
                "            v = u.astimezone(tz_old)\n"
                "            if v.hour != 12 or v.tzname() != 'UTC':\n"
                "                errors.append(f'old:{v.isoformat()}')\n"
                "    finally:\n"
                "        done.set()\n"
                "def worker_libc():\n"
                "    barrier.wait()\n"
                "    iters = 0\n"
                "    while not done.is_set() or iters < 300:\n"
                "        tm = time.localtime(ts)\n"
                "        if tm.tm_hour != 21 or os.environ.get('TZ') != 'JST-9':\n"
                "            errors.append(f'libc:{tm.tm_hour}:{os.environ.get(\"TZ\")}')\n"
                "            break\n"
                "        iters += 1\n"
                "t1 = threading.Thread(target=worker_old)\n"
                "t2 = threading.Thread(target=worker_libc)\n"
                "t1.start(); t2.start(); t1.join(); t2.join()\n"
                "assert not errors, errors[:5]\n"
                "assert not tzset_calls, f'Unexpected time.tzset() calls: {tzset_calls[:5]}'\n"
            )
            proc = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, msg=f"Multithread SOL-05 test failed:\n{proc.stderr}")

    def test_sol02_posix_tz_year_boundary_and_libc(self) -> None:
        # 1) Sol v4.5 exact year-boundary repros: FOO-2BAR-3,J2/0,M1.1.0/2
        kvoot_config.clear_config_notes()
        with mock.patch.dict(os.environ, {"KVOOT_TZ": "", "TZ": "FOO-2BAR-3,J2/0,M1.1.0/2"}):
            tz_flip = kvoot_config.resolve_tz()
        self.assertEqual(kvoot_config.get_config_notes(), [])
        for u_iso, exp_iso in (
            ("2022-12-31T22:30:00+00:00", "2023-01-01T00:30:00+02:00"),
            ("2033-12-31T22:30:00+00:00", "2034-01-01T00:30:00+02:00"),
        ):
            u = datetime.fromisoformat(u_iso)
            v = u.astimezone(tz_flip)
            self.assertEqual(v.isoformat(), exp_iso)
            self.assertEqual(v.fold, 0)
            self.assertEqual(v.timestamp(), u.timestamp())
            self.assertEqual(v.astimezone(timezone.utc), u)

        # 2) build_quota_report() preserves exact UTC instant in resets_at on year-boundary repro
        with tempfile.TemporaryDirectory() as tmp:
            codex_dir = Path(tmp) / "codex"
            reset_yb = datetime(2033, 12, 31, 22, 30, tzinfo=timezone.utc)
            now_yb = datetime(2033, 12, 31, 22, 0, tzinfo=timezone.utc)
            _write_rollout(
                codex_dir,
                "2033/12/31/rollout-yb.jsonl",
                [
                    self._codex_line(
                        "2033-12-31T21:55:00Z",
                        15.0,
                        10.0,
                        r5=int(reset_yb.timestamp()),
                        r7=int(reset_yb.timestamp()) + 86400,
                    )
                ],
            )
            kvoot_config.clear_config_notes()
            with mock.patch.dict(os.environ, {"KVOOT_TZ": "", "TZ": "FOO-2BAR-3,J2/0,M1.1.0/2"}):
                rep_yb = kvoot.build_quota_report(codex_sessions_dir=codex_dir, now=now_yb)
            w5_iso = rep_yb["sources"]["codex"]["window_5h"]["resets_at"]
            self.assertEqual(w5_iso, "2034-01-01T00:30:00+02:00")
            self.assertEqual(kvoot_config.parse_iso_timestamp(w5_iso), reset_yb)

        # 3) Explicit check for Sol's 0/25,365/25 example at 2024-01-01T00:00:00Z
        with mock.patch.dict(os.environ, {"KVOOT_TZ": "", "TZ": "FOO-2BAR-3,0/25,365/25"}):
            tz_0_365 = kvoot_config.resolve_tz()
        u_2024 = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
        v_2024 = u_2024.astimezone(tz_0_365)
        self.assertEqual(v_2024.isoformat(), "2024-01-01T02:00:00+02:00")
        self.assertEqual(v_2024.timestamp(), u_2024.timestamp())

        # 4) Broad year-boundary sweep (every 15 min +/-6h around Jan 1 00:00 UTC and local)
        #    covering every rule class cited in Sol's v4.5 audit report
        boundary_rules = [
            # Mixed Jn/Mm.w.d where start/end order flips across calendar years
            "FOO-2BAR-3,J2/0,M1.1.0/2",
            "FOO-2BAR-3,M1.1.0/2,J2/0",
            "FOO-2BAR-3,J364/2,M12.5.0/2",
            # Southern hemisphere (Oct -> Apr, incl. 30-min DST)
            "AEST-10AEDT,M10.1.0,M4.1.0/3",
            "<+1030>-10:30<+11>-11,M10.1.0,M4.1.0",
            # 0/25,365/25 and 0/365, J1/J365
            "FOO-2BAR-3,0/25,365/25",
            "FOO-2BAR-3,0/0,365/24",
            "FOO-2BAR-3,J1/0,J365/25",
            # Transition directly on the year boundary
            "FOO-2BAR-3,J1/0,M7.1.0/2",
            "FOO-2BAR-3,M7.1.0/2,J1/0",
            "UTC0DST-1,J1/0,M7.1.0/0",
            "UTC0DST-1,M7.1.0/0,J1/0",
            # J59/J60 and 59/60 (leap vs non-leap)
            "FOO-2BAR-3,J59/2,J60/2",
            "FOO-2BAR-3,59/2,60/2",
            # Last Sunday of month + /25, /-1, +-167:59:59
            "EET-2EEST,M3.5.0/3,M10.5.0/4",
            "FOO-2BAR-3,M1.1.0/-1,M12.5.0/25",
            "FOO-2BAR-3,M3.5.0/25,M10.5.0/25",
            "FOO-2BAR-3,M3.5.0/-1,M10.5.0/-1",
            "FOO-2BAR-3,0/-167:59:59,365/167:59:59",
            "FOO-2BAR-3,J1/-167:59:59,J365/167:59:59",
            # Equal start/end, sub-hour DST duration, negative DST & western hemisphere at year boundary
            "FOO-2BAR-3,J1/2,J1/3",
            "FOO-2BAR-3,J2/2,J2/3:30",
            "FOO-3BAR-2,J2/0,M1.1.0/2",
            "FOO+5BAR+4,J2/0,M1.1.0/2",
        ]

        boundary_years = (1970, 2000, 2023, 2024, 2025, 2026, 2033, 2034, 2035)
        rule_points: dict[str, list[datetime]] = {}
        tz_objs: dict[str, tzinfo] = {}
        for rule in boundary_rules:
            kvoot_config.clear_config_notes()
            with mock.patch.dict(os.environ, {"KVOOT_TZ": "", "TZ": rule}):
                tz_obj = kvoot_config.resolve_tz()
            self.assertEqual(kvoot_config.get_config_notes(), [], msg=rule)
            tz_objs[rule] = tz_obj
            # Offset of STD approximates local Jan 1 00:00 in UTC so +/-6h covers both UTC and local midnight
            std_off = tz_obj.utcoffset(datetime(2026, 1, 15, 12, 0)) or timedelta(0)
            pts_set: set[datetime] = set()
            for y in boundary_years:
                utc_jan1 = datetime(y, 1, 1, 0, 0, tzinfo=timezone.utc)
                local_jan1_utc = utc_jan1 - std_off
                for anchor in (utc_jan1, local_jan1_utc):
                    for m in range(-360, 361, 15):
                        dt_pt = anchor + timedelta(minutes=m)
                        if dt_pt.timestamp() >= 0:
                            pts_set.add(dt_pt)
            # Leap-day window for J59/J60 and 59/60 plus 32-bit epoch boundary
            for y in (2000, 2024, 2026):
                for d_off in (-1, 0, 1, 2):
                    pts_set.add(datetime(y, 2, 28, 22, 0, tzinfo=timezone.utc) + timedelta(days=d_off))
                    pts_set.add(datetime(y, 2, 28, 23, 0, tzinfo=timezone.utc) + timedelta(days=d_off))
                    pts_set.add(datetime(y, 3, 1, 0, 0, tzinfo=timezone.utc) + timedelta(days=d_off))
            for ts_edge in (0, 1, 2147483646, 2147483647, 2147483648):
                pts_set.add(datetime.fromtimestamp(ts_edge, tz=timezone.utc))
            pts = sorted(pts_set)
            rule_points[rule] = pts
            for u in pts:
                loc = u.astimezone(tz_obj)
                self.assertEqual(
                    loc.timestamp(),
                    u.timestamp(),
                    msg=f"{rule} @ {u.isoformat()} -> {loc.isoformat()} fold={loc.fold}",
                )
                self.assertEqual(loc.astimezone(timezone.utc), u, msg=f"{rule} roundtrip @ {u.isoformat()}")

        # On POSIX, cross-check all rules and points against libc in one isolated subprocess
        if hasattr(time, "tzset"):
            batch_probe = (
                "import json, os, sys, time\n"
                "req = json.load(sys.stdin)\n"
                "res = {}\n"
                "for rule, ts_list in req.items():\n"
                "    os.environ['TZ'] = rule\n"
                "    time.tzset()\n"
                "    rows = []\n"
                "    for ts in ts_list:\n"
                "        tm = time.localtime(ts)\n"
                "        rows.append([tm.tm_year, tm.tm_mon, tm.tm_mday, tm.tm_hour, tm.tm_min, tm.tm_sec, tm.tm_gmtoff, tm.tm_isdst, tm.tm_zone])\n"
                "    res[rule] = rows\n"
                "json.dump(res, sys.stdout)\n"
            )
            payload_req = {rule: [int(u.timestamp()) for u in pts] for rule, pts in rule_points.items()}
            proc = subprocess.run(
                [sys.executable, "-c", batch_probe],
                input=json.dumps(payload_req),
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            glibc_all = json.loads(proc.stdout)
            for rule, pts in rule_points.items():
                tz_obj = tz_objs[rule]
                with self.subTest(libc_year_boundary=rule):
                    for u, grow in zip(pts, glibc_all[rule]):
                        loc = u.astimezone(tz_obj)
                        gy, gmo, gd, gh, gmi, gs, goff, gisdst, gzone = grow
                        self.assertEqual(
                            (loc.year, loc.month, loc.day, loc.hour, loc.minute, loc.second),
                            (gy, gmo, gd, gh, gmi, gs),
                            msg=f"{rule} @ {u.isoformat()} -> {loc.isoformat()} vs glibc {grow}",
                        )
                        self.assertEqual(int(loc.utcoffset().total_seconds()), goff, msg=f"{rule} @ {u.isoformat()}")
                        self.assertEqual(loc.tzname(), gzone, msg=f"{rule} @ {u.isoformat()}")
                        if gisdst == 0:
                            self.assertEqual(loc.dst(), timedelta(0), msg=f"{rule} @ {u.isoformat()}")
                        else:
                            self.assertNotEqual(loc.dst(), timedelta(0), msg=f"{rule} @ {u.isoformat()}")

        # 5) Extreme year 1 and year 9999 safety (no OverflowError on 0/-167:59:59, 365/167:59:59)
        tz_ext = tz_objs["FOO-2BAR-3,0/-167:59:59,365/167:59:59"]
        for u_ext in (
            datetime(1, 1, 1, 0, 30, tzinfo=timezone.utc),
            datetime(9999, 12, 31, 12, 0, tzinfo=timezone.utc),
        ):
            loc_ext = u_ext.astimezone(tz_ext)
            self.assertEqual(loc_ext.astimezone(timezone.utc), u_ext)

    def test_version_single_source(self) -> None:
        self.assertEqual(kvoot.__version__, "4.7")
        self.assertIn(f"(v{kvoot.__version__})", kvoot.__doc__ or "")
        for lang_code in ("en", "et", "fi"):
            raw = json.loads((ROOT / "lang" / f"{lang_code}.json").read_text(encoding="utf-8"))
            self.assertIn("{version}", raw["cli_desc"])
            self.assertNotIn("v4)", raw["cli_desc"])
            with mock.patch.dict(os.environ, {"KVOOT_LANG": lang_code}):
                buf = io.StringIO()
                with mock.patch("sys.stdout", buf):
                    with self.assertRaises(SystemExit) as ctx:
                        kvoot.main(["--help"])
                self.assertEqual(ctx.exception.code, 0)
                self.assertIn(f"(v{kvoot.__version__})", buf.getvalue())

    def test_i20_config_utf8_bom(self) -> None:
        now = datetime(2026, 11, 18, 9, 0, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "kvoot.config.json"
            payload = json.dumps({"worker_limit": 33, "timezone": "UTC", "lang": "fi"}).encode("utf-8")
            cfg_path.write_bytes(b"\xef\xbb\xbf" + payload)
            with mock.patch.dict(os.environ, {"KVOOT_CONFIG": str(cfg_path), "KVOOT_TZ": ""}):
                kvoot_config.clear_config_notes()
                cfg = kvoot_config.load_config()
                notes = kvoot_config.get_config_notes()
                rep = kvoot.build_quota_report(codex_sessions_dir=FIXTURES_DIR, now=now)
        self.assertEqual(cfg.get("worker_limit"), 33)
        self.assertEqual(notes, [])
        self.assertEqual(rep["limits"]["worker"], 33.0)
        self.assertTrue(rep["read_at"].endswith("+00:00"))

    def test_paigalda_ikoon_rejects_unc_path(self) -> None:
        ps_script = ROOT / "paigalda_ikoon.ps1"
        self.assertTrue(ps_script.is_file())
        ps_text = ps_script.read_text(encoding="utf-8")
        self.assertIn("Run this from a local Windows folder", ps_text)

        if os.name == "nt":
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                shortcut_path = tmp_path / "Kvoot.lnk"
                for unc in (
                    r"\\wsl.localhost\Ubuntu\home\alice\kvoodimootur",
                    r"\\wsl$\Ubuntu\home\alice\kvoodimootur",
                    r"\\server\share\kvoodimootur",
                ):
                    proc = subprocess.run(
                        [
                            "powershell.exe",
                            "-NoProfile",
                            "-ExecutionPolicy",
                            "Bypass",
                            "-File",
                            str(ps_script),
                            "-RepoDir",
                            unc,
                            "-ShortcutPath",
                            str(shortcut_path),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=15,
                        check=False,
                    )
                    self.assertNotEqual(proc.returncode, 0, msg=f"UNC path unexpectedly allowed: {unc}")
                    self.assertIn("Run this from a local Windows folder", proc.stdout + proc.stderr)
                    self.assertFalse(shortcut_path.exists())

                local_repo = tmp_path / "local_repo"
                local_repo.mkdir()
                shutil.copy2(ROOT / "kvoot_ava.pyw", local_repo / "kvoot_ava.pyw")
                shutil.copy2(ROOT / "kvoot.ico", local_repo / "kvoot.ico")
                proc_ok = subprocess.run(
                    [
                        "powershell.exe",
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(ps_script),
                        "-RepoDir",
                        str(local_repo),
                        "-ShortcutPath",
                        str(shortcut_path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                self.assertEqual(proc_ok.returncode, 0, msg=proc_ok.stdout + proc_ok.stderr)
                self.assertTrue(shortcut_path.is_file())

    def test_export_rejects_symlinks_junctions_and_non_manifest_collisions(self) -> None:
        export_script = ROOT / "tools" / "export_public.py"

        def _simulate_prior_export_without_license(out_dir: Path) -> None:
            proc_init = subprocess.run(
                [sys.executable, str(export_script), "--out", str(out_dir)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_init.returncode, 0, msg=proc_init.stderr)
            (out_dir / "LICENSE").unlink()
            marker = out_dir / ".kvoot-export"
            manifest = json.loads(marker.read_text(encoding="utf-8"))
            manifest["files"] = [f for f in manifest["files"] if f != "LICENSE"]
            marker.write_text(json.dumps(manifest), encoding="utf-8")

        # 1) Non-manifest regular file collision with exported file (LICENSE) must fail without overwriting
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_dir = tmp_path / "export_collision"
            _simulate_prior_export_without_license(out_dir)
            (out_dir / "LICENSE").write_text("USER_LICENSE_KEEP\n", encoding="utf-8")
            proc_col = subprocess.run(
                [sys.executable, str(export_script), "--out", str(out_dir), "--force"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(proc_col.returncode, 0)
            self.assertEqual((out_dir / "LICENSE").read_text(encoding="utf-8"), "USER_LICENSE_KEEP\n")
            self.assertTrue((out_dir / "kvoot.py").is_file())

        # 2) File symlink pointing outside target (LICENSE -> outside.txt)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_dir = tmp_path / "export_file_symlink"
            _simulate_prior_export_without_license(out_dir)
            outside_file = tmp_path / "outside.txt"
            outside_file.write_text("KEEP_OUTSIDE\n", encoding="utf-8")
            try:
                (out_dir / "LICENSE").symlink_to(outside_file)
            except (OSError, NotImplementedError) as exc:
                if os.name != "nt":
                    raise
                # Windows without Developer Mode / symlink privilege: skip file-symlink subcase, junction is tested below
                symlink_supported = False
                symlink_skip_reason = str(exc)
            else:
                symlink_supported = True
                symlink_skip_reason = ""

            if symlink_supported:
                proc_sym = subprocess.run(
                    [sys.executable, str(export_script), "--out", str(out_dir), "--force"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(proc_sym.returncode, 0)
                self.assertEqual(outside_file.read_text(encoding="utf-8"), "KEEP_OUTSIDE\n")
                self.assertTrue((out_dir / "kvoot.py").is_file())

        # 3) Intermediate directory symlink or Windows NTFS junction (lang -> outside_lang)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_dir = tmp_path / "export_dir_link"
            proc_init = subprocess.run(
                [sys.executable, str(export_script), "--out", str(out_dir)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_init.returncode, 0, msg=proc_init.stderr)
            shutil.rmtree(out_dir / "lang")
            outside_lang = tmp_path / "outside_lang"
            outside_lang.mkdir()
            (outside_lang / "en.json").write_text('{"keep": true}\n', encoding="utf-8")

            if os.name == "nt":
                mk = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(out_dir / "lang"), str(outside_lang)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if mk.returncode != 0:
                    self.skipTest(f"NTFS junction creation unavailable: {mk.stderr or mk.stdout}")
            else:
                (out_dir / "lang").symlink_to(outside_lang, target_is_directory=True)

            proc_dir = subprocess.run(
                [sys.executable, str(export_script), "--out", str(out_dir), "--force"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(proc_dir.returncode, 0)
            self.assertEqual((outside_lang / "en.json").read_text(encoding="utf-8"), '{"keep": true}\n')
            self.assertTrue((out_dir / "kvoot.py").is_file())

            if os.name == "nt" and not symlink_supported:
                # Junction test passed; file symlink unavailable without privilege on Windows
                _ = symlink_skip_reason

    def test_sol06_readme_quickstart_and_exported_folder(self) -> None:
        expected_clone = "git clone https://github.com/T" + "Calv" + "ion/multi-ai-quota-monitor.git"
        expected_cd = "cd multi-ai-quota-monitor"
        for fname in ("README.md", "README.et.md"):
            text = (ROOT / fname).read_text(encoding="utf-8")
            self.assertIn(expected_clone, text, msg=f"Missing public clone URL in {fname}")
            self.assertIn(expected_cd, text, msg=f"Missing public folder cd command in {fname}")
            self.assertNotIn("cd kvoodimootur", text, msg=f"Stale folder cd command in {fname}")
            self.assertNotIn("<repo-url>", text, msg=f"Placeholder repo URL left in {fname}")
            # Ensure no non-title line references kvoodimootur as a folder/path
            for lineno, line in enumerate(text.splitlines(), start=1):
                if lineno == 1:
                    continue
                self.assertNotIn(
                    "kvoodimootur",
                    line,
                    msg=f"Unexpected kvoodimootur reference in {fname}:{lineno}: {line}",
                )

        # Verify quickstart runs verbatim inside a fresh exported folder named multi-ai-quota-monitor
        export_script = ROOT / "tools" / "export_public.py"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo_dir = tmp_path / "multi-ai-quota-monitor"
            proc_exp = subprocess.run(
                [sys.executable, str(export_script), "--out", str(repo_dir)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_exp.returncode, 0, msg=proc_exp.stderr)
            self.assertTrue((repo_dir / "kvoot.py").is_file())

            fake_home = tmp_path / "home"
            empty_codex = fake_home / "codex_empty"
            empty_codex.mkdir(parents=True)
            env = os.environ.copy()
            env.update(
                {
                    "KVOOT_KODU": str(fake_home),
                    "KVOOT_WIN_HOME": str(fake_home),
                    "KVOOT_STATE_DIR": str(fake_home / ".kvoot"),
                    "KVOOT_CODEX_DIRS": f"local={empty_codex}",
                    "KVOOT_TZ": "UTC",
                    "KVOOT_LANG": "en",
                }
            )
            env.pop("KVOOT_CONFIG", None)
            proc_run = subprocess.run(
                [sys.executable, "kvoot.py"],
                cwd=str(repo_dir),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_run.returncode, 0, msg=f"stdout={proc_run.stdout}\nstderr={proc_run.stderr}")
            json_out = repo_dir / "kvoot.json"
            html_out = repo_dir / "kvoot.html"
            self.assertTrue(json_out.is_file(), msg="kvoot.json was not written in multi-ai-quota-monitor")
            self.assertTrue(html_out.is_file(), msg="kvoot.html was not written in multi-ai-quota-monitor")
            parsed = json.loads(json_out.read_text(encoding="utf-8"))
            self.assertEqual(parsed.get("schema"), "kvoot/4")
            html_text = html_out.read_text(encoding="utf-8")
            self.assertIn("<!DOCTYPE html>", html_text)
            self.assertIn("tzSelect", html_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
