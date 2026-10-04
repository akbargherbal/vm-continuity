"""opencode CLI surface differs across versions; the tool must adapt.

OpenCode v2.0 moved export/import under the `session` subcommand:

    v1.x:  opencode export <id>        / opencode import <file>
    v2.0+: opencode session export <id> / opencode session import <file> --directory <dir>

`tools/opencode.py` probes once and caches the choice. These tests pin both
branches (regression guard for the v2.0.22 failure where export silently produced
a 2 KB help-text file and 0 sessions were captured).
"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "oc_tool_under_test", ROOT / "tools" / "opencode.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Res:
    def __init__(self, rc, out=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = ""


@pytest.mark.parametrize("rc,expected", [(0, ["session", "export"]), (1, ["export"])])
def test_export_prefix(monkeypatch, rc, expected):
    mod = _load()
    monkeypatch.setattr(mod, "_opencode", lambda *a: _Res(rc, "help"))
    assert mod._export_prefix() == expected


@pytest.mark.parametrize("rc,session_sub", [(0, True), (1, False)])
def test_import_argv(monkeypatch, rc, session_sub):
    mod = _load()
    monkeypatch.setattr(mod, "_opencode", lambda *a: _Res(rc, "help"))
    argv = mod._import_argv(Path("/tmp/s.json"), Path("/content/repo"))
    if session_sub:
        assert argv == ["session", "import", "/tmp/s.json", "--directory", "/content/repo"]
    else:
        assert argv == ["import", "/tmp/s.json"]
