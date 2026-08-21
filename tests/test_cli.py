"""The console entry point's argument handling.

`evomi-mcp` with no arguments is an MCP server: it reads JSON-RPC on stdin and
blocks until the client closes the stream. Anything that is not the server has
to answer and exit rather than block, or a person running it in a terminal sees
a hang with no output.
"""

import pytest

from evomi_mcp import __version__
from evomi_mcp.server import main


@pytest.fixture(autouse=True)
def never_serve(monkeypatch):
    """Fail loudly if a test reaches the stdio server, which would block."""
    monkeypatch.setattr(
        "evomi_mcp.server.run_stdio_server",
        lambda: pytest.fail("the stdio server should not have started"),
    )


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_prints_usage_and_exits_zero(capsys, flag):
    with pytest.raises(SystemExit) as excinfo:
        main([flag])

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "evomi-mcp" in out
    assert "stdio" in out
    assert "EVOMI_PUBLIC_API_KEY" in out
    assert "EVOMI_SCRAPER_API_KEY" in out
    assert "docs.evomi.com" in out


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_prints_the_version_and_exits_zero(capsys, flag):
    with pytest.raises(SystemExit) as excinfo:
        main([flag])

    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["--nope"], ["serve"], ["-x"]])
def test_an_unrecognised_argument_fails_on_stderr(capsys, argv):
    with pytest.raises(SystemExit) as excinfo:
        main(argv)

    assert excinfo.value.code != 0
    captured = capsys.readouterr()
    assert captured.err
    assert "usage" in captured.err.lower()


def test_no_arguments_runs_the_stdio_server(monkeypatch):
    started = []
    monkeypatch.setattr(
        "evomi_mcp.server.run_stdio_server", lambda: started.append(True)
    )

    main([])

    assert started == [True]
