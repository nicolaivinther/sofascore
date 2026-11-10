r"""Apply one hand-written migration to basketball_stats.

    poetry run python -m sofascore.runners.apply_migration migrations/001_sofascore_core.sql --dry-run

--dry-run only reads the catalog: it lists every object the file creates and whether it already
exists. Without --dry-run the whole file runs in one transaction (Postgres DDL is transactional,
so a failure leaves nothing behind). Migrations are IF NOT EXISTS, so re-running is a no-op.
"""

import argparse
import sys
from pathlib import Path

from sofascore.database.connection import connect
from sofascore.database.migrations import created_objects


def existing(objects: list[tuple[str, str]]) -> set[str]:
    """Names from ``objects`` that already exist in the public schema (columns as table.column)."""
    relations = [name for kind, name in objects if kind != "column"]
    columns = [name for kind, name in objects if kind == "column"]
    conn = connect()
    try:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = ANY(%s)",
                (relations,),
            )
            found = {row[0] for row in cur.fetchall()}
            cur.execute(
                "SELECT table_name || '.' || column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name || '.' || column_name = ANY(%s)",
                (columns,),
            )
            return found | {row[0] for row in cur.fetchall()}
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("path", type=Path, help="migration file, e.g. migrations/001_sofascore_core.sql")
    parser.add_argument("--dry-run", action="store_true", help="list objects and their state; change nothing")
    args = parser.parse_args(argv)

    sql = args.path.read_text(encoding="utf-8")
    objects = created_objects(sql)
    present = existing(objects)
    for kind, name in objects:
        print(f"{kind:6}  {name:60}  {'exists' if name in present else 'NEW'}")
    new = sum(name not in present for _, name in objects)
    print(f"{len(objects)} objects, {new} new, {len(objects) - new} already present")
    if args.dry_run:
        print("dry run: nothing applied")
        return 0
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    finally:
        conn.close()
    print(f"Applied {args.path.name} ({new} new objects)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
