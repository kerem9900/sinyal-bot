#!/usr/bin/env python3
"""
Sinyal Botu: güçlü LONG/SHORT fırsatlarında Telegram'a bildirim gönderir.

- İşlem AÇMAZ, sadece haber verir.
- Mantık: 4s/1s trend + giriş zaman diliminde FVG retest + RSI + hacim + yapı kırılımı
- Puan (0-8) MIN_SCORE ve üzerindeyse bildirim gider.

Kullanım:
  python bot.py --test       # Telegram bağlantısını dener
  python bot.py --status     # Şu anki puanları gösterir, bildirim göndermez
  python bot.py --once       # Tek sefer kontrol (cron / GitHub Actions için)
  python bot.py              # Sürekli çalışır, CHECK_EVERY_SEC'de bir kontrol eder
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests


def load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv()

# ----------------------------- AYARLAR -----------------------------
SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", "SOLUSDT").split(",") if s.strip()]
ENTRY_TF = os.getenv("ENTRY_TF", "15m")      # giriş zaman dilimi (15m, 1h)
MID_TF = "1h"                                # orta vade trend
HTF = "4h"                                   # büyük trend
MIN_SCORE = int(os.getenv("MIN_SCORE", "6"))  # bildirim için gereken puan (maks 8)
MAX_SCORE = 8
COOLDOWN_MIN = int(os.getenv("COOLDOWN_MIN", "120"))   # aynı yönde tekrar bildirim bekleme süresi
CHECK_EVERY_SEC = int(os.getenv("CHECK_EVERY_SEC", "300"))
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")
STATE_FILE = os.getenv("STATE_FILE", "state.json")
BINANCE_HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]
TR_TZ = timezone(timedelta(hours=3))


# ----------------------------- VERİ -----------------------------
def _binance(host, symbol, interval, limit):
    r = requests.get(
        f"{host}/api/v3/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=15,
    )
    r.raise_for_status()
    now_ms = int(time.time() * 1000)
    out = []
    for k in r.json():
        if int(k[6]) > now_ms:  # henüz kapanmamış mum
            continue
        out.append({"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
                    "l": float(k[3]), "c": float(k[4]), "v": float(k[5])})
    return out


def _okx(symbol, interval, limit):
    inst = symbol[:-4] + "-" + symbol[-4:] if symbol.endswith("USDT") else symbol
    bar = {"15m": "15m", "1h": "1H", "4h": "4H"}.get(interval, interval)
    r = requests.get(
        "https://www.okx.com/api/v5/market/candles",
        params={"instId": inst, "bar": bar, "limit": min(limit, 300)},
        timeout=15,
    )
    r.raise_for_status()
    out = []
    for k in reversed(r.json().get("data", [])):  # OKX yeniden eskiye verir
        if k[8] != "1":  # kapanmamış mum
            continue
        out.append({"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
                    "l": float(k[3]), "c": float(k[4]), "v": float(k[5])})
    if not out:
        raise RuntimeError("OKX boş veri döndürdü")
    return out


def get_klines(symbol, interval, limit=500):
    errors = []
    for host in BINANCE_HOSTS:
        try:
            return _binance(host, symbol, interval, limit)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{host}: {e}")
    try:
        return _okx(symbol, interval, limit)
    except Exception as e:  # noqa: BLE001
        errors.append(f"okx: {e}")
    raise RuntimeError("Veri alınamadı -> " + " | ".join(errors))


# ----------------------------- GÖSTERGELER -----------------------------
def ema(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    e = sum(values[:period]) / period
    out[period - 1] = e
    k = 2 / (period + 1)
    for i in range(period, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def rsi(closes, period=14):
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / period, losses / period
    out[period] = 100 - 100 / (1 + ag / al) if al else 100.0
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
        out[i] = 100 - 100 / (1 + ag / al) if al else 100.0
    return out


def atr(c, period=14):
    trs = []
    for i in range(1, len(c)):
        trs.append(max(c[i]["h"] - c[i]["l"],
                       abs(c[i]["h"] - c[i - 1]["c"]),
                       abs(c[i]["l"] - c[i - 1]["c"])))
    if len(trs) < period:
        return None
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def find_fvgs(c, lookback=80, min_size_atr=0.3, a=None):
    """3 mumluk boşluklar (Fair Value Gap). Kapanışla kapatılmamış olanları döndürür."""
    fvgs = []
    n = len(c)
    for i in range(max(2, n - lookback), n):
        if c[i]["l"] > c[i - 2]["h"]:
            kind, lo, hi = "bull", c[i - 2]["h"], c[i]["l"]
        elif c[i]["h"] < c[i - 2]["l"]:
            kind, lo, hi = "bear", c[i]["h"], c[i - 2]["l"]
        else:
            continue
        if a and (hi - lo) < min_size_atr * a:
            continue
        filled = False
        for j in range(i + 1, n):
            if kind == "bull" and c[j]["c"] < lo:
                filled = True
                break
            if kind == "bear" and c[j]["c"] > hi:
                filled = True
                break
        if not filled:
            fvgs.append({"kind": kind, "lo": lo, "hi": hi, "i": i})
    return fvgs


def last_swings(c, left=3, right=3, lookback=60):
    n = len(c)
    sh = sl = None
    for i in range(n - right - 1, max(left, n - lookback), -1):
        window = c[i - left:i + right + 1]
        if sh is None and c[i]["h"] == max(x["h"] for x in window):
            sh = c[i]["h"]
        if sl is None and c[i]["l"] == min(x["l"] for x in window):
            sl = c[i]["l"]
        if sh is not None and sl is not None:
            break
    return sh, sl


# ----------------------------- PUANLAMA -----------------------------
def evaluate(entry, mid, htf):
    """Her yön için 0-8 arası puan ve gerekçe döndürür. Sadece KAPANMIŞ mumlarla çalışır."""
    closes = [x["c"] for x in entry]
    last = entry[-1]
    price = last["c"]
    a = atr(entry)
    rs = rsi(closes)
    h_close = htf[-1]["c"]
    h50 = ema([x["c"] for x in htf], 50)[-1]
    h200 = ema([x["c"] for x in htf], 200)[-1]
    m_close = mid[-1]["c"]
    m50 = ema([x["c"] for x in mid], 50)[-1]
    fvgs = find_fvgs(entry, a=a)
    prev_vols = [x["v"] for x in entry[-21:-1]]
    avg_v = sum(prev_vols) / len(prev_vols) if prev_vols else 0
    sh, sl = last_swings(entry)

    out = {"price": price, "atr": a}
    for side in ("long", "short"):
        up = side == "long"
        score, why = 0, []

        # 1) 4 saatlik trend (+2 güçlü / +1 zayıf)
        if h50 is not None:
            weak = h_close > h50 if up else h_close < h50
            strong = weak and h200 is not None and ((h50 > h200) if up else (h50 < h200))
            if strong:
                score += 2
                why.append("4s trend güçlü " + ("yukarı" if up else "aşağı"))
            elif weak:
                score += 1
                why.append("4s fiyat EMA50 " + ("üstünde" if up else "altında"))

        # 2) 1 saatlik trend (+1)
        if m50 is not None and ((m_close > m50) if up else (m_close < m50)):
            score += 1
            why.append("1s fiyat EMA50 " + ("üstünde" if up else "altında"))

        # 3) FVG retest + teyit mumu (+2)
        kind = "bull" if up else "bear"
        for z in reversed(fvgs):
            if z["kind"] != kind or z["i"] >= len(entry) - 1:
                continue
            if up:
                hit = last["l"] <= z["hi"] and last["c"] >= z["lo"] and last["c"] > last["o"]
            else:
                hit = last["h"] >= z["lo"] and last["c"] <= z["hi"] and last["c"] < last["o"]
            if hit:
                score += 2
                why.append(f"{ENTRY_TF} {'alış' if up else 'satış'} FVG bölgesine tepki "
                           f"({fmt(z['lo'])}-{fmt(z['hi'])})")
                break

        # 4) RSI (+1, aşırı bölgede -1)
        rn, rp = rs[-1], rs[-2]
        if rn is not None and rp is not None:
            recent = [x for x in rs[-7:-1] if x is not None]
            if up:
                if (rn > rp and 35 <= rn <= 65) or (recent and min(recent) < 30 and rn > 30):
                    score += 1
                    why.append(f"RSI {rn:.0f} yükseliyor / aşırı satımdan dönüş")
                if rn > 70:
                    score -= 1
                    why.append(f"RSI {rn:.0f} aşırı alım (-1)")
            else:
                if (rn < rp and 35 <= rn <= 65) or (recent and max(recent) > 70 and rn < 70):
                    score += 1
                    why.append(f"RSI {rn:.0f} düşüyor / aşırı alımdan dönüş")
                if rn < 30:
                    score -= 1
                    why.append(f"RSI {rn:.0f} aşırı satım (-1)")

        # 5) Hacim teyidi (+1)
        in_favor = last["c"] > last["o"] if up else last["c"] < last["o"]
        if avg_v and last["v"] > 1.5 * avg_v and in_favor:
            score += 1
            why.append(f"Hacim ortalamanın {last['v'] / avg_v:.1f} katı")

        # 6) Yapı kırılımı (+1)
        if up and sh is not None and closes[-1] > sh and min(closes[-4:-1]) <= sh:
            score += 1
            why.append(f"Son tepe kırıldı ({fmt(sh)})")
        if (not up) and sl is not None and closes[-1] < sl and max(closes[-4:-1]) >= sl:
            score += 1
            why.append(f"Son dip kırıldı ({fmt(sl)})")

        levels = None
        if a:
            risk = 1.5 * a
            sgn = 1 if up else -1
            levels = {"stop": price - sgn * risk,
                      "tp1": price + sgn * 1.5 * risk,
                      "tp2": price + sgn * 3 * risk}
        out[side] = {"score": max(0, score), "why": why, "levels": levels}
    return out


# ----------------------------- BİLDİRİM -----------------------------
def fmt(p):
    return f"{p:,.2f}" if p >= 10 else (f"{p:.4f}" if p >= 1 else f"{p:.6f}")


def format_msg(symbol, side, res):
    s = res[side]
    icon = "🟢 LONG" if side == "long" else "🔴 SHORT"
    now = datetime.now(TR_TZ).strftime("%d.%m %H:%M")
    lines = [f"{icon} fırsatı: {symbol} ({ENTRY_TF})",
             f"Puan: {s['score']}/{MAX_SCORE}   |   {now}",
             f"Fiyat: {fmt(res['price'])}"]
    if s["levels"]:
        lv = s["levels"]
        lines.append(f"Stop: {fmt(lv['stop'])}  |  TP1: {fmt(lv['tp1'])}  |  TP2: {fmt(lv['tp2'])}")
        lines.append("(ATR bazlı öneri: stop 1.5xATR, TP1 1.5R, TP2 3R)")
    lines.append("")
    lines.append("Gerekçeler:")
    lines += [f"• {w}" for w in s["why"]]
    lines.append("")
    lines.append("⚠️ Yatırım tavsiyesi değildir. Grafiği kendin doğrula, stop-loss kullan.")
    return "\n".join(lines)


def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        print("[Telegram ayarlı değil, ekrana yazıyorum]\n" + text + "\n")
        return False
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": text}, timeout=15)
    if not r.ok:
        print("Telegram hatası:", r.status_code, r.text)
    return r.ok


# ----------------------------- ÇALIŞTIRMA -----------------------------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def evaluate_symbol(symbol):
    entry = get_klines(symbol, ENTRY_TF, 500)
    mid = get_klines(symbol, MID_TF, 300)
    htf = get_klines(symbol, HTF, 500)
    return evaluate(entry, mid, htf)


def run_once(status_only=False):
    state = load_state()
    now = time.time()
    for sym in SYMBOLS:
        try:
            res = evaluate_symbol(sym)
        except Exception as e:  # noqa: BLE001
            print(f"[{sym}] hata: {e}")
            continue
        L, S = res["long"], res["short"]
        print(f"[{datetime.now(TR_TZ):%H:%M}] {sym} fiyat {fmt(res['price'])} | "
              f"LONG {L['score']}/{MAX_SCORE} | SHORT {S['score']}/{MAX_SCORE}")
        if status_only:
            for name, x in (("LONG", L), ("SHORT", S)):
                for w in x["why"]:
                    print(f"   {name}: {w}")
            continue
        for side in ("long", "short"):
            if res[side]["score"] < MIN_SCORE:
                continue
            key = f"{sym}:{side}"
            if now - state.get(key, 0) < COOLDOWN_MIN * 60:
                continue
            send_telegram(format_msg(sym, side, res))
            state[key] = now
    if not status_only:
        save_state(state)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", help="tek sefer kontrol et ve çık")
    p.add_argument("--status", action="store_true", help="puanları göster, bildirim gönderme")
    p.add_argument("--test", action="store_true", help="Telegram test mesajı gönder")
    args = p.parse_args()

    if args.test:
        ok = send_telegram("✅ Sinyal botu bağlantısı çalışıyor.")
        sys.exit(0 if ok else 1)
    if args.status:
        run_once(status_only=True)
        return
    if args.once:
        run_once()
        return

    print(f"Bot başladı. Semboller: {', '.join(SYMBOLS)} | TF: {ENTRY_TF} | "
          f"Min puan: {MIN_SCORE}/{MAX_SCORE} | Kontrol: {CHECK_EVERY_SEC}s")
    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001
            print("Döngü hatası:", e)
        time.sleep(CHECK_EVERY_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBot durduruldu.")
