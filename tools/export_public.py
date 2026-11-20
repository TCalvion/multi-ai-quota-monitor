#!/usr/bin/env python3
"""Create a clean public export directory without internal docs, logs, or git history."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXPORT_MARKER = ".kvoot-export"

PUBLIC_FILES = (
    ".gitignore",
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
    "LICENSES/Apache-2.0.txt",
    "tools/export_public.py",
)

PUBLIC_DIRS = (
    "lang",
    "tests",
)

IGNORED_DIR_NAMES = frozenset({"__pycache__", ".pytest_cache", ".git", ".venv"})
IGNORED_SUFFIXES = (".pyc", ".pyo")


def _is_stat_link_or_reparse(st: os.stat_result) -> bool:
    """Return True if an lstat result represents a symlink or Windows reparse point."""
    if stat.S_ISLNK(st.st_mode):
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and (getattr(st, "st_file_attributes", 0) & reparse_flag))


def _is_link_or_reparse(path: Path) -> bool:
    """Return True if path exists (without following links) as a symlink or Windows reparse point."""
    try:
        return _is_stat_link_or_reparse(os.lstat(path))
    except FileNotFoundError:
        return False


def _collect_planned_exports() -> list[tuple[Path, str]]:
    """Collect all (source_path, relative_posix_path) pairs to be exported."""
    planned: list[tuple[Path, str]] = []
    for rel in PUBLIC_FILES:
        src = ROOT / rel
        if not src.exists():
            raise FileNotFoundError(f"Missing expected public file: {rel}")
        planned.append((src, rel))

    for rel_dir in PUBLIC_DIRS:
        src_dir = ROOT / rel_dir
        if not src_dir.exists():
            raise FileNotFoundError(f"Missing expected public directory: {rel_dir}")
        for root, dirs, files in os_walk_sorted(src_dir):
            dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIR_NAMES)
            for fname in sorted(files):
                if fname.endswith(IGNORED_SUFFIXES):
                    continue
                src_file = root / fname
                rel_file = src_file.relative_to(ROOT).as_posix()
                planned.append((src_file, rel_file))
    return planned


def _validate_target_subpath(
    target: Path,
    target_real: Path,
    rel_posix: str,
    existing_manifest_rel: set[str] | None,
) -> None:
    """Verify via lstat and realpath that every component of rel_posix stays safely inside target."""
    rel_path = Path(rel_posix)
    if rel_path.is_absolute() or any(p.lower() == ".git" for p in rel_path.parts):
        raise ValueError(f"Unsafe export path: {rel_posix}")

    parts = rel_path.parts
    for i in range(1, len(parts) + 1):
        cur = target.joinpath(*parts[:i])
        is_final = i == len(parts)
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            break
        if _is_stat_link_or_reparse(st):
            raise ValueError(f"Refusing to follow symlink or reparse point in export target: {cur}")
        if not is_final:
            if not stat.S_ISDIR(st.st_mode):
                raise ValueError(f"Export parent path exists and is not a directory: {cur}")
        else:
            if stat.S_ISDIR(st.st_mode):
                raise ValueError(f"Export destination exists as a directory: {cur}")
            if existing_manifest_rel is not None and rel_posix not in existing_manifest_rel:
                raise ValueError(
                    f"Refusing to overwrite non-manifest file in export target: {rel_posix}"
                )

    dst = target / rel_path
    dst_real = Path(os.path.realpath(dst))
    if target_real not in dst_real.parents:
        raise ValueError(f"Export destination escapes target directory: {rel_posix}")


def _safe_copy_file(src: Path, target: Path, target_real: Path, rel_posix: str) -> None:
    """Validate destination path with lstat/realpath immediately before copying."""
    _validate_target_subpath(target, target_real, rel_posix, existing_manifest_rel=None)
    dst = target / rel_posix
    dst.parent.mkdir(parents=True, exist_ok=True)
    _validate_target_subpath(target, target_real, rel_posix, existing_manifest_rel=None)
    shutil.copy2(src, dst)


def os_walk_sorted(top: Path):
    for dirpath, dirnames, filenames in os.walk(top):
        dirnames.sort()
        filenames.sort()
        yield Path(dirpath), dirnames, filenames


def _read_manifest_files(
    marker: Path,
    target: Path,
    target_real: Path,
    out_dir: Path,
) -> tuple[list[Path], set[str]]:
    """Read and validate relative file paths from the export marker manifest."""
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid export marker manifest ({EXPORT_MARKER}): {out_dir}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        raise ValueError(f"Invalid export marker manifest ({EXPORT_MARKER}): {out_dir}")
    resolved_files: list[Path] = []
    manifest_rel_set: set[str] = set()
    for raw_rel in data["files"]:
        if not isinstance(raw_rel, str) or not raw_rel.strip():
            raise ValueError(f"Invalid manifest path in {EXPORT_MARKER}: {out_dir}")
        rel_path = Path(raw_rel)
        if rel_path.is_absolute() or any(p.lower() == ".git" for p in rel_path.parts):
            raise ValueError(f"Unsafe manifest path in {EXPORT_MARKER}: {raw_rel}")
        cand = (target / rel_path).resolve()
        if target not in cand.parents:
            raise ValueError(f"Manifest path escapes target directory: {raw_rel}")
        rel_posix = rel_path.as_posix()
        _validate_target_subpath(target, target_real, rel_posix, existing_manifest_rel=None)
        resolved_files.append(cand)
        manifest_rel_set.add(rel_posix)
    return resolved_files, manifest_rel_set


def _clean_manifest_files(target: Path, manifest_paths: list[Path], out_dir: Path) -> None:
    """Delete only files listed in the export manifest and warn about any foreign files kept."""
    parent_dirs: set[Path] = set()
    for fp in manifest_paths:
        if fp.is_file() or fp.is_symlink():
            fp.unlink()
            cur = fp.parent
            while cur != target and target in cur.parents:
                parent_dirs.add(cur)
                cur = cur.parent
    for d in sorted(parent_dirs, key=lambda p: len(p.parts), reverse=True):
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass

    kept: list[str] = []
    for p in sorted(target.rglob("*")):
        if p.name == EXPORT_MARKER and p.parent == target:
            continue
        if p.is_file() or p.is_symlink():
            kept.append(p.relative_to(target).as_posix())
    if kept:
        print(
            f"Warning: keeping non-manifest file(s) in {out_dir}: {', '.join(kept)}",
            file=sys.stderr,
        )


def export_public(out_dir: Path, force: bool = False) -> list[str]:
    """Copy clean public files into out_dir and return relative exported paths."""
    src_root = ROOT.resolve()
    target = out_dir.resolve()
    target_real = Path(os.path.realpath(target))
    if target == src_root or src_root in target.parents or target in src_root.parents:
        raise ValueError(f"Output directory must be outside repository root: {out_dir}")

    planned = _collect_planned_exports()

    manifest_paths: list[Path] | None = None
    if target.exists():
        if not target.is_dir():
            raise ValueError(f"Output path exists and is not a directory: {out_dir}")
        git_entry = target / ".git"
        if git_entry.exists() or _is_link_or_reparse(git_entry):
            raise ValueError("Target is a git repository. Export to a fresh folder and copy the files.")
        has_items = any(target.iterdir())
        if has_items:
            if not force:
                raise ValueError(f"Output directory is not empty (use --force to replace): {out_dir}")
            marker = target / EXPORT_MARKER
            if not marker.is_file() or _is_link_or_reparse(marker):
                raise ValueError(
                    f"Refusing to delete non-empty directory without export marker ({EXPORT_MARKER}): {out_dir}"
                )
            manifest_paths, manifest_rel_set = _read_manifest_files(marker, target, target_real, out_dir)
            # Validate all planned destinations BEFORE deleting or writing any files.
            for _, rel_posix in planned:
                _validate_target_subpath(target, target_real, rel_posix, manifest_rel_set)
            _validate_target_subpath(target, target_real, EXPORT_MARKER, None)
            _clean_manifest_files(target, manifest_paths, out_dir)
    else:
        target.mkdir(parents=True, exist_ok=True)

    for _, rel_posix in planned:
        _validate_target_subpath(target, target_real, rel_posix, None)

    exported: list[str] = []
    for src, rel_posix in planned:
        _safe_copy_file(src, target, target_real, rel_posix)
        exported.append(rel_posix)

    sorted_exported = sorted(exported)
    manifest = {
        "marker": "kvoodimootur public export",
        "files": sorted_exported,
    }
    _validate_target_subpath(target, target_real, EXPORT_MARKER, None)
    (target / EXPORT_MARKER).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    return sorted_exported


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export clean public source tree")
    parser.add_argument("--out", type=Path, required=True, help="Target directory for public export")
    parser.add_argument("--force", action="store_true", help="Replace existing non-empty target directory")
    args = parser.parse_args(argv)

    try:
        files = export_public(args.out, force=args.force)
    except (ValueError, FileNotFoundError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Exported {len(files)} files to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
