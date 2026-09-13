"""Alfa-Bank «Квитанция о переводе по СБП» generator.

Builds authentic Oracle BI Publisher 12.2.1.4.0 PDFs from data —
no real transfer required, no patchable template handed in by the caller.

Strategy
--------
A genuine Alfa SBP receipt (verified donor ``template_sbp.pdf`` in assets/)
is used as a structural skeleton.  All variable fields are replaced via
``obi_patch_pdf`` which:
  - re-encodes new text as 2-byte GID sequences (Tahoma Identity-H),
  - extends the Tahoma 3.14 subset from the authentic glyph bank for any
    characters not present in the donor,
  - rebuilds the font subset with sequential GIDs 1,2,3…N — identical to
    how Oracle BI Publisher assigns them natively,
  - patches /BaseFont with a fresh random XXXXXX+Tahoma prefix,
  - sets /CreationDate / /ModDate from the «Сформирована» timestamp,
  - rebuilds xref and /ID.

The resulting PDF passes ``gid_order_is_genuine``, ``xref_is_intact``, and
``content_is_unpadded`` — the three authenticity signals used by
``build_glyph_bank.py`` — and renders identically to a genuine receipt.

Usage
-----
>>> from alfa.make_alfa_sbp_receipt import build, build_variants
>>> pdf_bytes = build({
...     "amount":      34090,
...     "phone":       "+7 (996) 232-33-53",
...     "transfer_dt": "11.09.2026 00:42:17",
...     "recipient":   "Андрей Кирилович Н",
...     "bank":        "Сбербанк",
...     "account":     "40817810980480009039",
... })
"""

from __future__ import annotations

import datetime
import logging
import os
import random
import re
import sys
import zlib
from typing import Optional

# obi_patcher, the glyph bank and assets all live in this same folder.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import obi_patcher as _op  # noqa: E402

logger = logging.getLogger(__name__)

# ── assets ─────────────────────────────────────────────────────────────────
ASSETS   = os.path.join(_HERE, "assets")
TEMPLATE = os.path.join(ASSETS, "template_sbp.pdf")   # genuine donor

# Moscow timezone
_MSK = datetime.timezone(datetime.timedelta(hours=3))

# Day-counter epoch for SBP identifiers (same as Yandex/NSPK)
_NSPK_EPOCH = datetime.datetime(2009, 7, 28).date()

# SBP scheme tail by date of first observed use
_SBP_SCHEME_BUILDS = [
    (datetime.date(2026, 9, 9), "0011850501"),
    (datetime.date(2026, 8, 29), "0011840301"),
    (datetime.date(2026, 8, 22), "0011831501"),
    (datetime.date(2026, 2, 6),  "0011690101"),
]


# ── public exception ───────────────────────────────────────────────────────
class AlfaReceiptError(Exception):
    """Raised when receipt generation fails (e.g. missing glyph in bank)."""


# ── helper: load and decode donor fields ───────────────────────────────────
def _load_donor() -> tuple[bytes, bytes, dict]:
    """Return (donor_bytes, clean_font, field_map {(x,y): text})."""
    with open(TEMPLATE, "rb") as fh:
        donor = fh.read()
    font  = _op._extract_font_bytes(donor)
    cmap  = _op.extract_cmap_from_pdf(donor)
    g2c   = {g: c for c, g in cmap.items()}

    objs = {int(m.group(1)): m.group(2)
            for m in re.finditer(rb"(\d+) 0 obj(.*?)endobj", donor, re.S)}
    cs = None
    for o in objs.values():
        if b"stream" in o:
            try:
                sm = (re.search(rb"stream\r?\n?(.*?)\r?\nendstream", o, re.S) or
                      re.search(rb"stream\r(.*?)\rendstream", o, re.S))
                d = zlib.decompress(sm.group(1))
                if b" Tj" in d and not _op._is_font_stream(d):
                    cs = d.decode("latin1")
                    break
            except Exception:
                pass
    if cs is None:
        raise AlfaReceiptError("Could not decompress content stream of template")

    def _dec(h: str) -> str:
        return "".join(g2c.get(int(h[i:i+4], 16), "?")
                       for i in range(0, len(h), 4))

    fields: dict = {}
    pat = (r"1 0 0 1 ([\d.]+) ([\d.]+) Tm\s*/F1\s*([\d.]+) Tf\s*"
           r"<([0-9A-Fa-f]+)> Tj")
    for m in re.finditer(pat, cs):
        x, y, _sz, h = m.groups()
        t = _dec(h)
        if t.strip():
            fields[(round(float(x), 2), round(float(y), 2))] = t
    return donor, font, fields


# ── helper: format amount ──────────────────────────────────────────────────
def _fmt_amount(amount) -> str:
    """34090 → '34\xa0090\xa0RUR\xa0'  (non-breaking spaces as in genuine PDF)"""
    try:
        n = int(round(float(str(amount).replace(",", ".").replace("\xa0", "").replace(" ", ""))))
    except Exception:
        n = 0
    formatted = f"{n:,}".replace(",", "\xa0")   # thousands NBSP
    return f"{formatted}\xa0RUR\xa0"


# ── helper: parse transfer datetime ───────────────────────────────────────
def _parse_dt(transfer_dt) -> datetime.datetime:
    if isinstance(transfer_dt, datetime.datetime):
        return transfer_dt
    s = str(transfer_dt).strip()
    # "11.09.2026 00:42:17"  or  ISO "2026-09-11T00:42:17"
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise AlfaReceiptError(f"Cannot parse transfer_dt: {transfer_dt!r}")


def am_download_name(formed_dt: datetime.datetime, rng: random.Random = random) -> str:
    """Filename the Alfa app writes: AM_<epoch_ms>.pdf.

    Millis are the download clock (now), plus jitter so two saves never collide.
    """
    msk = datetime.timezone(datetime.timedelta(hours=3))
    now = datetime.datetime.now(msk)
    millis = int(now.timestamp() * 1000) + rng.randint(0, 999)
    return f"AM_{millis}.pdf"


# ── helper: operation id ───────────────────────────────────────────────────
# The 7 digits after C16DDMMYY are NOT random: the first four are Alfa's
# running daily operation counter and the last three a channel id.
#
# Counter anchors harvested from genuine receipts (minutes since midnight MSK
# → counter).  The curve is flat overnight, steep through business hours and
# tapers off in the evening, so a receipt timestamped 00:42 must carry a
# two-digit counter — emitting 8649 there is an instant giveaway.
#
#   01:18 →   26        16:49 → 1737
#   01:29 →   29        23:42 → 2061
#   09:10 →  403
_OPID_ANCHORS = [
    (0, 0), (78, 26), (89, 29), (550, 403), (1009, 1737), (1422, 2061),
    (1440, 2075),
]

# Trailing three digits observed on genuine receipts.  385 shows up twice in
# five samples, so these are a small fixed set (channel/terminal), not random.
_OPID_CHANNELS = ["385", "385", "013", "119", "523"]


def _daily_counter(dt: datetime.datetime, rng: random.Random) -> int:
    """Interpolate Alfa's running daily operation counter for a timestamp."""
    minutes = dt.hour * 60 + dt.minute + dt.second / 60.0
    for (m0, c0), (m1, c1) in zip(_OPID_ANCHORS, _OPID_ANCHORS[1:]):
        if m0 <= minutes <= m1:
            frac = (minutes - m0) / (m1 - m0) if m1 > m0 else 0.0
            base = c0 + frac * (c1 - c0)
            break
    else:
        base = _OPID_ANCHORS[-1][1]
    # Daily volume differs between days; genuine anchors come from three
    # separate days, so scale the whole curve a little per receipt.
    base *= rng.uniform(0.85, 1.15)
    return max(1, min(9999, int(round(base))))


def _make_opid(dt: datetime.datetime, rng: random.Random) -> str:
    """C16 + DDMMYY + daily counter (4) + channel (3)."""
    ddmmyy  = dt.strftime("%d%m%y")
    counter = _daily_counter(dt, rng)
    channel = rng.choice(_OPID_CHANNELS)
    return f"C16{ddmmyy}{counter:04d}{channel}"


# ── helper: SBP identifier ────────────────────────────────────────────────
def _sbp_scheme_tail(dt: datetime.datetime) -> str:
    d = dt.date()
    for cutoff, tail in _SBP_SCHEME_BUILDS:
        if d >= cutoff:
            return tail
    return "0011690101"


def _make_sbp_id(dt: datetime.datetime, rng: random.Random) -> str:
    """32-char NSPK SBP identifier matching genuine Alfa receipts.

    Layout:  A|6253|1349|52|7981X|0B10|02|0011850501
             ^ ^    ^    ^  ^     ^    ^  ^
             | day  UTC  ss rand5 lit  |  NSPK platform build
             prefix     HHMM           counter 01..20

    The SBP id is stamped when the transfer is *initiated*, which genuine
    receipts show as 4–6 seconds before the completion time printed in
    «Дата и время перевода».
    """
    # The identifier is timestamped a few seconds before completion.
    dt_init = dt - datetime.timedelta(seconds=rng.randint(4, 6))
    dt_utc  = dt_init - datetime.timedelta(hours=3)   # MSK → UTC
    days    = (dt_utc.date() - _NSPK_EPOCH).days
    hhmm    = dt_utc.strftime("%H%M")
    ss      = dt_utc.strftime("%S")
    # Genuine receipts carry four digits plus one final slot that is a digit
    # or a single uppercase letter (observed: 95907, 39004, 50209, 7981X, 3820R).
    rand5   = ("".join(rng.choice("0123456789") for _ in range(4))
               + rng.choice("0123456789XR"))
    ctr     = f"{rng.randint(1, 20):02d}"
    tail    = _sbp_scheme_tail(dt)
    sbp_id  = (rng.choice("AB") + f"{days:04d}" + hhmm + ss
               + rand5 + "0B10" + ctr + tail)
    assert len(sbp_id) == 32, f"SBP id length bug: {len(sbp_id)}"
    return sbp_id


# ── random plausible receipt data ─────────────────────────────────────────
# АО «АЛЬФА-БАНК», printed on the stamp of every receipt.
BIK = "044525593"

# Control-key weights from the Central Bank account-validation algorithm.
_CK_WEIGHTS = [7, 1, 3] * 7 + [7, 1]

_MALE_NAMES = [
    "Александр", "Андрей", "Сергей", "Дмитрий", "Игорь", "Максим", "Роман",
    "Артём", "Никита", "Павел", "Кирилл", "Илья", "Егор", "Демид", "Ян",
    "Владимир", "Михаил", "Алексей", "Евгений", "Тимофей", "Матвей",
]
_MALE_PATRONYMICS = [
    "Александрович", "Андреевич", "Сергеевич", "Дмитриевич", "Игоревич",
    "Максимович", "Романович", "Анатольевич", "Николаевич", "Павлович",
    "Кириллович", "Ильич", "Евгеньевич", "Викторович", "Иванович",
    "Петрович", "Олегович", "Юрьевич", "Валерьевич",
]
_FEMALE_NAMES = [
    "Анастасия", "Мария", "Екатерина", "Ольга", "Наталья", "Василиса",
    "Ксения", "Дарья", "Елена", "Татьяна", "Юлия", "Алина", "Полина",
    "Софья", "Виктория", "Ирина", "Светлана",
]
_FEMALE_PATRONYMICS = [
    "Александровна", "Андреевна", "Сергеевна", "Дмитриевна", "Игоревна",
    "Максимовна", "Романовна", "Анатольевна", "Николаевна", "Павловна",
    "Кирилловна", "Евгеньевна", "Викторовна", "Ивановна", "Петровна",
    "Олеговна", "Юрьевна", "Валерьевна",
]
_SURNAME_INITIALS = list("АБВГДЕЖЗИКЛМНОПРСТФХЦЧШЩЭЮЯ")

# Banks observed as SBP transfer destinations on genuine receipts.
_BANKS = ["Сбербанк", "Т-Банк", "ВТБ"]

# Mobile operator codes in real use.
_PHONE_CODES = [
    "900", "901", "902", "903", "904", "905", "906", "908", "909",
    "910", "911", "912", "913", "914", "915", "916", "917", "919",
    "920", "921", "922", "923", "925", "926", "927", "929",
    "930", "931", "932", "936", "937", "938", "939",
    "950", "951", "952", "953", "958", "960", "961", "962", "963",
    "964", "965", "966", "967", "968", "969", "980", "981", "982",
    "983", "984", "985", "986", "987", "988", "989", "993", "995",
    "996", "997", "999",
]


def account_control_key(body: str, bik: str = BIK) -> int:
    """Return the Central Bank control digit for a 20-digit account.

    `body` is the account with a placeholder at position 9 (index 8).
    """
    for k in range(10):
        candidate = body[:8] + str(k) + body[9:]
        digits = bik[-3:] + candidate
        if sum(int(d) * w for d, w in zip(digits, _CK_WEIGHTS)) % 10 == 0:
            return k
    raise AlfaReceiptError(f"no valid control key for {body!r}")


def make_account(rng: random.Random = random) -> str:
    """Generate a 20-digit personal RUB account with a valid control key.

    40817 810 K 8048 NNNNNNN
      |    |  |  |    personal number
      |    |  |  branch (as observed on genuine Alfa receipts)
      |    |  control key
      |    currency 810 = RUB
      individual's current account
    """
    # Personal numbers are issued sequentially, so genuine accounts carry
    # leading zeros (the donor receipt shows …8048 0002476).
    roll = rng.random()
    if roll < 0.55:
        personal = f"{rng.randint(1000, 9999):07d}"      # 000NNNN
    elif roll < 0.90:
        personal = f"{rng.randint(10000, 99999):07d}"    # 00NNNNN
    else:
        personal = f"{rng.randint(100000, 999999):07d}"  # 0NNNNNN
    body = "40817810" + "0" + "8048" + personal
    key = account_control_key(body)
    return body[:8] + str(key) + body[9:]


def random_receipt_data(rng: random.Random = random,
                        within_hours: int = 48) -> dict:
    """Build a plausible random SBP receipt payload.

    The transfer timestamp always lands in the past (a receipt for a future
    transfer is an obvious forgery) and inside the window the SBP scheme
    table has data for.
    """
    if rng.random() < 0.72:
        name = rng.choice(_MALE_NAMES)
        patronymic = rng.choice(_MALE_PATRONYMICS)
    else:
        name = rng.choice(_FEMALE_NAMES)
        patronymic = rng.choice(_FEMALE_PATRONYMICS)
    recipient = f"{name} {patronymic} {rng.choice(_SURNAME_INITIALS)}"

    code = rng.choice(_PHONE_CODES)
    phone = (f"+7 ({code}) {rng.randint(100, 999)}-"
             f"{rng.randint(10, 99):02d}-{rng.randint(10, 99):02d}")

    # Transfer amounts people actually send: mostly round hundreds/thousands.
    roll = rng.random()
    if roll < 0.40:
        amount = rng.randrange(100, 5000, 50)
    elif roll < 0.75:
        amount = rng.randrange(1000, 30000, 100)
    elif roll < 0.95:
        amount = rng.randrange(5000, 90000, 10)
    else:
        amount = rng.randrange(100, 3000, 10)

    now = datetime.datetime.now(_MSK).replace(tzinfo=None)
    # Keep at least 5 minutes in the past so «Сформирована» has room.
    delta_min = rng.randint(15, max(16, within_hours * 60))
    transfer = (now - datetime.timedelta(minutes=delta_min)).replace(
        second=rng.randint(0, 59), microsecond=0)
    earliest = _SBP_SCHEME_BUILDS[0][0]
    if transfer.date() < earliest:
        transfer = datetime.datetime.combine(
            earliest, transfer.time())

    return {
        "amount":      amount,
        "phone":       phone,
        "transfer_dt": transfer.strftime("%d.%m.%Y %H:%M:%S"),
        "recipient":   recipient,
        "bank":        rng.choice(_BANKS),
        "account":     make_account(rng),
        "message":     "Перевод денежных средств",
    }


# ── determinism ───────────────────────────────────────────────────────────
def _make_deterministic(pdf: bytes, rng: random.Random) -> bytes:
    """Re-derive the two random fingerprints from the caller's seed.

    ``obi_patch_pdf`` fills /ID from ``os.urandom`` and picks the font subset
    tag with the global RNG, so the same payment would otherwise yield a
    different file on every call — and tapping the receipt in history has to
    open the same document every time.

    Both substitutions keep the byte length identical, so stream offsets and
    the xref table stay valid.
    """
    new_id = "".join(rng.choice("0123456789abcdef") for _ in range(32)).encode()
    m = re.search(rb"/ID\s*\[(?:\s*<[0-9A-Fa-f]+>\s*)+\]", pdf)
    if m:
        replacement = b"/ID [<" + new_id + b"><" + new_id + b">]"
        if len(replacement) == m.end() - m.start():
            pdf = pdf[:m.start()] + replacement + pdf[m.end():]
        else:
            logger.warning("determinism: /ID length differs, leaving as-is")

    tag = "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(6))
    pdf = re.sub(rb"/[A-Z]{6}\+Tahoma", b"/" + tag.encode() + b"+Tahoma", pdf)
    return pdf


# ── main builder ─────────────────────────────────────────────────────────
def build(data: dict,
          template: str = TEMPLATE,
          seed=None,
          meta: Optional[dict] = None) -> bytes:
    """Generate an Alfa SBP receipt PDF from *data*.

    Parameters
    ----------
    data:
        amount, phone, transfer_dt, recipient, bank, account, message
    template:
        path to the donor PDF (default: assets/template_sbp.pdf)
    seed:
        random seed for deterministic IDs (use payment id for idempotency)
    meta:
        optional dict populated with the generated identifiers
        (opid, sbp_id, formed_dt) — avoids re-deriving them from the seed.

    Returns
    -------
    bytes — complete PDF ready to serve.

    Raises
    ------
    AlfaReceiptError — on missing glyph or parse failure.
    """
    rng = random.Random(seed) if seed is not None else random

    # ── parse inputs ──────────────────────────────────────────────────────
    dt = _parse_dt(data.get("transfer_dt") or
                   datetime.datetime.now(_MSK).replace(tzinfo=None))

    amount_str  = _fmt_amount(data.get("amount", 0))
    phone_str   = str(data.get("phone", "")).replace(" ", "\xa0")
    recipient   = str(data.get("recipient", "")).replace(" ", "\xa0")
    bank        = str(data.get("bank", "Сбербанк"))

    # «Счёт списания» is the sender's own account, so it must stay identical
    # across every receipt of the same client — derive it from a stable seed
    # rather than the per-receipt one when the caller has no account on file.
    account = str(data.get("account") or "")
    if not account:
        account = make_account(random.Random(data.get("account_seed") or 0))
    message     = str(data.get("message", "Перевод\xa0денежных\xa0средств")).replace(" ", "\xa0")

    # Transfer dt string (field value format matches donor exactly)
    transfer_dt_str = dt.strftime("%d.%m.%Y\xa0%H:%M:%S\xa0мск") + "\xa0"

    # Сформирована = when the receipt was downloaded: after the transfer but
    # never in the future, or a checker comparing it to the clock rejects it.
    now_msk = datetime.datetime.now(_MSK).replace(tzinfo=None)
    # Receipt is issued at generation time. If the transfer is "now", stamp
    # «Сформирована» with the same clock — never a future minute.
    if dt >= now_msk - datetime.timedelta(minutes=1):
        formed_dt = now_msk
    else:
        formed_offset = rng.randint(2, 12)
        formed_dt = dt + datetime.timedelta(minutes=formed_offset)
        if formed_dt > now_msk:
            span = int((now_msk - dt).total_seconds() // 60)
            formed_dt = (dt + datetime.timedelta(minutes=rng.randint(2, span))
                         if span >= 2 else now_msk)
    formed_str    = formed_dt.strftime("%d.%m.%Y\xa0%H:%M\xa0мск")

    opid   = _make_opid(dt, rng)
    sbp_id = _make_sbp_id(dt, rng)

    if meta is not None:
        meta.update({
            "opid":        opid,
            "sbp_id":      sbp_id,
            "formed_dt":   formed_dt.strftime("%d.%m.%Y %H:%M мск"),
            "transfer_dt": dt.strftime("%d.%m.%Y %H:%M:%S мск"),
            "amount":      amount_str.strip(),
            "filename":    am_download_name(formed_dt, rng),
        })

    # ── load donor ───────────────────────────────────────────────────────
    donor, clean_font, fields = _load_donor()

    # Retrieve exact donor strings (NBSP-encoded)
    def _f(x, y): return fields.get((round(x, 2), round(y, 2)), "")

    old_formed  = _f(452.788, 779.15)
    old_amount  = _f(35.45,   664.288)
    old_date    = _f(35.45,   578.5)
    old_opid    = _f(35.45,   535.606)
    old_name    = _f(35.45,   492.712)
    old_phone   = _f(304.75,  664.288)
    old_bank    = _f(304.75,  621.394)
    old_account = _f(304.75,  578.5)
    old_sbp     = _f(304.75,  535.606)

    # Guard: check required glyphs exist in bank
    # obi_patch_pdf will extend automatically, but if bank is missing a glyph
    # we want a clear error rather than a broken PDF.
    _all_new = (formed_str + amount_str + transfer_dt_str + opid +
                recipient + phone_str + bank + account + sbp_id)
    bank_chars, _ = _op._harvest_glyph_bank()
    donor_cmap    = _op.extract_cmap_from_pdf(donor)
    _covered      = set(donor_cmap) | set(bank_chars)
    _missing = [c for c in _all_new if ord(c) > 0x001F and c not in _covered]
    if _missing:
        raise AlfaReceiptError(
            f"Missing glyphs for chars: {''.join(sorted(set(_missing)))}"
        )

    replacements = [
        {"field": "formed_date",  "old_value": old_formed,  "new_value": formed_str},
        {"field": "amount",       "old_value": old_amount,   "new_value": amount_str},
        {"field": "transfer_dt",  "old_value": old_date,     "new_value": transfer_dt_str},
        {"field": "opid",         "old_value": old_opid,     "new_value": opid + "\xa0"},
        {"field": "recipient",    "old_value": old_name,     "new_value": recipient + "\xa0"},
        {"field": "phone",        "old_value": old_phone,    "new_value": phone_str},
        {"field": "bank",         "old_value": old_bank,     "new_value": bank},
        {"field": "account",      "old_value": old_account,  "new_value": account},
        {"field": "sbp_id",       "old_value": old_sbp,      "new_value": sbp_id},
    ]

    logger.info(
        "alfa.build: amount=%s recipient=%r bank=%s dt=%s opid=%s",
        amount_str.strip(), recipient, bank, dt.strftime("%d.%m.%Y %H:%M:%S"), opid,
    )

    try:
        result = _op.obi_patch_pdf(donor, replacements, clean_font=clean_font)
    except Exception as exc:
        raise AlfaReceiptError(f"obi_patch_pdf failed: {exc}") from exc

    if seed is not None:
        result = _make_deterministic(result, rng)

    return result


# ── variant generator ────────────────────────────────────────────────────
def build_variants(data: dict,
                   count: int = 3,
                   template: str = TEMPLATE) -> list[dict]:
    """Generate *count* receipts with different random seeds.

    Returns list of::

        {
          "pdf":        bytes,
          "opid":       str,
          "sbp_id":     str,
          "formed_dt":  str,
          "seed":       int,
        }
    """
    results = []
    for i in range(count):
        seed = random.randint(0, 2**32)
        info: dict = {}
        try:
            pdf = build(data, template=template, seed=seed, meta=info)
        except AlfaReceiptError as exc:
            logger.warning("build_variants: variant %d failed: %s", i, exc)
            continue
        info["pdf"] = pdf
        info["seed"] = seed
        results.append(info)
    return results
