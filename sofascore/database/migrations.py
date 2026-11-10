"""Read the objects a migration file creates, for the apply runner and the tests."""

import re
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

_CREATE_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?(?P<kind>TABLE|VIEW|MATERIALIZED\s+VIEW|UNIQUE\s+INDEX|INDEX)"
    r"\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>[a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)

_ADD_COLUMN_RE = re.compile(
    r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?P<table>[a-z_][a-z0-9_]*)\s+"
    r"ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<column>[a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)


def strip_comments(sql: str) -> str:
    """Drop `--` line comments so commented-out statements are not counted."""
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def created_objects(sql: str) -> list[tuple[str, str]]:
    """Return (kind, name) for every CREATE and ADD COLUMN, in file order.

    kind is table/view/index (name = relation) or column (name = "table.column").
    """
    sql = strip_comments(sql)
    found = []
    for match in _CREATE_RE.finditer(sql):
        kind = match.group("kind").lower()
        kind = "index" if kind.endswith("index") else "view" if kind.endswith("view") else "table"
        found.append((match.start(), kind, match.group("name").lower()))
    for match in _ADD_COLUMN_RE.finditer(sql):
        found.append((match.start(), "column", f"{match.group('table')}.{match.group('column')}".lower()))
    return [(kind, name) for _, kind, name in sorted(found)]


def migration_files() -> list[Path]:
    """All numbered migrations, in apply order."""
    return sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
