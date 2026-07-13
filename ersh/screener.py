"""Скринер «ершистых» тикеров на споте MEXC.

Ищет неликвидные пары, в которых котирует одинокий маркетмейкер-бот:
узкий диапазон, повторяющийся клип, чередование ударов вверх/вниз.

CLI:  python3 -m ersh.screener [--top 25] ...
API:  screen(ScreenParams()) -> list[dict]  (отсортировано по score)
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
from dataclasses import dataclass
from math import floor, log10

from .mexc import Mexc, collapse_hits


@dataclass
class ScreenParams:
    min_vol: float = 500
    max_vol: float = 300_000
    min_spread: float = 0.08     # %, по 24h-тикеру
    max_candidates: int = 150
    trades_limit: int = 100
    workers: int = 6


def fetch_universe(client, p: ScreenParams):
    """Этап 1: все 24h-тикеры -> кандидаты по объёму и спреду."""
    tickers = client.tickers_24h()
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
        if not (p.min_vol <= qvol <= p.max_vol):
            continue
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid * 100
        if spread_pct < p.min_spread:
            continue
        candidates.append({"symbol": sym, "quote_vol_24h": qvol, "spread_24h_pct": spread_pct})
    candidates.sort(key=lambda c: c["spread_24h_pct"], reverse=True)
    return len(tickers), candidates[: p.max_candidates]


def clip_stats(hits):
    """Доля самого частого размера удара (клипа). Нотионал округляем до 2 значащих цифр."""
    if not hits:
        return 0.0, 0.0, 0.0

    def rnd(x):
        if x <= 0:
            return 0.0
        k = 1 - int(floor(log10(x)))
        return round(x, k)

    sizes = [rnd(h[1]) for h in hits]
    counts = Counter(sizes).most_common()
    top1 = counts[0][1] / len(sizes)
    top3 = sum(c for _, c in counts[:3]) / len(sizes)
    median_clip = statistics.median(h[1] for h in hits)
    return top1, top3, median_clip


def analyze_symbol(client, cand, trades_limit):
    sym = cand["symbol"]
    trades = client.trades(sym, limit=trades_limit)
    depth = client.depth(sym, limit=20)
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

    hits = collapse_hits(list(reversed(trades)))
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


def screen(p: ScreenParams = None, client=None, log=lambda m: None):
    p = p or ScreenParams()
    client = client or Mexc()
    total, candidates = fetch_universe(client, p)
    log(f"Всего тикеров: {total}, кандидатов после фильтра: {len(candidates)}")
    results = []
    with ThreadPoolExecutor(max_workers=p.workers) as pool:
        futures = {pool.submit(analyze_symbol, client, c, p.trades_limit): c for c in candidates}
        done = 0
        for fut in as_completed(futures):
            done += 1
            if done % 25 == 0:
                log(f"  {done}/{len(candidates)}")
            try:
                r = fut.result()
            except Exception as e:
                log(f"  ! {futures[fut]['symbol']}: {e}")
                continue
            if r:
                results.append(r)
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def main():
    ap = argparse.ArgumentParser(description="Скринер ершистых тикеров MEXC")
    ap.add_argument("--min-vol", type=float, default=500)
    ap.add_argument("--max-vol", type=float, default=300_000)
    ap.add_argument("--min-spread", type=float, default=0.08)
    ap.add_argument("--max-candidates", type=int, default=150)
    ap.add_argument("--trades-limit", type=int, default=100)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out-dir", default="reports")
    args = ap.parse_args()

    p = ScreenParams(min_vol=args.min_vol, max_vol=args.max_vol, min_spread=args.min_spread,
                     max_candidates=args.max_candidates, trades_limit=args.trades_limit,
                     workers=args.workers)
    results = screen(p, log=lambda m: print(m, file=sys.stderr))

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
