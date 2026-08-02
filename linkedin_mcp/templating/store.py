"""CRUD for the `templates` table, with validation on every write.

The table already existed in `0001_init.sql` and is shaped for exactly this
feature, so SEQ-02 adds no migration. `variations_json` holds the whole-message
variations, `kind` distinguishes static from ai from hybrid, and `ai_spec_json`
is the seam SEQ-05 (#23) fills in.

Every write validates. A body that cannot be parsed, names a token the renderer
has never heard of, or breaks the writing style rules is refused here rather
than stored and discovered later against a real lead. The connection, commit and
error conventions mirror :mod:`linkedin_mcp.leads.store`.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from linkedin_mcp.templating.errors import TemplateNotFoundError
from linkedin_mcp.templating.parser import (
    AI_KINDS,
    TEMPLATE_KINDS,
    Program,
    compile_bodies,
)
from linkedin_mcp.templating.style import DEFAULT_STYLE, StylePolicy


__all__ = [
    "AI_KINDS",
    "TEMPLATE_COLUMNS",
    "TEMPLATE_KINDS",
    "WRITABLE_FIELDS",
    "Template",
    "compile_template",
    "count_templates",
    "create_template",
    "delete_template",
    "get_template",
    "get_template_by_name",
    "inline_template",
    "list_templates",
    "require_template",
    "template_from_row",
    "update_template",
]

TEMPLATE_COLUMNS: tuple[str, ...] = (
    "id",
    "account_id",
    "name",
    "body",
    "variations_json",
    "kind",
    "ai_spec_json",
    "is_ai_generated",
    "created_at",
)

WRITABLE_FIELDS: frozenset[str] = frozenset(
    {"name", "body", "variations", "kind", "ai_spec", "is_ai_generated"}
)


@dataclass(frozen=True, slots=True)
class Template:
    """One message template: a body, its variations and how it is filled.

    `id` and `account_id` are None for a template that is not backed by a row,
    which is what `campaign_preview` and ad-hoc rendering use.
    """

    name: str
    body: str
    kind: str = "static"
    variations: tuple[str, ...] = ()
    ai_spec: dict[str, Any] = field(default_factory=dict)
    is_ai_generated: bool = False
    id: int | None = None
    account_id: int | None = None
    created_at: str | None = None

    def bodies(self) -> tuple[str, ...]:
        """The body plus its whole-message variations, in queue-assignment order."""
        return (self.body, *self.variations)

    @property
    def uses_ai(self) -> bool:
        return self.kind in AI_KINDS


def inline_template(
    body: str,
    *,
    name: str = "inline",
    kind: str = "static",
    variations: Sequence[str] = (),
    ai_spec: Mapping[str, Any] | None = None,
) -> Template:
    """Wrap a raw body as a `Template` without touching the database.

    Deliberately does not validate. Rendering an unparseable inline body should
    produce a `RenderRefusal`, not an exception from a constructor, so a caller
    experimenting with template text still gets the fail-safe behaviour.
    """
    return Template(
        name=name,
        body=body,
        kind=kind,
        variations=tuple(variations),
        ai_spec=dict(ai_spec or {}),
    )


def compile_template(
    template: Template,
    policy: StylePolicy = DEFAULT_STYLE,
) -> tuple[Program, ...]:
    """Parse and validate every body on a template."""
    return compile_bodies(template.bodies(), kind=template.kind, policy=policy)


def _require_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("template name is required")
    return cleaned


def _load_variations(raw: str | None) -> tuple[str, ...]:
    parsed = json.loads(raw or "[]")
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item) for item in parsed)


def _load_ai_spec(raw: str | None) -> dict[str, Any]:
    parsed = json.loads(raw or "{}")
    return parsed if isinstance(parsed, dict) else {}


def template_from_row(row: sqlite3.Row) -> Template:
    return Template(
        id=row["id"],
        account_id=row["account_id"],
        name=row["name"],
        body=row["body"],
        kind=row["kind"],
        variations=_load_variations(row["variations_json"]),
        ai_spec=_load_ai_spec(row["ai_spec_json"]),
        is_ai_generated=bool(row["is_ai_generated"]),
        created_at=row["created_at"],
    )


def create_template(
    conn: sqlite3.Connection,
    account_id: int,
    name: str,
    body: str,
    *,
    kind: str = "static",
    variations: Sequence[str] = (),
    ai_spec: Mapping[str, Any] | None = None,
    is_ai_generated: bool = False,
    policy: StylePolicy = DEFAULT_STYLE,
) -> Template:
    """Store a validated template and return it.

    Raises `TemplateSyntaxError` or `TemplateStyleError` before writing anything,
    so the table only ever holds templates that parse and that respect the
    writing style rules.
    """
    cleaned_name = _require_name(name)
    variation_bodies = tuple(variations)
    compile_bodies((body, *variation_bodies), kind=kind, policy=policy)

    cursor = conn.execute(
        """
        INSERT INTO templates
            (account_id, name, body, variations_json, kind, ai_spec_json, is_ai_generated)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            account_id,
            cleaned_name,
            body,
            json.dumps(list(variation_bodies)),
            kind,
            json.dumps(dict(ai_spec or {}), sort_keys=True),
            int(bool(is_ai_generated)),
        ),
    )
    conn.commit()

    created = get_template(conn, int(cursor.lastrowid))
    if created is None:  # pragma: no cover - the insert just succeeded
        raise TemplateNotFoundError(int(cursor.lastrowid))
    return created


def get_template(conn: sqlite3.Connection, template_id: int) -> Template | None:
    row = conn.execute(
        "SELECT * FROM templates WHERE id = ?", (template_id,)
    ).fetchone()
    return None if row is None else template_from_row(row)


def get_template_by_name(
    conn: sqlite3.Connection,
    account_id: int,
    name: str,
) -> Template | None:
    row = conn.execute(
        "SELECT * FROM templates WHERE account_id = ? AND name = ?",
        (account_id, _require_name(name)),
    ).fetchone()
    return None if row is None else template_from_row(row)


def require_template(
    conn: sqlite3.Connection,
    ref: int | str,
    *,
    account_id: int | None = None,
) -> Template:
    """Resolve a template by id, or by name within an account. Never returns None."""
    if isinstance(ref, int):
        found = get_template(conn, ref)
    else:
        if account_id is None:
            raise ValueError("resolving a template by name needs an account_id")
        found = get_template_by_name(conn, account_id, ref)
    if found is None:
        raise TemplateNotFoundError(ref)
    return found


def list_templates(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    kind: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[Template]:
    sql = "SELECT * FROM templates WHERE account_id = ?"
    params: list[Any] = [account_id]
    if kind is not None:
        if kind not in TEMPLATE_KINDS:
            raise ValueError(
                f"unknown template kind {kind!r}; expected one of "
                f"{', '.join(TEMPLATE_KINDS)}"
            )
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY templates.id"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params += [limit, offset]

    rows = conn.execute(sql, params).fetchall()
    return [template_from_row(row) for row in rows]


def count_templates(conn: sqlite3.Connection, account_id: int) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM templates WHERE account_id = ?", (account_id,)
        ).fetchone()[0]
    )


def update_template(
    conn: sqlite3.Connection,
    template_id: int,
    *,
    policy: StylePolicy = DEFAULT_STYLE,
    **fields: Any,
) -> Template:
    """Update writable fields, revalidating the whole template against the result.

    Validation runs on the merged template rather than on the changed field
    alone. Switching `kind` to `static` while the body still references an AI
    fragment has to fail, and it only looks wrong once the two are seen together.
    """
    current = get_template(conn, template_id)
    if current is None:
        raise TemplateNotFoundError(template_id)

    unknown = sorted(set(fields) - WRITABLE_FIELDS)
    if unknown:
        raise ValueError(f"unknown template fields: {', '.join(unknown)}")
    if not fields:
        return current

    name = _require_name(fields.get("name", current.name))
    body = fields.get("body", current.body)
    kind = fields.get("kind", current.kind)
    variations = tuple(fields.get("variations", current.variations))
    ai_spec = dict(fields.get("ai_spec", current.ai_spec))
    is_ai_generated = bool(fields.get("is_ai_generated", current.is_ai_generated))

    compile_bodies((body, *variations), kind=kind, policy=policy)

    conn.execute(
        """
        UPDATE templates
        SET name = ?, body = ?, variations_json = ?, kind = ?,
            ai_spec_json = ?, is_ai_generated = ?
        WHERE id = ?
        """,
        (
            name,
            body,
            json.dumps(list(variations)),
            kind,
            json.dumps(ai_spec, sort_keys=True),
            int(is_ai_generated),
            template_id,
        ),
    )
    conn.commit()

    updated = get_template(conn, template_id)
    if updated is None:  # pragma: no cover - the update just succeeded
        raise TemplateNotFoundError(template_id)
    return updated


def delete_template(conn: sqlite3.Connection, template_id: int) -> bool:
    cursor = conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))
    conn.commit()
    return cursor.rowcount > 0
