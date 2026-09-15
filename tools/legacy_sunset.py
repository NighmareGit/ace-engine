#!/usr/bin/env python3
"""legacy/ sunset checker — stdlib only.

# SUNSET_DATE: archival date for legacy/

Default behaviour (no --execute):
  - If today < SUNSET_DATE: print days remaining, exit 0.
  - If today >= SUNSET_DATE: print archive report, exit 1.

--execute flag:
  - If today < SUNSET_DATE: refuse with days-remaining, exit 2.
  - If today >= SUNSET_DATE: MOVE legacy/ to legacy-deleted-<YYYYMMDD>/,
    print a manifest of moved files, exit 0.

The legacy/ directory is NEVER deleted — it is moved (archived) intact.
"""

import os
import shutil
import sys
from datetime import date

SUNSET_DATE = date(2026, 9, 18)

LEGACY_DIR = os.path.join(os.path.dirname(__file__), os.pardir, "legacy")


def _legacy_files():
    """Return sorted list of non-init Python files in legacy/."""
    files = []
    if not os.path.isdir(LEGACY_DIR):
        return files
    for name in sorted(os.listdir(LEGACY_DIR)):
        if name.endswith(".py") and name != "__init__.py":
            files.append(name)
    return files


def _all_files(directory):
    """Return sorted list of all files (relative paths) under directory."""
    result = []
    for root, _dirs, filenames in os.walk(directory):
        for fname in sorted(filenames):
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, directory)
            result.append(rel)
    return result


def main():
    execute = "--execute" in sys.argv

    today = date.today()
    remaining = (SUNSET_DATE - today).days

    if not execute:
        # Default mode: report only
        if remaining > 0:
            print(f"legacy/ sunset date: {SUNSET_DATE}")
            print(f"Days remaining: {remaining}")
            print("No action required — legacy/ modules are still active.")
            return 0

        # Sunset has arrived or passed — show archive report (dry)
        legacy_files = _legacy_files()
        print(f"legacy/ sunset date: {SUNSET_DATE} (today: {today})")
        print(f"Modules eligible for archival: {len(legacy_files)}")
        print()

        for fname in legacy_files:
            full = os.path.join(LEGACY_DIR, fname)
            size = os.path.getsize(full) if os.path.isfile(full) else 0
            print(f"  ARCHIVE  {fname}  ({size:,} bytes)")

        print()
        print("__init__.py  (package init — moved with directory)")
        print()
        print("Run with --execute to move legacy/ to the archive directory.")
        return 1

    # --execute flag
    if remaining > 0:
        print(f"REFUSED: --execute requested but sunset date {SUNSET_DATE} is in the future.")
        print(f"Days remaining: {remaining}")
        print(f"Cannot archive legacy/ until {SUNSET_DATE}.")
        return 2

    # --execute on or after sunset date: archive by moving
    if not os.path.isdir(LEGACY_DIR):
        print("ERROR: legacy/ directory not found — nothing to archive.")
        return 1

    archive_name = f"legacy-deleted-{today.strftime('%Y%m%d')}"
    archive_path = os.path.join(os.path.dirname(LEGACY_DIR), archive_name)

    if os.path.exists(archive_path):
        print(f"ERROR: archive directory already exists: {archive_name}/")
        print("Manual intervention required — not overwriting.")
        return 1

    # Build manifest before moving
    files_to_move = _all_files(LEGACY_DIR)
    print(f"Archiving legacy/ → {archive_name}/")
    print(f"Files to move: {len(files_to_move)}")
    print()

    # MOVE the entire directory (never delete)
    shutil.move(LEGACY_DIR, archive_path)
    print("Move complete. Manifest of archived files:")
    print()

    for relpath in files_to_move:
        full = os.path.join(archive_path, relpath)
        size = os.path.getsize(full) if os.path.isfile(full) else 0
        print(f"  {relpath}  ({size:,} bytes)")

    print()
    print(f"Archive location: {archive_path}/")
    print("legacy/ has been moved — imports will now fail with ModuleNotFoundError.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
