# Деплой бота на бесплатный сервер

Бот использует **long polling** (getUpdates), работает на чистом Python (почти без внешних зависимостей). Подходят любые хостинги с Python 3.10+.

## Переменные окружения

| Переменная | Описание |
|------------|----------|
| `TELEGRAM_BOT_TOKEN` | Токен от @BotFather (обязательно) |
| `ALLOWED_USER_IDS` | ID через запятую (опционально, если пусто — доступ всем) |
| `HTTPS_PROXY` | Прокси (если нужен) |

---

## Вариант 1: Render.com (Web Service — бесплатно)

Background Workers на Render платные. Но **Web Service бесплатен** (750 ч/мес). Бот запускается через `run_render.py` — он поднимает HTTP-сервер (для health check) и бота в фоне.

1. [render.com](https://render.com) → New → **Web Service**
2. Подключите `https://github.com/sashazh39-bit/cheketop2131`
3. **Build Command:** `pip install -r requirements-bot.txt || true`
4. **Start Command:** `python3 run_render.py`
5. В **Environment** добавьте `TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS`
6. Deploy

**Важно:** бесплатный сервис засыпает через 15 мин без запросов. Чтобы бот работал 24/7, подключите [UptimeRobot](https://uptimerobot.com) (бесплатно): создайте монитор HTTP для вашего URL Render (например `https://cheketop-xxx.onrender.com`) с интервалом 5 минут — так Render не будет засыпать.

---

## Вариант 2: Bothost (рекомендуется для РФ)

[Bothost.ru](https://www.bothost.ru) — хостинг Telegram-ботов. Бесплатно: 1 бот 24/7, 256 MB RAM.

1. Регистрация на bothost.ru
2. Создайте бота, подключите репозиторий GitHub
3. Укажите: `python3 bot_standalone.py` или используйте их интерфейс
4. Добавьте переменные окружения

---

## Вариант 3: Railway

1. [railway.app](https://railway.app) → Start a New Project
2. Deploy from GitHub или загрузите папку
3. Railway определит Python по `Procfile`
4. **Variables** → добавьте `TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS`
5. Deploy

**Бесплатно:** $5 кредит в месяц (обычно хватает на небольшой бот).

---

## Вариант 4: PythonAnywhere

1. [pythonanywhere.com](https://www.pythonanywhere.com) → Free account
2. Files → загрузите все `.py` и папку проекта
3. **Tasks** → Add a new task:
   - Command: `python3 /home/ВАШ_USERNAME/чекетоп/bot_standalone.py`
   - Interval: every 5 minutes (или Always-on на платном)
4. **Consoles** → Bash → задайте переменные:
   ```bash
   export TELEGRAM_BOT_TOKEN="ваш_токен"
   export ALLOWED_USER_IDS="1445265832,7076663447"
   ```

**Важно:** Free tier ограничивает CPU (~100 сек/день). Для работы 24/7 лучше Always-on ($5/мес) или другой хостинг.

---

## Вариант 5: Koyeb

1. [koyeb.com](https://www.koyeb.com) → Create App
2. Docker или Git deploy
3. Для Git: укажите репозиторий, **Run command:** `pip install -r requirements-bot.txt 2>/dev/null; python3 bot_standalone.py`
4. Secrets → добавьте `TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS`

**Бесплатно:** 2 сервиса на free tier.

---

## Вариант 6: Oracle Cloud (всегда бесплатно)

1. [cloud.oracle.com](https://www.oracle.com/cloud/free/) → Free tier VM
2. Создайте Ubuntu VM
3. Подключитесь по SSH и установите:
   ```bash
   sudo apt update && sudo apt install -y python3 python3-pip
   cd /home/ubuntu && git clone ВАШ_РЕПО .
   pip3 install PyMuPDF  # опционально
   export TELEGRAM_BOT_TOKEN="токен"
   export ALLOWED_USER_IDS="1445265832,7076663447"
   nohup python3 bot_standalone.py > bot.log 2>&1 &
   ```
4. Для автозапуска после перезагрузки добавьте systemd service или cron `@reboot`.

---

## Какие файлы нужны для бота

Минимальный набор:
```
bot_standalone.py
pdf_patcher.py
vtb_patch_from_config.py
vtb_cmap.py
vtb_sber_reference.py
vtb_test_generator.py
vtb_config.json     # если есть
requirements-bot.txt
```

Файл `cid_patch_amount.py` нужен для режима Альфа-Банк (fallback).

---

## Проверка после деплоя

1. Отправьте боту `/start`
2. Если бот не отвечает — проверьте логи на хостинге (часто ошибка в токене или переменных)
3. Для ограничения доступа обязательно задайте `ALLOWED_USER_IDS`
