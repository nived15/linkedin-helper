"""Every registration factory in ``linkedin_mcp`` must reach the shipped server.

Tools, resources and prompts are all registered by ``register_*(mcp)`` factories
that a package ships and ``linkedin_browser_mcp`` is expected to call. Nothing
enforced the second half of that sentence, and twice it did not happen: MCP-01's
twelve campaign tools and SEQ-05's three draft tools were written, tested,
reviewed and merged while remaining invisible to every real MCP client.

The failure is quiet by construction. A factory's own suite registers against a
``FastMCP`` instance the test creates, so it stays green whether or not the real
server ever calls the factory. Both halves pass; only the wiring between them is
missing, and nothing was looking at the wiring.

MCP-05 (#28) merged the walks
-----------------------------
This file used to check ``register_*_tools`` only, and #27 added a near-copy of
both tests for its single resource factory. #28 would have made three copies, so
instead there is one walk over every ``register_*`` factory and one runtime check
that compares tools, resources, resource templates and prompts at once. A fourth
surface is then covered the day it is written rather than when somebody
remembers to clone the pair of tests again.

Which names count as a factory is deliberately broad: anything called
``register_*`` that is not on the short exclusion list below. Narrowing it to a
suffix is what let ``register_linkedin_resources`` sit outside the guard.
"""

import ast
import importlib
from pathlib import Path

import pytest
from fastmcp import FastMCP

import linkedin_browser_mcp

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_FILE = REPO_ROOT / "linkedin_browser_mcp.py"
PACKAGE_ROOT = REPO_ROOT / "linkedin_mcp"

NOT_MCP_FACTORIES = frozenset({"register_filter", "register_icp_filter"})
"""``register_*`` functions that register something other than an MCP surface.

`register_filter` adds a SEQ-01 sequence filter predicate and
`register_icp_filter` wires SEQ-05's ICP gate onto it. Neither takes a FastMCP
server and neither is called from the entry point by design, so demanding they
be reachable from it would be demanding the wrong thing. Named here rather than
excluded by a pattern, so a future factory has to be argued about instead of
silently matching a regex somebody wrote for a different reason.
"""


def _is_factory(name):
    return name.startswith("register_") and name not in NOT_MCP_FACTORIES


def _module_name(path):
    return ".".join(path.relative_to(REPO_ROOT).with_suffix("").parts)


def _factories_defined():
    """Map every ``register_*`` factory to its module and its own calls.

    A factory may register another rather than the server doing it directly:
    ``register_lead_tools`` calls ``register_crm_tools`` and
    ``register_harvest_tools``. Those are wired, just indirectly, so this builds
    a call graph rather than demanding the server name all of them.

    Only module-level definitions count. A ``register_*`` closure nested inside
    another function is not something the server could call by name.
    """
    graph = {}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _is_factory(node.name):
                continue
            graph[node.name] = (
                _module_name(path),
                {
                    call.func.id
                    for call in ast.walk(node)
                    if isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and _is_factory(call.func.id)
                },
            )
    return graph


def _factories_called_by_server():
    """Factories the server calls at import time, at module level only.

    Scoped to module level on purpose. A call parked inside a function that
    nobody invokes is exactly the dead wiring this test exists to catch, so
    finding the name anywhere in the file is not good enough.

    Assignments count as well as bare calls. ``register_linkedin_resources``
    returns a notifier the server keeps, so it appears as
    ``notifier = register_linkedin_resources(mcp)``, and a walk that only read
    ``ast.Expr`` would call it unwired.
    """
    tree = ast.parse(SERVER_FILE.read_text(encoding="utf-8"))
    called = set()
    for node in tree.body:
        if isinstance(node, ast.Expr):
            value = node.value
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
        else:
            continue
        if not isinstance(value, ast.Call):
            continue
        func = value.func
        if isinstance(func, ast.Name) and _is_factory(func.id):
            called.add(func.id)
    return called


async def _surface(server: FastMCP) -> set[str]:
    """Every tool, resource, resource template and prompt on one server.

    Kinds are prefixed so a prompt and a tool sharing a name cannot mask each
    other. That matters here: `safety_check` is a prompt and `worker_pause` is a
    tool, and a future pair that collided would otherwise look wired when only
    one of them was.
    """
    names = {f"tool:{tool.name}" for tool in await server.list_tools()}
    names |= {f"resource:{resource.uri}" for resource in await server.list_resources()}
    names |= {
        f"template:{item.uri_template}"
        for item in await server.list_resource_templates()
    }
    names |= {f"prompt:{prompt.name}" for prompt in await server.list_prompts()}
    return names


def test_every_register_factory_is_reachable_from_the_server():
    graph = _factories_defined()
    assert graph, "found no register_* factories, so this test proves nothing"

    reachable = set()
    frontier = list(_factories_called_by_server())
    while frontier:
        name = frontier.pop()
        if name in reachable:
            continue
        reachable.add(name)
        reachable_from_here = graph.get(name)
        if reachable_from_here is not None:
            frontier.extend(reachable_from_here[1])

    stranded = sorted(set(graph) - reachable)
    assert stranded == [], (
        f"these factories are never reached from {SERVER_FILE.name}, so what "
        f"they register does not exist as far as an MCP client is concerned: "
        f"{stranded}"
    )


def test_the_walk_covers_every_kind_of_factory_this_repo_ships():
    """Non-vacuity: all three surfaces are represented in the graph.

    Before #28 `_is_factory` matched `register_*_tools` only, so #27's resource
    factory needed a second near-identical test and a prompt factory would have
    needed a third. If this ever drops back to tools alone, the walk above has
    been narrowed and the other surfaces are unguarded again.
    """
    graph = _factories_defined()

    assert "register_campaign_tools" in graph
    assert "register_worker_tools" in graph
    assert "register_linkedin_resources" in graph
    assert "register_linkedin_prompts" in graph

    beyond_tools = sorted(name for name in graph if not name.endswith("_tools"))
    assert beyond_tools, "the walk found only *_tools factories, so it narrowed"

    called = _factories_called_by_server()
    assert "register_linkedin_resources" in called, (
        "the resource factory is called as an assignment; a walk that only read "
        "bare expressions would report it unwired"
    )


def test_the_reachability_walk_would_catch_a_stranded_factory():
    """The walk is not vacuous: an unreferenced factory is not reachable."""
    graph = _factories_defined()
    called = _factories_called_by_server()

    reachable = set()
    frontier = list(called)
    while frontier:
        name = frontier.pop()
        if name in reachable:
            continue
        reachable.add(name)
        if (entry := graph.get(name)) is not None:
            frontier.extend(entry[1])

    assert "register_never_written_anywhere" not in reachable
    assert "register_linkedin_prompts" in reachable
    assert set(graph) <= reachable


@pytest.mark.asyncio
async def test_everything_a_factory_registers_is_on_the_shipped_server():
    """The property the AST test approximates, asserted against real behaviour.

    Reachability proves the call is written. This proves the registrations
    arrive: each factory registers against a throwaway server and every name it
    produces, of every kind, must also be on the shipped one.

    Factories are imported from the module that defines them rather than read
    off the server, so a package wired in some new way is still covered, and no
    name is hardcoded, so a package adding a thirteenth tool or a seventh prompt
    is covered the day it is written.
    """
    shipped = await _surface(linkedin_browser_mcp.mcp)

    missing = {}
    for name, (module_name, _) in sorted(_factories_defined().items()):
        factory = getattr(importlib.import_module(module_name), name)
        probe = FastMCP("probe")
        factory(probe)
        absent = sorted(await _surface(probe) - shipped)
        if absent:
            missing[name] = absent

    assert missing == {}, (
        "these are registered by a factory but absent from the shipped server, "
        f"so no MCP client can reach them: {missing}"
    )


@pytest.mark.asyncio
async def test_the_shipped_server_serves_all_three_surfaces():
    """Counts, so a regression that unregistered a whole surface is loud.

    A lower bound for tools, which grow issue by issue, and exact numbers for
    resources and prompts, which #27 and #28 each define as a closed set.
    """
    served = await _surface(linkedin_browser_mcp.mcp)

    assert len({name for name in served if name.startswith("tool:")}) >= 42
    assert len({name for name in served if name.startswith("resource:")}) == 9
    assert len({name for name in served if name.startswith("template:")}) == 3
    assert len({name for name in served if name.startswith("prompt:")}) == 6
    assert "tool:worker_pause" in served
    assert "tool:worker_resume" in served
    assert "prompt:new_campaign" in served
