# Changelog

## 1.3.0

- MCP server over stdio for Evomi's Public (proxy) and Scraper APIs, exposing 39
  tools by default and 40 with `EVOMI_ENABLE_SPENDING=1`.
- Proxy account tools: products, credentials, geo-targeted connection strings,
  bulk proxy lists, targeting catalogues, usage, session rotation, browser
  profiles and service access.
- Scraping tools: single-page scraping, crawling, URL discovery, domain search, a
  conversational agent, async task status, and management of saved configs,
  extraction schemas, storage configs and schedules.
- `EVOMI_PUBLIC_API_KEY` is sufficient on its own; the scraper key is read from
  the Public API on the first scraping call and held in memory.
  `EVOMI_SCRAPER_API_KEY` pins a key and skips that lookup.
- `EVOMI_HIDE_PROXY_PASSWORDS=1` stops any tool emitting a proxy password or a
  service API key.
- Every tool states all four MCP annotation hints.
- `--help` and `--version` on the console entry point.
- Runs on `mcp` 1.x and 2.x, and on Python 3.10 through 3.14.
