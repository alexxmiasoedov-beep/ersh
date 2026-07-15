"""Реальная торговля «ершом» на BingX через ccxt. Одна позиция за раз на тикер.

Тот же конечный автомат, что в симуляторе (FLAT -> BUYING -> HOLDING ->
SELLING -> FLAT), но заявки настоящие. Рыночные данные — по-прежнему через
публичный REST (ersh.exchanges), приватные операции — через ccxt.

Предохранители:
  * торгуем только тикеры из явного вайтлиста cfg["live"]["tickers"];
  * фиксированный размер заявки order_usdt, максимум max_positions позиций;
  * дневной стоп-лосс max_daily_loss: при достижении новые входы запрещены;
  * kill switch: файл state/LIVE_STOP — новые входы запрещены, лимитки
    на покупку снимаются (позиции довыходят штатно);
  * стоп по времени как в симе: висим дольше max_hold_min — выходим маркетом.

CLI:  .venv/bin/python -m ersh.live [--config config.json]
"""

import argparse
import json
import os
import time

import ccxt

from .exchanges import make_client
from .run import load_config
from .tg import Notifier
from .watch import Watcher

LIVE_DEFAULTS = {
    "exchange": "bingx",
    "tickers": [],              # явный вайтлист, напр. ["GTC-USDT", "MBOX-USDT"]
    "order_usdt": 5.0,
    "max_positions": 3,
    "max_daily_loss": 5.0,      # USDT реализованного минуса за UTC-сутки
    "max_hold_min": 45.0,
    "reprice_sec": 45.0,
    "min_capture_pct": 0.05,
    "poll_seconds": 2.0,
}


class Position:
    def __init__(self):
        self.state = "FLAT"     # FLAT | BUYING | HOLDING | SELLING
        self.order_id = None
        self.placed_at = 0.0
        self.placed_price = 0.0
        self.entry_qty = 0.0    # base, уже за вычетом комиссии в базовой валюте
        self.entry_cost = 0.0   # USDT, потрачено с комиссией
        self.entry_time = 0.0


class LiveTrader:
    def __init__(self, cfg):
        live = {**LIVE_DEFAULTS, **cfg.get("live", {})}
        if not live["tickers"]:
            raise SystemExit("cfg['live']['tickers'] пуст — явно перечислите тикеры")
        if not (cfg.get("bingx_api_key") and cfg.get("bingx_api_secret")):
            raise SystemExit("нет bingx_api_key / bingx_api_secret в конфиге")
        self.cfg = cfg
        self.live = live
        self.tg = Notifier(cfg["telegram_token"], cfg["telegram_chat_id"])
        self.x = ccxt.bingx({"apiKey": cfg["bingx_api_key"],
                             "secret": cfg["bingx_api_secret"],
                             "options": {"defaultType": "spot"}})
        self.x.load_markets()
        # BingX запрещает API-торговлю на части символов (код 100421)
        info = self.x.spotV1PublicGetCommonSymbols()
        allowed = {s["symbol"] for s in info["data"]["symbols"]
                   if s.get("apiStateBuy") and s.get("apiStateSell")}
        bad = [t for t in live["tickers"] if t not in allowed]
        if bad:
            print(f"⚠️ API-торговля запрещена, выкидываю: {', '.join(bad)}", flush=True)
            live["tickers"] = [t for t in live["tickers"] if t in allowed]
        if not live["tickers"]:
            raise SystemExit("после фильтра apiState не осталось тикеров")
        fees = cfg.get("fees", {}).get("bingx", {})
        self.maker_fee = fees.get("maker", 0.001)
        self.watchers = {t: Watcher(t, make_client("bingx")) for t in live["tickers"]}
        self.pos = {t: Position() for t in live["tickers"]}
        self.stop_file = os.path.join(cfg["state_dir"], "LIVE_STOP")
        self.state_path = os.path.join(cfg["state_dir"], "live.json")
        self.day = self._utc_day()
        self.day_pnl = 0.0
        self.total = {"trades": 0, "pnl": 0.0}
        self.loss_stop_sent = False
        self._load_state()

    # ---------- утилиты ----------

    def _utc_day(self):
        return time.strftime("%Y-%m-%d", time.gmtime())

    def _log(self, text):
        print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)

    def _load_state(self):
        if os.path.exists(self.state_path):
            with open(self.state_path) as f:
                st = json.load(f)
            self.total.update(st.get("total", {}))
            if st.get("day") == self.day:
                self.day_pnl = st.get("day_pnl", 0.0)

    def _save_state(self):
        with open(self.state_path, "w") as f:
            json.dump({"total": self.total, "day": self.day,
                       "day_pnl": self.day_pnl}, f, indent=1)

    def ccxt_symbol(self, ticker):
        return ticker.replace("-", "/")

    def entries_blocked(self):
        if os.path.exists(self.stop_file):
            return "kill switch (state/LIVE_STOP)"
        if self.day != self._utc_day():
            self.day = self._utc_day()
            self.day_pnl = 0.0
            self.loss_stop_sent = False
        if self.day_pnl <= -self.live["max_daily_loss"]:
            if not self.loss_stop_sent:
                self.tg.send(f"🛑 LIVE: дневной лимит убытка "
                             f"{self.day_pnl:+.2f} USDT — входы остановлены до конца суток")
                self.loss_stop_sent = True
            return "дневной стоп-лосс"
        return None

    def open_count(self):
        return sum(1 for p in self.pos.values() if p.state != "FLAT")

    # ---------- работа с заявками ----------

    def fetch(self, ticker, order_id):
        return self.x.fetch_order(order_id, self.ccxt_symbol(ticker))

    def cancel_silent(self, ticker, order_id):
        """Снять заявку; если она уже исполнилась/снята — не падать."""
        try:
            self.x.cancel_order(order_id, self.ccxt_symbol(ticker))
        except (ccxt.OrderNotFound, ccxt.InvalidOrder):
            pass

    def filled_parts(self, o):
        """(qty базовой валюты за вычетом комиссии в базе, стоимость в USDT)."""
        filled = float(o.get("filled") or 0.0)
        avg = float(o.get("average") or o.get("price") or 0.0)
        cost = float(o.get("cost") or filled * avg)
        qty = filled
        for fee in (o.get("fees") or ([o["fee"]] if o.get("fee") else [])):
            if not fee or fee.get("cost") is None:
                continue
            cur = fee.get("currency")
            if cur and o["symbol"].startswith(cur + "/"):
                qty -= float(fee["cost"])       # комиссия удержана монетой
            elif cur == "USDT":
                cost += float(fee["cost"])      # комиссия удержана USDT
        if qty == filled and filled > 0:
            qty = filled * (1 - self.maker_fee)  # биржа не отдала fee — считаем сами
        return qty, cost

    def place_limit(self, ticker, side, qty, price):
        sym = self.ccxt_symbol(ticker)
        qty = float(self.x.amount_to_precision(sym, qty))
        price = float(self.x.price_to_precision(sym, price))
        o = self.x.create_order(sym, "limit", side, qty, price)
        return o["id"], price, qty

    # ---------- конечный автомат ----------

    def step(self, ticker):
        p = self.pos[ticker]
        w = self.watchers[ticker]
        w.poll()
        m = w.metrics(self.live["order_usdt"], self.maker_fee)
        if "band_lo" not in m or "bid" not in m:
            return
        tick = m.get("tick", 0.0) or 0.0
        buy_p = min(m["buy_price"], m["ask"] - tick)
        sell_p = max(m["sell_price"], m["bid"] + tick)
        capture = (sell_p - buy_p) / ((m["bid"] + m["ask"]) / 2) * 100 - 2 * self.maker_fee * 100

        if p.state == "FLAT":
            block = self.entries_blocked()
            if block or self.open_count() >= self.live["max_positions"]:
                return
            if capture < self.live["min_capture_pct"]:
                return
            qty = self.live["order_usdt"] / buy_p
            try:
                p.order_id, price, qty = self.place_limit(ticker, "buy", qty, buy_p)
            except ccxt.BaseError as e:
                self._log(f"⚠️ {ticker}: не смог поставить покупку: {e}")
                return
            p.state, p.placed_at, p.placed_price = "BUYING", time.time(), price
            self._log(f"{ticker}: покупка {qty:.8g} @ {price:.8g} (захват {capture:.2f}%)")
            return

        if p.state == "BUYING":
            o = self.fetch(ticker, p.order_id)
            if o["status"] == "closed":
                p.entry_qty, p.entry_cost = self.filled_parts(o)
                p.entry_time = time.time()
                p.state = "HOLDING"
                avg = float(o.get("average") or p.placed_price)
                self.tg.send(f"[LIVE] {ticker}: 🟢 ВХОД: куплено "
                             f"${p.entry_cost:.2f} @ {avg:.8g}")
            elif (time.time() - p.placed_at > self.live["reprice_sec"]
                  or os.path.exists(self.stop_file)):
                self.cancel_silent(ticker, p.order_id)
                o = self.fetch(ticker, p.order_id)
                p.entry_qty, p.entry_cost = self.filled_parts(o)
                if p.entry_qty > 0:              # частичный филл — доводим до выхода
                    p.entry_time = time.time()
                    p.state = "HOLDING"
                    self.tg.send(f"[LIVE] {ticker}: 🟢 ВХОД (частично): "
                                 f"${p.entry_cost:.2f}")
                else:
                    p.state = "FLAT"
            return

        if p.state == "HOLDING":
            try:
                p.order_id, price, _ = self.place_limit(ticker, "sell", p.entry_qty, sell_p)
            except ccxt.BaseError as e:
                self._log(f"⚠️ {ticker}: не смог поставить продажу: {e}")
                return
            p.state, p.placed_at, p.placed_price = "SELLING", time.time(), price
            self._log(f"{ticker}: продажа @ {price:.8g}")
            return

        if p.state == "SELLING":
            o = self.fetch(ticker, p.order_id)
            if o["status"] == "closed":
                self.settle(ticker, o, taker=False)
            elif time.time() - p.entry_time > self.live["max_hold_min"] * 60:
                self.cancel_silent(ticker, p.order_id)
                o = self.fetch(ticker, p.order_id)
                left_qty = p.entry_qty - float(o.get("filled") or 0.0)
                sym = self.ccxt_symbol(ticker)
                left_qty = float(self.x.amount_to_precision(sym, left_qty))
                if left_qty > 0:
                    try:
                        mo = self.x.create_order(sym, "market", "sell", left_qty)
                        self.settle(ticker, o, taker=True, market_order_id=mo["id"])
                        return
                    except ccxt.BaseError as e:
                        self._log(f"⚠️ {ticker}: маркет-выход не прошёл: {e} — повторю")
                        p.state = "HOLDING"   # попробуем ещё раз лимиткой
                        return
                self.settle(ticker, o, taker=False)
            elif time.time() - p.placed_at > self.live["reprice_sec"]:
                self.cancel_silent(ticker, p.order_id)
                o = self.fetch(ticker, p.order_id)
                sold_qty = float(o.get("filled") or 0.0)
                if sold_qty > 0 and o["status"] == "closed":
                    self.settle(ticker, o, taker=False)
                else:
                    p.entry_qty -= sold_qty   # частично продали — остаток заново
                    p.state = "HOLDING"
            return

    def settle(self, ticker, sell_order, taker, market_order_id=None):
        """Закрытие позиции: посчитать реализованный PnL по фактическим филлам."""
        p = self.pos[ticker]
        proceeds = 0.0
        for oid in filter(None, [sell_order.get("id"), market_order_id]):
            try:
                o = self.fetch(ticker, oid)
            except ccxt.BaseError:
                continue
            filled = float(o.get("filled") or 0.0)
            avg = float(o.get("average") or o.get("price") or 0.0)
            got = float(o.get("cost") or filled * avg)
            for fee in (o.get("fees") or ([o["fee"]] if o.get("fee") else [])):
                if fee and fee.get("currency") == "USDT" and fee.get("cost"):
                    got -= float(fee["cost"])
            proceeds += got
        pnl = proceeds - p.entry_cost
        held = (time.time() - p.entry_time) / 60
        self.day_pnl += pnl
        self.total["trades"] += 1
        self.total["pnl"] += pnl
        self._save_state()
        tag = "⏱ стоп по времени, выход маркетом" if taker else "🔴 ВЫХОД"
        self.tg.send(f"[LIVE] {ticker}: {tag}: PnL {pnl:+.4f} USDT, "
                     f"в позиции {held:.1f} мин\n"
                     f"💰 LIVE всего: {self.total['pnl']:+.4f} USDT за "
                     f"{self.total['trades']} сделок (сегодня {self.day_pnl:+.4f})")
        self.pos[ticker] = Position()

    # ---------- главный цикл ----------

    def run(self):
        bal = self.x.fetch_balance()
        usdt = bal.get("USDT", {}).get("free", 0.0)
        self.tg.send(f"🔴 LIVE-торговля запущена: BingX, тикеры "
                     f"{', '.join(self.live['tickers'])}, заявка "
                     f"${self.live['order_usdt']:.0f}, макс позиций "
                     f"{self.live['max_positions']}, дневной стоп "
                     f"-${self.live['max_daily_loss']:.0f}. Свободно {usdt:.2f} USDT")
        for w in self.watchers.values():
            w.prime()
        while True:
            for t in self.live["tickers"]:
                try:
                    self.step(t)
                except ccxt.NetworkError as e:
                    self._log(f"⚠️ {t}: сеть: {e}")
                except Exception as e:
                    self._log(f"⚠️ {t}: {type(e).__name__}: {e}")
                time.sleep(self.live["poll_seconds"] / max(1, len(self.live["tickers"])))


def main():
    ap = argparse.ArgumentParser(description="ёрш: реальная торговля (BingX)")
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args()
    LiveTrader(load_config(args.config)).run()


if __name__ == "__main__":
    main()
