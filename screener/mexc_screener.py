#!/usr/bin/env python3
"""Скринер «ершистых» тикеров на споте MEXC.

Ищет неликвидные пары, в которых котирует одинокий маркетмейкер-бот:
узкий диапазон, повторяющийся клип, чередование ударов вверх/вниз.
Работает только с публичным API, ключи не нужны.

Использование:
    python3 screener/mexc_screener.py [--top 25] [--max-candidates 150] ...
"""

import argparse
import csv
import json
import os
import statistics
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

BASE = "https://api.mexc.com"
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "ersh-screener/0.1"


def get(path, **params):
    for attempt in range(3):
        try:
            r = SESSION.get(BASE + path, params=params, timeout=15)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(1)
    return None


def fetch_universe(args):
    """Этап 1: все 24h-тикеры -> кандидаты по объёму и спреду."""
    tickers = get("/api/v3/ticker/24hr")
    candidates = []
    for t in tickers:
        sym = t["symbol"]
        if not sym.endswith("USDT"):
            continue
        try:
            bid = float(t["bidPrice"] or 0)
            ask = float(t["askPrice"] or 0)
            qvol = float(t["quoteVolume"] or 0)
            last = float(t["lastPrice"] or 0)
        except (ValueError, TypeError):
            continue
        if bid <= 0 or ask <= 0 or last <= 0 or ask <= bid:
            continue
        if not (args.min_vol <= qvol <= args.max_vol):
            continue
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid * 100
        if spread_pct < args.min_spread:
            continue
        candidates.append({"symbol": sym, "quote_vol_24h": qvol, "spread_24h_pct": spread_pct})
    candidates.sort(key=lambda c: c["spread_24h_pct"], reverse=True)
    return tickers, candidates[: args.max_candidates]


def collapse_hits(trades):
    """Свернуть записи с одинаковым timestamp+стороной в один «удар» (одна рыночная заявка,
    прошедшая несколько уровней). Возвращает список (side, notional_usdt, ts)."""
    hits = []
    for tr in trades:
        side = tr.get("tradeType") or ("ASK" if tr.get("isBuyerMaker") else "BID")
        notional = float(tr["quoteQty"])
        ts = tr["time"]
        if hits and hits[-1][0] == side and hits[-1][2] == ts:
            hits[-1][1] += notional
        else:
            hits.append([side, notional, ts])
    return hits


def clip_stats(hits):
    """Доля самого частого размера удара (клипа). Округляем нотионал до 2 значащих цифр."""
    if not hits:
        return 0.0, 0.0, 0.0
    def rnd(x):
        if x <= 0:
            return 0.0
        from math import floor, log10
        k = 1 - int(floor(log10(x)))
        return round(x, k)
    sizes = [rnd(n) for _, n, _ in hits]
    counts = Counter(sizes).most_common()
    top1 = counts[0][1] / len(sizes)
    top3 = sum(c for _, c in counts[:3]) / len(sizes)
    median_clip = statistics.median(n for _, n, _ in hits)
    return top1, top3, median_clip


def analyze_symbol(cand, trades_limit):
    sym = cand["symbol"]
    trades = get("/api/v3/trades", symbol=sym, limit=trades_limit)
    depth = get("/api/v3/depth", symbol=sym, limit=20)
    if not trades or not depth or not depth.get("bids") or not depth.get("asks"):
        return None

    bid = float(depth["bids"][0][0])
    ask = float(depth["asks"][0][0])
    if bid <= 0 or ask <= bid:
        return None
    mid = (bid + ask) / 2
    spread_pct = (ask - bid) / mid * 100

    prices = [float(t["price"]) for t in trades]
    range_pct = (max(prices) - min(prices)) / mid * 100

    hits = collapse_hits(trades)
    if len(hits) < 10:
        return None
    top1_clip, top3_clip, median_clip = clip_stats(hits)

    flips = sum(1 for a, b in zip(hits, hits[1:]) if a[0] != b[0])
    alternation = flips / (len(hits) - 1)

    span_min = (max(t["time"] for t in trades) - min(t["time"] for t in trades)) / 60000
    hits_per_min = len(hits) / span_min if span_min > 0 else 0.0

    book_value = sum(float(p) * float(q) for p, q in depth["bids"]) + \
                 sum(float(p) * float(q) for p, q in depth["asks"])

    clamp = lambda x: max(0.0, min(1.0, x))
    score = (
        0.30 * clamp(top3_clip)                      # повторяемость клипа = бот
        + 0.20 * clamp(alternation / 0.6)            # пинг-понг вверх/вниз
        + 0.20 * clamp(spread_pct / 1.0)             # есть что собирать
        + 0.15 * clamp(1 - range_pct / 3.0)          # узкий «ёрш», не тренд
        + 0.15 * clamp(hits_per_min / 5.0)           # достаточно активен
    )

    return {
        **cand,
        "score": round(score, 3),
        "spread_pct": round(spread_pct, 3),
        "range_pct": round(range_pct, 3),
        "top1_clip_share": round(top1_clip, 2),
        "top3_clip_share": round(top3_clip, 2),
        "median_clip_usdt": round(median_clip, 2),
        "alternation": round(alternation, 2),
        "hits_per_min": round(hits_per_min, 2),
        "book_top20_usdt": round(book_value),
        "bid": bid,
        "ask": ask,
    }


def main():
    ap = argparse.ArgumentParser(description="Скринер ершистых тикеров MEXC")
    ap.add_argument("--min-vol", type=float, default=500, help="мин. 24h объём, USDT")
    ap.add_argument("--max-vol", type=float, default=300_000, help="макс. 24h объём, USDT")
    ap.add_argument("--min-spread", type=float, default=0.08, help="мин. спред по 24h-тикеру, %%")
    ap.add_argument("--max-candidates", type=int, default=150, help="сколько кандидатов анализировать глубоко")
    ap.add_argument("--trades-limit", type=int, default=100, help="сколько последних сделок брать")
    ap.add_argument("--top", type=int, default=25, help="сколько строк вывести")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out-dir", default="reports")
    args = ap.parse_args()

    print("Этап 1: загружаю 24h-тикеры...", file=sys.stderr)
    tickers, candidates = fetch_universe(args)
    print(f"Всего тикеров: {len(tickers)}, кандидатов после фильтра: {len(candidates)}", file=sys.stderr)

    print(f"Этап 2: анализ ленты и стакана ({len(candidates)} пар)...", file=sys.stderr)
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(analyze_symbol, c, args.trades_limit): c for c in candidates}
        done = 0
        for fut in as_completed(futures):
            done += 1
            if done % 25 == 0:
                print(f"  {done}/{len(candidates)}", file=sys.stderr)
            try:
                r = fut.result()
            except Exception as e:
                print(f"  ! {futures[fut]['symbol']}: {e}", file=sys.stderr)
                continue
            if r:
                results.append(r)

    results.sort(key=lambda r: r["score"], reverse=True)

    os.makedirs(args.out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    json_path = os.path.join(args.out_dir, f"ersh_{stamp}.json")
    csv_path = os.path.join(args.out_dir, f"ersh_{stamp}.csv")
    with open(json_path, "w") as f:
        json.dump(results, f, indent=1, ensure_ascii=False)
    if results:
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=results[0].keys())
            w.writeheader()
            w.writerows(results)

    hdr = f"{'SYMBOL':<16}{'SCORE':>6}{'SPRD%':>7}{'RNG%':>7}{'CLIP$':>8}{'CLIP3':>6}{'ALT':>5}{'HIT/M':>6}{'VOL24H':>10}{'BOOK$':>9}"
    print("\n" + hdr)
    print("-" * len(hdr))
    for r in results[: args.top]:
        print(f"{r['symbol']:<16}{r['score']:>6.2f}{r['spread_pct']:>7.2f}{r['range_pct']:>7.2f}"
              f"{r['median_clip_usdt']:>8.2f}{r['top3_clip_share']:>6.2f}{r['alternation']:>5.2f}"
              f"{r['hits_per_min']:>6.1f}{r['quote_vol_24h']:>10.0f}{r['book_top20_usdt']:>9}")
    print(f"\nПроанализировано: {len(results)}. Отчёты: {json_path}, {csv_path}")


if __name__ == "__main__":
    main()
