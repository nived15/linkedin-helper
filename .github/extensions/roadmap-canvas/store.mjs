// Durable storage for roadmap status.
//
// The seed model in roadmap-data.mjs owns all prose (titles, descriptions,
// dependencies, phases). This module owns only MUTABLE state: status, notes and
// validation results. They are merged on read, so editing the seed never
// destroys progress and progress never freezes stale prose.
//
// State file: <repo>/docs/roadmap.json  (committed, so progress is versioned).

import { readFile, writeFile, mkdir } from "node:fs/promises";
import path from "node:path";
import { seedState, STATUSES } from "./roadmap-data.mjs";

// extension.mjs lives at <repo>/.github/extensions/roadmap-canvas/
const REPO_ROOT = path.resolve(import.meta.dirname, "..", "..", "..");
const STATE_DIR = path.join(REPO_ROOT, "docs");
const STATE_FILE = path.join(STATE_DIR, "roadmap.json");

export const stateFilePath = STATE_FILE;

/** Serialised writes so concurrent iframe POSTs cannot interleave. */
let writeChain = Promise.resolve();

function emptyOverlay() {
    return { version: 1, updatedAt: null, tasks: {}, constraints: {}, validations: {} };
}

async function readOverlay() {
    try {
        const raw = await readFile(STATE_FILE, "utf8");
        const parsed = JSON.parse(raw);
        return {
            version: parsed.version ?? 1,
            updatedAt: parsed.updatedAt ?? null,
            tasks: parsed.tasks ?? {},
            constraints: parsed.constraints ?? {},
            validations: parsed.validations ?? {},
        };
    } catch {
        return emptyOverlay();
    }
}

async function writeOverlay(overlay) {
    overlay.updatedAt = new Date().toISOString();
    await mkdir(STATE_DIR, { recursive: true });
    await writeFile(STATE_FILE, `${JSON.stringify(overlay, null, 2)}\n`, "utf8");
    return overlay;
}

/** Run a read-modify-write against the overlay without racing other writers. */
function mutate(fn) {
    const next = writeChain.then(async () => {
        const overlay = await readOverlay();
        const result = await fn(overlay);
        await writeOverlay(overlay);
        return result;
    });
    // Keep the chain alive even if one mutation rejects.
    writeChain = next.then(
        () => undefined,
        () => undefined,
    );
    return next;
}

/** Seed model with the persisted overlay applied. This is what the UI renders. */
export async function getState() {
    const overlay = await readOverlay();
    const state = seedState();
    state.updatedAt = overlay.updatedAt;

    for (const c of state.constraints) {
        const o = overlay.constraints[c.id];
        if (o?.status) c.status = o.status;
    }
    for (const m of state.modules) {
        for (const t of m.tasks) {
            const o = overlay.tasks[t.id];
            if (o?.status) t.status = o.status;
            if (typeof o?.notes === "string") t.notes = o.notes;
            if (o?.issueNumber != null) t.issueNumber = o.issueNumber;
            if (o?.prNumber != null) t.prNumber = o.prNumber;
            if (typeof o?.prMerged === "boolean") t.prMerged = o.prMerged;
        }
    }
    for (const v of state.validations) {
        const o = overlay.validations[v.id];
        if (o?.status) v.status = o.status;
        if (typeof o?.result === "string") v.result = o.result;
    }
    return state;
}

export function allTasks(state) {
    return state.modules.flatMap((m) => m.tasks.map((t) => ({ ...t, moduleId: m.id, moduleName: m.name })));
}

export function findTask(state, taskId) {
    return allTasks(state).find((t) => t.id.toLowerCase() === String(taskId).toLowerCase()) ?? null;
}

function assertStatus(status) {
    if (!STATUSES.includes(status)) {
        throw new Error(`Invalid status "${status}". Expected one of: ${STATUSES.join(", ")}`);
    }
}

/**
 * Tasks whose dependencies are not all done. Used to warn (not block) when
 * something is started out of order.
 */
export function unmetDeps(state, task) {
    const byId = new Map(allTasks(state).map((t) => [t.id, t]));
    return (task.deps ?? []).filter((d) => {
        const dep = byId.get(d);
        return dep && dep.status !== "done" && dep.status !== "deferred";
    });
}

export async function setTaskStatus(taskId, status, notes) {
    assertStatus(status);
    const before = await getState();
    const task = findTask(before, taskId);
    if (!task) throw new Error(`Unknown task id "${taskId}"`);

    await mutate((overlay) => {
        const entry = overlay.tasks[task.id] ?? {};
        entry.status = status;
        if (typeof notes === "string") entry.notes = notes;
        overlay.tasks[task.id] = entry;
    });

    const after = await getState();
    const updated = findTask(after, task.id);
    return { task: updated, warnings: status === "in-progress" || status === "done" ? unmetDeps(after, updated) : [] };
}

/**
 * Link a task to its tracking issue and/or PR. Every roadmap task should have
 * exactly one GitHub issue (opened when work starts) and one PR (opened when
 * work is ready for review). This is how the two stay associated with the
 * task ID so the roadmap-sync workflow can find its way back from a merged
 * PR to a task to flip to "done".
 *
 * Any of the three fields may be omitted; only the fields present are
 * updated, so `linkTaskIssue` and `linkTaskPr` can be called independently as
 * each artifact is created.
 */
export async function linkTask(taskId, { issueNumber, prNumber, prMerged } = {}) {
    const state = await getState();
    const task = findTask(state, taskId);
    if (!task) throw new Error(`Unknown task id "${taskId}"`);

    await mutate((overlay) => {
        const entry = overlay.tasks[task.id] ?? {};
        if (issueNumber != null) entry.issueNumber = Number(issueNumber);
        if (prNumber != null) entry.prNumber = Number(prNumber);
        if (typeof prMerged === "boolean") entry.prMerged = prMerged;
        overlay.tasks[task.id] = entry;
    });

    const after = await getState();
    return findTask(after, task.id);
}

export async function setConstraintStatus(id, status) {
    assertStatus(status);
    const state = await getState();
    if (!state.constraints.some((c) => c.id.toLowerCase() === String(id).toLowerCase())) {
        throw new Error(`Unknown constraint id "${id}"`);
    }
    const canonical = state.constraints.find((c) => c.id.toLowerCase() === String(id).toLowerCase()).id;
    await mutate((overlay) => {
        overlay.constraints[canonical] = { status };
    });
    return { id: canonical, status };
}

export async function setValidationStatus(id, status, result) {
    assertStatus(status);
    const state = await getState();
    const match = state.validations.find((v) => v.id.toLowerCase() === String(id).toLowerCase());
    if (!match) throw new Error(`Unknown validation id "${id}"`);
    await mutate((overlay) => {
        const entry = overlay.validations[match.id] ?? {};
        entry.status = status;
        if (typeof result === "string") entry.result = result;
        overlay.validations[match.id] = entry;
    });
    return { id: match.id, status };
}

/** Aggregate counts for the overview header and the agent-facing summary. */
export function summarise(state) {
    const tasks = allTasks(state);
    const active = tasks.filter((t) => t.status !== "deferred");
    const count = (list, s) => list.filter((t) => t.status === s).length;

    const byPhase = state.phases.map((p) => {
        const list = tasks.filter((t) => p.taskIds.includes(t.id));
        const live = list.filter((t) => t.status !== "deferred");
        return {
            id: p.id,
            name: p.name,
            total: live.length,
            done: count(live, "done"),
            inProgress: count(live, "in-progress"),
            blocked: count(live, "blocked"),
            deferred: list.length - live.length,
        };
    });

    const byModule = state.modules.map((m) => {
        const live = m.tasks.filter((t) => t.status !== "deferred");
        return {
            id: m.id,
            name: m.name,
            total: live.length,
            done: count(live, "done"),
            inProgress: count(live, "in-progress"),
            blocked: count(live, "blocked"),
        };
    });

    // Ready = pending, not deferred, all deps done or deferred.
    const ready = tasks
        .filter((t) => t.status === "pending" && unmetDeps(state, t).length === 0)
        .map((t) => ({ id: t.id, title: t.title, phase: t.phase }));

    return {
        totalTasks: active.length,
        done: count(active, "done"),
        inProgress: count(active, "in-progress"),
        blocked: count(active, "blocked"),
        pending: count(active, "pending"),
        deferred: tasks.length - active.length,
        percentComplete: active.length ? Math.round((count(active, "done") / active.length) * 100) : 0,
        validationsPassed: state.validations.filter((v) => v.status === "done").length,
        validationsTotal: state.validations.length,
        byPhase,
        byModule,
        ready,
    };
}
