"""Import and tool-registration tests for the server module.

server.py is imported by the console entry point, so an import-time failure
takes the whole server down before it serves a request -- and nothing else in
the suite imports it. Issue #37 was exactly that: an SDK major bump moved the
server class, and the crash only surfaced at startup.
"""

from __future__ import annotations

import asyncio
import threading

EXPECTED_TOOLS = {
    "add_custom_food",
    "add_food_entry",
    "add_recipe",
    "copy_day",
    "delete_custom_food",
    "get_biometrics",
    "get_daily_nutrition",
    "get_fasting_history",
    "get_fasting_stats",
    "get_food_details",
    "get_food_log",
    "get_macro_targets",
    "get_nutrition_scores",
    "import_recipe",
    "list_biometrics",
    "mark_day_complete",
    "remove_food_entry",
    "search_foods",
    "update_custom_food",
}


def test_server_module_imports():
    """The entry point does `from cronometer_api_mcp.server import main`."""
    from cronometer_api_mcp import server

    assert callable(server.main)


def test_server_identity():
    from cronometer_api_mcp.server import mcp

    assert mcp.name == "cronometer"


def test_server_is_mcpserver_instance():
    """Pins the SDK 2.x server class: 1.x's FastMCP no longer exists."""
    from mcp.server.mcpserver import MCPServer

    from cronometer_api_mcp.server import mcp

    assert isinstance(mcp, MCPServer)


def test_server_reports_a_version():
    """serverInfo.version must not be empty.

    SDK 2.x reports "" when `version=` is omitted, so the mistake is invisible
    until a client displays it.
    """
    from cronometer_api_mcp.server import _server_version

    assert _server_version()


def test_registered_tools():
    """Tools register as an import side effect of the @mcp.tool() decorators.

    Descriptions come from the docstrings and are what the model reads to pick
    a tool, so an empty one is a silent regression.
    """
    from cronometer_api_mcp.server import mcp

    tools = asyncio.run(mcp.list_tools())

    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert tool.description, f"{tool.name} has no description"


def test_tool_annotations_survive_schema_coercion():
    """The read-only hints must reach the wire model.

    They are declared in camelCase (`readOnlyHint`), which SDK 2.x accepts only
    as an input alias for the snake_case attributes. A silent coercion failure
    would strip the hints clients use to decide what is safe to call.
    """
    from cronometer_api_mcp.server import mcp

    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}

    assert tools["get_food_log"].annotations.read_only_hint is True
    assert tools["add_food_entry"].annotations.read_only_hint is False
    assert tools["delete_custom_food"].annotations.destructive_hint is True


def test_no_client_constructed_at_import():
    """Import must not need credentials or touch the network.

    The server is spawned fresh for every stdio session; the client is built
    lazily on the first tool call instead.
    """
    from cronometer_api_mcp import server

    assert server._client is None


def test_concurrent_get_client_builds_one_instance(monkeypatch):
    """SDK 2.x runs sync tools on worker threads, so _get_client() races.

    Two clients would mean two logins against a rate-limited endpoint (#3) and
    two writers for one session file.
    """
    from cronometer_api_mcp import server

    monkeypatch.setattr(server, "_client", None)

    built = []
    threads_n = 8
    start = threading.Barrier(threads_n)

    class FakeClient:
        def __init__(self) -> None:
            built.append(self)

    monkeypatch.setattr(server, "CronometerClient", FakeClient)

    seen = []

    def worker() -> None:
        start.wait()
        seen.append(server._get_client())

    threads = [threading.Thread(target=worker) for _ in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(built) == 1
    assert {id(c) for c in seen} == {id(built[0])}
