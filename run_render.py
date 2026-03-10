#!/usr/bin/env python3
"""Запуск бота как Web Service для Render.com (бесплатный тариф).

Render даёт бесплатно Web Service (не Worker). Этот скрипт поднимает
минимальный HTTP-сервер для health check и запускает бота в фоне.
Без входящих запросов Render засыпает через 15 мин — подключите
UptimeRobot (бесплатно) для пинга каждые 5 мин.
"""
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# Порт задаёт Render через переменную PORT
PORT = int(os.environ.get("PORT", 10000))


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass  # не засорять лог


def run_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    server.serve_forever()


def run_bot():
    from bot_standalone import run_bot, main
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN не задан")
        return
    run_bot(token)


if __name__ == "__main__":
    # Бот в отдельном потоке
    t = threading.Thread(target=run_bot, daemon=True)
    t.start()
    # HTTP-сервер в главном потоке (Render проверяет порт)
    run_server()
