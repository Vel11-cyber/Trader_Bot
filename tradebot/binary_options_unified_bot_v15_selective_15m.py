# -*- coding: utf-8 -*-
"""
Unified Binary Options Bot v15 selective 15m diagnostics: Binarium + Pocket Option + Deriv + Trend Pullback.

Update v6 SELECTIVE 15M:
- Default live mode is 15m selective paper trading.
- Whitelist from the 15m Forex replay: only best strict strategies are actionable.
- Optional near_4 live entries are allowed only for EUR/GBP PUT/FALL.
- Use --all-strategies to disable the whitelist and behave like v14.

Update v5 EXPIRY TEST:
- Added --expiration-minutes for 15m/30m replay and live paper testing.
- Added --compare-expirations to compare 15m vs 30m replay summaries.
- Added --forex-only / --platforms / --exclude-categories filters.
- Added optional --replay-near-min-score to backtest near-signal candidates.

Update v4 FIX:
- Restored missing calc_result() and calc_profit(); paper trades can close again.
- Live watcher now chooses the latest actually CLOSED 15m candle instead of always len(data)-2.
- One Yahoo download per symbol per loop via data_cache, instead of repeated downloads for duplicate strategies.
- Added yfinance retries and last-candle diagnostics.

Update v3:
- Added anti-spam / no-overlap cooldown.
- The same platform + strategy cannot open another paper trade until the cooldown window is over.
- Optional --cooldown-minutes controls this behavior.

Update v2:
- Fixed live watcher threshold for stock indices (^DJI, ^GSPC).
- 112 candles are enough for EMA100 + closed candle check, so the old 120-candle limit was too strict.

Что делает:
1. REPLAY:
   - Находит исторические сигналы по всем текущим стратегиям.
   - Сразу считает результат через выбранную экспирацию (--expiration-minutes) по внешним данным Yahoo Finance.
   - Пишет CSV с результатами.

2. LIVE WATCHER:
   - Один общий watcher вместо трёх PowerShell.
   - Проверяет все стратегии.
   - Отправляет свежие сигналы в Telegram.
   - Автоматически открывает "paper trade" в журнале.
   - Через выбранную экспирацию автоматически закрывает paper trade по внешним данным.
   - НЕ нажимает кнопки на сайтах и НЕ открывает реальные сделки.

Важно:
- Это безопасная авто-проверка сигналов, а не автоторговля на платформе.
- Источник сигналов: Yahoo Finance proxy data.
- Платформенные котировки Binarium/Pocket/Deriv надо сверять отдельно вручную.
- Входить реальными деньгами нельзя. Только demo/paper.

Установка:
    pip install yfinance pandas numpy

Replay v15 selective 15m:
    python binary_options_unified_bot_v15_selective_15m.py --replay --replay-days 60

Replay all strategies 15m Forex-only:
    python binary_options_unified_bot_v15_selective_15m.py --replay --replay-days 60 --expiration-minutes 15 --forex-only --all-strategies

Compare 15m vs 30m:
    python binary_options_unified_bot.py --replay --compare-expirations --replay-days 60 --forex-only

Разовая live-проверка:
    python binary_options_unified_bot.py --once --sound --telegram --notify-on-start

Постоянный selective 15m бот:
    python binary_options_unified_bot_v15_selective_15m.py --live --sound --telegram --notify-on-start

Telegram переменные PowerShell:
    $env:TELEGRAM_BOT_TOKEN="твой_токен"
    $env:TELEGRAM_CHAT_ID="твой_chat_id"
"""

import argparse
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

import numpy as np
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    raise SystemExit(
        "Не установлен yfinance. Установи так:\n"
        "pip install yfinance pandas numpy"
    )


INTERVAL_MINUTES = 15
EXPIRATION_BARS = 2
EXPIRATION_MINUTES = 30
BET_SIZE = 10.0
CLOSED_CANDLE_LAG_MINUTES = 1.0
YFINANCE_RETRIES = 3
YFINANCE_RETRY_SLEEP_SECONDS = 2
YFINANCE_TIMEOUT_SECONDS = 20


def calc_result(direction_side: str, entry_price: float, exit_price: float) -> str:
    """
    Binary/paper result from external Yahoo prices.

    UP   wins if exit > entry.
    DOWN wins if exit < entry.
    Equal price is treated as draw with zero P/L.
    """
    side = str(direction_side).strip().upper()
    entry = float(entry_price)
    exit_ = float(exit_price)

    if np.isclose(entry, exit_):
        return "draw"
    if side == "UP":
        return "win" if exit_ > entry else "loss"
    if side == "DOWN":
        return "win" if exit_ < entry else "loss"
    raise ValueError(f"Unknown direction_side: {direction_side!r}")


def calc_profit(result: str, payout_pct: float, stake: float) -> float:
    """Return net paper P/L for one binary option trade."""
    result = str(result).strip().lower()
    payout = float(payout_pct)
    stake = float(stake)

    if result == "win":
        return round(stake * payout / 100.0, 2)
    if result == "loss":
        return round(-stake, 2)
    if result == "draw":
        return 0.0
    raise ValueError(f"Unknown result: {result!r}")




def configure_expiration(expiration_minutes: int):
    """Set runtime expiration globally. Supports 15m candle multiples: 15, 30, 45..."""
    global EXPIRATION_MINUTES, EXPIRATION_BARS

    expiration_minutes = int(expiration_minutes)
    if expiration_minutes <= 0:
        raise ValueError("--expiration-minutes должен быть положительным числом")
    if expiration_minutes % INTERVAL_MINUTES != 0:
        raise ValueError(
            f"--expiration-minutes должен делиться на {INTERVAL_MINUTES}, "
            f"например 15 или 30. Получено: {expiration_minutes}"
        )

    EXPIRATION_MINUTES = expiration_minutes
    EXPIRATION_BARS = expiration_minutes // INTERVAL_MINUTES


def _parse_csv_arg(value: str) -> set[str]:
    if not value:
        return set()
    return {x.strip().lower() for x in str(value).split(",") if x.strip()}



# v15 selective whitelist from the user's 15m Forex replay.
# Key = (platform, strategy). Keep this narrow until more paper data confirms it.
V15_STRICT_WHITELIST = {
    ("Pocket Option", "GBP/USD CALL"),
    ("Binarium", "GBP/USD CALL"),
    ("Pocket Option", "USD/JPY PUT"),
    ("Pocket Option", "EUR/GBP PUT"),
    ("Deriv", "USD/JPY FALL"),
    ("Pocket Option", "USD/JPY CALL"),
    ("Deriv", "USD/JPY RISE"),
}

# Near signals are much noisier in the replay. Only these EUR/GBP near_4 entries
# were good enough to keep in PAPER/live testing.
V15_NEAR4_WHITELIST = {
    ("Pocket Option", "EUR/GBP PUT"),
    ("Deriv", "EUR/GBP FALL"),
}


def _strategy_key(obj: dict) -> tuple[str, str]:
    return (str(obj.get("platform", "")), str(obj.get("strategy", "")))


def _is_v15_selected_strategy(strategy: dict) -> bool:
    key = _strategy_key(strategy)
    return key in V15_STRICT_WHITELIST or key in V15_NEAR4_WHITELIST


def _is_v15_allowed_signal(row: dict, signal_type: str) -> bool:
    key = _strategy_key(row)
    signal_type = str(signal_type or "").lower()
    if signal_type == "strict":
        return key in V15_STRICT_WHITELIST
    if signal_type.startswith("near_"):
        return key in V15_NEAR4_WHITELIST
    return False


def selected_strategies(args) -> list[dict]:
    """Filter strategy list without editing STRATEGIES manually."""
    strategies = list(STRATEGIES)

    # v15 default: run only the curated 15m candidates. Use --all-strategies to disable.
    if not getattr(args, "all_strategies", False):
        strategies = [s for s in strategies if _is_v15_selected_strategy(s)]

    if getattr(args, "forex_only", False):
        strategies = [s for s in strategies if str(s.get("category", "")).lower() == "forex"]

    platforms = _parse_csv_arg(getattr(args, "platforms", ""))
    if platforms:
        strategies = [s for s in strategies if str(s.get("platform", "")).lower() in platforms]

    exclude_categories = _parse_csv_arg(getattr(args, "exclude_categories", ""))
    if exclude_categories:
        strategies = [s for s in strategies if str(s.get("category", "")).lower() not in exclude_categories]

    include_families = _parse_csv_arg(getattr(args, "strategy_families", ""))
    if include_families:
        strategies = [s for s in strategies if str(s.get("strategy_family", "mean_reversion")).lower() in include_families]

    return strategies


def apply_v15_live_policy(row: dict, args) -> dict:
    """
    Convert raw v14 signal diagnostics into the v15 selective live decision.

    - Strict signals are actionable only if strategy is in V15_STRICT_WHITELIST.
    - Near signals are promoted to actionable only for V15_NEAR4_WHITELIST and
      only when near_score >= --live-near-min-score and signal age is fresh.
    - --all-strategies restores v14 behavior.
    """
    if getattr(args, "all_strategies", False):
        if row.get("is_actionable"):
            row["signal_type"] = "strict"
            row["filter_decision"] = "all_strategies_strict"
        else:
            row["signal_type"] = ""
            row["filter_decision"] = "all_strategies_no_signal"
        return row

    was_strict_actionable = bool(row.get("is_actionable"))
    if was_strict_actionable:
        if _is_v15_allowed_signal(row, "strict"):
            row["signal_type"] = "strict"
            row["filter_decision"] = "v15_allowed_strict"
            return row

        row["is_actionable"] = False
        row["signal_type"] = ""
        row["filter_decision"] = "v15_blocked_strict"
        row["reason"] = "blocked_by_v15_whitelist"
        return row

    # Promote only carefully selected near_4 entries into paper/live.
    try:
        age = float(row.get("signal_age_minutes", 999999))
    except Exception:
        age = 999999.0
    try:
        near_score = int(float(row.get("near_score", 0) or 0))
    except Exception:
        near_score = 0

    live_near_min_score = int(getattr(args, "live_near_min_score", 4))
    near_fresh = 0 <= age <= float(getattr(args, "max_signal_age_minutes", 5.0))
    near_allowed = (
        not getattr(args, "disable_live_near", False)
        and bool(row.get("is_near_signal"))
        and near_score >= live_near_min_score
        and near_fresh
        and _is_v15_allowed_signal(row, f"near_{live_near_min_score}")
    )

    if near_allowed:
        row["is_actionable"] = True
        row["signal_type"] = f"near_{live_near_min_score}"
        row["filter_decision"] = "v15_allowed_near"
        row["reason"] = f"fresh_near_{live_near_min_score}_signal"
        return row

    row["signal_type"] = ""
    if bool(row.get("is_near_signal")):
        row["filter_decision"] = "v15_near_logged_only"
    else:
        row["filter_decision"] = "v15_no_signal"
    return row


def filter_v15_replay_rows(rows: list[dict], args) -> list[dict]:
    """Keep only replay rows matching the v15 strict/near whitelist unless --all-strategies is set."""
    if getattr(args, "all_strategies", False):
        return rows
    return [r for r in rows if _is_v15_allowed_signal(r, r.get("signal_type", ""))]

def make_replay_summary(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    group_cols = [
        "expiration_minutes", "platform", "asset", "category", "strategy",
        "strategy_family", "signal_type", "display_direction", "payout_pct",
    ]
    summary = (
        df.groupby(group_cols, dropna=False)
        .agg(
            trades=("result", "count"),
            wins=("result", lambda s: int((s == "win").sum())),
            losses=("result", lambda s: int((s == "loss").sum())),
            draws=("result", lambda s: int((s == "draw").sum())),
            profit=("profit", "sum"),
            avg_profit=("profit", "mean"),
        )
        .reset_index()
    )
    summary["winrate_pct"] = summary["wins"] / summary["trades"] * 100
    summary["breakeven_winrate_pct"] = 100 / (1 + summary["payout_pct"] / 100)
    summary["edge_vs_breakeven_pp"] = summary["winrate_pct"] - summary["breakeven_winrate_pct"]
    summary = summary.sort_values(["profit", "avg_profit", "trades"], ascending=[False, False, False])
    return summary

def latest_closed_position(
    data: pd.DataFrame,
    interval_minutes: int = INTERVAL_MINUTES,
    lag_minutes: float = CLOSED_CANDLE_LAG_MINUTES,
) -> int:
    """
    Return the row position of the latest candle whose close time is safely in the past.

    This fixes the fragile old assumption that data.iloc[-1] is always an unfinished
    live candle and data.iloc[-2] is always the latest closed one. Yahoo sometimes
    returns only closed candles, especially outside market hours.
    """
    if data.empty:
        raise ValueError("No candles available")

    now_ts = pd.Timestamp.now(tz="UTC")
    safe_now = now_ts - pd.Timedelta(minutes=lag_minutes)
    close_times = data.index + pd.Timedelta(minutes=interval_minutes)
    closed_mask = close_times <= safe_now

    positions = np.flatnonzero(np.asarray(closed_mask, dtype=bool))
    if len(positions) == 0:
        raise ValueError("No safely closed candles yet")

    return int(positions[-1])


def get_cached_data(
    yf_symbol: str,
    period: str = "5d",
    interval: str = "15m",
    data_cache: dict = None,
) -> pd.DataFrame:
    """Download once per symbol/period/interval per live loop when cache is provided."""
    key = (yf_symbol, period, interval)
    if data_cache is not None and key in data_cache:
        return data_cache[key].copy()

    data = download_data(yf_symbol, period=period, interval=interval)
    if data_cache is not None:
        data_cache[key] = data.copy()
    return data


# direction_side:
#   UP   = CALL / RISE / Купить вверх
#   DOWN = PUT / FALL / Продать вниз
STRATEGIES = [
    # =========================
    # Binarium
    # =========================
    {
        "platform": "Binarium",
        "asset": "GOLD",
        "category": "Metal",
        "yf": "GC=F",
        "strategy": "GOLD PUT",
        "display_direction": "PUT",
        "direction_side": "DOWN",
        "payout_pct": 77.0,
        "rsi_level": 68,
        "bb_std": 1.7,
        "adx_limit": 30,
        "ema_slope_limit": 0.1,
        "priority": "A",
    },
    {
        "platform": "Binarium",
        "asset": "GOLD",
        "category": "Metal",
        "yf": "GC=F",
        "strategy": "GOLD CALL",
        "display_direction": "CALL",
        "direction_side": "UP",
        "payout_pct": 77.0,
        "rsi_level": 30,
        "bb_std": 1.5,
        "adx_limit": 30,
        "ema_slope_limit": 0.1,
        "priority": "B",
    },
    {
        "platform": "Binarium",
        "asset": "GBP/USD",
        "category": "Forex",
        "yf": "GBPUSD=X",
        "strategy": "GBP/USD CALL",
        "display_direction": "CALL",
        "direction_side": "UP",
        "payout_pct": 78.0,
        "rsi_level": 28,
        "bb_std": 1.5,
        "adx_limit": 30,
        "ema_slope_limit": 0.1,
        "priority": "A",
    },

    # =========================
    # Pocket Option real Forex
    # =========================
    {
        "platform": "Pocket Option",
        "asset": "EUR/CAD",
        "category": "Forex",
        "yf": "EURCAD=X",
        "strategy": "EUR/CAD CALL",
        "display_direction": "CALL",
        "direction_side": "UP",
        "payout_pct": 85.0,
        "rsi_level": 28,
        "bb_std": 1.3,
        "adx_limit": 30,
        "ema_slope_limit": 0.1,
        "priority": "A",
    },
    {
        "platform": "Pocket Option",
        "asset": "CAD/CHF",
        "category": "Forex",
        "yf": "CADCHF=X",
        "strategy": "CAD/CHF CALL",
        "display_direction": "CALL",
        "direction_side": "UP",
        "payout_pct": 86.0,
        "rsi_level": 28,
        "bb_std": 1.5,
        "adx_limit": 30,
        "ema_slope_limit": 0.1,
        "priority": "A",
    },
    {
        "platform": "Pocket Option",
        "asset": "USD/JPY",
        "category": "Forex",
        "yf": "JPY=X",
        "strategy": "USD/JPY PUT",
        "display_direction": "PUT",
        "direction_side": "DOWN",
        "payout_pct": 86.0,
        "rsi_level": 68,
        "bb_std": 1.3,
        "adx_limit": 20,
        "ema_slope_limit": 0.1,
        "priority": "A",
    },
    {
        "platform": "Pocket Option",
        "asset": "USD/JPY",
        "category": "Forex",
        "yf": "JPY=X",
        "strategy": "USD/JPY CALL",
        "display_direction": "CALL",
        "direction_side": "UP",
        "payout_pct": 86.0,
        "rsi_level": 30,
        "bb_std": 1.3,
        "adx_limit": 30,
        "ema_slope_limit": 0.1,
        "priority": "A",
    },
    {
        "platform": "Pocket Option",
        "asset": "GBP/USD",
        "category": "Forex",
        "yf": "GBPUSD=X",
        "strategy": "GBP/USD CALL",
        "display_direction": "CALL",
        "direction_side": "UP",
        "payout_pct": 81.0,
        "rsi_level": 28,
        "bb_std": 1.5,
        "adx_limit": 30,
        "ema_slope_limit": 0.1,
        "priority": "B",
    },
    {
        "platform": "Pocket Option",
        "asset": "USD/CAD",
        "category": "Forex",
        "yf": "CAD=X",
        "strategy": "USD/CAD PUT",
        "display_direction": "PUT",
        "direction_side": "DOWN",
        "payout_pct": 84.0,
        "rsi_level": 70,
        "bb_std": 1.3,
        "adx_limit": 30,
        "ema_slope_limit": 0.1,
        "priority": "B",
    },
    {
        "platform": "Pocket Option",
        "asset": "EUR/GBP",
        "category": "Forex",
        "yf": "EURGBP=X",
        "strategy": "EUR/GBP PUT",
        "display_direction": "PUT",
        "direction_side": "DOWN",
        "payout_pct": 86.0,
        "rsi_level": 70,
        "bb_std": 1.3,
        "adx_limit": 25,
        "ema_slope_limit": 0.1,
        "priority": "B",
    },

    # =========================
    # Deriv real assets
    # =========================
    {
        "platform": "Deriv",
        "asset": "EUR/CAD",
        "category": "Forex",
        "yf": "EURCAD=X",
        "strategy": "EUR/CAD RISE",
        "display_direction": "RISE",
        "direction_side": "UP",
        "payout_pct": 81.9,
        "rsi_level": 28,
        "bb_std": 1.7,
        "adx_limit": 30,
        "ema_slope_limit": 0.1,
        "priority": "A",
    },
    {
        "platform": "Deriv",
        "asset": "Gold/USD",
        "category": "Metal",
        "yf": "GC=F",
        "strategy": "Gold/USD FALL",
        "display_direction": "FALL",
        "direction_side": "DOWN",
        "payout_pct": 81.9,
        "rsi_level": 68,
        "bb_std": 1.7,
        "adx_limit": 30,
        "ema_slope_limit": 0.1,
        "priority": "A",
    },
    {
        "platform": "Deriv",
        "asset": "USD/JPY",
        "category": "Forex",
        "yf": "JPY=X",
        "strategy": "USD/JPY RISE",
        "display_direction": "RISE",
        "direction_side": "UP",
        "payout_pct": 81.9,
        "rsi_level": 30,
        "bb_std": 1.3,
        "adx_limit": 30,
        "ema_slope_limit": 0.1,
        "priority": "A",
    },
    {
        "platform": "Deriv",
        "asset": "USD/JPY",
        "category": "Forex",
        "yf": "JPY=X",
        "strategy": "USD/JPY FALL",
        "display_direction": "FALL",
        "direction_side": "DOWN",
        "payout_pct": 81.9,
        "rsi_level": 68,
        "bb_std": 1.3,
        "adx_limit": 20,
        "ema_slope_limit": 0.1,
        "priority": "A",
    },
    {
        "platform": "Deriv",
        "asset": "USD/CAD",
        "category": "Forex",
        "yf": "CAD=X",
        "strategy": "USD/CAD FALL",
        "display_direction": "FALL",
        "direction_side": "DOWN",
        "payout_pct": 81.9,
        "rsi_level": 70,
        "bb_std": 1.3,
        "adx_limit": 30,
        "ema_slope_limit": 0.1,
        "priority": "A",
    },
    {
        "platform": "Deriv",
        "asset": "Silver/USD",
        "category": "Metal",
        "yf": "SI=F",
        "strategy": "Silver/USD RISE",
        "display_direction": "RISE",
        "direction_side": "UP",
        "payout_pct": 81.9,
        "rsi_level": 32,
        "bb_std": 1.3,
        "adx_limit": 25,
        "ema_slope_limit": 0.1,
        "priority": "B",
    },
    {
        "platform": "Deriv",
        "asset": "GBP/USD",
        "category": "Forex",
        "yf": "GBPUSD=X",
        "strategy": "GBP/USD RISE",
        "display_direction": "RISE",
        "direction_side": "UP",
        "payout_pct": 81.9,
        "rsi_level": 30,
        "bb_std": 1.3,
        "adx_limit": 20,
        "ema_slope_limit": 0.1,
        "priority": "B",
    },
    {
        "platform": "Deriv",
        "asset": "EUR/GBP",
        "category": "Forex",
        "yf": "EURGBP=X",
        "strategy": "EUR/GBP FALL",
        "display_direction": "FALL",
        "direction_side": "DOWN",
        "payout_pct": 81.9,
        "rsi_level": 70,
        "bb_std": 1.3,
        "adx_limit": 20,
        "ema_slope_limit": 0.1,
        "priority": "B",
    },
    {
        "platform": "Deriv",
        "asset": "Wall Street 30",
        "category": "Stock index",
        "yf": "^DJI",
        "strategy": "Wall Street 30 RISE",
        "display_direction": "RISE",
        "direction_side": "UP",
        "payout_pct": 81.9,
        "rsi_level": 28,
        "bb_std": 1.3,
        "adx_limit": 30,
        "ema_slope_limit": 0.1,
        "priority": "C",
    },
    {
        "platform": "Deriv",
        "asset": "US 500",
        "category": "Stock index",
        "yf": "^GSPC",
        "strategy": "US 500 FALL",
        "display_direction": "FALL",
        "direction_side": "DOWN",
        "payout_pct": 81.9,
        "rsi_level": 68,
        "bb_std": 1.3,
        "adx_limit": 25,
        "ema_slope_limit": 0.1,
        "priority": "C",
    },
    # =========================
    # Trend Pullback candidates
    # strategy_family = trend_pullback
    # PAPER/DEMO candidates only.
    # =========================
    {
        "platform": "Deriv",
        "asset": "Gold/USD",
        "category": "Metal",
        "yf": "GC=F",
        "strategy": "Gold/USD FALL trend_pullback",
        "strategy_family": "trend_pullback",
        "display_direction": "FALL",
        "direction_side": "DOWN",
        "payout_pct": 81.9,
        "priority": "TP-A",
        "bb_std": 1.5,
        "slope_limit": 0.001,
        "adx_min": 20,
        "roc_lookback": "roc_8_pct",
        "roc_limit": 0.03,
        "pullback_target": "BB_MID",
        "near_threshold_pct": 0.15,
        "rsi_low": 50,
        "rsi_high": 65,
        "confirm_candle": False,
        "backtest_note": "9 trades, 88.89% winrate, EV +6.17, max loss 1",
    },
    {
        "platform": "Deriv",
        "asset": "US Tech 100",
        "category": "Stock index",
        "yf": "^NDX",
        "strategy": "US Tech 100 RISE trend_pullback",
        "strategy_family": "trend_pullback",
        "display_direction": "RISE",
        "direction_side": "UP",
        "payout_pct": 81.9,
        "priority": "TP-A",
        "bb_std": 1.5,
        "slope_limit": 0.005,
        "adx_min": 15,
        "roc_lookback": "roc_8_pct",
        "roc_limit": 0.03,
        "pullback_target": "EMA20",
        "near_threshold_pct": 0.10,
        "rsi_low": 40,
        "rsi_high": 55,
        "confirm_candle": False,
        "backtest_note": "9 trades, 88.89% winrate, EV +6.17, max loss 1",
    },
    {
        "platform": "Deriv",
        "asset": "USD/CAD",
        "category": "Forex",
        "yf": "CAD=X",
        "strategy": "USD/CAD RISE trend_pullback",
        "strategy_family": "trend_pullback",
        "display_direction": "RISE",
        "direction_side": "UP",
        "payout_pct": 81.9,
        "priority": "TP-A",
        "bb_std": 1.5,
        "slope_limit": 0.002,
        "adx_min": 15,
        "roc_lookback": "roc_4_pct",
        "roc_limit": 0.03,
        "pullback_target": "EMA20",
        "near_threshold_pct": 0.05,
        "rsi_low": 40,
        "rsi_high": 55,
        "confirm_candle": False,
        "backtest_note": "13 trades, 84.62% winrate, EV +5.39, max loss 2",
    },
    {
        "platform": "Deriv",
        "asset": "USD/JPY",
        "category": "Forex",
        "yf": "JPY=X",
        "strategy": "USD/JPY FALL trend_pullback",
        "strategy_family": "trend_pullback",
        "display_direction": "FALL",
        "direction_side": "DOWN",
        "payout_pct": 81.9,
        "priority": "TP-A",
        "bb_std": 1.5,
        "slope_limit": 0.002,
        "adx_min": 15,
        "roc_lookback": "roc_4_pct",
        "roc_limit": 0.03,
        "pullback_target": "BB_MID",
        "near_threshold_pct": 0.05,
        "rsi_low": 45,
        "rsi_high": 60,
        "confirm_candle": False,
        "backtest_note": "10 trades, 80.00% winrate, EV +4.55, max loss 1",
    },
    {
        "platform": "Deriv",
        "asset": "Silver/USD",
        "category": "Metal",
        "yf": "SI=F",
        "strategy": "Silver/USD FALL trend_pullback",
        "strategy_family": "trend_pullback",
        "display_direction": "FALL",
        "direction_side": "DOWN",
        "payout_pct": 81.9,
        "priority": "TP-B",
        "bb_std": 1.5,
        "slope_limit": 0.005,
        "adx_min": 20,
        "roc_lookback": "roc_4_pct",
        "roc_limit": 0.03,
        "pullback_target": "BB_MID",
        "near_threshold_pct": 0.10,
        "rsi_low": 45,
        "rsi_high": 60,
        "confirm_candle": False,
        "backtest_note": "8 trades, 87.50% winrate, EV +5.92, max loss 1",
    },
    {
        "platform": "Deriv",
        "asset": "Silver/USD",
        "category": "Metal",
        "yf": "SI=F",
        "strategy": "Silver/USD RISE trend_pullback",
        "strategy_family": "trend_pullback",
        "display_direction": "RISE",
        "direction_side": "UP",
        "payout_pct": 81.9,
        "priority": "TP-B",
        "bb_std": 1.5,
        "slope_limit": 0.001,
        "adx_min": 20,
        "roc_lookback": "roc_8_pct",
        "roc_limit": 0.03,
        "pullback_target": "BB_MID",
        "near_threshold_pct": 0.05,
        "rsi_low": 40,
        "rsi_high": 55,
        "confirm_candle": False,
        "backtest_note": "8 trades, 87.50% winrate, EV +5.92, max loss 1",
    },
    {
        "platform": "Deriv",
        "asset": "US 500",
        "category": "Stock index",
        "yf": "^GSPC",
        "strategy": "US 500 RISE trend_pullback",
        "strategy_family": "trend_pullback",
        "display_direction": "RISE",
        "direction_side": "UP",
        "payout_pct": 81.9,
        "priority": "TP-B",
        "bb_std": 1.5,
        "slope_limit": 0.002,
        "adx_min": 15,
        "roc_lookback": "roc_8_pct",
        "roc_limit": 0.03,
        "pullback_target": "BB_MID",
        "near_threshold_pct": 0.15,
        "rsi_low": 40,
        "rsi_high": 55,
        "confirm_candle": False,
        "backtest_note": "13 trades, 76.92% winrate, EV +3.99, max loss 1",
    },
    {
        "platform": "Deriv",
        "asset": "Wall Street 30",
        "category": "Stock index",
        "yf": "^DJI",
        "strategy": "Wall Street 30 FALL trend_pullback",
        "strategy_family": "trend_pullback",
        "display_direction": "FALL",
        "direction_side": "DOWN",
        "payout_pct": 81.9,
        "priority": "TP-B",
        "bb_std": 1.5,
        "slope_limit": 0.001,
        "adx_min": 20,
        "roc_lookback": "roc_8_pct",
        "roc_limit": 0.05,
        "pullback_target": "EMA20",
        "near_threshold_pct": 0.10,
        "rsi_low": 45,
        "rsi_high": 60,
        "confirm_candle": False,
        "backtest_note": "9 trades, 77.78% winrate, EV +4.15, max loss 1",
    }
]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_utc(value) -> pd.Timestamp:
    return pd.Timestamp(value).tz_convert("UTC") if pd.Timestamp(value).tzinfo else pd.Timestamp(value).tz_localize("UTC")


def flatten_yfinance_columns(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    return df


def prepare_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    df = flatten_yfinance_columns(df.copy())
    needed = ["Open", "High", "Low", "Close"]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        raise ValueError(f"Нет колонок {missing}. Колонки: {list(df.columns)}")

    df = df[needed].dropna().copy()
    df = df[~df.index.duplicated(keep="last")]
    df = df.sort_index()

    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")

    return df


def download_data(yf_symbol: str, period: str = "5d", interval: str = "15m") -> pd.DataFrame:
    last_error = None

    for attempt in range(1, YFINANCE_RETRIES + 1):
        try:
            try:
                df = yf.download(
                    yf_symbol,
                    period=period,
                    interval=interval,
                    auto_adjust=False,
                    progress=False,
                    prepost=False,
                    threads=False,
                    timeout=YFINANCE_TIMEOUT_SECONDS,
                )
            except TypeError:
                # Older yfinance builds may not support the timeout keyword.
                df = yf.download(
                    yf_symbol,
                    period=period,
                    interval=interval,
                    auto_adjust=False,
                    progress=False,
                    prepost=False,
                    threads=False,
                )
            if df.empty:
                raise ValueError(f"Yahoo Finance вернул пустые данные для {yf_symbol}")

            prepared = prepare_ohlc(df)
            if prepared.empty:
                raise ValueError(f"После prepare_ohlc нет свечей для {yf_symbol}")

            return prepared

        except Exception as e:
            last_error = e
            if attempt < YFINANCE_RETRIES:
                print(f"[yfinance retry {attempt}/{YFINANCE_RETRIES}] {yf_symbol}: {e}", flush=True)
                time.sleep(YFINANCE_RETRY_SLEEP_SECONDS)

    raise ValueError(f"Yahoo Finance failed for {yf_symbol} after {YFINANCE_RETRIES} attempts: {last_error}")


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def bollinger(close: pd.Series, period: int = 20, std_mult: float = 1.5):
    mid = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0)
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    return mid, upper, lower


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["High"]
    low = df["Low"]
    close = df["Close"]

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    tr_smooth = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_dm_smooth = pd.Series(plus_dm, index=df.index).ewm(
        alpha=1 / period, adjust=False, min_periods=period
    ).mean()
    minus_dm_smooth = pd.Series(minus_dm, index=df.index).ewm(
        alpha=1 / period, adjust=False, min_periods=period
    ).mean()

    plus_di = 100 * plus_dm_smooth / tr_smooth.replace(0, np.nan)
    minus_di = 100 * minus_dm_smooth / tr_smooth.replace(0, np.nan)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def add_indicators(df: pd.DataFrame, bb_std: float) -> pd.DataFrame:
    out = df.copy()
    out["rsi"] = rsi(out["Close"], 14)

    mid, upper, lower = bollinger(out["Close"], 20, bb_std)
    out["bb_mid"] = mid
    out["bb_upper"] = upper
    out["bb_lower"] = lower

    out["adx"] = adx(out, 14)
    out["ema100"] = out["Close"].ewm(span=100, adjust=False, min_periods=100).mean()
    out["ema100_slope_pct"] = out["ema100"].pct_change() * 100
    out["ema20"] = out["Close"].ewm(span=20, adjust=False, min_periods=20).mean()
    out["ema50"] = out["Close"].ewm(span=50, adjust=False, min_periods=50).mean()
    out["roc_4_pct"] = out["Close"].pct_change(4) * 100
    out["roc_8_pct"] = out["Close"].pct_change(8) * 100
    out["bb_mid_20"] = out["Close"].rolling(20).mean()

    return out


def near_pct(value: float, target: float, threshold_pct: float) -> bool:
    if pd.isna(value) or pd.isna(target) or value == 0:
        return False
    return abs(value - target) / value * 100 <= threshold_pct


def trend_pullback_matches(data: pd.DataFrame, pos: int, strategy: dict) -> bool:
    if pos <= 0:
        return False

    row = data.iloc[pos]
    prev = data.iloc[pos - 1]

    needed = [
        "Close", "Open", "rsi", "adx", "ema20", "ema50", "ema100",
        "ema100_slope_pct", "bb_mid_20", strategy["roc_lookback"]
    ]
    if any(pd.isna(row[x]) for x in needed):
        return False

    if strategy["direction_side"] == "DOWN":
        trend_ok = (
            row["ema50"] < row["ema100"]
            and row["ema100_slope_pct"] < -strategy["slope_limit"]
            and row["Close"] < row["ema100"]
            and row[strategy["roc_lookback"]] < -strategy["roc_limit"]
            and row["adx"] >= strategy["adx_min"]
        )
    else:
        trend_ok = (
            row["ema50"] > row["ema100"]
            and row["ema100_slope_pct"] > strategy["slope_limit"]
            and row["Close"] > row["ema100"]
            and row[strategy["roc_lookback"]] > strategy["roc_limit"]
            and row["adx"] >= strategy["adx_min"]
        )

    if not trend_ok:
        return False

    if not (strategy["rsi_low"] <= row["rsi"] <= strategy["rsi_high"]):
        return False

    close = float(row["Close"])
    if strategy["pullback_target"] == "EMA20":
        targets = [row["ema20"]]
    elif strategy["pullback_target"] == "BB_MID":
        targets = [row["bb_mid_20"]]
    elif strategy["pullback_target"] == "EMA20_OR_BB_MID":
        targets = [row["ema20"], row["bb_mid_20"]]
    else:
        targets = [row["ema20"]]

    if not any(near_pct(close, float(t), strategy["near_threshold_pct"]) for t in targets if not pd.isna(t)):
        return False

    if strategy["direction_side"] == "DOWN":
        pullback_ok = (row["Close"] > row["Open"]) or (row["Close"] > prev["Close"])
        if not pullback_ok:
            return False
        if strategy.get("confirm_candle", False) and not (row["Close"] < row["Open"]):
            return False
    else:
        pullback_ok = (row["Close"] < row["Open"]) or (row["Close"] < prev["Close"])
        if not pullback_ok:
            return False
        if strategy.get("confirm_candle", False) and not (row["Close"] > row["Open"]):
            return False

    return True


def signal_matches(data: pd.DataFrame, pos: int, strategy: dict) -> bool:
    row = data.iloc[pos]

    if strategy.get("strategy_family", "mean_reversion") == "trend_pullback":
        return trend_pullback_matches(data, pos, strategy)

    needed = ["rsi", "bb_upper", "bb_lower", "adx", "ema100_slope_pct"]
    if any(pd.isna(row[x]) for x in needed):
        return False

    price = float(row["Close"])
    sideways = (
        row["adx"] < strategy.get("adx_limit", "")
        and abs(row["ema100_slope_pct"]) < strategy.get("ema_slope_limit", "")
    )

    if strategy["direction_side"] == "UP":
        return (
            row["rsi"] < strategy.get("rsi_level", "")
            and price < row["bb_lower"]
            and sideways
        )

    return (
        row["rsi"] > strategy.get("rsi_level", "")
        and price > row["bb_upper"]
        and sideways
    )



def near_signal_info(data: pd.DataFrame, pos: int, strategy: dict) -> dict:
    """
    Диагностика "почти сигнала".
    Не открывает сделку и не шлёт Telegram по умолчанию.
    Нужна, чтобы понимать, какие стратегии близко к входу и что именно не хватает.
    """
    row = data.iloc[pos]
    family = strategy.get("strategy_family", "mean_reversion")
    direction = strategy.get("direction_side", "")

    try:
        price = float(row["Close"])
    except Exception:
        return {"is_near_signal": False, "near_reason": "no_price", "near_score": 0}

    if family == "trend_pullback":
        needed = [
            "Close", "Open", "rsi", "adx", "ema20", "ema50", "ema100",
            "ema100_slope_pct", "bb_mid_20", strategy.get("roc_lookback", "roc_4_pct")
        ]
        if any((x not in row.index) or pd.isna(row[x]) for x in needed):
            return {"is_near_signal": False, "near_reason": "not_enough_trend_data", "near_score": 0}

        score = 0
        reasons = []

        roc_col = strategy.get("roc_lookback", "roc_4_pct")
        roc_value = row[roc_col]

        if direction == "DOWN":
            trend_parts = [
                row["ema50"] < row["ema100"],
                row["ema100_slope_pct"] < -strategy.get("slope_limit", 0),
                row["Close"] < row["ema100"],
                roc_value < -strategy.get("roc_limit", 0),
                row["adx"] >= strategy.get("adx_min", 0),
            ]
            pullback_ok = (row["Close"] > row["Open"]) or (row["Close"] > data.iloc[pos - 1]["Close"] if pos > 0 else False)
        else:
            trend_parts = [
                row["ema50"] > row["ema100"],
                row["ema100_slope_pct"] > strategy.get("slope_limit", 0),
                row["Close"] > row["ema100"],
                roc_value > strategy.get("roc_limit", 0),
                row["adx"] >= strategy.get("adx_min", 0),
            ]
            pullback_ok = (row["Close"] < row["Open"]) or (row["Close"] < data.iloc[pos - 1]["Close"] if pos > 0 else False)

        trend_count = sum(bool(x) for x in trend_parts)
        if trend_count >= 4:
            score += 1
        else:
            reasons.append(f"trend_parts={trend_count}/5")

        rsi_low = strategy.get("rsi_low", 0)
        rsi_high = strategy.get("rsi_high", 100)
        rsi_margin = 5
        rsi_near = (rsi_low - rsi_margin) <= row["rsi"] <= (rsi_high + rsi_margin)
        if rsi_near:
            score += 1
        else:
            reasons.append(f"rsi={row['rsi']:.2f} not near {rsi_low}-{rsi_high}")

        target_name = strategy.get("pullback_target", "EMA20")
        targets = []
        if target_name == "EMA20":
            targets = [("EMA20", row["ema20"])]
        elif target_name == "BB_MID":
            targets = [("BB_MID", row["bb_mid_20"])]
        elif target_name == "EMA20_OR_BB_MID":
            targets = [("EMA20", row["ema20"]), ("BB_MID", row["bb_mid_20"])]
        else:
            targets = [("EMA20", row["ema20"])]

        base_thr = float(strategy.get("near_threshold_pct", 0.10))
        pullback_distance = min(
            [abs(price - float(t)) / price * 100 for _, t in targets if not pd.isna(t)] or [999]
        )
        near_target = pullback_distance <= base_thr * 1.75
        if near_target:
            score += 1
        else:
            reasons.append(f"pullback_distance={pullback_distance:.4f}% > {base_thr*1.75:.4f}%")

        if pullback_ok:
            score += 1
        else:
            reasons.append("no_pullback_candle")

        is_near = score >= 3
        return {
            "is_near_signal": bool(is_near),
            "near_reason": "; ".join(reasons) if reasons else "trend_pullback_near",
            "near_score": score,
            "trend_parts_ok": trend_count,
            "pullback_distance_pct": round(float(pullback_distance), 6),
        }

    # Mean reversion diagnostics.
    needed = ["rsi", "bb_upper", "bb_lower", "adx", "ema100_slope_pct"]
    if any((x not in row.index) or pd.isna(row[x]) for x in needed):
        return {"is_near_signal": False, "near_reason": "not_enough_mr_data", "near_score": 0}

    score = 0
    reasons = []

    rsi_level = float(strategy.get("rsi_level", 50))
    adx_limit = float(strategy.get("adx_limit", 999))
    slope_limit = float(strategy.get("ema_slope_limit", 999))
    adx_margin = 5
    slope_margin = 1.5

    adx_near = row["adx"] < adx_limit + adx_margin
    slope_near = abs(row["ema100_slope_pct"]) < slope_limit * slope_margin if slope_limit != 999 else True

    if adx_near:
        score += 1
    else:
        reasons.append(f"adx={row['adx']:.2f} > {adx_limit}+{adx_margin}")

    if slope_near:
        score += 1
    else:
        reasons.append(f"slope={row['ema100_slope_pct']:.4f}% too high")

    if direction == "UP":
        rsi_near = row["rsi"] <= rsi_level + 4
        bb_distance = (price - float(row["bb_lower"])) / price * 100
        price_near = bb_distance <= 0.12
        if rsi_near:
            score += 1
        else:
            reasons.append(f"rsi={row['rsi']:.2f} > {rsi_level}+4")
        if price_near:
            score += 1
        else:
            reasons.append(f"price_above_bb_lower={bb_distance:.4f}%")
    else:
        rsi_near = row["rsi"] >= rsi_level - 4
        bb_distance = (float(row["bb_upper"]) - price) / price * 100
        price_near = bb_distance <= 0.12
        if rsi_near:
            score += 1
        else:
            reasons.append(f"rsi={row['rsi']:.2f} < {rsi_level}-4")
        if price_near:
            score += 1
        else:
            reasons.append(f"price_below_bb_upper={bb_distance:.4f}%")

    is_near = score >= 3
    return {
        "is_near_signal": bool(is_near),
        "near_reason": "; ".join(reasons) if reasons else "mean_reversion_near",
        "near_score": score,
        "bb_distance_pct": round(float(bb_distance), 6),
    }



def evaluate_latest(strategy: dict, max_signal_age_minutes: float, data_cache: dict = None) -> dict:
    df = get_cached_data(strategy["yf"], period="5d", interval="15m", data_cache=data_cache)
    data = add_indicators(df, strategy["bb_std"])

    if len(data) < 105:
        raise ValueError(f"Мало свечей для {strategy['platform']} {strategy['strategy']}: {len(data)}")

    latest_yahoo_candle_time = data.index[-1]
    latest_yahoo_candle_close_time = latest_yahoo_candle_time + pd.Timedelta(minutes=INTERVAL_MINUTES)
    latest_yahoo_age_minutes = (now_utc() - latest_yahoo_candle_close_time.to_pydatetime()).total_seconds() / 60

    # Берём последнюю реально закрытую свечу, а не слепо len(data)-2.
    pos = latest_closed_position(data, INTERVAL_MINUTES, CLOSED_CANDLE_LAG_MINUTES)
    row = data.iloc[pos]
    candle_time = data.index[pos]
    candle_close_time = candle_time + pd.Timedelta(minutes=INTERVAL_MINUTES)
    exit_candle_time = candle_time + pd.Timedelta(minutes=EXPIRATION_MINUTES)
    expiration_time = candle_close_time + pd.Timedelta(minutes=EXPIRATION_MINUTES)

    age_minutes = (now_utc() - candle_close_time.to_pydatetime()).total_seconds() / 60
    matches = signal_matches(data, pos, strategy)
    near_info = near_signal_info(data, pos, strategy)
    fresh = 0 <= age_minutes <= max_signal_age_minutes

    reason = "no_signal"
    is_actionable = False
    if matches and fresh:
        reason = "fresh_signal"
        is_actionable = True
    elif matches and not fresh:
        reason = "signal_but_old"

    return {
        "checked_at_utc": now_utc().isoformat(),
        "platform": strategy["platform"],
        "asset": strategy["asset"],
        "category": strategy["category"],
        "yf": strategy["yf"],
        "strategy": strategy["strategy"],
        "strategy_family": strategy.get("strategy_family", "mean_reversion"),
        "display_direction": strategy["display_direction"],
        "direction_side": strategy["direction_side"],
        "priority": strategy["priority"],
        "payout_pct": strategy["payout_pct"],
        "expiration_minutes": EXPIRATION_MINUTES,
        "expiration_bars": EXPIRATION_BARS,
        "signal_candle_utc": candle_time.isoformat(),
        "signal_candle_close_utc": candle_close_time.isoformat(),
        "exit_candle_utc": exit_candle_time.isoformat(),
        "expiration_time_utc": expiration_time.isoformat(),
        "signal_age_minutes": round(age_minutes, 2),
        "latest_yahoo_candle_utc": latest_yahoo_candle_time.isoformat(),
        "latest_yahoo_candle_close_utc": latest_yahoo_candle_close_time.isoformat(),
        "latest_yahoo_candle_age_minutes": round(latest_yahoo_age_minutes, 2),
        "selected_candle_pos": int(pos),
        "total_candles": int(len(data)),
        "external_entry_price": float(row["Close"]),
        "rsi": float(row["rsi"]) if not pd.isna(row["rsi"]) else np.nan,
        "rsi_level": strategy.get("rsi_level", ""),
        "bb_std": strategy.get("bb_std", ""),
        "bb_lower": float(row["bb_lower"]) if not pd.isna(row["bb_lower"]) else np.nan,
        "bb_upper": float(row["bb_upper"]) if not pd.isna(row["bb_upper"]) else np.nan,
        "adx": float(row["adx"]) if not pd.isna(row["adx"]) else np.nan,
        "adx_limit": strategy.get("adx_limit", ""),
        "ema100_slope_pct": float(row["ema100_slope_pct"]) if not pd.isna(row["ema100_slope_pct"]) else np.nan,
        "ema_slope_limit": strategy.get("ema_slope_limit", ""),
        "is_actionable": is_actionable,
        "reason": reason,
        "is_near_signal": bool(near_info.get("is_near_signal", False)) and not is_actionable,
        "near_reason": near_info.get("near_reason", ""),
        "near_score": near_info.get("near_score", 0),
        "bb_distance_pct": near_info.get("bb_distance_pct", ""),
        "pullback_distance_pct": near_info.get("pullback_distance_pct", ""),
        "trend_parts_ok": near_info.get("trend_parts_ok", ""),
        "backtest_note": strategy.get("backtest_note", ""),
        "slope_limit": strategy.get("slope_limit", ""),
        "adx_min": strategy.get("adx_min", ""),
        "roc_lookback": strategy.get("roc_lookback", ""),
        "roc_limit": strategy.get("roc_limit", ""),
        "pullback_target": strategy.get("pullback_target", ""),
        "near_threshold_pct": strategy.get("near_threshold_pct", ""),
        "rsi_low": strategy.get("rsi_low", ""),
        "rsi_high": strategy.get("rsi_high", ""),
        "confirm_candle": strategy.get("confirm_candle", ""),
    }



def replay_strategy(strategy: dict, replay_days: int, period: str, replay_near_min_score: int = 0) -> list[dict]:
    df = download_data(strategy["yf"], period=period, interval="15m")
    data = add_indicators(df, strategy["bb_std"])

    since = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=replay_days)
    rows = []

    for pos in range(len(data) - EXPIRATION_BARS):
        row = data.iloc[pos]
        future = data.iloc[pos + EXPIRATION_BARS]

        candle_time = data.index[pos]
        candle_close_time = candle_time + pd.Timedelta(minutes=INTERVAL_MINUTES)

        if candle_close_time < since:
            continue

        strict_match = signal_matches(data, pos, strategy)
        near_info = near_signal_info(data, pos, strategy)
        near_score = int(near_info.get("near_score", 0) or 0)
        near_match = replay_near_min_score > 0 and (not strict_match) and near_score >= replay_near_min_score

        if not strict_match and not near_match:
            continue

        entry = float(row["Close"])
        exit_ = float(future["Close"])
        result = calc_result(strategy["direction_side"], entry, exit_)
        profit = calc_profit(result, strategy["payout_pct"], BET_SIZE)
        signal_type = "strict" if strict_match else f"near_{replay_near_min_score}"

        rows.append(
            {
                "expiration_minutes": EXPIRATION_MINUTES,
                "expiration_bars": EXPIRATION_BARS,
                "signal_type": signal_type,
                "platform": strategy["platform"],
                "asset": strategy["asset"],
                "category": strategy["category"],
                "yf": strategy["yf"],
                "strategy": strategy["strategy"],
                "strategy_family": strategy.get("strategy_family", "mean_reversion"),
                "display_direction": strategy["display_direction"],
                "direction_side": strategy["direction_side"],
                "priority": strategy["priority"],
                "payout_pct": strategy["payout_pct"],
                "signal_candle_utc": candle_time.isoformat(),
                "signal_candle_close_utc": candle_close_time.isoformat(),
                "exit_candle_utc": data.index[pos + EXPIRATION_BARS].isoformat(),
                "expiration_time_utc": (candle_close_time + pd.Timedelta(minutes=EXPIRATION_MINUTES)).isoformat(),
                "external_entry_price": entry,
                "external_exit_price": exit_,
                "result": result,
                "profit": profit,
                "rsi": float(row["rsi"]) if not pd.isna(row["rsi"]) else np.nan,
                "rsi_level": strategy.get("rsi_level", ""),
                "bb_std": strategy.get("bb_std", ""),
                "bb_lower": float(row["bb_lower"]) if not pd.isna(row["bb_lower"]) else np.nan,
                "bb_upper": float(row["bb_upper"]) if not pd.isna(row["bb_upper"]) else np.nan,
                "adx": float(row["adx"]) if not pd.isna(row["adx"]) else np.nan,
                "adx_limit": strategy.get("adx_limit", ""),
                "ema100_slope_pct": float(row["ema100_slope_pct"]) if not pd.isna(row["ema100_slope_pct"]) else np.nan,
                "ema_slope_limit": strategy.get("ema_slope_limit", ""),
                "near_score": near_score,
                "near_reason": near_info.get("near_reason", ""),
                "bb_distance_pct": near_info.get("bb_distance_pct", ""),
                "pullback_distance_pct": near_info.get("pullback_distance_pct", ""),
                "trend_parts_ok": near_info.get("trend_parts_ok", ""),
            }
        )

    return rows



def run_replay_single(args, expiration_minutes: int, filename_suffix: str = "") -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    configure_expiration(expiration_minutes)

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    strategies = selected_strategies(args)
    all_rows = []
    errors = []

    suffix = filename_suffix or f"_{EXPIRATION_MINUTES}m"
    print(
        f"Replay started: days={args.replay_days}, strategies={len(strategies)}, "
        f"expiration={EXPIRATION_MINUTES}m, near_min_score={args.replay_near_min_score}"
    )

    for strategy in strategies:
        label = f"{strategy['platform']} / {strategy['strategy']} / {strategy['yf']}"
        try:
            print(f"[replay {EXPIRATION_MINUTES}m] {label}")
            rows = replay_strategy(strategy, args.replay_days, args.replay_period, args.replay_near_min_score)
            rows = filter_v15_replay_rows(rows, args)
            all_rows.extend(rows)
        except Exception as e:
            print(f"[ERROR] {label}: {e}")
            errors.append({
                "expiration_minutes": EXPIRATION_MINUTES,
                "platform": strategy["platform"],
                "strategy": strategy["strategy"],
                "yf": strategy["yf"],
                "error": str(e),
            })

    replay_path = out / f"binary_options_replay_trades{suffix}.csv"
    summary_path = out / f"binary_options_replay_summary{suffix}.csv"
    errors_path = out / f"binary_options_replay_errors{suffix}.csv"

    if all_rows:
        trades_df = pd.DataFrame(all_rows)
        trades_df = trades_df.sort_values(["signal_candle_utc", "platform", "strategy", "signal_type"])
        trades_df.to_csv(replay_path, index=False, encoding="utf-8-sig")
        summary_df = make_replay_summary(trades_df)
        summary_df.to_csv(summary_path, index=False, encoding="utf-8-sig")
    else:
        trades_df = pd.DataFrame()
        summary_df = pd.DataFrame()
        trades_df.to_csv(replay_path, index=False, encoding="utf-8-sig")
        summary_df.to_csv(summary_path, index=False, encoding="utf-8-sig")

    if errors:
        errors_df = pd.DataFrame(errors)
        errors_df.to_csv(errors_path, index=False, encoding="utf-8-sig")
    else:
        errors_df = pd.DataFrame()

    print("\nREPLAY ГОТОВ")
    print(f"Папка: {out.resolve()}")
    print(f"Экспирация: {EXPIRATION_MINUTES}m")
    print(f"Сделки: {replay_path}")
    print(f"Сводка: {summary_path}")
    if errors:
        print(f"Ошибки: {errors_path}")

    if not summary_df.empty:
        print("\nТОП по replay summary:")
        cols = [
            "expiration_minutes", "platform", "asset", "strategy", "signal_type", "display_direction",
            "trades", "winrate_pct", "profit", "avg_profit", "edge_vs_breakeven_pp",
        ]
        print(summary_df[cols].head(30).to_string(index=False))
    else:
        print("За выбранный период сигналов не найдено.")

    return trades_df, summary_df, errors_df


def run_replay_compare(args):
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    all_summaries = []
    for expiration in (15, 30):
        _, summary_df, _ = run_replay_single(args, expiration, filename_suffix=f"_{expiration}m")
        if not summary_df.empty:
            all_summaries.append(summary_df)

    compare_path = out / "binary_options_replay_compare_15m_vs_30m.csv"
    if not all_summaries:
        pd.DataFrame().to_csv(compare_path, index=False, encoding="utf-8-sig")
        print(f"\nCompare готов, но сигналов не найдено: {compare_path}")
        return

    combined = pd.concat(all_summaries, ignore_index=True)
    combined.to_csv(out / "binary_options_replay_summary_all_expirations.csv", index=False, encoding="utf-8-sig")

    key_cols = ["platform", "asset", "category", "strategy", "strategy_family", "signal_type", "display_direction", "payout_pct"]
    pivot = combined.pivot_table(
        index=key_cols,
        columns="expiration_minutes",
        values=["trades", "wins", "losses", "profit", "avg_profit", "winrate_pct", "edge_vs_breakeven_pp"],
        aggfunc="first",
    )
    pivot.columns = [f"{metric}_{int(exp)}m" for metric, exp in pivot.columns]
    compare = pivot.reset_index()

    # Helpful deltas when both expirations exist.
    for col in ["profit", "winrate_pct", "edge_vs_breakeven_pp", "trades"]:
        c15 = f"{col}_15m"
        c30 = f"{col}_30m"
        if c15 in compare.columns and c30 in compare.columns:
            compare[f"delta_{col}_15m_minus_30m"] = compare[c15] - compare[c30]

    sort_cols = [c for c in ["delta_profit_15m_minus_30m", "profit_15m", "profit_30m"] if c in compare.columns]
    if sort_cols:
        compare = compare.sort_values(sort_cols, ascending=[False] * len(sort_cols))

    compare.to_csv(compare_path, index=False, encoding="utf-8-sig")
    print(f"\nCOMPARE 15m vs 30m ГОТОВ: {compare_path}")

    show_cols = [
        "platform", "asset", "strategy", "signal_type",
        "trades_15m", "winrate_pct_15m", "profit_15m",
        "trades_30m", "winrate_pct_30m", "profit_30m",
    ]
    show_cols = [c for c in show_cols if c in compare.columns]
    print(compare[show_cols].head(30).to_string(index=False))


def run_replay(args):
    if args.compare_expirations:
        run_replay_compare(args)
    else:
        run_replay_single(args, args.expiration_minutes, filename_suffix=f"_{args.expiration_minutes}m")


def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        print("[telegram] TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")

    try:
        with urllib.request.urlopen(url, data=data, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"[telegram error] {e}")
        return False


def play_sound():
    try:
        import winsound
        winsound.Beep(1200, 450)
        winsound.Beep(1500, 450)
    except Exception:
        print("\a")


def load_json_set(path: Path) -> set:
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_json_set(path: Path, values: set):
    path.write_text(json.dumps(sorted(values), ensure_ascii=False, indent=2), encoding="utf-8")


def _timestamp_for_filename() -> str:
    return now_utc().strftime("%Y%m%d_%H%M%S")


def _write_csv_with_fallback(df: pd.DataFrame, path: Path, *, append: bool = False, header: bool = True):
    """
    Robust CSV writer for Windows.

    - Does not read and rewrite the whole historical CSV on every loop.
    - If Excel/another app locks the target file, writes a fallback file instead
      of crashing the live watcher.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    encoding = "utf-8-sig" if header or not path.exists() else "utf-8"

    try:
        if append:
            df.to_csv(path, mode="a", header=header, index=False, encoding=encoding)
        else:
            tmp = path.with_suffix(path.suffix + ".tmp")
            df.to_csv(tmp, index=False, encoding="utf-8-sig")
            os.replace(tmp, path)
    except PermissionError as e:
        fallback = path.with_name(f"{path.stem}_LOCKED_{_timestamp_for_filename()}{path.suffix}")
        df.to_csv(fallback, index=False, encoding="utf-8-sig")
        print(f"[csv locked] {path} is locked: {e}. Wrote fallback: {fallback}", flush=True)
    except OSError as e:
        fallback = path.with_name(f"{path.stem}_WRITE_ERROR_{_timestamp_for_filename()}{path.suffix}")
        df.to_csv(fallback, index=False, encoding="utf-8-sig")
        print(f"[csv write error] {path}: {e}. Wrote fallback: {fallback}", flush=True)


def append_csv(path: Path, rows: list[dict]):
    if not rows:
        return

    df = pd.DataFrame(rows)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Fast real append. The previous implementation read the entire CSV, concatenated,
    # and rewrote it every minute; after several days this can make the watcher look frozen.
    if not path.exists() or path.stat().st_size == 0:
        _write_csv_with_fallback(df, path, append=False, header=True)
        return

    try:
        existing_cols = list(pd.read_csv(path, nrows=0).columns)
    except Exception as e:
        backup = path.with_name(f"{path.stem}_BAD_HEADER_{_timestamp_for_filename()}{path.suffix}")
        try:
            os.replace(path, backup)
            print(f"[csv header error] Rotated bad CSV {path} -> {backup}: {e}", flush=True)
        except Exception:
            print(f"[csv header error] Could not rotate {path}: {e}", flush=True)
        _write_csv_with_fallback(df, path, append=False, header=True)
        return

    # If the schema changed between bot versions, rotate old file and start clean.
    new_cols = [c for c in df.columns if c not in existing_cols]
    if new_cols:
        backup = path.with_name(f"{path.stem}_OLD_SCHEMA_{_timestamp_for_filename()}{path.suffix}")
        try:
            os.replace(path, backup)
            print(f"[csv schema] Rotated old schema {path} -> {backup}; new columns: {new_cols}", flush=True)
        except PermissionError as e:
            fallback = path.with_name(f"{path.stem}_NEW_SCHEMA_{_timestamp_for_filename()}{path.suffix}")
            _write_csv_with_fallback(df, fallback, append=False, header=True)
            print(f"[csv schema locked] {path} locked: {e}. Wrote new rows to {fallback}", flush=True)
            return
        _write_csv_with_fallback(df, path, append=False, header=True)
        return

    for col in existing_cols:
        if col not in df.columns:
            df[col] = ""
    df = df[existing_cols]

    _write_csv_with_fallback(df, path, append=True, header=False)


def save_latest(path: Path, rows: list[dict]):
    df = pd.DataFrame(rows)
    _write_csv_with_fallback(df, path, append=False, header=True)


def write_heartbeat(path: Path, status: str, extra: dict | None = None):
    payload = {
        "time_utc": now_utc().isoformat(),
        "status": status,
    }
    if extra:
        payload.update(extra)
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[heartbeat error] {e}", flush=True)


def format_signal_message(row: dict) -> str:
    return (
        "🚨 <b>Unified binary signal</b>\n"
        f"Platform: <b>{row['platform']}</b>\n"
        f"Asset: <b>{row['asset']}</b>\n"
        f"Direction: <b>{row['display_direction']}</b> ({row['direction_side']})\n"
        f"Strategy: {row['strategy']}\n"
        f"Signal type: <b>{row.get('signal_type', 'strict') or 'strict'}</b>\n"
        f"Family: {row.get('strategy_family', 'mean_reversion')}\n"
        f"Priority: {row['priority']}\n"
        f"Expected payout: {row['payout_pct']}%\n"
        f"Expiration: {row.get('expiration_minutes', EXPIRATION_MINUTES)} min\n"
        f"Candle UTC: {row['signal_candle_utc']}\n"
        f"Close UTC: {row['signal_candle_close_utc']}\n"
        f"Age: {row['signal_age_minutes']} min\n"
        f"External entry price: {row['external_entry_price']}\n"
        f"RSI: {float(row['rsi']):.2f} / level {row.get('rsi_level', row.get('rsi_low', ''))}\n"
        f"ADX: {float(row['adx']):.2f} / limit {row.get('adx_limit', row.get('adx_min', ''))}\n"
        f"EMA slope: {row['ema100_slope_pct']:.4f}%\n"
        f"BB lower: {row['bb_lower']}\n"
        f"BB upper: {row.get('bb_upper', '')}\n"
        f"Backtest note: {row.get('backtest_note', '')}\n\n"
        "✅ Я уже открыл PAPER-сделку в журнале.\n"
        "На платформе вручную сверяй только DEMO, если есть возможность.\n"
        "Реальные деньги не использовать."
    )


def signal_id(platform: str, strategy: str, signal_candle_utc: str, direction: str, signal_type: str = "") -> str:
    signal_type = str(signal_type or "strict")
    return f"{platform}|{strategy}|{signal_candle_utc}|{direction}|{signal_type}"


def open_paper_trade(signal_row: dict, paper_path: Path) -> dict:
    trade_id = signal_id(
        signal_row["platform"],
        signal_row["strategy"],
        signal_row["signal_candle_utc"],
        signal_row["display_direction"],
        signal_row.get("signal_type", "strict"),
    )

    existing_ids = set()
    if paper_path.exists() and paper_path.stat().st_size > 0:
        try:
            old = pd.read_csv(paper_path)
            if "trade_id" in old.columns:
                existing_ids = set(old["trade_id"].astype(str))
        except Exception:
            pass

    if trade_id in existing_ids:
        return {}

    paper = {
        "trade_id": trade_id,
        "status": "OPEN",
        "opened_at_utc": now_utc().isoformat(),
        "closed_at_utc": "",
        "platform": signal_row["platform"],
        "asset": signal_row["asset"],
        "category": signal_row["category"],
        "yf": signal_row["yf"],
        "strategy": signal_row["strategy"],
        "display_direction": signal_row["display_direction"],
        "direction_side": signal_row["direction_side"],
        "priority": signal_row["priority"],
        "stake": BET_SIZE,
        "payout_pct": signal_row["payout_pct"],
        "expiration_minutes": signal_row.get("expiration_minutes", EXPIRATION_MINUTES),
        "expiration_bars": signal_row.get("expiration_bars", EXPIRATION_BARS),
        "signal_candle_utc": signal_row["signal_candle_utc"],
        "signal_candle_close_utc": signal_row["signal_candle_close_utc"],
        "exit_candle_utc": signal_row["exit_candle_utc"],
        "expiration_time_utc": signal_row["expiration_time_utc"],
        "external_entry_price": signal_row["external_entry_price"],
        "external_exit_price": "",
        "result": "",
        "profit": "",
        "platform_entry_price": "",
        "platform_exit_price": "",
        "platform_result": "",
        "price_gap_entry": "",
        "comment": "Auto paper trade from unified bot. Platform prices should be filled manually if checked.",
    }

    append_csv(paper_path, [paper])
    return paper


def close_due_paper_trades(paper_path: Path):
    if not paper_path.exists() or paper_path.stat().st_size == 0:
        return []

    df = pd.read_csv(paper_path)
    if df.empty or "status" not in df.columns:
        return []

    changed_rows = []
    now_ts = pd.Timestamp.now(tz="UTC")

    for idx, trade in df[df["status"] == "OPEN"].iterrows():
        try:
            exit_candle = parse_utc(trade["exit_candle_utc"])
            # Ждём, пока свеча экспирации точно закрылась и появилась в Yahoo.
            safe_close_time = exit_candle + pd.Timedelta(minutes=INTERVAL_MINUTES + 2)
            if now_ts < safe_close_time:
                continue

            yf_symbol = str(trade["yf"])
            data = download_data(yf_symbol, period="5d", interval="15m")

            # Ищем exact exit candle. Если нет exact, берём первую свечу после неё.
            candidates = data[data.index >= exit_candle]
            if candidates.empty:
                continue

            exit_row = candidates.iloc[0]
            exit_price = float(exit_row["Close"])
            entry_price = float(trade["external_entry_price"])
            direction_side = str(trade["direction_side"])
            result = calc_result(direction_side, entry_price, exit_price)
            profit = calc_profit(result, float(trade["payout_pct"]), float(trade["stake"]))

            df.loc[idx, "status"] = "CLOSED"
            df.loc[idx, "closed_at_utc"] = now_utc().isoformat()
            df.loc[idx, "external_exit_price"] = exit_price
            df.loc[idx, "result"] = result
            df.loc[idx, "profit"] = profit

            changed_rows.append(df.loc[idx].to_dict())

        except Exception as e:
            print(f"[paper close error] {trade.get('trade_id', idx)}: {e}")

    if changed_rows:
        df.to_csv(paper_path, index=False, encoding="utf-8-sig")

    return changed_rows


def format_closed_paper_message(row: dict) -> str:
    result_emoji = "✅" if row.get("result") == "win" else "❌"
    return (
        f"{result_emoji} <b>Paper trade closed</b>\n"
        f"Platform: <b>{row.get('platform')}</b>\n"
        f"Asset: <b>{row.get('asset')}</b>\n"
        f"Direction: <b>{row.get('display_direction')}</b>\n"
        f"Strategy: {row.get('strategy')}\n"
        f"Entry external: {row.get('external_entry_price')}\n"
        f"Exit external: {row.get('external_exit_price')}\n"
        f"Result: <b>{row.get('result')}</b>\n"
        f"Profit: <b>{row.get('profit')}</b>\n\n"
        "Это paper-результат по внешним данным. Сравни с платформой, если открывал DEMO вручную."
    )



def is_on_cooldown(signal_row: dict, paper_path: Path, cooldown_minutes: float) -> tuple[bool, str]:
    """
    Возвращает True, если по той же platform+strategy недавно уже была paper-сделка.
    Это защита от спама серией сигналов на каждой 15m-свече.
    """
    if cooldown_minutes <= 0:
        return False, ""

    if not paper_path.exists() or paper_path.stat().st_size == 0:
        return False, ""

    try:
        df = pd.read_csv(paper_path)
    except Exception:
        return False, ""

    if df.empty:
        return False, ""

    required = {"platform", "strategy", "signal_candle_close_utc"}
    if not required.issubset(set(df.columns)):
        return False, ""

    same = df[
        (df["platform"].astype(str) == str(signal_row["platform"]))
        & (df["strategy"].astype(str) == str(signal_row["strategy"]))
    ].copy()

    if same.empty:
        return False, ""

    try:
        current_close = parse_utc(signal_row["signal_candle_close_utc"])
        same["signal_close_ts"] = pd.to_datetime(same["signal_candle_close_utc"], utc=True, errors="coerce")
        same = same.dropna(subset=["signal_close_ts"])
        if same.empty:
            return False, ""

        last_close = same["signal_close_ts"].max()
        diff_minutes = (current_close - last_close).total_seconds() / 60.0

        if 0 <= diff_minutes < cooldown_minutes:
            return True, f"cooldown_active_{diff_minutes:.1f}m_since_last_signal"

    except Exception:
        return False, ""

    return False, ""


def run_live(args):
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    latest_path = out / "unified_watcher_latest.csv"
    log_path = out / "unified_watcher_log.csv"
    signals_path = out / "unified_actionable_signals.csv"
    near_path = out / "unified_near_signals.csv"
    loose_path = out / "unified_loose_paper_candidates.csv"
    sent_ids_path = out / "sent_signal_ids.json"
    closed_ids_path = out / "sent_closed_paper_ids.json"
    paper_path = out / "binary_options_paper_trades.csv"
    heartbeat_path = out / "bot_heartbeat.json"

    sent_ids = load_json_set(sent_ids_path)
    sent_closed_ids = load_json_set(closed_ids_path)

    strategies = selected_strategies(args)

    print("Unified binary options bot v15 SELECTIVE started")
    print(f"Strategies: {len(strategies)}")
    print(f"Selective whitelist: {'OFF (--all-strategies)' if args.all_strategies else 'ON'}")
    print(f"Live near signals: {'OFF' if args.disable_live_near else 'ON'}; min near score={args.live_near_min_score}")
    print(f"Expiration: {EXPIRATION_MINUTES}m ({EXPIRATION_BARS} bar(s))")
    print(f"Output: {out.resolve()}")
    print("Mode: v15 selective 15m signal + AUTO PAPER trades. No real platform clicks.")

    if args.telegram and args.notify_on_start:
        send_telegram(
            "✅ Unified binary options bot v15 SELECTIVE started\n"
            f"Strategies: {len(strategies)}\n"
            f"Expiration: {EXPIRATION_MINUTES}m\n"
            "Mode: v15 selective signals + AUTO PAPER trades\n"
            "No real-money orders. No browser clicks."
        )

    while True:
        write_heartbeat(heartbeat_path, "cycle_start")
        rows = []
        actionable = []
        data_cache = {}

        for strategy in strategies:
            try:
                row = evaluate_latest(strategy, args.max_signal_age_minutes, data_cache=data_cache)
                row = apply_v15_live_policy(row, args)
            except Exception as e:
                row = {
                    "checked_at_utc": now_utc().isoformat(),
                    "platform": strategy["platform"],
                    "asset": strategy["asset"],
                    "category": strategy["category"],
                    "yf": strategy["yf"],
                    "strategy": strategy["strategy"],
                    "display_direction": strategy["display_direction"],
                    "direction_side": strategy["direction_side"],
                    "priority": strategy["priority"],
                    "payout_pct": strategy["payout_pct"],
                    "expiration_minutes": EXPIRATION_MINUTES,
                    "expiration_bars": EXPIRATION_BARS,
                    "is_actionable": False,
                    "signal_type": "",
                    "filter_decision": "error",
                    "reason": f"error: {e}",
                    "is_near_signal": False,
                    "near_reason": "",
                    "near_score": 0,
                    "signal_age_minutes": "",
                    "latest_yahoo_candle_utc": "",
                    "latest_yahoo_candle_age_minutes": "",
                }

            rows.append(row)
            if row.get("is_actionable"):
                actionable.append(row)

        near_rows = [
            r for r in rows
            if r.get("is_near_signal") and not r.get("is_actionable")
        ]
        loose_rows = [
            r for r in near_rows
            if args.enable_loose_paper and 0 <= float(r.get("signal_age_minutes", 9999)) <= args.max_signal_age_minutes
        ]

        save_latest(latest_path, rows)
        append_csv(log_path, rows)
        append_csv(signals_path, actionable)
        append_csv(near_path, near_rows)
        append_csv(loose_path, loose_rows)

        # Открываем paper trades и отправляем сигналы.
        # Антиспам: если по той же platform+strategy недавно уже была сделка,
        # не открываем новую и не шлём Telegram.
        suppressed_rows = []
        for row in actionable:
            sid = signal_id(row["platform"], row["strategy"], row["signal_candle_utc"], row["display_direction"], row.get("signal_type", "strict"))

            cooldown, cooldown_reason = is_on_cooldown(row, paper_path, args.cooldown_minutes)
            if cooldown:
                row["reason"] = cooldown_reason
                suppressed_rows.append(row)
                continue

            paper = open_paper_trade(row, paper_path)

            if sid not in sent_ids:
                print("\n" + "=" * 80)
                print(format_signal_message(row).replace("<b>", "").replace("</b>", ""))
                print("=" * 80 + "\n")

                if args.sound:
                    play_sound()

                if args.telegram:
                    send_telegram(format_signal_message(row))

                sent_ids.add(sid)

        if suppressed_rows:
            suppress_path = out / "unified_suppressed_signals.csv"
            append_csv(suppress_path, suppressed_rows)

        # Закрываем созревшие paper trades.
        closed = close_due_paper_trades(paper_path)
        for row in closed:
            tid = str(row.get("trade_id", ""))
            if tid and tid not in sent_closed_ids:
                print(f"[paper closed] {row.get('platform')} {row.get('strategy')} {row.get('result')} profit={row.get('profit')}")
                if args.telegram:
                    send_telegram(format_closed_paper_message(row))
                sent_closed_ids.add(tid)

        save_json_set(sent_ids_path, sent_ids)
        save_json_set(closed_ids_path, sent_closed_ids)

        checked_at = now_utc().strftime("%Y-%m-%d %H:%M:%S UTC")
        suppressed_count = len(locals().get("suppressed_rows", []))
        near_count = len(locals().get("near_rows", []))
        loose_count = len(locals().get("loose_rows", []))
        write_heartbeat(
            heartbeat_path,
            "cycle_done",
            {
                "checked": len(rows),
                "signals": len(actionable),
                "near": near_count,
                "loose": loose_count,
                "suppressed": suppressed_count,
                "paper_closed": len(closed),
            },
        )
        print(f"\n[{checked_at}] checked={len(rows)}, signals={len(actionable)}, near={near_count}, loose={loose_count}, suppressed={suppressed_count}, paper_closed={len(closed)}")

        for row in rows:
            platform = str(row.get("platform", ""))[:13]
            asset = str(row.get("asset", ""))[:14]
            direction = str(row.get("display_direction", ""))[:5]
            reason = str(row.get("reason", ""))[:18]
            age = row.get("signal_age_minutes", "")
            rsi_val = row.get("rsi", np.nan)
            adx_val = row.get("adx", np.nan)
            try:
                rsi_text = f"{float(rsi_val):.2f}"
            except Exception:
                rsi_text = "nan"
            try:
                adx_text = f"{float(adx_val):.2f}"
            except Exception:
                adx_text = "nan"

            latest_age = row.get("latest_yahoo_candle_age_minutes", "")
            near_score = row.get("near_score", "")
            near_mark = f" | near={near_score}" if row.get("is_near_signal") else ""
            sig_type = str(row.get("signal_type", ""))[:8]
            filter_decision = str(row.get("filter_decision", ""))[:22]
            print(f"  {platform:13s} | {asset:14s} {direction:5s} | {reason:22s} | type={sig_type:8s} | filter={filter_decision:22s} | age={age} | last_yf_age={latest_age} | RSI={rsi_text} | ADX={adx_text}{near_mark}")

        if args.once:
            break

        time.sleep(args.sleep_seconds)


def notify_crash(error_text: str):
    """
    Отправляет Telegram-уведомление, если бот упал.
    Использует уже существующую функцию send_telegram.
    """
    try:
        message = (
            "🛑 <b>Unified binary bot stopped with error</b>\n\n"
            f"<pre>{error_text[-3500:]}</pre>\n\n"
            "Нужно перезапустить PowerShell / watchdog."
        )
        send_telegram(message)
    except Exception as e:
        print(f"[crash notify failed] {e}", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="results_unified_binary_bot", help="Папка результатов")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--replay", action="store_true", help="Сделать исторический replay")
    mode.add_argument("--live", action="store_true", help="Запустить постоянный live watcher")

    parser.add_argument("--once", action="store_true", help="Одна live-проверка и выход")
    parser.add_argument("--sleep-seconds", type=int, default=60, help="Пауза между проверками")
    parser.add_argument("--max-signal-age-minutes", type=float, default=5.0, help="Максимальный возраст сигнала после закрытия 15m свечи")
    parser.add_argument("--cooldown-minutes", type=float, default=15.0, help="Антиспам: не открывать повторный сигнал по той же platform+strategy, пока не прошло N минут")
    parser.add_argument("--enable-loose-paper", action="store_true", help="Писать мягкие near-сигналы в unified_loose_paper_candidates.csv. Telegram не шлёт и сделки не открывает.")

    parser.add_argument("--expiration-minutes", type=int, default=15, help="Экспирация для replay/live paper: 15 или 30 минут. Должна делиться на 15.")
    parser.add_argument("--forex-only", action="store_true", help="Тестировать/запускать только стратегии category=Forex")
    parser.add_argument("--platforms", default="", help="Фильтр платформ через запятую, например: Pocket Option,Deriv")
    parser.add_argument("--exclude-categories", default="", help="Исключить категории через запятую, например: Metal,Stock index")
    parser.add_argument("--strategy-families", default="", help="Фильтр семейств стратегий, например: mean_reversion,trend_pullback")
    parser.add_argument("--all-strategies", action="store_true", help="Отключить v15 whitelist и запустить все стратегии как в v14")
    parser.add_argument("--live-near-min-score", type=int, default=4, help="Для v15 live: минимальный near_score, который можно открыть как PAPER по near whitelist")
    parser.add_argument("--disable-live-near", action="store_true", help="Для v15 live: не открывать near_4 PAPER-сделки, только strict whitelist")

    parser.add_argument("--replay-days", type=int, default=10, help="Сколько последних дней анализировать в replay")
    parser.add_argument("--replay-period", default="60d", help="Период yfinance для replay")
    parser.add_argument("--compare-expirations", action="store_true", help="В replay сравнить 15m и 30m и создать compare CSV")
    parser.add_argument("--replay-near-min-score", type=int, default=4, help="0 = только строгие сигналы. 4 = также тестировать near_score>=4; 3 = агрессивнее.")

    parser.add_argument("--sound", action="store_true", help="Звуковой сигнал")
    parser.add_argument("--telegram", action="store_true", help="Отправлять Telegram")
    parser.add_argument("--notify-on-start", action="store_true", help="Telegram сообщение при старте")
    args = parser.parse_args()

    if not args.compare_expirations:
        configure_expiration(args.expiration_minutes)

    if args.replay:
        run_replay(args)
    else:
        # По умолчанию live/once, чтобы старые привычки запуска работали.
        run_live(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user Ctrl+C", flush=True)
        try:
            send_telegram("🟡 Unified binary bot stopped manually with Ctrl+C")
        except Exception:
            pass
    except Exception:
        import traceback
        err = traceback.format_exc()
        print(err, flush=True)
        notify_crash(err)
        raise
