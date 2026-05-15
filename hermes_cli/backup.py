"""
Backup and import commands for hermes CLI.

`hermes backup` creates a zip archive of the entire ~/.hermes/ directory
(excluding the hermes-agent repo and transient files).

`hermes import` restores from a backup zip, overlaying onto the current
HERMES_HOME root.
"""

import json
import os
import shutil
import sqlite3
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

from hermes_constants import get_default_hermes_root, display_hermes_home


# ---------------------------------------------------------------------------
# Exclusion rules
# ---------------------------------------------------------------------------

# Directory names to skip entirely (matched against each path component)
_EXCLUDED_DIRS = {
    "hermes-agent",     # the codebase repo — re-clone instead
    "__pycache__",      # bytecode caches — regenerated on import
    ".git",             # nested git dirs (profiles shouldn't have these, but safety)
    "node_modules",     # js deps if website/ somehow leaks in
    "backups",          # backup archives must not recursively include backups
    "checkpoints",      # session-local trajectory cache
    "state-snapshots",  # quick restore points are regenerated separately
}

# File-name suffixes to skip
_EXCLUDED_SUFFIXES = (
    ".pyc",
    ".pyo",
    ".db-wal",
    ".db-shm",
    ".db-journal",
)

# File names to skip (runtime state that's meaningless on another machine)
_EXCLUDED_NAMES = {
    "gateway.pid",
    "cron.pid",
}


def _should_exclude(rel_path: Path) -> bool:
    """Return True if *rel_path* (relative to hermes root) should be skipped."""
    parts = rel_path.parts

    # Any path component matches an excluded dir name
    for part in parts:
        if part in _EXCLUDED_DIRS:
            return True

    name = rel_path.name

    if name in _EXCLUDED_NAMES:
        return True

    if name.endswith(_EXCLUDED_SUFFIXES):
        return True

    return False


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def _format_size(nbytes: int) -> str:
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}" if unit != "B" else f"{nbytes} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


def _iter_backup_files(hermes_root: Path, out_path: Path | None = None) -> tuple[list[tuple[Path, Path]], set[str]]:
    """Collect files for a full zip backup using shared exclusion rules."""
    files_to_add: list[tuple[Path, Path]] = []
    skipped_dirs: set[str] = set()

    for dirpath, dirnames, filenames in os.walk(hermes_root, followlinks=False):
        dp = Path(dirpath)
        rel_dir = dp.relative_to(hermes_root)

        orig_dirnames = dirnames[:]
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]
        for removed in set(orig_dirnames) - set(dirnames):
            skipped_dirs.add(str(rel_dir / removed))

        for fname in filenames:
            fpath = dp / fname
            rel = fpath.relative_to(hermes_root)

            if _should_exclude(rel):
                continue

            if out_path is not None:
                try:
                    if fpath.resolve() == out_path.resolve():
                        continue
                except (OSError, ValueError):
                    pass

            files_to_add.append((fpath, rel))

    return files_to_add, skipped_dirs


def _write_backup_zip(hermes_root: Path, out_path: Path) -> tuple[int, int]:
    """Write a Hermes backup zip and return ``(file_count, total_bytes)``."""
    files_to_add, _skipped_dirs = _iter_backup_files(hermes_root, out_path)
    total_bytes = 0

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for abs_path, rel_path in files_to_add:
            try:
                zf.write(abs_path, arcname=str(rel_path))
                total_bytes += abs_path.stat().st_size
            except (PermissionError, OSError, ValueError):
                continue

    return len(files_to_add), total_bytes


def _rotate_backups(backups_dir: Path, pattern: str, keep: int) -> int:
    """Delete older matching backup files, preserving unrelated files."""
    if keep <= 0:
        keep = 1
    entries = sorted(
        backups_dir.glob(pattern),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    deleted = 0
    for old in entries[keep:]:
        try:
            old.unlink()
            deleted += 1
        except OSError:
            pass
    return deleted


def run_backup(args) -> None:
    """Create a zip backup of the Hermes home directory."""
    hermes_root = get_default_hermes_root()

    if not hermes_root.is_dir():
        print(f"Error: Hermes home directory not found at {hermes_root}")
        sys.exit(1)

    # Determine output path
    if args.output:
        out_path = Path(args.output).expanduser().resolve()
        # If user gave a directory, put the zip inside it
        if out_path.is_dir():
            stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
            out_path = out_path / f"hermes-backup-{stamp}.zip"
    else:
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        out_path = Path.home() / f"hermes-backup-{stamp}.zip"

    # Ensure the suffix is .zip
    if out_path.suffix.lower() != ".zip":
        out_path = out_path.with_suffix(out_path.suffix + ".zip")

    # Ensure parent directory exists
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Collect files
    print(f"Scanning {display_hermes_home()} ...")
    files_to_add: list[tuple[Path, Path]] = []  # (absolute, relative)
    skipped_dirs = set()

    for dirpath, dirnames, filenames in os.walk(hermes_root, followlinks=False):
        dp = Path(dirpath)
        rel_dir = dp.relative_to(hermes_root)

        # Prune excluded directories in-place so os.walk doesn't descend
        orig_dirnames = dirnames[:]
        dirnames[:] = [
            d for d in dirnames
            if d not in _EXCLUDED_DIRS
        ]
        for removed in set(orig_dirnames) - set(dirnames):
            skipped_dirs.add(str(rel_dir / removed))

        for fname in filenames:
            fpath = dp / fname
            rel = fpath.relative_to(hermes_root)

            if _should_exclude(rel):
                continue

            # Skip the output zip itself if it happens to be inside hermes root
            try:
                if fpath.resolve() == out_path.resolve():
                    continue
            except (OSError, ValueError):
                pass

            files_to_add.append((fpath, rel))

    if not files_to_add:
        print("No files to back up.")
        return

    # Create the zip
    file_count = len(files_to_add)
    print(f"Backing up {file_count} files ...")

    total_bytes = 0
    errors = []
    t0 = time.monotonic()

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for i, (abs_path, rel_path) in enumerate(files_to_add, 1):
            try:
                zf.write(abs_path, arcname=str(rel_path))
                total_bytes += abs_path.stat().st_size
            except (PermissionError, OSError, ValueError) as exc:
                errors.append(f"  {rel_path}: {exc}")
                continue

            # Progress every 500 files
            if i % 500 == 0:
                print(f"  {i}/{file_count} files ...")

    elapsed = time.monotonic() - t0
    zip_size = out_path.stat().st_size

    # Summary
    print()
    print(f"Backup complete: {out_path}")
    print(f"  Files:       {file_count}")
    print(f"  Original:    {_format_size(total_bytes)}")
    print(f"  Compressed:  {_format_size(zip_size)}")
    print(f"  Time:        {elapsed:.1f}s")

    if skipped_dirs:
        print(f"\n  Excluded directories:")
        for d in sorted(skipped_dirs):
            print(f"    {d}/")

    if errors:
        print(f"\n  Warnings ({len(errors)} files skipped):")
        for e in errors[:10]:
            print(e)
        if len(errors) > 10:
            print(f"  ... and {len(errors) - 10} more")

    print(f"\nRestore with: hermes import {out_path.name}")


# ---------------------------------------------------------------------------
# Quick snapshots and automated backup helpers
# ---------------------------------------------------------------------------

_QUICK_DEFAULT_KEEP = 20

_QUICK_PATHS = (
    "config.yaml",
    ".env",
    "auth.json",
    "state.db",
    "hermes_state.db",
    "memory_store.db",
    "gateway_state.json",
    "cron/jobs.json",
    "platforms/pairing",
    "pairing",
    "feishu_comment_pairing.json",
)


def _snapshot_root(hermes_home: Path) -> Path:
    return hermes_home / "state-snapshots"


def _safe_copy_db(src: Path, dst: Path) -> bool:
    """Copy a SQLite database using the backup API so WAL state is included."""
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        src_conn = sqlite3.connect(str(src))
        try:
            dst_conn = sqlite3.connect(str(dst))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
        return True
    except sqlite3.Error:
        try:
            if dst.exists():
                dst.unlink()
        except OSError:
            pass
        return False


def _copy_snapshot_file(src: Path, dst: Path) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix == ".db":
        if _safe_copy_db(src, dst):
            return True
    try:
        shutil.copy2(src, dst)
        return True
    except OSError:
        return False


def _iter_quick_snapshot_sources(hermes_home: Path) -> list[tuple[Path, Path]]:
    sources: list[tuple[Path, Path]] = []
    for rel_name in _QUICK_PATHS:
        src = hermes_home / rel_name
        if not src.exists():
            continue
        if src.is_file():
            rel = src.relative_to(hermes_home)
            if not _should_exclude(rel):
                sources.append((src, rel))
            continue
        for child in src.rglob("*"):
            if not child.is_file():
                continue
            rel = child.relative_to(hermes_home)
            if not _should_exclude(rel):
                sources.append((child, rel))
    return sources


def _sanitize_label(label: str | None) -> str:
    if not label:
        return ""
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in label.strip())
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return cleaned[:48]


def create_quick_snapshot(
    label: str | None = None,
    hermes_home: Path | None = None,
    keep: int = _QUICK_DEFAULT_KEEP,
) -> str | None:
    """Create a small restore point for critical Hermes state files."""
    home = Path(hermes_home) if hermes_home is not None else get_default_hermes_root()
    if not home.is_dir():
        return None

    sources = _iter_quick_snapshot_sources(home)
    if not sources:
        return None

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    suffix = _sanitize_label(label)
    snap_id = f"{stamp}-{suffix}" if suffix else stamp
    snap_dir = _snapshot_root(home) / snap_id
    snap_dir.mkdir(parents=True, exist_ok=False)

    files: list[str] = []
    total_bytes = 0
    for src, rel in sources:
        dst = snap_dir / rel
        if _copy_snapshot_file(src, dst):
            rel_posix = rel.as_posix()
            files.append(rel_posix)
            try:
                total_bytes += src.stat().st_size
            except OSError:
                pass

    if not files:
        shutil.rmtree(snap_dir, ignore_errors=True)
        return None

    manifest = {
        "id": snap_id,
        "label": suffix,
        "created_at": time.time(),
        "files": sorted(files),
        "total_bytes": total_bytes,
    }
    (snap_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    prune_quick_snapshots(keep=keep, hermes_home=home)
    return snap_id


def list_quick_snapshots(limit: int | None = None, hermes_home: Path | None = None) -> list[dict]:
    home = Path(hermes_home) if hermes_home is not None else get_default_hermes_root()
    root = _snapshot_root(home)
    if not root.is_dir():
        return []

    snapshots: list[dict] = []
    for snap_dir in root.iterdir():
        if not snap_dir.is_dir():
            continue
        manifest_path = snap_dir / "manifest.json"
        if manifest_path.exists():
            try:
                meta = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}
        else:
            meta = {}
        meta.setdefault("id", snap_dir.name)
        meta.setdefault("created_at", snap_dir.stat().st_mtime)
        meta["path"] = str(snap_dir)
        snapshots.append(meta)

    snapshots.sort(key=lambda item: item.get("created_at", 0), reverse=True)
    if limit is not None:
        return snapshots[:limit]
    return snapshots


def prune_quick_snapshots(
    keep: int = _QUICK_DEFAULT_KEEP,
    hermes_home: Path | None = None,
) -> int:
    home = Path(hermes_home) if hermes_home is not None else get_default_hermes_root()
    root = _snapshot_root(home)
    if not root.is_dir():
        return 0
    if keep <= 0:
        keep = 1

    snapshots = list_quick_snapshots(limit=None, hermes_home=home)
    deleted = 0
    for snap in snapshots[keep:]:
        path = Path(snap["path"])
        try:
            shutil.rmtree(path)
            deleted += 1
        except OSError:
            pass
    return deleted


def restore_quick_snapshot(snap_id: str, hermes_home: Path | None = None) -> bool:
    home = Path(hermes_home) if hermes_home is not None else get_default_hermes_root()
    snap_dir = _snapshot_root(home) / snap_id
    manifest_path = snap_dir / "manifest.json"
    if not manifest_path.is_file():
        return False

    try:
        meta = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False

    restored = 0
    for rel_name in meta.get("files", []):
        rel = Path(rel_name)
        src = snap_dir / rel
        dst = home / rel
        try:
            dst.resolve().relative_to(home.resolve())
        except ValueError:
            continue
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            restored += 1

    return restored > 0


def run_quick_backup(args) -> None:
    """CLI entry point for ``hermes backup --quick``."""
    snap_id = create_quick_snapshot(label=getattr(args, "label", None))
    if not snap_id:
        print("No critical state files found to snapshot.")
        return
    snap_dir = _snapshot_root(get_default_hermes_root()) / snap_id
    print(f"Quick backup complete: {snap_dir}")
    print(f"Snapshot ID: {snap_id}")


def _create_rotated_backup(
    prefix: str,
    *,
    hermes_home: Path | None = None,
    keep: int = 5,
) -> Path | None:
    home = Path(hermes_home) if hermes_home is not None else get_default_hermes_root()
    if not home.is_dir():
        return None

    backups_dir = home / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S-%f")
    out_path = backups_dir / f"{prefix}-{stamp}.zip"
    _write_backup_zip(home, out_path)
    _rotate_backups(backups_dir, f"{prefix}-*.zip", keep)
    return out_path


def create_pre_update_backup(
    *,
    hermes_home: Path | None = None,
    keep: int = 5,
) -> Path | None:
    """Create the zip backup used by ``hermes update --backup``."""
    return _create_rotated_backup("pre-update", hermes_home=hermes_home, keep=keep)


def create_pre_migration_backup(
    *,
    hermes_home: Path | None = None,
    keep: int = 5,
) -> Path | None:
    """Create the zip backup used before migration commands mutate state."""
    return _create_rotated_backup("pre-migration", hermes_home=hermes_home, keep=keep)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def _validate_backup_zip(zf: zipfile.ZipFile) -> tuple[bool, str]:
    """Check that a zip looks like a Hermes backup.

    Returns (ok, reason).
    """
    names = zf.namelist()
    if not names:
        return False, "zip archive is empty"

    # Look for telltale files that a hermes home would have
    markers = {"config.yaml", ".env", "state.db"}
    found = set()
    for n in names:
        # Could be at the root or one level deep (if someone zipped the directory)
        basename = Path(n).name
        if basename in markers:
            found.add(basename)

    if not found:
        return False, (
            "zip does not appear to be a Hermes backup "
            "(no config.yaml, .env, or state databases found)"
        )

    return True, ""


def _detect_prefix(zf: zipfile.ZipFile) -> str:
    """Detect if the zip has a common directory prefix wrapping all entries.

    Some tools zip as `.hermes/config.yaml` instead of `config.yaml`.
    Returns the prefix to strip (empty string if none).
    """
    names = [n for n in zf.namelist() if not n.endswith("/")]
    if not names:
        return ""

    # Find common prefix
    parts_list = [Path(n).parts for n in names]

    # Check if all entries share a common first directory
    first_parts = {p[0] for p in parts_list if len(p) > 1}
    if len(first_parts) == 1:
        prefix = first_parts.pop()
        # Only strip if it looks like a hermes dir name
        if prefix in (".hermes", "hermes"):
            return prefix + "/"

    return ""


def run_import(args) -> None:
    """Restore a Hermes backup from a zip file."""
    zip_path = Path(args.zipfile).expanduser().resolve()

    if not zip_path.is_file():
        print(f"Error: File not found: {zip_path}")
        sys.exit(1)

    if not zipfile.is_zipfile(zip_path):
        print(f"Error: Not a valid zip file: {zip_path}")
        sys.exit(1)

    hermes_root = get_default_hermes_root()

    with zipfile.ZipFile(zip_path, "r") as zf:
        # Validate
        ok, reason = _validate_backup_zip(zf)
        if not ok:
            print(f"Error: {reason}")
            sys.exit(1)

        prefix = _detect_prefix(zf)
        members = [n for n in zf.namelist() if not n.endswith("/")]
        file_count = len(members)

        print(f"Backup contains {file_count} files")
        print(f"Target: {display_hermes_home()}")

        if prefix:
            print(f"Detected archive prefix: {prefix!r} (will be stripped)")

        # Check for existing installation
        has_config = (hermes_root / "config.yaml").exists()
        has_env = (hermes_root / ".env").exists()

        if (has_config or has_env) and not args.force:
            print()
            print("Warning: Target directory already has Hermes configuration.")
            print("Importing will overwrite existing files with backup contents.")
            print()
            try:
                answer = input("Continue? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                sys.exit(1)
            if answer not in ("y", "yes"):
                print("Aborted.")
                return

        # Extract
        print(f"\nImporting {file_count} files ...")
        hermes_root.mkdir(parents=True, exist_ok=True)

        errors = []
        restored = 0
        t0 = time.monotonic()

        for member in members:
            # Strip prefix if detected
            if prefix and member.startswith(prefix):
                rel = member[len(prefix):]
            else:
                rel = member

            if not rel:
                continue

            target = hermes_root / rel

            # Security: reject absolute paths and traversals
            try:
                target.resolve().relative_to(hermes_root.resolve())
            except ValueError:
                errors.append(f"  {rel}: path traversal blocked")
                continue

            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as dst:
                    dst.write(src.read())
                restored += 1
            except (PermissionError, OSError) as exc:
                errors.append(f"  {rel}: {exc}")

            if restored % 500 == 0:
                print(f"  {restored}/{file_count} files ...")

        elapsed = time.monotonic() - t0

        # Summary
        print()
        print(f"Import complete: {restored} files restored in {elapsed:.1f}s")
        print(f"  Target: {display_hermes_home()}")

        if errors:
            print(f"\n  Warnings ({len(errors)} files skipped):")
            for e in errors[:10]:
                print(e)
            if len(errors) > 10:
                print(f"  ... and {len(errors) - 10} more")

        # Post-import: restore profile wrapper scripts
        profiles_dir = hermes_root / "profiles"
        restored_profiles = []
        if profiles_dir.is_dir():
            try:
                from hermes_cli.profiles import (
                    create_wrapper_script, check_alias_collision,
                    _is_wrapper_dir_in_path, _get_wrapper_dir,
                )
                for entry in sorted(profiles_dir.iterdir()):
                    if not entry.is_dir():
                        continue
                    profile_name = entry.name
                    # Only create wrappers for directories with config
                    if not (entry / "config.yaml").exists() and not (entry / ".env").exists():
                        continue
                    collision = check_alias_collision(profile_name)
                    if collision:
                        print(f"  Skipped alias '{profile_name}': {collision}")
                        restored_profiles.append((profile_name, False))
                    else:
                        wrapper = create_wrapper_script(profile_name)
                        restored_profiles.append((profile_name, wrapper is not None))

                if restored_profiles:
                    created = [n for n, ok in restored_profiles if ok]
                    skipped = [n for n, ok in restored_profiles if not ok]
                    if created:
                        print(f"\n  Profile aliases restored: {', '.join(created)}")
                    if skipped:
                        print(f"  Profile aliases skipped:  {', '.join(skipped)}")
                    if not _is_wrapper_dir_in_path():
                        print(f"\n  Note: {_get_wrapper_dir()} is not in your PATH.")
                        print('  Add to your shell config (~/.bashrc or ~/.zshrc):')
                        print('    export PATH="$HOME/.local/bin:$PATH"')
            except ImportError:
                # hermes_cli.profiles might not be available (fresh install)
                if any(profiles_dir.iterdir()):
                    print(f"\n  Profiles detected but aliases could not be created.")
                    print(f"  Run: hermes profile list  (after installing hermes)")

        # Guidance
        print()
        if not (hermes_root / "hermes-agent").is_dir():
            print("Note: The hermes-agent codebase was not included in the backup.")
            print("  If this is a fresh install, run: hermes update")

        if restored_profiles:
            gw_profiles = [n for n, _ in restored_profiles]
            print("\nTo re-enable gateway services for profiles:")
            for pname in gw_profiles:
                print(f"  hermes -p {pname} gateway install")

        print("Done. Your Hermes configuration has been restored.")
