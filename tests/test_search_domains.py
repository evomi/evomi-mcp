"""`search_domains` takes one query or a list of them, on both SDK majors.

Arguments are validated against `inputSchema` before dispatch, because mcp 2.x
performs no validation of its own. The description promises "an array of up to
10 queries", so the schema has to declare both shapes for both to reach the
handler. These pin both shapes, so the schema cannot narrow under the
description.
"""

import pytest

from evomi_mcp import server as server_module
from evomi_mcp.server import _input_schema, list_tools


class _StubClient:
    """Records what the dispatch handed the API, so `query` can be compared."""

    def __init__(self):
        self.calls = []

    async def search_domains(self, query, max_urls=20, region="us-en"):
        self.calls.append({"query": query, "max_urls": max_urls, "region": region})
        return {"domains": []}


def _is_error(result) -> bool:
    return result.is_error if hasattr(result, "is_error") else result.isError


@pytest.fixture
def client(monkeypatch):
    stub = _StubClient()
    monkeypatch.setattr(server_module, "_client", stub)
    return stub


async def test_a_single_query_is_accepted(client):
    result = await server_module.tool_result(
        "search_domains", {"query": "online bookstores"}
    )

    assert _is_error(result) is False
    assert client.calls == [
        {"query": "online bookstores", "max_urls": 20, "region": "us-en"}
    ]


async def test_an_array_of_queries_reaches_the_api_unchanged(client):
    """The docs' multi-query example, run through the validator and the dispatch."""
    queries = ["online bookstores", "book shops UK"]

    result = await server_module.tool_result(
        "search_domains", {"query": queries, "max_urls": 50}
    )

    assert _is_error(result) is False
    assert client.calls == [{"query": queries, "max_urls": 50, "region": "us-en"}]


async def test_more_queries_than_are_offered_is_refused(client):
    result = await server_module.tool_result(
        "search_domains", {"query": [f"query {n}" for n in range(11)]}
    )

    assert _is_error(result) is True
    assert result.content[0].text.startswith("Input validation error")
    assert not client.calls, "the call must not reach the API"


@pytest.mark.parametrize("query", [7, [], [["nested"]], {"q": "x"}])
async def test_a_query_that_is_neither_shape_is_refused(client, query):
    result = await server_module.tool_result("search_domains", {"query": query})

    assert _is_error(result) is True
    assert not client.calls


async def test_the_schema_promises_what_the_description_does():
    tool = next(t for t in await list_tools() if t.name == "search_domains")
    query = _input_schema(tool)["properties"]["query"]

    assert set(query["type"]) == {"string", "array"}
    assert query["items"] == {"type": "string"}
    assert f"up to {query['maxItems']} queries" in query["description"]
