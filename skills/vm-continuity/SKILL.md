---
name: vm-continuity
description: Keep state alive across ephemeral VMs (Colab and similar). Use when the user is worried about losing sessions or state when a VM/disconnect resets, wants to back up, find, read, or restore OpenCode (agent) sessions, carry state to a new/fresh VM, or asks "will this survive a restart". Trigger words - ephemeral, Colab, VM reset, lost session, continuity, back up sessions, restore session, find a session, new VM, fresh VM, disconnect, opencode sessions, survives restart.
---

# VM continuity (ephemeral VMs)

Everything under `/content` **and `/root`** dies when a Colab VM stops. Only GitHub and
GCS persist. This skill is the procedure for deciding what must survive, capturing it
*consistently*, shipping it to a store, and getting a **specific session** back later.

Standalone repo `vm-continuity`; it ships its own `gsutil` mover and does **not** depend
on any project's scripts. `install.sh` registers this skill globally at
`~/.config/opencode/skills/vm-continuity`, so it works in every project.

> **Implementation status.** `PLAN.md` is the design of record. The code currently
> captures a consistent DB snapshot + per-session exports into a single staging folder
> (prototype). The retrieval-first layout below (one folder per session, `index.json`,
> `fetch`/`delete`/`prune`) is the target and is not fully implemented yet. Read `PLAN.md`
> before relying on commands marked *(planned)*.

## The model

| Lives in | Survives VM stop? | Notes |
|---|---|---|
| `/content/**` (repo tree, datasets, outputs) | No | |
| `/root/**` (`~/.local/share/opencode`, `~/.config/opencode`, HF cache) | No | Home is the same ephemeral disk |
| GitHub | Yes | The repo *definition* (code, docs, skills) |
| GCS | Yes | Run artifacts, datasets, and this continuity store |

Bias toward capturing what is expensive to regenerate; record cheap/deterministic things
as regenerable instead (see inventory).

## What we keep, and why (retrieval-first)

The purpose is **not** to resume the latest state. It is to come back to a *specific*
session to:

- **consult** it — "how did you arrive at that x value?" (needs only the transcript), or
- **resume / finish off** interrupted work — e.g. 6/8 tracks done, VM died (needs the
  re-importable session **and** the run-state prefix, e.g. `_runs_status.log`).

So the durable artifact is the **per-session** record; the whole-store DB is only a fallback.

## Stateful inventory

One row per thing worth carrying; `README.md` explains the row format. Add a row when you
add a tool.

| Thing | Local source | Precious? | How it is captured | How it is restored |
|---|---|---|---|---|
| `opencode` | `~/.local/share/opencode`, `~/.config/opencode` | Yes — sessions, config | `vm-continuity capture opencode` (consistent DB snapshot + per-session exports) | `pull` then `restore opencode -- --mode db\|export` |
| repo (git) | the project tree | Yes | `git push` (not this tool) | `git clone` |
| HF cache | `~/.cache/huggingface` | No — regenerable | don't | re-warm via project bootstrap |
| training/inference artifacts | project output dirs | Yes | the *project's own* daemon (external to this tool) | run-specific GCS restore |

## Three-stage contract

Capture and transport are **separate jobs**:

1. **Capture** — tool-specific, must be *consistent*. For a live SQLite DB that means
   `sqlite3.backup()`, never `rsync` (rsync copies `db`/`-wal`/`-shm` at different instants
   → torn DB). Writes to the ephemeral staging area `/content/vm_state/<tool>/`.
2. **Ship / pull** — generic, this repo's own `gsutil -m rsync -r -c` mover. No project
   script involved. rsync is append/update (no `-d`): it never deletes.
3. **Restore** — tool-specific, deliberate, never automatic.

## Store layout

```
<CONTINUITY_GCS>/opencode_sessions/       (CONTINUITY_GCS + tool STORE)
  index.json                         catalog (merged; regenerable)
  sessions/<YYYYMMDD>_<id>/          one folder per session
    transcript.md                    primary, tool-neutral — reads forever
    session.json                     lossless, re-importable (OpenCode format)
    meta.json                        title/dates/project/parent/counts/version
    children/                        subagent sessions, nested (one fetch gets all)
  config/                            ~/.config/opencode (service.json excluded)
  db/opencode.db                     optional whole-store fallback (schema-coupled)
```

`transcript.md` is rendered from `session.json`, so the *archive* has zero schema coupling;
only the renderer is coupled, at capture time.

## Commands

```bash
vm-continuity list
vm-continuity capture [tool ...]
vm-continuity ship [--dry-run] [tool ...]
vm-continuity pull [tool ...]
vm-continuity restore opencode -- --mode db          # exact (needs db/ fallback)
vm-continuity restore opencode -- --mode export -- --directory <dir>   # portable
# planned: fetch <id|prefix> | delete <id> | prune --before <date> | reindex
```

Env: `CONTINUITY_GCS` (base; tool lands under `<base>/opencode_sessions/`),
`CONTINUITY_STAGE` (default `/content/vm_state`).

## Find & fetch one session

```bash
grep -i "<what you remember>" index.json      # find the folder
gsutil -m cp -r <CONTINUITY_GCS>/opencode_sessions/sessions/<YYYYMMDD>_<id>/ /content/fetched/
# consult: read transcript.md    resume: opencode session import …/session.json --directory <repo>
```

## Delete / prune (isolated)

Per-session folders are **independent**: deleting one never affects another or future
captures. The only shared file is `index.json`, which is mutated by **merge/prune** (never
overwritten from a fresh VM) and is regenerable from the per-session `meta.json` files.
`db/opencode.db` is monolithic — replaced whole or omitted, never pruned per session.

## Consistency rules (hard-won)

- **Never rsync a live SQLite DB.** Snapshot with `sqlite3.backup()`; ship the snapshot.
- **Enumerate sessions from the DB, not `opencode session list`** (project-scoped,
  top-level-only — it hides child/subagent sessions). `select id from session_v2` catches all.
- **Export to a file, never through a pipe.** `opencode session export` truncates
  non-deterministically on a pipe (same session: 260 KB / 671 KB / 1.4 MB / 2.2 MB across
  runs) but is byte-stable to a file.
- **`session import` ignores `OPENCODE_DB`** unless `--standalone`; import parents before
  children (a child fails while its parent is absent).
- **Record the tool version** (`meta.json`/`CAPTURE.json`); DB schemas are version-coupled.
  The per-session JSON is the version-robust fallback.
- **`session import` re-homes** the session to the target project (new `projectID`); the
  session *ID* is preserved, so `opencode -s <id>` works after import.
- **Secrets.** Prefer env-based auth; the DB can still contain prompts/tool output. Keep
  the bucket private.

## Adding a tool (extension recipe)

1. Create `tools/<tool>.py` exposing `NAME`, optional `STORE`, `capture(stage, log) -> dict`,
   and `restore(stage, argv) -> int`. Copy `tools/opencode.py` as the template.
2. Add an inventory row above.
3. Nothing else — `continuity.py` discovers tools by filename.

If OpenCode is replaced, `tools/opencode.py` is dropped and the skill/mover are untouched.

## Verify — don't claim it's backed up, check

- `vm-continuity list` prints each tool's `CAPTURE.json` summary (sessions, sizes, version).
- Remote: compare `gsutil ls -l <store>/…/opencode.db` against the local mtime and report
  the drift. Cold-VM restore is the honest proof; until run, state it as untested.
