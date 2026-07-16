"""Второй проход: у топ-кандидатов проверяем двусторонность потока и стабильность спреда.

Два замера с паузой 60с. Критерии «пригоден для ерша»:
  - поток двусторонний: доля ударов вверх (BID) в 25–75%
  - спред стабилен: в обоих замерах >= 0.3% и сузился не более чем вдвое
  - свежие принты между замерами (поток не иссяк)
"""
import json
import sys
import time


from ersh.exchanges import BingX

SCR = "reports"
results = json.load(open(f"{SCR}/flow_scan_results.json"))
live = [r for r in results
        if r["prints_h"] >= 30 and r["spread_pct"] >= 0.3 and r["edge_frac"] >= 0.4]
live.sort(key=lambda r: -min(r["prints_h"], 500) * min(r["spread_pct"], 5))
cands = live[:30]
print(f"проверяю {len(cands)} кандидатов, два замера с паузой 60с", flush=True)

client = BingX()

def snap(sym):
    trades = client.trades(sym, limit=100)
    depth = client.depth(sym, limit=5)
    bid = float(depth["bids"][0][0]) if depth["bids"] else 0
    ask = float(depth["asks"][0][0]) if depth["asks"] else 0
    spread = (ask - bid) / bid * 100 if bid > 0 and ask > bid else 0
    last_ts = trades[0]["time"] if trades else 0
    bid_hits = sum(1 for t in trades if t["tradeType"] == "BID")
    return {"spread": spread, "last_ts": last_ts, "bid_frac": bid_hits / max(1, len(trades))}

s1 = {}
for r in cands:
    try:
        s1[r["symbol"]] = snap(r["symbol"])
    except Exception:
        pass
time.sleep(60)
ok = []
for r in cands:
    sym = r["symbol"]
    if sym not in s1:
        continue
    try:
        s2 = snap(sym)
    except Exception:
        continue
    a, b = s1[sym], s2
    two_sided = 0.25 <= b["bid_frac"] <= 0.75
    spread_ok = a["spread"] >= 0.3 and b["spread"] >= 0.3 and b["spread"] >= a["spread"] / 2
    fresh = b["last_ts"] > a["last_ts"]
    verdict = two_sided and spread_ok and fresh
    print(f"  {sym}: спред {a['spread']:.2f}%->{b['spread']:.2f}%, buy-доля {b['bid_frac']:.0%}, "
          f"свежие принты: {'да' if fresh else 'НЕТ'} => {'✅' if verdict else '—'}", flush=True)
    if verdict:
        ok.append({**r, "spread_now": round(b["spread"], 2), "bid_frac": round(b["bid_frac"], 2)})

json.dump(ok, open(f"{SCR}/flow_verified.json", "w"))
print(f"\nпрошли проверку: {len(ok)}")
for r in sorted(ok, key=lambda x: -x["spread_now"]):
    print(f"  {r['symbol']}: спред {r['spread_now']}%, {r['prints_h']:.0f} принтов/ч, "
          f"buy {r['bid_frac']:.0%}, клип ${r['clip_usdt']}")
