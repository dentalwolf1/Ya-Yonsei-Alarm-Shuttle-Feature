from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from datetime import date, datetime, time, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.memory import MemoryJobStore
import solapi
import pytz
import sqlite3
import re
import os
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

KST = pytz.timezone("Asia/Seoul")
SOLAPI_API_KEY    = os.environ["SOLAPI_API_KEY"]
SOLAPI_API_SECRET = os.environ["SOLAPI_API_SECRET"]
SENDER_PHONE      = os.environ["SENDER_PHONE"]
DB_PATH           = os.environ.get("DB_PATH", "users.db")

# Fixed shuttle departure time — set via env var (HH:MM, 24-hour KST)
_dep_h, _dep_m    = map(int, os.environ.get("SHUTTLE_DEPARTURE_TIME", "08:30").split(":"))
SHUTTLE_DEPARTURE = time(_dep_h, _dep_m)

# ── App & scheduler ───────────────────────────────────────────────────────────

app = FastAPI(title="Kakao Shuttle Alert Bot")
scheduler = BackgroundScheduler(jobstores={"default": MemoryJobStore()})
scheduler.start()

# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                kakao_user_id TEXT PRIMARY KEY,
                phone         TEXT NOT NULL
            )
        """)
        conn.commit()

init_db()

def lookup_phone(kakao_user_id: str) -> str | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT phone FROM users WHERE kakao_user_id = ?", (kakao_user_id,)
        ).fetchone()
    return row["phone"] if row else None

# ── SMS ───────────────────────────────────────────────────────────────────────

def send_sms(phone: str, message: str, job_label: str):
    try:
        client = solapi.SolapiMessageService(SOLAPI_API_KEY, SOLAPI_API_SECRET)
        client.send({"to": phone, "from": SENDER_PHONE, "text": message})
        logger.info(f"[SMS] {job_label} → {phone}")
    except Exception as e:
        logger.error(f"[SMS] {job_label} failed: {e}")

# ── Scheduling ────────────────────────────────────────────────────────────────

def schedule_alerts(kakao_user_id: str, phone: str, intended_date: date):
    departure_dt = KST.localize(datetime.combine(intended_date, SHUTTLE_DEPARTURE))
    reservation_open_dt = KST.localize(
        datetime.combine(intended_date - timedelta(days=2), time(14, 0))
    )
    now = datetime.now(KST)
    date_label = intended_date.strftime("%m월 %d일")
    uid = f"{kakao_user_id}_{intended_date.isoformat()}"

    jobs = [
        (
            f"{uid}_reservation_open",
            reservation_open_dt,
            f"오늘 14시부터 {date_label} 셔틀 예약이 시작됩니다. 지금 바로 예약하세요!",
        ),
        (
            f"{uid}_10min",
            departure_dt - timedelta(minutes=10),
            f"{date_label} 셔틀 출발 10분 전입니다. 탑승 준비해주세요.",
        ),
        (
            f"{uid}_5min",
            departure_dt - timedelta(minutes=5),
            f"{date_label} 셔틀 출발 5분 전입니다. 탑승 준비해주세요.",
        ),
    ]

    scheduled = []
    for job_id, run_at, msg in jobs:
        if run_at > now:
            scheduler.add_job(
                send_sms,
                trigger="date",
                run_date=run_at,
                args=[phone, msg, job_id],
                id=job_id,
                replace_existing=True,
            )
            scheduled.append((job_id, run_at))
            logger.info(f"Scheduled {job_id} at {run_at}")
        else:
            logger.warning(f"Skipped {job_id} — time already passed ({run_at})")

    return departure_dt, scheduled

# ── Date parsing ──────────────────────────────────────────────────────────────

def parse_date(text: str) -> date | None:
    text = text.strip()
    today = datetime.now(KST).date()

    # YYYY-MM-DD or YYYY/MM/DD
    m = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", text)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # MM-DD or MM/DD (assume current year, roll over if past)
    m = re.search(r"(\d{1,2})[/-](\d{1,2})", text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        d = date(today.year, month, day)
        return d if d >= today else date(today.year + 1, month, day)

    # Korean: M월 D일
    m = re.search(r"(\d{1,2})월\s*(\d{1,2})일", text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        d = date(today.year, month, day)
        return d if d >= today else date(today.year + 1, month, day)

    return None

# ── Kakao response helpers ────────────────────────────────────────────────────

def kakao_text(text: str) -> dict:
    return {
        "version": "2.0",
        "template": {"outputs": [{"simpleText": {"text": text}}]},
    }

# ── Kakao webhook ─────────────────────────────────────────────────────────────

@app.post("/kakao/webhook")
async def kakao_webhook(request: Request):
    body = await request.json()

    user_id  = body["userRequest"]["user"]["id"]
    utterance = body["userRequest"]["utterance"].strip()

    phone = lookup_phone(user_id)
    if not phone:
        return JSONResponse(kakao_text(
            "등록된 전화번호가 없습니다. 관리자에게 문의해주세요."
        ))

    intended_date = parse_date(utterance)
    if not intended_date:
        return JSONResponse(kakao_text(
            "날짜를 인식하지 못했습니다.\n"
            "예시: 2026-06-10 / 6월 10일 / 6/10"
        ))

    today = datetime.now(KST).date()
    if intended_date <= today:
        return JSONResponse(kakao_text("오늘 이후 날짜를 입력해주세요."))

    departure_dt, scheduled = schedule_alerts(user_id, phone, intended_date)
    dep_time_str = departure_dt.strftime("%H:%M")
    date_str = intended_date.strftime("%Y년 %m월 %d일")

    if not scheduled:
        return JSONResponse(kakao_text(
            f"{date_str} 셔틀({dep_time_str})은 알림을 보내기에 너무 임박했습니다."
        ))

    return JSONResponse(kakao_text(
        f"✅ {date_str} 셔틀 예약 알림이 설정되었습니다!\n\n"
        f"• 출발 시간: {dep_time_str}\n"
        f"• 예약 오픈 알림: 출발 2일 전 14:00\n"
        f"• 탑승 알림: 출발 10분 전, 5분 전\n\n"
        f"등록된 번호({phone[:3]}****{phone[-4:]})로 SMS를 보내드립니다."
    ))

# ── Admin: register / update user phone ──────────────────────────────────────

class UserRegistration(BaseModel):
    kakao_user_id: str
    phone: str

@app.post("/admin/users")
def register_user(reg: UserRegistration):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO users (kakao_user_id, phone) VALUES (?, ?)"
            " ON CONFLICT(kakao_user_id) DO UPDATE SET phone = excluded.phone",
            (reg.kakao_user_id, reg.phone),
        )
        conn.commit()
    return {"status": "ok", "kakao_user_id": reg.kakao_user_id}

@app.get("/health")
def health():
    return {"status": "ok"}
