"""opencode continuity tool: consistent capture + restore.

Contract (see ../README.md):
    NAME = "opencode"
    capture(stage: Path, log) -> dict     # writes STAGE/opencode/, returns manifest
    restore(stage: Path, argv: list) -> int

Why this is not just an rsync: the live `opencode.db` is SQLite in WAL mode and is
written continuously. Copying it (or `gsutil rsync`-ing its directory) copies the
db/-wal/-shm at different instants and can yield a torn database. We take a
consistent snapshot with the sqlite3 backup API instead.

Sessions are enumerated from the snapshot, not from `opencode session list`:
`session list` is project-scoped and top-level-only, so it silently hides child
(subagent) sessions. `select id from session_v2` catches every one, and
`opencode export <id>` works from any working directory.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

NAME = "opencode"
# Store folder under CONTINUITY_GCS (see continuity.py: store_uri).
STORE = "opencode_sessions"

# Never copy these out of the data dir into the snapshot: the WAL/shm are folded
# into the consistent snapshot, and logs are noisy diagnostic-only data.
_SKIP_DATA = {"opencode.db-wal", "opencode.db-shm", "log"}
# Never restore this: it is machine-local (holds the server password/pid).
_SKIP_CONFIG = {"service.json"}

# opencode keeps a git snapshot of the workspace under data_dir()/snapshot; it is
# small on small projects but can be many GB on a large one. Skip side dirs above
# this size unless the operator raises CONTINUITY_SIDE_MAX_MB.
SIDE_MAX_BYTES = int(float(os.environ.get("CONTINUITY_SIDE_MAX_MB", "200")) * 1024 * 1024)


def _session_table(con: sqlite3.Connection) -> str:
    """opencode renamed its sessions table across versions (session_v2 -> session);
    pick whichever this DB actually has."""
    names = {r[0] for r in con.execute(
        "select name from sqlite_master where type='table'")}
    if "session_v2" in names:
        return "session_v2"
    if "session" in names:
        return "session"
    raise sqlite3.OperationalError("no session table (looked for session_v2, session)")


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def data_dir() -> Path:
    return _home() / ".local/share/opencode"


def config_dir() -> Path:
    return _home() / ".config/opencode"


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def _opencode(*args: str) -> subprocess.CompletedProcess | None:
    exe = shutil.which("opencode")
    if not exe:
        return None
    try:
        return subprocess.run(
            [exe, *args], capture_output=True, text=True, timeout=300
        )
    except Exception:
        return None


_EXPORT_PREFIX: list[str] | None = None
_IMPORT_SESSION_SUB: bool | None = None


def _export_prefix() -> list[str]:
    """`opencode session export` (v2.0+) vs top-level `opencode export` (older).

    Detect once; this is the touch point noted in README.md.
    """
    global _EXPORT_PREFIX
    if _EXPORT_PREFIX is None:
        probe = _opencode("session", "export", "--help")
        _EXPORT_PREFIX = (
            ["session", "export"] if (probe and probe.returncode == 0) else ["export"]
        )
    return _EXPORT_PREFIX


def _import_argv(src: Path, directory: Path) -> list[str]:
    """`opencode session import <file> --directory <dir>` (v2.0+) vs `opencode import <file>`."""
    global _IMPORT_SESSION_SUB
    if _IMPORT_SESSION_SUB is None:
        probe = _opencode("session", "import", "--help")
        _IMPORT_SESSION_SUB = bool(probe and probe.returncode == 0)
    if _IMPORT_SESSION_SUB:
        return ["session", "import", str(src), "--directory", str(directory)]
    return ["import", str(src)]


def _run_to_file(args: list[str], dest: Path) -> int | None:
    """Run opencode with stdout redirected to a real file, returning its exit code.

    `opencode export` truncates non-deterministically when stdout is a *pipe*
    (the same session yielded 260 KB / 671 KB / 1.4 MB / 2.2 MB across runs),
    but is byte-stable when stdout is a file. Never route export through a
    pipe, and never use `_opencode` (capture_output=True) for it.
    """
    exe = shutil.which("opencode")
    if not exe:
        return None
    try:
        with open(dest, "wb") as f:
            res = subprocess.run(
                [exe, *args], stdout=f, stderr=subprocess.PIPE, timeout=300
            )
        return res.returncode
    except Exception:
        return None


def _snapshot_db(db: Path, dest: Path) -> None:
    """Consistent copy of a live SQLite DB via the backup API."""
    if dest.exists():
        dest.unlink()
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _copytree_fresh(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def capture(stage: Path, log=print) -> dict:
    """Capture opencode state into `stage` (already STAGE/opencode)."""
    stage.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "tool": NAME,
        "captured": dt.datetime.now().isoformat(timespec="seconds"),
        "source": {"data_dir": str(data_dir()), "config_dir": str(config_dir())},
        "files": {},
        "notes": [],
    }

    ver = _opencode("--version")
    manifest["opencode_version"] = (
        ver.stdout.strip() if ver and ver.returncode == 0 else None
    )

    # 1. Consistent DB snapshot.
    db = data_dir() / "opencode.db"
    snap = stage / "opencode.db"
    if db.exists():
        _snapshot_db(db, snap)
        manifest["files"]["opencode.db"] = {
            "bytes": snap.stat().st_size,
            "sha256": _sha256(snap),
        }
        log(f"[opencode] db snapshot: {snap.stat().st_size} bytes")
    else:
        manifest["notes"].append("no opencode.db present")

    # 2. Config (never service.json).
    cdest = stage / "config"
    if cdest.exists():
        shutil.rmtree(cdest)
    cdest.mkdir(parents=True, exist_ok=True)
    if config_dir().is_dir():
        for p in sorted(config_dir().iterdir()):
            if p.name in _SKIP_CONFIG or p.suffix not in (".json", ".jsonc"):
                continue
            shutil.copy2(p, cdest / p.name)
            manifest["files"][f"config/{p.name}"] = {"bytes": p.stat().st_size}

    # 3. Per-session exports for EVERY session (incl. child/subagent ones).
    ids: list[str] = []
    if snap.exists():
        con = sqlite3.connect(str(snap))
        try:
            ids = [r[0] for r in con.execute(
                f"select id from {_session_table(con)} order by time_created")]
        finally:
            con.close()
    sdest = stage / "sessions"
    if sdest.exists():
        shutil.rmtree(sdest)
    sdest.mkdir(parents=True, exist_ok=True)
    exported, failed = 0, []
    for sid in ids:
        dest = sdest / f"{sid}.json"
        rc = _run_to_file([*_export_prefix(), sid], dest)
        if rc == 0 and dest.exists() and dest.stat().st_size > 0:
            exported += 1
        else:
            failed.append(sid)
    manifest["sessions"] = {"total": len(ids), "exported": exported, "failed": failed}
    if ids and _opencode("--version") is None:
        manifest["notes"].append("opencode not on PATH: DB captured, session exports skipped")

    # 4. Side artifacts (contingency). opencode's workspace snapshot git dir can be
    #    many GB; skip oversized side dirs rather than shipping the whole project.
    for sub in ("snapshot", "tool-output"):
        s = data_dir() / sub
        if not s.is_dir():
            continue
        size = _dir_size(s)
        if size > SIDE_MAX_BYTES:
            manifest["notes"].append(
                f"skipped {sub}/ ({size} bytes > {SIDE_MAX_BYTES} limit; "
                f"raise CONTINUITY_SIDE_MAX_MB to include)"
            )
            continue
        _copytree_fresh(s, stage / sub)
        manifest["files"][f"{sub}/"] = "dir"

    (stage / "CAPTURE.json").write_text(json.dumps(manifest, indent=2) + "\n")
    log(f"[opencode] captured {exported}/{len(ids)} sessions -> {stage}")
    return manifest


def _restore_db(stage: Path, mode_args: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="continuity restore opencode")
    ap.add_argument("--db-path", default=None, help="target opencode.db (default: opencode debug paths db)")
    a = ap.parse_args(mode_args)

    snap = stage / "opencode.db"
    if not snap.exists():
        print(f"no snapshot at {snap}; run `pull opencode` first", file=__import__("sys").stderr)
        return 3

    explicit = a.db_path is not None
    target = Path(a.db_path) if explicit else None
    if target is None:
        res = _opencode("debug", "paths", "db")
        target = Path(res.stdout.strip()) if res and res.returncode == 0 else data_dir() / "opencode.db"

    # Only touch the live background service when restoring the real store; an
    # explicit --db-path is an alternate/test target and must not disturb it.
    if not explicit:
        _opencode("service", "stop")  # best effort
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        bak = target.with_name(target.name + ".bak-" + dt.datetime.now().strftime("%Y%m%d%H%M%S"))
        shutil.copy2(target, bak)
        print(f"backed up existing db -> {bak}")
    shutil.copy2(snap, target)
    for side in ("-wal", "-shm"):
        p = Path(str(target) + side)
        if p.exists():
            p.unlink()

    # config (never service.json)
    csrc = stage / "config"
    if csrc.is_dir():
        config_dir().mkdir(parents=True, exist_ok=True)
        for p in csrc.iterdir():
            if p.name in _SKIP_CONFIG:
                continue
            shutil.copy2(p, config_dir() / p.name)
        print(f"restored config -> {config_dir()}")

    _opencode("service", "start") if not explicit else None
    print(f"restored db -> {target}")
    print("verify: cd <repo> && opencode session list   then   opencode -s <session-id>")
    return 0


def _restore_export(stage: Path, mode_args: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="continuity restore opencode")
    ap.add_argument("--directory", default=os.getcwd(),
                    help="existing project dir to import into (default: cwd)")
    a = ap.parse_args(mode_args)

    directory = Path(a.directory).expanduser()
    if not directory.is_dir():
        print(f"--directory does not exist: {directory}", file=__import__("sys").stderr)
        return 3
    sdir = stage / "sessions"
    files = sorted(sdir.glob("*.json")) if sdir.is_dir() else []
    if not files:
        print(f"no session exports at {sdir}", file=__import__("sys").stderr)
        return 3

    # Import parents before their children: a child session fails while its parent
    # is absent. parent_id lives in the DB snapshot, not the exported JSON.
    parent_of: dict[str, str | None] = {}
    db = stage / "opencode.db"
    if db.exists():
        con = sqlite3.connect(str(db))
        try:
            parent_of = {r[0]: r[1] for r in con.execute(
                f"select id, parent_id from {_session_table(con)}")}
        finally:
            con.close()
    files.sort(key=lambda f: 0 if not parent_of.get(f.stem) else 1)

    def _import(f: Path) -> bool:
        res = _opencode(*_import_argv(f, directory))
        return bool(res and res.returncode == 0)

    imported, failed = 0, []
    for f in files:
        if _import(f):
            imported += 1
        else:
            failed.append(f)
    if failed:  # retry now that every parent has been imported
        retry, failed = list(failed), []
        for f in retry:
            if _import(f):
                imported += 1
            else:
                failed.append(f)
    print(f"imported {imported}/{len(files)} sessions into {directory}")
    if failed:
        print("failed:", ", ".join(f.stem for f in failed))
    return 0 if not failed else 1


def restore(stage: Path, argv: list[str]) -> int:
    """Dispatch to the db/export restore path.

    Accepts BOTH documented spellings:

        restore opencode -- --mode db|export ...
        restore opencode -- db|export ...

    and tolerates one extra ``--`` before the mode's own options
    (``-- --mode export -- --directory DIR``). No mode given means ``db``.
    """
    argv = list(argv)
    if argv and argv[0] == "--":            # tolerate the outer "--"
        argv = argv[1:]
    if argv and argv[0] == "--mode":
        if len(argv) >= 2 and argv[1] in ("db", "export"):
            mode, rest = argv[1], argv[2:]
        else:
            print("--mode must be one of: db, export", file=sys.stderr)
            return 2
    elif argv and argv[0] in ("db", "export"):
        mode, rest = argv[0], argv[1:]
    else:
        mode, rest = "db", argv
    if rest and rest[0] == "--":            # tolerate an inner "--" before opts
        rest = rest[1:]
    if mode == "export":
        return _restore_export(stage, rest)
    return _restore_db(stage, rest)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2 or sys.argv[1] not in ("capture", "restore"):
        print("usage: python tools/opencode.py capture | restore [db|export] [opts]")
        raise SystemExit(2)
    st = Path(os.environ.get("CONTINUITY_STAGE", "/content/vm_state")) / NAME
    if sys.argv[1] == "capture":
        capture(st)
    else:
        raise SystemExit(restore(st, sys.argv[2:]))
