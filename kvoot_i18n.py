#!/usr/bin/env python3
"""Mitmekeelsuse (i18n) tugi: laeb lang/*.json failid, valib keele ja tõlgib võtmed."""

from __future__ import annotations

import ctypes
import json
import locale
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import kvoot_config

LANG_DIR = Path(__file__).resolve().parent / "lang"


def available_languages(lang_dir: Path | None = None) -> dict[str, dict[str, str]]:
    """Laeb kõik lang/*.json keelefailid sõnastikku (ilma koodimuudatusteta uue keele lisamisel)."""
    target_dir = lang_dir or LANG_DIR
    bundles: dict[str, dict[str, str]] = {}
    if not target_dir.exists() or not target_dir.is_dir():
        return bundles

    files = sorted(target_dir.glob("*.json"), key=lambda p: (0 if p.stem == "en" else 1, p.stem.lower()))
    for fp in files:
        code = fp.stem.strip().lower()
        if not code:
            continue
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(raw, dict):
            bundles[code] = {str(k): str(v) for k, v in raw.items() if isinstance(v, (str, int, float))}
    return bundles


def _normalize_lang_tag(raw: str | None, bundles: dict[str, dict[str, str]]) -> str | None:
    if not raw or not isinstance(raw, str):
        return None
    cleaned = raw.strip().split(".", 1)[0].split("@", 1)[0].replace("-", "_").lower()
    if not cleaned or cleaned in ("c", "posix"):
        return None
    if cleaned in bundles:
        return cleaned
    primary = cleaned.split("_", 1)[0]
    if primary in bundles:
        return primary
    name_aliases = {
        "english": "en",
        "estonian": "et",
        "eesti": "et",
        "finnish": "fi",
        "suomi": "fi",
    }
    alias = name_aliases.get(primary)
    if alias and alias in bundles:
        return alias
    return None


def detect_system_lang(lang_dir: Path | None = None) -> str:
    """Tuvastab süsteemi keele ja tagastab saadaoleva keelekoodi (varuks 'en')."""
    bundles = available_languages(lang_dir)
    if not bundles:
        return "en"

    for env_key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        tag = _normalize_lang_tag(os.environ.get(env_key), bundles)
        if tag:
            return tag

    if os.name == "nt" and hasattr(ctypes, "windll"):
        try:
            lcid = int(ctypes.windll.kernel32.GetUserDefaultUILanguage())
            win_loc = locale.windows_locale.get(lcid)
            tag = _normalize_lang_tag(win_loc, bundles)
            if tag:
                return tag
        except Exception:
            pass

    try:
        loc_lang = locale.getlocale()[0]
        tag = _normalize_lang_tag(loc_lang, bundles)
        if tag:
            return tag
    except Exception:
        pass

    return "en" if "en" in bundles else next(iter(bundles))


def resolve_lang(lang: str | None = None, lang_dir: Path | None = None) -> str:
    """Valib keele järjekorras: otsene argument -> KVOOT_LANG -> config -> süsteemi keel -> 'en'."""
    bundles = available_languages(lang_dir)
    if not bundles:
        return "en"

    if lang is not None:
        tag = _normalize_lang_tag(lang, bundles)
        if tag:
            return tag

    env_lang = os.environ.get("KVOOT_LANG", "").strip()
    if env_lang:
        tag = _normalize_lang_tag(env_lang, bundles)
        if tag:
            return tag

    cfg = kvoot_config.load_config()
    cfg_lang = str(cfg.get("lang") or "").strip()
    if cfg_lang:
        tag = _normalize_lang_tag(cfg_lang, bundles)
        if tag:
            return tag

    return detect_system_lang(lang_dir)


def t(key: str, lang: str | None = None, lang_dir: Path | None = None, **kwargs: Any) -> str:
    """Tõlgib võtme aktiivsesse keelde; puuduv võti tagastab ingliskeelse teksti."""
    bundles = available_languages(lang_dir)
    chosen = resolve_lang(lang, lang_dir=lang_dir)
    en_bundle = bundles.get("en", {})
    active_bundle = bundles.get(chosen, en_bundle)

    template = active_bundle.get(key)
    if not isinstance(template, str) or not template:
        template = en_bundle.get(key, key)

    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, ValueError, IndexError):
        fallback = en_bundle.get(key, key)
        try:
            return fallback.format(**kwargs)
        except (KeyError, ValueError, IndexError):
            return fallback


def render_part(part: Any, lang: str | None = None, lang_dir: Path | None = None) -> str:
    """Renderdab ühe struktureeritud tõlkeosa ({key, args} või {parts, sep}) sihtkeeles."""
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return str(part)
    prefix = str(part.get("prefix") or "")
    suffix = str(part.get("suffix") or "")
    if isinstance(part.get("parts"), list):
        sep = str(part.get("sep") if part.get("sep") is not None else "; ")
        body = sep.join(render_part(p, lang=lang, lang_dir=lang_dir) for p in part["parts"] if p is not None)
        return f"{prefix}{body}{suffix}"
    key = part.get("key")
    if not isinstance(key, str) or not key:
        return f"{prefix}{part.get('text', '')}{suffix}"
    raw_args = part.get("args")
    resolved_args: dict[str, Any] = {}
    if isinstance(raw_args, dict):
        for ak, av in raw_args.items():
            if isinstance(av, dict) and ("key" in av or isinstance(av.get("parts"), list)):
                resolved_args[ak] = render_part(av, lang=lang, lang_dir=lang_dir)
            else:
                resolved_args[ak] = av
    txt = t(key, lang=lang, lang_dir=lang_dir, **resolved_args)
    return f"{prefix}{txt}{suffix}"


def render_parts(
    parts: Sequence[Any],
    lang: str | None = None,
    lang_dir: Path | None = None,
    sep: str = "; ",
) -> str:
    """Renderdab järjestikuste tõlkeosade loendi üheks sõneks."""
    return sep.join(render_part(p, lang=lang, lang_dir=lang_dir) for p in parts if p)


def get_all_bundles(lang_dir: Path | None = None) -> dict[str, dict[str, str]]:
    """Tagastab kõik saadaolevad keelepaketid HTML-i keelelüliti jaoks."""
    return available_languages(lang_dir)
