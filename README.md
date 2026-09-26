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

## Use

```bash
vm-continuity list
vm-continuity capture            # consistent DB snapshot + per-session exports
vm-continuity ship [--dry-run]   # rsync this VM's namespace to GCS
vm-continuity hosts              # list VM namespaces in the store
vm-continuity pull [--host H]    # fetch a namespace to the staging area
vm-continuity restore opencode -- --mode db|export
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
