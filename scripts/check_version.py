#!/usr/bin/env python3
"""Assert that this package declares one version everywhere it declares one.

`pyproject.toml`'s `[project] version` is what lands in the built artifact's
metadata; `evomi_mcp.__version__` is what the code reports about itself. Nothing
in the language keeps the two in step, so this check does.

With no arguments it compares the two files against each other. Pass
`--expect VERSION` to require both to equal a third value as well, which is how
the release workflow pins the artifact to the tag being released.

The versions are read out of the files as text rather than by importing the
package, so this runs with nothing installed and cannot be fooled by a stale
install in the current environment.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
INIT = REPO_ROOT / "src" / "evomi_mcp" / "__init__.py"

_ASSIGNMENT = re.compile(r"""^version\s*=\s*["']([^"']+)["']""")
_DUNDER = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.MULTILINE)


def _version_from_toml_text(text: str) -> str | None:
    """Return `[project] version` without a TOML parser.

    `tomllib` is only in the standard library from 3.11 and this package
    supports 3.10, so the assignment is scanned for inside the `[project]`
    table.
    """
    in_project = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_project = stripped == "[project]"
            continue
        if in_project:
            match = _ASSIGNMENT.match(stripped)
            if match:
                return match.group(1)
    return None


def version_from_pyproject(path: Path = PYPROJECT) -> str:
    text = path.read_text(encoding="utf-8")
    try:
        import tomllib
    except ImportError:
        version = _version_from_toml_text(text)
    else:
        version = tomllib.loads(text).get("project", {}).get("version")

    if not version:
        raise SystemExit(f"no [project] version found in {path}")
    return version


def version_from_init(path: Path = INIT) -> str:
    match = _DUNDER.search(path.read_text(encoding="utf-8"))
    if match is None:
        raise SystemExit(f"no __version__ assignment found in {path}")
    return match.group(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check that the declared versions of this package agree.",
    )
    parser.add_argument(
        "--expect",
        metavar="VERSION",
        help=(
            "additionally require both files to equal this version; a single "
            "leading 'v' is stripped, so a git tag can be passed verbatim"
        ),
    )
    args = parser.parse_args(argv)

    declared = {
        "pyproject.toml  [project] version": version_from_pyproject(),
        "src/evomi_mcp/__init__.py  __version__": version_from_init(),
    }
    if args.expect is not None:
        expected = args.expect[1:] if args.expect.startswith("v") else args.expect
        declared[f"--expect  (given {args.expect!r})"] = expected

    distinct = set(declared.values())
    if len(distinct) > 1:
        print("VERSION MISMATCH — these must all be the same string:", file=sys.stderr)
        for source, value in declared.items():
            print(f"  {value:<16} <-- {source}", file=sys.stderr)
        print(
            "\nBump every location to the same version before releasing.",
            file=sys.stderr,
        )
        return 1

    version = distinct.pop()
    print(f"version OK: {version} agrees across {len(declared)} source(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
