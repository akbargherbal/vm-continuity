#!/usr/bin/env python3
"""VM-continuity dispatcher: capture / ship / pull / restore / list.

Tool-agnostic. Each tool is a module under tools/ exposing:
    NAME = "<tool>"
    STORE = "<store-folder>"                 # optional; defaults to NAME
    capture(stage: Path, log) -> dict
    restore(stage: Path, argv: list) -> int

Staging is ephemeral (/content/vm_state); the GCS store is the persistent copy.
Self-contained: ships with its own gsutil mover — no dependency on any project's
backup script. See skills/vm-continuity/SKILL.md for the model and rules.

Usage:
    python continuity.py list
    python continuity.py capture [tool ...]
    python continuity.py ship [--dry-run] [--force] [tool ...]
    python continuity.py hosts [tool ...]
    python continuity.py pull [--host H] [tool ...]
    python continuity.py restore <tool> [-- <tool args>]
    python continuity.py watch [--interval-minutes N] [--dry-run] [tool ...]
    python continuity.py status

Each VM ships to its OWN namespace, <base>/<STORE>/by_host/<host>/, so a fresh VM's
captures can never clobber another VM's store. `pull` reads a host namespace (or the
legacy aggregate root if one exists).

Env overrides:
    CONTINUITY_STAGE   default /content/vm_state
    CONTINUITY_GCS     base bucket/prefix; a tool lands under <base>/<STORE>/
                       (default gs://akbar-december-2024-backup)
    CONTINUITY_HOST    override the host namespace name (default: hostname)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS_DIR = HERE / "tools"

STAGE = Path(os.environ.get("CONTINUITY_STAGE", "/content/vm_state"))
GCS = os.environ.get("CONTINUITY_GCS", "gs://akbar-december-2024-backup").rstrip("/")


def store_uri(tools: dict, name: str) -> str:
    """A tool's logical store root: <base>/<STORE>/."""
    sub = getattr(tools[name], "STORE", name)
    return f"{GCS}/{sub}" if sub else GCS


def host_name() -> str:
    """This VM's namespace name (override with CONTINUITY_HOST)."""
    raw = os.environ.get("CONTINUITY_HOST") or socket.gethostname()
    return re.sub(r"[^A-Za-z0-9._-]", "-", raw).strip("-") or "unknown"


def ship_uri(tools: dict, name: str) -> str:
    """Where THIS VM ships: <root>/by_host/<host>/."""
    return f"{store_uri(tools, name)}/by_host/{host_name()}"


def _gsutil_exists(uri: str) -> bool:
    return subprocess.run(["gsutil", "-q", "stat", uri], capture_output=True).returncode == 0


def _list_hosts(root: str) -> list[str]:
    res = subprocess.run(
        ["gsutil", "ls", "-d", root.rstrip("/") + "/by_host/*/"],
        capture_output=True, text=True,
    )
    hosts = []
    for line in res.stdout.splitlines():
        line = line.strip().rstrip("/")
        if "/by_host/" not in line:
            continue
        hosts.append(line.rsplit("/", 1)[-1])
    return sorted(set(hosts))


def _local_session_count(src: Path):
    cap = src / "CAPTURE.json"
    if not cap.exists():
        return None
    try:
        return int(json.loads(cap.read_text()).get("sessions", {}).get("total"))
    except Exception:
        return None


def _remote_session_count(store: str):
    res = subprocess.run(
        ["gsutil", "cat", store.rstrip("/") + "/CAPTURE.json"],
        capture_output=True, text=True,
    )
    if res.returncode != 0 or not res.stdout.strip():
        return None
    try:
        return int(json.loads(res.stdout).get("sessions", {}).get("total"))
    except Exception:
        return None


def load_tools() -> dict:
    import importlib.util

    tools: dict = {}
    for path in sorted(TOOLS_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(f"continuity_tool_{path.stem}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        tools[mod.NAME] = mod
    return tools


def cmd_list(tools: dict) -> int:
    for name in tools:
        cap = STAGE / name / "CAPTURE.json"
        if cap.exists():
            m = json.loads(cap.read_text())
            s = m.get("sessions", {})
            print(
                f"{name}: captured {m.get('captured')}  "
                f"version={m.get('opencode_version')}  "
                f"sessions={s.get('exported')}/{s.get('total')}  "
                f"db={m.get('files', {}).get('opencode.db', {}).get('bytes')}B"
            )
        else:
            print(f"{name}: no capture at {cap}")
    return 0


def cmd_capture(tools: dict, names: list[str]) -> int:
    rc = 0
    for name in names:
        m = tools[name].capture(STAGE / name)
        if not m:
            rc = 1
    return rc


def cmd_ship(tools: dict, names: list[str], dry_run: bool, force: bool = False) -> int:
    """rsync each tool's staging folder to its store (append/update, never deletes).

    Anti-clobber: unless --force, refuse to ship a tool whose local session count is
    *below* the store's — so a fresh VM (near-empty DB) cannot overwrite a richer backup.
    """
    rc = 0
    for name in names:
        src = STAGE / name
        if not src.is_dir():
            print(f"[ship] skip {name}: no local capture at {src}")
            continue
        dst = ship_uri(tools, name)
        if not dry_run and not force:
            local = _local_session_count(src)
            remote = _remote_session_count(dst)
            if local is not None and remote is not None and local < remote:
                print(
                    f"[ship] REFUSING {name}: local has {local} sessions, store has "
                    f"{remote} — possible clobber from a fresh VM. Use --force to override."
                )
                rc = 1
                continue
        cmd = ["gsutil", "-m", "rsync", "-r", "-c", str(src), dst.rstrip("/") + "/"]
        print(f"[ship] {src} -> {dst}/")
        if dry_run:
            print(f"[ship] dry-run: {' '.join(cmd)}")
            continue
        if subprocess.run(cmd).returncode != 0:
            rc = 1
    return rc


def _watch_status_path() -> Path:
    return STAGE / "watch_status.json"


def _write_watch_status(**fields) -> None:
    """Best-effort, atomic-ish write of the watch loop's status. Never raises.

    This is the "never fail silently" surface: `vm-continuity status` reads it.
    A failure to write status must not kill the loop.
    """
    path = _watch_status_path()
    try:
        cur = json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        cur = {}
    cur.update(fields)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(cur, indent=2) + "\n")
        tmp.replace(path)
    except Exception:
        pass


def _watch_iteration(tools: dict, names: list[str], dry_run: bool, force: bool):
    """One capture+ship pass. Returns (ok, error_or_None). Never raises."""
    try:
        cap_rc = cmd_capture(tools, names)
        ship_rc = cmd_ship(tools, names, dry_run, force)
    except Exception as e:  # a transient error must not end the loop
        return False, f"{type(e).__name__}: {e}"
    if cap_rc == 0 and ship_rc == 0:
        return True, None
    return False, f"capture rc={cap_rc}, ship rc={ship_rc}"


def cmd_watch(tools: dict, names: list[str], interval_minutes: float,
              dry_run: bool, force: bool) -> int:
    """capture + ship on a loop. Intended to be run detached (see SKILL.md).

    Self-healing by design: a transient failure is logged and retried with a
    capped backoff instead of ending the loop. Status is written to
    STAGE/watch_status.json and read by `continuity.py status`, so a persistent
    failure is *visible* without anyone babysitting the process.
    """
    interval = max(60.0, interval_minutes * 60.0)
    _write_watch_status(
        pid=os.getpid(),
        started=dt.datetime.now().isoformat(timespec="seconds"),
        interval_minutes=interval_minutes,
        tools=names,
        consecutive_failures=0,
        last_result="starting",
        last_error=None,
    )
    print(f"[watch] capture+ship every {interval_minutes:.0f} min "
          f"(dry-run={dry_run}, force={force}); status -> {_watch_status_path()}")
    fails = 0
    while True:
        ok, err = _watch_iteration(tools, names, dry_run, force)
        now = dt.datetime.now().isoformat(timespec="seconds")
        if ok:
            fails = 0
            _write_watch_status(last_capture=now, last_ship_ok=now, last_result="ok",
                                consecutive_failures=0, last_error=None)
            print(f"[watch] ok at {now}")
            delay = interval
        else:
            fails += 1
            _write_watch_status(last_result="failed", last_failure=now,
                                consecutive_failures=fails, last_error=err)
            print(f"[watch] FAILED ({fails}) at {now}: {err} — retrying", file=sys.stderr)
            delay = min(interval, 60.0 * (2 ** min(fails - 1, 3)))
        time.sleep(delay)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def cmd_status(tools: dict) -> int:
    """One-line health of the session-backup loop. Exit 0 only when healthy.

    Cheap enough to run before a task and at session end (see AGENTS.md): the
    point is to KNOW whether backup happened, not to repair anything.
    """
    path = _watch_status_path()
    if not path.exists():
        print(f"[status] loop=DOWN (no {path} — never started?)")
        return 1
    try:
        m = json.loads(path.read_text())
    except Exception as e:
        print(f"[status] loop=UNKNOWN (unreadable {path}: {e})")
        return 1
    pid = m.get("pid")
    running = bool(pid) and _pid_alive(int(pid))
    age = "n/a"
    last_ok = m.get("last_ship_ok")
    if last_ok:
        try:
            secs = (dt.datetime.now() - dt.datetime.fromisoformat(last_ok)).total_seconds()
            age = f"{int(secs)}s ago" if secs < 3600 else f"{secs / 3600:.1f}h ago"
        except Exception:
            age = "?"
    fails = m.get("consecutive_failures", 0)
    state = "OK" if (running and not fails) else ("STALE" if running else "DOWN")
    where = f"running(pid {pid})" if running else "DOWN"
    err = m.get("last_error")
    print(f"[status] loop={where} state={state} last_ship_ok={last_ok} ({age}) "
          f"failures={fails}" + (f" last_error={err}" if err else ""))
    return 0 if state == "OK" else 1


def _resolve_pull_source(root: str, host: str | None) -> str | None:
    if host:
        return f"{root}/by_host/{host}"
    if _gsutil_exists(root.rstrip("/") + "/CAPTURE.json"):
        return root  # legacy/aggregate store (pre-namespace)
    hosts = _list_hosts(root)
    if len(hosts) == 1:
        return f"{root}/by_host/{hosts[0]}"
    if not hosts:
        print(f"[pull] nothing at {root} (no CAPTURE.json, no by_host/ entries)",
              file=sys.stderr)
        return None
    print(f"[pull] multiple hosts — pass --host <one of>: {', '.join(hosts)}", file=sys.stderr)
    return None


def cmd_pull(tools: dict, names: list[str], host: str | None = None) -> int:
    rc = 0
    for name in names:
        dst = STAGE / name
        dst.mkdir(parents=True, exist_ok=True)
        src = _resolve_pull_source(store_uri(tools, name), host)
        if src is None:
            return 2
        cmd = ["gsutil", "-m", "rsync", "-r", src, str(dst)]
        print(f"[pull] {src} -> {dst}")
        if subprocess.run(cmd).returncode != 0:
            rc = 1
    return rc


def cmd_hosts(tools: dict) -> int:
    for name in tools:
        root = store_uri(tools, name)
        legacy = _gsutil_exists(root.rstrip("/") + "/CAPTURE.json")
        hosts = _list_hosts(root)
        print(f"{name}: root={root}")
        print(f"  legacy root CAPTURE.json: {'yes' if legacy else 'no'}")
        for h in hosts:
            print(f"  by_host/{h}/")
        if not hosts:
            print("  by_host/: (none)")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    tools = load_tools()

    if cmd == "list":
        return cmd_list(tools)
    if cmd == "status":
        return cmd_status(tools)
    if cmd == "hosts":
        return cmd_hosts(tools)
    if cmd in ("capture", "ship", "pull", "watch"):
        dry = "--dry-run" in rest
        force = "--force" in rest
        interval = 15.0
        host = None
        for flag, conv in (("--interval-minutes", float), ("--host", str)):
            if flag in rest:
                i = rest.index(flag)
                try:
                    val = conv(rest[i + 1])
                except (IndexError, ValueError):
                    print(f"{flag} needs a value", file=sys.stderr)
                    return 2
                rest = rest[:i] + rest[i + 2:]
                if flag == "--interval-minutes":
                    interval = val
                else:
                    host = val
        names = [a for a in rest if not a.startswith("-")] or list(tools)
        unknown = [n for n in names if n not in tools]
        if unknown:
            print(f"unknown tool(s): {', '.join(unknown)}", file=sys.stderr)
            return 2
        if cmd == "capture":
            return cmd_capture(tools, names)
        if cmd == "pull":
            return cmd_pull(tools, names, host)
        if cmd == "watch":
            return cmd_watch(tools, names, interval, dry, force)
        return cmd_ship(tools, names, dry, force)
    if cmd == "restore":
        if not rest or rest[0] not in tools:
            print("usage: continuity.py restore <tool> [-- <tool args>]", file=sys.stderr)
            return 2
        name = rest[0]
        name_args = rest[1:]
        if name_args and name_args[0] == "--":
            name_args = name_args[1:]
        return tools[name].restore(STAGE / name, name_args)

    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
