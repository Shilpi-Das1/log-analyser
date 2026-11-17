"""Migrate the legacy shared knowledge.db to per-project DB files.

Before this change all KB entries lived in DATA_DIR/knowledge.db.
After this change each project has its own DATA_DIR/<project>/knowledge.db.

Run once after upgrading:

    python scripts/migrate_kb.py [--dry-run]

The original knowledge.db is NOT deleted; it is kept as a backup so you can
re-run this script safely or roll back manually.
"""
import argparse
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

# Allow running from the project root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config  # noqa: E402  (must come after sys.path insert)

_SCHEMA = """CREATE TABLE IF NOT EXISTS kb (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, signature TEXT NOT NULL,
    norm TEXT NOT NULL, problem TEXT NOT NULL, category TEXT DEFAULT '', fix_text TEXT DEFAULT '',
    patch_file TEXT DEFAULT '', patch_blocks TEXT DEFAULT '', status TEXT NOT NULL,
    link TEXT DEFAULT '', hits INTEGER DEFAULT 0, created TEXT, updated TEXT)"""


def _open(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    conn.commit()
    return conn


def migrate(dry_run: bool = False) -> None:
    legacy = config.DATA_DIR / "knowledge.db"
    if not legacy.exists():
        print("Nothing to migrate: legacy knowledge.db not found.")
        return

    with closing(sqlite3.connect(legacy, timeout=10)) as src:
        src.row_factory = sqlite3.Row
        rows = src.execute("SELECT * FROM kb ORDER BY id").fetchall()

    if not rows:
        print("Legacy knowledge.db is empty; nothing to migrate.")
        return

    # Group by project
    by_project: dict[str, list] = {}
    for row in rows:
        by_project.setdefault(row["project"] or "", []).append(dict(row))

    print(f"Found {len(rows)} entries across {len(by_project)} project(s).")

    for project, entries in by_project.items():
        dest = (config.DATA_DIR / project / "knowledge.db") if project else (config.DATA_DIR / "knowledge.db")
        if dest == legacy:
            print(f"  [skip] project='' maps to the same file; nothing to do.")
            continue
        print(f"  → project '{project}': {len(entries)} entries  →  {dest}")
        if dry_run:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        with closing(_open(dest)) as tgt:
            for e in entries:
                # Insert only if not already present (idempotent re-runs).
                exists = tgt.execute(
                    "SELECT 1 FROM kb WHERE project=? AND signature=?",
                    (e["project"], e["signature"])
                ).fetchone()
                if not exists:
                    tgt.execute(
                        "INSERT INTO kb (project, signature, norm, problem, category, fix_text, "
                        "patch_file, patch_blocks, status, link, hits, created, updated) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (e["project"], e["signature"], e["norm"], e["problem"], e["category"],
                         e["fix_text"], e["patch_file"], e["patch_blocks"], e["status"],
                         e["link"], e["hits"], e["created"], e["updated"]))
            tgt.commit()
        print(f"    Migrated {len(entries)} entries.")

    if not dry_run:
        backup = legacy.with_suffix(".db.bak")
        shutil.copy2(legacy, backup)
        print(f"\nOriginal knowledge.db kept as backup: {backup}")
        print("Migration complete. The legacy file has NOT been deleted.")
        print("Once satisfied, you may remove it manually.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be migrated without writing anything.")
    args = parser.parse_args()
    if args.dry_run:
        print("DRY RUN — no files will be written.\n")
    migrate(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
