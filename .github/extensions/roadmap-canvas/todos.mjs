// Two-way bridge between the roadmap board and the session `todos` table.
//
// The session DB is plain SQLite at
//   $COPILOT_HOME/session-state/<sessionId>/session.db
// and the CLI holds it open, so every access here uses a busy timeout and is
// best-effort. A locked or missing DB degrades to a no-op rather than breaking
// the board.
//
// Status vocabularies differ. The todos table has a CHECK constraint allowing
// only pending / in_progress / done / blocked, so:
//   - "in-progress" (board) maps to "in_progress" (db)
//   - "deferred" has no db equivalent and maps to "blocked" on push. That is
//     lossy, and pull never produces "deferred" as a result.

import { DatabaseSync } from "node:sqlite";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";
import { TASK_TODOS } from "./roadmap-data.mjs";
import { allTasks } from "./store.mjs";

const DB_STATUSES = ["pending", "in_progress", "done", "blocked"];

// Progress ordering used to pick the least advanced status. "blocked" is not on
// this axis; it is handled separately and always wins.
const RANK = { pending: 0, "in-progress": 1, done: 2 };

export function sessionDbPath(sessionId) {
    if (!sessionId) return null;
    const home = process.env.COPILOT_HOME || path.join(homedir(), ".copilot");
    return path.join(home, "session-state", sessionId, "session.db");
}

function toDbStatus(boardStatus) {
    if (boardStatus === "in-progress") return "in_progress";
    if (boardStatus === "deferred") return "blocked";
    return boardStatus;
}

function toBoardStatus(dbStatus) {
    return dbStatus === "in_progress" ? "in-progress" : dbStatus;
}

function openDb(sessionId, { readOnly = false } = {}) {
    const file = sessionDbPath(sessionId);
    if (!file || !existsSync(file)) return null;
    try {
        const db = new DatabaseSync(file, { readOnly });
        db.exec("PRAGMA busy_timeout = 4000");
        return db;
    } catch {
        return null;
    }
}

/** Board task ID -> todo IDs, and the reverse index. */
function indexes(state) {
    const tasks = allTasks(state);
    const byTask = new Map();
    const byTodo = new Map();
    for (const t of tasks) {
        const todos = TASK_TODOS[t.id] ?? [];
        byTask.set(t.id, todos);
        for (const todoId of todos) {
            if (!byTodo.has(todoId)) byTodo.set(todoId, []);
            byTodo.get(todoId).push(t);
        }
    }
    return { tasks, byTask, byTodo };
}

/** Least advanced status across the tasks that claim a todo. Blocked wins. */
function rollDownToTodo(tasks) {
    if (tasks.some((t) => t.status === "blocked")) return "blocked";
    if (tasks.every((t) => t.status === "deferred")) return "blocked";
    const live = tasks.filter((t) => t.status !== "deferred");
    if (!live.length) return "blocked";
    let lowest = live[0].status;
    for (const t of live) if (RANK[t.status] < RANK[lowest]) lowest = t.status;
    return toDbStatus(lowest);
}

/**
 * Decide what a push should actually write.
 *
 * The board is coarser than the todo list, so a todo can legitimately be ahead
 * of the task that claims it. Pushing the board's status blindly would demote
 * that todo and destroy real progress. So a normal push is monotonic: it only
 * ever advances a todo. Blocked is the exception, because that is a deliberate
 * signal worth propagating downward.
 *
 * Returns null when nothing should change.
 */
function plannedPush(current, want, { force }) {
    if (want === current) return null;
    if (force) return want;
    if (want === "blocked") return want;
    if (current === "blocked") return want;
    return RANK[toBoardStatus(want)] > RANK[toBoardStatus(current)] ? want : null;
}

/** Roll a set of todo statuses up into a single board status. */
function rollUpToTask(dbStatuses) {
    if (!dbStatuses.length) return null;
    if (dbStatuses.some((s) => s === "blocked")) return "blocked";
    if (dbStatuses.every((s) => s === "done")) return "done";
    if (dbStatuses.some((s) => s === "done" || s === "in_progress")) return "in-progress";
    return "pending";
}

function readTodos(db) {
    const rows = db.prepare("SELECT id, title, status FROM todos").all();
    return new Map(rows.map((r) => [r.id, r]));
}

/**
 * Compare the board against the todos table without changing anything.
 * Returns the drift in both directions plus coverage warnings.
 */
export function diffTodos(sessionId, state) {
    const db = openDb(sessionId, { readOnly: true });
    if (!db) return { available: false, reason: "Session todos database not found or not readable." };
    try {
        const { byTask, byTodo } = indexes(state);
        const rows = readTodos(db);

        const pushChanges = [];
        const pushDemotions = [];
        for (const [todoId, tasks] of byTodo) {
            const row = rows.get(todoId);
            if (!row) continue;
            const want = rollDownToTodo(tasks);
            if (want === row.status) continue;
            const entry = {
                todoId,
                title: row.title,
                from: row.status,
                to: want,
                drivenBy: tasks.map((t) => `${t.id}:${t.status}`),
            };
            if (plannedPush(row.status, want, { force: false })) pushChanges.push(entry);
            else pushDemotions.push(entry);
        }

        const pullChanges = [];
        for (const [taskId, todoIds] of byTask) {
            if (!todoIds.length) continue;
            const statuses = todoIds.map((id) => rows.get(id)?.status).filter(Boolean);
            const want = rollUpToTask(statuses);
            const task = allTasks(state).find((t) => t.id === taskId);
            if (!task || task.status === "deferred" || !want || want === task.status) continue;
            pullChanges.push({
                taskId,
                title: task.title,
                from: task.status,
                to: want,
                drivenBy: todoIds.map((id) => `${id}:${rows.get(id)?.status ?? "missing"}`),
            });
        }

        const unmappedTodos = [...rows.keys()].filter((id) => !byTodo.has(id));
        const unmappedTasks = [...byTask.entries()].filter(([, v]) => !v.length).map(([k]) => k);
        const missingTodos = [...byTodo.keys()].filter((id) => !rows.has(id));

        return {
            available: true,
            inSync: pushChanges.length === 0 && pullChanges.length === 0,
            todoCount: rows.size,
            pushChanges,
            pushDemotions,
            pullChanges,
            unmappedTodos,
            unmappedTasks,
            missingTodos,
        };
    } finally {
        db.close();
    }
}

/**
 * Board wins. Advance every mapped todo to the status its tasks imply.
 *
 * Monotonic unless `force` is set: a todo that is further along than the board
 * keeps its status, because the board cannot express that detail. Pass force to
 * mirror the board exactly, demotions included.
 *
 * Best-effort: a locked database returns { available: false }.
 */
export function pushToTodos(sessionId, state, { force = false } = {}) {
    const db = openDb(sessionId);
    if (!db) return { available: false, applied: 0, changes: [], skipped: [] };
    try {
        const { byTodo } = indexes(state);
        const rows = readTodos(db);
        const stmt = db.prepare("UPDATE todos SET status = ?, updated_at = datetime('now') WHERE id = ?");
        const changes = [];
        const skipped = [];
        db.exec("BEGIN");
        try {
            for (const [todoId, tasks] of byTodo) {
                const row = rows.get(todoId);
                if (!row) continue;
                const want = rollDownToTodo(tasks);
                if (!DB_STATUSES.includes(want)) continue;
                const next = plannedPush(row.status, want, { force });
                if (!next) {
                    if (want !== row.status) skipped.push({ todoId, kept: row.status, boardWanted: want });
                    continue;
                }
                stmt.run(next, todoId);
                changes.push({ todoId, from: row.status, to: next });
            }
            db.exec("COMMIT");
        } catch (err) {
            db.exec("ROLLBACK");
            throw err;
        }
        return { available: true, applied: changes.length, changes, skipped };
    } finally {
        db.close();
    }
}

/**
 * Todos win. Roll finer-grained todo progress up into board statuses.
 * Returns the board updates to apply; the caller persists them through the
 * store so docs/roadmap.json stays the single write path.
 */
export function pullFromTodos(sessionId, state) {
    const db = openDb(sessionId, { readOnly: true });
    if (!db) return { available: false, updates: [] };
    try {
        const { byTask } = indexes(state);
        const rows = readTodos(db);
        const tasks = allTasks(state);
        const updates = [];
        for (const [taskId, todoIds] of byTask) {
            if (!todoIds.length) continue;
            const task = tasks.find((t) => t.id === taskId);
            if (!task || task.status === "deferred") continue;
            const statuses = todoIds.map((id) => rows.get(id)?.status).filter(Boolean);
            const want = rollUpToTask(statuses);
            if (want && want !== task.status) updates.push({ taskId, from: task.status, to: want });
        }
        return { available: true, updates };
    } finally {
        db.close();
    }
}

/** Verify the mapping covers every todo exactly as intended. Used by tests. */
export function auditMapping(sessionId, state) {
    return diffTodos(sessionId, state);
}

export { toBoardStatus, toDbStatus };
