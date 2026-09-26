"""The watch loop must self-heal (never die on a transient error) and its status
must make a persistent failure visible. See fix_plan.md, decision C3.
"""
import datetime as dt
import json

import continuity


def test_iteration_ok(monkeypatch):
    monkeypatch.setattr(continuity, "cmd_capture", lambda *a, **k: 0)
    monkeypatch.setattr(continuity, "cmd_ship", lambda *a, **k: 0)
    ok, err = continuity._watch_iteration({}, ["opencode"], False, False)
    assert ok is True and err is None


def test_iteration_survives_exception(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("gsutil exploded")

    monkeypatch.setattr(continuity, "cmd_capture", boom)
    ok, err = continuity._watch_iteration({}, ["opencode"], False, False)
    assert ok is False and "gsutil exploded" in err


def test_iteration_reports_nonzero(monkeypatch):
    monkeypatch.setattr(continuity, "cmd_capture", lambda *a, **k: 0)
    monkeypatch.setattr(continuity, "cmd_ship", lambda *a, **k: 1)
    ok, err = continuity._watch_iteration({}, ["opencode"], False, False)
    assert ok is False and "rc=1" in err


def test_write_status_never_raises_on_missing_parent(tmp_path, monkeypatch):
    monkeypatch.setattr(continuity, "STAGE", tmp_path / "does" / "not" / "exist")
    continuity._write_watch_status(last_result="ok")  # must not raise


def test_status_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(continuity, "STAGE", tmp_path)
    monkeypatch.setattr(continuity, "_pid_alive", lambda pid: True)
    now = dt.datetime.now().isoformat(timespec="seconds")
    (tmp_path / "watch_status.json").write_text(
        json.dumps({"pid": 1234, "last_ship_ok": now,
                    "consecutive_failures": 0, "last_error": None})
    )
    assert continuity.cmd_status({}) == 0


def test_status_flags_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(continuity, "STAGE", tmp_path)
    monkeypatch.setattr(continuity, "_pid_alive", lambda pid: True)
    (tmp_path / "watch_status.json").write_text(
        json.dumps({"pid": 1234, "last_ship_ok": "2026-09-26T11:00:00",
                    "consecutive_failures": 2, "last_error": "gsutil exploded"})
    )
    assert continuity.cmd_status({}) == 1


def test_status_down_when_pid_dead(tmp_path, monkeypatch):
    monkeypatch.setattr(continuity, "STAGE", tmp_path)
    monkeypatch.setattr(continuity, "_pid_alive", lambda pid: False)
    (tmp_path / "watch_status.json").write_text(
        json.dumps({"pid": 999999, "consecutive_failures": 0})
    )
    assert continuity.cmd_status({}) == 1


def test_status_down_when_never_started(tmp_path, monkeypatch):
    monkeypatch.setattr(continuity, "STAGE", tmp_path)
    assert continuity.cmd_status({}) == 1
