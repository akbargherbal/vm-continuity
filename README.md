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

On a fresh Colab VM the launching notebook should clone this repo and run `install.sh`
(one line), independent of any project's `setup.sh`.

## Use

```bash
vm-continuity list
vm-continuity capture            # consistent DB snapshot + per-session exports
vm-continuity ship [--dry-run]   # rsync the staging folder to GCS
vm-continuity pull               # fetch the store to the staging area
vm-continuity restore opencode -- --mode db|export
```

Env overrides: `CONTINUITY_GCS` (store base; the tool lands under `<base>/opencode_sessions/`)
and `CONTINUITY_STAGE` (ephemeral staging, default `/content/vm_state`).

## Layout

```
<CONTINUITY_GCS>/opencode_sessions/
  index.json                         catalog (merged; regenerable)
  sessions/<YYYYMMDD>_<id>/          one folder per session
    transcript.md  session.json  meta.json
    children/                        subagent sessions, nested
  config/                            ~/.config/opencode (service.json excluded)
  db/opencode.db                     optional whole-store fallback
```

## Design / build

See **`PLAN.md`** for the full design (artifact set, capture/ship/delete flows,
isolation guarantees, guards, cost, milestones) and `skills/vm-continuity/SKILL.md` for
the operating procedure.

## The one invariant

Capture must be *consistent*: snapshot live databases (`sqlite3.backup()`), never rsync
them. Ship the snapshot. Restore deliberately.
