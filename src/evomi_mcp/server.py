"""MCP Server for the Evomi scraping and proxy APIs."""

import argparse
import json
import sys
from typing import Any

import jsonschema
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

from . import __version__
from .annotations import (
    CREATE,
    CREATE_OPEN_WORLD,
    DELETE,
    FETCH,
    LOOKUP,
    OPEN_AGENT,
    TOGGLE,
    UPDATE,
    UPDATE_OPEN_WORLD,
)
from .client import EvomiClient
from .proxy_tools import PROXY_TOOL_NAMES, handle_proxy_tool, proxy_tool_definitions
from .security import scrub

# mcp 1.x registers request handlers through the `@server.list_tools()` /
# `@server.call_tool()` decorators; 2.x replaced those with `on_*` constructor
# kwargs dispatched by method string. Both are supported: the tool table and
# the dispatch below are shared, and only the registration at the bottom of
# this module differs between them.
_MCP_2X = hasattr(Server, "add_request_handler")

# Global client instance (initialized on first use)
_client: EvomiClient | None = None


class ToolError(Exception):
    """A tool call that failed, carrying the message to report to the client.

    Reported as a `CallToolResult` with `isError` set rather than as a JSON-RPC
    error: the MCP spec treats a failure inside a tool as something the model
    should see and be able to correct, not as a transport fault. The message is
    used verbatim, so the raising site owns its wording.
    """


def get_client() -> EvomiClient:
    """Get or create the Evomi API client."""
    global _client
    if _client is None:
        _client = EvomiClient()
    return _client


def format_result(result: dict[str, Any] | list[Any]) -> str:
    """Format API result for MCP response."""
    if isinstance(result, list):
        return json.dumps(result, indent=2, default=str)
    
    # Remove internal headers before formatting
    clean_result = {k: v for k, v in result.items() if not k.startswith("_")}
    
    # Add credit info as top-level if available
    if result.get("_credits_used"):
        clean_result["credits_used"] = result["_credits_used"]
    if result.get("_credits_remaining"):
        clean_result["credits_remaining"] = result["_credits_remaining"]
    if result.get("_mode_used"):
        clean_result["mode_used"] = result["_mode_used"]
    
    return json.dumps(clean_result, indent=2, default=str)


async def list_tools() -> list[Tool]:
    """List available MCP tools."""
    return [
        # ─── Scraping Operations ────────────────────────────────────────────────
        Tool(
            name="scrape_url",
            description="Scrape a single URL. Modes: request (fast), browser (JS), auto (detect). Output: html, markdown, screenshot, pdf. Use ai_enhance with ai_prompt for AI extraction.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to scrape"},
                    "mode": {"type": "string", "enum": ["request", "browser", "auto"], "default": "auto"},
                    "output": {"type": "string", "enum": ["html", "markdown", "screenshot", "pdf"], "default": "markdown"},
                    "device": {"type": "string", "enum": ["windows", "macos", "android"], "default": "windows", "description": "Device type to emulate"},
                    "proxy_type": {"type": "string", "enum": ["datacenter", "residential"], "default": "residential"},
                    "proxy_country": {"type": "string", "default": "US", "description": "Two-letter country code for proxy"},
                    "proxy_session_id": {"type": "string", "description": "Proxy session ID (6-8 characters)"},
                    "wait_until": {"type": "string", "enum": ["load", "domcontentloaded", "networkidle", "commit"], "default": "domcontentloaded"},
                    "ai_enhance": {"type": "boolean", "default": False},
                    "ai_prompt": {"type": "string", "description": "Prompt for AI extraction"},
                    "ai_source": {"type": "string", "enum": ["markdown", "screenshot"], "description": "Source for AI processing"},
                    "ai_force_json": {"type": "boolean", "default": True, "description": "Force AI response to be valid JSON"},
                    "js_instructions": {"type": "array", "items": {"type": "object"}, "description": "JS actions: click, wait, fill, wait_for"},
                    "execute_js": {"type": "string", "description": "Raw JavaScript code to execute"},
                    "screenshot": {"type": "boolean", "default": False},
                    "pdf": {"type": "boolean", "default": False},
                    "wait_seconds": {"type": "integer", "default": 0, "description": "Seconds to wait after page load"},
                    "excluded_tags": {"type": "array", "items": {"type": "string"}, "description": "HTML tags to remove"},
                    "excluded_selectors": {"type": "array", "items": {"type": "string"}, "description": "CSS selectors to remove"},
                    "block_resources": {"type": "array", "items": {"type": "string"}, "description": "Resource types to block"},
                    "additional_headers": {"type": "object", "description": "Extra HTTP headers"},
                    "capture_headers": {"type": "boolean", "default": False, "description": "Capture response headers"},
                    "network_capture": {"type": "array", "items": {"type": "object"}, "description": "Network capture filters"},
                    "async_mode": {"type": "boolean", "default": False},
                    "config_id": {"type": "string", "description": "Saved config ID to use"},
                    "scheme_id": {"type": "string", "description": "Saved extraction schema ID"},
                    "extract_scheme": {"type": "array", "items": {"type": "object"}, "description": "Inline extraction schema"},
                    "storage_id": {"type": "string", "description": "Storage config ID for results"},
                    "use_default_storage": {"type": "boolean", "default": False},
                    "no_html": {"type": "boolean", "default": False, "description": "Exclude HTML from JSON response"},
                },
                "required": ["url"],
            },
            annotations=FETCH,
        ),
        Tool(
            name="crawl_website",
            description="Crawl a website to discover and scrape multiple pages. Follows links up to specified depth. The scraper_config can include a config_id to use saved settings, or full scraping options.",
            inputSchema={
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "Domain to crawl"},
                    "max_urls": {"type": "integer", "default": 100},
                    "depth": {"type": "integer", "default": 2},
                    "url_pattern": {"type": "string", "description": "Regex pattern to filter URLs"},
                    "scraper_config": {"type": "object", "description": "Scraping config for each page. Can include 'config_id' to use a saved config, or full options like mode, output, extract_scheme, etc."},
                    "async_mode": {"type": "boolean", "default": False},
                },
                "required": ["domain"],
            },
            annotations=FETCH,
        ),
        Tool(
            name="map_website",
            description="Discover URLs from a website using sitemaps, CommonCrawl, or in-site crawling.",
            inputSchema={
                "type": "object",
                "properties": {
                    "domain": {"type": "string"},
                    "sources": {"type": "array", "items": {"type": "string", "enum": ["sitemap", "commoncrawl", "crawl"]}, "default": ["sitemap", "commoncrawl"]},
                    "max_urls": {"type": "integer", "default": 500},
                    "url_pattern": {"type": "string"},
                    "check_if_live": {"type": "boolean", "default": False},
                    "async_mode": {"type": "boolean", "default": False},
                },
                "required": ["domain"],
            },
            annotations=FETCH,
        ),
        Tool(
            name="search_domains",
            description="Find domains by searching the web. Use this to discover websites when you don't know specific domains. Returns list of domains with URLs and titles.",
            inputSchema={
                "type": "object",
                "properties": {
                    # Arguments are validated against this schema before the
                    # handler is reached, so both shapes the description
                    # promises have to be declared here.
                    "query": {
                        "type": ["string", "array"],
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 10,
                        "description": "Search query, or an array of up to 10 queries to run together",
                    },
                    "max_urls": {"type": "integer", "default": 20, "description": "Max domains per query (max: 100)"},
                    "region": {"type": "string", "default": "us-en", "description": "Region for results and proxy (e.g., 'us-en', 'de-de')"},
                },
                "required": ["query"],
            },
            annotations=FETCH,
        ),
        Tool(
            name="agent_request",
            description="AI-powered conversational assistant that can do ANY Evomi operation. Ask it to: create configs, run scrapes, discover URLs, schedule jobs, check account info, and more. Examples: 'Scrape example.com and figure out what to extract', 'Create a config for Amazon products', 'Schedule my config to run daily', 'How many credits do I have?'",
            inputSchema={
                "type": "object",
                "properties": {"message": {"type": "string", "description": "Natural language request for any Evomi operation"}},
                "required": ["message"],
            },
            annotations=OPEN_AGENT,
        ),
        Tool(
            name="get_task_status",
            description="Check the status of an async task (scrape, crawl, or map).",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "task_type": {"type": "string", "enum": ["scrape", "crawl", "map", "config_generate", "schema"], "default": "scrape"},
                },
                "required": ["task_id"],
            },
            annotations=LOOKUP,
        ),
        Tool(
            name="get_account_info",
            description="Get account information including credit balance and user details.",
            inputSchema={"type": "object", "properties": {}},
            annotations=LOOKUP,
        ),
        
        # ─── Config Management ──────────────────────────────────────────────────
        Tool(
            name="list_configs",
            description="List all saved scrape configurations.",
            inputSchema={
                "type": "object",
                "properties": {
                    "page": {"type": "integer", "default": 1},
                    "per_page": {"type": "integer", "default": 20},
                    "sort_by": {"type": "string", "enum": ["created_at", "updated_at", "name"], "default": "created_at"},
                    "sort_order": {"type": "string", "enum": ["asc", "desc"], "default": "desc"},
                },
            },
            annotations=LOOKUP,
        ),
        Tool(
            name="create_config",
            description="Create a new scrape configuration for reuse.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "config": {"type": "object", "description": "Scraping configuration"},
                },
                "required": ["name", "config"],
            },
            annotations=CREATE,
        ),
        Tool(
            name="get_config",
            description="Get a saved scrape configuration by ID.",
            inputSchema={"type": "object", "properties": {"config_id": {"type": "string"}}, "required": ["config_id"]},
            annotations=LOOKUP,
        ),
        Tool(
            name="update_config",
            description="Update an existing scrape configuration.",
            inputSchema={
                "type": "object",
                "properties": {
                    "config_id": {"type": "string"},
                    "name": {"type": "string"},
                    "config": {"type": "object"},
                },
                "required": ["config_id"],
            },
            annotations=UPDATE,
        ),
        Tool(
            name="delete_config",
            description="Delete a scrape configuration.",
            inputSchema={"type": "object", "properties": {"config_id": {"type": "string"}}, "required": ["config_id"]},
            annotations=DELETE,
        ),
        Tool(
            name="generate_config",
            description="Generate a scrape config from a natural language prompt using AI.",
            inputSchema={
                "type": "object",
                "properties": {"name": {"type": "string"}, "prompt": {"type": "string", "description": "Describe what to scrape"}},
                "required": ["name", "prompt"],
            },
            annotations=CREATE_OPEN_WORLD,
        ),
        
        # ─── Schema Management ───────────────────────────────────────────────────
        Tool(
            name="list_schemas",
            description="List all saved extraction schemas.",
            inputSchema={
                "type": "object",
                "properties": {
                    "page": {"type": "integer", "default": 1},
                    "per_page": {"type": "integer", "default": 20},
                    "sort_by": {"type": "string", "default": "created_at"},
                    "sort_order": {"type": "string", "default": "desc"},
                },
            },
            annotations=LOOKUP,
        ),
        Tool(
            name="create_schema",
            description="Create a new extraction schema for structured data extraction.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "config": {"type": "object", "description": "Schema config with url and extract_scheme"},
                    "test": {"type": "boolean", "default": False},
                    "fix": {"type": "boolean", "default": False},
                    "extract_prompt": {"type": "string"},
                },
                "required": ["name", "config"],
            },
            annotations=CREATE_OPEN_WORLD,
        ),
        Tool(
            name="get_schema",
            description="Get a saved extraction schema by ID.",
            inputSchema={"type": "object", "properties": {"scheme_id": {"type": "string"}}, "required": ["scheme_id"]},
            annotations=LOOKUP,
        ),
        Tool(
            name="update_schema",
            description="Update an existing extraction schema.",
            inputSchema={
                "type": "object",
                "properties": {
                    "scheme_id": {"type": "string"},
                    "name": {"type": "string"},
                    "config": {"type": "object"},
                    "test": {"type": "boolean", "default": False},
                    "fix": {"type": "boolean", "default": False},
                    "extract_prompt": {"type": "string"},
                },
                "required": ["scheme_id", "name", "config"],
            },
            annotations=UPDATE_OPEN_WORLD,
        ),
        Tool(
            name="delete_schema",
            description="Delete an extraction schema.",
            inputSchema={"type": "object", "properties": {"scheme_id": {"type": "string"}}, "required": ["scheme_id"]},
            annotations=DELETE,
        ),
        Tool(
            name="get_schema_status",
            description="Get the test status of an extraction schema.",
            inputSchema={"type": "object", "properties": {"scheme_id": {"type": "string"}}, "required": ["scheme_id"]},
            annotations=LOOKUP,
        ),
        
        # ─── Storage Management ──────────────────────────────────────────────────
        Tool(
            name="list_storage_configs",
            description="List all storage configurations (S3, GCS, Azure Blob).",
            inputSchema={"type": "object", "properties": {}},
            annotations=LOOKUP,
        ),
        Tool(
            name="create_storage_config",
            description="Create a new storage configuration for saving scraped data.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "storage_type": {"type": "string", "enum": ["s3_compatible", "gcs", "azure_blob"]},
                    "config": {"type": "object", "description": "Storage credentials"},
                    "set_as_default": {"type": "boolean", "default": False},
                },
                "required": ["name", "storage_type", "config"],
            },
            annotations=CREATE,
        ),
        Tool(
            name="update_storage_config",
            description="Update an existing storage configuration.",
            inputSchema={
                "type": "object",
                "properties": {
                    "storage_id": {"type": "string"},
                    "name": {"type": "string"},
                    "config": {"type": "object"},
                    "set_as_default": {"type": "boolean"},
                },
                "required": ["storage_id"],
            },
            annotations=UPDATE,
        ),
        Tool(
            name="delete_storage_config",
            description="Delete a storage configuration.",
            inputSchema={"type": "object", "properties": {"storage_id": {"type": "string"}}, "required": ["storage_id"]},
            annotations=DELETE,
        ),
        
        # ─── Schedule Management ─────────────────────────────────────────────────
        Tool(
            name="list_schedules",
            description="List all scheduled scrape jobs.",
            inputSchema={
                "type": "object",
                "properties": {
                    "page": {"type": "integer", "default": 1},
                    "per_page": {"type": "integer", "default": 20},
                    "active_only": {"type": "boolean", "default": False},
                },
            },
            annotations=LOOKUP,
        ),
        Tool(
            name="create_schedule",
            description="Create a new scheduled scrape job that runs at regular intervals.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "config_id": {"type": "string", "description": "Config ID to run"},
                    "interval_minutes": {"type": "integer", "description": "Run interval (min: 5)"},
                    "start_time": {"type": "string", "description": "Start time (HH:MM)"},
                    "stop_on_error": {"type": "boolean", "default": True},
                },
                "required": ["name", "config_id", "interval_minutes"],
            },
            annotations=CREATE,
        ),
        Tool(
            name="get_schedule",
            description="Get a scheduled job by ID.",
            inputSchema={"type": "object", "properties": {"schedule_id": {"type": "string"}}, "required": ["schedule_id"]},
            annotations=LOOKUP,
        ),
        Tool(
            name="update_schedule",
            description="Update an existing scheduled job.",
            inputSchema={
                "type": "object",
                "properties": {
                    "schedule_id": {"type": "string"},
                    "name": {"type": "string"},
                    "config_id": {"type": "string"},
                    "interval_minutes": {"type": "integer"},
                    "stop_on_error": {"type": "boolean"},
                },
                "required": ["schedule_id"],
            },
            annotations=UPDATE,
        ),
        Tool(
            name="delete_schedule",
            description="Delete a scheduled job.",
            inputSchema={"type": "object", "properties": {"schedule_id": {"type": "string"}}, "required": ["schedule_id"]},
            annotations=DELETE,
        ),
        Tool(
            name="toggle_schedule",
            description="Toggle a scheduled job active/inactive.",
            inputSchema={"type": "object", "properties": {"schedule_id": {"type": "string"}}, "required": ["schedule_id"]},
            annotations=TOGGLE,
        ),
        Tool(
            name="list_schedule_runs",
            description="Get execution history for a scheduled job.",
            inputSchema={
                "type": "object",
                "properties": {
                    "schedule_id": {"type": "string"},
                    "page": {"type": "integer", "default": 1},
                    "per_page": {"type": "integer", "default": 20},
                },
                "required": ["schedule_id"],
            },
            annotations=LOOKUP,
        ),
        # ─── Proxy & Account (Evomi Public API) ─────────────────────────────────
        *proxy_tool_definitions(),
    ]


async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Handle tool calls."""
    # Dispatched before the scraper client is built: the proxy tools use a
    # different credential, so needing only that one must be enough.
    if name in PROXY_TOOL_NAMES:
        try:
            return [TextContent(type="text", text=await handle_proxy_tool(name, arguments))]
        except Exception as e:
            # Raised rather than returned as content, because raising is what
            # sets `isError`. Scrubbed, because an API error can quote the
            # request that carried the key.
            raise ToolError(f"Error: {scrub(str(e))}") from e
    
    client = get_client()
    
    try:
        # ─── Scraping Operations ────────────────────────────────────────────────
        if name == "scrape_url":
            result = await client.scrape(
                url=arguments["url"],
                mode=arguments.get("mode", "auto"),
                output=arguments.get("output", "markdown"),
                device=arguments.get("device", "windows"),
                proxy_type=arguments.get("proxy_type", "residential"),
                proxy_country=arguments.get("proxy_country", "US"),
                proxy_session_id=arguments.get("proxy_session_id"),
                wait_until=arguments.get("wait_until", "domcontentloaded"),
                ai_enhance=arguments.get("ai_enhance", False),
                ai_prompt=arguments.get("ai_prompt"),
                ai_source=arguments.get("ai_source"),
                ai_force_json=arguments.get("ai_force_json", True),
                js_instructions=arguments.get("js_instructions"),
                execute_js=arguments.get("execute_js"),
                screenshot=arguments.get("screenshot", False),
                pdf=arguments.get("pdf", False),
                wait_seconds=arguments.get("wait_seconds", 0),
                excluded_tags=arguments.get("excluded_tags"),
                excluded_selectors=arguments.get("excluded_selectors"),
                block_resources=arguments.get("block_resources"),
                additional_headers=arguments.get("additional_headers"),
                capture_headers=arguments.get("capture_headers", False),
                network_capture=arguments.get("network_capture"),
                async_mode=arguments.get("async_mode", False),
                config_id=arguments.get("config_id"),
                scheme_id=arguments.get("scheme_id"),
                extract_scheme=arguments.get("extract_scheme"),
                storage_id=arguments.get("storage_id"),
                use_default_storage=arguments.get("use_default_storage", False),
                no_html=arguments.get("no_html", False),
            )
            
            if arguments.get("async_mode") and result.get("task_id"):
                return [TextContent(type="text", text=f"Task submitted. ID: {result['task_id']}\nUse get_task_status to check results.")]
            
            return [TextContent(type="text", text=format_result(result))]
        
        elif name == "crawl_website":
            result = await client.crawl(
                domain=arguments["domain"],
                max_urls=arguments.get("max_urls", 100),
                depth=arguments.get("depth", 2),
                url_pattern=arguments.get("url_pattern"),
                scraper_config=arguments.get("scraper_config"),
                async_mode=arguments.get("async_mode", False),
            )
            
            if arguments.get("async_mode") and result.get("task_id"):
                return [TextContent(type="text", text=f"Crawl task submitted. ID: {result['task_id']}\nUse get_task_status with task_type='crawl'.")]
            
            return [TextContent(type="text", text=format_result(result))]
        
        elif name == "map_website":
            result = await client.map_website(
                domain=arguments["domain"],
                sources=arguments.get("sources"),
                max_urls=arguments.get("max_urls", 500),
                url_pattern=arguments.get("url_pattern"),
                check_if_live=arguments.get("check_if_live", False),
                async_mode=arguments.get("async_mode", False),
            )
            
            if arguments.get("async_mode") and result.get("task_id"):
                return [TextContent(type="text", text=f"Map task submitted. ID: {result['task_id']}\nUse get_task_status with task_type='map'.")]
            
            return [TextContent(type="text", text=format_result(result))]
        
        elif name == "search_domains":
            result = await client.search_domains(
                query=arguments["query"],
                max_urls=arguments.get("max_urls", 20),
                region=arguments.get("region", "us-en"),
            )
            return [TextContent(type="text", text=format_result(result))]
        
        elif name == "agent_request":
            result = await client.agent_request(message=arguments["message"])
            return [TextContent(type="text", text=format_result(result))]
        
        elif name == "get_task_status":
            result = await client.get_task_status(
                task_id=arguments["task_id"],
                task_type=arguments.get("task_type", "scrape"),
            )
            return [TextContent(type="text", text=format_result(result))]
        
        elif name == "get_account_info":
            result = await client.get_account_info()
            return [TextContent(type="text", text=format_result(result))]
        
        # ─── Config Management ──────────────────────────────────────────────────
        elif name == "list_configs":
            result = await client.list_configs(
                page=arguments.get("page", 1),
                per_page=arguments.get("per_page", 20),
                sort_by=arguments.get("sort_by", "created_at"),
                sort_order=arguments.get("sort_order", "desc"),
            )
            return [TextContent(type="text", text=format_result(result))]
        
        elif name == "create_config":
            result = await client.create_config(name=arguments["name"], config=arguments["config"])
            return [TextContent(type="text", text=format_result(result))]
        
        elif name == "get_config":
            result = await client.get_config(config_id=arguments["config_id"])
            return [TextContent(type="text", text=format_result(result))]
        
        elif name == "update_config":
            result = await client.update_config(
                config_id=arguments["config_id"],
                name=arguments.get("name"),
                config=arguments.get("config"),
            )
            return [TextContent(type="text", text=format_result(result))]
        
        elif name == "delete_config":
            result = await client.delete_config(config_id=arguments["config_id"])
            return [TextContent(type="text", text=format_result(result))]
        
        elif name == "generate_config":
            result = await client.generate_config(name=arguments["name"], prompt=arguments["prompt"])
            return [TextContent(type="text", text=format_result(result))]
        
        # ─── Schema Management ───────────────────────────────────────────────────
        elif name == "list_schemas":
            result = await client.list_schemas(
                page=arguments.get("page", 1),
                per_page=arguments.get("per_page", 20),
                sort_by=arguments.get("sort_by", "created_at"),
                sort_order=arguments.get("sort_order", "desc"),
            )
            return [TextContent(type="text", text=format_result(result))]
        
        elif name == "create_schema":
            result = await client.create_schema(
                name=arguments["name"],
                config=arguments["config"],
                test=arguments.get("test", False),
                fix=arguments.get("fix", False),
                extract_prompt=arguments.get("extract_prompt"),
            )
            return [TextContent(type="text", text=format_result(result))]
        
        elif name == "get_schema":
            result = await client.get_schema(scheme_id=arguments["scheme_id"])
            return [TextContent(type="text", text=format_result(result))]
        
        elif name == "update_schema":
            result = await client.update_schema(
                scheme_id=arguments["scheme_id"],
                name=arguments["name"],
                config=arguments["config"],
                test=arguments.get("test", False),
                fix=arguments.get("fix", False),
                extract_prompt=arguments.get("extract_prompt"),
            )
            return [TextContent(type="text", text=format_result(result))]
        
        elif name == "delete_schema":
            result = await client.delete_schema(scheme_id=arguments["scheme_id"])
            return [TextContent(type="text", text=format_result(result))]
        
        elif name == "get_schema_status":
            result = await client.get_schema_status(scheme_id=arguments["scheme_id"])
            return [TextContent(type="text", text=format_result(result))]
        
        # ─── Storage Management ──────────────────────────────────────────────────
        elif name == "list_storage_configs":
            result = await client.list_storage_configs()
            return [TextContent(type="text", text=format_result(result))]
        
        elif name == "create_storage_config":
            result = await client.create_storage_config(
                name=arguments["name"],
                storage_type=arguments["storage_type"],
                config=arguments["config"],
                set_as_default=arguments.get("set_as_default", False),
            )
            return [TextContent(type="text", text=format_result(result))]
        
        elif name == "update_storage_config":
            result = await client.update_storage_config(
                storage_id=arguments["storage_id"],
                name=arguments.get("name"),
                config=arguments.get("config"),
                set_as_default=arguments.get("set_as_default"),
            )
            return [TextContent(type="text", text=format_result(result))]
        
        elif name == "delete_storage_config":
            result = await client.delete_storage_config(storage_id=arguments["storage_id"])
            return [TextContent(type="text", text=format_result(result))]
        
        # ─── Schedule Management ─────────────────────────────────────────────────
        elif name == "list_schedules":
            result = await client.list_schedules(
                page=arguments.get("page", 1),
                per_page=arguments.get("per_page", 20),
                active_only=arguments.get("active_only", False),
            )
            return [TextContent(type="text", text=format_result(result))]
        
        elif name == "create_schedule":
            result = await client.create_schedule(
                name=arguments["name"],
                config_id=arguments["config_id"],
                interval_minutes=arguments["interval_minutes"],
                start_time=arguments.get("start_time"),
                stop_on_error=arguments.get("stop_on_error", True),
            )
            return [TextContent(type="text", text=format_result(result))]
        
        elif name == "get_schedule":
            result = await client.get_schedule(schedule_id=arguments["schedule_id"])
            return [TextContent(type="text", text=format_result(result))]
        
        elif name == "update_schedule":
            result = await client.update_schedule(
                schedule_id=arguments["schedule_id"],
                name=arguments.get("name"),
                config_id=arguments.get("config_id"),
                interval_minutes=arguments.get("interval_minutes"),
                stop_on_error=arguments.get("stop_on_error"),
            )
            return [TextContent(type="text", text=format_result(result))]
        
        elif name == "delete_schedule":
            result = await client.delete_schedule(schedule_id=arguments["schedule_id"])
            return [TextContent(type="text", text=format_result(result))]
        
        elif name == "toggle_schedule":
            result = await client.toggle_schedule(schedule_id=arguments["schedule_id"])
            return [TextContent(type="text", text=format_result(result))]
        
        elif name == "list_schedule_runs":
            result = await client.list_schedule_runs(
                schedule_id=arguments["schedule_id"],
                page=arguments.get("page", 1),
                per_page=arguments.get("per_page", 20),
            )
            return [TextContent(type="text", text=format_result(result))]
        
        else:
            raise ToolError(f"Unknown tool: {name}")
    
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"Error: {scrub(str(e))}") from e


# ─── SDK plumbing ───────────────────────────────────────────────────────────
# Everything above is SDK-major agnostic; everything below adapts it to
# whichever `mcp` is installed.

_tool_schemas: dict[str, dict[str, Any]] | None = None


def _input_schema(tool: Tool) -> dict[str, Any]:
    # mcp 2.x renamed the field to `input_schema`, keeping `inputSchema` as the
    # wire alias. Aliases apply to (de)serialization, not attribute access.
    return getattr(tool, "input_schema", None) or tool.inputSchema


def _tool_annotations(tool: Tool) -> dict[str, Any]:
    """The tool's annotations as they go on the wire, or {} if it set none.

    mcp 2.x renames every field on `ToolAnnotations` to snake_case and keeps the
    camelCase spelling as a serialization alias only, so `readOnlyHint` cannot
    be read as an attribute there. Dumping by alias reads the same on both
    majors and is what a client receives.
    """
    if tool.annotations is None:
        return {}
    return tool.annotations.model_dump(by_alias=True, exclude_none=True)


async def _validation_error(name: str, arguments: dict[str, Any]) -> str | None:
    """Report why `arguments` fail the tool's `inputSchema`, if they do.

    mcp 2.x performs no argument validation of its own, so a missing required
    argument would otherwise reach the dispatch as a `KeyError`. Tools with no
    advertised schema are not validated.
    """
    global _tool_schemas
    if _tool_schemas is None:
        _tool_schemas = {tool.name: _input_schema(tool) for tool in await list_tools()}

    schema = _tool_schemas.get(name)
    if schema is None:
        return None
    try:
        jsonschema.validate(instance=arguments, schema=schema)
    except jsonschema.ValidationError as e:
        return f"Input validation error: {e.message}"
    return None


def _errored(message: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=message)], isError=True)


async def tool_result(name: str, arguments: dict[str, Any]) -> CallToolResult:
    """Run one tool call and shape the outcome into a `CallToolResult`.

    Both SDK majors dispatch through here, so error reporting is decided in one
    place: `isError` set, with the raiser's message as the body.
    """
    validation_error = await _validation_error(name, arguments)
    if validation_error is not None:
        return _errored(validation_error)

    try:
        content = await call_tool(name, arguments)
    except Exception as e:
        return _errored(str(e))

    return CallToolResult(content=list(content), isError=False)


if _MCP_2X:
    from mcp.server import ServerRequestContext
    from mcp.types import CallToolRequestParams, ListToolsResult, PaginatedRequestParams

    async def _on_list_tools(
        ctx: ServerRequestContext, params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        return ListToolsResult(tools=await list_tools())

    async def _on_call_tool(ctx: ServerRequestContext, params: CallToolRequestParams) -> CallToolResult:
        # 2.x hands the params model through; `arguments` is optional on the
        # wire, where 1.x's decorator defaulted it to {}.
        return await tool_result(params.name, params.arguments or {})

    server = Server(
        "evomi-mcp",
        version=__version__,
        on_list_tools=_on_list_tools,
        on_call_tool=_on_call_tool,
    )
else:

    async def _call_tool_1x(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        """Feed `tool_result`'s verdict to mcp 1.x in the shape it accepts.

        Returning a `CallToolResult` from a decorated handler only works from
        mcp 1.20 on; raising works on every 1.x, because the decorator turns an
        exception into `CallToolResult(isError=True)` with `str(exc)` as the
        body, which is the same result the 2.x path builds directly. Error
        results come only from `_errored`, which makes exactly one text block.
        """
        result = await tool_result(name, arguments)
        if result.isError:
            raise ToolError(result.content[0].text)
        return list(result.content)

    # Applied as plain calls so `list_tools` stays bound to the undecorated
    # function, which is what the 2.x path uses.
    server = Server("evomi-mcp", version=__version__)
    server.list_tools()(list_tools)
    server.call_tool()(_call_tool_1x)


USAGE_EPILOG = """\
evomi-mcp is a Model Context Protocol server. It speaks MCP over stdio and is
started by an MCP client rather than run interactively; with no arguments it
reads JSON-RPC on stdin and writes it to stdout.

Environment variables:
  EVOMI_PUBLIC_API_KEY        Public API key, for the proxy and account tools.
                              Also fetches the scraper key when that is unset.
  EVOMI_SCRAPER_API_KEY       Scraper API key, for the scraping tools.
  EVOMI_API_KEY               Fallback for either key above.
  EVOMI_PUBLIC_BASE_URL       Public API base URL.
  EVOMI_BASE_URL              Scraper API base URL.
  EVOMI_HIDE_PROXY_PASSWORDS  Set to 1 to stop any tool returning a credential.
  EVOMI_ENABLE_SPENDING       Set to 1 to register the tools that spend the
                              account's balance.

API keys: https://my.evomi.com/settings/api
Documentation: https://docs.evomi.com
"""


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="evomi-mcp",
        description="MCP server for the Evomi proxy and web scraping APIs.",
        epilog=USAGE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"evomi-mcp {__version__}",
    )
    return parser.parse_args(argv)


def run_stdio_server() -> None:
    """Serve MCP over stdin/stdout until the client closes the stream."""
    import asyncio

    async def run():
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    asyncio.run(run())


def main(argv: list[str] | None = None) -> None:
    """Console entry point: parse arguments, then serve over stdio."""
    _parse_args(sys.argv[1:] if argv is None else argv)
    run_stdio_server()


if __name__ == "__main__":
    main()