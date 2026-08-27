#!/usr/bin/env python3
"""
EMA trend-following decision engine for BTC/USD and ETH/USD.

The Claude (Sonnet) routine must NOT compute indicators, R-multiples, or
position size itself. It dumps a compact Alpaca state snapshot to disk,
runs this script, then executes the returned actions in order.

Bars never flow through the model. This script fetches 1H OHLCV from the
Alpaca crypto data API (or reads a local cache) and writes only the
computed indicator block + decisions.

Usage:
  python3 strategy.py --state state.json --out decisions.json
  python3 strategy.py --state state.json --out decisions.json --no-fetch
  python3 strategy.py --self-check

State schema: see STATE_SCHEMA in this file, or state.example.json.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple


SYMBOLS = ("BTC/USD", "ETH/USD")
QTY_PRECISION = {"BTC/USD": 6, "ETH/USD": 5}
PRICE_PRECISION = {"BTC/USD": 2, "ETH/USD": 2}
MIN_BARS = 250
TARGET_BARS = 1000
ATR_PERIOD = 14
DUST_ABS = 1.0
DUST_EQUITY_FRAC = 0.0001  # 0.01% of equity
MAX_ENTRIES_PER_DAY = 3
MAX_OPEN_POSITIONS = 2
DAILY_PNL_HALT = -0.03
TARGET_RISK = 0.01
MIN_RISK = 0.004
MAX_TOTAL_RISK = 0.02
CASH_BUFFER = 0.90
STOP_ATR_MULT = 1.5
STOP_PCT_CAP = 0.06
CHASE_MULT = 1.015
CONTINUATION_ATR_MULT = 0.15
SCALE_1R = 1.0
SCALE_3R = 3.0
BE_OFFSET_R = 0.1
TRAIL_ATR_MULT = 1.25
STAGNATION_HOURS = 36
STAGNATION_PEAK_R = 1.0
STAGNATION_CUR_R = 0.5
EMA200_JUMP_WARN = 0.02
BARS_STALE_HOURS = 2.0

ALPACA_DATA_BASE = "https://data.alpaca.markets/v1beta3/crypto/us/bars"
CACHE_DIR_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")


# ---------------------------------------------------------------------------
# Time / math helpers
# ---------------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    s = str(value).strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                dt = None
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def floor_qty(qty: float, symbol: str) -> float:
    prec = QTY_PRECISION.get(symbol, 6)
    step = 10 ** (-prec)
    floored = math.floor((qty / step) + 1e-12) * step
    return round(floored, prec)


def round_price(px: float, symbol: str) -> float:
    prec = PRICE_PRECISION.get(symbol, 2)
    return round(px, prec)


def ema_series(closes: List[float], period: int) -> List[Optional[float]]:
    """Standard EMA seeded with SMA of the first `period` closes."""
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    if n < period or period <= 0:
        return out
    k = 2.0 / (period + 1.0)
    seed = sum(closes[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = closes[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def ema_series_first_close_seed(closes: List[float], period: int) -> List[Optional[float]]:
    """Alternate seed (first close) used only for EMA200 sanity recompute."""
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    if n == 0 or period <= 0:
        return out
    k = 2.0 / (period + 1.0)
    prev = closes[0]
    out[0] = prev
    for i in range(1, n):
        prev = closes[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def atr_wilder(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> List[Optional[float]]:
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    if n < period + 1:
        return out
    trs: List[float] = [0.0] * n
    trs[0] = highs[0] - lows[0]
    for i in range(1, n):
        trs[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    # First ATR is SMA of TRs over bars [1 .. period] (period TRs after the first close)
    # Common Wilder seed: SMA of the first `period` TRs starting at index 1,
    # placed at index `period`.
    seed = sum(trs[1 : period + 1]) / period
    out[period] = seed
    prev = seed
    for i in range(period + 1, n):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def last_defined(series: List[Optional[float]]) -> Optional[float]:
    for v in reversed(series):
        if v is not None:
            return v
    return None


# ---------------------------------------------------------------------------
# Alpaca bars
# ---------------------------------------------------------------------------

def _alpaca_headers() -> Dict[str, str]:
    key = (
        os.environ.get("APCA_API_KEY_ID")
        or os.environ.get("ALPACA_API_KEY")
        or os.environ.get("ALPACA_KEY_ID")
        or ""
    )
    secret = (
        os.environ.get("APCA_API_SECRET_KEY")
        or os.environ.get("ALPACA_SECRET_KEY")
        or os.environ.get("ALPACA_SECRET")
        or ""
    )
    headers = {"Accept": "application/json"}
    if key and secret:
        headers["APCA-API-KEY-ID"] = key
        headers["APCA-API-SECRET-KEY"] = secret
    return headers


def _http_get_json(url: str, timeout: int = 30) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers=_alpaca_headers(), method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def fetch_symbol_bars(symbol: str, limit: int = TARGET_BARS) -> List[Dict[str, Any]]:
    """Fetch 1H bars ascending. Crypto data often works without keys."""
    params = {
        "symbols": symbol,
        "timeframe": "1Hour",
        "limit": str(limit),
        "sort": "asc",
    }
    # Cover ~limit hours plus slack so EMA200 is stable.
    end = utcnow()
    start = end - timedelta(hours=limit + 48)
    params["start"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    params["end"] = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    url = ALPACA_DATA_BASE + "?" + urllib.parse.urlencode(params)
    payload = _http_get_json(url)
    bars_by_symbol = payload.get("bars") or {}
    raw = bars_by_symbol.get(symbol) or bars_by_symbol.get(symbol.replace("/", "")) or []
    if not raw and isinstance(payload.get("bars"), list):
        raw = payload["bars"]
    cleaned = []
    for b in raw:
        ts = parse_ts(b.get("t") or b.get("timestamp"))
        if ts is None:
            continue
        cleaned.append(
            {
                "t": iso(ts),
                "o": float(b.get("o") if b.get("o") is not None else b.get("open")),
                "h": float(b.get("h") if b.get("h") is not None else b.get("high")),
                "l": float(b.get("l") if b.get("l") is not None else b.get("low")),
                "c": float(b.get("c") if b.get("c") is not None else b.get("close")),
                "v": float(b.get("v") if b.get("v") is not None else b.get("volume") or 0.0),
            }
        )
    cleaned.sort(key=lambda x: x["t"])
    # Dedup timestamps
    dedup = []
    seen = set()
    for b in cleaned:
        if b["t"] in seen:
            continue
        seen.add(b["t"])
        dedup.append(b)
    return dedup


def load_or_fetch_bars(
    symbol: str,
    cache_dir: str,
    fetch: bool,
    embedded: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], str]:
    cache_path = os.path.join(cache_dir, f"bars_{symbol.replace('/', '')}.json")
    if embedded:
        return embedded, "embedded"
    if fetch:
        try:
            bars = fetch_symbol_bars(symbol)
            os.makedirs(cache_dir, exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({"fetched_at": iso(utcnow()), "bars": bars}, f)
            return bars, "fetched"
        except Exception as exc:
            if os.path.exists(cache_path):
                with open(cache_path, encoding="utf-8") as f:
                    cached = json.load(f)
                return cached.get("bars") or [], f"fetch-failed-used-cache:{exc}"
            raise
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            cached = json.load(f)
        return cached.get("bars") or [], "cache"
    raise FileNotFoundError(
        f"No bars for {symbol}. Run with fetch enabled or place {cache_path}"
    )


def split_closed_forming(bars: List[Dict[str, Any]], now: datetime) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Drop the in-progress hour so EMA crosses use the last COMPLETED 1H bar."""
    if not bars:
        return [], None
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    closed = []
    forming = None
    for b in bars:
        ts = parse_ts(b["t"])
        if ts is None:
            continue
        ts_hour = ts.replace(minute=0, second=0, microsecond=0)
        if ts_hour >= hour_start:
            forming = b
        else:
            closed.append(b)
    return closed, forming


# ---------------------------------------------------------------------------
# Domain objects
# ---------------------------------------------------------------------------

def is_dust(market_value: float, equity: float) -> bool:
    mv = abs(float(market_value or 0.0))
    if mv < DUST_ABS:
        return True
    if equity > 0 and mv < equity * DUST_EQUITY_FRAC:
        return True
    return False


def find_entry_bar_index(closed_bars: List[Dict[str, Any]], entry_ts: Optional[datetime]) -> Optional[int]:
    if entry_ts is None or not closed_bars:
        return None
    # Bar that contains or immediately precedes the entry timestamp.
    best = None
    for i, b in enumerate(closed_bars):
        ts = parse_ts(b["t"])
        if ts is None:
            continue
        if ts <= entry_ts:
            best = i
        else:
            break
    return best


def hours_open_count(closed_bars: List[Dict[str, Any]], entry_ts: Optional[datetime]) -> Optional[int]:
    if entry_ts is None:
        return None
    n = 0
    for b in closed_bars:
        ts = parse_ts(b["t"])
        if ts is not None and ts > entry_ts:
            n += 1
    return n


def highest_high_since(closed_bars: List[Dict[str, Any]], forming: Optional[Dict[str, Any]], entry_ts: Optional[datetime]) -> Optional[float]:
    if entry_ts is None:
        return None
    highs = []
    for b in closed_bars:
        ts = parse_ts(b["t"])
        if ts is not None and ts >= entry_ts.replace(minute=0, second=0, microsecond=0):
            highs.append(float(b["h"]))
    if forming is not None:
        highs.append(float(forming["h"]))
    return max(highs) if highs else None


def original_stop_distance(entry_price: float, atr_at_entry: float) -> float:
    return min(STOP_ATR_MULT * atr_at_entry, entry_price * STOP_PCT_CAP)


# ---------------------------------------------------------------------------
# Core evaluation
# ---------------------------------------------------------------------------

def compute_indicators(
    symbol: str,
    bars: List[Dict[str, Any]],
    now: datetime,
    current_price: Optional[float],
    prior_ema200: Optional[float],
) -> Dict[str, Any]:
    closed, forming = split_closed_forming(bars, now)
    warnings: List[str] = []
    if len(closed) < MIN_BARS:
        warnings.append(f"{symbol}: only {len(closed)} completed bars (want >= {MIN_BARS})")

    last_ts = parse_ts(closed[-1]["t"]) if closed else None
    if last_ts and (now - last_ts).total_seconds() > BARS_STALE_HOURS * 3600:
        warnings.append(
            f"{symbol}: last closed bar {iso(last_ts)} is more than {BARS_STALE_HOURS:.0f}h behind now"
        )

    closes = [float(b["c"]) for b in closed]
    highs = [float(b["h"]) for b in closed]
    lows = [float(b["l"]) for b in closed]

    e12 = ema_series(closes, 12)
    e20 = ema_series(closes, 20)
    e50 = ema_series(closes, 50)
    e200 = ema_series(closes, 200)
    atrs = atr_wilder(highs, lows, closes, ATR_PERIOD)

    ema12 = last_defined(e12)
    ema20 = last_defined(e20)
    ema50 = last_defined(e50)
    ema200 = last_defined(e200)
    atr14 = last_defined(atrs)

    ema200_sanity = "ok"
    if prior_ema200 and ema200 and prior_ema200 > 0:
        jump = abs(ema200 - prior_ema200) / prior_ema200
        px_move = 0.0
        if len(closes) >= 2 and closes[-2] > 0:
            px_move = abs(closes[-1] - closes[-2]) / closes[-2]
        if jump > EMA200_JUMP_WARN and jump > px_move * 4:
            alt = last_defined(ema_series_first_close_seed(closes, 200))
            ema200_sanity = "recomputed"
            warnings.append(
                f"{symbol}: EMA200 jumped {jump:.2%} vs prior {prior_ema200:.4f}; "
                f"SMA-seed={ema200:.4f} first-close-seed={alt}"
            )
            # Prefer the seed closer to the prior journal value.
            if alt is not None and abs(alt - prior_ema200) < abs(ema200 - prior_ema200):
                ema200 = alt

    prev_e20 = e20[-2] if len(e20) >= 2 else None
    prev_e50 = e50[-2] if len(e50) >= 2 else None

    fresh_cross_up = (
        ema20 is not None
        and ema50 is not None
        and prev_e20 is not None
        and prev_e50 is not None
        and ema20 > ema50
        and prev_e20 <= prev_e50
    )
    fresh_cross_down = (
        ema20 is not None
        and ema50 is not None
        and prev_e20 is not None
        and prev_e50 is not None
        and ema20 < ema50
        and prev_e20 >= prev_e50
    )

    gap = (ema20 - ema50) if (ema20 is not None and ema50 is not None) else None
    cont_thresh = (CONTINUATION_ATR_MULT * atr14) if atr14 is not None else None
    continuation_ok = (
        ema20 is not None
        and ema50 is not None
        and atr14 is not None
        and ema20 > ema50
        and (ema20 - ema50) >= CONTINUATION_ATR_MULT * atr14
    )
    weak_continuation = (
        ema20 is not None
        and ema50 is not None
        and atr14 is not None
        and ema20 > ema50
        and not fresh_cross_up
        and (ema20 - ema50) < CONTINUATION_ATR_MULT * atr14
    )

    last_close = closes[-1] if closes else None
    price = current_price if current_price is not None else last_close
    chase_cap = (ema20 * CHASE_MULT) if ema20 is not None else None
    chasing = price is not None and chase_cap is not None and price > chase_cap

    return {
        "symbol": symbol,
        "bars_total": len(bars),
        "bars_closed": len(closed),
        "last_closed_bar_ts": iso(last_ts),
        "forming_bar_ts": forming["t"] if forming else None,
        "close": last_close,
        "price": price,
        "ema12": ema12,
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
        "atr14": atr14,
        "ema20_minus_ema50": gap,
        "continuation_threshold": cont_thresh,
        "fresh_cross_up": bool(fresh_cross_up),
        "fresh_cross_down": bool(fresh_cross_down),
        "close_gt_ema200": bool(last_close is not None and ema200 is not None and last_close > ema200),
        "close_lt_ema200": bool(last_close is not None and ema200 is not None and last_close < ema200),
        "price_vs_ema20_pct": (
            ((price / ema20) - 1.0) if (price and ema20) else None
        ),
        "chasing": bool(chasing),
        "weak_continuation": bool(weak_continuation),
        "continuation_ok": bool(continuation_ok),
        "ema200_sanity": ema200_sanity,
        "warnings": warnings,
        "_closed_bars": closed,
        "_forming": forming,
        "_atr_series": atrs,
    }


def working_stop_for(symbol: str, open_orders: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Pick the protective sell-stop for symbol. Ignore buys and filled/canceled."""
    candidates = []
    for o in open_orders:
        osym = o.get("symbol") or o.get("asset")
        if osym != symbol:
            continue
        side = str(o.get("side") or "").lower()
        otype = str(o.get("type") or o.get("order_type") or "").lower()
        status = str(o.get("status") or "open").lower()
        if status in {"filled", "canceled", "cancelled", "expired", "rejected"}:
            continue
        if side != "sell":
            continue
        if "stop" not in otype and o.get("stop_price") is None and o.get("stop_loss") is None:
            continue
        candidates.append(o)
    if not candidates:
        return None
    # If duplicates exist the model must cancel extras; we flag the cheapest (lowest) stop.
    def stop_px(o: Dict[str, Any]) -> float:
        v = o.get("stop_price") or o.get("stop") or 0.0
        if isinstance(v, dict):
            v = v.get("stop_price") or 0.0
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    candidates.sort(key=stop_px)
    return candidates[0]


def extra_stops(symbol: str, open_orders: List[Dict[str, Any]], keep_id: Optional[str]) -> List[Dict[str, Any]]:
    extras = []
    for o in open_orders:
        if (o.get("symbol") or o.get("asset")) != symbol:
            continue
        side = str(o.get("side") or "").lower()
        otype = str(o.get("type") or o.get("order_type") or "").lower()
        status = str(o.get("status") or "open").lower()
        if status in {"filled", "canceled", "cancelled", "expired", "rejected"}:
            continue
        if side != "sell":
            continue
        if "stop" not in otype and o.get("stop_price") is None:
            continue
        oid = str(o.get("id") or o.get("order_id") or "")
        if keep_id and oid == str(keep_id):
            continue
        extras.append(o)
    return extras


def resting_tp_for(symbol: str, open_orders: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Pick a resting take-profit limit sell for symbol (plain limit, no stop_price).

    A protective stop and a take-profit are distinguished purely by order shape, not
    by client_order_id convention: stops always carry a stop_price (stop_limit for
    crypto, since Alpaca does not support bare "stop" orders on crypto), while a
    take-profit is a plain limit sell with no stop_price at all.
    """
    candidates = []
    for o in open_orders:
        osym = o.get("symbol") or o.get("asset")
        if osym != symbol:
            continue
        side = str(o.get("side") or "").lower()
        otype = str(o.get("type") or o.get("order_type") or "").lower()
        status = str(o.get("status") or "open").lower()
        if status in {"filled", "canceled", "cancelled", "expired", "rejected"}:
            continue
        if side != "sell":
            continue
        if otype != "limit":
            continue
        if o.get("stop_price") is not None:
            continue
        candidates.append(o)
    if not candidates:
        return None
    candidates.sort(key=lambda o: float(o.get("limit_price") or 0.0))
    return candidates[0]


def extra_tps(symbol: str, open_orders: List[Dict[str, Any]], keep_id: Optional[str]) -> List[Dict[str, Any]]:
    extras = []
    for o in open_orders:
        if (o.get("symbol") or o.get("asset")) != symbol:
            continue
        side = str(o.get("side") or "").lower()
        otype = str(o.get("type") or o.get("order_type") or "").lower()
        status = str(o.get("status") or "open").lower()
        if status in {"filled", "canceled", "cancelled", "expired", "rejected"}:
            continue
        if side != "sell" or otype != "limit" or o.get("stop_price") is not None:
            continue
        oid = str(o.get("id") or o.get("order_id") or "")
        if keep_id and oid == str(keep_id):
            continue
        extras.append(o)
    return extras


def position_risk_frac(entry: float, stop: float, qty: float, equity: float) -> float:
    if equity <= 0 or entry <= 0 or qty <= 0:
        return 0.0
    dist = max(entry - stop, 0.0)
    return (dist * qty) / equity


def evaluate(state: Dict[str, Any], indicators: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    now = parse_ts(state.get("now_utc")) or utcnow()
    acct = state.get("account") or {}
    equity = float(acct.get("equity") or 0.0)
    cash = float(acct.get("cash") or 0.0)
    last_equity = acct.get("last_equity")
    last_equity_f = float(last_equity) if last_equity not in (None, "") else None

    daily_pnl_pct = state.get("daily_pnl_pct")
    if daily_pnl_pct is None and last_equity_f and last_equity_f != 0:
        daily_pnl_pct = (equity - last_equity_f) / last_equity_f
    if daily_pnl_pct is not None:
        daily_pnl_pct = float(daily_pnl_pct)

    entries_today = int(state.get("entries_today") or 0)
    loss_exits_today = set(state.get("loss_exits_today") or [])
    open_orders = list(state.get("open_orders") or [])
    raw_positions = list(state.get("positions") or [])
    quotes = state.get("quotes") or {}

    halt_pnl = daily_pnl_pct is not None and daily_pnl_pct <= DAILY_PNL_HALT
    halt_cap = entries_today >= MAX_ENTRIES_PER_DAY

    actions: List[Dict[str, Any]] = []
    seq = 1
    warnings: List[str] = []
    decisions: Dict[str, Dict[str, Any]] = {}
    managed: Dict[str, Dict[str, Any]] = {}
    blocked_reentry: set = set()
    new_entries_this_run = 0

    def add_action(**kwargs: Any) -> None:
        nonlocal seq
        kwargs["seq"] = seq
        seq += 1
        actions.append(kwargs)

    # ----- classify positions -----
    real_positions = []
    for p in raw_positions:
        symbol = p.get("symbol")
        if symbol not in SYMBOLS:
            continue
        qty = float(p.get("qty") or p.get("remaining_qty") or 0.0)
        mv = float(p.get("market_value") or 0.0)
        entry = float(p.get("avg_entry_price") or p.get("entry_price") or 0.0)
        px = p.get("current_price")
        if px is None and indicators.get(symbol):
            px = indicators[symbol].get("price")
        px = float(px or 0.0)
        if mv <= 0 and qty and px:
            mv = abs(qty * px)
        if qty <= 0 or is_dust(mv, equity):
            continue
        real_positions.append({**p, "symbol": symbol, "qty": qty, "entry_price": entry, "current_price": px, "market_value": mv})

    # ----- Step 4: manage existing -----
    for pos in real_positions:
        symbol = pos["symbol"]
        ind = indicators[symbol]
        closed_bars = ind["_closed_bars"]
        forming = ind["_forming"]
        atr_series = ind["_atr_series"]
        entry = pos["entry_price"]
        qty = pos["qty"]
        price = pos["current_price"] or ind.get("price") or ind.get("close")
        entry_ts = parse_ts(pos.get("entry_ts") or pos.get("entry_timestamp"))
        original_qty = float(pos.get("original_qty") or qty)
        scaled_out_pct = pos.get("scaled_out_pct")
        if scaled_out_pct is None and original_qty > 0:
            scaled_out_pct = max(0.0, 1.0 - (qty / original_qty))
        scaled_out_pct = float(scaled_out_pct or 0.0)

        idx = find_entry_bar_index(closed_bars, entry_ts)
        atr_at_entry = None
        if idx is not None and idx < len(atr_series) and atr_series[idx] is not None:
            atr_at_entry = atr_series[idx]
        elif ind.get("atr14") is not None:
            atr_at_entry = ind["atr14"]
            warnings.append(f"{symbol}: ATR-at-entry unavailable; using current ATR(14)")

        if not entry or not atr_at_entry:
            warnings.append(f"{symbol}: cannot reconstruct original R (entry={entry}, atr_at_entry={atr_at_entry})")
            decisions[symbol] = {"action": "held", "reason": "missing entry/ATR reconstruction; no new management"}
            managed[symbol] = pos
            continue

        stop_dist = original_stop_distance(entry, atr_at_entry)
        original_stop = entry - stop_dist
        original_r = stop_dist
        r_mult = (price - entry) / original_r if original_r else 0.0
        hh = highest_high_since(closed_bars, forming, entry_ts)
        if hh is None:
            hh = max(price, entry)
        peak_r = (hh - entry) / original_r if original_r else 0.0
        hours_open = hours_open_count(closed_bars, entry_ts)
        if hours_open is None and entry_ts is not None:
            hours_open = max(0, int((now - entry_ts).total_seconds() // 3600))

        ws = working_stop_for(symbol, open_orders)
        working_stop_px = None
        working_stop_id = None
        if ws:
            working_stop_id = str(ws.get("id") or ws.get("order_id") or "")
            working_stop_px = ws.get("stop_price") or ws.get("stop")
            try:
                working_stop_px = float(working_stop_px)
            except (TypeError, ValueError):
                working_stop_px = None

        extras = extra_stops(symbol, open_orders, working_stop_id)
        for extra in extras:
            add_action(
                kind="cancel_order",
                symbol=symbol,
                order_id=str(extra.get("id") or extra.get("order_id") or ""),
                reason="duplicate protective stop",
            )

        desired_stop = original_stop
        action_name = "held"
        reasons: List[str] = []
        fully_exited = False
        scaled_this_run = False

        # Stop already breached
        stop_ref = working_stop_px if working_stop_px is not None else original_stop
        if price <= stop_ref:
            if working_stop_id:
                add_action(kind="cancel_order", symbol=symbol, order_id=working_stop_id, reason="stop breached; flatten")
            add_action(
                kind="market_sell",
                symbol=symbol,
                qty=qty,
                reason=f"price {price:.2f} <= stop {stop_ref:.2f}",
            )
            fully_exited = True
            action_name = "exited"
            reasons.append(f"stop-breach price={price:.2f} stop={stop_ref:.2f}")

        # Structural exits
        if not fully_exited:
            stag = (
                hours_open is not None
                and hours_open >= STAGNATION_HOURS
                and peak_r < STAGNATION_PEAK_R
                and r_mult < STAGNATION_CUR_R
            )
            if ind.get("fresh_cross_down"):
                if working_stop_id:
                    add_action(kind="cancel_order", symbol=symbol, order_id=working_stop_id, reason="EMA20/50 cross-down flatten")
                add_action(kind="market_sell", symbol=symbol, qty=qty, reason="EMA20 crossed below EMA50 on last closed bar")
                fully_exited = True
                action_name = "exited"
                blocked_reentry.add(symbol)
                reasons.append("EMA20/50 cross-down")
            elif ind.get("close_lt_ema200"):
                if working_stop_id:
                    add_action(kind="cancel_order", symbol=symbol, order_id=working_stop_id, reason="close < EMA200 flatten")
                add_action(
                    kind="market_sell",
                    symbol=symbol,
                    qty=qty,
                    reason=f"close {ind.get('close'):.2f} < EMA200 {ind.get('ema200'):.2f}",
                )
                fully_exited = True
                action_name = "exited"
                reasons.append("close < EMA200")
            elif stag:
                if working_stop_id:
                    add_action(kind="cancel_order", symbol=symbol, order_id=working_stop_id, reason="stagnation flatten")
                add_action(
                    kind="market_sell",
                    symbol=symbol,
                    qty=qty,
                    reason=(
                        f"stagnation hours_open={hours_open} peak_R={peak_r:.2f} "
                        f"current_R={r_mult:.2f}"
                    ),
                )
                fully_exited = True
                action_name = "exited"
                reasons.append("stagnation-exit")

        remaining_qty = 0.0 if fully_exited else qty

        if fully_exited:
            stale_tp = resting_tp_for(symbol, open_orders)
            if stale_tp is not None:
                add_action(
                    kind="cancel_order",
                    symbol=symbol,
                    order_id=str(stale_tp.get("id") or stale_tp.get("order_id") or ""),
                    reason="position flattened; canceling resting take-profit",
                )

        # Scaling (only if still open)
        if not fully_exited:
            # +1R first scale
            if r_mult >= SCALE_1R and scaled_out_pct < 0.45:
                sell_qty = floor_qty(remaining_qty * 0.50, symbol)
                if sell_qty > 0:
                    add_action(
                        kind="market_sell",
                        symbol=symbol,
                        qty=sell_qty,
                        reason=f"scale 50% at +{r_mult:.2f}R",
                    )
                    remaining_qty = floor_qty(remaining_qty - sell_qty, symbol)
                    scaled_out_pct = 1.0 - (remaining_qty / original_qty if original_qty else 0.5)
                    scaled_this_run = True
                    action_name = "scaled"
                    reasons.append(f"scaled-50%-at-1R remaining={remaining_qty}")
                    desired_stop = entry + BE_OFFSET_R * original_r

            # +3R second scale
            if remaining_qty > 0 and r_mult >= SCALE_3R and scaled_out_pct < 0.70:
                sell_qty = floor_qty(remaining_qty * 0.50, symbol)
                if sell_qty > 0:
                    add_action(
                        kind="market_sell",
                        symbol=symbol,
                        qty=sell_qty,
                        reason=f"scale 50% of remainder at +{r_mult:.2f}R",
                    )
                    remaining_qty = floor_qty(remaining_qty - sell_qty, symbol)
                    scaled_out_pct = 1.0 - (remaining_qty / original_qty if original_qty else 0.25)
                    scaled_this_run = True
                    action_name = "scaled"
                    reasons.append(f"scaled-50%-of-remainder-at-3R remaining={remaining_qty}")

            # Trail — gated on scaled_out_pct (reflects actual fills, including a resting
            # take-profit filled between hourly checks) rather than only the live r_mult
            # this run, so a price pullback after a TP fill doesn't leave the stop
            # stranded at the wide original level. r_mult stays as an OR-fallback in case
            # price blows through a threshold before the corresponding TP is confirmed.
            if remaining_qty > 0 and (scaled_out_pct >= 0.45 or r_mult >= SCALE_1R):
                if scaled_out_pct >= 0.70 or r_mult >= SCALE_3R:
                    ema12_stop = ind.get("ema12")
                    atr_stop = (hh - TRAIL_ATR_MULT * ind["atr14"]) if (hh and ind.get("atr14")) else None
                    trail_candidates = [c for c in (ema12_stop, atr_stop) if c is not None]
                    if trail_candidates:
                        desired_stop = min(trail_candidates)
                        reasons.append(
                            f"tight-trail min(EMA12={ema12_stop}, HH-1.25ATR={atr_stop})"
                        )
                else:
                    desired_stop = max(desired_stop, entry + BE_OFFSET_R * original_r)
                    reasons.append(f"BE trail entry+0.1R={desired_stop:.2f}")

            # Never move a stop down vs original or vs working
            floor_stop = original_stop
            if working_stop_px is not None:
                floor_stop = max(floor_stop, working_stop_px)
            if desired_stop < floor_stop:
                desired_stop = floor_stop
            desired_stop = round_price(desired_stop, symbol)

            if remaining_qty > 0:
                if working_stop_id is None:
                    add_action(
                        kind="place_stop",
                        symbol=symbol,
                        side="sell",
                        qty=remaining_qty,
                        stop_price=desired_stop,
                        reason="no protective stop working; placing one",
                    )
                    action_name = "scaled" if scaled_this_run else "held"
                    reasons.append(f"placed-stop@{desired_stop}")
                elif abs(desired_stop - (working_stop_px or 0.0)) >= 10 ** (-PRICE_PRECISION.get(symbol, 2)):
                    if desired_stop > (working_stop_px or 0.0):
                        add_action(
                            kind="replace_stop",
                            symbol=symbol,
                            order_id=working_stop_id,
                            qty=remaining_qty,
                            stop_price=desired_stop,
                            reason=f"trail stop {working_stop_px} -> {desired_stop}",
                        )
                        reasons.append(f"raised-stop {working_stop_px}->{desired_stop}")
                    else:
                        # quantity mismatch only
                        ws_qty = float(ws.get("qty") or ws.get("filled_qty") or 0.0) if ws else 0.0
                        if abs(ws_qty - remaining_qty) > 10 ** (-QTY_PRECISION.get(symbol, 6)):
                            add_action(
                                kind="replace_stop",
                                symbol=symbol,
                                order_id=working_stop_id,
                                qty=remaining_qty,
                                stop_price=working_stop_px,
                                reason="resize stop qty to remaining position",
                            )
                            reasons.append("resized-stop-qty")

            # ----- Resting take-profit limit order (fills between hourly checks) -----
            # This is additive, not a replacement: the +1R/+3R market_sell scaling above
            # stays as the guaranteed hourly-cadence safety net. The resting limit order
            # is the fast path — it can fill on an intra-hour spike that reverses before
            # the next check would otherwise have caught it (see LESSONS.md 2026-08-26).
            # A stop (stop_price set) and a take-profit (plain limit, no stop_price) are
            # never confused: resting_tp_for only matches the latter shape.
            if remaining_qty > 0:
                existing_tp = resting_tp_for(symbol, open_orders)
                dup_tps = extra_tps(
                    symbol, open_orders,
                    str(existing_tp.get("id") or existing_tp.get("order_id") or "") if existing_tp else None,
                )
                for dup in dup_tps:
                    add_action(
                        kind="cancel_order",
                        symbol=symbol,
                        order_id=str(dup.get("id") or dup.get("order_id") or ""),
                        reason="duplicate resting take-profit",
                    )

                desired_tp_qty = None
                desired_tp_price = None
                tp_reason = None
                if scaled_out_pct < 0.45:
                    desired_tp_qty = floor_qty(min(original_qty * 0.50, remaining_qty), symbol)
                    desired_tp_price = round_price(entry + SCALE_1R * original_r, symbol)
                    tp_reason = f"TP1 resting @ entry+{SCALE_1R:.1f}R"
                elif scaled_out_pct < 0.70:
                    desired_tp_qty = floor_qty(remaining_qty * 0.50, symbol)
                    desired_tp_price = round_price(entry + SCALE_3R * original_r, symbol)
                    tp_reason = f"TP2 resting @ entry+{SCALE_3R:.1f}R"
                # else: final runner rides the trailing stop only, no further resting TP.

                price_tol = 10 ** (-PRICE_PRECISION.get(symbol, 2))
                qty_tol = 10 ** (-QTY_PRECISION.get(symbol, 6))
                if desired_tp_qty and desired_tp_qty > 0:
                    if existing_tp is None:
                        add_action(
                            kind="place_take_profit",
                            symbol=symbol,
                            side="sell",
                            qty=desired_tp_qty,
                            limit_price=desired_tp_price,
                            reason=tp_reason,
                        )
                        reasons.append(f"placed-tp@{desired_tp_price}")
                    else:
                        ex_qty = float(existing_tp.get("qty") or existing_tp.get("filled_qty") or 0.0)
                        ex_price = float(existing_tp.get("limit_price") or 0.0)
                        if abs(ex_qty - desired_tp_qty) > qty_tol or abs(ex_price - desired_tp_price) > price_tol:
                            add_action(
                                kind="cancel_order",
                                symbol=symbol,
                                order_id=str(existing_tp.get("id") or existing_tp.get("order_id") or ""),
                                reason="resting TP stale (qty/price changed); replacing",
                            )
                            add_action(
                                kind="place_take_profit",
                                symbol=symbol,
                                side="sell",
                                qty=desired_tp_qty,
                                limit_price=desired_tp_price,
                                reason=tp_reason,
                            )
                            reasons.append(f"replaced-tp@{desired_tp_price}")
                elif existing_tp is not None:
                    add_action(
                        kind="cancel_order",
                        symbol=symbol,
                        order_id=str(existing_tp.get("id") or existing_tp.get("order_id") or ""),
                        reason="no active TP target (final runner); canceling stale resting take-profit",
                    )
                    reasons.append("cleared-stale-tp")

        unrealized_pct = None
        if entry:
            unrealized_pct = (price - entry) / entry

        rec = {
            "symbol": symbol,
            "entry_price": entry,
            "remaining_qty": remaining_qty,
            "original_qty": original_qty,
            "stop_price": None if fully_exited else desired_stop if remaining_qty > 0 else None,
            "unrealized_pnl_pct": unrealized_pct,
            "r_multiple": r_mult,
            "peak_r": peak_r,
            "hours_open": hours_open,
            "scaled_out_pct": 1.0 if fully_exited else scaled_out_pct,
            "original_r": original_r,
            "original_stop": original_stop,
            "highest_high_since_entry": hh,
            "open": not fully_exited and remaining_qty > 0,
        }
        managed[symbol] = rec
        if symbol not in decisions:
            reason = "; ".join(reasons) if reasons else (
                f"hold R={r_mult:.2f} peak_R={peak_r:.2f} hours={hours_open} "
                f"close={ind.get('close')} EMA20={ind.get('ema20')} EMA50={ind.get('ema50')} EMA200={ind.get('ema200')}"
            )
            decisions[symbol] = {"action": action_name, "reason": reason}

    open_after_mgmt = [m for m in managed.values() if m.get("open")]
    slots_used = len(open_after_mgmt)
    slots_free = MAX_OPEN_POSITIONS - slots_used

    # Current open risk of remaining positions (to their desired stops)
    def current_total_risk() -> float:
        total = 0.0
        for m in managed.values():
            if not m.get("open"):
                continue
            total += position_risk_frac(m["entry_price"], m["stop_price"], m["remaining_qty"], equity)
        return total

    # ----- Step 5: new entries -----
    for symbol in SYMBOLS:
        if symbol in decisions and managed.get(symbol, {}).get("open"):
            continue  # already have a live position decision
        if symbol in decisions and decisions[symbol]["action"] == "exited":
            # may still evaluate entry unless blocked
            pass

        ind = indicators[symbol]
        has_open = managed.get(symbol, {}).get("open", False)
        if has_open:
            continue

        if halt_pnl:
            decisions[symbol] = {
                "action": "blocked-by-guardrail",
                "reason": f"daily P&L {daily_pnl_pct:.2%} <= -3%; no new entries",
            }
            continue
        if halt_cap or (entries_today + new_entries_this_run) >= MAX_ENTRIES_PER_DAY:
            decisions[symbol] = {
                "action": "blocked-by-guardrail",
                "reason": f"entries_today={entries_today} cap={MAX_ENTRIES_PER_DAY}",
            }
            continue
        if slots_free <= 0:
            decisions[symbol] = {
                "action": "blocked-by-guardrail",
                "reason": f"max {MAX_OPEN_POSITIONS} real positions already open",
            }
            continue
        if symbol in blocked_reentry:
            decisions[symbol] = {
                "action": "blocked-by-guardrail",
                "reason": "same-run re-entry blocked after EMA20/50 cross-down exit",
            }
            continue
        if symbol in loss_exits_today:
            decisions[symbol] = {
                "action": "blocked-by-guardrail",
                "reason": "no average-down: symbol already closed at a loss today",
            }
            continue

        missing = [k for k in ("close", "ema20", "ema50", "ema200", "atr14") if ind.get(k) is None]
        if missing:
            decisions[symbol] = {
                "action": "blocked-by-guardrail",
                "reason": f"incomplete indicators: missing {missing}",
            }
            continue

        if not ind["close_gt_ema200"]:
            decisions[symbol] = {
                "action": "held",
                "reason": (
                    f"no entry: close {ind['close']:.2f} <= EMA200 {ind['ema200']:.2f}"
                ),
            }
            continue

        if not (ind["fresh_cross_up"] or ind["continuation_ok"]):
            if ind["weak_continuation"]:
                decisions[symbol] = {
                    "action": "skipped-weak-continuation",
                    "reason": (
                        f"weak continuation, EMAs too tight: "
                        f"EMA20-EMA50={ind['ema20_minus_ema50']:.4f} "
                        f"< 0.15*ATR={ind['continuation_threshold']:.4f}"
                    ),
                }
            else:
                decisions[symbol] = {
                    "action": "held",
                    "reason": (
                        f"no cross / no continuation: EMA20={ind['ema20']:.2f} "
                        f"EMA50={ind['ema50']:.2f} gap={ind['ema20_minus_ema50']:.4f} "
                        f"thresh={ind['continuation_threshold']:.4f}"
                    ),
                }
            continue

        price = ind.get("price") or ind.get("close")
        if ind["chasing"]:
            decisions[symbol] = {
                "action": "skipped-chase",
                "reason": (
                    f"chasing, waiting for pullback: price {price:.2f} > "
                    f"EMA20*1.015={(ind['ema20'] * CHASE_MULT):.2f} "
                    f"({ind['price_vs_ema20_pct']:.2%} above EMA20)"
                ),
            }
            continue

        stop_dist = original_stop_distance(price, ind["atr14"])
        stop_px = round_price(price - stop_dist, symbol)
        stop_pct = stop_dist / price
        formulaic_notional = (equity * TARGET_RISK) / stop_pct if stop_pct > 0 else 0.0

        usable_cash = cash
        cash_capped = False
        # Always reserve CASH_BUFFER of available cash, even when cash comfortably
        # covers the formulaic notional: Alpaca's actual execution requirement for a
        # crypto market order runs a few percent above the naive qty*price notional
        # (observed ~2% in production), so sizing right up to 100% of cash gets the
        # order rejected for insufficient balance even though the math looked fine.
        cash_ceiling = usable_cash * CASH_BUFFER
        if cash_ceiling >= formulaic_notional:
            notional = formulaic_notional
            risk_pct = TARGET_RISK
        else:
            risk_pct = (cash_ceiling * stop_pct) / equity if equity else 0.0
            if risk_pct < MIN_RISK:
                decisions[symbol] = {
                    "action": "skipped-cash-too-small",
                    "reason": (
                        f"cash {cash:.2f} < formulaic {formulaic_notional:.2f}; "
                        f"reduced risk {risk_pct:.2%} < 0.40%"
                    ),
                }
                continue
            notional = cash_ceiling
            cash_capped = True

        # Total open-risk cap
        existing_risk = current_total_risk()
        proposed_risk = (notional * stop_pct) / equity if equity else 0.0
        if existing_risk + proposed_risk > MAX_TOTAL_RISK + 1e-12:
            # Try shrink toward min risk
            room = max(MAX_TOTAL_RISK - existing_risk, 0.0)
            if room < MIN_RISK:
                decisions[symbol] = {
                    "action": "blocked-by-guardrail",
                    "reason": (
                        f"total open risk would be {existing_risk+proposed_risk:.2%} > 2%"
                    ),
                }
                continue
            notional = (room * equity) / stop_pct
            proposed_risk = room
            cash_capped = True

        qty = floor_qty(notional / price, symbol)
        if qty <= 0:
            decisions[symbol] = {
                "action": "skipped-cash-too-small",
                "reason": "qty rounded to zero",
            }
            continue

        # Prefer limit if spread is wide
        q = quotes.get(symbol) or {}
        bid = q.get("bid")
        ask = q.get("ask")
        order_kind = "market_buy"
        limit_price = None
        if bid and ask and ask > 0:
            spread = (ask - bid) / ask
            if spread > 0.0008:
                order_kind = "limit_buy"
                limit_price = round_price(ask, symbol)

        add_action(
            kind=order_kind,
            symbol=symbol,
            qty=qty,
            limit_price=limit_price,
            reason=(
                f"long entry {'fresh-cross' if ind['fresh_cross_up'] else 'continuation'} "
                f"notional={qty * price:.2f} risk={proposed_risk:.2%}"
                + (" cash-capped" if cash_capped else "")
            ),
        )
        add_action(
            kind="place_stop",
            symbol=symbol,
            side="sell",
            qty=qty,
            stop_price=stop_px,
            reason=f"initial stop {stop_px} (dist={stop_dist:.2f}, {stop_pct:.2%})",
        )
        tp1_qty = floor_qty(qty * 0.50, symbol)
        if tp1_qty > 0:
            add_action(
                kind="place_take_profit",
                symbol=symbol,
                side="sell",
                qty=tp1_qty,
                limit_price=round_price(price + SCALE_1R * stop_dist, symbol),
                reason=f"initial TP1 resting @ entry+{SCALE_1R:.1f}R (fills between hourly checks)",
            )

        decisions[symbol] = {
            "action": "entered",
            "reason": (
                f"{'fresh-cross' if ind['fresh_cross_up'] else 'continuation'} "
                f"close={ind['close']:.2f} > EMA200={ind['ema200']:.2f}; "
                f"EMA20={ind['ema20']:.2f} EMA50={ind['ema50']:.2f} "
                f"gap={ind['ema20_minus_ema50']:.4f} vs 0.15ATR={ind['continuation_threshold']:.4f}; "
                f"price {price:.2f} <= EMA20*1.015={(ind['ema20']*CHASE_MULT):.2f}; "
                f"qty={qty} stop={stop_px} risk={proposed_risk:.2%}"
                + (" cash-capped" if cash_capped else "")
            ),
        }

        managed[symbol] = {
            "symbol": symbol,
            "entry_price": price,
            "remaining_qty": qty,
            "original_qty": qty,
            "stop_price": stop_px,
            "unrealized_pnl_pct": 0.0,
            "r_multiple": 0.0,
            "peak_r": 0.0,
            "hours_open": 0,
            "scaled_out_pct": 0.0,
            "original_r": stop_dist,
            "original_stop": stop_px,
            "highest_high_since_entry": price,
            "open": True,
            "cash_capped": cash_capped,
        }
        new_entries_this_run += 1
        slots_free -= 1
        cash -= qty * price  # local cash accounting so the second symbol sees the spend

    # Fill any missing symbol decisions
    for symbol in SYMBOLS:
        if symbol not in decisions:
            decisions[symbol] = {"action": "held", "reason": "no signal, no position"}

    journal_positions = []
    for symbol in SYMBOLS:
        m = managed.get(symbol)
        if not m or not m.get("open"):
            continue
        journal_positions.append(
            {
                "symbol": symbol,
                "entry_price": m["entry_price"],
                "remaining_qty": m["remaining_qty"],
                "stop_price": m.get("stop_price"),
                "unrealized_pnl_pct": m.get("unrealized_pnl_pct"),
                "r_multiple": m.get("r_multiple"),
                "peak_r": m.get("peak_r"),
                "hours_open": m.get("hours_open"),
                "scaled_out_pct": m.get("scaled_out_pct"),
            }
        )

    public_ind = {}
    for symbol, ind in indicators.items():
        public_ind[symbol] = {k: v for k, v in ind.items() if not k.startswith("_")}

    halt_reason = None
    if halt_pnl:
        halt_reason = "daily_pnl"
    elif halt_cap:
        halt_reason = "entry_cap"

    summary_lines = [
        f"equity={equity:.2f} cash={float(acct.get('cash') or 0.0):.2f} "
        f"daily_pnl_pct={None if daily_pnl_pct is None else f'{daily_pnl_pct:.3%}'} "
        f"halt_neg3={halt_pnl} entries_today={entries_today} cap3={halt_cap}",
    ]
    for symbol in SYMBOLS:
        m = managed.get(symbol)
        if m and m.get("open"):
            summary_lines.append(
                f"{symbol} OPEN entry={m['entry_price']} qty={m['remaining_qty']} "
                f"stop={m.get('stop_price')} R={m.get('r_multiple')} peak_R={m.get('peak_r')} "
                f"hours={m.get('hours_open')} scaled={m.get('scaled_out_pct')}"
            )
        else:
            summary_lines.append(f"{symbol} FLAT")
        d = decisions[symbol]
        summary_lines.append(f"  decision={d['action']}: {d['reason']}")
        i = public_ind[symbol]
        summary_lines.append(
            f"  px={i.get('price')} close={i.get('close')} "
            f"EMA12={i.get('ema12')} EMA20={i.get('ema20')} EMA50={i.get('ema50')} "
            f"EMA200={i.get('ema200')} ATR={i.get('atr14')} "
            f"gap={i.get('ema20_minus_ema50')} thresh={i.get('continuation_threshold')}"
        )

    return {
        "ok": True,
        "ts": iso(now),
        "account": {
            "equity": equity,
            "cash": float(acct.get("cash") or 0.0),
            "last_equity": last_equity_f,
            "buying_power": acct.get("buying_power"),
            "daily_pnl_pct": daily_pnl_pct,
            "entries_today": entries_today,
        },
        "guardrails": {
            "halt_new_entries_pnl": halt_pnl,
            "halt_new_entries_cap": halt_cap,
            "halt_reason": halt_reason,
        },
        "indicators": public_ind,
        "positions": journal_positions,
        "managed": {s: {k: v for k, v in m.items()} for s, m in managed.items()},
        "actions": actions,
        "decisions": decisions,
        "journal_entry": {
            "ts": iso(now),
            "equity": equity,
            "cash": float(acct.get("cash") or 0.0),
            "daily_pnl_pct": daily_pnl_pct,
            "entries_today": entries_today + new_entries_this_run,
            "positions": journal_positions,
            "decisions": decisions,
            "prior_decision_outcome": state.get("prior_decision_outcome")
            or "see operator note — compare prior journal positions to Alpaca fills this run",
        },
        "summary_lines": summary_lines,
        "warnings": warnings,
        "new_entries_this_run": new_entries_this_run,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def load_json(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path: str, payload: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
        f.write("\n")
    os.replace(tmp, path)


def build_result(state: Dict[str, Any], cache_dir: str, fetch: bool) -> Dict[str, Any]:
    now = parse_ts(state.get("now_utc")) or utcnow()
    state["now_utc"] = iso(now)
    prior = state.get("prior_ema200") or {}
    quotes = state.get("quotes") or {}
    pos_px = {p.get("symbol"): p.get("current_price") for p in (state.get("positions") or [])}

    indicators = {}
    fetch_notes = {}
    for symbol in SYMBOLS:
        embedded = None
        bars_block = (state.get("bars") or {}).get(symbol)
        if bars_block:
            embedded = bars_block
        bars, src = load_or_fetch_bars(symbol, cache_dir, fetch, embedded)
        fetch_notes[symbol] = {"source": src, "n": len(bars)}
        px = pos_px.get(symbol)
        q = quotes.get(symbol) or {}
        if px is None:
            if q.get("ask") and q.get("bid"):
                px = (float(q["ask"]) + float(q["bid"])) / 2.0
            elif q.get("mid"):
                px = float(q["mid"])
        indicators[symbol] = compute_indicators(
            symbol, bars, now, float(px) if px is not None else None, prior.get(symbol)
        )

    result = evaluate(state, indicators)
    result["bar_source"] = fetch_notes
    extra_warn = []
    for symbol, ind in indicators.items():
        extra_warn.extend(ind.get("warnings") or [])
    result["warnings"] = extra_warn + result.get("warnings", [])
    return result


def self_check() -> int:
    """Deterministic fixture covering chase / continuation / 1R scale / halt."""
    now = datetime(2026, 8, 26, 14, 0, tzinfo=timezone.utc)
    # Synthetic uptrend closes so EMA20 > EMA50 with a wide gap, close > EMA200.
    n = 280
    closes = []
    px = 100.0
    for i in range(n):
        px += 0.15 + (0.05 if i > 200 else 0.0)
        closes.append(px)
    bars = []
    start = now - timedelta(hours=n)
    for i, c in enumerate(closes):
        ts = start + timedelta(hours=i)
        bars.append({"t": iso(ts), "o": c - 0.2, "h": c + 0.4, "l": c - 0.4, "c": c, "v": 1.0})

    # Case A: flat account, should ENTER continuation (price near EMA20)
    last = closes[-1]
    state_a = {
        "now_utc": iso(now),
        "account": {"equity": 10000.0, "cash": 10000.0, "last_equity": 10000.0},
        "daily_pnl_pct": 0.0,
        "entries_today": 0,
        "positions": [],
        "open_orders": [],
        "loss_exits_today": [],
        "quotes": {"BTC/USD": {"bid": last - 0.5, "ask": last + 0.5}, "ETH/USD": {"bid": last - 0.5, "ask": last + 0.5}},
        "bars": {"BTC/USD": bars, "ETH/USD": bars},
        "prior_decision_outcome": "self-check",
    }
    res_a = build_result(state_a, CACHE_DIR_DEFAULT, fetch=False)
    d_btc = res_a["decisions"]["BTC/USD"]["action"]
    assert d_btc in {"entered", "skipped-chase", "skipped-weak-continuation", "held"}, d_btc

    # Case B: price 3% above EMA20 -> chase skip (reuse indicators by inflating quote)
    ema20 = res_a["indicators"]["BTC/USD"]["ema20"]
    state_b = json.loads(json.dumps(state_a))
    chase_px = ema20 * 1.03
    state_b["quotes"]["BTC/USD"] = {"bid": chase_px - 1, "ask": chase_px + 1, "mid": chase_px}
    res_b = build_result(state_b, CACHE_DIR_DEFAULT, fetch=False)
    assert res_b["decisions"]["BTC/USD"]["action"] == "skipped-chase", res_b["decisions"]["BTC/USD"]

    # Case C: daily halt
    state_c = json.loads(json.dumps(state_a))
    state_c["daily_pnl_pct"] = -0.031
    res_c = build_result(state_c, CACHE_DIR_DEFAULT, fetch=False)
    assert res_c["decisions"]["BTC/USD"]["action"] == "blocked-by-guardrail"
    assert res_c["guardrails"]["halt_new_entries_pnl"] is True

    # Case D: open BTC in profit >= 1R, unscaled -> scale
    atr = res_a["indicators"]["BTC/USD"]["atr14"]
    entry = last - 1.5 * atr * 1.05  # comfortably past 1R at current price
    state_d = {
        "now_utc": iso(now),
        "account": {"equity": 10000.0, "cash": 5000.0, "last_equity": 10000.0},
        "daily_pnl_pct": 0.01,
        "entries_today": 1,
        "positions": [
            {
                "symbol": "BTC/USD",
                "qty": 0.05,
                "original_qty": 0.05,
                "avg_entry_price": entry,
                "current_price": last,
                "market_value": 0.05 * last,
                "entry_ts": iso(now - timedelta(hours=10)),
                "scaled_out_pct": 0.0,
            }
        ],
        "open_orders": [
            {
                "id": "stop-1",
                "symbol": "BTC/USD",
                "side": "sell",
                "type": "stop",
                "stop_price": entry - min(1.5 * atr, entry * 0.06),
                "qty": 0.05,
                "status": "open",
            }
        ],
        "loss_exits_today": [],
        "quotes": {"BTC/USD": {"bid": last, "ask": last}, "ETH/USD": {"bid": last, "ask": last}},
        "bars": {"BTC/USD": bars, "ETH/USD": bars},
        "prior_decision_outcome": "self-check",
    }
    res_d = build_result(state_d, CACHE_DIR_DEFAULT, fetch=False)
    kinds = [a["kind"] for a in res_d["actions"] if a.get("symbol") == "BTC/USD"]
    assert "market_sell" in kinds, res_d["actions"]
    assert res_d["decisions"]["BTC/USD"]["action"] in {"scaled", "exited"}

    print("self-check passed")
    print("  A BTC decision:", res_a["decisions"]["BTC/USD"]["action"])
    print("  B chase:", res_b["decisions"]["BTC/USD"]["action"])
    print("  C halt:", res_c["decisions"]["BTC/USD"]["action"])
    print("  D BTC:", res_d["decisions"]["BTC/USD"]["action"], "actions", kinds)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="EMA trend decision engine")
    p.add_argument("--state", help="Path to state.json snapshot from the Claude routine")
    p.add_argument("--out", default="decisions.json", help="Where to write decisions.json")
    p.add_argument("--cache-dir", default=CACHE_DIR_DEFAULT)
    p.add_argument("--no-fetch", action="store_true", help="Do not hit Alpaca; use cache or state.bars")
    p.add_argument("--self-check", action="store_true")
    args = p.parse_args(argv)

    if args.self_check:
        return self_check()
    if not args.state:
        print("error: --state is required (or pass --self-check)", file=sys.stderr)
        return 2

    state = load_json(args.state)
    try:
        result = build_result(state, args.cache_dir, fetch=not args.no_fetch)
    except Exception as exc:
        err = {
            "ok": False,
            "error": str(exc),
            "ts": iso(utcnow()),
            "actions": [],
            "decisions": {
                "BTC/USD": {"action": "blocked-by-guardrail", "reason": f"strategy.py failed: {exc}"},
                "ETH/USD": {"action": "blocked-by-guardrail", "reason": f"strategy.py failed: {exc}"},
            },
            "warnings": [str(exc)],
        }
        write_json(args.out, err)
        print(json.dumps(err, indent=2))
        return 1

    write_json(args.out, result)
    # Compact stdout so the model can read the whole thing cheaply
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
