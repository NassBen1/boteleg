# webhook_app.py
import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from aiogram.types import Update

from main import bot, dp, BOT_TOKEN

# --------- Config ---------
# Secret pour sécuriser le chemin ET le header Telegram
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET") or BOT_TOKEN

# Base publique du service (ex: https://ton-bot.onrender.com)
# Render expose souvent RENDER_EXTERNAL_URL. Sinon, définis WEBHOOK_BASE manuellement.
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE") or os.getenv("RENDER_EXTERNAL_URL")
if not WEBHOOK_BASE:
    # On peut démarrer sans, mais le set_webhook échouera. Message clair dans les logs.
    logging.warning("WEBHOOK_BASE/RENDER_EXTERNAL_URL absent : le set_webhook ne pourra pas se faire.")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{WEBHOOK_BASE}{WEBHOOK_PATH}" if WEBHOOK_BASE else None

# --------- App & lifecycle ---------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Nettoie tout webhook précédent puis installe le nouveau
    if WEBHOOK_URL:
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            await bot.set_webhook(
                url=WEBHOOK_URL,
                secret_token=WEBHOOK_SECRET,  # Telegram enverra le header X-Telegram-Bot-Api-Secret-Token
                allowed_updates=list(dp.resolve_used_update_types()),
            )
            logging.info(f"✅ Webhook installé: {WEBHOOK_URL}")
        except Exception as e:
            logging.exception(f"❌ set_webhook a échoué: {e}")
    else:
        logging.warning("⏭️ set_webhook ignoré (pas d'URL publique). Définis WEBHOOK_BASE.")

    yield

    # Optionnel : laisser le webhook en place à l’arrêt
    # await bot.delete_webhook(drop_pending_updates=False)

app = FastAPI(title="Telegram Bot (Webhook)", lifespan=lifespan)

# --------- Health / keep-alive ---------
@app.get("/")
async def root_get():
    return {"ok": True, "service": "telegram-bot"}

@app.head("/")
async def root_head():
    return ""

@app.get("/ping")
async def ping_get():
    return {"status": "ok"}

@app.head("/ping")
async def ping_head():
    return ""

@app.options("/ping")
async def ping_options():
    return ""

# --------- Webhook Telegram ---------
@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    # (Optionnel) Vérifier le header secret si Telegram l'envoie
    token_hdr = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if token_hdr and token_hdr != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Bad secret header")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Bad JSON")

    try:
        update = Update.model_validate(payload)  # aiogram v3 / pydantic v2
        await dp.feed_update(bot, update)
    except Exception as e:
        logging.exception("Erreur pendant le traitement du webhook: %s", e)
        # éviter les retries agressifs côté Telegram
        return {"ok": False}

    return {"ok": True}
