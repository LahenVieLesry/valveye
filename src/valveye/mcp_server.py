"""Valveye MCP Server — exposes game tools to external MCP clients.

Usage:
    python -m valveye.mcp_server

Or with the entry point:
    valveye-mcp
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any

from mcp.server import Server
from mcp.server.stdio import run_server
from mcp.types import TextContent, Tool

from valveye.cli import build_services
from valveye.schemas import ToolError

app = Server("valveye")

# Lazy-initialized service references
_services: dict[str, Any] = {}


def _get_services() -> dict[str, Any]:
    if not _services:
        repo, price_service, recommender, _scheduler, tools, game_data, _notifier, _steam_library, _audit_logger, _user_profile = build_services()
        all_tools, tool_groups = tools
        _services["repo"] = repo
        _services["price_service"] = price_service
        _services["recommender"] = recommender
        _services["game_data"] = game_data
        _services["all_tools"] = all_tools
        _services["tool_groups"] = tool_groups
    return _services


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="query_game_price",
            description=(
                "Query current and historical low price for a Steam game."
                " Returns price, currency, and whether it's at historical low."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "game": {"type": "string", "description": "Game name (Steam official English name)"},
                    "window": {
                        "type": "string", "enum": ["all", "12m", "3m"],
                        "default": "all", "description": "Time window for historical low",
                    },
                },
                "required": ["game"],
            },
        ),
        Tool(
            name="compare_regional_prices",
            description="Compare game prices across all 23 Steam regions with automatic currency conversion.",
            inputSchema={
                "type": "object",
                "properties": {
                    "game": {"type": "string", "description": "Game name (Steam official English name)"},
                    "target_currency": {
                        "type": "string", "default": "",
                        "description": "Target currency (auto-detected if empty)",
                    },
                },
                "required": ["game"],
            },
        ),
        Tool(
            name="recommend_similar_games",
            description=(
                "Find games similar to a given game using multi-signal analysis"
                " (tags, Steam recommendations, review quality)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "game": {"type": "string", "description": "Game name (Steam official English name)"},
                    "top_n": {"type": "integer", "default": 5, "description": "Number of recommendations"},
                },
                "required": ["game"],
            },
        ),
        Tool(
            name="get_game_info",
            description=(
                "Get detailed game information including description, tags,"
                " developer, release date, and review statistics."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "game": {"type": "string", "description": "Game name (Steam official English name)"},
                },
                "required": ["game"],
            },
        ),
        Tool(
            name="get_game_reviews",
            description="Get player review snippets for a game (positive or negative).",
            inputSchema={
                "type": "object",
                "properties": {
                    "game": {"type": "string", "description": "Game name (Steam official English name)"},
                    "review_type": {"type": "string", "enum": ["positive", "negative"], "default": "negative"},
                    "count": {"type": "integer", "default": 3, "description": "Number of review snippets"},
                },
                "required": ["game"],
            },
        ),
        Tool(
            name="get_player_library",
            description=(
                "Get a player's Steam game library (owned games)."
                " Returns game count, names, app IDs, and playtime."
                " Requires STEAM_API_KEY and STEAM_ID to be configured."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "steam_id": {
                        "type": "string",
                        "default": "",
                        "description": "Steam ID (uses configured default if empty)",
                    },
                    "include_playtime": {
                        "type": "boolean",
                        "default": True,
                        "description": "Include playtime data",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="get_trending_games",
            description=(
                "Get trending / featured Steam games."
                " Categories: top_sellers (bestsellers), new_releases (recently launched),"
                " specials (current deals), coming_soon (upcoming)."
                " Primary source: Steam Store API; falls back to SteamSpy if unavailable."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["top_sellers", "new_releases", "specials", "coming_soon"],
                        "default": "top_sellers",
                        "description": "Category of trending games",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "description": "Max number of games to return",
                    },
                    "cc": {
                        "type": "string",
                        "default": "cn",
                        "description": "Country code for pricing (e.g. cn, us, jp)",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="search_similar_candidates",
            description=(
                "Search for games similar to a given game (lightweight candidate list)."
                " Returns titles, tags, negative ratios, and source signals."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "game": {"type": "string", "description": "Game name (Steam official English name)"},
                    "top_n": {"type": "integer", "default": 15, "description": "Number of candidates"},
                },
                "required": ["game"],
            },
        ),
        Tool(
            name="search_by_description",
            description=(
                "Search games by natural language description."
                " Example: 'pixel farming game with co-op'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "Natural language description"},
                    "top_n": {"type": "integer", "default": 10, "description": "Number of results"},
                },
                "required": ["description"],
            },
        ),
        Tool(
            name="list_subscriptions",
            description="List all active price-alert subscriptions.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="subscribe_game",
            description=(
                "Subscribe to price alerts for a game."
                " Requires channels_json as a JSON array of notification channels."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "User identifier"},
                    "game": {"type": "string", "description": "Game name (Steam official English name)"},
                    "channels_json": {"type": "string", "description": 'JSON array like [{"type":"email","to":"user@example.com"}]'},
                    "window": {"type": "string", "enum": ["all", "12m", "3m"], "default": "all"},
                    "region": {"type": "string", "default": "", "description": "Region code (auto-detected if empty)"},
                    "currency": {"type": "string", "default": "", "description": "Currency code (auto-detected if empty)"},
                },
                "required": ["user_id", "game", "channels_json"],
            },
        ),
        Tool(
            name="get_game_details_batch",
            description="Get detailed info for multiple games (batch version).",
            inputSchema={
                "type": "object",
                "properties": {
                    "games": {"type": "string", "description": "Comma-separated English game names"},
                },
                "required": ["games"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    svc = _get_services()
    tool_map = {t.name: t for t in svc["all_tools"]}

    # Map MCP tool names to LangChain tool names
    mcp_to_langchain = {
        "query_game_price": "query_low_price",
        "compare_regional_prices": "compare_prices",
        "recommend_similar_games": "recommend_similar_games",
        "get_game_info": "get_game_details",
        "get_game_reviews": "get_game_reviews",
        "get_player_library": "get_player_library",
        "get_trending_games": "get_trending_games",
        "search_similar_candidates": "search_similar_candidates",
        "search_by_description": "search_by_description",
        "list_subscriptions": "list_subscriptions",
        "subscribe_game": "subscribe_game",
        "get_game_details_batch": "request_game_details",
    }

    lc_name = mcp_to_langchain.get(name)
    if not lc_name or lc_name not in tool_map:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    lc_tool = tool_map[lc_name]

    # Build kwargs from MCP arguments
    kwargs = dict(arguments)
    # Add default user_query for tools that need it
    if lc_name in ("query_low_price", "compare_prices"):
        kwargs.setdefault("user_query", kwargs.get("game", ""))

    try:
        if inspect.iscoroutinefunction(lc_tool.func):
            result = await lc_tool.func(**kwargs)
        else:
            result = lc_tool.func(**kwargs)
        return [TextContent(type="text", text=str(result))]
    except ToolError as e:
        return [TextContent(type="text", text=str(e))]
    except Exception as e:
        return [TextContent(type="text", text=f"❌ 未知错误: {e}")]


async def main() -> None:
    """Run the MCP server on stdio."""
    # Pre-initialize services
    _get_services()
    async with run_server(app) as server:
        await server.run()


if __name__ == "__main__":
    asyncio.run(main())
