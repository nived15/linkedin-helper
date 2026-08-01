from __future__ import annotations

import sqlite3
from pathlib import Path


DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "linkedin-helper.db"
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
MIGRATION_PATTERN = "[0-9][0-9][0-9][0-9]_*.sql"


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a SQLite connection configured for the MCP database."""
    if str(db_path) == ":memory:":
        path = ":memory:"
    else:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def migration_files(migrations_dir: str | Path = MIGRATIONS_DIR) -> list[Path]:
    """Return migration files sorted by version."""
    return sorted(Path(migrations_dir).glob(MIGRATION_PATTERN))


def ensure_schema_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def split_sql_statements(sql: str) -> list[str]:
    """Split a migration script into complete SQL statements."""
    statements: list[str] = []
    buffer_lines: list[str] = []

    for line in sql.splitlines():
        buffer_lines.append(line)
        buffer = "\n".join(buffer_lines)
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer_lines = []

    trailing = "\n".join(buffer_lines).strip()
    if trailing:
        if not sqlite3.complete_statement(trailing):
            raise ValueError("Migration contains an incomplete trailing SQL statement")
        statements.append(trailing)

    return statements


def applied_migrations(conn: sqlite3.Connection) -> set[str]:
    ensure_schema_migrations_table(conn)
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row["version"] for row in rows}


def migrate(
    conn: sqlite3.Connection,
    migrations_dir: str | Path = MIGRATIONS_DIR,
) -> list[str]:
    """Apply unapplied migrations in order and return the versions applied."""
    ensure_schema_migrations_table(conn)
    applied = applied_migrations(conn)
    applied_now: list[str] = []

    for migration_path in migration_files(migrations_dir):
        version = migration_path.stem
        if version in applied:
            continue

        migration_sql = migration_path.read_text(encoding="utf-8").strip()
        conn.execute("BEGIN")
        try:
            for statement in split_sql_statements(migration_sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (?)",
                (version,),
            )
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
        applied_now.append(version)
        applied.add(version)

    return applied_now


def initialize_database(
    db_path: str | Path = DEFAULT_DB_PATH,
    migrations_dir: str | Path = MIGRATIONS_DIR,
) -> sqlite3.Connection:
    """Open the database connection and apply all pending migrations."""
    conn = connect(db_path)
    migrate(conn, migrations_dir=migrations_dir)
    return conn
