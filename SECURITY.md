# Security Policy

## Reporting a vulnerability

Email **support@evomi.com** with a description of the issue and, where possible,
the steps to reproduce it. Please do not open a public issue for a security
report.

We will acknowledge your report and keep you informed while we investigate.

## Supported versions

Fixes are released against the latest published version of `evomi-mcp`.

## Credentials

This server reads Evomi API keys from the environment and sends them as request
headers. Keys are never written to disk and never appear in tool output or error
messages.

Four tools return live credentials by design: `get_proxy_credentials`,
`build_proxy_connection_string` and `generate_proxy_list` return proxy passwords,
and `get_api_access` returns a service API key when called with
`include_api_key`. Those values become part of the conversation with whichever
model is connected. Set `EVOMI_HIDE_PROXY_PASSWORDS=1` to stop every one of them
emitting a credential.
