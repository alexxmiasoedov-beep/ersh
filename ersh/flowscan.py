"""Первый проход анализатора реального потока по всем API-доступным тикерам BingX.

Для каждого тикера: 100 последних принтов + текущий стакан. Сигналы:
  prints_h   — принтов в час (по временному охвату последних 100)
  spread_pct — текущий спред
  mid_frac   — доля принтов СТРОГО ВНУТРИ текущего спреда (маркер wash-трейдинга:
               самосделки MM-бота печатаются между бидом и аском, не трогая книгу)
  edge_frac  — доля принтов на уровне/за лучшими ценами (маркер реального потока,
               бьющего в книгу)
  qty_uniq   — доля уникальных размеров принтов (боты повторяют размеры)
  clip_usdt  — медианный размер принта
"""
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor


from ersh.exchanges import BingX

OUT = "reports/flow_scan_results.json"

client = BingX()
info = client.get("/openApi/spot/v1/common/symbols")
symbols = [s["symbol"] for s in info["data"]["symbols"]
           if s.get("apiStateBuy") and s.get("apiStateSell") and s["symbol"].endswith("-USDT")]
print(f"тикеров к анализу: {len(symbols)}", flush=True)


def analyze(sym):
    try:
        trades = client.trades(sym, limit=100)
        if len(trades) < 10:
            return None
        depth = client.depth(sym, limit=5)
        if not depth["bids"] or not depth["asks"]:
            return None
        bid, ask = float(depth["bids"][0][0]), float(depth["asks"][0][0])
        if bid <= 0 or ask <= bid:
            return None
        span_ms = trades[0]["time"] - trades[-1]["time"]
        if span_ms <= 0:
            return None
        prints_h = len(trades) / (span_ms / 3.6e6)
        prices = [float(t["price"]) for t in trades]
        mid_in = sum(1 for p in prices if bid < p < ask)
        edge = sum(1 for p in prices if p <= bid or p >= ask)
        qtys = [t["qty"] for t in trades]
        notionals = sorted(t["quoteQty"] for t in trades)
        return {"symbol": sym,
                "prints_h": round(prints_h, 1),
                "spread_pct": round((ask - bid) / bid * 100, 3),
                "mid_frac": round(mid_in / len(trades), 2),
                "edge_frac": round(edge / len(trades), 2),
                "qty_uniq": round(len(set(qtys)) / len(qtys), 2),
                "clip_usdt": round(statistics.median(notionals), 2)}
    except Exception:
        return None


t0 = time.time()
results = []
with ThreadPoolExecutor(max_workers=4) as pool:
    for i, r in enumerate(pool.map(analyze, symbols)):
        if r:
            results.append(r)
        if (i + 1) % 200 == 0:
            print(f"  {i + 1}/{len(symbols)} ({time.time() - t0:.0f}с)", flush=True)

json.dump(results, open(OUT, "w"))
print(f"готово: {len(results)} тикеров с данными за {time.time() - t0:.0f}с -> {OUT}", flush=True)

# краткая выжимка: живой поток + пригодный спред
live = [r for r in results
        if r["prints_h"] >= 30 and r["spread_pct"] >= 0.3 and r["edge_frac"] >= 0.4]
live.sort(key=lambda r: -r["prints_h"] * r["spread_pct"])
print(f"\nживых кандидатов (принты бьют в книгу, спред >= 0.3%): {len(live)}")
for r in live[:20]:
    print(f"  {r['symbol']}: {r['prints_h']:.0f} принтов/ч, спред {r['spread_pct']:.2f}%, "
          f"edge {r['edge_frac']:.0%}, mid {r['mid_frac']:.0%}, клип ${r['clip_usdt']}")
