"""Вотчер: наблюдение за тикером в реальном времени.

Отслеживает диапазон котирования бота («ёрш»), размер клипа, частоту ударов,
присутствие конкурентов в стакане и оценивает потенциальный доход в час.

CLI:  python3 -m ersh.watch SYLUSDT --minutes 10
"""

import argparse
import statistics
import time
from collections import deque

from .mexc import Mexc, TradeFeed, collapse_hits, infer_tick


def quantile(sorted_vals, q):
    if not sorted_vals:
        return 0.0
    i = q * (len(sorted_vals) - 1)
    lo = int(i)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (i - lo)


class Watcher:
    """Копит сделки и снимки стакана, отдаёт метрики «ерша»."""

    def __init__(self, symbol, client=None, window_min=15.0):
        self.symbol = symbol
        self.client = client or Mexc()
        self.window_ms = window_min * 60_000
        self.feed = TradeFeed(self.client, symbol)
        self.hits = deque()          # [side, notional, ts, price]
        self.depth = None
        self.depth_samples = 0
        self.foreign_samples = 0
        self.started = time.time()

    def poll(self):
        """Один опрос: новые сделки + стакан. Возвращает список новых ударов."""
        fresh = self.feed.poll()
        new_hits = collapse_hits(fresh) if fresh else []
        for h in new_hits:
            self.hits.append(h)
        depth = self.client.depth(self.symbol, limit=20)
        if depth and depth.get("bids") and depth.get("asks"):
            self.depth = depth
            self.depth_samples += 1
            # Конкурент = чья-то заявка стоит ВНУТРИ зоны ударов бота (где встали бы мы).
            # Свои мимолётные котировки бот ставит и тут же бьёт — при опросе раз в
            # ~2с они попадают в выборку редко; постоянная заявка внутри = чужая.
            if len(self.hits) >= 6:
                prices = sorted(h[3] for h in self.hits)
                lo, hi = quantile(prices, 0.1), quantile(prices, 0.9)
                bb = float(depth["bids"][0][0])
                ba = float(depth["asks"][0][0])
                if lo < bb <= hi or lo <= ba < hi:
                    self.foreign_samples += 1
        now_ms = time.time() * 1000
        while self.hits and self.hits[0][2] < now_ms - self.window_ms:
            self.hits.popleft()
        return new_hits

    def prime(self):
        """Стартовое наполнение окна историей из ленты (до 100 сделок)."""
        trades = self.client.trades(self.symbol, limit=100)
        if trades:
            for h in collapse_hits(list(reversed(trades))):
                self.hits.append(h)
            now = time.time()
            for t in trades:  # тот же снимок в seen, чтобы не задвоить и не потерять
                self.feed.seen[(t["time"], t["price"], t["qty"], t.get("tradeType"))] = now
        self.feed.primed = True

    def metrics(self, order_usdt=None, maker_fee=0.0):
        hits = list(self.hits)
        m = {"symbol": self.symbol, "n_hits": len(hits)}
        if self.depth:
            bid = float(self.depth["bids"][0][0])
            ask = float(self.depth["asks"][0][0])
            mid = (bid + ask) / 2
            m.update(bid=bid, ask=ask, spread_pct=(ask - bid) / mid * 100,
                     tick=infer_tick(self.depth))
        if len(hits) < 6:
            return m

        prices = sorted(h[3] for h in hits)
        mid = m.get("bid") and (m["bid"] + m["ask"]) / 2 or statistics.median(prices)
        band_lo, band_hi = quantile(prices, 0.1), quantile(prices, 0.9)
        buy_p, sell_p = quantile(prices, 0.2), quantile(prices, 0.8)

        span_min = (hits[-1][2] - hits[0][2]) / 60_000 or 1e-9
        bid_hits = [h for h in hits if h[0] == "BID"]   # удары вверх (покупки тейкером)
        ask_hits = [h for h in hits if h[0] == "ASK"]   # удары вниз (продажи тейкером)
        clip = statistics.median(h[1] for h in hits)

        # цикл = нас налили внизу (ASK-удар) + вынесли вверху (BID-удар)
        pairs_per_hour = min(len(bid_hits), len(ask_hits)) / span_min * 60
        size = min(order_usdt or clip * 0.8, clip)
        capture_pct = (sell_p - buy_p) / mid * 100 - 2 * maker_fee * 100
        income_hr = pairs_per_hour * size * max(capture_pct, 0) / 100

        m.update(
            band_lo=band_lo, band_hi=band_hi, buy_price=buy_p, sell_price=sell_p,
            band_pct=(band_hi - band_lo) / mid * 100,
            clip_usdt=clip,
            hits_per_min=len(hits) / span_min,
            bid_hits=len(bid_hits), ask_hits=len(ask_hits),
            pairs_per_hour=pairs_per_hour,
            capture_pct=capture_pct,
            est_income_hr=income_hr,
            competition=self.foreign_samples / self.depth_samples if self.depth_samples else 0.0,
        )
        return m


def fmt_metrics(m):
    if "band_lo" not in m:
        return f"{m['symbol']}: мало данных (ударов: {m.get('n_hits', 0)})"
    return (f"{m['symbol']}: спред {m.get('spread_pct', 0):.2f}% | ёрш {m['band_lo']:.8g}–{m['band_hi']:.8g} "
            f"({m['band_pct']:.2f}%) | клип ${m['clip_usdt']:.2f} | {m['hits_per_min']:.1f} уд/мин "
            f"(▲{m['bid_hits']}/▼{m['ask_hits']}) | конкуренты {m['competition'] * 100:.0f}% | "
            f"захват {m['capture_pct']:.2f}% | ~${m['est_income_hr']:.2f}/час")


def main():
    ap = argparse.ArgumentParser(description="Вотчер ершистого тикера")
    ap.add_argument("symbol")
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--poll", type=float, default=2.0, help="интервал опроса, сек")
    ap.add_argument("--order-usdt", type=float, default=None)
    ap.add_argument("--window", type=float, default=15, help="окно метрик, мин")
    args = ap.parse_args()

    w = Watcher(args.symbol.upper(), window_min=args.window)
    w.prime()
    print(f"Наблюдаю {w.symbol} {args.minutes} мин (опрос каждые {args.poll}с)...")
    deadline = time.time() + args.minutes * 60
    last_status = 0.0
    while time.time() < deadline:
        new = w.poll()
        for h in new:
            side = "▲BID" if h[0] == "BID" else "▼ASK"
            print(f"  {time.strftime('%H:%M:%S', time.localtime(h[2] / 1000))} {side} "
                  f"${h[1]:.2f} @ {h[3]:.8g}")
        if time.time() - last_status > 30:
            print(fmt_metrics(w.metrics(args.order_usdt)))
            last_status = time.time()
        time.sleep(args.poll)

    print("\n=== Итог ===")
    print(fmt_metrics(w.metrics(args.order_usdt)))


if __name__ == "__main__":
    main()
