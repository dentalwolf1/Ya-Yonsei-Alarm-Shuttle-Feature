import os
import re
import json
import hmac
import hashlib
import logging
from datetime import datetime, timedelta, time, date

import pytz
import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.memory import MemoryJobStore

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Solapi credentials
# ---------------------------------------------------------------------------
SOLAPI_API_KEY    = os.getenv("SOLAPI_API_KEY", "")
SOLAPI_API_SECRET = os.getenv("SOLAPI_API_SECRET", "")
SOLAPI_SENDER     = os.getenv("SOLAPI_SENDER", "")

if not SOLAPI_API_KEY or not SOLAPI_API_SECRET or not SOLAPI_SENDER:
    logger.warning("Solapi credentials not fully set — SMS will fail.")
else:
    logger.info("Solapi SMS credentials loaded successfully")

# ---------------------------------------------------------------------------
# Shuttle config
# ---------------------------------------------------------------------------
KST = pytz.timezone("Asia/Seoul")

# Fixed shuttle departure time — set SHUTTLE_DEPARTURE_TIME=HH:MM in .env
_h, _m = map(int, os.getenv("SHUTTLE_DEPARTURE_TIME", "08:30").split(":"))
SHUTTLE_DEPARTURE = time(_h, _m)

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
scheduler = BackgroundScheduler(
    jobstores={"default": MemoryJobStore()},
    timezone=KST,
)
if not scheduler.running:
    scheduler.start()
    logger.info(
        "Scheduler started | server UTC: %s | KST: %s",
        datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M:%S"),
    )

# ---------------------------------------------------------------------------
# Phone book — persists across restarts via JSON file
# ---------------------------------------------------------------------------
PHONE_BOOK_FILE = os.path.join(os.path.dirname(__file__), "phone_book.json")


def _load_phone_book() -> dict:
    try:
        with open(PHONE_BOOK_FILE, "r") as f:
            data = json.load(f)
            logger.info("Phone book loaded: %d users", len(data))
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        logger.info("No existing phone book — starting fresh")
        return {}


def _save_phone_book() -> None:
    try:
        with open(PHONE_BOOK_FILE, "w") as f:
            json.dump(user_phone_book, f)
        logger.info("Phone book saved: %d users", len(user_phone_book))
    except Exception as e:
        logger.error("Failed to save phone book: %s", e)


user_phone_book: dict = _load_phone_book()

# ---------------------------------------------------------------------------
# Kakao Open Builder response helper
# ---------------------------------------------------------------------------

def send_text_response(text: str) -> dict:
    return {
        "version": "2.0",
        "template": {"outputs": [{"simpleText": {"text": text}}]},
    }

# ---------------------------------------------------------------------------
# Solapi SMS (raw HMAC-SHA256 auth — no SDK needed)
# ---------------------------------------------------------------------------

def _solapi_auth_header() -> dict:
    now     = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    salt    = os.urandom(16).hex()
    message = now + salt
    sig     = hmac.new(
        SOLAPI_API_SECRET.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        "Authorization": (
            f"HMAC-SHA256 apiKey={SOLAPI_API_KEY}, "
            f"date={now}, salt={salt}, signature={sig}"
        ),
        "Content-Type": "application/json",
    }


def send_sms(phone_number: str, text: str) -> None:
    url     = "https://api.solapi.com/messages/v4/send"
    headers = _solapi_auth_header()
    payload = {
        "message": {
            "to":   phone_number,
            "from": SOLAPI_SENDER,
            "text": text,
            "type": "SMS",
        }
    }
    logger.info("Sending SMS to %s", phone_number)
    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    if resp.status_code not in (200, 201):
        logger.error("SMS FAILED | phone: %s | status: %s | response: %s",
                     phone_number, resp.status_code, resp.text)
    else:
        logger.info("SMS SENT | phone: %s | time: %s KST",
                    phone_number,
                    datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M:%S"))

# ---------------------------------------------------------------------------
# Scheduling — reservation-open alert + two departure alerts
# ---------------------------------------------------------------------------

def schedule_shuttle_alerts(phone_number: str, intended_date: date) -> list:
    now_kst    = datetime.now(tz=KST)
    dep_dt     = KST.localize(datetime.combine(intended_date, SHUTTLE_DEPARTURE))
    res_open_dt = KST.localize(
        datetime.combine(intended_date - timedelta(days=2), time(14, 0))
    )
    date_label = intended_date.strftime("%m월 %d일")
    scheduled  = []

    jobs = [
        (
            f"{phone_number}_{intended_date}_reservation_open",
            res_open_dt,
            f"[셔틀 알림] 오늘 14시부터 {date_label} 셔틀 예약이 시작됩니다. 지금 바로 예약하세요!",
            "예약 오픈 알림",
        ),
        (
            f"{phone_number}_{intended_date}_10min",
            dep_dt - timedelta(minutes=10),
            f"[셔틀 알림] {date_label} 셔틀 출발 10분 전입니다. 탑승 준비해주세요.",
            "출발 10분 전",
        ),
        (
            f"{phone_number}_{intended_date}_5min",
            dep_dt - timedelta(minutes=5),
            f"[셔틀 알림] {date_label} 셔틀 출발 5분 전입니다. 탑승 준비해주세요.",
            "출발 5분 전",
        ),
    ]

    for job_id, fire_at, message, label in jobs:
        if fire_at <= now_kst:
            logger.info("Skipping '%s' — already past (%s)", label, fire_at)
            continue
        scheduler.add_job(
            send_sms,
            trigger="date",
            run_date=fire_at,
            args=[phone_number, message],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=120,
        )
        scheduled.append((label, fire_at.strftime("%m/%d %H:%M")))
        logger.info("Scheduled | %s | %s KST", label, fire_at.strftime("%Y-%m-%d %H:%M"))

    return scheduled

# ---------------------------------------------------------------------------
# Input parsers
# ---------------------------------------------------------------------------

# First-time registration: 01012345678
PHONE_ONLY_PATTERN = re.compile(r'^(01\d{8,9})\s*$')

# With phone + date: 01012345678 06/10  or  01012345678 6월10일
WITH_PHONE_PATTERN = re.compile(
    r'^(01\d{8,9})\s+(\d{1,2})[/월](\d{1,2})일?\s*$'
)

# Returning user sends date only: 06/10  or  6월10일  or  6월 10일
NO_PHONE_PATTERN = re.compile(
    r'^(\d{1,2})[/월]\s*(\d{1,2})일?\s*$'
)

REGISTER_HELP = (
    "처음 사용하시는군요! 📱\n"
    "먼저 전화번호를 등록해 주세요.\n\n"
    "전화번호만 입력:\n"
    "예시: 01012345678\n\n"
    "또는 전화번호와 함께 바로 날짜 설정:\n"
    "예시: 01012345678 06/10"
)

FORMAT_HELP = (
    "셔틀을 예약할 날짜를 입력해 주세요.\n\n"
    "입력 형식: 월/일\n"
    "예시: 06/10\n"
    "예시: 6월10일\n\n"
    "• 출발 2일 전 14:00에 예약 오픈 SMS\n"
    "• 출발 10분 전, 5분 전에 탑승 안내 SMS"
)


def parse_intended_date(month: int, day: int) -> date | None:
    now_kst = datetime.now(tz=KST).date()
    year    = now_kst.year
    try:
        d = date(year, month, day)
    except ValueError:
        return None
    if d <= now_kst:
        try:
            d = date(year + 1, month, day)
        except ValueError:
            return None
    return d

# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

@app.route("/webhook", methods=["POST"])
def webhook():
    body         = request.get_json(silent=True) or {}
    user_request = body.get("userRequest", {})
    user_obj     = user_request.get("user", {})
    user_id      = (
        user_obj.get("id") or
        user_obj.get("userId") or
        user_obj.get("key") or
        body.get("bot", {}).get("id", "")
    )
    user_id   = str(user_id).strip()
    utterance = user_request.get("utterance", "").strip()

    logger.info("Webhook | user: %s | utterance: %s", user_id, utterance)

    if not utterance:
        return jsonify(send_text_response(FORMAT_HELP))

    # ── Case 1: phone number only → register ────────────────────────────────
    phone_only = PHONE_ONLY_PATTERN.match(utterance)
    if phone_only:
        phone = phone_only.group(1)
        user_phone_book[user_id] = phone
        _save_phone_book()
        logger.info("Registered phone %s for user %s", phone, user_id)
        return jsonify(send_text_response(
            f"✅ 전화번호 {phone} 가 등록되었습니다!\n\n"
            "이제 셔틀 날짜를 입력해 주세요.\n"
            "입력 형식: 월/일\n"
            "예시: 06/10"
        ))

    # ── Case 2: phone + date (first use with date) ───────────────────────────
    with_phone = WITH_PHONE_PATTERN.match(utterance)
    if with_phone:
        phone  = with_phone.group(1)
        month  = int(with_phone.group(2))
        day    = int(with_phone.group(3))

        user_phone_book[user_id] = phone
        _save_phone_book()
        logger.info("Registered phone %s for user %s", phone, user_id)

        intended_date = parse_intended_date(month, day)
        if intended_date is None:
            return jsonify(send_text_response("⚠️ 날짜가 올바르지 않습니다.\n" + FORMAT_HELP))

        scheduled = schedule_shuttle_alerts(phone, intended_date)
        return _confirmation_response(intended_date, scheduled, phone)

    # ── Case 3: returning user — date only ──────────────────────────────────
    no_phone = NO_PHONE_PATTERN.match(utterance)
    if no_phone:
        if not user_id or user_id not in user_phone_book:
            logger.info("No phone for user_id: '%s'", user_id)
            return jsonify(send_text_response(REGISTER_HELP))

        phone  = user_phone_book[user_id]
        month  = int(no_phone.group(1))
        day    = int(no_phone.group(2))

        intended_date = parse_intended_date(month, day)
        if intended_date is None:
            return jsonify(send_text_response("⚠️ 날짜가 올바르지 않습니다.\n" + FORMAT_HELP))

        scheduled = schedule_shuttle_alerts(phone, intended_date)
        return _confirmation_response(intended_date, scheduled, phone)

    # ── No pattern matched ───────────────────────────────────────────────────
    if user_id and user_id in user_phone_book:
        return jsonify(send_text_response("⚠️ 입력 형식이 올바르지 않습니다.\n\n" + FORMAT_HELP))
    return jsonify(send_text_response(REGISTER_HELP))


def _confirmation_response(intended_date: date, scheduled: list, phone: str):
    dep_time_str = SHUTTLE_DEPARTURE.strftime("%H:%M")
    date_str     = intended_date.strftime("%m월 %d일")

    if not scheduled:
        return jsonify(send_text_response(
            f"⚠️ {date_str} 셔틀 알림을 설정할 수 없습니다.\n"
            "모든 알림 시간이 이미 지났습니다.\n"
            "더 이후 날짜를 입력해 주세요."
        ))

    lines = "\n".join(f"  • {label}: {t} KST" for label, t in scheduled)
    return jsonify(send_text_response(
        f"✅ 셔틀 알림이 설정되었습니다!\n"
        f"📅 탑승 날짜: {date_str}\n"
        f"🕐 출발 시간: {dep_time_str} KST\n"
        f"📱 SMS 수신: {phone}\n"
        f"🔔 알림 일정:\n{lines}"
    ))

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    now_kst = datetime.now(tz=KST)
    jobs = [
        {
            "id": j.id,
            "next_run_kst": j.next_run_time.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST")
            if j.next_run_time else None,
        }
        for j in scheduler.get_jobs()
    ]
    return jsonify({
        "status":               "ok",
        "server_time_kst":      now_kst.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "shuttle_departure":    SHUTTLE_DEPARTURE.strftime("%H:%M"),
        "sms_ready":            bool(SOLAPI_API_KEY and SOLAPI_API_SECRET and SOLAPI_SENDER),
        "registered_users":     len(user_phone_book),
        "scheduled_jobs_count": len(jobs),
        "scheduled_jobs":       jobs,
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
