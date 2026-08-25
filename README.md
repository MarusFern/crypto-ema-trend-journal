# Crypto EMA Trend Hourly Paper — Trading Journal

This repo is the persistent memory for the "Crypto EMA Trend Hourly Paper" cloud routine
(Alpaca paper account #PA3X8XF6J20K). Each hourly run is a fresh, isolated cloud session
with no memory of prior runs — this repo is how it reconstructs history and context.

## Files

- `journal.jsonl` — one JSON object per line, appended every run. This is the append-only
  source of truth: account state, decisions made, and reasoning for that run.
- `LESSONS.md` — a short, curated, human-and-agent-readable list of observations the agent
  has noticed across runs (e.g. "entries taken right at the edge of the pullback filter
  tended to underperform"). This file is advisory only.

## Rules for the agent

- **Read** the last ~20 lines of `journal.jsonl` and all of `LESSONS.md` at the start of
  every run, before evaluating anything, to have context on recent decisions and outcomes.
- **Append** exactly one new line to `journal.jsonl` at the end of every run — never edit
  or delete past lines. It is an immutable log.
- **`LESSONS.md` may only be appended to, and only when a genuinely new, well-evidenced
  observation emerges** (e.g. a repeated pattern across 3+ runs, or a clear postmortem on
  a losing trade). Do not add speculative or single-data-point notes.
- **The strategy's hard rules (EMA/ATR/position sizing/guardrails) are fixed by the human
  operator and defined in the routine's own prompt.** This journal and its lessons are for
  *context and observation only* — the agent must never use `LESSONS.md` to silently alter
  entry/exit/sizing rules. If a lesson suggests a rule should change, say so explicitly in
  the run summary so the human can review and decide — do not self-modify the strategy.
- Always `git pull` before reading (in case of a race with a manual edit), and
  `git add`, `commit`, `push` after appending. If push fails (e.g. non-fast-forward),
  pull/rebase and retry once; if it still fails, note the failure in the run summary
  rather than losing the entry.
