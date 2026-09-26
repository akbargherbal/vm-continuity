# Plan — per-session OpenCode backup (retrieval-first, standalone repo)

Status: **proposed, not implemented.** Supersedes the rolling-DB prototype now in
`tools/opencode.py`. Purpose/rationale: `/content/opencode-backup.md` (ephemeral). In brief:
retrieve a past session to **consult** it or **finish off** work — not to resume the latest state.

## Packaging — its own repo, independent of this project

Decision (2026-09-26): the tool is a **standalone, tool-agnostic repo**, not part of
`maqamrock-yue2-lora-finetuning`.

- **No dependency on this project — including `backup_to_gcp.py`.** The repo ships its own
  minimal mover (`gsutil -m rsync -r -c`). `backup_to_gcp.py` stays here, untouched, and
  is *not* used by continuity.
- **Global availability:** `install.sh` registers the skill globally by symlinking
  `~/.config/opencode/skills/vm-continuity` → `<repo>/skills/vm-continuity`, so OpenCode
  advertises it in **every** project (OpenCode's global skill source).
- **Fresh VM:** clone the repo and run `install.sh` (adds the global skill link, checks/
  installs OpenCode, prints next steps). The launching notebook gets one clone+install
  line, alongside its existing project `setup.sh` line. The two repos are independent.
- **Config:** `CONTINUITY_GCS` (store root) and `CONTINUITY_STAGE` from env, like
  `GCP_BACKUP_BASE`; the notebook exports them. No silent personal-bucket default.
- **Removed from this project** (all currently untracked): `skills/vm-continuity/`,
  `continuity/`, `.claude/skills/vm-continuity` → moved to the new repo. This project keeps
  at most a one-line pointer in `agent_notes/`/docs.
- **GitHub:** creating the remote and first push need the user's `gh` auth (the same PAT
  flow as this repo). The agent scaffolds locally; the user creates/pushes.

Repo layout:

```
vm-continuity/
  README.md
  PLAN.md                       # this file
  install.sh                    # global skill link + opencode check/install
  continuity.py                 # dispatcher: capture/ship/pull/fetch/restore/delete/prune/reindex/list
  tools/opencode.py             # one tool module (the pattern for future tools)
  skills/vm-continuity/SKILL.md
  tests/
```

## Goals / non-goals

**Goals**
- Find and fetch **one specific session**, by title/date, with a plain `gsutil cp`.
- **Read** it forever (`transcript.md`) with no dependency on OpenCode.
- **Resume/finish-off** it (`session.json` + `opencode session import`).
- Survive OpenCode schema/format drift: the durable record must not be a binary DB.
- Deleting old archives must be isolated and must not affect new sessions.
- Work across **any** project on any VM.

**Non-goals**
- Whole-store "latest state" mirroring (the prototype).
- Dated snapshots / per-pass history / search infrastructure.
- Replacing or depending on this project's `backup_to_gcp.py`.

## GCS layout (final)

```
<CONTINUITY_GCS>/                     e.g. gs://akbar-december-2024-backup/opencode_sessions
  index.json                          # merged catalog; regenerable from metas
  sessions/
    20260920_ses_f243389d.../         # <YYYYMMDD-created>_<stable session id>
      meta.json
      transcript.md                   # primary, tool-neutral
      session.json                    # lossless, re-importable
      children/
        20260920_ses_f23f4a056..../   # same three files, nested (one fetch gets all)
  config/
    opencode.json / opencode.jsonc
  db/
    opencode.db                       # optional full-fidelity fallback (default ON)
    meta.json                         # version + captured_at + source
  CAPTURE.json                        # store-level provenance of the last pass
```

Sessions are **independently addressable**; children nest under the parent so one
`gsutil cp -r` of the parent retrieves the whole thread.

## Artifact formats

| File | Role | Coupling |
|---|---|---|
| `transcript.md` | human-readable record + provenance header (id, title, dates, repo HEAD, version) | **none** — reads forever |
| `session.json` | `opencode session export`; the only re-importable artifact | OpenCode export format |
| `meta.json` | title, dates, project dir, parent, message count, bytes, source host/label, OpenCode version, repo HEAD | none |
| `index.json` | catalog: one entry per session (parents + children) with `path` | none |
| `db/opencode.db` | full-fidelity whole-store fallback, version-tagged | OpenCode DB schema |

`transcript.md` is rendered from `session.json` — no DB-schema reader in the archive path,
so the archive has zero schema coupling.

## Capture flow

1. Consistent `sqlite3.backup()` of the live `opencode.db` → temp (never rsync a live DB).
2. Enumerate **all** session ids from the snapshot (`select id from session_v2`) — not
   `opencode session list` (project-scoped, hides children).
3. Per session: `opencode session export <id>` with **stdout redirected to a file**
   (pipes truncate non-deterministically).
4. Render `session.json` → `transcript.md`.
5. Write `meta.json`; nest children under their parent.
6. Copy `~/.config/opencode/*.json(c)` (minus `service.json`).
7. Optionally refresh the `db/` snapshot.

## Ship flow (own mover)

- `gsutil -m rsync -r -c` the session folders — checksum upload → only changed files;
  **no `-d`**, so nothing is deleted remotely.
- **Index merge, not overwrite:** `gsutil cat` the remote `index.json`, upsert entries
  keyed by session id, write back. Never drop entries not seen locally. `reindex`
  regenerates entirely from remote `meta.json` files.
- Replace `db/opencode.db` in place (single file, not dated).
- Write `CAPTURE.json`.

## Retrieve / restore flows

- **Consult (primary):** `gsutil -m cp -r <store>/sessions/<date>_<id>/ /content/fetched/`
  → read `transcript.md`.
- **Resume:** `opencode session import …/session.json --directory <repo>` →
  `opencode -s <session-id>` (id preserved). Import parents before children.
- **Finish-off:** fetch the session **and** point the agent at the run-state prefix
  (e.g. inference `out/`, sidecars, `_runs_status.log`).

## Delete / prune

- `delete <session-id>`: remove its folder from the store (and children if a parent), drop
  its index entries, rewrite the index. **Isolation guarantee:** session folders are
  independent; deleting one never affects another.
- `prune --before YYYY-MM-DD [--dry-run] [--keep N]`: delete folders + index entries.
- `db/opencode.db` is monolithic — replaced whole or omitted (`--no-db`), never pruned
  per session.
- Index is the only shared file; all mutation goes through merge/prune so it never shrinks
  from a fresh VM.

## Guards

- Refuse to ship if the local session count is materially below the remote index count
  unless `--force` (protects catalog/DB; the folders are already safe).
- Tolerate a missing OpenCode data/config dir (fresh VM).
- Record OpenCode version everywhere; never assume import compatibility.

## Cadence + cost

- Detached 15-min loop: capture → render → ship. Cheap (only changed sessions upload).
- Measured basis (real data; 3.8–4.0 bytes/token JSON, markdown ≈ 0.8× JSON): a 500k-token
  session ≈ 4–4.5 MB neutral. **5/day × 30 days ≈ 0.7 GB/month** neutral, **+~1–1.5 GB**
  for the single DB fallback → **~2 GB/month**. GCS ≈ $0.02/GB/month → cents.

## Test plan

- Local: capture → confirm `transcript.md` renders and contains a known fact; build
  `index.json`; fetch one session; import into a temp DB (`OPENCODE_DB` + `--standalone`);
  `delete`/`prune --dry-run` against a scratch prefix.
- Isolation: delete a copied session folder in a scratch prefix; index updates; others
  untouched.
- Cold VM (the real proof): clone → install → pull → consult + import + `opencode -s`
  *in a project other than this one*, to prove cross-project independence.
- Storage: verify the month extrapolation.

## Open questions

1. **Repo name:** `vm-continuity` (recommended) or something else?
2. **DB fallback default:** ON (assumed, ~2 GB/month is fine) or OFF?
3. **Index:** merge-on-write *and* regenerable — confirm.
4. **`db/` refresh cadence:** every pass, hourly, or on demand?
5. **Default label** for provenance: hostname, or a user `--label`?
6. **Invocation:** a `vm-continuity` shim on PATH (from `install.sh`) vs `python <repo>/continuity.py`?
7. **Repo visibility:** private (recommended — sessions can contain sensitive content).

## Milestones

0. Create the standalone repo + `install.sh` (global skill link); **move**
   `skills/vm-continuity/` + `continuity/` out of this project; remove the project
   `.claude/skills/vm-continuity` symlink; leave a one-line pointer.
1. Per-session capture + `transcript.md` renderer + local `index.json`.
2. `ship` with index merge + `pull`/`fetch` (dry-run GCS first).
3. `delete` / `prune` / `reindex`.
4. `db/` fallback + guards.
5. Update `SKILL.md`/`README.md`; wire the 15-min loop; commit (push needs the user's PAT).
6. Cold-VM restore proof in a non-project directory.
