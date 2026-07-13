"""Оркестратор: автообновляемый вотчлист + симуляторы + сигналы в Telegram.

Цикл:
  1. Раз в refresh_minutes прогоняет скринер по всем парам MEXC.
  2. Обновляет вотчлист с гистерезисом: добавляет тикеры со score >= add_score,
     убирает со score < drop_score (или пропавшие). Изменения — в Telegram.
  3. На каждый тикер вотчлиста крутится свой симулятор; входы/выходы и
     PnL-отчёты шлются в Telegram.
  4. Раз в час — сводка по всем тикерам.

Состояние (вотчлист, накопленный PnL) переживает рестарт в state/state.json.

CLI:  python3 -m ersh.run [--config config.json]
"""

import argparse
import json
import os
import threading
import time

from .mexc import Mexc
from .screener import ScreenParams, screen
from .sim import Simulator
from .tg import Notifier

DEFAULTS = {
    "telegram_token": None,
    "telegram_chat_id": None,
    "start_balance": 100.0,
    "watchlist_size": 3,
    "refresh_minutes": 60,
    "add_score": 0.65,
    "drop_score": 0.50,
    "max_competition": 0.4,
    "order_usdt": None,          # null = авто (80% клипа бота)
    "maker_fee": 0.0,
    "taker_fee": 0.0005,
    "poll_seconds": 2.0,
    "summary_minutes": 60,
    "state_dir": "state",
    "screener": {"min_vol": 500, "max_vol": 300000, "min_spread": 0.08, "max_candidates": 150},
}


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        with open(path) as f:
            user = json.load(f)
        for k, v in user.items():
            if k == "screener" and isinstance(v, dict):
                cfg["screener"] = {**DEFAULTS["screener"], **v}
            else:
                cfg[k] = v
    return cfg


class Orchestrator:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = Mexc()
        self.tg = Notifier(cfg["telegram_token"], cfg["telegram_chat_id"])
        self.sims = {}      # symbol -> (Simulator, Thread)
        self.scores = {}    # symbol -> последний score из скринера
        self.state_path = os.path.join(cfg["state_dir"], "state.json")
        os.makedirs(cfg["state_dir"], exist_ok=True)
        self.total = {"trades": 0, "pnl": 0.0, "balance": cfg["start_balance"]}
        self._load_state()

    # ---------- состояние ----------

    def _load_state(self):
        if os.path.exists(self.state_path):
            with open(self.state_path) as f:
                st = json.load(f)
            self.scores = st.get("scores", {})
            self.total.update(st.get("total", {}))

    def _save_state(self):
        with open(self.state_path, "w") as f:
            json.dump({"scores": self.scores, "watchlist": sorted(self.sims),
                       "total": self.total}, f, indent=1)

    # ---------- симуляторы ----------

    def on_event(self, e):
        kind = e["kind"]
        if kind == "exit":
            self.total["trades"] += 1
            self.total["pnl"] += e.get("pnl", 0.0)
            self.total["balance"] += e.get("pnl", 0.0)
            self._save_state()
            self.tg.send(f"{e['symbol']}: {e['text']}\n"
                         f"💰 Баланс: ${self.total['balance']:.2f} "
                         f"(старт ${self.cfg['start_balance']:.0f}, "
                         f"PnL {self.total['pnl']:+.4f}, сделок {self.total['trades']})")
        elif kind in ("entry", "summary"):
            self.tg.send(f"{e['symbol']}: {e['text']}")
        else:
            print(f"[{time.strftime('%H:%M:%S')}] {e['symbol']} {e['text']}", flush=True)

    def start_sim(self, symbol):
        sim = Simulator(symbol, client=Mexc(), order_usdt=self.cfg["order_usdt"],
                        maker_fee=self.cfg["maker_fee"], taker_fee=self.cfg["taker_fee"],
                        on_event=self.on_event)
        th = threading.Thread(target=sim.run, kwargs={"poll": self.cfg["poll_seconds"]},
                              daemon=True, name=f"sim-{symbol}")
        th.start()
        self.sims[symbol] = (sim, th)

    def stop_sim(self, symbol):
        sim, _ = self.sims.pop(symbol)
        sim.stop = True

    # ---------- вотчлист ----------

    def refresh_watchlist(self):
        sp = ScreenParams(**self.cfg["screener"])
        try:
            results = screen(sp, client=self.client)
        except Exception as e:
            self.tg.send(f"⚠️ Скринер упал: {e}")
            return
        by_symbol = {r["symbol"]: r for r in results}
        self.scores = {s: r["score"] for s, r in by_symbol.items()}

        # выбрасываем испортившиеся
        for sym in list(self.sims):
            r = by_symbol.get(sym)
            if r is None or r["score"] < self.cfg["drop_score"]:
                why = "пропал из скрина" if r is None else f"score упал до {r['score']:.2f}"
                self.stop_sim(sym)
                self.tg.send(f"📋 − {sym}: убран из вотчлиста ({why})")

        # добавляем лучших из свежего скрина
        for r in results:
            if len(self.sims) >= self.cfg["watchlist_size"]:
                break
            sym = r["symbol"]
            if sym in self.sims or r["score"] < self.cfg["add_score"]:
                continue
            self.start_sim(sym)
            self.tg.send(f"📋 + {sym}: в вотчлист (score {r['score']:.2f}, "
                         f"спред {r['spread_pct']:.2f}%, клип ${r['median_clip_usdt']:.2f}, "
                         f"объём 24ч ${r['quote_vol_24h']:.0f})")
        self._save_state()

    def hourly_summary(self):
        lines = [f"⏱ Сводка ersh | 💰 баланс ${self.total['balance']:.2f}, "
                 f"всего сделок {self.total['trades']}, PnL {self.total['pnl']:+.4f} USDT"]
        for sym, (sim, _) in sorted(self.sims.items()):
            s = sim.stats
            wr = s["wins"] / s["trades"] * 100 if s["trades"] else 0
            lines.append(f"  {sym}: сделок {s['trades']}, winrate {wr:.0f}%, "
                         f"PnL {s['pnl']:+.4f}, состояние {sim.state}")
        self.tg.send("\n".join(lines))

    # ---------- главный цикл ----------

    def run(self):
        self.tg.send("🚀 ersh запущен: скрин рынка + симуляция + сигналы")
        next_refresh = 0.0
        next_summary = time.time() + self.cfg["summary_minutes"] * 60
        while True:
            now = time.time()
            if now >= next_refresh:
                self.refresh_watchlist()
                next_refresh = now + self.cfg["refresh_minutes"] * 60
            if now >= next_summary:
                self.hourly_summary()
                self._save_state()
                next_summary = now + self.cfg["summary_minutes"] * 60
            time.sleep(5)


def main():
    ap = argparse.ArgumentParser(description="Оркестратор ersh")
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args()
    Orchestrator(load_config(args.config)).run()


if __name__ == "__main__":
    main()
