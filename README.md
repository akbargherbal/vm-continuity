# vm-continuity

Keep state alive across **ephemeral VMs** (Colab and similar). Today it backs up
**OpenCode sessions** — per session, as tool-neutral files you can find, read, or
re-import later — and it is built to be tool-agnostic and **standalone**: no dependency
on any project repo.

- **Retrieval-first.** The goal is to come back to a *specific* session to consult it
  ("how did you arrive at that x value?") or to finish off interrupted work — not to keep a
  rolling copy of the latest state.
- **Neutral by default.** Each session is stored as `transcript.md` (reads forever, no
  coupling), `session.json` (lossless, re-importable), and `meta.json`. A single
  whole-store DB snapshot is kept as an optional fallback.
- **Independent.** Ships its own `gsutil` mover; does not use any project's backup script.

## Install

```bash
git clone <this repo> ~/vm-continuity && bash ~/vm-continuity/install.sh
```

`install.sh` registers the skill globally at `~/.config/opencode/skills/vm-continuity`
(so it is available in **every** project), installs OpenCode if missing, and drops a
`vm-continuity` shim on `~/.local/bin`.

On a fresh Colab VM, a project's `bootstrap/setup.sh` clones-or-pulls this repo and runs
`install.sh --no-opencode` as one of its parallel jobs (the project installs OpenCode
itself). The project only *fetches* the tool; the tool and its skill live here. For a
private repo the bootstrap needs a `GH_TOKEN`; a public repo clones non-interactively.

## Compatibility

Tested against OpenCode **1.18.x**. OpenCode's DB/CLI surface has shifted across
versions; `tools/opencode.py` adapts to whichever it finds:

| Area | Older | 1.18.x | 2.0.x |
|---|---|---|---|
| sessions table | `session_v2` | `session` | `session` — detected either way |
| session export/import | `opencode session export/import` | `opencode export/import` | `opencode session export/import` |

So a store captured on one version is still enumerable and restorable on another.
The tool **probes the CLI once** and caches which spelling this OpenCode speaks
(`_export_prefix()` / `_import_argv()` in `tools/opencode.py`); if OpenCode changes
again, those two functions — plus `_session_table()` — are the touch points.

> Captured on v2.0.22. Export is written to a file (never a pipe); a broken export
> leaves a small help-text file, so `CAPTURE.json`'s `sessions.exported` is the
> signal to check.

### Side artifacts (`snapshot/`)

opencode keeps a git snapshot of the workspace under `data_dir()/snapshot`; it is
small on small projects but can be **several GB** on a large one. The tool skips
side dirs larger than `CONTINUITY_SIDE_MAX_MB` (default **200 MB**) and records the
skip in `CAPTURE.json` `notes`, so a session backup stays tens of MB instead of
shipping the whole project. Raise the limit to include them.

## Use

```bash
vm-continuity list
vm-continuity capture            # consistent DB snapshot + per-session exports
vm-continuity ship [--dry-run]   # rsync this VM's namespace to GCS
vm-continuity hosts              # list VM namespaces in the store
vm-continuity pull [--host H]    # fetch a namespace to the staging area
vm-continuity restore opencode -- --mode db|export
vm-continuity status             # one-line health of the backup loop (exit 0 = healthy)
```

Env overrides: `CONTINUITY_GCS` (store base; the tool lands under `<base>/opencode_sessions/`)
and `CONTINUITY_STAGE` (ephemeral staging, default `/content/vm_state`).

## Layout

Each VM writes its **own namespace** (`by_host/<host>/`), so it can never clobber another
VM's store. The target retrieval-first layout (per-session folders + `index.json`) is
described in `PLAN.md`.

```
<CONTINUITY_GCS>/opencode_sessions/
  by_host/<host>/                    this VM's capture
    CAPTURE.json  opencode.db  config/  sessions/  snapshot/  tool-output/
  <legacy root>                      pre-namespace store, still restorable
```

## Design / build

See **`PLAN.md`** for the full design (artifact set, capture/ship/delete flows,
isolation guarantees, guards, cost, milestones) and `skills/vm-continuity/SKILL.md` for
the operating procedure.

## The one invariant

Capture must be *consistent*: snapshot live databases (`sqlite3.backup()`), never rsync
them. Ship the snapshot. Restore deliberately.
