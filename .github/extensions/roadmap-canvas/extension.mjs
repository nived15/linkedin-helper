// Roadmap canvas: interactive execution tracker for the Linked Helper parity build.
//
// Renders the approved master plan as a live board across four sections:
//   1. System requirements and safety constraints
//   2. Architectural modules and workflow matrix (5 modules, 24 tasks)
//   3. Phased roadmap and dependency graph (4 phases)
//   4. System validation and suite criteria
//
// Durable state lives in <repo>/docs/roadmap.json, keyed by task/constraint/
// validation ID rather than instanceId, so the board survives reloads, restarts
// and fresh panels. Never console.log here: stdout is JSON-RPC.

import { createServer } from "node:http";
import { createCanvas, CanvasError, joinSession } from "@github/copilot-sdk/extension";

import { renderHtml } from "./renderer.mjs";
import {
    getState,
    summarise,
    findTask,
    unmetDeps,
    setTaskStatus,
    setConstraintStatus,
    setValidationStatus,
    stateFilePath,
} from "./store.mjs";

/** instanceId -> { server, url } */
const servers = new Map();
/** Every open SSE response across every instance. */
const sseClients = new Set();

let session;

function log(message, level = "info") {
    try {
        session?.log?.(message, { level, ephemeral: true });
    } catch {
        /* logging must never break a request */
    }
}

function broadcast() {
    for (const res of sseClients) {
        try {
            res.write("event: changed\ndata: {}\n\n");
        } catch {
            sseClients.delete(res);
        }
    }
}

function sendJson(res, status, body) {
    const payload = JSON.stringify(body);
    res.writeHead(status, {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
        "Content-Length": Buffer.byteLength(payload),
    });
    res.end(payload);
}

async function readBody(req) {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    if (!chunks.length) return {};
    try {
        return JSON.parse(Buffer.concat(chunks).toString("utf8"));
    } catch {
        throw new Error("Request body was not valid JSON");
    }
}

async function handle(req, res) {
    const url = new URL(req.url, "http://127.0.0.1");
    const route = url.pathname;

    if (route === "/" || route === "/index.html") {
        const html = renderHtml();
        res.writeHead(200, {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
            "Content-Length": Buffer.byteLength(html),
        });
        res.end(html);
        return;
    }

    if (route === "/events") {
        res.writeHead(200, {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            Connection: "keep-alive",
        });
        res.write("retry: 3000\n\n");
        sseClients.add(res);
        const ping = setInterval(() => {
            try {
                res.write(": ping\n\n");
            } catch {
                /* cleaned up on close */
            }
        }, 25000);
        req.on("close", () => {
            clearInterval(ping);
            sseClients.delete(res);
        });
        return;
    }

    if (route === "/api/state" && req.method === "GET") {
        const state = await getState();
        sendJson(res, 200, { state, summary: summarise(state) });
        return;
    }

    if (req.method === "POST") {
        const body = await readBody(req);
        try {
            if (route === "/api/task") {
                if (typeof body.status === "string") await setTaskStatus(body.id, body.status, body.notes);
                else if (typeof body.notes === "string") {
                    const current = findTask(await getState(), body.id);
                    if (!current) throw new Error(`Unknown task id "${body.id}"`);
                    await setTaskStatus(body.id, current.status, body.notes);
                }
            } else if (route === "/api/constraint") {
                await setConstraintStatus(body.id, body.status);
            } else if (route === "/api/validation") {
                const state = await getState();
                const match = state.validations.find((v) => v.id === body.id);
                if (!match) throw new Error(`Unknown validation id "${body.id}"`);
                await setValidationStatus(body.id, body.status ?? match.status, body.result);
            } else {
                sendJson(res, 404, { error: "not_found" });
                return;
            }
        } catch (err) {
            sendJson(res, 400, { error: String(err?.message ?? err) });
            return;
        }
        broadcast();
        const state = await getState();
        sendJson(res, 200, { state, summary: summarise(state) });
        return;
    }

    sendJson(res, 404, { error: "not_found" });
}

async function startServer() {
    const server = createServer((req, res) => {
        handle(req, res).catch((err) => {
            log(`roadmap canvas request failed: ${err?.message ?? err}`, "error");
            if (!res.headersSent) sendJson(res, 500, { error: String(err?.message ?? err) });
            else res.end();
        });
    });
    await new Promise((resolve, reject) => {
        server.once("error", reject);
        server.listen(0, "127.0.0.1", resolve);
    });
    const { port } = server.address();
    return { server, url: `http://127.0.0.1:${port}/` };
}

/** Compact, token-cheap projection of the board for the agent. */
function agentView(state) {
    const s = summarise(state);
    return {
        title: state.title,
        stateFile: "docs/roadmap.json",
        progress: `${s.done}/${s.totalTasks} done (${s.percentComplete}%)`,
        counts: {
            done: s.done,
            inProgress: s.inProgress,
            blocked: s.blocked,
            pending: s.pending,
            deferred: s.deferred,
        },
        validations: `${s.validationsPassed}/${s.validationsTotal} passing`,
        constraints: state.constraints.map((c) => ({ id: c.id, title: c.title, status: c.status })),
        phases: s.byPhase,
        modules: s.byModule,
        readyToStart: s.ready,
    };
}

const canvas = createCanvas({
    id: "roadmap",
    displayName: "Linked Helper parity roadmap",
    description:
        "Interactive execution board for rebuilding this repo as an MCP-native Linked Helper equivalent: safety constraints, 5 module task matrix, 4 phases with dependencies, and the validation suite.",
    inputSchema: {
        type: "object",
        properties: {
            tab: {
                type: "string",
                enum: ["overview", "modules", "roadmap", "validation"],
                description: "Which section to focus when the board opens.",
            },
        },
        additionalProperties: false,
    },
    actions: [
        {
            name: "get_state",
            description: "Read the current board: progress counts, per-phase and per-module rollups, and unblocked tasks.",
            handler: async () => agentView(await getState()),
        },
        {
            name: "get_task",
            description: "Read one work item in full, including description, dependencies, notes and plan references.",
            inputSchema: {
                type: "object",
                properties: { id: { type: "string", description: "Task ID, for example CORE-03." } },
                required: ["id"],
                additionalProperties: false,
            },
            handler: async (ctx) => {
                const state = await getState();
                const task = findTask(state, ctx.input.id);
                if (!task) throw new CanvasError("task_not_found", `No task with id "${ctx.input.id}".`);
                return { ...task, waitingOn: unmetDeps(state, task) };
            },
        },
        {
            name: "set_task_status",
            description: "Update a work item's status, and optionally attach a note. Persists to docs/roadmap.json.",
            inputSchema: {
                type: "object",
                properties: {
                    id: { type: "string", description: "Task ID, for example DB-01." },
                    status: {
                        type: "string",
                        enum: ["pending", "in-progress", "done", "blocked", "deferred"],
                        description: "New status.",
                    },
                    notes: { type: "string", description: "Optional note stored against the task." },
                },
                required: ["id", "status"],
                additionalProperties: false,
            },
            handler: async (ctx) => {
                let result;
                try {
                    result = await setTaskStatus(ctx.input.id, ctx.input.status, ctx.input.notes);
                } catch (err) {
                    throw new CanvasError("task_update_failed", String(err?.message ?? err));
                }
                broadcast();
                return {
                    id: result.task.id,
                    status: result.task.status,
                    warning: result.warnings.length
                        ? `Dependencies not yet done: ${result.warnings.join(", ")}`
                        : undefined,
                };
            },
        },
        {
            name: "set_constraint_status",
            description: "Update a Section 1 requirement or safety constraint, for example REQ-02.",
            inputSchema: {
                type: "object",
                properties: {
                    id: { type: "string" },
                    status: {
                        type: "string",
                        enum: ["pending", "in-progress", "done", "blocked", "deferred"],
                    },
                },
                required: ["id", "status"],
                additionalProperties: false,
            },
            handler: async (ctx) => {
                let result;
                try {
                    result = await setConstraintStatus(ctx.input.id, ctx.input.status);
                } catch (err) {
                    throw new CanvasError("constraint_update_failed", String(err?.message ?? err));
                }
                broadcast();
                return result;
            },
        },
        {
            name: "set_validation_result",
            description: "Record the outcome of a Section 4 validation check, with the observed result text.",
            inputSchema: {
                type: "object",
                properties: {
                    id: { type: "string", description: "Validation ID, for example VAL-01." },
                    status: {
                        type: "string",
                        enum: ["pending", "in-progress", "done", "blocked", "deferred"],
                        description: "Use done for a pass, blocked for a fail.",
                    },
                    result: { type: "string", description: "What was actually observed." },
                },
                required: ["id", "status"],
                additionalProperties: false,
            },
            handler: async (ctx) => {
                let result;
                try {
                    result = await setValidationStatus(ctx.input.id, ctx.input.status, ctx.input.result);
                } catch (err) {
                    throw new CanvasError("validation_update_failed", String(err?.message ?? err));
                }
                broadcast();
                return result;
            },
        },
        {
            name: "summarize_progress",
            description: "Short prose status report covering phases, blockers, and what to pick up next.",
            handler: async () => {
                const state = await getState();
                const s = summarise(state);
                const lines = [
                    `${state.title}: ${s.done}/${s.totalTasks} work items done (${s.percentComplete}%).`,
                    `In progress ${s.inProgress}, blocked ${s.blocked}, pending ${s.pending}, deferred ${s.deferred}.`,
                    `Validation suite: ${s.validationsPassed}/${s.validationsTotal} passing.`,
                    "",
                    "Phases:",
                    ...s.byPhase.map((p) => `  ${p.id} ${p.name}: ${p.done}/${p.total} done, ${p.blocked} blocked.`),
                    "",
                ];
                const blocked = state.modules
                    .flatMap((m) => m.tasks)
                    .filter((t) => t.status === "blocked");
                if (blocked.length) {
                    lines.push("Blocked:");
                    blocked.forEach((t) => lines.push(`  ${t.id} ${t.title}${t.notes ? ` - ${t.notes}` : ""}`));
                    lines.push("");
                }
                lines.push(
                    s.ready.length
                        ? `Ready to start: ${s.ready.map((r) => `${r.id} (${r.title})`).join(", ")}.`
                        : "Nothing is unblocked. Every pending item is waiting on a dependency.",
                );
                return { report: lines.join("\n"), summary: s };
            },
        },
    ],
    open: async (ctx) => {
        let entry = servers.get(ctx.instanceId);
        if (!entry) {
            entry = await startServer();
            servers.set(ctx.instanceId, entry);
        }
        const state = await getState();
        const s = summarise(state);
        return {
            url: entry.url,
            title: state.title,
            status: `${s.done}/${s.totalTasks} done · ${s.percentComplete}% · ${s.ready.length} ready`,
        };
    },
    onClose: async (ctx) => {
        const entry = servers.get(ctx.instanceId);
        if (!entry) return;
        servers.delete(ctx.instanceId);
        await new Promise((resolve) => entry.server.close(() => resolve()));
    },
});

session = await joinSession({ canvases: [canvas] });
log(`roadmap canvas ready, state file ${stateFilePath}`);
