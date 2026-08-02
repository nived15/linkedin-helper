"""Every tool factory in ``linkedin_mcp`` must reach the shipped server.

Tools are registered by ``register_*_tools(mcp)`` factories that each package
ships and ``linkedin_browser_mcp`` is expected to call. Nothing enforced the
second half of that sentence, and twice it did not happen: MCP-01's twelve
campaign tools and SEQ-05's three draft tools were written, tested, reviewed
and merged while remaining invisible to every real MCP client.

The failure is quiet by construction. A factory's own suite registers against a
``FastMCP`` instance the test creates, so it stays green whether or not the
real server ever calls the factory. Both halves pass; only the wiring between
them is missing, and nothing was looking at the wiring.
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


def _is_factory(name):
    return name.startswith("register_") and name.endswith("_tools")


def _module_name(path):
    return ".".join(path.relative_to(REPO_ROOT).with_suffix("").parts)


def _factories_defined():
    """Map every ``register_*_tools`` factory to its module and its own calls.

    A factory may register another rather than the server doing it directly:
    ``register_lead_tools`` calls ``register_crm_tools`` and
    ``register_harvest_tools``. Those are wired, just indirectly, so this
    builds a call graph rather than demanding the server name all of them.
    """
    graph = {}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
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
    """
    tree = ast.parse(SERVER_FILE.read_text(encoding="utf-8"))
    called = set()
    for node in tree.body:
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        if isinstance(func, ast.Name) and _is_factory(func.id):
            called.add(func.id)
    return called


def test_every_register_factory_is_reachable_from_the_server():
    graph = _factories_defined()
    assert graph, "found no register_*_tools factories, so this test proves nothing"

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
        f"these factories are never reached from {SERVER_FILE.name}, so the tools "
        f"they register do not exist as far as an MCP client is concerned: {stranded}"
    )


@pytest.mark.asyncio
async def test_every_tool_a_factory_registers_is_on_the_shipped_server():
    """The property the AST test approximates, asserted against real behaviour.

    Reachability proves the call is written. This proves the tools arrive:
    each factory registers against a throwaway server and every name it
    produces must also be on the shipped one. Factories are imported from the
    module that defines them rather than read off the server, so a package
    wired in some new way is still covered, and no tool name is hardcoded, so
    a package adding a thirteenth tool is covered the day it is written.
    """
    shipped = {tool.name for tool in await linkedin_browser_mcp.mcp.list_tools()}

    missing = {}
    for name, (module_name, _) in sorted(_factories_defined().items()):
        factory = getattr(importlib.import_module(module_name), name)
        probe = FastMCP("probe")
        factory(probe)
        absent = sorted({tool.name for tool in await probe.list_tools()} - shipped)
        if absent:
            missing[name] = absent

    assert missing == {}, (
        "these tools are registered by a factory but absent from the shipped "
        f"server, so no MCP client can call them: {missing}"
    )
