"""MCP-03 (#26): every LinkedIn action is a queued job, and the worker runs it.

The issue's Definition of Done has one line that cannot be satisfied by prose:

    verified by inspection that no MCP tool in the entire server can drive
    Playwright directly

So it is a test here rather than a claim in a pull request body.
:func:`playwright_reaching_tools` AST-walks every module in the repository,
finds every `@mcp.tool()` function, follows the calls it makes into the helpers
defined beside it, and reports the ones that reach a page.

The session lifecycle exemption is named in :data:`SESSION_LIFECYCLE_TOOLS` and
nowhere else. `login_linkedin`, `login_linkedin_secure` and `close_browser`
still drive a browser because they are not LinkedIn actions: they create and
destroy the session the queue's executors need, so queueing them is circular.
The design already treats them as a separate class, since they are `login`,
`login_secure` and `browser_close` in `UNMETERED_ACTIONS` rather than metered
actions. Writing the exemption down as data, with a test that fails the moment a
fourth tool joins them, is the difference between a documented exception and
three tools quietly outside a guarantee.
"""

from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import linkedin_browser_mcp
import worker as worker_module
from linkedin_mcp.audit import Outcome
from linkedin_mcp.core.config import (
    ADHOC_CANCEL_ACTION,
    ADHOC_ENQUEUE_ACTION,
    ADHOC_QUEUE_ACTIONS,
    ADHOC_STATUS_ACTION,
    HARD_CEILINGS,
    METERED_ACTIONS,
    UNMETERED_ACTIONS,
)
from linkedin_mcp.executors import build_executors
from linkedin_mcp.executors.contract import (
    ACTION_KEY,
    ADHOC_ACTIONS,
    APPROVED_KEY,
    MAX_RESULT_BYTES,
    RESULT_KEY,
    adhoc_job_spec,
    adhoc_jobs,
    is_adhoc_action_job,
    record_job_result,
)
from linkedin_mcp.sequences import JobState, StepSpec, insert_job, transaction
from linkedin_mcp.safety import get_gate
from linkedin_mcp.tools import is_harvest_job, validated_payload
from linkedin_mcp.worker import ActionResult, ActionStatus, build_worker
from linkedin_mcp.worker.selection import ad_hoc_due_jobs

from test_worker_support import (  # noqa: F401
    BASE_TIME,
    env,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

PROFILE_URL = "https://www.linkedin.com/in/ada-lovelace/"
POST_URL = "https://www.linkedin.com/posts/ada-lovelace_activity-123"


def tick_moment() -> datetime:
    """A moment after anything a tool has just queued.

    `enqueue_action` timestamps a job with the real clock, because in production
    that is when the action becomes due. The worker fixtures elsewhere tick at a
    frozen `BASE_TIME` in the past, and a job scheduled for now is not due then.
    Ticking slightly ahead of real time is what makes the queued job visible,
    and it costs the tests nothing: nothing here depends on the wall clock.
    """
    return datetime.now(timezone.utc) + timedelta(seconds=5)



# ======================================================================
# The DoD line, as a test
# ======================================================================

SESSION_LIFECYCLE_TOOLS = {
    "close_browser",
    "login_linkedin",
    "login_linkedin_secure",
}
"""The only MCP tools allowed to drive a browser, and why.

Session lifecycle, not LinkedIn actions. An executor cannot run until a session
exists, and the only way to create one is to open a browser and let a human log
in, so queueing these would deadlock the queue on itself. They spend `login`,
`login_secure` and `browser_close`, which are unmetered precisely because they
do nothing to anyone else's LinkedIn account.

Nothing else may join this set without the reviewer of that change reading this
docstring first, which is the entire point of writing it down.
"""

PAGE_METHODS = frozenset(
    {
        "goto",
        "new_page",
        "query_selector",
        "query_selector_all",
        "wait_for_selector",
        "wait_for_url",
        "set_viewport_size",
        "screenshot",
        "keyboard",
        "mouse",
    }
)
"""Playwright surface a tool would have to touch to act on LinkedIn itself."""

BROWSER_MODULES = (
    "playwright",
    "linkedin_mcp.browser",
    "linkedin_mcp.executors.support",
    "linkedin_mcp.executors.linkedin",
)
"""Modules whose exports drive a page. Importing one is not enough; calling it is."""

BROWSER_CLASSES = frozenset({"BrowserSession", "async_playwright", "sync_playwright"})

SKIP_DIRS = frozenset({".git", ".venv", "venv", "__pycache__", "node_modules", "tests"})


def repo_modules() -> list[Path]:
    """Every Python module in the repository that could register a tool."""
    return [
        path
        for path in sorted(REPO_ROOT.rglob("*.py"))
        if not (set(path.parts) & SKIP_DIRS) and not path.name.startswith("test_")
    ]


def _is_mcp_tool(node: ast.AST) -> bool:
    for decorator in getattr(node, "decorator_list", []):
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and target.attr == "tool":
            return True
    return False


# MCP-04 (#27) ---------------------------------------------------------------
#
# This guard used to look only for `@mcp.tool`, which meant the resource surface
# added in #27 walked straight past it. Twelve `@mcp.resource` functions could
# have driven a browser and this file would have stayed green while they did,
# because the string `mcp.resource` appeared nowhere in it.
#
# A resource must never drive a page. It is a read: opening a browser to answer
# one would be slow, would spend LinkedIn budget on something that changes
# nothing, and would bypass the job queue that every write in this repository is
# required to go through. So the walk below now covers both kinds.


def _is_mcp_resource(node: ast.AST) -> bool:
    for decorator in getattr(node, "decorator_list", []):
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and target.attr == "resource":
            return True
    return False


def _surface_kind(node: ast.AST) -> str | None:
    """`"tool"`, `"resource"` or `None` for anything else."""
    if _is_mcp_tool(node):
        return "tool"
    if _is_mcp_resource(node):
        return "resource"
    return None


def mcp_surface_in(source: str, label: str = "<module>") -> dict[str, str]:
    """Every MCP-registered function in one module, mapped to its kind."""
    tree = ast.parse(source, filename=label)
    return {
        node.name: kind
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and (kind := _surface_kind(node)) is not None
    }


def _browser_names(tree: ast.Module) -> set[str]:
    """Names imported from a page-driving module, plus the browser classes."""
    names = set(BROWSER_CLASSES)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if any(
                node.module == module or node.module.startswith(module + ".")
                for module in BROWSER_MODULES
            ):
                names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if any(
                    alias.name == module or alias.name.startswith(module + ".")
                    for module in BROWSER_MODULES
                ):
                    names.add(alias.asname or alias.name.split(".")[0])
    return names


def _local_functions(tree: ast.Module) -> dict[str, ast.AST]:
    """Every function defined anywhere in the module, keyed by name.

    Nested definitions count. The tool functions in `linkedin_mcp/tools/` live
    inside a `register_*` factory, so a walk that only looked at module level
    would find no tools at all and pass vacuously.
    """
    found: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.setdefault(node.name, node)
    return found


def _direct_evidence(node: ast.AST, browser_names: set[str]) -> list[str]:
    """Page-driving things this one function body does, ignoring what it calls."""
    evidence: list[str] = []
    for inner in ast.walk(node):
        if isinstance(inner, ast.Attribute) and inner.attr in PAGE_METHODS:
            evidence.append(f"page method .{inner.attr}()")
        elif isinstance(inner, ast.Name) and inner.id in browser_names:
            evidence.append(f"browser symbol {inner.id}")
        elif isinstance(inner, ast.Attribute) and isinstance(inner.value, ast.Name):
            if inner.value.id in browser_names:
                evidence.append(f"browser symbol {inner.value.id}.{inner.attr}")
    return evidence


def playwright_reaching_surface(
    source: str, label: str = "<module>"
) -> dict[str, list[str]]:
    """Return every `@mcp.tool()` or `@mcp.resource()` that can reach a page.

    Follows calls into functions defined in the same module, because the way a
    tool would smuggle Playwright back in is through a helper rather than in the
    tool body itself. That is exactly what `login_linkedin` does today via
    `fill_selector_fallback`, and this finds it.
    """
    tree = ast.parse(source, filename=label)
    browser_names = _browser_names(tree)
    functions = _local_functions(tree)

    def reaches(node: ast.AST, seen: set[str]) -> list[str]:
        evidence = _direct_evidence(node, browser_names)
        if evidence:
            return evidence
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            callee = inner.func
            name = (
                callee.id
                if isinstance(callee, ast.Name)
                else callee.attr
                if isinstance(callee, ast.Attribute)
                else None
            )
            if name is None or name in seen or name not in functions:
                continue
            seen.add(name)
            deeper = reaches(functions[name], seen)
            if deeper:
                return [f"via {name}(): {deeper[0]}"]
        return []

    offenders: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _surface_kind(
            node
        ):
            evidence = reaches(node, {node.name})
            if evidence:
                offenders[node.name] = evidence
    return offenders


playwright_reaching_tools = playwright_reaching_surface
"""Kept under the old name so a reader grepping for it still lands here.

Before #27 this walked tools only. It now walks tools and resources, and the
name that says so is `playwright_reaching_surface`.
"""


@pytest.mark.asyncio
async def test_no_mcp_tool_in_the_server_can_drive_playwright():
    """The DoD line, checked rather than asserted.

    Every `@mcp.tool()` and, since #27, every `@mcp.resource()` in the
    repository, in every module that registers one, with the session lifecycle
    exemption named explicitly so a reviewer can see what is exempt and argue
    with it.

    The exemption is deliberately spent on tools only. A resource is a read and
    no read needs a browser to create a session, so a resource that turned up in
    the allowlist by sharing a name with an exempt tool would be a bug.
    """
    offenders: dict[str, list[str]] = {}
    for path in repo_modules():
        source = path.read_text(encoding="utf-8")
        label = str(path.relative_to(REPO_ROOT))
        kinds = mcp_surface_in(source, label)
        for name, evidence in playwright_reaching_surface(source, label).items():
            exempt = kinds.get(name) == "tool" and name in SESSION_LIFECYCLE_TOOLS
            if not exempt:
                offenders[f"{label}::{kinds.get(name, 'tool')} {name}"] = evidence

    assert offenders == {}, (
        "these MCP tools reach Playwright; they must enqueue a job instead:\n"
        + "\n".join(f"  {where}: {why}" for where, why in sorted(offenders.items()))
    )


@pytest.mark.asyncio
async def test_the_guard_finds_the_tools_it_claims_to_be_guarding():
    """A guard that walked over an empty list would pass just as quietly."""
    found: set[str] = set()
    for path in repo_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found.update(
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and _is_mcp_tool(node)
        )

    registered = {tool.name for tool in await linkedin_browser_mcp.mcp.list_tools()}
    assert registered <= found, sorted(registered - found)
    assert len(registered) >= 14
    assert SESSION_LIFECYCLE_TOOLS <= registered


@pytest.mark.asyncio
async def test_the_guard_finds_the_resources_it_claims_to_be_guarding():
    """MCP-04 (#27): the same non-vacuity check, for the resource half.

    PR #51 is the reason this exists. Fifteen tools were fully tested against a
    FastMCP instance the tests built themselves and stayed green for weeks while
    the shipped server never registered them. So the count here is taken from
    `linkedin_browser_mcp.mcp`, the object the process actually serves, and
    compared against what the source walk found.
    """
    found: set[str] = set()
    for path in repo_modules():
        found.update(
            name
            for name, kind in mcp_surface_in(
                path.read_text(encoding="utf-8"), str(path)
            ).items()
            if kind == "resource"
        )

    assert len(found) >= 12, sorted(found)

    served = await linkedin_browser_mcp.mcp.list_resources()
    templates = await linkedin_browser_mcp.mcp.list_resource_templates()
    assert len(served) + len(templates) == 12
    assert all(str(resource.uri).startswith("linkedin://") for resource in served)
    assert all(item.uri_template.startswith("linkedin://") for item in templates)


@pytest.mark.asyncio
async def test_the_guard_catches_a_tool_that_drives_a_page():
    """The same walk, against a module that does what the guard forbids."""
    smuggled = '''
from linkedin_mcp.executors.support import wait_for_selector_fallback

def helper(page):
    return wait_for_selector_fallback(page, "connect_button")

@mcp.tool()
async def like_a_post(post_url: str) -> dict:
    return await helper(page)
'''
    assert playwright_reaching_surface(smuggled, "smuggled.py")

    obvious = '''
@mcp.tool()
async def like_a_post(post_url: str) -> dict:
    page = await session.new_page(post_url)
    return {"status": "success"}
'''
    assert playwright_reaching_surface(obvious, "obvious.py")

    innocent = '''
@mcp.tool()
async def like_a_post(post_url: str) -> dict:
    return enqueue_action("post_like", {"post_url": post_url})
'''
    assert playwright_reaching_surface(innocent, "innocent.py") == {}


@pytest.mark.asyncio
async def test_the_guard_catches_a_resource_that_drives_a_page():
    """MCP-04 (#27): three ways a resource could reach a page, all caught.

    Directly, through one helper, and through two. The two-hop case is the one
    worth having: a resource that called a formatting helper that happened to
    call a scraping helper is how this would arrive in real life, and it is
    invisible to anything that only reads the decorated function body.
    """
    direct = '''
@mcp.resource("linkedin://campaigns")
async def campaigns_resource() -> str:
    page = await session.new_page("https://www.linkedin.com/feed/")
    return "{}"
'''
    assert playwright_reaching_surface(direct, "direct.py") == {
        "campaigns_resource": ["page method .new_page()"]
    }

    one_hop = '''
from linkedin_mcp.executors.support import wait_for_selector_fallback

def scrape(page):
    return wait_for_selector_fallback(page, "campaign_card")

@mcp.resource("linkedin://campaigns")
async def campaigns_resource() -> str:
    return await scrape(page)
'''
    assert "campaigns_resource" in playwright_reaching_surface(one_hop, "one_hop.py")

    two_hops = '''
from linkedin_mcp.browser import BrowserSession

def open_page():
    return BrowserSession()

def gather():
    return open_page()

@mcp.resource("linkedin://campaigns")
async def campaigns_resource() -> str:
    return gather()
'''
    evidence = playwright_reaching_surface(two_hops, "two_hops.py")
    assert "campaigns_resource" in evidence
    assert "via gather()" in evidence["campaigns_resource"][0]

    innocent = '''
@mcp.resource("linkedin://campaigns")
async def campaigns_resource() -> str:
    return json.dumps(campaigns_overview(tool_connection(), tool_account_id()))
'''
    assert playwright_reaching_surface(innocent, "innocent.py") == {}


@pytest.mark.asyncio
async def test_the_resource_guard_is_non_vacuous_against_the_real_module():
    """MCP-04 (#27): mutate the shipped resource module and watch the guard bite.

    The snippets above prove the walk works on strings someone wrote to be
    caught. This proves it works on the file that actually registers the twelve
    resources, by appending a page-driving resource to a copy of its real source
    and re-running the same walk the repository-wide guard runs.

    The mutation is asserted to have landed, and the mutated source is asserted
    to still parse, before the result is read. A syntactically broken injection
    would fail the walk for the wrong reason and prove nothing.
    """
    path = REPO_ROOT / "linkedin_mcp" / "resources" / "server.py"
    source = path.read_text(encoding="utf-8")
    label = str(path.relative_to(REPO_ROOT))

    assert playwright_reaching_surface(source, label) == {}

    mutation = (
        "\n\n"
        "@mcp.resource('linkedin://campaigns/scraped')\n"
        "async def scraped_campaigns() -> str:\n"
        "    page = await session.new_page('https://www.linkedin.com/feed/')\n"
        "    return await page.query_selector_all('.campaign')\n"
    )
    mutated = source + mutation

    assert "scraped_campaigns" in mutated
    ast.parse(mutated, filename=label)

    caught = playwright_reaching_surface(mutated, label)
    assert "scraped_campaigns" in caught, caught
    assert path.read_text(encoding="utf-8") == source


@pytest.mark.asyncio
async def test_the_lifecycle_exemption_is_only_session_lifecycle():
    """The three exempt tools must be the ones the allowlist claims they are.

    A tool named `login_linkedin` that had quietly grown a "and also send these
    invitations" branch would still be allowlisted by name. Its audited action
    type is what says what it actually spends, so that is what is checked.
    """
    source = (REPO_ROOT / "linkedin_browser_mcp.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    audited: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Name)
                and decorator.func.id == "audit_linkedin_action"
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
            ):
                audited[node.name] = decorator.args[0].value

    exempt = {audited[name] for name in SESSION_LIFECYCLE_TOOLS}
    assert exempt == {"login", "login_secure", "browser_close"}
    assert exempt <= UNMETERED_ACTIONS
    assert not (exempt & METERED_ACTIONS)


# ======================================================================
# The migration path, driven end to end
# ======================================================================


class FakeElement:
    """One control on the page. Records what was done to it."""

    def __init__(self, name: str, page: "FakeProfilePage") -> None:
        self.name = name
        self.page = page
        self.typed: list[str] = []

    async def click(self) -> None:
        self.page.clicked.append(self.name)

    async def type(self, chunk: str) -> None:
        self.typed.append(chunk)

    async def fill(self, value: str) -> None:
        self.typed.append(value)

    async def press(self, key: str) -> None:
        self.page.pressed.append(key)

    @property
    def text(self) -> str:
        return "".join(self.typed)


class FakeProfilePage:
    """A LinkedIn profile with a Connect button, and nothing else.

    Enough surface for `goto_profile(direct=True)`, the detection sweep and the
    connection request executor. It is deliberately not a general Playwright
    stand-in: a test that needed one would be testing Playwright.
    """

    def __init__(self, url: str = "https://www.linkedin.com/feed/") -> None:
        self.url = url
        self.visited: list[str] = []
        self.clicked: list[str] = []
        self.pressed: list[str] = []
        self.elements: dict[str, FakeElement] = {}

    async def goto(self, url: str, wait_until: str | None = None, timeout: int | None = None):
        self.visited.append(url)
        self.url = url

    def _element(self, name: str) -> FakeElement:
        return self.elements.setdefault(name, FakeElement(name, self))

    async def wait_for_selector(self, selector: str, timeout: int | None = None):
        # Matched against the real selector catalogue rather than by guesswork,
        # so a selector rename breaks this test instead of quietly making it
        # exercise a different control.
        if "data-member-id" in selector or "top-card" in selector:
            return self._element("top_card")
        if "Invite" in selector:
            return self._element("connect")
        if "Add a note" in selector:
            return self._element("add_note")
        if selector.startswith("textarea"):
            return self._element("note_field")
        if "Send invitation" in selector:
            return self._element("send")
        raise TimeoutError(selector)

    async def query_selector(self, selector: str):
        return None

    async def content(self) -> str:
        return "<html><body>Ada Lovelace</body></html>"

    async def title(self) -> str:
        return "Ada Lovelace | LinkedIn"

    async def evaluate(self, script: str, arg: Any = None):
        return "Ada Lovelace"


class FakeBrowser:
    """The `BrowserSession` shape the runner hands an executor."""

    def __init__(self, page: FakeProfilePage) -> None:
        self.page = page
        self.pages_opened = 0
        self.saved = 0

    async def new_page(self, url: str | None = None):
        self.pages_opened += 1
        if url:
            await self.page.goto(url)
        return self.page

    async def save_session(self, page: Any) -> None:
        self.saved += 1


class Ctx:
    """The MCP context object, which these tools only ever log through."""

    def info(self, *args: Any) -> None:
        pass

    def warning(self, *args: Any) -> None:
        pass

    def error(self, *args: Any) -> None:
        pass


def worker_for(env, browser: FakeBrowser | None = None, **config):
    async def supplier():
        return browser

    settings = {"pace_between_actions": False, "sweep_every_ticks": 1, **config}
    return build_worker(
        env.conn,
        env.account_id,
        worker_id="w1",
        executors=build_executors(),
        clock=env.clock,
        browser_supplier=supplier,
        **settings,
    )


@pytest.mark.asyncio
async def test_a_connection_request_runs_from_tool_call_to_ledger(env):
    """The migration path, in one test.

    Tool call, queue row, selection, safety gate, executor, `actions_log`. This
    is the test that says the product still works: if it passes, the eight tools
    that used to act inline still reach LinkedIn, they just do it from the
    worker where the caps bind.
    """
    page = FakeProfilePage()
    browser = FakeBrowser(page)

    queued = await linkedin_browser_mcp.send_connection_request(
        PROFILE_URL, Ctx(), note="Enjoyed your piece on Copilot rollouts.", direct=True
    )

    assert queued["status"] == "queued"
    assert queued["action_type"] == "connection_request"
    assert browser.pages_opened == 0, "the tool must not have touched a browser"

    row = env.job(queued["job_id"])
    assert row["state"] == JobState.PENDING.value
    assert row["campaign_id"] is None
    payload = json.loads(row["payload_json"])
    assert payload[ACTION_KEY] == "connection_request"
    assert payload[APPROVED_KEY] is True
    assert payload["profile_url"] == PROFILE_URL

    # The seam MCP-02 built for harvests routes this without a line of new code.
    due = ad_hoc_due_jobs(env.conn, env.account_id, now=tick_moment())
    assert queued["job_id"] in [job.id for job in due]

    report = await worker_for(env, browser).tick(now=tick_moment())

    assert [job.outcome for job in report.jobs] == ["success"]
    assert page.visited == [PROFILE_URL]
    assert page.clicked == ["connect", "add_note", "send"]
    assert browser.saved == 1

    logged = env.logged(action_type="connection_request")
    assert len(logged) == 1
    assert logged[0]["outcome"] == Outcome.SUCCESS.value
    assert json.loads(logged[0]["detail_json"])["target"] == PROFILE_URL

    status = await linkedin_browser_mcp.mcp.call_tool(
        "action_status", {"job_id": queued["job_id"]}
    )
    answer = status.structured_content
    assert answer["state"] == JobState.DONE.value
    assert answer["result"]["profile_name"] == "Ada Lovelace"
    assert answer["result"]["note_included"] is True


@pytest.mark.asyncio
async def test_a_queued_action_passes_the_gate_gets_jitter_and_lands_in_the_ledger(env):
    """DoD line three, proved rather than asserted.

    All three happen in the worker, on the way to LinkedIn, which is the point:
    an inline tool could ask the gate and then act regardless, and nothing about
    its pacing or its ledger row was enforced by anything but its own good
    manners.
    """
    page = FakeProfilePage()
    first = await linkedin_browser_mcp.send_connection_request(
        PROFILE_URL, Ctx(), direct=True
    )
    second = await linkedin_browser_mcp.send_connection_request(
        "https://www.linkedin.com/in/grace-hopper/", Ctx(), direct=True
    )
    assert first["status"] == second["status"] == "queued"

    asked: list[str] = []
    gate = get_gate()
    original = gate.acquire

    def watched(account_id, action_type, *args, **kwargs):
        lease = original(account_id, action_type, *args, **kwargs)
        asked.append(action_type)
        return lease

    gate.acquire = watched
    env.sleeper.calls.clear()
    try:
        report = await worker_for(env, FakeBrowser(page), pace_between_actions=True).tick(
            now=tick_moment()
        )
    finally:
        gate.acquire = original

    # The gate: asked once per action, at execution time, by the worker.
    assert asked == ["connection_request", "connection_request"]

    # CORE-04: the humanizer paced the run rather than anything sleeping directly.
    assert env.sleeper.calls, "no pacing was applied between two LinkedIn actions"

    # The ledger the gate reads next time.
    ledger = env.logged(action_type="connection_request")
    assert [row["outcome"] for row in ledger] == [Outcome.SUCCESS.value] * 2
    assert [job.outcome for job in report.jobs] == ["success", "success"]


@pytest.mark.asyncio
async def test_the_daily_cap_now_binds_on_a_tool_that_used_to_ignore_it(env):
    """The reason this issue exists.

    An inline tool asked the gate once and then acted, so the cap was a number
    the caller was trusted to respect. Now the gate is asked again at execution
    time, by the worker, and a job queued under the cap and executed over it is
    refused rather than sent.
    """
    page = FakeProfilePage()
    browser = FakeBrowser(page)

    queued = await linkedin_browser_mcp.send_connection_request(
        PROFILE_URL, Ctx(), direct=True
    )
    assert queued["status"] == "queued"

    # The budget disappears between the enqueue and the execution, exactly as it
    # would if another worker spent it in the meantime.
    env.set_cap("connection_request", daily_cap=0)

    report = await worker_for(env, browser).tick(now=tick_moment())

    # `retry_scheduled` rather than a flat refusal: the daily cap resets, so the
    # runner reschedules instead of throwing the invitation away.
    assert [job.outcome for job in report.jobs] == ["retry_scheduled"]
    assert page.visited == [], "a refused action must not reach LinkedIn"
    assert env.logged(action_type="connection_request", outcome=Outcome.REFUSED.value)


@pytest.mark.asyncio
async def test_a_manual_action_and_a_campaign_action_are_the_same_row(env):
    """The DoD's "indistinguishable once it reaches the queue", measured.

    They are not byte-identical and should not be: a campaign job carries the
    campaign and step it came from, which is how the runner knows whose approval
    rule to consult. Everything the safety gate reads is identical, which is the
    property that matters, so that is what this compares.
    """
    campaign_id = env.campaign(
        [StepSpec("connection_request", config={"direct": True})], approval_mode="auto"
    )
    lead_id = env.lead("Ada", public_id="ada-campaign")
    env.enrol(campaign_id, [lead_id])

    manual = await linkedin_browser_mcp.send_connection_request(
        "https://www.linkedin.com/in/grace-hopper/", Ctx(), direct=True
    )

    page = FakeProfilePage()
    await worker_for(env, FakeBrowser(page)).tick(now=tick_moment())

    rows = {row["id"]: row for row in env.conn.execute("SELECT * FROM jobs").fetchall()}
    manual_row = rows[manual["job_id"]]
    campaign_row = next(
        row for row in rows.values() if row["campaign_id"] == campaign_id
    )

    # What the gate meters: same account, same action type, same table.
    assert manual_row["account_id"] == campaign_row["account_id"]
    assert manual_row["action_type"] == campaign_row["action_type"] == "connection_request"

    # What differs, and why it has to.
    assert manual_row["campaign_id"] is None and campaign_row["campaign_id"] == campaign_id
    assert manual_row["step_id"] is None and campaign_row["step_id"] is not None

    ledger = env.logged(action_type="connection_request")
    assert len(ledger) == 2
    assert {row["outcome"] for row in ledger} == {Outcome.SUCCESS.value}
    assert {row["account_id"] for row in ledger} == {env.account_id}
    # The gate counts rows in this table by account, action type and time. None
    # of those three distinguishes the two, which is the whole claim.
    assert len({(row["account_id"], row["action_type"]) for row in ledger}) == 1


@pytest.mark.asyncio
async def test_every_action_tool_enqueues_and_none_of_them_acts(env):
    """All eight migrated tools, each producing a job and touching nothing."""
    ctx = Ctx()
    calls = [
        (linkedin_browser_mcp.get_linkedin_profile, ("ada-lovelace", ctx), {}),
        (linkedin_browser_mcp.browse_linkedin_feed, (ctx,), {"count": 3}),
        (linkedin_browser_mcp.search_linkedin_profiles, ("copilot", ctx), {}),
        (linkedin_browser_mcp.view_linkedin_profile, (PROFILE_URL, ctx), {}),
        (linkedin_browser_mcp.interact_with_linkedin_post, (POST_URL, ctx), {"action": "like"}),
        (linkedin_browser_mcp.send_connection_request, (PROFILE_URL, ctx), {}),
        (linkedin_browser_mcp.search_linkedin_posts, ("copilot", ctx), {}),
    ]
    for tool, args, kwargs in calls:
        result = await tool(*args, **kwargs)
        assert result["status"] == "queued", (tool.__name__, result)
        assert "job_id" in result

    batch = await linkedin_browser_mcp.comment_on_approved_posts(
        [{"post_url": POST_URL, "comment": "Matches what I see in enterprise rollouts."}],
        ctx,
    )
    assert batch["summary"] == {"total": 1, "queued": 1, "refused": 0, "failed": 0}

    jobs = adhoc_jobs(env.conn, env.account_id)
    assert len(jobs) == 8
    assert all(job.state == JobState.PENDING.value for job in jobs)
    assert {job.payload[ACTION_KEY] for job in jobs} == {
        "profile_view",
        "feed_browse",
        "profile_search",
        "post_like",
        "connection_request",
        "post_search",
        "post_comment",
    }


@pytest.mark.asyncio
async def test_the_refusals_a_caller_used_to_get_are_the_refusals_they_get(env):
    """Validation still happens at the call site, against the same arguments."""
    ctx = Ctx()

    bad_profile = await linkedin_browser_mcp.send_connection_request("nonsense", ctx)
    assert bad_profile["status"] == "error"
    assert "linkedin.com/in/" in bad_profile["message"]

    long_note = await linkedin_browser_mcp.send_connection_request(
        PROFILE_URL, ctx, note="x" * 301
    )
    assert long_note["status"] == "error"
    assert "too long" in long_note["message"]

    bad_post = await linkedin_browser_mcp.interact_with_linkedin_post("nonsense", ctx)
    assert bad_post == {"status": "error", "message": "Invalid LinkedIn post URL"}

    bad_action = await linkedin_browser_mcp.interact_with_linkedin_post(
        POST_URL, ctx, action="endorse"
    )
    assert bad_action["status"] == "error"
    assert "Invalid action" in bad_action["message"]

    # The one refusal the inline version did not make: a comment with no text
    # used to be silently downgraded to a read.
    empty_comment = await linkedin_browser_mcp.interact_with_linkedin_post(
        POST_URL, ctx, action="comment"
    )
    assert empty_comment["status"] == "error"
    assert "comment is required" in empty_comment["message"]

    assert adhoc_jobs(env.conn, env.account_id) == []


# ======================================================================
# The three MCP-03 tools
# ======================================================================


@pytest.mark.asyncio
async def test_the_generic_tool_queues_what_the_legacy_tools_queue(env):
    queued = await linkedin_browser_mcp.mcp.call_tool(
        "action_enqueue_adhoc",
        {"action": "connection_request", "profile_url": PROFILE_URL, "approved": True},
    )
    answer = queued.structured_content
    assert answer["status"] == "queued"
    assert answer["action_type"] == "connection_request"
    assert answer["legacy_tool"] == "send_connection_request"
    assert answer["approval_required"] is True

    listed = await linkedin_browser_mcp.mcp.call_tool("action_status", {})
    assert listed.structured_content["count"] == 1


@pytest.mark.asyncio
async def test_an_unknown_action_names_the_ones_that_exist(env):
    result = await linkedin_browser_mcp.mcp.call_tool(
        "action_enqueue_adhoc", {"action": "endorse_skills"}
    )
    answer = result.structured_content
    assert answer["status"] == "error"
    assert "endorse_skills" not in answer["known_actions"]
    assert "connection_request" in answer["known_actions"]
    assert adhoc_jobs(env.conn, env.account_id) == []


@pytest.mark.asyncio
async def test_a_pending_action_can_be_cancelled_and_a_leased_one_cannot(env):
    queued = await linkedin_browser_mcp.browse_linkedin_feed(Ctx(), count=3)
    cancelled = await linkedin_browser_mcp.mcp.call_tool(
        "action_cancel", {"job_id": queued["job_id"]}
    )
    assert cancelled.structured_content["state"] == JobState.CANCELLED.value
    assert env.job(queued["job_id"])["state"] == JobState.CANCELLED.value

    again = await linkedin_browser_mcp.mcp.call_tool(
        "action_cancel", {"job_id": queued["job_id"]}
    )
    assert again.structured_content["status"] == "error"
    assert "too late" in again.structured_content["message"]

    missing = await linkedin_browser_mcp.mcp.call_tool("action_cancel", {"job_id": 9999})
    assert missing.structured_content["status"] == "error"


@pytest.mark.asyncio
async def test_a_cancelled_action_is_never_executed(env):
    page = FakeProfilePage()
    queued = await linkedin_browser_mcp.send_connection_request(
        PROFILE_URL, Ctx(), direct=True
    )
    await linkedin_browser_mcp.mcp.call_tool("action_cancel", {"job_id": queued["job_id"]})

    report = await worker_for(env, FakeBrowser(page)).tick(now=tick_moment())

    assert list(report.jobs) == []
    assert page.visited == []
    assert env.logged(action_type="connection_request") == []


# ======================================================================
# Payload validation
# ======================================================================


@pytest.mark.parametrize(
    ("name", "fields", "fragment"),
    [
        ("connection_request", {"profile_url": "nope"}, "linkedin.com/in/"),
        ("connection_request", {"profile_url": PROFILE_URL, "note": "x" * 301}, "too long"),
        ("post_like", {"post_url": "nope"}, "Invalid LinkedIn post URL"),
        ("post_comment", {"post_url": POST_URL, "comment": "  "}, "comment is required"),
        ("profile_search", {"query": "  "}, "query is required"),
        ("profile_search", {"query": "ok", "count": 0}, "at least 1"),
        ("post_search", {"query": "ok", "sort_by": "vibes"}, "sort_by must be one of"),
        ("feed_browse", {"post_url": POST_URL}, "does not take"),
        ("profile_view", {"profile_url": PROFILE_URL, "shape": "everything"}, "shape must be"),
    ],
)
def test_validated_payload_refuses_what_the_inline_tools_refused(name, fields, fragment):
    with pytest.raises(ValueError) as refused:
        validated_payload(name, fields)
    assert fragment in str(refused.value)


def test_validated_payload_fills_in_the_defaults_the_tools_had():
    assert validated_payload("feed_browse", {}) == {"count": 5}
    assert validated_payload("post_search", {"query": "copilot"}) == {
        "query": "copilot",
        "count": 10,
        "sort_by": "relevance",
    }
    assert validated_payload("profile_view", {"profile_url": PROFILE_URL})["shape"] == "detail"


def test_a_count_larger_than_the_page_can_hold_is_clamped_not_refused():
    assert validated_payload("feed_browse", {"count": 5000})["count"] == 50
    assert validated_payload("profile_search", {"query": "a", "count": 5000})["count"] == 100


# ======================================================================
# The executor side
# ======================================================================


@pytest.mark.asyncio
async def test_the_worker_registers_an_executor_for_every_registered_action():
    """A queued action nothing can run would fail with a job id and no clue."""
    executors = build_executors()
    assert set(executors) == {action.action_type for action in ADHOC_ACTIONS.values()}
    assert set(worker_module.default_executors()) == set(executors)


def test_the_daemon_can_still_be_started_with_an_empty_registry():
    """`--no-default-executors` is what a scheduling-only run wants."""
    on = worker_module.parse_args(["--db", "x.db", "--account", "1"])
    assert on.no_default_executors is False
    off = worker_module.parse_args(
        ["--db", "x.db", "--account", "1", "--no-default-executors"]
    )
    assert off.no_default_executors is True


def test_the_worker_still_imports_no_browser_at_module_level():
    """`--status` has to work on a machine with no Playwright installed.

    Which is why `DEFAULT_EXECUTORS` is a `module:attribute` string resolved
    when the daemon actually starts, rather than an import at the top of this
    file.
    """
    source = (REPO_ROOT / "worker.py").read_text(encoding="utf-8")
    assert "import playwright" not in source
    assert "from linkedin_mcp.executors" not in source
    assert "BrowserSession" in source


@pytest.mark.asyncio
async def test_a_harvest_is_not_mistaken_for_a_one_off_search(env):
    """MCP-02's People harvest also spends `profile_search` and has no campaign.

    Both arrive at the same executor because the registry is keyed by
    `action_type`. If this executor ran the harvest as a one-off search it would
    spend the budget, return search results, and store none of the leads the
    harvest was queued to collect.
    """
    harvest_id = env.enqueue_ad_hoc(
        "profile_search",
        payload=json.dumps({"harvest": "people_search", "run_id": 7}),
        scheduled_for=BASE_TIME,
    )
    harvest = next(
        job
        for job in ad_hoc_due_jobs(env.conn, env.account_id, now=tick_moment())
        if job.id == harvest_id
    )
    assert is_harvest_job(harvest)
    assert not is_adhoc_action_job(harvest)

    report = await worker_for(env, None).tick(now=tick_moment())

    outcome = next(job for job in report.jobs if job.job_id == harvest_id)
    assert outcome.outcome in {"failure", "retry_scheduled"}
    assert "harvest" in (env.job(harvest_id)["last_error"] or "").lower()


@pytest.mark.asyncio
async def test_an_action_queued_as_the_wrong_type_is_refused_not_run(env):
    """A row whose payload and `action_type` disagree charges the wrong budget."""
    mismatched = env.enqueue_ad_hoc(
        "profile_search",
        payload=json.dumps({ACTION_KEY: "connection_request", APPROVED_KEY: True}),
    )
    page = FakeProfilePage()

    await worker_for(env, FakeBrowser(page)).tick(now=tick_moment())

    assert page.visited == []
    assert "wrong budget" in (env.job(mismatched)["last_error"] or "")


@pytest.mark.asyncio
async def test_an_executor_with_no_browser_says_so_rather_than_pretending(env):
    await linkedin_browser_mcp.send_connection_request(PROFILE_URL, Ctx(), direct=True)

    report = await worker_for(env, None).tick(now=tick_moment())

    assert [job.outcome for job in report.jobs] == ["retry_scheduled"]
    assert env.logged(action_type="connection_request", outcome=Outcome.FAILURE.value)


# ======================================================================
# Campaign steps: the "sequence" in "sequence execution tools"
# ======================================================================


@pytest.mark.asyncio
async def test_a_campaign_step_runs_through_the_same_executor(env):
    """A sequenced invitation now actually goes out.

    The registry is keyed by `action_type`, so the campaign lane and the ad-hoc
    lane arrive at the same executor. A campaign step names no action in its
    payload, because SEQ-02 defines a step by its type and its config, so the
    dispatcher recognises it by the presence of `ctx.step` and derives the
    target from the lead. Without this, registering executors would have made
    every one-off action work and left every campaign failing.
    """
    campaign_id = env.campaign(
        [StepSpec("connection_request", config={"direct": True, "note": "Hello Ada."})]
    )
    lead_id = env.lead("Ada Lovelace", public_id="ada-lovelace")
    env.enrol(campaign_id, [lead_id])

    page = FakeProfilePage()
    report = await worker_for(env, FakeBrowser(page)).tick(now=tick_moment())

    assert [job.outcome for job in report.jobs] == ["success"]
    assert page.visited == ["https://www.linkedin.com/in/ada-lovelace"]
    assert page.clicked == ["connect", "add_note", "send"]
    assert page.elements["note_field"].text == "Hello Ada."

    logged = env.logged(action_type="connection_request")
    assert len(logged) == 1
    assert logged[0]["campaign_id"] == campaign_id
    assert logged[0]["lead_id"] == lead_id


@pytest.mark.asyncio
async def test_a_campaign_step_targets_its_own_lead_and_nobody_elses(env):
    """The URL is derived from the lead, never read from the payload.

    A step is generated by the scheduler for a lead it picked. If the payload
    could name the target, a step aimed at one lead could act on another, and
    the `actions_log` row would still say it was the first one.
    """
    campaign_id = env.campaign(
        [StepSpec("connection_request", config={"direct": True})]
    )
    lead_id = env.lead("Grace Hopper", public_id="grace-hopper")
    env.enrol(campaign_id, [lead_id])

    with transaction(env.conn):
        env.conn.execute(
            "UPDATE jobs SET payload_json = ? WHERE campaign_id = ?",
            (json.dumps({"profile_url": PROFILE_URL}), campaign_id),
        )

    page = FakeProfilePage()
    await worker_for(env, FakeBrowser(page)).tick(now=tick_moment())

    assert page.visited == ["https://www.linkedin.com/in/grace-hopper"]


@pytest.mark.asyncio
async def test_a_campaign_step_with_no_lead_profile_skips_rather_than_guesses(env):
    campaign_id = env.campaign(
        [StepSpec("connection_request", config={"direct": True})]
    )
    lead_id = env.lead("Anonymous", member_id="urn:li:member:9")
    env.enrol(campaign_id, [lead_id])

    page = FakeProfilePage()
    report = await worker_for(env, FakeBrowser(page)).tick(now=tick_moment())

    assert [job.outcome for job in report.jobs] == ["skipped"]
    assert page.visited == []


@pytest.mark.asyncio
async def test_a_campaign_step_this_executor_cannot_target_fails_by_name(env):
    """A `post_like` step has no target derivable from a lead, and says so."""
    campaign_id = env.campaign([StepSpec("post_like")])
    lead_id = env.lead("Ada", public_id="ada-post")
    env.enrol(campaign_id, [lead_id])

    page = FakeProfilePage()
    await worker_for(env, FakeBrowser(page)).tick(now=tick_moment())

    assert page.visited == []
    detail = json.dumps(
        [dict(row) for row in env.logged(action_type="post_like")]
    )
    assert "no target this executor can derive" in detail


@pytest.mark.asyncio
async def test_a_result_too_large_for_the_queue_is_replaced_not_trimmed(env):
    job_id = env.enqueue_ad_hoc(
        "feed_browse", payload=json.dumps({ACTION_KEY: "feed_browse"})
    )
    assert record_job_result(env.conn, job_id, {"posts": ["x" * 200] * 500})

    stored = json.loads(env.job(job_id)["payload_json"])[RESULT_KEY]
    assert stored["truncated"] is True
    assert stored["bytes"] > MAX_RESULT_BYTES
    assert "fewer items" in stored["message"]
    # And the action name is still readable, so `action_status` still works.
    assert json.loads(env.job(job_id)["payload_json"])[ACTION_KEY] == "feed_browse"


@pytest.mark.asyncio
async def test_a_small_result_is_stored_whole(env):
    job_id = env.enqueue_ad_hoc(
        "feed_browse", payload=json.dumps({ACTION_KEY: "feed_browse"})
    )
    assert record_job_result(env.conn, job_id, {"posts": [{"content": "hello"}]})
    assert json.loads(env.job(job_id)["payload_json"])[RESULT_KEY] == {
        "posts": [{"content": "hello"}]
    }
    assert not record_job_result(env.conn, 9999, {"posts": []})


@pytest.mark.asyncio
async def test_an_action_that_happened_is_not_retried_because_the_result_would_not_store(env):
    """Losing a result must not resend an invitation.

    `_finish` records the answer after the page work, so a write failure there
    means the invitation is already gone. Failing the job would retry it.
    """
    page = FakeProfilePage()
    browser = FakeBrowser(page)
    await linkedin_browser_mcp.send_connection_request(PROFILE_URL, Ctx(), direct=True)

    from linkedin_mcp.executors import linkedin as executors

    def explode(*args, **kwargs):
        raise sqlite_error()

    def sqlite_error():
        return RuntimeError("disk full")

    original = executors.record_job_result
    executors.record_job_result = explode
    try:
        report = await worker_for(env, browser).tick(now=tick_moment())
    finally:
        executors.record_job_result = original

    assert [job.outcome for job in report.jobs] == ["success"]
    assert page.clicked == ["connect", "send"]
    assert env.logged(action_type="connection_request", outcome=Outcome.SUCCESS.value)


# ======================================================================
# Configuration and schema
# ======================================================================


def test_the_queue_tools_spend_an_action_type_the_config_knows():
    assert ADHOC_QUEUE_ACTIONS <= UNMETERED_ACTIONS
    assert not (ADHOC_QUEUE_ACTIONS & METERED_ACTIONS)
    assert ADHOC_QUEUE_ACTIONS == {
        ADHOC_ENQUEUE_ACTION,
        ADHOC_STATUS_ACTION,
        ADHOC_CANCEL_ACTION,
    }
    # Enqueueing is not itself a LinkedIn action, so it has no ceiling. Every
    # action it can queue does.
    assert not (ADHOC_QUEUE_ACTIONS & set(HARD_CEILINGS))
    for action in ADHOC_ACTIONS.values():
        assert action.action_type in METERED_ACTIONS, action.name


def test_mcp_03_added_no_migration():
    """Every table this issue needs already exists.

    Asserted against this issue's own files rather than against the repository's
    complete migration list. A test that pinned the whole list would fail the
    next time any unrelated issue added one, and would blame the author of this
    one for it. Two pull requests shipped exactly that bug and both had to fix
    it.
    """
    mine = [
        REPO_ROOT / "linkedin_mcp" / "executors",
        REPO_ROOT / "linkedin_mcp" / "tools" / "actions.py",
    ]
    for path in mine:
        assert not list(path.rglob("*.sql")) if path.is_dir() else path.suffix == ".py"

    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [*(REPO_ROOT / "linkedin_mcp" / "executors").glob("*.py"),
                     REPO_ROOT / "linkedin_mcp" / "tools" / "actions.py",
                     REPO_ROOT / "linkedin_browser_mcp.py"]
    )
    assert "CREATE TABLE" not in sources
    assert "ALTER TABLE" not in sources


def test_the_job_spec_a_tool_writes_is_the_shape_selection_already_routes():
    spec = adhoc_job_spec(1, "connection_request", {"profile_url": PROFILE_URL})
    assert spec.campaign_id is None
    assert spec.step_id is None
    assert spec.action_type == "connection_request"
    assert spec.priority == 0
    assert json.loads(spec.payload_json)[ACTION_KEY] == "connection_request"


@pytest.mark.asyncio
async def test_a_job_this_account_does_not_own_is_not_readable(env):
    other = env.log.ensure_account("other@example.com")
    spec = adhoc_job_spec(other, "feed_browse", {"count": 3})
    with transaction(env.conn):
        stranger = insert_job(env.conn, spec, state=JobState.PENDING)

    assert stranger not in [job.id for job in adhoc_jobs(env.conn, env.account_id)]
    answer = await linkedin_browser_mcp.mcp.call_tool(
        "action_status", {"job_id": stranger}
    )
    assert answer.structured_content["status"] == "error"


@pytest.mark.asyncio
async def test_the_executor_result_shape_is_what_the_runner_expects():
    """`ActionResult` is the contract; a dict would be silently mishandled."""
    executors = build_executors()
    for action_type, executor in executors.items():
        result = await executor(_ContextWithoutAction(action_type))
        assert isinstance(result, ActionResult), action_type
        assert result.status is ActionStatus.FAILED


class _ContextWithoutAction:
    """Just enough `ActionContext` for the dispatcher's refusal path."""

    def __init__(self, action_type: str) -> None:
        self.action_type = action_type
        self.payload: dict[str, Any] = {}
        self.job = type("Job", (), {"id": 1})()
        self.browser = None
        self.conn = None
        self.step = None
        self.lead_id = None
        self.config: dict[str, Any] = {}
