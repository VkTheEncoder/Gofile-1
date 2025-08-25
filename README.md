# Telegram → GoFile Uploader (Multi‑Account Auto‑Rotation)

This Telegram bot uploads any file you send to **GoFile** and returns the share link.
It supports **multiple GoFile accounts** and **auto-rotates** when an account’s **monthly traffic** is exhausted.

> ✅ Works out of the box with long‑polling.  
> ⚙️ Optional webhook mode is included for Render/Heroku (set `USE_WEBHOOK=true`).

---

## Features
- Upload Telegram documents / videos / audio / photos to GoFile
- Pool of GoFile tokens (free or premium)
- **Auto-rotation** across tokens when monthly traffic is depleted
- `/stats` shows current account & usage
- Clean async implementation with `python-telegram-bot` v20+ and `aiohttp`
- Safe config via `.env` (no tokens in code)

---

## Quick Start (Local / Polling)

1. **Python 3.10+** recommended.
2. Create `.env` in project root:
   ```env
   TELEGRAM_BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
   GOFILE_TOKENS=tokenA,tokenB,tokenC
   # Optional:
   DOWNLOAD_DIR=downloads
   LOG_LEVEL=INFO
   ```
3. Install deps:
   ```bash
   pip install -r requirements.txt
   ```
4. Run:
   ```bash
   python -m app.bot
   ```

Send any file to your bot — you’ll receive a GoFile link back.

---

## Deploy (Render/Heroku/Docker)

### Render (Polling)
- **Start Command**: `python -m app.bot`
- Add environment variables from `.env` above.
- Use a Background Worker service (recommended) or Web Service (no public HTTP needed for polling).

### Render (Webhook)
1. Set env vars:
   ```env
   USE_WEBHOOK=true
   WEBHOOK_URL=https://your-service.onrender.com/webhook
   PORT=10000               # Render provides $PORT automatically
   ```
2. **Start Command**: `python -m app.webhook`
3. Expose HTTP port in Render Web Service.

### Docker
```bash
docker build -t tg-gofile-rotator .
docker run -it --rm --env-file .env tg-gofile-rotator
```

---

## Commands
- `/start` — hello and how-to
- `/help` — mini help
- `/stats` — active account + usage (if API provides usage fields)

---

## Notes on GoFile API
- Official API docs (May 16, 2025) show the **global upload endpoint**: `https://upload.gofile.io/uploadfile` with header `Authorization: Bearer <TOKEN>`.
- Account endpoints: `GET /accounts/getid` and `GET /accounts/{accountId}` (used to fetch usage and root info).  
  The API is **BETA** and field names may change — this bot parses usage fields defensively.

---

## Project Structure
```
app/
  __init__.py
  bot.py               # polling entrypoint
  webhook.py           # optional webhook server entrypoint
  config.py            # environment config
  gofile_api.py        # GoFile client
  account_pool.py      # token rotation logic
  handlers.py          # Telegram handlers
requirements.txt
Dockerfile
Procfile
render.yaml           # optional Render spec (webhook)
.env.example
```

---

## Security
- **Never** commit real tokens. Use `.env`.
- The bot deletes downloaded temp files after upload.
- You can control `DOWNLOAD_DIR` via env (defaults to `downloads`).

---

## License
MIT
