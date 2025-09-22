# webhook_app.py
import os
from fastapi import FastAPI, Request, HTTPException
from aiogram.types import Update

# On réutilise le bot et le dispatcher définis dans main.py
from main import bot, dp, BOT_TOKEN

app = FastAPI()

# Secret du chemin webhook (évite les scans)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET") or BOT_TOKEN  # mets un secret dans Render

@app.get("/")
async def health():
    return {"ok": True}

@app.post(f"/webhook/{WEBHOOK_SECRET}")
async def telegram_webhook(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Bad JSON")
    # Aiogram v3 / Pydantic v2
    update = Update.model_validate(payload)
    await dp.feed_update(bot, update)
    return {"ok": True}
