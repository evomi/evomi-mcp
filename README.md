# Evomi MCP Server

A Model Context Protocol (MCP) server for Evomi's proxy and web scraping APIs.
It gives AI assistants like Claude direct access to a proxy account — credentials,
connection strings, geo targeting, usage, session rotation — alongside a full
scraping toolkit for crawling sites and extracting structured data.

39 tools by default, 40 with `EVOMI_ENABLE_SPENDING=1`.

## Features

### Proxy & account management

- **Proxy Credentials** - Username and password for each product on the account
- **Connection Strings** - Geo-targeted, session-pinned strings, with a curl command to check them
- **Bulk Proxy Lists** - Up to 50 endpoints per call
- **Geo Targeting** - Search the countries, regions, cities and ISPs a product actually offers
- **Usage & Balance** - Bandwidth over 24h, 3d and 7d, in total and per bucket
- **Session Control** - Rotate a sticky session onto a new exit IP
- **Browser Profiles** - List saved fingerprint profiles, and order more behind an opt-in
- **Service Access** - Scraper and Browser credits, concurrency and endpoints

### Web scraping

- **Single Page Scraping** - Scrape any URL with automatic JavaScript detection
- **Website Crawling** - Multi-page crawling with depth control
- **URL Discovery** - Find URLs via sitemaps, CommonCrawl, or in-site crawling
- **Domain Search** - Find domains by searching the web
- **AI-Powered Extraction** - Use AI to extract structured data from pages
- **Conversational Agent** - Natural language interface for scraping tasks
- **Config Management** - Save and reuse scraping configurations
- **Schema Management** - Define and test extraction schemas
- **Storage Configuration** - Manage cloud storage for scraped data
- **Scheduled Jobs** - Automate scraping on a schedule

## Installation

```bash
pip install evomi-mcp
```

Needs Python 3.10 or newer. The server runs on both major versions of the `mcp`
SDK (`>=1.8.0,<3`), so it installs into an environment already pinned to 1.x as
well as a fresh one that resolves 2.x.

Run `evomi-mcp --help` for the environment variables it reads, or `evomi-mcp
--version` for the installed version. With no arguments it speaks MCP over
stdio, which is how an MCP client starts it.

## Configuration

One credential is enough:

```bash
export EVOMI_PUBLIC_API_KEY="your-public-api-key"
```

Take it from [Settings > API](https://my.evomi.com/settings/api) for a personal
account, or Settings > Team for a team one. It authenticates the proxy and
account tools directly, and the scraping tools authenticate with the account's
scraper key, which the server reads from the Public API on the first scraping
call and keeps in memory for the process. It is never written to disk and never
appears in tool output.

| Variable | Key | Used by |
| --- | --- | --- |
| `EVOMI_PUBLIC_API_KEY` | Public API key | proxy credentials, usage, targeting, sessions — and, indirectly, everything else |
| `EVOMI_SCRAPER_API_KEY` | Scraper API key, from the same page | scraping, crawling, configs, schemas, schedules. Optional: set it to pin a specific key, and no lookup is made |
| `EVOMI_API_KEY` | Fallback for either of the above | both, when the specific variable is unset |

Where a specific variable is set it wins over `EVOMI_API_KEY`. Setting a scraper
key alone works too, and the proxy and account tools then need
`EVOMI_PUBLIC_API_KEY` as well, since one key cannot serve both APIs.

Optional settings:

```bash
export EVOMI_BASE_URL="https://scrape.evomi.com"   # scraper API, default
export EVOMI_PUBLIC_BASE_URL="https://api.evomi.com"  # public API, default
export EVOMI_HIDE_PROXY_PASSWORDS=1  # mask every proxy password and service API key
export EVOMI_ENABLE_SPENDING=1  # register the tools that spend account balance
```

### Credentials in tool output

`get_proxy_credentials`, `build_proxy_connection_string` and `generate_proxy_list`
return live proxy passwords, which is what they are for, and `get_api_access`
returns a service API key when asked with `include_api_key`. Every other tool
returns balances, endpoints, usage and targeting data only.

Four things bound that. Those four tools carry an instruction in their
descriptions not to repeat the value back unless it was asked for directly — the
MCP spec has no annotation for a sensitive result, so the description is the only
channel that reaches the model. `generate_proxy_list` and
`build_proxy_connection_string` return at most **50** entries per call, well under
the 500 the Public API allows, and refuse a larger request rather than clamping
it. The `curl_example` that comes with a connection string has its password
masked, since it is the field most likely to be pasted into a terminal or a
ticket; pass `runnable_curl_example: true` for the form that can be run. And
`EVOMI_HIDE_PROXY_PASSWORDS=1` turns disclosure off entirely:

| Tool | With the flag set |
| :--- | :--- |
| `get_proxy_credentials` | **Masked.** The username, gateway, ports and balance are unchanged |
| `get_api_access` | **Masked**, even when `include_api_key: true` is passed explicitly |
| `build_proxy_connection_string` | **Refuses**, naming the variable |
| `generate_proxy_list` | **Refuses** before the API is called, so the bulk credentials are never minted |
| every other tool | Unchanged — none of them returns a credential |

The two that refuse do so because their entire output is the credential: a
connection string with a masked password cannot connect. The refusal points at
`list_proxy_products` and `list_proxy_targeting_options`, which give the gateway
hostname, ports and targeting values with no credential in them, and the
descriptions the model sees change too.

`EVOMI_ENABLE_SPENDING=1` is the opposite, an opt-in for the one tool that costs
money. Without it `order_browser_profile` is not registered at all, so the
connected model never sees it.

### Tool annotations

Every tool sets all four of the MCP spec's hints. `destructiveHint` and
`openWorldHint` default to **true**, so a tool that omits them advertises itself
as potentially destructive and as reaching an unbounded external world.

`openWorldHint` is true for the tools that reach an address the caller chose
(`scrape_url`, `crawl_website`, `map_website`, `search_domains`, `agent_request`,
and the schema and config tools that validate against the page they describe),
and false for everything that only talks to Evomi's own endpoints.
`readOnlyHint` is false for the tools that create, update, delete, toggle, rotate
or order something. The `MUTATING` prefix on `rotate_proxy_session` and
`order_browser_profile` is in the description as well, because the description
reaches every model where an annotation only reaches a client that reads it.

## Usage with Claude Desktop

Add to your Claude Desktop configuration (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "evomi": {
      "command": "evomi-mcp",
      "env": {
        "EVOMI_PUBLIC_API_KEY": "your-public-api-key"
      }
    }
  }
}
```

Or if installed from source:

```json
{
  "mcpServers": {
    "evomi": {
      "command": "python",
      "args": ["-m", "evomi_mcp.server"],
      "env": {
        "EVOMI_PUBLIC_API_KEY": "your-public-api-key"
      }
    }
  }
}
```

## Available Tools

### Proxy & Account (9 tools, Evomi Public API, + 1 opt-in)

| Tool | Description | Returns credentials |
| :--- | :--- | :--- |
| `list_proxy_products` | Products on the account with endpoints, ports, balance and username | No |
| `get_proxy_credentials` | Proxy username and password for one product | **Yes** |
| `build_proxy_connection_string` | Connection strings with geo targeting, sessions and expert filters, plus a curl check | **Yes** |
| `generate_proxy_list` | Bulk proxy list (up to 50 per call) from the Public API generator | **Yes** |
| `get_proxy_usage` | Bandwidth used over 24h / 3d / 7d, total and per bucket | No |
| `list_proxy_targeting_options` | Searchable countries, regions, cities, ISPs and continents per product, each with the `id` the gateway accepts | No |
| `rotate_proxy_session` | **Mutating** — force a sticky session onto a new exit IP | No |
| `get_api_access` | Scraper and Browser access, credits, concurrency and endpoints (keys masked by default) | Only on request |
| `list_browser_profiles` | Saved browser fingerprint profiles | No |
| `order_browser_profile` | **Mutating, spends money** — orders a browser fingerprint profile, charged against the account's data balance. Only registered when `EVOMI_ENABLE_SPENDING=1` | No |

### Scraping Operations (6 tools)

| Tool | Description |
|------|-------------|
| `scrape_url` | Scrape a single URL with configurable options |
| `crawl_website` | Crawl a website to discover and scrape multiple pages |
| `map_website` | Discover URLs from a website |
| `search_domains` | Find domains by searching the web |
| `agent_request` | AI-powered conversational scraping assistant |
| `get_task_status` | Check the status of an async task |

### Config Management (6 tools)

| Tool | Description |
|------|-------------|
| `list_configs` | List all saved scrape configurations |
| `create_config` | Create a new scrape configuration |
| `get_config` | Get a saved scrape configuration by ID |
| `update_config` | Update an existing scrape configuration |
| `delete_config` | Delete a scrape configuration |
| `generate_config` | Generate a scrape config from natural language using AI |

### Schema Management (6 tools)

| Tool | Description |
|------|-------------|
| `list_schemas` | List all saved extraction schemas |
| `create_schema` | Create a new extraction schema |
| `get_schema` | Get a saved extraction schema by ID |
| `update_schema` | Update an existing extraction schema |
| `delete_schema` | Delete an extraction schema |
| `get_schema_status` | Get the test status of an extraction schema |

### Storage Management (4 tools)

| Tool | Description |
|------|-------------|
| `list_storage_configs` | List all storage configurations |
| `create_storage_config` | Create a new storage configuration |
| `update_storage_config` | Update an existing storage configuration |
| `delete_storage_config` | Delete a storage configuration |

### Schedule Management (7 tools)

| Tool | Description |
|------|-------------|
| `list_schedules` | List all scheduled scrape jobs |
| `create_schedule` | Create a new scheduled scrape job |
| `get_schedule` | Get a scheduled job by ID |
| `update_schedule` | Update an existing scheduled job |
| `delete_schedule` | Delete a scheduled job |
| `toggle_schedule` | Toggle a scheduled job active/inactive |
| `list_schedule_runs` | Get execution history for a scheduled job |

### Account (1 tool)

| Tool | Description |
|------|-------------|
| `get_account_info` | Get account information including credit balance |

## Tool Examples

### Scraping

```json
// Basic scrape
{"url": "https://example.com"}

// AI extraction
{"url": "https://example.com/products", "ai_enhance": true, "ai_prompt": "Extract product names and prices"}

// Browser mode with actions
{"url": "https://example.com", "mode": "browser", "js_instructions": [{"click": ".accept-cookies"}, {"wait": 1000}]}
```

### Crawling

```json
// Basic crawl
{"domain": "example.com", "max_urls": 50}

// With URL filter
{"domain": "example.com", "url_pattern": "/products/", "depth": 3}
```

### Domain Search

```json
// Find domains
{"query": "best e-commerce sites for electronics", "max_urls": 20, "region": "us-en"}

// Up to 10 queries in one call, max_urls applying to each
{"query": ["online bookstores", "book shops UK"], "max_urls": 50}
```

### Config Management

```json
// Create config
{"name": "Product Scraper", "config": {"mode": "browser", "output": "markdown"}}

// Generate config with AI
{"name": "Amazon Scraper", "prompt": "Scrape product title, price, and reviews from Amazon"}
```

### Scheduling

```json
// Create daily schedule
{"name": "Daily Prices", "config_id": "cfg_abc123", "interval_minutes": 1440, "start_time": "09:00"}
```

## Credits

Scraping operations consume credits, and what a call costs depends on the mode it
runs in and the options it carries. The current rates are at
[docs.evomi.com](https://docs.evomi.com); `get_api_access` reports the balance on
the account.

Each scraping response carries `credits_used` and `credits_remaining`, so the
cost of a call is visible in its own result.

## Development

### Setup

```bash
pip install -e ".[dev]"
```

### Running the Server Directly

```bash
evomi-mcp
# or
python -m evomi_mcp.server
```

### Tests

```bash
pytest
```

The suite runs against both `mcp` majors, and CI runs it on each of them across
the supported Python versions.

### Releasing

The version is declared in both `pyproject.toml` and `src/evomi_mcp/__init__.py`,
and CI fails if they disagree. Publishing a GitHub Release tagged `vX.Y.Z` builds
the artifacts, runs the suite against the built wheel and uploads to PyPI over
Trusted Publishing, so there is no token anywhere.

## Links

- [Evomi Website](https://evomi.com)
- [Evomi Dashboard](https://my.evomi.com)
- [API Documentation](https://docs.evomi.com)

## License

MIT — see [LICENSE](LICENSE).
