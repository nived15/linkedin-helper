"""MCP-05 (#28): the six guided workflows, as MCP prompts.

Why these exist
---------------
The same five workflows already shipped as Markdown under `.github/`: five
agents, five skills and five slash commands. Every one of them is invisible to
any client that is not Copilot in VS Code. A prompt is the protocol's own way of
saying "here is a guided flow", so moving them here means Claude Desktop, a
custom client or a script gets the same walkthrough Copilot gets.

Nothing under `.github/` is deleted by this change. Both surfaces are live, so
there is no window where Nived cannot post or engage while clients move across.

What a prompt is allowed to do
------------------------------
Return text. That is the whole contract, and two repository guards hold it up,
both extended by this issue from the shape #27 gave them for resources:

- `tests/test_actions.py` walks every `@mcp.prompt` in the tree exactly as it
  walks every `@mcp.tool` and `@mcp.resource`, and fails the build if one can
  reach Playwright, directly or through a helper.
- `tests/test_audit_log.py` records that prompts are deliberately outside the
  `@audit_linkedin_action` requirement, and pins that none of them takes a
  LinkedIn action.

Why prompts carry no `@audit_linkedin_action`
---------------------------------------------
This is #27's argument for resources, and it applies here with less doubt rather
than more. `actions_log` is not a diary of everything that happened. It is the
ledger `linkedin_mcp.safety.limits` counts to decide whether the account has
budget left, and `linkedin://safety/today` reports that arithmetic. A row in it
is a claim that some of today's LinkedIn budget was spent.

Rendering a prompt spends none. It opens no browser, contacts nobody, reads no
database and changes nothing: these functions are pure string builders. Auditing
them would mean a client that listed the prompt menu on connect had already
eaten into the day's invitation allowance before it did anything, and the number
`safety_check` reports would be wrong because `safety_check` was rendered.

The rule for anyone extending this file: if a prompt ever needs to act, it is a
tool, and a tool carries the decorator.

Voice
-----
Every prompt embeds :func:`linkedin_mcp.prompts.voice.voice_rules`, which is
generated from `linkedin_mcp/templating/style.py` rather than retyped, so the
rules a prompt states are the rules the template store enforces. The prompt text
itself is asserted in `tests/test_prompts.py` to pass `style_violations`.
"""

from __future__ import annotations

from fastmcp import FastMCP

from linkedin_mcp.core.config import (
    GLOBAL_DAILY_CEILING,
    GLOBAL_HOURLY_CEILING,
    INVITE_ACTION,
    ceiling_for,
)
from linkedin_mcp.executors.contract import ADHOC_ACTIONS
from linkedin_mcp.prompts.contract import (
    FUNNEL_URI_HINT,
    HARVEST_AUDIENCE,
    NEW_CAMPAIGN,
    REVIEW_DRAFTS,
    SAFETY_CHECK,
    TRIAGE_REPLIES,
    WEEKLY_REPORT,
    read_first,
)
from linkedin_mcp.prompts.voice import voice_rules
from linkedin_mcp.sequences.steps import LOCAL_ACTIONS

__all__ = ["register_linkedin_prompts"]


def _block(*parts: str) -> str:
    """Join sections with one blank line and drop the empty ones."""
    return "\n\n".join(part.strip() for part in parts if part and part.strip())


def _given(label: str, value: str, fallback: str) -> str:
    """Render one caller-supplied argument, or say what to do without it."""
    text = (value or "").strip()
    return f"{label}: {text}" if text else f"{label}: not given. {fallback}"


def _invite_ceilings() -> str:
    """State the real caps, from `core.config`, rather than round numbers.

    "Numbers over generalities" is one of the voice rules, so a prompt that said
    "stay under the limits" would be breaking the rule it is teaching. These are
    read from the hard ceilings the safety gate actually clamps against, so a
    ceiling that changes changes the prompt.
    """
    invite = ceiling_for(INVITE_ACTION)
    return (
        f"The hard ceilings are {invite.daily} invitations a day and "
        f"{invite.weekly} a week, plus {GLOBAL_DAILY_CEILING} metered actions a "
        f"day and {GLOBAL_HOURLY_CEILING} an hour across everything. "
        "A configured cap can lower any of these. Nothing raises them, so a "
        "number above a ceiling is silently clamped rather than honoured."
    )


def _executable_actions() -> str:
    """List the actions a worker can actually run, read from the registry.

    Generated rather than typed, because a prompt that named an action with no
    executor would send a client down a path that fails at the queue. That is
    exactly what happened with `message`: the queue accepts the idea, the
    ceiling exists, and there is no executor, so a step naming it can never run.
    Reading `ADHOC_ACTIONS` means an action added later appears here the day its
    executor does.
    """
    runnable = ", ".join(sorted(ADHOC_ACTIONS))
    local = ", ".join(sorted(LOCAL_ACTIONS))
    return (
        f"{runnable}. Local steps that reach nothing on LinkedIn are {local}, "
        "and they cost no budget."
    )


def register_linkedin_prompts(mcp: FastMCP) -> None:
    """Register the six MCP-05 prompts on `mcp`.

    Called from `linkedin_browser_mcp.py`, and reachability from that file is
    asserted by `tests/test_tool_registration.py` for the same reason PR #51
    exists: fifteen tools were once merged, tested and invisible because nobody
    called their factory.
    """

    @mcp.prompt(
        name=NEW_CAMPAIGN,
        description=(
            "Build an outreach campaign end to end: audience, sequence, "
            "templates, limits, then the human approval gate."
        ),
    )
    def new_campaign(
        audience: str,
        goal: str = "",
        daily_invite_target: str = "",
    ) -> str:
        """Walk a campaign from nothing to approved. Ends at `campaign_approve`."""
        return _block(
            "Build one outreach campaign for Nived, from an empty database to a "
            "campaign waiting at the approval gate. Work through the four "
            "stages in order. Do not skip ahead: every stage after the first "
            "needs the campaign id the first one returns.",
            read_first(NEW_CAMPAIGN),
            _given("Audience", audience, "Ask before continuing."),
            _given(
                "Goal",
                goal,
                "Ask what a reply is supposed to lead to, then continue.",
            ),
            _given(
                "Daily invitation target",
                daily_invite_target,
                "Use whatever the account already allows.",
            ),
            "Stage 1, audience.\n"
            "Find the people first, because a sequence written before you know "
            "who it is for reads like a mailshot. Use lead_search to see who is "
            "already stored, filtered by tag or by text. If the audience is not "
            "in the database yet, run the harvest_audience prompt first and "
            "come back. Create the campaign with campaign_create, which always "
            "starts it as a draft and runs nothing. Then enrol people with "
            "campaign_add_leads, by lead_ids or by tags. A blacklisted lead is "
            "still enrolled, into the excluded sub-list with no job, so the "
            "record of why somebody was not contacted survives.",
            "Stage 2, sequence.\n"
            "Add each step with campaign_add_step, in the order it should run. "
            "Put the free steps first. A filter step and an ICP gate reach "
            "nothing on LinkedIn and cost no budget. Qualifying before you "
            "invite is what stops the day's invitations going to the wrong "
            "people. campaign_set_icp attaches a plain-language ideal customer "
            "profile as exactly that kind of gate. Then the outreach steps.\n"
            "Only use action types the worker can actually execute: "
            + _executable_actions()
            + "\n"
            "There is no message step and no send_message action. The queue is "
            "ready for one and this repository has no message composer "
            "selectors, so an executor would be a promise it cannot keep. Build "
            "the sequence out of what exists and tell Nived that follow-up "
            "messages are manual for now. Set bunch_size when several leads "
            "should get the same step back to back.",
            "Stage 3, templates.\n"
            "Read " + FUNNEL_URI_HINT + " and linkedin://templates to see what "
            "already exists before writing anything new. Attach a stored "
            "template to a step with campaign_set_template, by id or by name. "
            "The template keeps its variables, its spintax and its whole "
            "message variations; the step only points at it, and rendering "
            "happens when the job runs. Then call campaign_preview. It renders "
            "against the people actually enrolled rather than invented "
            "examples. That is the only way to catch a template that produces "
            "\"Hi ,\" for a lead with no first name. Read every sample. Fix the "
            "template and preview again rather than approving something you "
            "have not read.",
            "Stage 4, limits.\n" + _invite_ceilings() + "\n"
            "Read linkedin://safety/today for the headroom left before you "
            "commit to a number. No tool on this server writes account_limits, "
            "so a target you were given is a plan to check the campaign against "
            "rather than a cap you can set from here. Say so plainly if the "
            "target is above what the account currently allows. Campaign steps "
            "only run inside the account's working hours, so a target the "
            "account cannot physically reach in a day means nothing.",
            "Stage 5, the gate.\n"
            "Call campaign_approve. That is the single human sign-off and it is "
            "where this prompt ends. Approving moves the campaign from draft to "
            "pending_approval and freezes the definition: campaign_add_step, "
            "campaign_set_template and campaign_set_icp all refuse afterwards. "
            "Nothing has been sent. Only campaign_start reaches active, which is "
            "the one status the worker executes, and that is a separate decision "
            "for Nived to make after he has read what you built.",
            "Never call campaign_approve or campaign_start on your own "
            "initiative. Show Nived the preview output and the step list, and "
            "wait for him to say yes.",
            voice_rules(),
        )

    @mcp.prompt(
        name=REVIEW_DRAFTS,
        description=(
            "Work the draft queue: generate text for parked drafts, then walk "
            "the human approval queue one draft at a time."
        ),
    )
    def review_drafts(kind: str = "", campaign_id: str = "") -> str:
        """Two queues, one tool, one argument. Generation, then approval."""
        return _block(
            "Work Nived's draft queue. There are two queues here and they are "
            "different jobs, so do them in order and do not mix them.",
            read_first(REVIEW_DRAFTS),
            _given("Draft kind", kind, "Work every kind."),
            _given("Campaign", campaign_id, "Work every campaign."),
            "Queue one, generation.\n"
            "Call drafts_list_pending with status='needs_generation'. These are "
            "drafts the worker parked because a step needed text that does not "
            "exist yet. Write the text, then submit it with drafts_submit. A "
            "draft of kind icp_evaluation needs a verdict object with match, "
            "score and reason rather than text. Submitted text is held to the "
            "voice rules below and refused outright if it breaks one, so read "
            "them before you write rather than after the refusal.",
            "Queue two, approval.\n"
            "Call drafts_list_pending with status='pending_approval'. These are "
            "waiting for a human, not for you. Show Nived each one with the "
            "lead it targets and the step it belongs to. Say what you would "
            "change and why. Then wait. Only call drafts_approve for a draft he "
            "has said yes to, and use reviewed_text to carry his edit rather "
            "than approving the original and hoping.",
            "Nothing sends until a draft is approved. The template engine "
            "treats an unapproved fragment as absent and refuses the whole "
            "render, so an unreviewed draft blocks its own message rather than "
            "leaking half of one.",
            voice_rules(),
        )

    @mcp.prompt(
        name=TRIAGE_REPLIES,
        description=(
            "Sort the unread inbox into replies that need Nived, replies that "
            "need a template, and people who should stop being contacted."
        ),
    )
    def triage_replies(limit: str = "") -> str:
        """Read the inbox, sort it, and draft nothing that gets sent unread."""
        return _block(
            "Triage the replies waiting in Nived's LinkedIn inbox. Sort them, "
            "propose responses, and send none of them yourself.",
            read_first(TRIAGE_REPLIES),
            _given("How many threads", limit, "Take the twenty oldest."),
            "What unread means here.\n"
            "There is no read marker in the database and nothing scrapes "
            "LinkedIn's own unread badge. A thread counts as unread when its "
            "newest stored message is inbound, meaning they replied and we did "
            "not. The resource payload carries that definition and what it "
            "excludes, so read it rather than assuming.",
            "Sort every thread into one of four buckets and say which.\n"
            "Interested: they want to continue. Draft a reply that answers the "
            "specific thing they asked. Use lead_get on the lead id first, "
            "because a reply that ignores what their profile says is worse than "
            "a slow one.\n"
            "Not now: a soft no with a reason. Propose a short "
            "acknowledgement and nothing else.\n"
            "Not interested: a clear no. Propose no reply at all, and suggest "
            "adding them to the blacklist so no campaign contacts them again.\n"
            "Not a reply: an auto-responder or an unrelated message. Say so and "
            "move on.",
            "Then hand the list to Nived with your drafts attached. He decides "
            "what goes out, and he sends it himself.\n"
            "There is no message executor on this server. The queue accepts the "
            "idea and this repository has no message composer selectors, so the "
            "enqueue tool refuses a message action. Naming it would send him "
            "down a path that fails.\n"
            "The runnable actions are "
            + _executable_actions()
            + "\n"
            "Say plainly that replying is manual today rather than implying the "
            "worker will do it.",
            voice_rules(),
        )

    @mcp.prompt(
        name=WEEKLY_REPORT,
        description=(
            "Report the week from the ledger: what ran, what it produced, what "
            "refused, and the one thing to change next week."
        ),
    )
    def weekly_report() -> str:
        """Seven days of outcomes, read from actions_log rather than guessed."""
        return _block(
            "Write Nived's weekly outreach report. Every number in it comes "
            "from a resource. Do not estimate anything and do not carry a "
            "number over from a previous report.",
            read_first(WEEKLY_REPORT),
            "The window is the last seven days, ending now. "
            "linkedin://analytics/weekly is a rolling read with no date "
            "parameter, so there is no way to ask this server for an earlier "
            "week. Say which seven days you are reporting rather than letting "
            "the reader assume.",
            "Cover five things, in this order.\n"
            "1. Volume. Actions attempted per day and per action type, from "
            "linkedin://analytics/weekly. Say how much of the ceiling that "
            "used.\n"
            "2. Outcome. Successes against failures against refusals. A "
            "refusal is not a failure: it is the gate doing its job, and the "
            "reason it gives is the interesting part.\n"
            "3. Funnel. For each active campaign, read "
            + FUNNEL_URI_HINT
            + " and report how many leads moved and how many did not.\n"
            "4. Health. Read linkedin://worker/status. A stalled worker "
            "explains a quiet week better than any theory about the audience "
            "does, and a paused worker explains it completely.\n"
            "5. One change. Name the single highest-value thing to do "
            "differently next week, and say what number would move if it "
            "worked.",
            "A quiet week has causes and they are visible. Check the refusals "
            "before you write anything about audience quality. Reaching a cap, "
            "hitting the dedupe window, sitting outside working hours and a "
            "worker that died on Friday all look identical from the outside and "
            "the ledger tells them apart.",
            voice_rules(),
        )

    @mcp.prompt(
        name=SAFETY_CHECK,
        description=(
            "Answer whether it is safe to keep going right now, and stop the "
            "worker if it is not."
        ),
    )
    def safety_check() -> str:
        """Headroom, account state, worker health, and the pause if needed."""
        return _block(
            "Answer one question for Nived: is it safe to keep sending right "
            "now. Read the resources, give a plain yes or no, then say what "
            "you read that decided it.",
            read_first(SAFETY_CHECK),
            "Check these five things.\n"
            "1. Headroom. linkedin://safety/today gives remaining budget per "
            "action type plus the global ceilings. "
            + _invite_ceilings()
            + "\n"
            "2. Account state. The same payload carries it. Anything other "
            "than active means the gate is already refusing metered work, and "
            "challenged means a human has to log in and clear it.\n"
            "3. Worker health. linkedin://worker/status reports a worker that "
            "wedged rather than one that finished. Its status says what it "
            "thought it was doing, its health says whether anyone still "
            "believes it.\n"
            "4. Unroutable jobs. A count that will not go down is a fault, not "
            "a backlog.\n"
            "5. Refusal pattern. linkedin://stats/daily shows today. A run of "
            "refusals of one type is a configuration problem. A run of "
            "failures is a LinkedIn problem.",
            "If the answer is no, stop the worker with worker_pause and give a "
            "reason. That stops both job lanes, which is the point: "
            "campaign_pause stops one campaign, and the ad-hoc lane that runs "
            "harvests and one-off invitations does not read campaign status at "
            "all. Confirm it took effect by reading linkedin://worker/status "
            "again, where paused reads true and campaigns_running reads false. "
            "Use worker_resume when it is safe, and the queue picks up where it "
            "stopped rather than being re-planned.",
            "Never raise a cap to clear a refusal. The hard ceilings clamp "
            "every configured number, so the attempt would fail quietly and the "
            "refusal is the system working.",
            voice_rules(),
        )

    @mcp.prompt(
        name=HARVEST_AUDIENCE,
        description=(
            "Pick a harvest source, queue the extraction, and check what "
            "actually landed before anyone gets contacted."
        ),
    )
    def harvest_audience(source: str, target: str = "") -> str:
        """Choose the source, queue the job, verify the leads. No contact."""
        return _block(
            "Build a list of people for Nived to reach. This prompt ends with "
            "leads in the database and nobody contacted.",
            read_first(HARVEST_AUDIENCE),
            _given(
                "Source",
                source,
                "Ask which of the seven below fits, then continue.",
            ),
            _given("Target", target, "Ask what to search for or whose post to read."),
            "Pick the source that matches what you actually know.\n"
            "harvest_people_search for a search you can describe by keywords, "
            "title, company, industry or location.\n"
            "harvest_post_engagers for the people who liked or commented on a "
            "specific post, which is the warmest source here because they have "
            "already shown what they care about.\n"
            "harvest_group_members and harvest_event_attendees for a group or "
            "an event.\n"
            "harvest_company_employees for one company's people tab.\n"
            "harvest_connections for Nived's own connections.\n"
            "harvest_import_csv for a list that already exists as a file.\n"
            "There is no Sales Navigator source. It needs a paid subscription, "
            "so it is descoped and no tool pretends otherwise.",
            "Every harvest tool queues work and returns a job id straight away. "
            "It scrapes nothing itself, so a large harvest never blocks the "
            "client. Poll harvest_status with that job id until it finishes, "
            "and report the counts it gives rather than guessing progress.",
            "Then check what landed before anything else happens. Run "
            "lead_search over the new leads and read a few with lead_get. A "
            "harvest that returned 200 rows of which 40 have no name and no "
            "title is a worse input than 30 good ones. The campaign built on it "
            "will render badly for exactly those leads. Tag the ones worth "
            "keeping so campaign_add_leads can enrol them by tag.",
            "Harvesting still spends budget. Walking search pages costs "
            "profile_search and post_read from the same daily allowance an "
            "invitation comes out of, so read linkedin://safety/today first and "
            "size the harvest to what is left.",
            voice_rules(),
        )
