import os
import re
import json
import hmac
import hashlib
import logging
import threading
import time as _time
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

SOLAPI_API_KEY = os.getenv("SOLAPI_API_KEY", "")
SOLAPI_API_SECRET = os.getenv("SOLAPI_API_SECRET", "")
SOLAPI_SENDER = os.getenv("SOLAPI_SENDER", "")

if not SOLAPI_API_KEY or not SOLAPI_API_SECRET or not SOLAPI_SENDER:
    logger.warning("Solapi credentials not fully set — SMS will fail.")
else:
    logger.info("Solapi SMS credentials loaded successfully")

KST = pytz.timezone("Asia/Seoul")

scheduler = BackgroundScheduler(
    jobstores={"default": MemoryJobStore()},
    timezone=KST,
)
if not scheduler.running:
    scheduler.start()
    logger.info("Scheduler started (KST)")

APP_URL = os.getenv("APP_URL", "https://ya-yonsei-alarm-shuttle-feature.onrender.com")

def _keep_alive():
    while True:
        _time.sleep(600)
        try:
            requests.get(f"{APP_URL}/health", timeout=10)
            logger.info("Keep-alive ping sent")
        except Exception as e:
            logger.warning("Keep-alive ping failed: %s", e)

threading.Thread(target=_keep_alive, daemon=True).start()

def send_text_response(text: str) -> dict:
    return {
        "version": "2.0",
        "template": {"outputs": [{"simpleText": {"text": text}}]},
    }

def get_user_id(body: dict) -> str:
    user_request = body.get("userRequest", {})
    user_obj = user_request.get("user", {})
    user_id = (
        user_obj.get("id") or
        user_obj.get("userId") or
        user_obj.get("key") or
        body.get("bot", {}).get("id", "")
    )
    return str(user_id).strip()

def _solapi_auth_header() -> dict:
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    salt = os.urandom(16).hex()
    message = now + salt
    sig = hmac.new(
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
    url = "https://api.solapi.com/messages/v4/send"
    headers = _solapi_auth_header()
    payload = {
        "message": {
            "to": phone_number,
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

def schedule_shuttle_alerts(phone: str, intended_date: date) -> list:
    now_kst = datetime.now(tz=KST)
    alert_date = intended_date - timedelta(days=2)
    date_label = intended_date.strftime("%m월 %d일")
    scheduled = []
    jobs = [
        (
            f"shuttle_{phone}_{intended_date}_1350",
            KST.localize(datetime.combine(alert_date, time(13, 50))),
            f"[셔틀 알림] {date_label} 셔틀 예약이 10분 후 시작됩니다!",
            "13:50 알림",
        ),
        (
            f"shuttle_{phone}_{intended_date}_1355",
            KST.localize(datetime.combine(alert_date, time(13, 55))),
            f"[셔틀 알림] {date_label} 셔틀 예약이 5분 후 시작됩니다!",
            "13:55 알림",
        ),
    ]
    for job_id, fire_at, message, label in jobs:
        if fire_at <= now_kst:
            logger.info("Skipping '%s' — already past", label)
            continue
        scheduler.add_job(
            send_sms, trigger="date", run_date=fire_at,
            args=[phone, message], id=job_id,
            replace_existing=True, misfire_grace_time=120,
        )
        scheduled.append((label, fire_at.strftime("%m/%d %H:%M")))
        logger.info("Shuttle alert scheduled | %s | %s KST",
                    label, fire_at.strftime("%Y-%m-%d %H:%M"))
    return scheduled

# ---------------------------------------------------------------------------
# ★ INPUT FORMAT (must start with "셔틀 ")
#
#   셔틀 01012345678 06/10
# ---------------------------------------------------------------------------

KEYWORD = "셔틀"

FORMAT_HELP = (
    "🚌 셔틀 알림 설정 방법:\n\n"
    "  셔틀 전화번호 월/일\n\n"
    "예시:\n"
    "  셔틀 01012345678 06/10\n"
    "  셔틀 01012345678 6월10일\n\n"
    "• 탑승일 2일 전 13:50, 13:55에 예약 알림 SMS 발송"
)

# 셔틀 01012345678 06/10  or  셔틀 01012345678 6월10일
INPUT_PATTERN = re.compile(
    r'^셔틀\s+(01\d{8,9})\s+(\d{1,2})[/월]\s*(\d{1,2})일?\s*$'
)

def parse_shuttle_date(month: int, day: int) -> date | None:
    now_kst = datetime.now(tz=KST).date()
    year = now_kst.year
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

@app.route("/webhook", methods=["POST"])
def webhook():
    body = request.get_json(silent=True) or {}
    user_id = get_user_id(body)
    utterance = body.get("userRequest", {}).get("utterance", "").strip()
    logger.info("Webhook | user: %s | utterance: %s", user_id, utterance)

    if not utterance.startswith(KEYWORD):
        return jsonify(send_text_response(FORMAT_HELP))

    m = INPUT_PATTERN.match(utterance)
    if not m:
        return jsonify(send_text_response("⚠️ 입력 형식이 올바르지 않습니다.\n\n" + FORMAT_HELP))

    phone = m.group(1)
    month = int(m.group(2))
    day = int(m.group(3))
    intended_date = parse_shuttle_date(month, day)

    if intended_date is None:
        return jsonify(send_text_response("⚠️ 날짜가 올바르지 않습니다.\n\n" + FORMAT_HELP))

    scheduled = schedule_shuttle_alerts(phone, intended_date)
    date_str = intended_date.strftime("%m월 %d일")

    if not scheduled:
        return jsonify(send_text_response(
            f"⚠️ {date_str} 셔틀 알림을 설정할 수 없습니다.\n"
            "모든 알림 시간이 이미 지났습니다.\n"
            "더 이후 날짜를 입력해 주세요."
        ))

    lines = "\n".join(f" • {label}: {t} KST" for label, t in scheduled)
    return jsonify(send_text_response(
        f"✅ 셔틀 알림이 설정되었습니다!\n"
        f"📅 탑승 날짜: {date_str}\n"
        f"📱 SMS 수신: {phone}\n"
        f"🔔 알림 일정:\n{lines}"
    ))

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
        "status": "ok",
        "server_time_kst": now_kst.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "sms_ready": bool(SOLAPI_API_KEY and SOLAPI_API_SECRET and SOLAPI_SENDER),
        "scheduled_jobs_count": len(jobs),
        "scheduled_jobs": jobs,
    })

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
