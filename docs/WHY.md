# Why back up OpenCode sessions (and what's good enough)

*Written 2026-09-26 on a CPU-only Colab VM. This file is purpose-first: it exists to
decide which trade-offs are acceptable, not to specify the implementation. The
mechanics live in `skills/vm-continuity/SKILL.md` and `continuity/` in the repo.*

## Candidate uses, ranked by realistic value here

1. **Resume an in-flight session on a new VM.** The reason this came up. The value is
   the agent's *working memory* — decisions made mid-task, exact commands, half-finished
   reasoning, the specific error — so a new session doesn't re-derive it.
2. **Recover work the VM death ate** — a finding from a tool call, the command that
   worked, a conclusion reached but never written down. This is #1's shadow: insurance
   against losing un-durable work.
3. **Accountability / reproducibility** — what the agent actually ran and why. Largely
   already covered by run logs, sidecars, and manifests in GCS.
4. **Searchable personal history** — "how did we solve X months ago." The repo already
   does this for the project via `DECISIONS.md` / `PROGRESS.md`.

## The uncomfortable competition

This repo already keeps a **curated** memory: `DECISIONS.md`, `PROGRESS.md`,
`agent_notes/current.md`. That is deliberate — the session is a *means*; the docs are
the *memory*. So the backup's real job is narrower than it feels:

> **Don't lose the most recent working session, and don't lose findings not yet
> distilled into those docs.**

That's uses #1 and #2. Uses #3 and #4 are mostly already served elsewhere.

## If the job is #1/#2, the requirements are

- **Complete and consistent** — a truncated transcript is worthless (this is exactly the
  `session export`-on-a-pipe bug that had to be fixed).
- **One-command restore** — if restoring is fiddly, it fails when you actually need it.
- **Freshness + origin visible** — you must be able to tell "this is current, and which
  VM it came from," or you risk restoring stale state.
- **The fresh VM must not clobber the good copy before you restore it.**

## Trade-offs that are fine (what we can drop)

- **Full dated history / searchable archive** — only needed for use #4, which the docs
  largely cover. Drop unless there's a real need to search old sessions.
- **Per-pass snapshots** — never. ~21 MB every 15 min is ~**60 GB/month**. That's the line.
- **Per-session JSON exports** — cheap, and the version-change fallback. Keep.
- **`snapshot/` and `tool-output/`** — small; keep or drop, not load-bearing.

## What we can't drop

- Consistency, completeness (child/subagent sessions), and **simple restore**.
- **Anti-clobber — the real risk.** A rolling `latest/` that always overwrites has a
  nasty failure mode: a fresh VM that hasn't restored yet, with the 15-min loop already
  running, ships its own near-empty DB and **destroys the exact backup you needed.**
  This is the most likely way to lose the very thing this effort protects — it's not a
  history nicety, it's the core risk.

## Good enough, in my view

- `latest/` every 15 min → resume.
- **One dated `snapshots/YYYY-MM-DD/` per day, kept ~30 days** → simultaneously the
  clobber insurance *and* light history. ~21 MB/day ≈ **600 MB/month**. Fine.
- Provenance in `CAPTURE.json` (timestamp, label, host, repo `HEAD`) → so "latest" is
  trustworthy and you know its origin.
- A **guard**: refuse to ship if the new capture has materially fewer sessions than what
  is already remote, unless `--force`.
- Nothing else. No index, no per-pass history, no search — until a use demands it.

This is roughly 20% of the design that serves ~90% of uses #1/#2, and the daily snapshot
is what makes it *safe* rather than merely cheap.

## The one decision to make

Is the job **"resume / recover recent work"** (then the above is the whole thing), or
**"keep a searchable archive of everything"** (then add an index + longer retention)?
My bet is the former — but it's the user's call.
