# YA! Yonsei Alarm — Shuttle Bot

Sends SMS reminders at 13:50 and 13:55, two days before the user's intended shuttle ride date.

---

## Files

| File | Purpose |
|---|---|
| `app.py` | Main server — handles messages and schedules SMS alerts |
| `requirements.txt` | Python libraries to install |
| `Procfile` | Tells Render how to start the app |
| `.env` | Your secret credentials (never upload to GitHub) |
| `.gitignore` | Keeps .env and other files off GitHub |

---

## User Flow

**First time:**
```
User: 01012345678
Bot:  ✅ 전화번호 등록되었습니다! 날짜만 입력하세요 😊
```

**Every time after:**
```
User: 06/10
Bot:  ✅ 셔틀 알림이 설정되었습니다!
      📅 탑승 날짜: 06월 10일
      📱 SMS 수신: 01012345678
      🔔 알림 일정:
        • 13:50 알림: 06/08 13:50 KST
        • 13:55 알림: 06/08 13:55 KST
```

---

## Render Setup

### Build Command
```
pip install -r requirements.txt
```

### Start Command
```
python app.py
```

### Environment Variables
| Key | Value |
|---|---|
| `SOLAPI_API_KEY` | from Solapi dashboard |
| `SOLAPI_API_SECRET` | from Solapi dashboard |
| `SOLAPI_SENDER` | your registered sender number |
| `APP_URL` | `https://ya-yonsei-alarm-shuttle-feature.onrender.com` |

---

## Kakao Open Builder Skill URL
```
https://ya-yonsei-alarm-shuttle-feature.onrender.com/webhook
```

---

## Upload to GitHub
Upload only these 3 files — never upload .env:
- app.py
- requirements.txt
- Procfile
