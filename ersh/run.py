"""Оркестратор: автообновляемый вотчлист по нескольким биржам + симуляторы + Telegram.

Цикл:
  1. Раз в refresh_minutes прогоняет скринер по всем парам каждой биржи.
  2. Обновляет вотчлист с гистерезисом: добавляет тикеры со score >= add_score,
     убирает со score < drop_score (или пропавшие). Изменения — в лог сервиса.
  3. На каждую пару (биржа, тикер) крутится свой симулятор; в Telegram идут
     только 🟢 входы, 🔴 выходы (PnL, время в позиции, баланс биржи) и сводки.
  4. У каждой биржи свой виртуальный баланс (start_balance) — это счётчик
     прогресса, на размер позиций он не влияет.

Состояние (вотчлист, балансы) переживает рестарт в state/state.json.

CLI:  python3 -m ersh.run [--config config.json]
"""

import argparse
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .exchanges import DEFAULT_FEES, NAMES, make_client
from .screener import ScreenParams, screen
from .sim import Simulator
from .tg import Notifier

DEFAULTS = {
    "telegram_token": None,
    "telegram_chat_id": None,
    "exchanges": ["mexc", "bingx", "gate"],
    "fees": DEFAULT_FEES,
    "start_balance": 100.0,
    "watchlist_size": None,      # null = без ограничения (всё, что прошло add_score)
    "refresh_minutes": 60,
    "add_score": 0.65,
    "drop_score": 0.50,
    "max_competition": 0.4,
    "order_usdt": None,          # null = авто (80% клипа бота)
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
            if k in ("screener", "fees") and isinstance(v, dict):
                cfg[k] = {**DEFAULTS[k], **v}
            else:
                cfg[k] = v
    return cfg


class Orchestrator:
    def __init__(self, cfg):
        self.cfg = cfg
        self.exchanges = [e.lower() for e in cfg["exchanges"]]
        self.clients = {ex: make_client(ex) for ex in self.exchanges}
        self.tg = Notifier(cfg["telegram_token"], cfg["telegram_chat_id"])
        self.sims = {}      # (exchange, symbol) -> (Simulator, Thread)
        self.scores = {ex: {} for ex in self.exchanges}
        self.balances = {ex: {"trades": 0, "pnl": 0.0, "balance": cfg["start_balance"]}
                         for ex in self.exchanges}
        self.state_path = os.path.join(cfg["state_dir"], "state.json")
        os.makedirs(cfg["state_dir"], exist_ok=True)
        self._load_state()

    # ---------- состояние ----------

    def _load_state(self):
        if not os.path.exists(self.state_path):
            return
        with open(self.state_path) as f:
            st = json.load(f)
        scores = st.get("scores", {})
        balances = st.get("balances", {})
        # старый однобиржевой формат: плоские scores и общий total -> это MEXC
        if scores and not any(k in NAMES for k in scores):
            scores = {"mexc": scores}
        if not balances and "total" in st:
            balances = {"mexc": st["total"]}
        for ex, v in scores.items():
            if ex in self.scores:
                self.scores[ex] = v
        for ex, v in balances.items():
            if ex in self.balances:
                self.balances[ex].update(v)

    def _save_state(self):
        with open(self.state_path, "w") as f:
            json.dump({"scores": self.scores,
                       "watchlist": sorted(f"{ex}:{sym}" for ex, sym in self.sims),
                       "balances": self.balances}, f, indent=1)

    # ---------- симуляторы ----------

    def _log(self, text):
        print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)

    def on_event(self, ex, e):
        kind = e["kind"]
        exname = NAMES[ex]
        if kind == "exit":
            b = self.balances[ex]
            b["trades"] += 1
            b["pnl"] += e.get("pnl", 0.0)
            b["balance"] += e.get("pnl", 0.0)
            self._save_state()
            self.tg.send(f"[{exname}] {e['symbol']}: {e['text']}\n"
                         f"💰 Баланс {exname}: ${b['balance']:.2f} "
                         f"(старт ${self.cfg['start_balance']:.0f}, "
                         f"PnL {b['pnl']:+.4f}, сделок {b['trades']})")
        elif kind == "entry":
            self.tg.send(f"[{exname}] {e['symbol']}: {e['text']}")
        else:
            self._log(f"[{exname}] {e['symbol']} {e['text']}")

    def start_sim(self, ex, symbol):
        fees = self.cfg["fees"].get(ex, {})
        sim = Simulator(symbol, client=make_client(ex), order_usdt=self.cfg["order_usdt"],
                        maker_fee=fees.get("maker", 0.0), taker_fee=fees.get("taker", 0.001),
                        on_event=lambda e, ex=ex: self.on_event(ex, e))
        th = threading.Thread(target=sim.run, kwargs={"poll": self.cfg["poll_seconds"]},
                              daemon=True, name=f"sim-{ex}-{symbol}")
        th.start()
        self.sims[(ex, symbol)] = (sim, th)

    def stop_sim(self, key):
        sim, _ = self.sims.pop(key)
        sim.stop = True

    # ---------- вотчлист ----------

    def refresh_watchlist(self):
        sp = ScreenParams(**self.cfg["screener"])
        with ThreadPoolExecutor(max_workers=len(self.exchanges)) as pool:
            futures = {ex: pool.submit(screen, sp, self.clients[ex]) for ex in self.exchanges}
        for ex, fut in futures.items():
            exname = NAMES[ex]
            try:
                results = fut.result()
            except Exception as e:
                self._log(f"⚠️ [{exname}] скринер упал: {e}")
                continue
            by_symbol = {r["symbol"]: r for r in results}
            self.scores[ex] = {s: r["score"] for s, r in by_symbol.items()}

            # выбрасываем испортившиеся
            for key in [k for k in self.sims if k[0] == ex]:
                r = by_symbol.get(key[1])
                if r is None or r["score"] < self.cfg["drop_score"]:
                    why = "пропал из скрина" if r is None else f"score упал до {r['score']:.2f}"
                    self.stop_sim(key)
                    self._log(f"📋 [{exname}] − {key[1]}: убран из вотчлиста ({why})")

            # добавляем лучших из свежего скрина
            limit = self.cfg["watchlist_size"]
            for r in results:
                if limit is not None and sum(1 for k in self.sims if k[0] == ex) >= limit:
                    break
                sym = r["symbol"]
                if (ex, sym) in self.sims or r["score"] < self.cfg["add_score"]:
                    continue
                self.start_sim(ex, sym)
                self._log(f"📋 [{exname}] + {sym}: в вотчлист (score {r['score']:.2f}, "
                          f"спред {r['spread_pct']:.2f}%, клип ${r['median_clip_usdt']:.2f}, "
                          f"объём 24ч ${r['quote_vol_24h']:.0f})")
        self._save_state()

    def hourly_summary(self):
        lines = ["⏱ Сводка ersh"]
        for ex in self.exchanges:
            b = self.balances[ex]
            n = sum(1 for k in self.sims if k[0] == ex)
            lines.append(f"💰 {NAMES[ex]}: баланс ${b['balance']:.2f}, "
                         f"PnL {b['pnl']:+.4f}, сделок {b['trades']}, тикеров {n}")
        for (ex, sym), (sim, _) in sorted(self.sims.items()):
            s = sim.stats
            if not s["trades"] and sim.state == "FLAT":
                continue
            wr = s["wins"] / s["trades"] * 100 if s["trades"] else 0
            lines.append(f"  [{NAMES[ex]}] {sym}: сделок {s['trades']}, winrate {wr:.0f}%, "
                         f"PnL {s['pnl']:+.4f}, состояние {sim.state}")
        self.tg.send("\n".join(lines))

    # ---------- главный цикл ----------

    def run(self):
        self.tg.send(f"🚀 ersh запущен: {', '.join(NAMES[ex] for ex in self.exchanges)} — "
                     f"скрин рынка + симуляция + сигналы")
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
