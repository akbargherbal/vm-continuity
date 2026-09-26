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
    python continuity.py pull [tool ...]
    python continuity.py restore <tool> [-- <tool args>]
    python continuity.py watch [--interval-minutes N] [--dry-run] [tool ...]

Env overrides:
    CONTINUITY_STAGE   default /content/vm_state
    CONTINUITY_GCS     base bucket/prefix; a tool lands under <base>/<STORE>/
                       (default gs://akbar-december-2024-backup)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS_DIR = HERE / "tools"

STAGE = Path(os.environ.get("CONTINUITY_STAGE", "/content/vm_state"))
GCS = os.environ.get("CONTINUITY_GCS", "gs://akbar-december-2024-backup").rstrip("/")


def store_uri(tools: dict, name: str) -> str:
    """Where a tool's store lives: <base>/<STORE>/."""
    sub = getattr(tools[name], "STORE", name)
    return f"{GCS}/{sub}" if sub else GCS


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
        dst = store_uri(tools, name)
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


def cmd_watch(tools: dict, names: list[str], interval_minutes: float,
              dry_run: bool, force: bool) -> int:
    """capture + ship on a loop. Intended to be run detached (see SKILL.md)."""
    interval = max(60.0, interval_minutes * 60.0)
    print(f"[watch] capture+ship every {interval_minutes:.0f} min "
          f"(dry-run={dry_run}, force={force})")
    while True:
        cmd_capture(tools, names)
        cmd_ship(tools, names, dry_run, force)
        time.sleep(interval)


def cmd_pull(tools: dict, names: list[str]) -> int:
    rc = 0
    for name in names:
        dst = STAGE / name
        dst.mkdir(parents=True, exist_ok=True)
        src = store_uri(tools, name)
        cmd = ["gsutil", "-m", "rsync", "-r", src, str(dst)]
        print(f"[pull] {src} -> {dst}")
        if subprocess.run(cmd).returncode != 0:
            rc = 1
    return rc


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    tools = load_tools()

    if cmd == "list":
        return cmd_list(tools)
    if cmd in ("capture", "ship", "pull", "watch"):
        dry = "--dry-run" in rest
        force = "--force" in rest
        interval = 15.0
        if "--interval-minutes" in rest:
            i = rest.index("--interval-minutes")
            try:
                interval = float(rest[i + 1])
            except (IndexError, ValueError):
                print("--interval-minutes needs a number", file=sys.stderr)
                return 2
            rest = rest[:i] + rest[i + 2:]
        names = [a for a in rest if not a.startswith("-")] or list(tools)
        unknown = [n for n in names if n not in tools]
        if unknown:
            print(f"unknown tool(s): {', '.join(unknown)}", file=sys.stderr)
            return 2
        if cmd == "capture":
            return cmd_capture(tools, names)
        if cmd == "pull":
            return cmd_pull(tools, names)
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
