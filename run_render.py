#!/usr/bin/env python3
"""Запуск бота как Web Service для Render.com (бесплатный тариф).

Render даёт бесплатно Web Service (не Worker). Этот скрипт поднимает
минимальный HTTP-сервер для health check и запускает бота в фоне.
Без входящих запросов Render засыпает через 15 мин — подключите
UptimeRobot (бесплатно) для пинга каждые 5 мин.
"""
import os
import threading

# PORT обязателен для Render: https://render.com/docs/web-services#port-binding
PORT = int(os.environ.get("PORT", 10000))


def run_bot():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN не задан", flush=True)
        return
    try:
        from receipt_db import build_and_save
        try:
            build_and_save()
            print("receipt_index: build OK", flush=True)
        except Exception as e:
            print(f"receipt_index build: {e}", flush=True)
        from bot_standalone import run_bot as _run_bot
        _run_bot(token)
    except Exception as e:
        print(f"Bot error: {e}", flush=True)


if __name__ == "__main__":
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    print(f"Listening on 0.0.0.0:{PORT}", flush=True)

    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    server.serve_forever()
