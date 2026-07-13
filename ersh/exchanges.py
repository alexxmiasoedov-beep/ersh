"""Клиенты публичных REST API бирж, нормализованные под формат MEXC.

Единый интерфейс (тот же, что у ersh.mexc.Mexc):
  tickers_24h() -> [{symbol, bidPrice, askPrice, quoteVolume, lastPrice}]
  trades(symbol, limit) -> свежие первыми: [{time(ms), price, qty, quoteQty,
                           tradeType: BID (удар вверх) | ASK (удар вниз)}]
  depth(symbol, limit) -> {"bids": [[p, q], ...] по убыванию,
                           "asks": [[p, q], ...] по возрастанию}, цены строками

Благодаря нормализации скринер/вотчер/симулятор работают с любой биржей.
"""

import time

import requests

from .mexc import Mexc


class Rest:
    BASE = ""

    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "ersh/0.1"

    def get(self, path, **params):
        for attempt in range(3):
            try:
                r = self.s.get(self.BASE + path, params=params, timeout=15)
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


class BingX(Rest):
    BASE = "https://open-api.bingx.com"

    def _data(self, path, **params):
        resp = self.get(path, **params)
        if not resp or resp.get("code") != 0:
            raise RuntimeError(f"BingX {path}: {resp and resp.get('msg')}")
        return resp["data"]

    def tickers_24h(self):
        return [{"symbol": t["symbol"],
                 "bidPrice": t.get("bidPrice") or 0,
                 "askPrice": t.get("askPrice") or 0,
                 "quoteVolume": t.get("quoteVolume") or 0,
                 "lastPrice": t.get("lastPrice") or 0}
                for t in self._data("/openApi/spot/v1/ticker/24hr")]

    def trades(self, symbol, limit=100):
        out = []
        for t in self._data("/openApi/spot/v1/market/trades", symbol=symbol, limit=limit):
            price, qty = float(t["price"]), float(t["qty"])
            out.append({"time": int(t["time"]), "price": str(t["price"]),
                        "qty": str(t["qty"]), "quoteQty": price * qty,
                        # buyerMaker=true: тейкер продал -> удар вниз
                        "tradeType": "ASK" if t.get("buyerMaker") else "BID"})
        return out

    def depth(self, symbol, limit=20):
        d = self._data("/openApi/spot/v1/market/depth", symbol=symbol, limit=limit)
        # BingX отдаёт asks по убыванию — приводим к обычному порядку
        bids = sorted(d.get("bids", []), key=lambda lv: float(lv[0]), reverse=True)
        asks = sorted(d.get("asks", []), key=lambda lv: float(lv[0]))
        return {"bids": [[str(p), str(q)] for p, q in bids],
                "asks": [[str(p), str(q)] for p, q in asks]}


class Gate(Rest):
    BASE = "https://api.gateio.ws/api/v4"

    def tickers_24h(self):
        return [{"symbol": t["currency_pair"],
                 "bidPrice": t.get("highest_bid") or 0,
                 "askPrice": t.get("lowest_ask") or 0,
                 "quoteVolume": t.get("quote_volume") or 0,
                 "lastPrice": t.get("last") or 0}
                for t in self.get("/spot/tickers")]

    def trades(self, symbol, limit=100):
        out = []
        for t in self.get("/spot/trades", currency_pair=symbol, limit=limit):
            price, qty = float(t["price"]), float(t["amount"])
            out.append({"time": int(float(t["create_time_ms"])), "price": t["price"],
                        "qty": t["amount"], "quoteQty": price * qty,
                        # side — сторона тейкера: buy -> удар вверх
                        "tradeType": "BID" if t["side"] == "buy" else "ASK"})
        return out

    def depth(self, symbol, limit=20):
        d = self.get("/spot/order_book", currency_pair=symbol, limit=limit)
        return {"bids": d.get("bids", []), "asks": d.get("asks", [])}


EXCHANGES = {"mexc": Mexc, "bingx": BingX, "gate": Gate}
NAMES = {"mexc": "MEXC", "bingx": "BingX", "gate": "Gate"}

# Спотовые комиссии по умолчанию (VIP0), доли: 0.001 = 0.1%
DEFAULT_FEES = {
    "mexc": {"maker": 0.0, "taker": 0.0005},
    "bingx": {"maker": 0.001, "taker": 0.001},
    "gate": {"maker": 0.002, "taker": 0.002},
}


def make_client(name):
    return EXCHANGES[name.lower()]()
