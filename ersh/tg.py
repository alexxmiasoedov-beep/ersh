"""Отправка сигналов в Telegram-группу через Bot API.

Без токена работает в режиме консоли (для отладки).
Токен и chat_id берутся из config.json или переменных окружения
TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.
"""

import os
import queue
import threading
import time

import requests


class Notifier:
    def __init__(self, token=None, chat_id=None):
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
        self.q = queue.Queue()
        self.enabled = bool(self.token and self.chat_id)
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def send(self, text):
        """Поставить сообщение в очередь (не блокирует торговый цикл)."""
        print(f"[TG{'√' if self.enabled else '·console'}] {text}", flush=True)
        if self.enabled:
            self.q.put(text)

    def _loop(self):
        while True:
            text = self.q.get()
            for attempt in range(3):
                try:
                    r = requests.post(
                        f"https://api.telegram.org/bot{self.token}/sendMessage",
                        json={"chat_id": self.chat_id, "text": text,
                              "disable_web_page_preview": True},
                        timeout=15,
                    )
                    if r.status_code == 429:
                        time.sleep(int(r.json().get("parameters", {}).get("retry_after", 5)))
                        continue
                    r.raise_for_status()
                    break
                except requests.RequestException:
                    time.sleep(2 * (attempt + 1))
            time.sleep(0.5)  # мягкий лимит на частоту
