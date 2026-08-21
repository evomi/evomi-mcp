"""The package must declare one version, not two.

`pyproject.toml` and `src/evomi_mcp/__init__.py` each name a version, and
nothing but a check keeps the two in step. `test_smoke.py` compares
`__version__` to the installed distribution metadata, but only when the
environment happens to hold a fresh install of the current tree. These tests
read the two files, so they hold whatever is installed and run in a bare
checkout.

The same script gates a release against the git tag — see
`.github/workflows/release.yml` — so these tests are also what keep that gate
honest.
"""

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECK_VERSION = REPO_ROOT / "scripts" / "check_version.py"


def _load_check_version():
    """Import `scripts/check_version.py` without putting `scripts/` on the path.

    It is a script, not a module in the distribution, so there is nothing to
    import it by name.
    """
    spec = importlib.util.spec_from_file_location("check_version", CHECK_VERSION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_version = _load_check_version()


def test_declared_versions_agree():
    assert check_version.version_from_pyproject() == check_version.version_from_init()


def test_the_check_passes_on_this_tree():
    assert check_version.main([]) == 0


def test_the_check_rejects_a_version_that_does_not_match(capsys):
    """A check that cannot fail is not a check.

    The release workflow's only protection against publishing a version other
    than the one the tag names is this exit code, so assert it is reachable
    rather than trusting it.
    """
    assert check_version.main(["--expect", "0.0.0"]) == 1
    assert "VERSION MISMATCH" in capsys.readouterr().err


def test_a_leading_v_in_a_tag_is_accepted():
    """Tags are conventionally `v1.2.3`; the version string itself never is."""
    declared = check_version.version_from_pyproject()
    assert check_version.main(["--expect", f"v{declared}"]) == 0
    assert check_version.main(["--expect", declared]) == 0
