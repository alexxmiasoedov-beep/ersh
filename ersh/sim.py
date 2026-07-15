"""Симулятор сбора спреда, максимально приближенный к реальности.

Paper-trading об живую ленту биржи. Две модели исполнения:

optimistic — лимитка исполняется, если принт прошёл лучше нашей цены, либо
по нашей цене после прохождения объёма очереди (+ наш объём). Завышает
результат: заявка невидима для рынка, MM-бот и конкуренты не реагируют.

pessimistic (по умолчанию) — сверху добавлены три поправки:
  * задержка FILL_DELAY_SEC: принты сразу после постановки не считаются
    (заявка ещё «не доехала» до стакана);
  * принт ровно по нашей цене двигает очередь только на AT_PRICE_SHARE
    своего объёма (остальное съедают MM-бот и конкуренты впереди нас);
  * мгновенный филл — только если принт прошёл СКВОЗЬ нашу цену.
Реальность лежит между двумя моделями, ближе к пессимистичной.

Стратегия — как в видео: одна позиция за раз.
FLAT -> ставим лимитку на покупку у нижней границы ерша ->
LONG -> ставим лимитку на продажу у верхней границы -> FLAT.

CLI:  python3 -m ersh.sim SYLUSDT --minutes 15
"""

import argparse
import time

from .mexc import Mexc
from .watch import Watcher, quantile

MIN_NOTIONAL = 1.0          # минимальная заявка на споте MEXC, USDT

# Параметры пессимистичной модели исполнения
FILL_DELAY_SEC = 3.0        # заявка «доезжает» до стакана: ранние принты не наши
AT_PRICE_SHARE = 0.25       # доля принта по нашей цене, двигающая нашу очередь


class Order:
    def __init__(self, side, price, notional, queue_ahead):
        self.side = side                  # BUY / SELL
        self.price = price
        self.notional = notional          # USDT
        self.queue_ahead = queue_ahead    # USDT перед нами в очереди на уровне
        self.filled_through = 0.0         # объём, прошедший по нашей цене после постановки
        self.placed_at = time.time()


class Simulator:
    def __init__(self, symbol, client=None, order_usdt=None, maker_fee=0.0, taker_fee=0.0005,
                 window_min=15.0, max_hold_min=45.0, reprice_sec=45.0, min_capture_pct=0.05,
                 fill_model="pessimistic", on_event=None):
        self.symbol = symbol
        self.client = client or Mexc()
        self.watcher = Watcher(symbol, self.client, window_min=window_min)
        self.order_usdt = order_usdt
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.max_hold_sec = max_hold_min * 60
        self.reprice_sec = reprice_sec
        self.min_capture_pct = min_capture_pct
        self.fill_model = fill_model
        self.on_event = on_event or (lambda e: None)

        self.state = "FLAT"               # FLAT | BUY_PLACED | LONG | SELL_PLACED
        self.order = None
        self.entry_price = None
        self.entry_notional = None
        self.entry_time = None
        self.stats = {"trades": 0, "wins": 0, "pnl": 0.0, "turnover": 0.0, "timeouts": 0}
        self.stop = False

    # ---------- инфраструктура ----------

    def emit(self, kind, text, **data):
        self.on_event({"kind": kind, "symbol": self.symbol, "text": text,
                       "ts": time.time(), **data})

    def level_notional(self, side, price):
        """Сколько USDT стоит в стакане на уровне price (очередь перед нами)."""
        if not self.watcher.depth:
            return 0.0
        for p, q in self.watcher.depth["bids" if side == "BUY" else "asks"]:
            if abs(float(p) - price) < 1e-12:
                return float(p) * float(q)
        return 0.0

    def order_size(self, clip):
        size = self.order_usdt if self.order_usdt else clip * 0.8
        return max(MIN_NOTIONAL, min(size, clip))  # не больше клипа бота

    # ---------- модель исполнения ----------

    def check_fill(self, new_hits):
        """Проверка лимитки об новые принты. По нашей цене — только после
        прохождения объёма очереди + нашего объёма; в пессимистичной модели
        принты сразу после постановки не считаются, а по нашей цене засчитывается
        лишь AT_PRICE_SHARE объёма (см. докстринг модуля)."""
        o = self.order
        if not o:
            return False
        pess = self.fill_model == "pessimistic"
        for side, notional, ts, price in new_hits:
            if pess and ts / 1000.0 < o.placed_at + FILL_DELAY_SEC:
                continue                              # заявка ещё не стояла в стакане
            through = price < o.price - 1e-12 if o.side == "BUY" else price > o.price + 1e-12
            if through:
                return True                           # прошли сквозь наш уровень
            if abs(price - o.price) < 1e-12:
                o.filled_through += notional * (AT_PRICE_SHARE if pess else 1.0)
                if o.filled_through >= o.queue_ahead + o.notional:
                    return True
        return False

    # ---------- стратегия ----------

    def target_prices(self, m):
        """Цены входа/выхода: квантили ерша, зажатые в текущий спред (не пересекать)."""
        tick = m.get("tick", 0.0) or 0.0
        buy = min(m["buy_price"], m["ask"] - tick)
        sell = max(m["sell_price"], m["bid"] + tick)
        return buy, sell

    def step(self):
        new_hits = self.watcher.poll()
        m = self.watcher.metrics(self.order_usdt, self.maker_fee)
        if "band_lo" not in m or "bid" not in m:
            return

        buy_p, sell_p = self.target_prices(m)
        capture = (sell_p - buy_p) / ((m["bid"] + m["ask"]) / 2) * 100 - 2 * self.maker_fee * 100

        if self.state == "FLAT":
            if capture < self.min_capture_pct:
                return  # спред не окупается — сидим в стороне
            size = self.order_size(m["clip_usdt"])
            self.order = Order("BUY", buy_p, size, self.level_notional("BUY", buy_p))
            self.state = "BUY_PLACED"
            self.emit("place_buy", f"ставлю покупку ${size:.2f} @ {buy_p:.8g} "
                                   f"(ёрш {m['band_lo']:.8g}–{m['band_hi']:.8g}, захват {capture:.2f}%)",
                      price=buy_p, size=size)
            return

        if self.state == "BUY_PLACED":
            if self.check_fill(new_hits):
                self.entry_price = self.order.price
                self.entry_notional = self.order.notional
                self.entry_time = time.time()
                self.state = "LONG"
                self.emit("entry", f"🟢 ВХОД: куплено ${self.entry_notional:.2f} @ {self.entry_price:.8g}",
                          price=self.entry_price, size=self.entry_notional)
                self.order = None
            elif time.time() - self.order.placed_at > self.reprice_sec:
                # ёрш уехал — переставляемся (в реальности: пробел + новая лимитка)
                self.order = None
                self.state = "FLAT"
            return

        if self.state == "LONG":
            size = self.entry_notional
            self.order = Order("SELL", sell_p, size, self.level_notional("SELL", sell_p))
            self.state = "SELL_PLACED"
            self.emit("place_sell", f"ставлю продажу @ {sell_p:.8g}", price=sell_p, size=size)
            return

        if self.state == "SELL_PLACED":
            if self.check_fill(new_hits):
                self.close(self.order.price, taker=False)
            elif time.time() - self.entry_time > self.max_hold_sec:
                # стоп по времени: выходим тейкером по best bid — честная цена бегства
                self.stats["timeouts"] += 1
                self.close(m["bid"], taker=True)
            elif time.time() - self.order.placed_at > self.reprice_sec:
                self.order = None
                self.state = "LONG"  # переставим продажу по свежим квантилям
            return

    def close(self, exit_price, taker):
        gross = (exit_price - self.entry_price) / self.entry_price
        fees = self.maker_fee + (self.taker_fee if taker else self.maker_fee)
        pnl = self.entry_notional * (gross - fees)
        held = time.time() - self.entry_time
        self.stats["trades"] += 1
        self.stats["pnl"] += pnl
        self.stats["turnover"] += self.entry_notional
        if pnl > 0:
            self.stats["wins"] += 1
        tag = "⏱ стоп по времени, выход тейкером" if taker else "🔴 ВЫХОД"
        self.emit("exit", f"{tag}: продано @ {exit_price:.8g}, PnL {pnl:+.4f} USDT "
                          f"({gross * 100 - fees * 100:+.2f}%), в позиции {held / 60:.1f} мин",
                  price=exit_price, pnl=pnl, taker=taker)
        self.order = None
        self.entry_price = None
        self.state = "FLAT"

    # ---------- запуск ----------

    def run(self, minutes=None, poll=2.0):
        self.watcher.prime()
        self.emit("start", f"симуляция запущена (заявка "
                           f"{'$%.2f' % self.order_usdt if self.order_usdt else 'авто по клипу'})")
        deadline = time.time() + minutes * 60 if minutes else None
        while not self.stop and (deadline is None or time.time() < deadline):
            try:
                self.step()
            except Exception as e:
                self.emit("error", f"ошибка: {e}")
                time.sleep(5)
            time.sleep(poll)
        # финальный отчёт
        s = self.stats
        wr = s["wins"] / s["trades"] * 100 if s["trades"] else 0
        self.emit("summary", f"итог: сделок {s['trades']}, winrate {wr:.0f}%, "
                             f"PnL {s['pnl']:+.4f} USDT, оборот ${s['turnover']:.2f}, "
                             f"стопов по времени {s['timeouts']}")
        return s


def main():
    ap = argparse.ArgumentParser(description="Симулятор сбора спреда")
    ap.add_argument("symbol")
    ap.add_argument("--minutes", type=float, default=15)
    ap.add_argument("--poll", type=float, default=2.0)
    ap.add_argument("--order-usdt", type=float, default=None)
    ap.add_argument("--maker-fee", type=float, default=0.0)
    ap.add_argument("--taker-fee", type=float, default=0.0005)
    ap.add_argument("--min-capture", type=float, default=0.05,
                    help="мин. захват спреда, %%, ниже которого не торгуем")
    ap.add_argument("--exchange", default="mexc", help="mexc | bingx | gate")
    ap.add_argument("--fill-model", default="pessimistic", choices=["pessimistic", "optimistic"])
    args = ap.parse_args()

    def printer(e):
        print(f"[{time.strftime('%H:%M:%S')}] {e['symbol']} {e['text']}", flush=True)

    from .exchanges import make_client
    sim = Simulator(args.symbol.upper(), client=make_client(args.exchange), order_usdt=args.order_usdt,
                    maker_fee=args.maker_fee, taker_fee=args.taker_fee,
                    min_capture_pct=args.min_capture, fill_model=args.fill_model,
                    on_event=printer)
    sim.run(minutes=args.minutes, poll=args.poll)


if __name__ == "__main__":
    main()
