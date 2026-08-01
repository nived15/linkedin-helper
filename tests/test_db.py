import re
import sqlite3

from linkedin_mcp.core.db import MIGRATIONS_DIR, initialize_database, migrate


def list_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        """
    ).fetchall()
    return {row["name"] for row in rows}


def expected_tables() -> set[str]:
    tables = set(
        re.findall(
            r"CREATE TABLE IF NOT EXISTS ([a-zA-Z0-9_]+)",
            (MIGRATIONS_DIR / "0001_init.sql").read_text(encoding="utf-8"),
        )
    )
    tables.add("schema_migrations")
    return tables


def test_initialize_database_applies_full_schema(tmp_path):
    db_path = tmp_path / "linkedin-helper.db"

    conn = initialize_database(db_path)

    assert expected_tables().issubset(list_tables(conn))
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    versions = [
        row["version"]
        for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    ]
    assert versions == ["0001_init"]


def test_initialize_database_is_idempotent_on_rerun(tmp_path):
    db_path = tmp_path / "linkedin-helper.db"

    first_conn = initialize_database(db_path)
    first_conn.close()

    second_conn = initialize_database(db_path)
    assert migrate(second_conn) == []
    assert second_conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1
    assert expected_tables().issubset(list_tables(second_conn))
    versions = [
        row["version"]
        for row in second_conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    ]
    assert versions == ["0001_init"]
