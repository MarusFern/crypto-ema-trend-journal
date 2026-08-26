# Sonnet-ready EMA trend routine

`strategy.py` owns every indicator, R reconstruction, size, and entry/exit boolean.
The Claude Sonnet 5 routine only fetches Alpaca account/position/order state, runs the script, executes the returned actions, and journals.

This is the refactor Claude recommended: bars never pass through the model.

## Files

| file | what |
|------|------|
| `strategy.py` | Decision engine. Stdlib only. Fetches its own 1H bars. |
| `CLAUDE_ROUTINE_PROMPT.md` | Drop-in replacement for the old long Claude instructions prompt. |
| `state.example.json` | Shape of the snapshot the routine must write before calling the script. |

## Put `strategy.py` where the hourly job can see it

Copy `strategy.py` into the journal repo root (`crypto-ema-trend-journal`) so every scheduled run has it after `git checkout main`.

```bash
cp strategy.py /path/to/crypto-ema-trend-journal/strategy.py
```

Commit it once on `main`. After that the routine should not edit it.

## What the Sonnet job does each hour

1. Checkout `main`, read `journal.jsonl` + `LESSONS.md`.
2. Pull account, activities, positions, open orders, latest quotes from the Alpaca MCP connector. **No bars.**
3. Write `state.json` (see the example).
4. Run:

```bash
python3 strategy.py --state state.json --out decisions.json
```

5. Execute `decisions.json` → `actions` in `seq` order via `place_crypto_order` / cancel / replace.
6. Append `journal_entry` to `journal.jsonl`, commit, `git push origin HEAD:main`.

## Optional env for bar fetch

Crypto historical bars often work unauthenticated. If Alpaca starts requiring keys, export paper credentials:

```bash
export APCA_API_KEY_ID=...
export APCA_API_SECRET_KEY=...
```

Bars are cached under `strategy.py`'s `.cache/` directory so a retry does not refetch.

## Sanity check

```bash
python3 strategy.py --self-check
```

## Why this is cheaper / safer on Sonnet 5

The old prompt forced the model to pull ~250–1000 bars per symbol through MCP and then re-emit them into a Python heredoc. That paid for every bar twice (tool input + model output) and put ~40 interacting branches on the model's back.

Now the model sees a few dozen lines of account state plus a compact `decisions.json`. Adherence work remains (tool sequence, stop placement, journal push). Signal work does not.
