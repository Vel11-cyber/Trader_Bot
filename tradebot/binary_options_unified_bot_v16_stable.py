#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Binary Options Unified Watcher v16 Stable
Paper/Demo only. No real money. No platform clicks.
Fixes: pandas float64 crash, __main__ typo, trailing spaces in keys/values.
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
    raise SystemExit("Не установлен yfinance. Установи: pip install yfinance pandas numpy")

# ======================== CONFIG ========================
INTERVAL_MINUTES = 15
EXPIRATION_BARS = 2
EXPIRATION_MINUTES = 30
BET_SIZE = 10.0
PAPER_CSV = "binary_options_paper_trades.csv"
HEARTBEAT_JSON = "bot_heartbeat.json"
SENT_IDS_JSON = "sent_signal_ids.json"
CLOSED_IDS_JSON = "sent_closed_paper_ids.json"

# ======================== UTILS ========================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def parse_utc(value) -> pd.Timestamp:
    if pd.isna(value):
        return pd.NaT
    ts = pd.Timestamp(str(value).strip())
    return ts.tz_convert("UTC") if ts.tzinfo else ts.tz_localize("UTC")

def safe_read_csv(path: Path) -> pd.DataFrame:
    """Читает CSV с явными типами, чтобы избежать float64 для дат/строк."""
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=[
            "trade_id", "status", "opened_at_utc", "closed_at_utc",
            "platform", "asset", "category", "yf", "strategy",
            "display_direction", "direction_side", "priority",
            "stake", "payout_pct", "signal_candle_utc", "signal_candle_close_utc",
            "exit_candle_utc", "expiration_time_utc", "external_entry_price",
            "external_exit_price", "result", "profit", "comment"
        ])
    df = pd.read_csv(path, dtype={
        "trade_id": str, "status": str, "opened_at_utc": str, "closed_at_utc": str,
        "external_exit_price": str, "result": str, "profit": str, "comment": str
    })
    # Принудительная очистка от 'nan' и пробелов
    for col in ["trade_id", "status", "closed_at_utc", "result", "profit"]:
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip().replace("nan", "")
    return df

def safe_append_csv(path: Path, rows: list[dict]):
    if not rows:
        return
    df_new = pd.DataFrame(rows)
    # Гарантируем типы
    for col in ["trade_id", "status", "closed_at_utc", "result", "profit"]:
        if col in df_new.columns:
            df_new[col] = df_new[col].astype(str)
    
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        df_old = safe_read_csv(path)
        df = pd.concat([df_old, df_new], ignore_index=True)
    else:
        df = df_new
    df.to_csv(path, index=False, encoding="utf-8-sig")

def load_json_set(path: Path) -> set:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(x).strip() for x in data}
    except Exception:
        return set()

def save_json_set(path: Path, values: set):
    path.write_text(json.dumps(sorted(values), ensure_ascii=False, indent=2), encoding="utf-8")

def flatten_yfinance_columns(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df = df[~df.index.duplicated(keep="last")].sort_index()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    return df

def download_data(yf_symbol: str, period: str = "5d", interval: str = "15m") -> pd.DataFrame:
    df = yf.download(yf_symbol.strip(), period=period, interval=interval, auto_adjust=False, progress=False, prepost=False, threads=False)
    if df.empty:
        raise ValueError(f"Yahoo Finance вернул пустые данные для {yf_symbol}")
    return flatten_yfinance_columns(df)

# ======================== INDICATORS ========================
def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def bollinger(close: pd.Series, period: int = 20, std_mult: float = 1.5):
    mid = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0)
    return mid, mid + std_mult * std, mid - std_mult * std

def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    up_move, down_move = high.diff(), -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    tr_s = tr.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    plus_s = pd.Series(plus_dm, index=df.index).ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    minus_s = pd.Series(minus_dm, index=df.index).ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    plus_di = 100 * plus_s / tr_s.replace(0, np.nan)
    minus_di = 100 * minus_s / tr_s.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1/period, adjust=False, min_periods=period).mean()

def add_indicators(df: pd.DataFrame, bb_std: float) -> pd.DataFrame:
    out = df.copy()
    out["rsi"] = rsi(out["Close"], 14)
    out["bb_mid"], out["bb_upper"], out["bb_lower"] = bollinger(out["Close"], 20, bb_std)
    out["adx"] = adx(out, 14)
    out["ema100"] = out["Close"].ewm(span=100, adjust=False, min_periods=100).mean()
    out["ema100_slope_pct"] = out["ema100"].pct_change() * 100
    out["ema20"] = out["Close"].ewm(span=20, adjust=False, min_periods=20).mean()
    out["ema50"] = out["Close"].ewm(span=50, adjust=False, min_periods=50).mean()
    out["roc_4_pct"] = out["Close"].pct_change(4) * 100
    out["roc_8_pct"] = out["Close"].pct_change(8) * 100
    out["bb_mid_20"] = out["Close"].rolling(20).mean()
    return out

# ======================== STRATEGIES (CLEANED) ========================
STRATEGIES = [
    {"platform":"Binarium","asset":"GOLD","category":"Metal","yf":"GC=F","strategy":"GOLD PUT","display_direction":"PUT","direction_side":"DOWN","payout_pct":77.0,"rsi_level":68,"bb_std":1.7,"adx_limit":30,"ema_slope_limit":0.1,"priority":"A"},
    {"platform":"Binarium","asset":"GOLD","category":"Metal","yf":"GC=F","strategy":"GOLD CALL","display_direction":"CALL","direction_side":"UP","payout_pct":77.0,"rsi_level":30,"bb_std":1.5,"adx_limit":30,"ema_slope_limit":0.1,"priority":"B"},
    {"platform":"Binarium","asset":"GBP/USD","category":"Forex","yf":"GBPUSD=X","strategy":"GBP/USD CALL","display_direction":"CALL","direction_side":"UP","payout_pct":78.0,"rsi_level":28,"bb_std":1.5,"adx_limit":30,"ema_slope_limit":0.1,"priority":"A"},
    {"platform":"Pocket Option","asset":"EUR/CAD","category":"Forex","yf":"EURCAD=X","strategy":"EUR/CAD CALL","display_direction":"CALL","direction_side":"UP","payout_pct":85.0,"rsi_level":28,"bb_std":1.3,"adx_limit":30,"ema_slope_limit":0.1,"priority":"A"},
    {"platform":"Pocket Option","asset":"USD/JPY","category":"Forex","yf":"JPY=X","strategy":"USD/JPY PUT","display_direction":"PUT","direction_side":"DOWN","payout_pct":86.0,"rsi_level":68,"bb_std":1.3,"adx_limit":20,"ema_slope_limit":0.1,"priority":"A"},
    {"platform":"Pocket Option","asset":"USD/JPY","category":"Forex","yf":"JPY=X","strategy":"USD/JPY CALL","display_direction":"CALL","direction_side":"UP","payout_pct":86.0,"rsi_level":30,"bb_std":1.3,"adx_limit":30,"ema_slope_limit":0.1,"priority":"A"},
    {"platform":"Pocket Option","asset":"GBP/USD","category":"Forex","yf":"GBPUSD=X","strategy":"GBP/USD CALL","display_direction":"CALL","direction_side":"UP","payout_pct":81.0,"rsi_level":28,"bb_std":1.5,"adx_limit":30,"ema_slope_limit":0.1,"priority":"B"},
    {"platform":"Pocket Option","asset":"EUR/GBP","category":"Forex","yf":"EURGBP=X","strategy":"EUR/GBP PUT","display_direction":"PUT","direction_side":"DOWN","payout_pct":86.0,"rsi_level":70,"bb_std":1.3,"adx_limit":25,"ema_slope_limit":0.1,"priority":"B"},
    {"platform":"Deriv","asset":"Gold/USD","category":"Metal","yf":"GC=F","strategy":"Gold/USD FALL","display_direction":"FALL","direction_side":"DOWN","payout_pct":81.9,"rsi_level":68,"bb_std":1.7,"adx_limit":30,"ema_slope_limit":0.1,"priority":"A"},
    {"platform":"Deriv","asset":"USD/JPY","category":"Forex","yf":"JPY=X","strategy":"USD/JPY RISE","display_direction":"RISE","direction_side":"UP","payout_pct":81.9,"rsi_level":30,"bb_std":1.3,"adx_limit":30,"ema_slope_limit":0.1,"priority":"A"},
    {"platform":"Deriv","asset":"USD/JPY","category":"Forex","yf":"JPY=X","strategy":"USD/JPY FALL","display_direction":"FALL","direction_side":"DOWN","payout_pct":81.9,"rsi_level":68,"bb_std":1.3,"adx_limit":20,"ema_slope_limit":0.1,"priority":"A"},
    {"platform":"Deriv","asset":"EUR/GBP","category":"Forex","yf":"EURGBP=X","strategy":"EUR/GBP FALL","display_direction":"FALL","direction_side":"DOWN","payout_pct":81.9,"rsi_level":70,"bb_std":1.3,"adx_limit":20,"ema_slope_limit":0.1,"priority":"B"},
]

def signal_matches( pd.DataFrame, pos: int, strategy: dict) -> bool:
    row = data.iloc[pos]
    needed = ["rsi", "bb_upper", "bb_lower", "adx", "ema100_slope_pct"]
    if any(pd.isna(row[x]) for x in needed):
        return False
    price = float(row["Close"])
    sideways = row["adx"] < float(strategy.get("adx_limit", 999)) and abs(row["ema100_slope_pct"]) < float(strategy.get("ema_slope_limit", 999))
    if strategy["direction_side"] == "UP":
        return row["rsi"] < float(strategy["rsi_level"]) and price < float(row["bb_lower"]) and sideways
    return row["rsi"] > float(strategy["rsi_level"]) and price > float(row["bb_upper"]) and sideways

def near_signal_info( pd.DataFrame, pos: int, strategy: dict) -> dict:
    row = data.iloc[pos]
    needed = ["rsi", "bb_upper", "bb_lower", "adx", "ema100_slope_pct"]
    if any(pd.isna(row[x]) for x in needed):
        return {"is_near_signal": False, "near_reason": "not_enough_data", "near_score": 0}
    score = 0; reasons = []
    price = float(row["Close"])
    rsi_level = float(strategy.get("rsi_level", 50))
    adx_limit = float(strategy.get("adx_limit", 999))
    slope_limit = float(strategy.get("ema_slope_limit", 999))
    
    if row["adx"] < adx_limit + 5: score += 1
    else: reasons.append("adx_too_high")
    if abs(row["ema100_slope_pct"]) < slope_limit * 1.5: score += 1
    else: reasons.append("slope_too_high")
    
    if strategy["direction_side"] == "UP":
        if row["rsi"] <= rsi_level + 4: score += 1
        else: reasons.append("rsi_high")
        bb_dist = (price - float(row["bb_lower"])) / price * 100
        if bb_dist <= 0.15: score += 1
        else: reasons.append("bb_far")
    else:
        if row["rsi"] >= rsi_level - 4: score += 1
        else: reasons.append("rsi_low")
        bb_dist = (float(row["bb_upper"]) - price) / price * 100
        if bb_dist <= 0.15: score += 1
        else: reasons.append("bb_far")
        
    return {"is_near_signal": score >= 3, "near_reason": "; ".join(reasons), "near_score": score}

# ======================== PAPER TRADING (FIXED) ========================
def signal_id(platform: str, strategy: str, candle_utc: str, direction: str) -> str:
    return f"{platform.strip()}|{strategy.strip()}|{candle_utc.strip()}|{direction.strip()}"

def calc_result(direction_side: str, entry: float, exit: float) -> str:
    return "win" if (direction_side == "UP" and exit > entry) or (direction_side == "DOWN" and exit < entry) else "loss"

def calc_profit(result: str, payout_pct: float, stake: float) -> float:
    return stake * payout_pct / 100 if result == "win" else -stake

def open_paper_trade(row: dict, paper_path: Path) -> dict:
    tid = signal_id(row["platform"], row["strategy"], row["signal_candle_utc"], row["display_direction"])
    df = safe_read_csv(paper_path)
    if tid in df["trade_id"].astype(str):
        return {}
    trade = {
        "trade_id": tid, "status": "OPEN", "opened_at_utc": now_utc().isoformat(), "closed_at_utc": "",
        "platform": row["platform"], "asset": row["asset"], "category": row["category"], "yf": row["yf"],
        "strategy": row["strategy"], "display_direction": row["display_direction"], "direction_side": row["direction_side"],
        "priority": row["priority"], "stake": BET_SIZE, "payout_pct": row["payout_pct"],
        "signal_candle_utc": row["signal_candle_utc"], "signal_candle_close_utc": row["signal_candle_close_utc"],
        "exit_candle_utc": row["exit_candle_utc"], "expiration_time_utc": row["expiration_time_utc"],
        "external_entry_price": row["external_entry_price"], "external_exit_price": "", "result": "", "profit": "",
        "comment": "Auto paper trade. Platform prices manual if checked."
    }
    safe_append_csv(paper_path, [trade])
    return trade

def close_due_paper_trades(paper_path: Path):
    df = safe_read_csv(paper_path)
    if df.empty or "status" not in df.columns:
        return []
    open_trades = df[df["status"] == "OPEN"].copy()
    if open_trades.empty:
        return []
    
    # Приводим ключевые колонки к строковому типу перед модификацией
    for col in ["closed_at_utc", "external_exit_price", "result", "profit"]:
        if col in df.columns:
            df[col] = df[col].astype(str).replace("nan", "").replace("NaN", "")
    
    now_ts = pd.Timestamp.now(tz="UTC")
    changed = False
    closed_rows = []
    
    for idx, t in open_trades.iterrows():
        try:
            exit_candle = parse_utc(t["exit_candle_utc"])
            safe_time = exit_candle + pd.Timedelta(minutes=INTERVAL_MINUTES + 2)
            if now_ts < safe_time:
                continue
                
            yf_sym = str(t["yf"]).strip()
            data = download_data(yf_sym, period="5d", interval="15m")
            candidates = data[data.index >= exit_candle]
            if candidates.empty: continue
            exit_price = float(candidates.iloc[0]["Close"])
            entry_price = float(t["external_entry_price"])
            direction = str(t["direction_side"]).strip()
            res = calc_result(direction, entry_price, exit_price)
            prof = calc_profit(res, float(t["payout_pct"]), float(t["stake"]))
            
            # Безопасная запись
            df.at[idx, "status"] = "CLOSED"
            df.at[idx, "closed_at_utc"] = now_utc().isoformat()
            df.at[idx, "external_exit_price"] = str(exit_price)
            df.at[idx, "result"] = res
            df.at[idx, "profit"] = str(prof)
            closed_rows.append(df.loc[idx].to_dict())
            changed = True
        except Exception as e:
            print(f"[paper close error] {t.get('trade_id', idx)}: {e}")
            
    if changed:
        df.to_csv(paper_path, index=False, encoding="utf-8-sig")
    return closed_rows

# ======================== TELEGRAM & LIVE LOOP ========================
def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id: return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode("utf-8")
    try:
        with urllib.request.urlopen(url, data=data, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"[telegram error] {e}")
        return False

def evaluate_latest(strategy: dict, max_age: float) -> dict:
    data = add_indicators(download_data(strategy["yf"]), strategy["bb_std"])
    if len(data) < 105: raise ValueError(f"Мало свечей: {len(data)}")
    pos = len(data) - 2
    row = data.iloc[pos]
    candle_time = data.index[pos]
    candle_close = candle_time + pd.Timedelta(minutes=INTERVAL_MINUTES)
    exit_time = candle_time + pd.Timedelta(minutes=EXPIRATION_MINUTES)
    age = (now_utc() - candle_close.to_pydatetime()).total_seconds() / 60.0
    
    matches = signal_matches(data, pos, strategy)
    near = near_signal_info(data, pos, strategy)
    fresh = 0 <= age <= max_age
    
    return {
        "checked_at_utc": now_utc().isoformat(), "platform": strategy["platform"], "asset": strategy["asset"],
        "category": strategy["category"], "yf": strategy["yf"], "strategy": strategy["strategy"],
        "strategy_family": "mean_reversion", "display_direction": strategy["display_direction"],
        "direction_side": strategy["direction_side"], "priority": strategy["priority"],
        "payout_pct": strategy["payout_pct"], "signal_candle_utc": candle_time.isoformat(),
        "signal_candle_close_utc": candle_close.isoformat(), "exit_candle_utc": exit_time.isoformat(),
        "expiration_time_utc": (candle_close + pd.Timedelta(minutes=EXPIRATION_MINUTES)).isoformat(),
        "signal_age_minutes": round(age, 2), "external_entry_price": float(row["Close"]),
        "rsi": float(row["rsi"]), "rsi_level": strategy.get("rsi_level", ""),
        "bb_lower": float(row["bb_lower"]), "bb_upper": float(row["bb_upper"]),
        "adx": float(row["adx"]), "adx_limit": strategy.get("adx_limit", ""),
        "ema100_slope_pct": float(row["ema100_slope_pct"]), "is_actionable": matches and fresh,
        "reason": "fresh_signal" if matches and fresh else "no_signal",
        "is_near_signal": bool(near.get("is_near_signal")) and not (matches and fresh),
        "near_reason": near.get("near_reason", ""), "near_score": near.get("near_score", 0)
    }

def is_on_cooldown(row: dict, paper_path: Path, minutes: float) -> tuple[bool, str]:
    if minutes <= 0: return False, ""
    df = safe_read_csv(paper_path)
    if df.empty: return False, ""
    same = df[(df["platform"].astype(str).str.strip() == str(row["platform"]).strip()) & 
              (df["strategy"].astype(str).str.strip() == str(row["strategy"]).strip())].copy()
    if same.empty: return False, ""
    try:
        same["ts"] = pd.to_datetime(same["signal_candle_close_utc"], utc=True, errors="coerce")
        same = same.dropna(subset=["ts"])
        if same.empty: return False, ""
        diff = (parse_utc(row["signal_candle_close_utc"]) - same["ts"].max()).total_seconds() / 60.0
        if 0 <= diff < minutes: return True, f"cooldown_{diff:.1f}m"
    except: pass
    return False, ""

def run_live(args):
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    paper_path = out / PAPER_CSV
    sent_ids = load_json_set(out / SENT_IDS_JSON)
    closed_ids = load_json_set(out / CLOSED_IDS_JSON)
    
    print(f"🚀 Unified Bot v16 Stable | Strategies: {len(STRATEGIES)} | Output: {out.resolve()}")
    while True:
        rows = []
        for s in STRATEGIES:
            try: rows.append(evaluate_latest(s, args.max_signal_age_minutes))
            except Exception as e: rows.append({"checked_at_utc": now_utc().isoformat(), "platform": s["platform"], "asset": s["asset"], "strategy": s["strategy"], "is_actionable": False, "reason": f"error: {e}", "is_near_signal": False, "near_score": 0, "rsi": np.nan, "adx": np.nan})
            
        actionable = [r for r in rows if r.get("is_actionable")]
        near_rows = [r for r in rows if r.get("is_near_signal") and not r.get("is_actionable")]
        
        # Сохраняем логи
        safe_append_csv(out / "unified_watcher_latest.csv", rows)
        safe_append_csv(out / "unified_near_signals.csv", near_rows)
        
        # Открываем сделки
        suppressed = 0
        for r in actionable:
            sid = signal_id(r["platform"], r["strategy"], r["signal_candle_utc"], r["display_direction"])
            cool, reason = is_on_cooldown(r, paper_path, args.cooldown_minutes)
            if cool:
                suppressed += 1
                continue
            trade = open_paper_trade(r, paper_path)
            if trade and sid not in sent_ids:
                msg = f"🚨 Signal: {r['platform']} {r['asset']} {r['display_direction']}\nStrategy: {r['strategy']}\nRSI: {r['rsi']:.2f} | ADX: {r['adx']:.2f}"
                print(msg)
                if args.telegram: send_telegram(msg)
                sent_ids.add(sid)
                
        # Закрываем сделки
        closed = close_due_paper_trades(paper_path)
        for c in closed:
            tid = str(c.get("trade_id", ""))
            if tid and tid not in closed_ids:
                msg = f"📊 Paper Closed: {c['platform']} {c['asset']} {c['display_direction']} | Result: {c['result']} | Profit: {c['profit']}"
                print(msg)
                if args.telegram: send_telegram(msg)
                closed_ids.add(tid)
                
        save_json_set(out / SENT_IDS_JSON, sent_ids)
        save_json_set(out / CLOSED_IDS_JSON, closed_ids)
        
        # Heartbeat & Console
        heartbeat = {"last_check": now_utc().isoformat(), "checked": len(rows), "signals": len(actionable), "near": len(near_rows), "suppressed": suppressed, "paper_closed": len(closed)}
        (out / HEARTBEAT_JSON).write_text(json.dumps(heartbeat, indent=2), encoding="utf-8")
        print(f"[{now_utc().strftime('%H:%M:%S UTC')}] checked={len(rows)} signals={len(actionable)} near={len(near_rows)} sup={suppressed} closed={len(closed)}")
        
        if args.once: break
        time.sleep(args.sleep_seconds)

def main():
    parser = argparse.ArgumentParser(description="Binary Options Unified Bot v16")
    parser.add_argument("--output", default="results_unified_binary_bot", help="Output dir")
    parser.add_argument("--live", action="store_true", help="Run live loop")
    parser.add_argument("--once", action="store_true", help="Single run")
    parser.add_argument("--sleep-seconds", type=int, default=60)
    parser.add_argument("--max-signal-age-minutes", type=float, default=5.0)
    parser.add_argument("--cooldown-minutes", type=float, default=30.0)
    parser.add_argument("--telegram", action="store_true", help="Send alerts")
    args = parser.parse_args()
    run_live(args)

if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt: print("\n⏹ Stopped by user.")
    except Exception as e: print(f"\n💥 CRASH: {e}\n")