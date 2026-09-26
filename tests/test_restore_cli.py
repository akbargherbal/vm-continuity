"""Every documented `restore` spelling must route correctly (see bug_report.md).

Regression guard: the README/SKILL document `restore opencode -- --mode db|export`,
which continuity.py never parsed, so the documented form exited 2.
"""
import continuity


def _patch(monkeypatch):
    """Patch the one tool module main() will load, and record the routing."""
    tool = continuity.load_tools()["opencode"]
    calls: dict = {}

    def _db(stage, rest):
        calls["mode"] = "db"
        calls["rest"] = rest
        return 0

    def _export(stage, rest):
        calls["mode"] = "export"
        calls["rest"] = rest
        return 0

    monkeypatch.setattr(tool, "_restore_db", _db)
    monkeypatch.setattr(tool, "_restore_export", _export)
    # main() calls load_tools() itself; make it return the patched module.
    monkeypatch.setattr(continuity, "load_tools", lambda: {"opencode": tool})
    return calls


def test_documented_mode_db(monkeypatch):
    calls = _patch(monkeypatch)
    rc = continuity.main(["restore", "opencode", "--", "--mode", "db"])
    assert rc == 0
    assert calls["mode"] == "db" and calls["rest"] == []


def test_documented_mode_export_with_inner_dashdash(monkeypatch):
    calls = _patch(monkeypatch)
    rc = continuity.main(
        ["restore", "opencode", "--", "--mode", "export", "--", "--directory", "/content"]
    )
    assert rc == 0
    assert calls["mode"] == "export"
    assert calls["rest"] == ["--directory", "/content"]


def test_positional_export(monkeypatch):
    calls = _patch(monkeypatch)
    rc = continuity.main(["restore", "opencode", "--", "export", "--directory", "/content"])
    assert rc == 0
    assert calls["mode"] == "export" and calls["rest"] == ["--directory", "/content"]


def test_default_is_db(monkeypatch):
    calls = _patch(monkeypatch)
    rc = continuity.main(["restore", "opencode"])
    assert rc == 0
    assert calls["mode"] == "db" and calls["rest"] == []


def test_db_path_passthrough(monkeypatch):
    calls = _patch(monkeypatch)
    rc = continuity.main(["restore", "opencode", "--", "--db-path", "/tmp/x.db"])
    assert rc == 0
    assert calls["mode"] == "db" and calls["rest"] == ["--db-path", "/tmp/x.db"]


def test_invalid_mode_errors(monkeypatch):
    calls = _patch(monkeypatch)
    rc = continuity.main(["restore", "opencode", "--", "--mode", "bogus"])
    assert rc == 2
    assert "mode" not in calls
