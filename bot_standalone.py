#!/usr/bin/env python3
"""Telegram-бот без внешних зависимостей (только stdlib).
Отправьте чек → выберите банк → введите "с какой на какую" → получите PDF.
"""
# Обход: прокси и SSL (корпоративные сети / Python на macOS)
import os as _os
import ssl
import urllib.request as _ur
from urllib.parse import urlparse
_ssl_ctx = ssl._create_unverified_context()
_proxy_url = _os.environ.get("HTTPS_PROXY") or _os.environ.get("https_proxy") or _os.environ.get("HTTP_PROXY") or _os.environ.get("http_proxy")
_handlers = [_ur.HTTPSHandler(context=_ssl_ctx)]
if _proxy_url:
    _proxy = {"http": _proxy_url, "https": _proxy_url}
    _handlers.insert(0, _ur.ProxyHandler(_proxy))
    # Прокси с авторизацией user:pass@host:port
    try:
        p = urlparse(_proxy_url)
        if p.username and p.password:
            _pm = _ur.HTTPPasswordMgrWithDefaultRealm()
            _pm.add_password(None, f"{p.scheme}://{p.hostname}:{p.port or (443 if p.scheme == 'https' else 80)}", p.username, p.password)
            _handlers.insert(1, _ur.ProxyBasicAuthHandler(_pm))
    except Exception:
        pass
_ur.install_opener(_ur.build_opener(*_handlers))

import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

_BOT_DIR = Path(__file__).parent
_ADD_GLYPHS_SCRIPT = _BOT_DIR / "add_glyphs_to_13_03.py"

# Загрузка .env вручную (без python-dotenv)
try:
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
except Exception:
    pass

from pdf_patcher import patch_pdf_file, patch_amount as pdf_patch_amount, format_amount_display
from vtb_patch_from_config import patch_from_values, patch_amount_only
from vtb_cmap import get_unsupported_chars, format_unsupported_error, suggest_replacement, FALLBACK_TIPS
from vtb_sber_reference import scan_vtb_unsupported_chars
from receipt_db import (
    receipt_supports_chars,
    get_missing_chars_in_receipt,
    chars_from_text_fields,
    find_donor,
    get_bank_report,
    get_bank_counts,
    load_index,
    add_receipt_to_index,
    build_and_save,
    get_operation_id_from_pdf,
    COMMON_AMOUNTS,
    VTB_SUBTYPES,
)

BASE = "https://api.telegram.org/bot"

VTB_UNSUPPORTED_NOTICE = (
    "⚠️ Нельзя использовать: ё, Ё, неразрывный дефис (‑). "
    "Замените на: е, Е, обычный дефис (-)."
)
USER_STATE: dict[int, dict] = {}

# Разрешённые user_id (через запятую в .env: ALLOWED_USER_IDS=123456,789012)
_ALLOWED_IDS: set[int] = set()
_raw = os.environ.get("ALLOWED_USER_IDS", "").strip()
if _raw:
    for s in _raw.replace(" ", "").split(","):
        if s.isdigit():
            _ALLOWED_IDS.add(int(s))

ACCESS_DENIED_MSG = "🚫 Доступ запрещён. Бот доступен только ограниченному кругу пользователей."

MAIN_MENU_TEXT = (
    "👋 Главная\n\n"
    "📄 Загрузить чек — отправьте PDF и измените сумму/ФИО.\n"
    "✨ Сгенерировать — чек из базы без загрузки.\n"
    "📋 Создать выписку — редактирование или по чеку.\n"
    "📂 Проверка базы — просмотр и добавление чеков-доноров.\n"
    "📋 Заявки — просмотр и смена статусов."
)
MAIN_MENU_KB = [
    [{"text": "📄 Загрузить чек", "callback_data": "main_new"}],
    [{"text": "✨ Сгенерировать", "callback_data": "main_generate"}],
    [{"text": "📋 Создать выписку", "callback_data": "main_stmt"}],
    [{"text": "📂 Проверка базы", "callback_data": "main_db"}],
    [{"text": "📋 Заявки", "callback_data": "main_zayavki"}],
    [{"text": "📝 Последние изменения", "callback_data": "main_changelog"}],
]

CHANGELOG_TEXT = (
    "📝 Последние изменения\n\n"
    "• Система заявок — после создания чека можно оформить заявку. "
    "В разделе «Заявки» — список, нажмите на заявку и выберите статус: В работе или Оплачено.\n\n"
    "• Полная замена ВТБ — режим «Все поля»: замена суммы, даты, ФИО плательщика/получателя, телефона и банка. "
    "Замена ФИО и банка пока может работать некорректно.\n\n"
    "• Автозамена ё→е, ‑→- — при вводе ФИО/телефона/банка символы заменяются автоматически, если structurally всё ок.\n\n"
    "• Проверка базы — кнопка «Обновить индекс» для пересборки после ручного добавления чеков."
)

ZAYAVKI_DIR = Path(__file__).parent / "заявки"
STATUS_LABELS = {"новый": "🆕 Новый", "в работе": "🔄 В работе", "оплачено": "💰 Оплачено"}
STATUS_SHORT = {"n": "новый", "w": "в работе", "o": "оплачено"}
STATUS_TO_SHORT = {"новый": "n", "в работе": "w", "оплачено": "o"}


def save_zayavka(uid: int, username: str, amount_from: int, amount_to: int, bank: str, pdf_name: str, description: str) -> Path | None:
    """Сохранить заявку в JSON (файл по дате). Возвращает путь к файлу или None."""
    try:
        ZAYAVKI_DIR.mkdir(parents=True, exist_ok=True)
        day = datetime.now().strftime("%Y-%m-%d")
        filepath = ZAYAVKI_DIR / f"{day}.json"
        data = []
        if filepath.exists():
            try:
                data = json.loads(filepath.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = []
        entry = {
            "id": f"{int(time.time() * 1000)}_{uid}",
            "user_id": uid,
            "username": username or "",
            "timestamp": datetime.now().isoformat(),
            "amount_from": amount_from,
            "amount_to": amount_to,
            "bank": bank,
            "pdf_name": pdf_name,
            "описание": description,
            "статус": "новый",
        }
        data.append(entry)
        filepath.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return filepath
    except Exception as e:
        print(f"  ⚠️ Ошибка сохранения заявки: {e}")
        return None


def load_all_zayavki() -> list[dict]:
    """Загрузить все заявки из JSON (все файлы заявки/*.json), сортировка по дате (новые сверху)."""
    result = []
    if not ZAYAVKI_DIR.exists():
        return result
    for fp in sorted(ZAYAVKI_DIR.glob("*.json"), reverse=True):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            for e in data:
                e.setdefault("статус", "новый")
                result.append(e)
        except (json.JSONDecodeError, OSError):
            continue
    result.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return result


def update_zayavka_status(zayavka_id: str, new_status: str) -> bool:
    """Обновить статус заявки по id. Вернуть True если успешно."""
    if not ZAYAVKI_DIR.exists():
        return False
    for fp in ZAYAVKI_DIR.glob("*.json"):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            for i, e in enumerate(data):
                if e.get("id") == zayavka_id:
                    data[i]["статус"] = new_status
                    fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                    return True
        except (json.JSONDecodeError, OSError):
            continue
    return False


def get_zayavka_by_id(zayavka_id: str) -> dict | None:
    """Найти заявку по id."""
    for z in load_all_zayavki():
        if z.get("id") == zayavka_id:
            return z
    return None


def build_zayavki_list(limit: int = 15) -> tuple[str, str]:
    """Список заявок: каждая — кнопка для открытия."""
    try:
        items = load_all_zayavki()
    except Exception as e:
        print(f"  ⚠️ Ошибка загрузки заявок: {e}")
        return "📋 Заявки:\n\nОшибка загрузки.", json.dumps({"inline_keyboard": []})
    if not items:
        return "📋 Заявки:\n\nНет заявок. Нажмите на заявку для просмотра.", json.dumps({"inline_keyboard": []})
    lines = [f"📋 Заявки (всего {len(items)})\n\nНажмите на заявку:"]
    keyboard = []
    for i, z in enumerate(items[:limit]):
        try:
            st = z.get("статус", "новый")
            am_from = int(z.get("amount_from") or 0)
            am_to = int(z.get("amount_to") or 0)
            ts = (z.get("timestamp") or "")[:10]
            lbl = STATUS_LABELS.get(st, st)
            btn_text = f"{i+1}. {format_amount_display(am_from)}→{format_amount_display(am_to)} ₽ · {ts} {lbl}"[:64]
            zid = z.get("id") or ""
            if not zid:
                continue
            cb = f"v_{zid}"[:64]
            keyboard.append([{"text": btn_text, "callback_data": cb}])
        except Exception as e:
            print(f"  ⚠️ Ошибка заявки {i}: {e}")
            continue
    return "\n".join(lines), json.dumps({"inline_keyboard": keyboard})


def build_zayavka_detail(zayavka_id: str) -> tuple[str, str] | None:
    """Карточка заявки с кнопками [В работе] [Оплачено] [Назад]."""
    z = get_zayavka_by_id(zayavka_id)
    if not z:
        return None
    st = z.get("статус", "новый")
    am_from = int(z.get("amount_from") or 0)
    am_to = int(z.get("amount_to") or 0)
    ts = (z.get("timestamp") or "")[:16].replace("T", " ")
    desc = z.get("описание") or "(без описания)"
    bank = z.get("bank", "")
    bank_name = {"alfa": "Альфа", "vtb": "ВТБ", "auto": "Авто"}.get(bank, bank)
    lbl = STATUS_LABELS.get(st, st)
    txt = (
        f"📋 Заявка\n\n"
        f"💰 {format_amount_display(am_from)} → {format_amount_display(am_to)} ₽\n"
        f"🏦 {bank_name} · {ts}\n"
        f"📌 {lbl}\n\n"
        f"📝 {desc}"
    )
    cb_w = f"st_{zayavka_id}_w"[:64]
    cb_o = f"st_{zayavka_id}_o"[:64]
    kb = {
        "inline_keyboard": [
            [
                {"text": "🔄 В работе", "callback_data": cb_w},
                {"text": "💰 Оплачено", "callback_data": cb_o},
            ],
            [{"text": "⬅️ К списку", "callback_data": "main_zayavki"}],
        ]
    }
    return txt, json.dumps(kb)


def tg_request(token: str, method: str, data: dict | None = None, files: dict | None = None) -> dict:
    url = f"{BASE}{token}/{method}"
    max_retries = 3
    last_err = None
    for attempt in range(max_retries):
        try:
            return _tg_request_once(url, data, files)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_err = e
            if attempt < max_retries - 1:
                delay = (attempt + 1) * 3
                print(f"  ⚠️ Сеть: {e}. Повтор через {delay} сек...")
                time.sleep(delay)
            else:
                raise
    raise last_err


def _tg_request_once(url: str, data: dict | None, files: dict | None) -> dict:
    if files:
        boundary = "----WebKitFormBoundary" + os.urandom(16).hex()
        body = []
        if data:
            for k, v in data.items():
                body.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        for k, (fname, fbytes) in files.items():
            body.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fname}\"\r\nContent-Type: application/pdf\r\n\r\n".encode())
            body.append(fbytes)
            body.append(b"\r\n")
        body.append(f"--{boundary}--\r\n".encode())
        full_body = b"".join(body)
        req = urllib.request.Request(url, data=full_body, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    elif data:
        req = urllib.request.Request(url, data=json.dumps(data).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
    else:
        req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=35) as r:
        return json.loads(r.read().decode())


def tg_get_file(token: str, file_path: str) -> bytes:
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            if attempt < 2:
                print(f"  ⚠️ Загрузка файла: {e}. Повтор...")
                time.sleep(3)
            else:
                raise


def tg_get_file_path(token: str, file_id: str) -> str:
    r = tg_request(token, "getFile", {"file_id": file_id})
    if not r.get("ok"):
        raise RuntimeError(r.get("description", "getFile failed"))
    return r["result"]["file_path"]


def parse_amounts(text: str) -> tuple[int, int] | None:
    nums = re.findall(r"\d+", text.strip())
    if len(nums) >= 2:
        return int(nums[0]), int("".join(nums[1:]))
    return None


def parse_amount_pairs(text: str) -> list[tuple[int, int]]:
    """Разбор '10 5000 50 1000' → [(10, 5000), (50, 1000)]."""
    nums = re.findall(r"\d+", text.strip())
    pairs = []
    i = 0
    while i + 1 < len(nums):
        pairs.append((int(nums[i]), int(nums[i + 1])))
        i += 2
    return pairs


def parse_custom_replacement(text: str) -> tuple[str, str] | None:
    """Разбор 'поле=значение' → (поле, значение)."""
    text = text.strip()
    if "=" in text:
        k, _, v = text.partition("=")
        return (k.strip().lower(), v.strip()) if k.strip() and v.strip() else None
    return None


def run_check_caps(payer: str, recipient: str) -> tuple[bool, str]:
    """Проверка ФИО: получатся ли заглавные буквы. Возвращает (ok, output)."""
    if not _ADD_GLYPHS_SCRIPT.exists():
        return False, f"Скрипт не найден: {_ADD_GLYPHS_SCRIPT}"
    try:
        proc = subprocess.run(
            [os.sys.executable, str(_ADD_GLYPHS_SCRIPT), "--check-caps", "--payer", payer or "", "--recipient", recipient or ""],
            cwd=str(_BOT_DIR),
            capture_output=True,
            text=True,
            timeout=30,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        full = out or err or proc.stderr.decode(errors="replace") if proc.stderr else ""
        if proc.returncode != 0 and not full:
            full = f"Код выхода: {proc.returncode}"
        return proc.returncode == 0, full[:4000]  # лимит Telegram
    except subprocess.TimeoutExpired:
        return False, "Таймаут проверки (30 сек)"
    except Exception as e:
        return False, f"Ошибка: {e}"


def run_sbp_generate(
    payer: str,
    recipient: str,
    amount: int,
    date_str: str,
    bank: str = "Т-Банк",
    account: str | None = None,
    operation_id: str | None = None,
) -> tuple[bool, bytes | None, str]:
    """Генерация чека СБП через add_glyphs_to_13_03.py. Возвращает (ok, pdf_bytes, err).
    date_str: "DD.MM.YYYY" или "DD.MM.YYYY, HH:MM" или "now"
    """
    if not _ADD_GLYPHS_SCRIPT.exists():
        return False, None, f"Скрипт не найден: {_ADD_GLYPHS_SCRIPT}"
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False, dir=str(_BOT_DIR)) as tf:
        out_path = tf.name
    try:
        if not date_str or date_str.strip().lower() in ("now", "сейчас"):
            dt = datetime.now()
            date_part = dt.strftime("%d.%m.%Y")
            time_part = dt.strftime("%H:%M")
        else:
            # "13.03.2026, 20:04" или "13.03.2026"
            parts = date_str.strip().split(",")
            date_part = parts[0].strip()
            time_part = parts[1].strip() if len(parts) > 1 else datetime.now().strftime("%H:%M")
        cmd = [
            os.sys.executable, str(_ADD_GLYPHS_SCRIPT),
            "--replace", "--hybrid-safe", "--auto-base",
            "--payer", payer or "Иван Иванович И.",
            "--recipient", recipient or "Иван Иванович И.",
            "--bank", bank or "Т-Банк",
            "--amount", str(amount),
            "--date", date_part,
            "--time", time_part,
            "-o", out_path,
        ]
        if account and re.match(r"^\d{4}$", account):
            cmd.extend(["--account", account])
        if operation_id:
            cmd.extend(["--operation-id", operation_id])
        proc = subprocess.run(cmd, cwd=str(_BOT_DIR), capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()[:500]
            return False, None, err or f"Код выхода: {proc.returncode}"
        if not Path(out_path).exists():
            return False, None, "PDF не создан"
        pdf_bytes = Path(out_path).read_bytes()
        return True, pdf_bytes, ""
    except subprocess.TimeoutExpired:
        return False, None, "Таймаут генерации (60 сек)"
    except Exception as e:
        return False, None, str(e)
    finally:
        try:
            Path(out_path).unlink(missing_ok=True)
        except OSError:
            pass


def _vtb_full_validate_text(text: str, field_name: str) -> tuple[str | None, str | None]:
    """Валидация текста. Вернуть (None, None) если ок; (err_msg, suggested) при ошибке.
    suggested — вариант с заменой ё→е, ‑→- (если применим)."""
    bad = get_unsupported_chars(text)
    if not bad:
        return None, None
    suggested = suggest_replacement(text)
    return format_unsupported_error(bad, field_name), suggested


def _run_vtb_full_patch(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Выполнить патч ВТБ и отправить PDF."""
    inp = state["file_path"]
    amount_from = state.get("vtb_amount_from") or state.get("vtb_amount")
    amount_to = state.get("vtb_amount")

    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})

    required_chars = chars_from_text_fields(
        state.get("vtb_payer") or "",
        state.get("vtb_recipient") or "",
        state.get("vtb_phone") or "",
        state.get("vtb_bank") or "",
    )
    if required_chars and not receipt_supports_chars(inp, required_chars):
        missing = get_missing_chars_in_receipt(inp, required_chars)
        miss_txt = f"Не хватает букв: «{'» «'.join(sorted(missing))}»" if missing else ""
        send(f"❌ Имя недоступно. В вашем чеке отсутствуют нужные буквы.\n{miss_txt}\nПопробуйте «Сгенерировать» — бот найдёт чек с подходящими символами.")
        return

    try:
        data = bytearray(Path(inp).read_bytes())
        # Для ВТБ сначала используем точное выравнивание суммы по wall из patch_amount_only.
        try:
            data = bytearray(patch_amount_only(data, Path(inp), amount_to))
        except Exception:
            ok_sum, err_sum, new_data = pdf_patch_amount(data, amount_from, amount_to, bank="vtb")
            if not ok_sum or new_data is None:
                send(f"❌ Сумма не найдена.\nПроверь: в чеке должна быть сумма {format_amount_display(amount_from)} ₽. {err_sum or ''}")
                return
            data = bytearray(new_data)
        # Остальные поля (дата, ФИО, телефон, банк) — amount=None, сумму уже поменяли
        out_bytes = patch_from_values(
            data,
            Path(inp),
            date_str=state.get("vtb_date", "now"),
            payer=state.get("vtb_payer"),
            recipient=state.get("vtb_recipient"),
            phone=state.get("vtb_phone"),
            bank=state.get("vtb_bank"),
            amount=None,
            account=None,
        )
        out_name = Path(state["file_name"]).stem + f"_{format_amount_display(state['vtb_amount']).replace(' ', '_')}.pdf"
        try:
            os.unlink(inp)
        except OSError:
            pass
        del USER_STATE[uid]
        am_from = state.get("vtb_amount_from") or state.get("vtb_amount")
        caption = f"✅ Готово: {format_amount_display(am_from)} ₽ → {format_amount_display(state['vtb_amount'])} ₽"
        tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": caption}, files={"document": (out_name, out_bytes)})
        USER_STATE[uid] = {
            "awaiting": "report_choice",
            "amount_from": state.get("vtb_amount_from") or state.get("vtb_amount", 0),
            "amount_to": state["vtb_amount"],
            "bank": "vtb",
            "pdf_name": out_name,
        }
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": "📋 Отчёт:",
            "reply_markup": json.dumps({
                "inline_keyboard": [
                    [{"text": "Тест", "callback_data": "report_test"}],
                    [{"text": "Заявка", "callback_data": "report_zayavka"}],
                    [{"text": "🏠 Главное меню", "callback_data": "main_back"}],
                ],
            }),
        })
    except ValueError as e:
        send(f"❌ Ошибка: {e}")
    except Exception as e:
        send(f"❌ Ошибка: {e}\n\nПопробуй снова или отправь чек заново.")


def _vtb_full_send_next(state: dict, aw: str, prompt: str, chat_id: int, token: str, tg_req) -> None:
    """Отправить следующий шаг. Для payer/recipient/phone/bank — с кнопкой «Оставить текущим»."""
    KEEP_PROMPTS = {
        "vtb_payer": ("3️⃣ Плательщик (ФИО):", "vtb_keep_payer"),
        "vtb_recipient": ("4️⃣ Получатель (ФИО):", "vtb_keep_recipient"),
        "vtb_phone": ("5️⃣ Телефон получателя:", "vtb_keep_phone"),
        "vtb_bank": ("6️⃣ Банк получателя:", "vtb_keep_bank"),
    }
    state["awaiting"] = aw
    if aw in KEEP_PROMPTS:
        p, cb = KEEP_PROMPTS[aw]
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": p,
            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "📌 Оставить текущим", "callback_data": cb}]]}),
        })
    else:
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": prompt})


def _gen_send_next(state: dict, aw: str, prompt: str, chat_id: int, token: str, tg_req) -> None:
    """Следующий шаг режима «Сгенерировать»."""
    KEEP_PROMPTS = {
        "gen_payer": ("1️⃣ Плательщик (ФИО):", "gen_keep_payer"),
        "gen_recipient": ("2️⃣ Получатель (ФИО):", "gen_keep_recipient"),
        "gen_phone": ("5️⃣ Телефон получателя:", "gen_keep_phone"),
        "gen_bank": ("6️⃣ Банк получателя:", "gen_keep_bank"),
        "gen_account": ("6️⃣ Номер счёта (4 цифры, напр. 9426):", "gen_keep_account"),
        "gen_operation_id": ("7️⃣ ID операции (B606...):", "gen_keep_opid"),
    }
    state["awaiting"] = aw
    if aw in KEEP_PROMPTS:
        p, cb = KEEP_PROMPTS[aw]
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": p,
            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "📌 Оставить пустым", "callback_data": cb}]]}),
        })
    else:
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": prompt})


def _do_gen_fio_check_and_continue(token: str, uid: int, chat_id: int, state: dict, send, next_step) -> None:
    """Проверить ФИО (--check-caps), показать результат, перейти к настройке суммы."""
    payer = state.get("gen_payer") or ""
    recipient = state.get("gen_recipient") or ""
    ok, output = run_check_caps(payer, recipient)
    txt = "📋 Результат проверки ФИО:\n\n" + (output or "(пусто)")
    send(txt[:4000])
    next_step("gen_amount", "📝 Настройка.\n\n1️⃣ Сумма (например 10000):")


def _gen_next_after_bank(state: dict, next_step) -> None:
    """После ввода банка: для СБП — gen_account, для ВТБ на ВТБ — gen_operation_id."""
    if state.get("gen_vtb_subtype") == "vtb_sbp":
        next_step("gen_account", "6️⃣ Номер счёта (4 цифры, напр. 9426). Оставить пустым — из шаблона:")
    else:
        next_step("gen_operation_id", "7️⃣ ID операции (B606...). Оставить или ввести:")


def _run_sbp_generate(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Сгенерировать чек СБП через add_glyphs_to_13_03.py."""
    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})

    amount = state.get("gen_amount") or 0
    date_str = state.get("gen_date") or "now"
    ok, pdf_bytes, err = run_sbp_generate(
        payer=state.get("gen_payer") or "Иван Иванович И.",
        recipient=state.get("gen_recipient") or "Иван Иванович И.",
        amount=amount,
        date_str=date_str,
        bank=state.get("gen_bank") or "Т-Банк",
        account=state.get("gen_account"),
        operation_id=state.get("gen_operation_id"),
    )
    if not ok:
        send(f"❌ Ошибка генерации: {err}")
        return
    out_name = f"чек_{format_amount_display(amount).replace(' ', '_')}.pdf"
    del USER_STATE[uid]
    tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": f"✅ Сгенерировано: {format_amount_display(amount)} ₽"}, files={"document": (out_name, pdf_bytes)})
    USER_STATE[uid] = {"awaiting": "report_choice", "amount_from": 0, "amount_to": amount, "bank": "vtb", "pdf_name": out_name}
    tg_req(token, "sendMessage", {
        "chat_id": chat_id,
        "text": "📋 Отчёт:",
        "reply_markup": json.dumps({
            "inline_keyboard": [
                [{"text": "Тест", "callback_data": "report_test"}],
                [{"text": "Заявка", "callback_data": "report_zayavka"}],
                [{"text": "🏠 Главное меню", "callback_data": "main_back"}],
            ],
        }),
    })


def _gen_next_after_bank(state: dict, next_step) -> None:
    """После gen_bank: для vtb_sbp → gen_account, иначе → gen_operation_id."""
    if state.get("gen_vtb_subtype") == "vtb_sbp":
        next_step("gen_account", "6️⃣ Номер счёта (4 цифры, напр. 9426). Или «оставить»:")
    else:
        next_step("gen_operation_id", "7️⃣ ID операции (B606...). Оставить из чека или ввести свой:")


def _run_gen_patch(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Сгенерировать чек: найти донора, патч, отправить PDF."""
    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})

    required_chars = chars_from_text_fields(
        state.get("gen_payer") or "",
        state.get("gen_recipient") or "",
        state.get("gen_phone") or "",
        state.get("gen_bank") or "",
    )
    bank_key = state.get("gen_vtb_subtype") or "vtb_sbp"
    donor_path, amount_from = find_donor(required_chars, bank_key)
    if not donor_path:
        missing, scanned, _ = get_bank_report(required_chars, bank_key)
        idx = load_index()
        lines = [
            "❌ Имя недоступно. В базе нет чека с нужными буквами.",
            f"📂 Просмотрено чеков: {scanned}",
            f"🔍 bank_key={bank_key}, в индексе: {list(idx.keys())}",
        ]
        if missing:
            missing_sorted = sorted(missing)
            lines.append(f"❌ Не хватает букв ({len(missing)}): «{'» «'.join(missing_sorted)}»")
        lines.append("Добавьте чеки в «Проверка базы» или замените буквы (ё→е, ‑→-).")
        send("\n".join(lines))
        del USER_STATE[uid]
        return

    amount_to = state["gen_amount"]
    user_from = state.get("gen_amount_from")
    if amount_from is not None:
        amounts_to_try = [amount_from]
    elif user_from is not None:
        amounts_to_try = [user_from] + [a for a in COMMON_AMOUNTS if a != user_from]
    else:
        amounts_to_try = COMMON_AMOUNTS

    try:
        data = bytearray(donor_path.read_bytes())
        try:
            data = bytearray(patch_amount_only(data, donor_path, amount_to))
        except Exception:
            ok_sum, err_sum, new_data = False, None, None
            for am in amounts_to_try:
                ok_sum, err_sum, new_data = pdf_patch_amount(data, am, amount_to, bank="vtb")
                if ok_sum and new_data is not None:
                    break
            if not ok_sum or new_data is None:
                send(f"❌ Сумма не найдена в доноре. {err_sum or ''}")
                del USER_STATE[uid]
                return
            data = bytearray(new_data)
        out_bytes = patch_from_values(
            data,
            donor_path,
            date_str=state.get("gen_date", "now"),
            payer=state.get("gen_payer"),
            recipient=state.get("gen_recipient"),
            phone=state.get("gen_phone"),
            bank=state.get("gen_bank"),
            amount=None,
            operation_id=state.get("gen_operation_id"),
            account=None,
        )
        out_name = donor_path.stem + f"_{format_amount_display(amount_to).replace(' ', '_')}.pdf"
        del USER_STATE[uid]
        caption = f"✅ Сгенерировано: {format_amount_display(amount_to)} ₽"
        tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": caption}, files={"document": (out_name, out_bytes)})
        op_id = state.get("gen_operation_id") or get_operation_id_from_pdf(out_bytes)
        if op_id:
            tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"📋 ID операции (скопируйте при необходимости):\n{op_id}"})
        USER_STATE[uid] = {
            "awaiting": "report_choice",
            "amount_from": amount_from or 0,
            "amount_to": amount_to,
            "bank": "vtb",
            "pdf_name": out_name,
        }
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": "📋 Отчёт:",
            "reply_markup": json.dumps({
                "inline_keyboard": [
                    [{"text": "Тест", "callback_data": "report_test"}],
                    [{"text": "Заявка", "callback_data": "report_zayavka"}],
                    [{"text": "🏠 Главное меню", "callback_data": "main_back"}],
                ],
            }),
        })
    except Exception as e:
        send(f"❌ Ошибка: {e}")
        if uid in USER_STATE and state.get("gen_bank_type"):
            del USER_STATE[uid]


def _do_gen_fio_check_and_continue(token: str, uid: int, chat_id: int, state: dict, send, next_step) -> None:
    """Проверка ФИО (run_check_caps) и переход к настройке (gen_amount)."""
    payer = state.get("gen_payer") or ""
    recipient = state.get("gen_recipient") or ""
    send("⏳ Проверяю буквы...")
    ok, output = run_check_caps(payer, recipient)
    send("📋 Результат проверки ФИО:\n\n" + (output or "(пусто)"))
    next_step("gen_amount", "Настройка.\n\n1️⃣ Сумма (например 10000):")


def _gen_next_after_bank(state: dict, next_step) -> None:
    """После банка: для СБП — gen_account, для ВТБ на ВТБ — gen_operation_id."""
    if state.get("gen_vtb_subtype") == "vtb_sbp":
        next_step("gen_account", "6️⃣ Номер счёта (4 цифры, напр. 9426). Или оставить из шаблона:")
    else:
        next_step("gen_operation_id", "7️⃣ ID операции (B606...). Оставить или ввести:")


def _run_sbp_generate(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Генерация чека СБП через add_glyphs_to_13_03.py."""
    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})
    ok, pdf_bytes, err = run_sbp_generate(
        payer=state.get("gen_payer") or "Иван Иванович И.",
        recipient=state.get("gen_recipient") or "Иван Иванович И.",
        amount=state["gen_amount"],
        date_str=state.get("gen_date", "now"),
        bank=state.get("gen_bank") or "Т-Банк",
        account=state.get("gen_account"),
        operation_id=state.get("gen_operation_id"),
    )
    if not ok or not pdf_bytes:
        send(f"❌ Ошибка генерации: {err or 'PDF не создан'}")
        if uid in USER_STATE:
            del USER_STATE[uid]
        return
    out_name = f"чек_{format_amount_display(state['gen_amount']).replace(' ', '_')}.pdf"
    del USER_STATE[uid]
    caption = f"✅ Сгенерировано: {format_amount_display(state['gen_amount'])} ₽"
    tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": caption}, files={"document": (out_name, pdf_bytes)})
    USER_STATE[uid] = {
        "awaiting": "report_choice",
        "amount_from": 0,
        "amount_to": state["gen_amount"],
        "bank": "vtb",
        "pdf_name": out_name,
    }
    tg_req(token, "sendMessage", {
        "chat_id": chat_id,
        "text": "📋 Отчёт:",
        "reply_markup": json.dumps({
            "inline_keyboard": [
                [{"text": "Тест", "callback_data": "report_test"}],
                [{"text": "Заявка", "callback_data": "report_zayavka"}],
                [{"text": "🏠 Главное меню", "callback_data": "main_back"}],
            ],
        }),
    })


def _handle_gen_input(token: str, uid: int, chat_id: int, text: str, msg: dict, tg_req) -> None:
    """Обработка пошагового ввода режима «Сгенерировать»."""
    state = USER_STATE[uid]
    awaiting = state.get("awaiting", "")

    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})

    def next_step(aw: str, prompt: str):
        _gen_send_next(state, aw, prompt, chat_id, token, tg_req)

    if awaiting in ("gen_type", "gen_subtype", "gen_bank") and state.get("gen_bank_type") is None:
        send("Выберите тип перевода и банк кнопками выше.")
        return

    if awaiting == "gen_amount":
        nums = re.findall(r"\d+", text.strip())
        if not nums:
            send("❌ Введите сумму (например: 50000) или две: с какой на какую (10 1000)")
            return
        if len(nums) == 1:
            state["gen_amount"] = int(nums[0])
            state["gen_amount_from"] = None
        else:
            state["gen_amount_from"] = int(nums[0])
            state["gen_amount"] = int("".join(nums[1:]))
        if state["gen_amount"] <= 0:
            send("❌ Сумма должна быть больше 0.")
            return
        next_step("gen_date", "2️⃣ Дата (дд.мм.гггг чч:мм). Enter или «сейчас» — текущая дата")

    elif awaiting == "gen_date":
        text_stripped = text.strip().lower()
        if not text_stripped or text_stripped in ("сейчас", "enter", ""):
            state["gen_date"] = "now"
        else:
            state["gen_date"] = text.strip()
        if state.get("gen_vtb_subtype") == "vtb_sbp":
            next_step("gen_phone", "5️⃣ Телефон получателя:")
        else:
            next_step("gen_payer", "3️⃣ Плательщик (ФИО):")

    elif awaiting == "gen_payer":
        t = text.strip().lower()
        if t in ("пропустить", "-", "=", "оставить пустым"):
            state["gen_payer"] = None
            next_step("gen_recipient", "4️⃣ Получатель (ФИО):")
            return
        t = text.strip()
        err, suggested = _vtb_full_validate_text(t, "Плательщик")
        if err:
            if suggested:
                state["gen_payer"] = suggested
                send(f"✅ Применена замена (ё→е): {suggested}")
                next_step("gen_recipient", "4️⃣ Получатель (ФИО):")
            else:
                send(err)
                return
        else:
            state["gen_payer"] = t
            next_step("gen_recipient", "4️⃣ Получатель (ФИО):")

    elif awaiting == "gen_recipient":
        t = text.strip().lower()
        if t in ("пропустить", "-", "=", "оставить пустым"):
            state["gen_recipient"] = None
            if state.get("gen_vtb_subtype") == "vtb_sbp":
                _do_gen_fio_check_and_continue(token, uid, chat_id, state, send, next_step)
            else:
                next_step("gen_phone", "5️⃣ Телефон получателя:")
            return
        t = text.strip()
        err, suggested = _vtb_full_validate_text(t, "Получатель")
        if err:
            if suggested:
                state["gen_recipient"] = suggested
                send(f"✅ Применена замена (ё→е): {suggested}")
                if state.get("gen_vtb_subtype") == "vtb_sbp":
                    _do_gen_fio_check_and_continue(token, uid, chat_id, state, send, next_step)
                else:
                    next_step("gen_phone", "5️⃣ Телефон получателя:")
            else:
                send(err)
                return
        else:
            state["gen_recipient"] = t
            if state.get("gen_vtb_subtype") == "vtb_sbp":
                _do_gen_fio_check_and_continue(token, uid, chat_id, state, send, next_step)
            else:
                next_step("gen_phone", "5️⃣ Телефон получателя:")

    elif awaiting == "gen_operation_id":
        t = text.strip().replace(" ", "").replace("\n", "")
        if not t or t.lower() in ("пропустить", "-", "=", "оставить", "оставить пустым"):
            state["gen_operation_id"] = None
            send("⏳ Генерирую чек...")
            if state.get("gen_vtb_subtype") == "vtb_sbp":
                _run_sbp_generate(token, uid, chat_id, state, tg_req)
            else:
                _run_gen_patch(token, uid, chat_id, state, tg_req)
            return
        if not re.match(r"^[AB]606[\dA-Fa-f]{15,30}$", t):
            send("❌ Формат: B606... или A606... (цифры и A-F, 20-32 символа)")
            return
        state["gen_operation_id"] = t
        send("⏳ Генерирую чек...")
        if state.get("gen_vtb_subtype") == "vtb_sbp":
            _run_sbp_generate(token, uid, chat_id, state, tg_req)
        else:
            _run_gen_patch(token, uid, chat_id, state, tg_req)

    elif awaiting == "gen_phone":
        t = text.strip().lower()
        if t in ("пропустить", "-", "=", "оставить пустым"):
            state["gen_phone"] = None
            next_step("gen_bank", "6️⃣ Банк получателя:")
            return
        t = text.strip()
        err, suggested = _vtb_full_validate_text(t, "Телефон")
        if err:
            if suggested:
                state["gen_phone"] = suggested
                send(f"✅ Применена замена: {suggested}")
                next_step("gen_bank", "6️⃣ Банк получателя:")
            else:
                send(err)
                return
        else:
            state["gen_phone"] = t
            next_step("gen_bank", "6️⃣ Банк получателя:")

    elif awaiting == "gen_bank":
        t = text.strip().lower()
        if t in ("пропустить", "-", "=", "оставить пустым"):
            state["gen_bank"] = None
            _gen_next_after_bank(state, next_step)
            return
        t = text.strip()
        err, suggested = _vtb_full_validate_text(t, "Банк")
        if err:
            if suggested:
                state["gen_bank"] = suggested
                send(f"✅ Применена замена: {suggested}")
                _gen_next_after_bank(state, next_step)
            else:
                send(err)
                return
        else:
            state["gen_bank"] = t
            _gen_next_after_bank(state, next_step)

    elif awaiting == "gen_account":
        t = text.strip()
        if t.lower() in ("пропустить", "-", "=", "оставить пустым"):
            state["gen_account"] = None
            next_step("gen_operation_id", "7️⃣ ID операции (B606...). Оставить или ввести:")
            return
        if re.match(r"^\d{4}$", t):
            state["gen_account"] = t
            next_step("gen_operation_id", "7️⃣ ID операции (B606...). Оставить или ввести:")
        else:
            send("❌ Номер счёта — 4 цифры (например 9426)")


def _handle_stmt_text(token: str, uid: int, chat_id: int, text: str, tg_req) -> None:
    """Обработка текста в режиме выписки."""
    state = USER_STATE[uid]
    mode = state.get("mode", "")
    step = state.get("step", "")

    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})

    if mode == "statement_edit":
        if step == "amounts":
            pairs = parse_amount_pairs(text)
            if not pairs:
                send("❌ Неверный формат. Введите: 10 10000 или 10 5000 50 1000")
                return
            try:
                from vyписка_service import calculate_balance_and_expenses
            except ImportError:
                send("❌ Модуль выписки недоступен")
                return
            amounts = [(p[0], p[1]) for p in pairs]
            USER_STATE[uid]["replacements"] = {"amounts": amounts}
            USER_STATE[uid]["step"] = "confirm"
            trans = list(state.get("transactions", []))
            amount_map = {int(p[0]): p[1] for p in amounts}
            trans_replaced = [float(amount_map.get(int(t), t)) for t in trans if t > 0]
            balance_end, expenses = calculate_balance_and_expenses(trans_replaced, 55242.65)
            kb = json.dumps({
                "inline_keyboard": [
                    [{"text": "⏭ Пропустить", "callback_data": "stmt_skip"}, {"text": "➡️ Далее", "callback_data": "stmt_next"}],
                    [{"text": "➕ Свои замены", "callback_data": "stmt_custom"}],
                ],
            })
            tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"✅ Замены: {amounts}\n\n📊 Расходы: {expenses:.2f} ₽\n📊 Баланс на конец: {balance_end:.2f} ₽\n\nПропустить / Далее или + свои замены", "reply_markup": kb})
            return
        if step == "custom":
            parsed = parse_custom_replacement(text)
            if not parsed:
                send("❌ Формат: поле=значение (ФИО=..., баланс_начало=..., телефон=...)")
                return
            key, value = parsed
            repl = USER_STATE[uid].setdefault("replacements", {})
            if key in ("fio", "фио"):
                try:
                    from vyписка_service import get_missing_chars
                    fp = Path(USER_STATE[uid]["file_path"])
                    missing = get_missing_chars(fp, value)
                    if missing:
                        kb = json.dumps({"inline_keyboard": [[{"text": "🔄 Повторить", "callback_data": "stmt_retry_fio"}, {"text": "⏭ Без замены ФИО", "callback_data": "stmt_skip_fio"}]]})
                        send(f"⚠️ Недоступные символы: {''.join(missing)}\nПовторите или пропустите.")
                        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "Выберите:", "reply_markup": kb})
                        USER_STATE[uid]["pending_fio"] = value
                        return
                except ImportError:
                    pass
                repl["fio"] = value
            elif key in ("баланс_начало", "balance_start", "balance"):
                try:
                    repl["balance_start"] = float(value.replace(",", "."))
                except ValueError:
                    send("❌ Введите число для баланса.")
                    return
            elif key in ("телефон", "phone"):
                repl["phone"] = value
            elif key in ("номер_заявки", "application_id"):
                repl["application_id"] = value
            send(f"✅ Добавлено: {key}={value}\nВведите ещё или нажмите Далее.")

    elif mode == "statement_from_receipt" and step == "balance":
        try:
            balance_start = float(text.replace(",", ".").replace(" ", ""))
        except ValueError:
            send("❌ Введите число (баланс на начало).")
            return
        try:
            from vyписка_service import BASE_STATEMENT, BASE_AMOUNT, BASE_OLD_FIO, patch_statement, calculate_balance_and_expenses
            amount = USER_STATE[uid]["amount"]
            balance_end, expenses = calculate_balance_and_expenses([float(amount)], balance_start)
            repl = {"amounts": [(BASE_AMOUNT, amount)], "balance_end": balance_end, "expenses": expenses, "fio": USER_STATE[uid].get("generated_fio", "Иванов Иван Иванович"), "old_fio": BASE_OLD_FIO}
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as out_fp:
                out_path = out_fp.name
            ok, err = patch_statement(BASE_STATEMENT, Path(out_path), repl)
            try:
                os.unlink(USER_STATE[uid]["file_path"])
            except OSError:
                pass
            del USER_STATE[uid]
            if not ok:
                send(f"❌ Ошибка: {err}")
                try:
                    os.unlink(out_path)
                except OSError:
                    pass
                return
            with open(out_path, "rb") as f:
                pdf_bytes = f.read()
            try:
                os.unlink(out_path)
            except OSError:
                pass
            tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": f"✅ Выписка готова: {amount} ₽"}, files={"document": (f"выписка_{amount}.pdf", pdf_bytes)})
        except ImportError as e:
            send(f"❌ Модуль выписки недоступен: {e}")
            if uid in USER_STATE:
                del USER_STATE[uid]


def _handle_vtb_full_input(token: str, uid: int, chat_id: int, text: str, msg: dict, tg_req) -> None:
    """Обработка пошагового ввода для ВТБ «Все поля»."""
    state = USER_STATE[uid]
    awaiting = state.get("awaiting", "")
    inp = state["file_path"]

    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})

    def next_step(aw: str, prompt: str):
        _vtb_full_send_next(state, aw, prompt, chat_id, token, tg_req)

    if awaiting == "vtb_amount":
        parsed = parse_amounts(text)
        if not parsed:
            send("❌ Введите две суммы: с какой на какую (например: 10 1000 или 50 50000)")
            return
        amount_from, amount_to = parsed
        if amount_from <= 0 or amount_to <= 0:
            send("❌ Суммы должны быть больше 0.")
            return
        state["vtb_amount_from"] = amount_from
        state["vtb_amount"] = amount_to
        next_step("vtb_date", "2️⃣ Дата (дд.мм.гггг, чч:мм). Enter или «сейчас» — текущая дата")

    elif awaiting == "vtb_date":
        text_stripped = text.strip().lower()
        if not text_stripped or text_stripped in ("сейчас", "enter", ""):
            state["vtb_date"] = "now"
        else:
            state["vtb_date"] = text.strip()
        next_step("vtb_payer", "3️⃣ Плательщик (ФИО):")

    elif awaiting == "vtb_payer":
        t = text.strip().lower()
        if t in ("оставить", "текущим", "оставить текущим", "пропустить", "-", "="):
            state["vtb_payer"] = None
            next_step("vtb_recipient", "4️⃣ Получатель (ФИО):")
            return
        t = text.strip()
        err, suggested = _vtb_full_validate_text(t, "Плательщик")
        if err:
            if suggested:
                state["vtb_payer"] = suggested
                send(f"✅ Применена замена (ё→е): {suggested}")
                next_step("vtb_recipient", "4️⃣ Получатель (ФИО):")
            else:
                send(err)
                return
        else:
            state["vtb_payer"] = t
            next_step("vtb_recipient", "4️⃣ Получатель (ФИО):")

    elif awaiting == "vtb_recipient":
        t = text.strip().lower()
        if t in ("оставить", "текущим", "оставить текущим", "пропустить", "-", "="):
            state["vtb_recipient"] = None
            next_step("vtb_phone", "5️⃣ Телефон получателя:")
            return
        t = text.strip()
        err, suggested = _vtb_full_validate_text(t, "Получатель")
        if err:
            if suggested:
                state["vtb_recipient"] = suggested
                send(f"✅ Применена замена (ё→е): {suggested}")
                next_step("vtb_phone", "5️⃣ Телефон получателя:")
            else:
                send(err)
                return
        else:
            state["vtb_recipient"] = t
            next_step("vtb_phone", "5️⃣ Телефон получателя:")

    elif awaiting == "vtb_phone":
        t = text.strip().lower()
        if t in ("оставить", "текущим", "оставить текущим", "пропустить", "-", "="):
            state["vtb_phone"] = None
            next_step("vtb_bank", "6️⃣ Банк получателя:")
            return
        t = text.strip()
        err, suggested = _vtb_full_validate_text(t, "Телефон")
        if err:
            if suggested:
                state["vtb_phone"] = suggested
                send(f"✅ Применена замена: {suggested}")
                next_step("vtb_bank", "6️⃣ Банк получателя:")
            else:
                send(err)
                return
        else:
            state["vtb_phone"] = t
            next_step("vtb_bank", "6️⃣ Банк получателя:")

    elif awaiting == "vtb_bank":
        t = text.strip().lower()
        if t in ("оставить", "текущим", "оставить текущим", "пропустить", "-", "="):
            state["vtb_bank"] = None
            send("⏳ Обрабатываю чек...")
            _run_vtb_full_patch(token, uid, chat_id, state, tg_req)
            return
        t = text.strip()
        err, suggested = _vtb_full_validate_text(t, "Банк")
        if err:
            if suggested:
                state["vtb_bank"] = suggested
                send(f"✅ Применена замена: {suggested}\n⏳ Обрабатываю чек...")
                _run_vtb_full_patch(token, uid, chat_id, state, tg_req)
                return
            send(err)
            return
        state["vtb_bank"] = t
        send("⏳ Обрабатываю чек...")
        _run_vtb_full_patch(token, uid, chat_id, state, tg_req)


def _do_stmt_apply(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Применить патч выписки и отправить PDF."""
    try:
        from vyписка_service import patch_statement, calculate_balance_and_expenses, BASE_OLD_FIO
    except ImportError as e:
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Модуль выписки недоступен: {e}"})
        if uid in USER_STATE:
            del USER_STATE[uid]
        return
    fp = Path(state["file_path"])
    repl = state.get("replacements", {}).copy()
    trans = list(state.get("transactions", []))
    amount_map = {}
    for pair in repl.get("amounts", []):
        if len(pair) >= 2:
            amount_map[int(pair[0])] = pair[1]
    trans_replaced = [float(amount_map.get(int(t), amount_map.get(t, t))) for t in trans if t > 0]
    balance_start = repl.get("balance_start", 55242.65)
    balance_end, expenses = calculate_balance_and_expenses(trans_replaced, balance_start)
    repl["balance_end"] = balance_end
    repl["expenses"] = expenses
    repl.setdefault("old_fio", BASE_OLD_FIO)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as out_fp:
        out_path = out_fp.name
    ok, err = patch_statement(fp, Path(out_path), repl)
    try:
        os.unlink(state["file_path"])
    except OSError:
        pass
    del USER_STATE[uid]
    if not ok:
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка: {err}"})
        try:
            os.unlink(out_path)
        except OSError:
            pass
        return
    out_name = Path(state.get("file_name", "выписка.pdf")).stem + "_patched.pdf"
    with open(out_path, "rb") as f:
        pdf_bytes = f.read()
    try:
        os.unlink(out_path)
    except OSError:
        pass
    tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": "✅ Выписка готова"}, files={"document": (out_name, pdf_bytes)})


def run_bot(token: str) -> None:
    offset = 0
    print("Бот запущен (без зависимостей)...")

    while True:
        try:
            # timeout 10 — короткий long-poll, меньше обрывов на нестабильной сети
            r = tg_request(token, "getUpdates", {"offset": offset, "timeout": 10})
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode(errors="replace")
            except Exception:
                pass
            err_msg = body or e.reason
            if e.code == 400:
                offset = 0
                print(f"⚠️ getUpdates 400: {err_msg[:120]}")
                if "webhook" in err_msg.lower():
                    print("   Подсказка: webhook мог остаться — перезапустите бота.")
            else:
                print(f"Ошибка getUpdates HTTP {e.code}: {err_msg[:80]}")
            time.sleep(5)
            continue
        except Exception as e:
            print(f"Ошибка getUpdates: {e}")
            time.sleep(5)
            continue

        if not r.get("ok"):
            print("Ответ API:", r)
            time.sleep(5)
            continue

        for upd in r.get("result", []):
            offset = upd["update_id"] + 1

            if "message" in upd:
                msg = upd["message"]
                uid = msg["from"]["id"]
                chat_id = msg["chat"]["id"]
                if _ALLOWED_IDS and uid not in _ALLOWED_IDS:
                    tg_request(token, "sendMessage", {"chat_id": chat_id, "text": ACCESS_DENIED_MSG})
                    continue
                text = msg.get("text", "").strip()

                if text == "/start":
                    if uid in USER_STATE:
                        if "file_path" in USER_STATE[uid]:
                            try:
                                os.unlink(USER_STATE[uid]["file_path"])
                            except OSError:
                                pass
                        del USER_STATE[uid]
                    tg_request(token, "sendMessage", {
                        "chat_id": msg["chat"]["id"],
                        "text": MAIN_MENU_TEXT,
                        "reply_markup": json.dumps({"inline_keyboard": MAIN_MENU_KB}),
                    })
                    continue

                if text == "/main":
                    txt, km = build_zayavki_list()
                    kb = json.loads(km)
                    kb["inline_keyboard"].append([{"text": "🔄 Обновить", "callback_data": "main_zayavki"}, {"text": "⬅️ Назад", "callback_data": "main_back"}])
                    tg_request(token, "sendMessage", {"chat_id": chat_id, "text": txt, "reply_markup": json.dumps(kb)})
                    continue

                if uid in USER_STATE and USER_STATE[uid].get("awaiting") == "report_choice":
                    tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "📋 Нажмите кнопку «Тест» или «Заявка» выше."})
                    continue
                if uid in USER_STATE and USER_STATE[uid].get("awaiting") == "zayavka_description":
                    state = USER_STATE[uid]
                    description = text or "(без описания)"
                    username = msg.get("from", {}).get("username", "") or msg.get("from", {}).get("first_name", "")
                    fp = save_zayavka(
                        uid, username,
                        state["amount_from"], state["amount_to"], state["bank"], state["pdf_name"],
                        description,
                    )
                    del USER_STATE[uid]
                    if fp:
                        tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "✅ Заявка сохранена."})
                    else:
                        tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "⚠️ Ошибка сохранения. Заявка не записана."})
                    continue

                if "document" in msg:
                    doc = msg["document"]
                    fname = doc.get("file_name", "")
                    if not fname.lower().endswith(".pdf"):
                        tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "❌ Отправьте PDF-файл."})
                        continue
                    state = USER_STATE.get(uid, {})
                    mode = state.get("mode", "")
                    if mode == "statement_edit":
                        tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "⏳ Скачиваю выписку..."})
                        try:
                            fp = tg_get_file_path(token, doc["file_id"])
                            pdf_data = tg_get_file(token, fp)
                        except Exception as e:
                            tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": f"❌ Ошибка: {e}"})
                            continue
                        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                            tf.write(pdf_data)
                            path = tf.name
                        try:
                            from vyписка_service import scan_statement_amounts, scan_statement_transactions
                            amounts = scan_statement_amounts(Path(path))
                            transactions = scan_statement_transactions(Path(path))
                        except ImportError:
                            tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "❌ Модуль выписки недоступен."})
                            try:
                                os.unlink(path)
                            except OSError:
                                pass
                            continue
                        USER_STATE[uid] = {"mode": "statement_edit", "step": "amounts", "file_path": path, "file_name": fname, "replacements": {}, "transactions": transactions}
                        tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "✅ Выписка получена.\n\n💰 Введите замены сумм: с какой на какую\nНапример: 10 10000 или 10 5000 50 1000"})
                        continue
                    if mode == "statement_from_receipt":
                        tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "⏳ Скачиваю чек..."})
                        try:
                            fp = tg_get_file_path(token, doc["file_id"])
                            pdf_data = tg_get_file(token, fp)
                        except Exception as e:
                            tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": f"❌ Ошибка: {e}"})
                            continue
                        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                            tf.write(pdf_data)
                            path = tf.name
                        try:
                            from receipt_extractor import extract_from_receipt, generate_fio_from_first_letter
                            from vyписка_service import calculate_balance_and_expenses
                            extracted = extract_from_receipt(Path(path))
                            amount = extracted.get("amount")
                            if not amount:
                                tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "❌ Не удалось извлечь сумму из чека."})
                                try:
                                    os.unlink(path)
                                except OSError:
                                    pass
                                del USER_STATE[uid]
                                continue
                            fio_r = extracted.get("fio_recipient", "") or extracted.get("fio_payer", "")
                            first_letter = fio_r[0] if fio_r else "И"
                            generated_fio = generate_fio_from_first_letter(first_letter)
                            USER_STATE[uid] = {"mode": "statement_from_receipt", "step": "balance", "file_path": path, "file_name": fname, "amount": amount, "generated_fio": generated_fio}
                            tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": f"✅ Чек получен.\n\n📊 Сумма: {amount} ₽\n👤 ФИО: {generated_fio}\n\n💰 Введите баланс на начало периода (число):"})
                        except ImportError as e:
                            tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": f"❌ Модуль недоступен: {e}"})
                            try:
                                os.unlink(path)
                            except OSError:
                                pass
                            del USER_STATE[uid]
                        continue
                    aw = state.get("awaiting", "")
                    if aw.startswith("db_add_"):
                        bank = aw.replace("db_add_", "")
                        tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "⏳ Скачиваю и добавляю в базу..."})
                        try:
                            fp = tg_get_file_path(token, doc["file_id"])
                            pdf_data = tg_get_file(token, fp)
                        except Exception as e:
                            tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": f"❌ Ошибка загрузки: {e}"})
                            continue
                        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                            tf.write(pdf_data)
                            path = tf.name
                        if add_receipt_to_index(path, bank):
                            try:
                                os.unlink(path)
                            except OSError:
                                pass
                            del USER_STATE[uid]
                            tg_request(token, "sendMessage", {
                                "chat_id": msg["chat"]["id"],
                                "text": f"✅ Чек добавлен в базу ({'СБП' if bank == 'vtb_sbp' else 'ВТБ на ВТБ' if bank == 'vtb_vtb_vtb' else 'Альфа'})",
                                "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ К базе", "callback_data": "main_db"}]]}),
                            })
                        else:
                            try:
                                os.unlink(path)
                            except OSError:
                                pass
                            tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "❌ Не удалось добавить (возможно пустой PDF или ошибка)"})
                        continue
                    if "gen_bank_type" in USER_STATE.get(uid, {}) or "gen_transfer_type" in USER_STATE.get(uid, {}):
                        tg_request(token, "sendMessage", {
                            "chat_id": msg["chat"]["id"],
                            "text": "В режиме генерации чек не нужен. Продолжайте ввод выше или нажмите /start.",
                        })
                        continue
                    tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "⏳ Скачиваю чек..."})
                    try:
                        fp = tg_get_file_path(token, doc["file_id"])
                        pdf_data = tg_get_file(token, fp)
                    except Exception as e:
                        tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": f"❌ Ошибка загрузки: {e}"})
                        continue
                    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                        tf.write(pdf_data)
                        path = tf.name
                    old = USER_STATE.get(uid, {})
                    if "file_path" in old and old["file_path"] != path:
                        try:
                            os.unlink(old["file_path"])
                        except OSError:
                            pass
                    USER_STATE[uid] = {"file_path": path, "file_name": fname}
                    tg_request(token, "sendMessage", {
                        "chat_id": msg["chat"]["id"],
                        "text": "📎 Чек получен. Выберите банк:",
                        "reply_markup": json.dumps({
                            "inline_keyboard": [
                                [
                                    {"text": "🏦 Альфа-Банк", "callback_data": "bank_alfa"},
                                    {"text": "🏛 ВТБ", "callback_data": "bank_vtb"},
                                ],
                                [{"text": "🔍 Авто", "callback_data": "bank_auto"}],
                                [{"text": "❌ Отмена", "callback_data": "cancel"}],
                            ],
                        }),
                    })
                    continue

                # Режим выписки: текст
                if uid in USER_STATE and USER_STATE[uid].get("mode", "").startswith("statement_"):
                    _handle_stmt_text(token, uid, chat_id, text, tg_request)
                    continue

                # ВТБ «Все поля»: пошаговый ввод
                if uid in USER_STATE and USER_STATE[uid].get("awaiting", "").startswith("vtb_"):
                    _handle_vtb_full_input(token, uid, chat_id, text, msg, tg_request)
                    continue

                # Режим «Сгенерировать»: по gen_bank_type (надёжнее, чем только awaiting)
                if uid in USER_STATE and "gen_bank_type" in USER_STATE[uid]:
                    _handle_gen_input(token, uid, chat_id, text, msg, tg_request)
                    continue

                if uid in USER_STATE and "bank" in USER_STATE[uid]:
                    parsed = parse_amounts(text)
                    if not parsed:
                        tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "❌ Неверный формат. Введите две суммы, например: 10 5000"})
                        continue
                    amount_from, amount_to = parsed
                    if amount_from <= 0 or amount_to <= 0:
                        tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "❌ Суммы должны быть больше 0."})
                        continue
                    chat_id = msg["chat"]["id"]
                    state = USER_STATE[uid]
                    inp = state["file_path"]
                    bank = state["bank"]
                    out_name = Path(state["file_name"]).stem + f"_{format_amount_display(amount_to).replace(' ', '_')}.pdf"

                    # ВТБ «Только сумма»: patch_amount_only выравнивает по wall.
                    # pdf_patcher не пересчитывает Tm для kern -11.11111 → сумма смещается вправо.
                    if bank == "vtb" and state.get("vtb_mode") == "amount":
                        tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "⏳ Обрабатываю чек..."})
                        try:
                            print(f"  Патч ВТБ (только сумма) {amount_from}→{amount_to}...", end=" ", flush=True)
                            data = bytearray(Path(inp).read_bytes())
                            try:
                                out_bytes = bytes(patch_amount_only(data, Path(inp), amount_to))
                            except Exception:
                                ok_sum, err_sum, new_data = pdf_patch_amount(data, amount_from, amount_to, bank="vtb")
                                if not ok_sum or new_data is None:
                                    del USER_STATE[uid]
                                    try:
                                        os.unlink(inp)
                                    except OSError:
                                        pass
                                    tg_request(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Сумма не найдена. В чеке должна быть {format_amount_display(amount_from)} ₽. {err_sum or ''}"})
                                    continue
                                out_bytes = new_data
                            try:
                                os.unlink(inp)
                            except OSError:
                                pass
                            del USER_STATE[uid]
                            print("отправляю...", end=" ", flush=True)
                            caption = f"✅ Готово: {format_amount_display(amount_from)} ₽ → {format_amount_display(amount_to)} ₽"
                            tg_request(token, "sendDocument", {"chat_id": chat_id, "caption": caption}, files={"document": (out_name, out_bytes)})
                            print("готово")
                            USER_STATE[uid] = {
                                "awaiting": "report_choice",
                                "amount_from": amount_from,
                                "amount_to": amount_to,
                                "bank": bank,
                                "pdf_name": out_name,
                            }
                            tg_request(token, "sendMessage", {
                                "chat_id": chat_id,
                                "text": "📋 Отчёт:",
                                "reply_markup": json.dumps({
                                    "inline_keyboard": [
                                        [{"text": "Тест", "callback_data": "report_test"}],
                                        [{"text": "Заявка", "callback_data": "report_zayavka"}],
                                        [{"text": "🏠 Главное меню", "callback_data": "main_back"}],
                                    ],
                                }),
                            })
                        except Exception as e:
                            print("ошибка:", e)
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка: {e}\n\nПопробуй снова или отправь чек заново."})
                        continue

                    tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "⏳ Обрабатываю чек..."})
                    try:
                        print(f"  Патч {amount_from}→{amount_to} ({bank})...", end=" ", flush=True)
                        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                            out_path = tf.name
                        ok, err = patch_pdf_file(inp, out_path, amount_from, amount_to, bank=bank)
                        try:
                            os.unlink(inp)
                        except OSError:
                            pass
                        del USER_STATE[uid]
                        if not ok:
                            print("не найден")
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка: {err}"})
                            try:
                                os.unlink(out_path)
                            except OSError:
                                pass
                            continue
                        with open(out_path, "rb") as f:
                            pdf_bytes = f.read()
                        try:
                            os.unlink(out_path)
                        except OSError:
                            pass
                        print("отправляю...", end=" ", flush=True)
                        caption = f"✅ Готово: {format_amount_display(amount_from)} ₽ → {format_amount_display(amount_to)} ₽"
                        tg_request(token, "sendDocument", {"chat_id": chat_id, "caption": caption}, files={"document": (out_name, pdf_bytes)})
                        print("готово")
                        USER_STATE[uid] = {
                            "awaiting": "report_choice",
                            "amount_from": amount_from,
                            "amount_to": amount_to,
                            "bank": bank,
                            "pdf_name": out_name,
                        }
                        tg_request(token, "sendMessage", {
                            "chat_id": chat_id,
                            "text": "📋 Отчёт:",
                            "reply_markup": json.dumps({
                                "inline_keyboard": [
                                    [{"text": "Тест", "callback_data": "report_test"}],
                                    [{"text": "Заявка", "callback_data": "report_zayavka"}],
                                    [{"text": "🏠 Главное меню", "callback_data": "main_back"}],
                                ],
                            }),
                        })
                    except Exception as e:
                        print("ошибка:", e)
                        tg_request(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка: {e}\n\nПопробуй снова или отправь чек заново."})
                    continue

                if uid in USER_STATE:
                    if "gen_bank_type" in USER_STATE[uid] or "gen_transfer_type" in USER_STATE[uid]:
                        tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "❌ Продолжайте ввод выше или нажмите кнопки."})
                    else:
                        tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "❌ Сначала выберите банк (кнопками выше)."})
                else:
                    tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "❌ Сначала отправьте чек и выберите банк."})

            elif "callback_query" in upd:
                q = upd["callback_query"]
                uid = q["from"]["id"]
                tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"]})
                if _ALLOWED_IDS and uid not in _ALLOWED_IDS:
                    tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": ACCESS_DENIED_MSG})
                    continue
                if q["data"] == "main_generate":
                    USER_STATE[uid] = {"awaiting": "gen_type", "gen_transfer_type": None, "gen_bank_type": None}
                    tg_request(token, "editMessageText", {
                        "chat_id": q["message"]["chat"]["id"],
                        "message_id": q["message"]["message_id"],
                        "text": (
                            "✨ Сгенерировать чек\n\n"
                            "Бот найдёт один чек из базы с нужными буквами.\n\n"
                            "Выберите тип перевода:"
                        ),
                        "reply_markup": json.dumps({
                            "inline_keyboard": [
                                [{"text": "📱 Перевод по СБП", "callback_data": "gen_type_sbp"}],
                                [{"text": "💳 По номеру карты (скоро)", "callback_data": "gen_type_card"}],
                                [{"text": "🌐 Трансгран (скоро)", "callback_data": "gen_type_transgran"}],
                                [{"text": "⬅️ Назад", "callback_data": "main_back"}],
                            ],
                        }),
                    })
                    continue
                if q["data"] == "gen_type_sbp":
                    USER_STATE[uid] = {"awaiting": "gen_subtype", "gen_transfer_type": "sbp", "gen_bank_type": None, "gen_vtb_subtype": None}
                    tg_request(token, "editMessageText", {
                        "chat_id": q["message"]["chat"]["id"],
                        "message_id": q["message"]["message_id"],
                        "text": (
                            "✨ Перевод по СБП\n\n"
                            "Выберите тип чека:"
                        ),
                        "reply_markup": json.dumps({
                            "inline_keyboard": [
                                [{"text": "📱 СБП", "callback_data": "gen_subtype_sbp"}],
                                [{"text": "🏛 ВТБ на ВТБ", "callback_data": "gen_subtype_vtb_vtb"}],
                                [{"text": "⬅️ Назад", "callback_data": "main_generate"}],
                            ],
                        }),
                    })
                    continue
                if q["data"] == "gen_subtype_sbp":
                    USER_STATE[uid] = {"awaiting": "gen_bank", "gen_transfer_type": "sbp", "gen_bank_type": None, "gen_vtb_subtype": "vtb_sbp"}
                    tg_request(token, "editMessageText", {
                        "chat_id": q["message"]["chat"]["id"],
                        "message_id": q["message"]["message_id"],
                        "text": (
                            "✨ СБП\n\n"
                            f"{VTB_UNSUPPORTED_NOTICE}\n\n"
                            "Выберите банк (пока только ВТБ):"
                        ),
                        "reply_markup": json.dumps({
                            "inline_keyboard": [
                                [{"text": "🏛 ВТБ", "callback_data": "gen_bank_vtb"}],
                                [{"text": "⬅️ Назад", "callback_data": "main_generate"}],
                            ],
                        }),
                    })
                    continue
                if q["data"] == "gen_subtype_vtb_vtb":
                    USER_STATE[uid] = {"awaiting": "gen_bank", "gen_transfer_type": "sbp", "gen_bank_type": None, "gen_vtb_subtype": "vtb_vtb_vtb"}
                    tg_request(token, "editMessageText", {
                        "chat_id": q["message"]["chat"]["id"],
                        "message_id": q["message"]["message_id"],
                        "text": (
                            "✨ ВТБ на ВТБ\n\n"
                            f"{VTB_UNSUPPORTED_NOTICE}\n\n"
                            "Выберите банк (пока только ВТБ):"
                        ),
                        "reply_markup": json.dumps({
                            "inline_keyboard": [
                                [{"text": "🏛 ВТБ", "callback_data": "gen_bank_vtb"}],
                                [{"text": "⬅️ Назад", "callback_data": "main_generate"}],
                            ],
                        }),
                    })
                    continue
                if q["data"] in ("gen_type_card", "gen_type_transgran"):
                    if uid in USER_STATE and "gen_bank_type" in USER_STATE[uid]:
                        del USER_STATE[uid]
                    tg_request(token, "editMessageText", {
                        "chat_id": q["message"]["chat"]["id"],
                        "message_id": q["message"]["message_id"],
                        "text": "Пока доступен только перевод по СБП.",
                        "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "main_back"}]]}),
                    })
                    continue
                if q["data"] == "main_db":
                    counts = get_bank_counts()
                    n_sbp, n_vtb, n_alfa = counts["vtb_sbp"], counts["vtb_vtb_vtb"], counts["alfa"]
                    tg_request(token, "editMessageText", {
                        "chat_id": q["message"]["chat"]["id"],
                        "message_id": q["message"]["message_id"],
                        "text": (
                            f"📂 Проверка базы\n\n"
                            f"СБП: {n_sbp} | ВТБ на ВТБ: {n_vtb} | Альфа: {n_alfa}\n\n"
                            "Чтобы добавить чек: выберите тип, затем загрузите PDF.\n"
                            "Если добавили файлы вручную — нажмите «Обновить индекс»."
                        ),
                        "reply_markup": json.dumps({
                            "inline_keyboard": [
                                [{"text": "📋 СБП", "callback_data": "db_list_vtb_sbp"}, {"text": "➕ СБП", "callback_data": "db_add_vtb_sbp"}],
                                [{"text": "📋 ВТБ→ВТБ", "callback_data": "db_list_vtb_vtb"}, {"text": "➕ ВТБ→ВТБ", "callback_data": "db_add_vtb_vtb"}],
                                [{"text": "📋 Альфа", "callback_data": "db_list_alfa"}, {"text": "➕ Альфа", "callback_data": "db_add_alfa"}],
                                [{"text": "🔄 Обновить индекс", "callback_data": "db_rebuild"}],
                                [{"text": "⬅️ Назад", "callback_data": "main_back"}],
                            ],
                        }),
                    })
                    continue
                if q["data"] == "db_rebuild":
                    try:
                        idx = build_and_save()
                        n_sbp = len(idx.get("vtb_sbp", {}))
                        n_vtb = len(idx.get("vtb_vtb_vtb", {}))
                        n_alfa = len(idx.get("alfa", {}))
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": f"✅ Индекс обновлён\n\nСБП: {n_sbp} | ВТБ на ВТБ: {n_vtb} | Альфа: {n_alfa}",
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ К базе", "callback_data": "main_db"}]]}),
                        })
                    except Exception as e:
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": f"⚠️ Ошибка: {e}",
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "main_db"}]]}),
                        })
                    continue
                if q["data"] in ("db_add_vtb_sbp", "db_add_vtb_vtb", "db_add_alfa"):
                    if "vtb_sbp" in q["data"]:
                        bank = "vtb_sbp"
                    elif "vtb_vtb" in q["data"]:
                        bank = "vtb_vtb_vtb"
                    else:
                        bank = "alfa"
                    USER_STATE[uid] = {"awaiting": f"db_add_{bank}"}
                    lbl = {"vtb_sbp": "СБП", "vtb_vtb_vtb": "ВТБ на ВТБ", "alfa": "Альфа"}.get(bank, bank)
                    tg_request(token, "editMessageText", {
                        "chat_id": q["message"]["chat"]["id"],
                        "message_id": q["message"]["message_id"],
                        "text": f"➕ Отправьте PDF-чек для добавления в базу ({lbl})",
                        "reply_markup": json.dumps({"inline_keyboard": [[{"text": "❌ Отмена", "callback_data": "main_db"}]]}),
                    })
                    continue
                if q["data"] == "db_list_vtb_sbp":
                    idx = load_index()
                    items = list(idx.get("vtb_sbp", {}).keys())
                    lines = ["📋 Чеки СБП:"] + (items[:20] or ["(пусто)"])
                    if len(items) > 20:
                        lines.append(f"... и ещё {len(items) - 20}")
                    tg_request(token, "editMessageText", {
                        "chat_id": q["message"]["chat"]["id"],
                        "message_id": q["message"]["message_id"],
                        "text": "\n".join(lines),
                        "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "main_db"}]]}),
                    })
                    continue
                if q["data"] == "db_list_vtb_vtb":
                    idx = load_index()
                    items = list(idx.get("vtb_vtb_vtb", {}).keys())
                    lines = ["📋 Чеки ВТБ на ВТБ:"] + (items[:20] or ["(пусто)"])
                    if len(items) > 20:
                        lines.append(f"... и ещё {len(items) - 20}")
                    tg_request(token, "editMessageText", {
                        "chat_id": q["message"]["chat"]["id"],
                        "message_id": q["message"]["message_id"],
                        "text": "\n".join(lines),
                        "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "main_db"}]]}),
                    })
                    continue
                if q["data"] == "db_list_alfa":
                    idx = load_index()
                    items = list(idx.get("alfa", {}).keys())
                    lines = ["📋 Чеки Альфа:"] + (items[:20] or ["(пусто)"])
                    if len(items) > 20:
                        lines.append(f"... и ещё {len(items) - 20}")
                    tg_request(token, "editMessageText", {
                        "chat_id": q["message"]["chat"]["id"],
                        "message_id": q["message"]["message_id"],
                        "text": "\n".join(lines),
                        "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "main_db"}]]}),
                    })
                    continue
                if q["data"] == "gen_bank_vtb":
                    prev = USER_STATE.get(uid, {})
                    subtype = prev.get("gen_vtb_subtype", "vtb_sbp")
                    USER_STATE[uid] = {
                        "awaiting": "gen_payer" if subtype == "vtb_sbp" else "gen_amount",
                        "gen_bank_type": "vtb",
                        "gen_transfer_type": prev.get("gen_transfer_type", "sbp"),
                        "gen_vtb_subtype": subtype,
                    }
                    if subtype == "vtb_sbp":
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": (
                                "✨ Сгенерировать чек СБП (ВТБ)\n\n"
                                "📋 Сначала проверка ФИО.\n\n"
                                "1️⃣ Плательщик (например: Артем Никитич К.):"
                            ),
                        })
                    else:
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": (
                                "✨ Сгенерировать (ВТБ)\n\n"
                                "1️⃣ Сумма: с какой на какую (например: 10 1000) или одна сумма (50000)"
                            ),
                        })
                    continue
                if q["data"] in ("gen_keep_payer", "gen_keep_recipient", "gen_keep_phone", "gen_keep_bank", "gen_keep_account", "gen_keep_opid"):
                    if uid not in USER_STATE:
                        tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "❌ Сессия истекла"})
                        continue
                    state = USER_STATE[uid]
                    cid = q["message"]["chat"]["id"]
                    subtype = state.get("gen_vtb_subtype", "vtb_sbp")
                    keep_map = {
                        "gen_keep_payer": ("gen_payer", "gen_recipient", "4️⃣ Получатель (ФИО):"),
                        "gen_keep_recipient": ("gen_recipient", "gen_phone" if subtype != "vtb_sbp" else None, "5️⃣ Телефон:" if subtype != "vtb_sbp" else ""),
                        "gen_keep_phone": ("gen_phone", "gen_bank", "6️⃣ Банк получателя:"),
                        "gen_keep_bank": ("gen_bank", "gen_account" if subtype == "vtb_sbp" else "gen_operation_id", "6️⃣ Счёт (4 цифры):" if subtype == "vtb_sbp" else "7️⃣ ID операции:"),
                        "gen_keep_account": ("gen_account", "gen_operation_id", "7️⃣ ID операции (B606...). Оставить или ввести:"),
                        "gen_keep_opid": ("gen_operation_id", None, ""),
                    }
                    field_key, next_aw, _ = keep_map[q["data"]]
                    state[field_key] = None
                    tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "✅ Пусто"})
                    if next_aw:
                        _gen_send_next(state, next_aw, "", cid, token, tg_request)
                    else:
                        tg_request(token, "sendMessage", {"chat_id": cid, "text": "⏳ Генерирую чек..."})
                        if state.get("gen_vtb_subtype") == "vtb_sbp":
                            _run_sbp_generate(token, uid, cid, state, tg_request)
                        else:
                            _run_gen_patch(token, uid, cid, state, tg_request)
                    continue
                if q["data"] == "main_new":
                    tg_request(token, "editMessageText", {
                        "chat_id": q["message"]["chat"]["id"],
                        "message_id": q["message"]["message_id"],
                        "text": (
                            "📄 Новый чек\n\n"
                            "1. Отправьте PDF-чек\n"
                            "2. Выберите банк (Альфа-Банк или ВТБ)\n"
                            "3. Введите сумму: с какой на какую (например: 10 5000)"
                        ),
                        "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "main_back"}]]}),
                    })
                    continue
                if q["data"] == "main_stmt":
                    tg_request(token, "editMessageText", {
                        "chat_id": q["message"]["chat"]["id"],
                        "message_id": q["message"]["message_id"],
                        "text": "📄 Создание выписки\n\nВыберите вариант:",
                        "reply_markup": json.dumps({
                            "inline_keyboard": [
                                [{"text": "✏️ Редактирование своей выписки", "callback_data": "stmt_edit"}],
                                [{"text": "📄 Выписка по чеку", "callback_data": "stmt_receipt"}],
                                [{"text": "⬅️ Назад", "callback_data": "main_back"}],
                            ],
                        }),
                    })
                    continue
                if q["data"] == "stmt_edit":
                    USER_STATE[uid] = {"mode": "statement_edit", "step": "upload"}
                    tg_request(token, "editMessageText", {
                        "chat_id": q["message"]["chat"]["id"],
                        "message_id": q["message"]["message_id"],
                        "text": "✏️ Редактирование своей выписки\n\nОтправьте PDF-файл выписки.",
                        "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "main_back"}]]}),
                    })
                    continue
                if q["data"] == "stmt_receipt":
                    USER_STATE[uid] = {"mode": "statement_from_receipt", "step": "upload"}
                    tg_request(token, "editMessageText", {
                        "chat_id": q["message"]["chat"]["id"],
                        "message_id": q["message"]["message_id"],
                        "text": "📄 Выписка по чеку\n\nОтправьте PDF-файл чека.",
                        "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "main_back"}]]}),
                    })
                    continue
                if q["data"] == "stmt_skip" or q["data"] == "stmt_next":
                    if uid not in USER_STATE or USER_STATE[uid].get("mode") != "statement_edit":
                        tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "❌ Сессия истекла"})
                        continue
                    _do_stmt_apply(token, uid, q["message"]["chat"]["id"], USER_STATE[uid], tg_request)
                    continue
                if q["data"] == "stmt_custom":
                    if uid in USER_STATE:
                        USER_STATE[uid]["step"] = "custom"
                    tg_request(token, "editMessageText", {
                        "chat_id": q["message"]["chat"]["id"],
                        "message_id": q["message"]["message_id"],
                        "text": "➕ Введите замены: поле=значение\nФИО=..., баланс_начало=..., телефон=..., номер_заявки=...\nПосле ввода нажмите Далее.",
                        "reply_markup": json.dumps({"inline_keyboard": [[{"text": "➡️ Далее", "callback_data": "stmt_next"}]]}),
                    })
                    continue
                if q["data"] == "stmt_retry_fio":
                    if uid in USER_STATE:
                        USER_STATE[uid]["step"] = "custom"
                    tg_request(token, "sendMessage", {"chat_id": q["message"]["chat"]["id"], "text": "Введите ФИО заново (без недоступных символов):"})
                    continue
                if q["data"] == "stmt_skip_fio":
                    if uid in USER_STATE:
                        USER_STATE[uid].pop("pending_fio", None)
                    tg_request(token, "sendMessage", {"chat_id": q["message"]["chat"]["id"], "text": "ФИО не заменяется. Введите другие замены или Далее."})
                    continue
                if q["data"] == "main_back":
                    if uid in USER_STATE and USER_STATE[uid].get("mode", "").startswith("statement_"):
                        if "file_path" in USER_STATE[uid]:
                            try:
                                os.unlink(USER_STATE[uid]["file_path"])
                            except OSError:
                                pass
                        del USER_STATE[uid]
                    tg_request(token, "editMessageText", {
                        "chat_id": q["message"]["chat"]["id"],
                        "message_id": q["message"]["message_id"],
                        "text": MAIN_MENU_TEXT,
                        "reply_markup": json.dumps({"inline_keyboard": MAIN_MENU_KB}),
                    })
                    continue
                if q["data"] == "main_changelog":
                    tg_request(token, "editMessageText", {
                        "chat_id": q["message"]["chat"]["id"],
                        "message_id": q["message"]["message_id"],
                        "text": CHANGELOG_TEXT,
                        "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "main_back"}]]}),
                    })
                    continue
                if q["data"] == "main_zayavki":
                    try:
                        txt, km = build_zayavki_list()
                        kb = json.loads(km)
                        kb["inline_keyboard"].append([{"text": "🔄 Обновить", "callback_data": "main_zayavki"}, {"text": "⬅️ Назад", "callback_data": "main_back"}])
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": txt,
                            "reply_markup": json.dumps(kb),
                        })
                    except Exception as e:
                        print(f"  ⚠️ main_zayavki: {e}")
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "⚠️ Ошибка обновления. Попробуйте ещё раз.",
                        })
                    continue
                if q["data"].startswith("v_"):
                    try:
                        zid = q["data"][2:] if len(q["data"]) > 2 else ""
                        if zid and (detail := build_zayavka_detail(zid)):
                            txt, km = detail
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": txt,
                                "reply_markup": km,
                            })
                        else:
                            tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "⚠️ Заявка не найдена"})
                    except Exception as e:
                        print(f"  ⚠️ v_ callback: {e}")
                        tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "⚠️ Ошибка"})
                    continue
                if q["data"].startswith("st_"):
                    try:
                        parts = q["data"].split("_")
                        if len(parts) >= 3:
                            zid = "_".join(parts[1:-1])
                            short = parts[-1]
                            new_st = STATUS_SHORT.get(short)
                            if new_st and update_zayavka_status(zid, new_st):
                                if detail := build_zayavka_detail(zid):
                                    txt, km = detail
                                    tg_request(token, "editMessageText", {
                                        "chat_id": q["message"]["chat"]["id"],
                                        "message_id": q["message"]["message_id"],
                                        "text": txt,
                                        "reply_markup": km,
                                    })
                                else:
                                    txt, km = build_zayavki_list()
                                    kb = json.loads(km)
                                    kb["inline_keyboard"].append([{"text": "⬅️ Назад", "callback_data": "main_back"}])
                                    tg_request(token, "editMessageText", {
                                        "chat_id": q["message"]["chat"]["id"],
                                        "message_id": q["message"]["message_id"],
                                        "text": txt,
                                        "reply_markup": json.dumps(kb),
                                    })
                            else:
                                tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "⚠️ Не удалось"})
                    except Exception as e:
                        print(f"  ⚠️ st_ callback: {e}")
                        tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "⚠️ Ошибка"})
                    continue
                if q["data"] == "cancel":
                    if uid in USER_STATE and "file_path" in USER_STATE[uid]:
                        try:
                            os.unlink(USER_STATE[uid]["file_path"])
                        except OSError:
                            pass
                        del USER_STATE[uid]
                    main_text = (
                        "👋 Главное меню.\n\n"
                        "📋 Отправьте PDF-чек или /start для начала."
                    )
                    tg_request(token, "editMessageText", {
                        "chat_id": q["message"]["chat"]["id"],
                        "message_id": q["message"]["message_id"],
                        "text": main_text,
                        "reply_markup": json.dumps({"inline_keyboard": []}),
                    })
                    continue
                if q["data"] == "report_test":
                    if uid in USER_STATE:
                        del USER_STATE[uid]
                    tg_request(token, "editMessageText", {
                        "chat_id": q["message"]["chat"]["id"],
                        "message_id": q["message"]["message_id"],
                        "text": "📋 Отчёт: Тест (ничего не сохранено)",
                        "reply_markup": json.dumps({"inline_keyboard": []}),
                    })
                    continue
                if q["data"] == "report_zayavka":
                    if uid not in USER_STATE:
                        tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Сессия истекла. Отправьте чек заново."})
                        continue
                    USER_STATE[uid]["awaiting"] = "zayavka_description"
                    tg_request(token, "editMessageText", {
                        "chat_id": q["message"]["chat"]["id"],
                        "message_id": q["message"]["message_id"],
                        "text": "📝 Опишите заявку:\nссылка на платеж / казино / обменник",
                        "reply_markup": json.dumps({"inline_keyboard": []}),
                    })
                    continue
                if q["data"] == "vtb_mode_amount":
                    if uid not in USER_STATE:
                        tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                        continue
                    USER_STATE[uid]["vtb_mode"] = "amount"
                    tg_request(token, "editMessageText", {
                        "chat_id": q["message"]["chat"]["id"],
                        "message_id": q["message"]["message_id"],
                        "text": "💰 Введите сумму: с какой на какую менять\nНапример: 10 5000 или 50 10000",
                    })
                    continue
                if q["data"] == "vtb_mode_full":
                    if uid not in USER_STATE:
                        tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                        continue
                    USER_STATE[uid]["vtb_mode"] = "full"
                    USER_STATE[uid]["awaiting"] = "vtb_amount"
                    txt = (
                        "📋 Режим «Все поля»\n\n"
                        f"{VTB_UNSUPPORTED_NOTICE}\n\n"
                        "1️⃣ Сумма: с какой на какую менять (например: 10 1000 или 50 50000)"
                    )
                    tg_request(token, "editMessageText", {
                        "chat_id": q["message"]["chat"]["id"],
                        "message_id": q["message"]["message_id"],
                        "text": txt,
                    })
                    continue
                if q["data"] in ("vtb_keep_payer", "vtb_keep_recipient", "vtb_keep_phone", "vtb_keep_bank"):
                    if uid not in USER_STATE:
                        tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "❌ Сессия истекла"})
                        continue
                    state = USER_STATE[uid]
                    cid, mid = q["message"]["chat"]["id"], q["message"]["message_id"]
                    keep_map = {
                        "vtb_keep_payer": ("vtb_payer", "vtb_recipient", "4️⃣ Получатель (ФИО):"),
                        "vtb_keep_recipient": ("vtb_recipient", "vtb_phone", "5️⃣ Телефон получателя:"),
                        "vtb_keep_phone": ("vtb_phone", "vtb_bank", "6️⃣ Банк получателя:"),
                        "vtb_keep_bank": ("vtb_bank", None, ""),
                    }
                    field_key, next_aw, _ = keep_map[q["data"]]
                    state[field_key] = None
                    tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "✅ Оставлено текущим"})
                    if next_aw:
                        _vtb_full_send_next(state, next_aw, "", cid, token, tg_request)
                    else:
                        tg_request(token, "sendMessage", {"chat_id": cid, "text": "⏳ Обрабатываю чек..."})
                        _run_vtb_full_patch(token, uid, cid, state, tg_request)
                    continue
                if uid not in USER_STATE:
                    tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                    continue
                bank_map = {"bank_alfa": "alfa", "bank_vtb": "vtb", "bank_auto": "auto"}
                bank = bank_map.get(q["data"], "auto")
                bank_name = {"alfa": "Альфа-Банк", "vtb": "ВТБ", "auto": "Авто"}[bank]
                USER_STATE[uid]["bank"] = bank
                if bank == "vtb":
                    scan_tip = ""
                    try:
                        bad = scan_vtb_unsupported_chars(USER_STATE[uid]["file_path"])
                        if bad:
                            tips = [f"«{c}» → «{FALLBACK_TIPS.get(c, '?')}»" if c in FALLBACK_TIPS else f"«{c}»" for c in sorted(bad)]
                            scan_tip = f"\n\n⚠️ В чеке найдены буквы для замены: {', '.join(tips)}"
                    except Exception:
                        pass
                    tg_request(token, "editMessageText", {
                        "chat_id": q["message"]["chat"]["id"],
                        "message_id": q["message"]["message_id"],
                        "text": f"✅ Банк: {bank_name}{scan_tip}\n\nВыберите режим:",
                        "reply_markup": json.dumps({
                            "inline_keyboard": [
                                [{"text": "💰 Только сумма", "callback_data": "vtb_mode_amount"}],
                                [{"text": "📋 Все поля (дата, ФИО, телефон, банк)", "callback_data": "vtb_mode_full"}],
                                [{"text": "⬅️ Назад", "callback_data": "cancel"}],
                            ],
                        }),
                    })
                else:
                    tg_request(token, "editMessageText", {
                        "chat_id": q["message"]["chat"]["id"],
                        "message_id": q["message"]["message_id"],
                        "text": f"✅ Банк: {bank_name}\n\n💰 Введите сумму: с какой на какую менять\nНапример: 10 5000 или 50 10000",
                    })
                continue


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("Задайте TELEGRAM_BOT_TOKEN (в .env или export)")
        return
    if _proxy_url:
        _masked = _proxy_url.split("@")[-1] if "@" in _proxy_url else _proxy_url[:50]
        print("🔒 Прокси:", _masked)
    # Проверка токена и сброс webhook (иначе getUpdates даёт 400)
    try:
        r = tg_request(token, "getMe")
        if r.get("ok"):
            print("✅ Бот:", r["result"].get("username", "?"))
            if _ALLOWED_IDS:
                print("🔐 Доступ: только", len(_ALLOWED_IDS), "пользовател(ей)")
            dw = tg_request(token, "deleteWebhook", {"drop_pending_updates": True})
            if not dw.get("ok"):
                print("⚠️ deleteWebhook:", dw.get("description", dw))
            run_bot(token)
        else:
            print("❌ Токен неверный:", r.get("description"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("❌ Токен неверный (401). Сделай в @BotFather:")
            print("   /mybots → выбери бота → API Token → Revoke current token")
            print("   Обнови .env с новым токеном")
        else:
            print("❌ Ошибка HTTP:", e.code, e.reason)


if __name__ == "__main__":
    main()
