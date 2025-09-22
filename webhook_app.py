# webhook_app.py
import os
import logging
from fastapi import FastAPI, Request, HTTPException
from aiogram.types import Update

# On réutilise le bot et le dispatcher définis dans main.py (aiogram v3)
from main import bot, dp, BOT_TOKEN

app = FastAPI(title="Telegram Bot on Render")

# Secret pour le chemin du webhook (ne PAS exposer)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET") or BOT_TOKEN  # recommandé: définir WEBHOOK_SECRET dans Render

# --- Health checks / Keep alive ---

@app.get("/")
async def root():
    # Route de base, OK pour Render
    return {"ok": True, "service": "telegram-bot"}

@app.get("/ping")
async def ping():
    # Route à utiliser dans UptimeRobot
    return {"status": "ok"}

@app.head("/ping")
async def ping_head():
    # Certains moniteurs utilisent HEAD ; on renvoie 200
    return ""

# --- Webhook Telegram (chemin secret) ---

@app.post(f"/webhook/{WEBHOOK_SECRET}")
async def telegram_webhook(request: Request):
    try:
        payload = await request.json()
    except Exception:
        # JSON invalide → 400
        raise HTTPException(status_code=400, detail="Bad JSON")

    try:
        update = Update.model_validate(payload)  # Pydantic v2 (aiogram v3)
        await dp.feed_update(bot, update)
    except Exception as e:
        logging.exception("Erreur pendant le traitement du webhook: %s", e)
        # On répond 200 pour éviter que Telegram réessaye en boucle si l'erreur est côté logique
        return {"ok": False, "error": "internal"}

    return {"ok": True}
