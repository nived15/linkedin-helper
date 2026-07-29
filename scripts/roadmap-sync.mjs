#!/usr/bin/env node
// Flips roadmap tasks to "done" and stamps PR metadata when a merged PR
// closes their tracking GitHub issue. Run by
// .github/workflows/roadmap-sync.yml on every `pull_request` closed event.
// Exits quietly (no error, no file change) if the PR was closed without
// merging or its body has no recognizable "Closes #N" style keyword.
//
// Reuses the roadmap canvas's own store.mjs so this script and the live
// canvas extension can never disagree about the on-disk docs/roadmap.json
// schema. store.mjs has zero non-core dependencies, so this script needs no
// `npm install` step in CI.

import { getState, allTasks, linkTask, setTaskStatus } from "../.github/extensions/roadmap-canvas/store.mjs";

// Matches "Closes #12", "closes: #12", "Fixes #12", "Resolves #12" etc.
// case-insensitively, singular or past tense, with or without a colon.
const CLOSING_KEYWORD_RE = /\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*#(\d+)/gi;

function parseClosedIssueNumbers(prBody) {
    const numbers = new Set();
    for (const match of String(prBody ?? "").matchAll(CLOSING_KEYWORD_RE)) {
        numbers.add(Number(match[2]));
    }
    return [...numbers];
}

async function main() {
    const prNumber = Number(process.env.PR_NUMBER);
    const prBody = process.env.PR_BODY ?? "";
    const merged = process.env.PR_MERGED === "true";

    if (!merged) {
        console.log("PR was closed without merging; nothing to sync.");
        return;
    }
    if (!Number.isFinite(prNumber)) {
        throw new Error("PR_NUMBER env var is required and must be numeric.");
    }

    const issueNumbers = parseClosedIssueNumbers(prBody);
    if (!issueNumbers.length) {
        console.log('No "Closes #N" style keyword found in the PR body; nothing to sync.');
        return;
    }

    const state = await getState();
    const tasks = allTasks(state);
    let updated = 0;

    for (const issueNumber of issueNumbers) {
        const task = tasks.find((t) => t.issueNumber === issueNumber);
        if (!task) {
            console.log(`No roadmap task references issue #${issueNumber}; skipping.`);
            continue;
        }
        await linkTask(task.id, { prNumber, prMerged: true });
        await setTaskStatus(task.id, "done", task.notes);
        console.log(`Marked ${task.id} done (issue #${issueNumber} closed by PR #${prNumber}).`);
        updated += 1;
    }

    if (!updated) {
        console.log("No roadmap tasks matched any closed issue; nothing to commit.");
    }
}

main().catch((err) => {
    console.error(err);
    process.exit(1);
});
