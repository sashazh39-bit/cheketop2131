#!/usr/bin/env python3
"""Запуск бота как Web Service для Render.com (бесплатный тариф).

Render даёт бесплатно Web Service (не Worker). Этот скрипт поднимает
минимальный HTTP-сервер для health check и запускает бота в фоне.
Без входящих запросов Render засыпает через 15 мин — подключите
UptimeRobot (бесплатно) для пинга каждые 5 мин.
"""
import json
import os
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

# PORT обязателен для Render: https://render.com/docs/web-services#port-binding
PORT = int(os.environ.get("PORT", 10000))
WATCHDOG_TIMEOUT_SEC = int(os.environ.get("WATCHDOG_TIMEOUT_SEC", "180"))
WATCHDOG_CHECK_INTERVAL_SEC = int(os.environ.get("WATCHDOG_CHECK_INTERVAL_SEC", "10"))

STATE_LOCK = threading.Lock()
STATE = {
    "started_at": time.time(),
    "last_bot_heartbeat": 0.0,
    "last_bot_error": "",
    "restart_count": 0,
    "bot_alive": False,
}


def _set_state(**kwargs) -> None:
    with STATE_LOCK:
        STATE.update(kwargs)


def _get_state() -> dict:
    with STATE_LOCK:
        return dict(STATE)


def run_bot():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN не задан", flush=True)
        _set_state(last_bot_error="TELEGRAM_BOT_TOKEN не задан", bot_alive=False)
        return
    try:
        from receipt_db import build_and_save
        try:
            build_and_save()
            print("receipt_index: build OK", flush=True)
        except Exception as e:
            print(f"receipt_index build: {e}", flush=True)
        from bot_standalone import run_bot as _run_bot
    except Exception as e:
        print(f"Bot init error: {e}", flush=True)
        _set_state(last_bot_error=f"Bot init error: {e}", bot_alive=False)
        return

    _set_state(last_bot_heartbeat=time.time(), bot_alive=True)
    while True:
        try:
            _set_state(last_bot_heartbeat=time.time(), bot_alive=True)
            _run_bot(token)
            # Если основной цикл внезапно вернулся — считаем это аварией.
            raise RuntimeError("bot_standalone.run_bot вернулся без исключения")
        except Exception as e:
            _set_state(
                last_bot_error=str(e)[:500],
                restart_count=_get_state()["restart_count"] + 1,
                last_bot_heartbeat=time.time(),
                bot_alive=True,
            )
            print(f"Bot crashed: {e}. Перезапуск через 5 сек...", flush=True)
            time.sleep(5)


def watchdog_loop(bot_thread: threading.Thread) -> None:
    while True:
        time.sleep(max(1, WATCHDOG_CHECK_INTERVAL_SEC))
        st = _get_state()
        now = time.time()
        hb = st.get("last_bot_heartbeat", 0.0)
        hb_age = now - hb if hb else 10**9

        if not bot_thread.is_alive():
            print("WATCHDOG: bot thread is not alive, exit(1) for Render restart", flush=True)
            os._exit(1)

        if hb_age > WATCHDOG_TIMEOUT_SEC:
            print(
                f"WATCHDOG: heartbeat stale ({int(hb_age)}s > {WATCHDOG_TIMEOUT_SEC}s), exit(1) for Render restart",
                flush=True,
            )
            os._exit(1)


class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            st = _get_state()
            now = time.time()
            hb = st.get("last_bot_heartbeat", 0.0)
            hb_age = now - hb if hb else None
            healthy = bool(st.get("bot_alive")) and hb_age is not None and hb_age <= WATCHDOG_TIMEOUT_SEC

            if self.path in ("/", "/healthz"):
                status = 200 if healthy else 503
                payload = {
                    "status": "ok" if healthy else "degraded",
                    "uptime_sec": int(now - st["started_at"]),
                    "bot_alive": st["bot_alive"],
                    "heartbeat_age_sec": None if hb_age is None else int(hb_age),
                    "restart_count": st["restart_count"],
                    "last_bot_error": st["last_bot_error"],
                }
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if self.path == "/metrics":
                metrics = (
                    f"bot_uptime_seconds {int(now - st['started_at'])}\n"
                    f"bot_restart_count {int(st['restart_count'])}\n"
                    f"bot_alive {1 if st['bot_alive'] else 0}\n"
                    f"bot_heartbeat_age_seconds {int(hb_age) if hb_age is not None else -1}\n"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(metrics)))
                self.end_headers()
                self.wfile.write(metrics)
                return

            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Not Found")

        def log_message(self, format, *args):
            pass


if __name__ == "__main__":
    class ReusableHTTPServer(HTTPServer):
        allow_reuse_address = True

    server = ReusableHTTPServer(("0.0.0.0", PORT), HealthHandler)
    print(f"Listening on 0.0.0.0:{PORT}", flush=True)

    bot_thread = threading.Thread(target=run_bot, daemon=True, name="bot-thread")
    bot_thread.start()

    watchdog_thread = threading.Thread(target=watchdog_loop, args=(bot_thread,), daemon=True, name="watchdog-thread")
    watchdog_thread.start()

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
