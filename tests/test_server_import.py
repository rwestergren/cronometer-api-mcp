"""Import and tool-registration tests for the server module.

server.py is imported by the console entry point, so an import-time failure
takes the whole server down before it serves a request -- and nothing else in
the suite imports it.
"""

from __future__ import annotations

import asyncio

EXPECTED_TOOLS = {
    "add_custom_food",
    "add_food_entry",
    "copy_day",
    "get_biometrics",
    "get_daily_nutrition",
    "get_fasting_history",
    "get_fasting_stats",
    "get_food_details",
    "get_food_log",
    "get_macro_targets",
    "get_nutrition_scores",
    "list_biometrics",
    "mark_day_complete",
    "remove_food_entry",
    "search_foods",
}


def test_server_module_imports():
    """The entry point does `from cronometer_api_mcp.server import main`."""
    from cronometer_api_mcp import server

    assert callable(server.main)


def test_server_identity():
    from cronometer_api_mcp.server import mcp

    assert mcp.name == "cronometer"


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


def test_no_client_constructed_at_import():
    """Import must not need credentials or touch the network.

    The server is spawned fresh for every stdio session; the client is built
    lazily on the first tool call instead.
    """
    from cronometer_api_mcp import server

    assert server._client is None
