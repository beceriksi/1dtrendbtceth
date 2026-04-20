import os
import time
import requests
from datetime import datetime, timezone, timedelta
import pandas as pd

OKX_BASE = "https://www.okx.com"
SYMBOLS = ["BTC-USDT", "ETH-USDT"]

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# ---------------------- Genel Yardımcılar ---------------------- #

def ts():
    # Türkiye Saati (UTC+3)
    return (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S TSİ")

def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("\n[UYARI] Telegram bilgileri eksik. Mesaj içeriği:\n", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {'chat_id': CHAT_ID, 'text': text, 'parse_mode': 'Markdown'}
    try:
        r = requests.post(url, data=payload, timeout=15)
        if r.status_code != 200: print("[HATA] Telegram gönderilemedi:", r.text)
    except Exception as e: print("[HATA] Telegram hatası:", e)

# ---------------------- OKX GET Wrapper ---------------------- #

def jget_okx(path, params=None, retries=5):
    url = f"{OKX_BASE}{path}"
    for _ in range(retries):
        try:
            r = requests.get(url, params=params, timeout=10)
            data = r.json()
            if data.get("code") == "0": return data.get("data", [])
            time.sleep(1)
        except: time.sleep(1)
    return []

# ---------------------- Mum Verisi (1D) ---------------------- #

def get_candles(inst, bar="1D", limit=150):
    raw = jget_okx("/api/v5/market/candles", {"instId": inst, "bar": bar, "limit": limit})
    if not raw or len(raw) < 30: return None
    
    raw = list(reversed(raw))
    rows = []
    for r in raw:
        rows.append({
            "ts": datetime.fromtimestamp(int(r[0]) / 1000, tz=timezone.utc),
            "open": float(r[1]), "high": float(r[2]), "low": float(r[3]),
            "close": float(r[4]), "volume": float(r[5])
        })
    return pd.DataFrame(rows)

# ---------------------- Whale / Net Flow ---------------------- #

def get_trade_flow(inst):
    # Günlük analizde son trade akışına bakıyoruz
    data = jget_okx("/api/v5/market/trades", {"instId": inst, "limit": 300})
    if not data: return {"net": 0, "cat": "-", "dir": None}

    buy_usd = 0
    sell_usd = 0
    max_size = 0
    max_side = None

    for t in data:
        try:
            usd = float(t["px"]) * float(t["sz"])
            side = t["side"]
            if side == "buy": buy_usd += usd
            else: sell_usd += usd
            if usd > max_size:
                max_size = usd
                max_side = side
        except: continue

    cat = "XXL" if max_size >= 1_000_000 else "XL" if max_size >= 500_000 else "L" if max_size >= 150_000 else "M" if max_size >= 50_000 else "-"
    return {"net": buy_usd - sell_usd, "cat": cat, "dir": "UP" if max_side == "buy" else "DOWN" if max_side == "sell" else None}

# ---------------------- İndikatörler & Structure (İlk Dosyadaki Mantık) ---------------------- #

def add_indicators(df):
    close = df["close"]
    df["ema_fast"] = close.ewm(span=14, adjust=False).mean()
    df["ema_slow"] = close.ewm(span=28, adjust=False).mean()
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["vol_sma20"] = df["volume"].rolling(20).mean()
    df["v_ratio"] = df["volume"] / df["vol_sma20"]
    return df

def detect_swings(df, look=2):
    df["swing_high"] = False
    df["swing_low"] = False
    for i in range(look, len(df) - look):
        h = df["high"].iloc[i]
        l = df["low"].iloc[i]
        if all(h > df["high"].iloc[i-k] for k in range(1, look+1)) and all(h > df["high"].iloc[i+k] for k in range(1, look+1)):
            df.at[i, "swing_high"] = True
        if all(l < df["low"].iloc[i-k] for k in range(1, look+1)) and all(l < df["low"].iloc[i+k] for k in range(1, look+1)):
            df.at[i, "swing_low"] = True
    return df

def get_structure(df, idx):
    highs = [i for i in range(idx+1) if df.at[i, "swing_high"]]
    lows  = [i for i in range(idx+1) if df.at[i, "swing_low"]]
    ht = lt = last_hi = last_lo = None
    if len(highs) >= 2:
        last_hi, prev_hi = highs[-1], highs[-2]
        ht = "HH" if df.at[last_hi, "high"] > df.at[prev_hi, "high"] else "LH"
    if len(lows) >= 2:
        last_lo, prev_lo = lows[-1], lows[-2]
        lt = "HL" if df.at[last_lo, "low"] > df.at[prev_lo, "low"] else "LL"
    
    struct_dir = "UP" if (ht == "HH" or lt == "HL") else "DOWN" if (ht == "LH" or lt == "LL") else "NEUTRAL"
    return {"dir": struct_dir, "high": ht, "low": lt, "hi_idx": last_hi, "lo_idx": last_lo}

def trend_decision(df, idx, whale_dir):
    st = get_structure(df, idx)
    ema_dir = "UP" if df.at[idx, "ema_fast"] > df.at[idx, "ema_slow"] else "DOWN"
    macd_dir = "UP" if df.at[idx, "macd"] > df.at[idx, "macd_signal"] else "DOWN"
    
    confirmed = None
    if st["dir"] != "NEUTRAL" and st["dir"] == ema_dir:
        match = 2
        if macd_dir == st["dir"]: match += 1
        if whale_dir == st["dir"]: match += 1
        if match >= 3: confirmed = st["dir"]
    
    return {"confirmed": confirmed, "structure": st, "raw_ema": ema_dir}

# ---------------------- Ana Analiz ---------------------- #

def analyze(inst):
    df = get_candles(inst, "1D") # BURASI 1D OLDU
    if df is None: return None
    df = add_indicators(df)
    df = detect_swings(df)
    trade = get_trade_flow(inst)
    
    whale_dir = trade["dir"] if abs(trade["net"]) > 100000 else None
    res = trend_decision(df, len(df)-1, whale_dir)
    
    return {
        "inst": inst, "now": res, "close": df["close"].iloc[-1],
        "net": trade["net"], "cat": trade["cat"], "v_ratio": df["v_ratio"].iloc[-1]
    }

# ---------------------- MAIN ---------------------- #

def main():
    header = f"📊 **24 SAATLİK ANALİZ (GÜNLÜK 1D)**\n"
    header += f"⏰ {ts()}\n"
    header += "----------------------------------\n\n"
    
    body = ""
    for s in SYMBOLS:
        d = analyze(s)
        if not d: continue
        
        trend = d["now"]["confirmed"]
        yön = "🟢 LONG / YÜKSELEN" if trend == "UP" else "🔴 SHORT / DÜŞEN" if trend == "DOWN" else "⚪ NÖTR / BELİRSİZ"
        
        body += (
            f"*{s.split('-')[0]}*\n"
            f"• Trend: {yön}\n"
            f"• Fiyat: ${d['close']:,.2f}\n"
            f"• Yapı: {d['now']['structure']['high'] or '-'}/{d['now']['structure']['low'] or '-'}\n"
            f"• Balina: {d['cat']} ({d['net']:,.0f} USDT)\n"
            f"• vRatio: {d['v_ratio']:.2f}\n\n"
        )
    
    send_telegram(header + body)

if __name__ == "__main__":
    main()
