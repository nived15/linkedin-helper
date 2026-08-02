import re
import sqlite3

from linkedin_mcp.core.db import (
    MIGRATIONS_DIR,
    initialize_database,
    migrate,
    migration_files,
)


def list_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        """
    ).fetchall()
    return {row["name"] for row in rows}


def applied_versions(conn: sqlite3.Connection) -> list[str]:
    return [
        row["version"]
        for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    ]


def expected_versions() -> list[str]:
    return [path.stem for path in migration_files()]


def expected_tables() -> set[str]:
    tables: set[str] = set()
    for path in migration_files():
        tables.update(
            re.findall(
                r"CREATE TABLE IF NOT EXISTS ([a-zA-Z0-9_]+)",
                path.read_text(encoding="utf-8"),
            )
        )
    tables.add("schema_migrations")
    return tables


def test_migration_files_are_versioned_uniquely_and_in_order():
    versions = expected_versions()

    assert versions == sorted(versions)
    assert len(set(versions)) == len(versions)
    assert versions[0] == "0001_init"
    assert (MIGRATIONS_DIR / "0001_init.sql").exists()


def test_initialize_database_applies_full_schema(tmp_path):
    db_path = tmp_path / "linkedin-helper.db"

    conn = initialize_database(db_path)

    assert expected_tables().issubset(list_tables(conn))
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert applied_versions(conn) == expected_versions()


def test_initialize_database_is_idempotent_on_rerun(tmp_path):
    db_path = tmp_path / "linkedin-helper.db"

    first_conn = initialize_database(db_path)
    first_conn.close()

    second_conn = initialize_database(db_path)
    assert migrate(second_conn) == []
    assert second_conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == len(
        expected_versions()
    )
    assert expected_tables().issubset(list_tables(second_conn))
    assert applied_versions(second_conn) == expected_versions()
