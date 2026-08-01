#!/usr/bin/env node
// Flips roadmap tasks to "done" and stamps PR metadata when a merged PR closes
// their tracking GitHub issue. Run by .github/workflows/roadmap-sync.yml on
// every push to the default branch.
//
// Why push and not `pull_request: closed`: PRs opened by the Copilot coding
// agent are authored by a bot, so GitHub holds their workflow runs at
// "action_required" until a maintainer clicks approve. Runs #30, #31 and #32
// all stalled that way and the board silently fell three tasks behind. A push
// to the default branch is attributed to the human who clicked merge, so it
// runs unattended.
//
// The trade-off is that a push payload carries no PR body, and the
// "Fixes #N" keyword we key off lives in the body. So this script resolves the
// merged PR itself: it reads PR numbers out of the pushed commit subjects
// (merge and squash commits both carry them), falls back to the commits API
// for rebase merges, then fetches each PR to read its body.
//
// Exits quietly (no error, no file change) when a push carries no merged PR,
// which is the normal case for a direct commit to the default branch.
//
// Reuses the roadmap canvas's own store.mjs so this script and the live canvas
// extension can never disagree about the on-disk docs/roadmap.json schema.
// store.mjs has zero non-core dependencies, so this script needs no
// `npm install` step in CI.

import { execFileSync } from "node:child_process";
import { getState, allTasks, linkTask, setTaskStatus } from "../.github/extensions/roadmap-canvas/store.mjs";

// Matches "Closes #12", "closes: #12", "Fixes #12", "Resolves #12" etc.
// case-insensitively, singular or past tense, with or without a colon.
const CLOSING_KEYWORD_RE = /\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*#(\d+)/gi;

// "Merge pull request #32 from owner/branch" - the default merge commit subject.
const MERGE_COMMIT_RE = /^Merge pull request #(\d+)\b/;

// "Some PR title (#32)" - the default squash and rebase commit subject.
const SQUASH_COMMIT_RE = /\(#(\d+)\)\s*$/;

const ZERO_SHA = "0".repeat(40);
const API_ROOT = process.env.GITHUB_API_URL || "https://api.github.com";

// A single push only ever needs its newest commits. rev-list returns newest
// first, so this keeps every merge commit in a normal push while refusing to
// walk an entire branch history if `before` points somewhere unexpected.
const MAX_COMMITS = 50;

function parseClosedIssueNumbers(prBody) {
    const numbers = new Set();
    for (const match of String(prBody ?? "").matchAll(CLOSING_KEYWORD_RE)) {
        numbers.add(Number(match[2]));
    }
    return [...numbers];
}

function git(args) {
    return execFileSync("git", args, { encoding: "utf8" });
}

function commitSubjects(beforeSha, afterSha) {
    if (!afterSha) return [];

    // `before` is the zero SHA on a branch's first push, and points at a
    // discarded commit after a force push. Either way the range is unusable,
    // so fall back to the pushed head commit on its own.
    let range = afterSha;
    const usableBefore = beforeSha && beforeSha !== ZERO_SHA;
    if (usableBefore) {
        try {
            git(["cat-file", "-e", `${beforeSha}^{commit}`]);
            range = `${beforeSha}..${afterSha}`;
        } catch {
            console.log(`Push base ${beforeSha} is not in this checkout; inspecting ${afterSha} alone.`);
        }
    }

    try {
        const out = git(["log", "--format=%s", `--max-count=${MAX_COMMITS}`, range]);
        return out.split("\n").map((line) => line.trim()).filter(Boolean);
    } catch (err) {
        console.log(`Could not read commit range ${range}: ${err.message}`);
        return [];
    }
}

function parsePullNumbersFromSubjects(subjects) {
    const numbers = new Set();
    for (const subject of subjects) {
        const merge = subject.match(MERGE_COMMIT_RE);
        if (merge) {
            numbers.add(Number(merge[1]));
            continue;
        }
        const squash = subject.match(SQUASH_COMMIT_RE);
        if (squash) numbers.add(Number(squash[1]));
    }
    return [...numbers];
}

// `softStatuses` are responses that mean "no such thing", which some callers
// treat as an answer rather than a failure. Everything else throws, because a
// silently swallowed 403 or 5xx would leave the board quietly stale, which is
// the exact failure this workflow exists to prevent.
async function api(pathname, token, { softStatuses = [] } = {}) {
    const res = await fetch(`${API_ROOT}${pathname}`, {
        headers: {
            Accept: "application/vnd.github+json",
            Authorization: `Bearer ${token}`,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "roadmap-sync",
        },
    });
    if (softStatuses.includes(res.status)) return null;
    if (!res.ok) {
        throw new Error(`GitHub API ${pathname} failed: ${res.status} ${res.statusText}`);
    }
    return res.json();
}

// Fallback for rebase merges, whose commit subjects carry no PR number.
// A commit GitHub does not recognise answers 404, or 422 when the SHA is well
// formed but unknown to the repo. Both mean "no PR here", not "broken".
async function pullNumbersForCommit(repo, sha, token) {
    const pulls = await api(`/repos/${repo}/commits/${sha}/pulls`, token, {
        softStatuses: [404, 422],
    });
    if (pulls === null) {
        console.log(`GitHub does not know commit ${sha}; treating it as carrying no PR.`);
        return [];
    }
    return pulls.map((pull) => pull.number);
}

async function main() {
    const repo = process.env.GITHUB_REPOSITORY;
    const token = process.env.GITHUB_TOKEN;
    const beforeSha = (process.env.PUSH_BEFORE || "").trim();
    const afterSha = (process.env.PUSH_AFTER || "").trim();
    const explicitPr = Number((process.env.PR_NUMBER || "").trim());

    if (!repo) throw new Error("GITHUB_REPOSITORY env var is required.");
    if (!token) throw new Error("GITHUB_TOKEN env var is required.");

    let pullNumbers = [];
    if (Number.isFinite(explicitPr) && explicitPr > 0) {
        // Manual workflow_dispatch repair for a PR the automation missed.
        pullNumbers = [explicitPr];
        console.log(`Syncing PR #${explicitPr} on request.`);
    } else {
        pullNumbers = parsePullNumbersFromSubjects(commitSubjects(beforeSha, afterSha));
        if (!pullNumbers.length && afterSha) {
            console.log("No PR number in the pushed commit subjects; asking the commits API.");
            pullNumbers = await pullNumbersForCommit(repo, afterSha, token);
        }
    }

    if (!pullNumbers.length) {
        console.log("This push carries no pull request; nothing to sync.");
        return;
    }

    const state = await getState();
    const tasks = allTasks(state);
    let updated = 0;

    for (const prNumber of pullNumbers) {
        const pull = await api(`/repos/${repo}/pulls/${prNumber}`, token);
        if (!pull.merged_at) {
            console.log(`PR #${prNumber} is not merged; skipping.`);
            continue;
        }

        const issueNumbers = parseClosedIssueNumbers(pull.body);
        if (!issueNumbers.length) {
            console.log(`PR #${prNumber} has no "Closes #N" style keyword in its body; skipping.`);
            continue;
        }

        for (const issueNumber of issueNumbers) {
            const task = tasks.find((t) => t.issueNumber === issueNumber);
            if (!task) {
                console.log(`No roadmap task references issue #${issueNumber}; skipping.`);
                continue;
            }
            if (task.status === "done" && task.prNumber === prNumber) {
                console.log(`${task.id} is already done and linked to PR #${prNumber}; leaving it alone.`);
                continue;
            }
            await linkTask(task.id, { prNumber, prMerged: true });
            await setTaskStatus(task.id, "done", task.notes);
            console.log(`Marked ${task.id} done (issue #${issueNumber} closed by PR #${prNumber}).`);
            updated += 1;
        }
    }

    if (!updated) {
        console.log("No roadmap tasks matched any closed issue; nothing to commit.");
    }
}

// Setting exitCode rather than calling process.exit() lets Node drain the
// still-open keep-alive socket from fetch. Exiting hard here aborts the
// process on Windows instead of returning a clean failure code.
main().catch((err) => {
    console.error(err);
    process.exitCode = 1;
});
