#!/usr/bin/env python3
"""
Backtest: Bot.py'deki puanlama mantığını geçmiş veride dener.
Geçmiş mumları Binance'ten (olmazsa OKX'ten) kendisi indirir, senin bir şey yüklemen gerekmez.

Nasıl çalışır:
- Her 15 dakikalık mumda, botun o an göreceği veriyle puanı hesaplar (geleceği görmez).
- Puan eşiği geçince bot bildirim gönderirdi; o fiyattan işleme girdiğini varsayar.
- Stop = 1.5xATR. İşlem stop, hedef ya da süre dolunca (varsayılan 24 saat) kapanır.
- Aynı mumda hem stop hem hedef görülürse stop vurulmuş sayılır (kötümser).

Kullanım:
  python backtest.py                        # ilk sembol (SOLUSDT), son 90 gün
  python backtest.py --days 180 --telegram  # sonucu Telegram'a da gönder
"""
import argparse
import time
from bisect import bisect_right

import requests

try:
    import Bot as bot  # GitHub'daki dosya adı Bot.py
except ImportError:
    import bot  # küçük harfli bot.py

MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
DAY_MS = 86_400_000


# ----------------------------- GEÇMİŞ VERİ -----------------------------
def _fetch_binance(host, symbol, interval, start_ms, end_ms):
    out, cur = [], start_ms
    now_ms = int(time.time() * 1000)
    while cur < end_ms:
        r = requests.get(
            f"{host}/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "startTime": cur,
                    "endTime": end_ms, "limit": 1000},
            timeout=20,
        )
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        for k in rows:
            if int(k[6]) > now_ms:  # kapanmamış mum
                continue
            out.append({"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
                        "l": float(k[3]), "c": float(k[4]), "v": float(k[5])})
        cur = int(rows[-1][0]) + MS[interval]
        if len(rows) < 1000:
            break
        time.sleep(0.2)
    return out


def _fetch_okx(symbol, interval, start_ms, end_ms):
    inst = symbol[:-4] + "-" + symbol[-4:] if symbol.endswith("USDT") else symbol
    bar = {"15m": "15m", "1h": "1H", "4h": "4H"}[interval]
    out, after = [], None
    while True:
        params = {"instId": inst, "bar": bar, "limit": 100}
        if after:
            params["after"] = after
        r = requests.get("https://www.okx.com/api/v5/market/history-candles",
                         params=params, timeout=20)
        r.raise_for_status()
        data = r.json().get("data", [])
        if not data:
            break
        for k in data:  # yeniden eskiye gelir
            t = int(k[0])
            if k[8] != "1" or t < start_ms or t >= end_ms:
                continue
            out.append({"t": t, "o": float(k[1]), "h": float(k[2]),
                        "l": float(k[3]), "c": float(k[4]), "v": float(k[5])})
        oldest = int(data[-1][0])
        if oldest <= start_ms:
            break
        after = str(oldest)
        time.sleep(0.15)
    out.sort(key=lambda x: x["t"])
    return out


def fetch_history(symbol, interval, start_ms, end_ms):
    errors = []
    for host in bot.BINANCE_HOSTS:
        try:
            data = _fetch_binance(host, symbol, interval, start_ms, end_ms)
            if data:
                return data
        except Exception as e:  # noqa: BLE001
            errors.append(f"{host}: {e}")
    try:
        data = _fetch_okx(symbol, interval, start_ms, end_ms)
        if data:
            return data
    except Exception as e:  # noqa: BLE001
        errors.append(f"okx: {e}")
    raise RuntimeError("Geçmiş veri alınamadı -> " + " | ".join(errors))


# ----------------------------- SİMÜLASYON -----------------------------
def precompute(entry, mid, htf, hold, warm=120, progress=True):
    """Her 15dk mum için, o anda botun göreceği veriyle puanları hesaplar."""
    mid_t = [x["t"] for x in mid]
    htf_t = [x["t"] for x in htf]
    evals = {}
    last = len(entry) - hold - 1
    for i in range(warm, last):
        close_t = entry[i]["t"] + MS["15m"]
        mi = bisect_right(mid_t, close_t - MS["1h"])    # o ana kadar kapanmış 1s mumlar
        hi = bisect_right(htf_t, close_t - MS["4h"])    # o ana kadar kapanmış 4s mumlar
        if mi < 60 or hi < 210:
            continue
        evals[i] = bot.evaluate(entry[max(0, i - 499):i + 1],
                                mid[max(0, mi - 300):mi],
                                htf[max(0, hi - 500):hi])
        if progress and len(evals) % 1000 == 0:
            print(f"  {len(evals)} mum işlendi...", flush=True)
    return evals


def _walk(up, i, entry, price, stop, target, hold):
    risk = abs(price - stop)
    rr = abs(target - price) / risk
    end = min(len(entry), i + 1 + hold)
    for j in range(i + 1, end):
        c = entry[j]
        if (c["l"] <= stop) if up else (c["h"] >= stop):
            return -1.0                      # aynı mumda ikisi de görülürse stop sayılır
        if (c["h"] >= target) if up else (c["l"] <= target):
            return rr
    last = entry[end - 1]["c"]               # süre doldu: son mum kapanışından çık
    return ((last - price) if up else (price - last)) / risk


def simulate_trade(side, i, entry, price, s, hold, fee):
    up = side == "long"
    lv = s["levels"]
    risk = abs(price - lv["stop"])
    fee_r = fee * price / risk               # komisyon + kayma, R cinsinden
    return {
        "side": side, "i": i, "t": entry[i]["t"], "score": s["score"],
        "r1": _walk(up, i, entry, price, lv["stop"], lv["tp1"], hold) - fee_r,
        "r2": _walk(up, i, entry, price, lv["stop"], lv["tp2"], hold) - fee_r,
    }


def select_trades(evals, entry, min_score, hold, fee):
    cd = max(1, bot.COOLDOWN_MIN // 15)      # bot gibi: aynı yönde 2 saat bekleme
    block = {"long": -1, "short": -1}
    trades = []
    for i in sorted(evals):
        for side in ("long", "short"):
            s = evals[i][side]
            if s["score"] < min_score or not s["levels"] or i < block[side]:
                continue
            block[side] = i + cd
            trades.append(simulate_trade(side, i, entry, evals[i]["price"], s, hold, fee))
    return trades


def stats(trades, key):
    n = len(trades)
    if n == 0:
        return None
    wins = sum(1 for t in trades if t[key] > 0)
    total = sum(t[key] for t in trades)
    peak = cum = dd = 0.0
    for t in trades:
        cum += t[key]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return {"n": n, "win": 100 * wins / n, "total": total, "avg": total / n, "dd": dd}


# ----------------------------- RAPOR -----------------------------
def build_report(symbol, days, evals, entry, hold, fee, thresholds=(5, 6, 7)):
    lines = [f"Backtest: {symbol} | son {days} gün | 15dk giriş, 1s/4s trend",
             f"Stop 1.5xATR, en fazla {hold // 4} saat açık, komisyon+kayma %{fee * 100:.2f}",
             "(+1R = stop mesafesi kadar kazanç, -1R = stop)", ""]
    for th in thresholds:
        tr = select_trades(evals, entry, th, hold, fee)
        if not tr:
            lines.append(f"Eşik {th}/{bot.MAX_SCORE}: sinyal çıkmadı")
            lines.append("")
            continue
        nl = sum(1 for t in tr if t["side"] == "long")
        lines.append(f"Eşik {th}/{bot.MAX_SCORE}: {len(tr)} işlem (long {nl} / short {len(tr) - nl})")
        for label, key in (("Hedef 1.5R", "r1"), ("Hedef 3R  ", "r2")):
            s = stats(tr, key)
            lines.append(f"  {label}: kazanma %{s['win']:.0f} | toplam {s['total']:+.1f}R | "
                         f"işlem başı {s['avg']:+.2f}R | maks düşüş {s['dd']:.1f}R")
        lines.append("")
    lines.append("Not: 30'dan az işlemde sonuç güvenilir değildir. "
                 "Geçmiş sonuç gelecek için garanti vermez. Yatırım tavsiyesi değildir.")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default=bot.SYMBOLS[0])
    p.add_argument("--days", type=int, default=90, help="kaç günlük geçmiş test edilsin")
    p.add_argument("--hold", type=int, default=96, help="işlem en fazla kaç 15dk mum açık kalsın")
    p.add_argument("--fee", type=float, default=0.001, help="komisyon+kayma (0.001 = %%0.1)")
    p.add_argument("--telegram", action="store_true", help="sonucu Telegram'a da gönder")
    a = p.parse_args()

    now_ms = int(time.time() * 1000)
    start = now_ms - a.days * DAY_MS
    print(f"{a.symbol} için geçmiş veri indiriliyor...", flush=True)
    entry = fetch_history(a.symbol, "15m", start - 130 * MS["15m"], now_ms)
    mid = fetch_history(a.symbol, "1h", start - 70 * MS["1h"], now_ms)
    htf = fetch_history(a.symbol, "4h", start - 215 * MS["4h"], now_ms)
    print(f"Veri hazır: {len(entry)} adet 15dk, {len(mid)} adet 1s, {len(htf)} adet 4s mum.")
    print("Puanlar hesaplanıyor (1-2 dakika sürebilir)...", flush=True)
    evals = precompute(entry, mid, htf, a.hold)
    if not evals:
        print("Yeterli veri yok, daha fazla gün dene.")
        return
    report = build_report(a.symbol, a.days, evals, entry, a.hold, a.fee)
    print("\n" + report)
    if a.telegram:
        bot.send_telegram(report)


if __name__ == "__main__":
    main()
