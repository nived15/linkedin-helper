import sqlite3

from linkedin_mcp.core.db import initialize_database, migrate


EXPECTED_TABLES = {
    "accounts",
    "account_limits",
    "working_hours",
    "actions_log",
    "safety_events",
    "worker_heartbeat",
    "leads",
    "lead_contacts",
    "lead_experience",
    "lead_education",
    "lead_skills",
    "tags",
    "lead_tags",
    "lead_custom_fields",
    "blacklist",
    "campaigns",
    "campaign_steps",
    "templates",
    "campaign_leads",
    "jobs",
    "ai_drafts",
    "messages",
    "webhooks",
    "webhook_deliveries",
    "harvest_runs",
    "schema_migrations",
}


def list_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        """
    ).fetchall()
    return {row["name"] for row in rows}


def test_initialize_database_applies_full_schema(tmp_path):
    db_path = tmp_path / "linkedin-helper.db"

    conn = initialize_database(db_path)

    assert EXPECTED_TABLES.issubset(list_tables(conn))
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    versions = [
        row["version"]
        for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    ]
    assert versions == ["0001_init"]


def test_migrate_is_idempotent_on_rerun(tmp_path):
    db_path = tmp_path / "linkedin-helper.db"

    first_conn = initialize_database(db_path)
    first_conn.close()

    second_conn = initialize_database(db_path)
    assert migrate(second_conn) == []
    assert second_conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1
    assert EXPECTED_TABLES.issubset(list_tables(second_conn))
    versions = [
        row["version"]
        for row in second_conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    ]
    assert versions == ["0001_init"]
