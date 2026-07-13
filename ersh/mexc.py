"""Тонкий клиент публичного REST API спота MEXC."""

import time

import requests

BASE = "https://api.mexc.com"


class Mexc:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "ersh/0.1"

    def get(self, path, **params):
        for attempt in range(3):
            try:
                r = self.s.get(BASE + path, params=params, timeout=15)
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

    def tickers_24h(self):
        return self.get("/api/v3/ticker/24hr")

    def trades(self, symbol, limit=100):
        return self.get("/api/v3/trades", symbol=symbol, limit=limit)

    def depth(self, symbol, limit=20):
        return self.get("/api/v3/depth", symbol=symbol, limit=limit)


class TradeFeed:
    """Дедупликация ленты сделок между опросами.

    MEXC не отдаёт id сделок (id=null), поэтому ключ — (time, price, qty, side).
    Возвращает только новые сделки в хронологическом порядке.
    """

    def __init__(self, client, symbol, limit=100):
        self.client = client
        self.symbol = symbol
        self.limit = limit
        self.seen = {}
        self.primed = False

    def poll(self, now=None):
        trades = self.client.trades(self.symbol, limit=self.limit)
        if not trades:
            return []
        now = now or time.time()
        fresh = []
        for t in reversed(trades):  # API отдаёт свежие первыми
            key = (t["time"], t["price"], t["qty"], t.get("tradeType"))
            if key in self.seen:
                continue
            self.seen[key] = now
            fresh.append(t)
        # чистим ключи старше 30 минут, чтобы не расти бесконечно
        cutoff = now - 1800
        for k in [k for k, ts in self.seen.items() if ts < cutoff]:
            del self.seen[k]
        if not self.primed:
            self.primed = True
            return []  # первый опрос — только заполняем seen
        return fresh


def collapse_hits(trades):
    """Свернуть сделки с одинаковым timestamp+стороной в «удары» (одна рыночная заявка).

    Возвращает список [side, notional_usdt, ts, price_last].
    """
    hits = []
    for tr in trades:
        side = tr.get("tradeType") or ("ASK" if tr.get("isBuyerMaker") else "BID")
        notional = float(tr["quoteQty"])
        ts = tr["time"]
        price = float(tr["price"])
        if hits and hits[-1][0] == side and hits[-1][2] == ts:
            hits[-1][1] += notional
            hits[-1][3] = price
        else:
            hits.append([side, notional, ts, price])
    return hits


def infer_tick(depth):
    """Шаг цены по количеству знаков в ценах стакана."""
    decimals = 0
    for side in ("bids", "asks"):
        for p, _ in depth.get(side, [])[:5]:
            if "." in p:
                decimals = max(decimals, len(p.split(".")[1].rstrip("0") or "0"))
    return 10 ** -decimals if decimals else 1.0
