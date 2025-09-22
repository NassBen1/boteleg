# main.py — version PayPal.me
import os, asyncio, time, urllib.parse
from pathlib import Path
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, InputMediaPhoto
)
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv

from sheets import list_categories, list_products, get_product, get_image_for, append_order
from models import carts, add_to_cart, remove_from_cart, empty_cart, cart_total_cents

# ----------- .env -----------
load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN manquant (ajoute-le dans .env)")

def _parse_admins(env_val: str):
    ids = []
    for x in (env_val or "").split(","):
        x = x.strip()
        if not x:
            continue
        try:
            ids.append(int(x))
        except ValueError:
            pass
    return ids

ADMINS = _parse_admins(os.getenv("ADMINS", ""))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "").lstrip("@").strip()
SUPPORT_URL_ENV = os.getenv("SUPPORT_URL", "").strip()

# --- PayPal.me ---
PAYPAL_ME = os.getenv("PAYPAL_ME", "").strip().replace("https://paypal.me/", "").replace("paypal.me/", "").lstrip("/")
# (doit être rempli)
if not PAYPAL_ME:
    print("[WARN] PAYPAL_ME est vide. Configure-le dans .env pour activer le paiement.")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

PAGE_SIZE = 4

# ----- États -----
# _stage: "name" -> "phone" -> "address"
user_checkout = {}          # uid -> {"_active": True, "_stage": "...", "name","phone","address"}
checkout_prompt = {}        # uid -> "name" | "phone" | "address"
manual_size_wait = {}       # uid -> {"pid":..., "color":...}
custom_model_wait = {}      # uid -> {"file_id": "...", "caption": "..."}

# ---------------------------- Utils ----------------------------

def money(cents: int) -> str:
    return f"{cents/100:.2f} €"

def support_url() -> str | None:
    if SUPPORT_URL_ENV:
        return SUPPORT_URL_ENV
    if ADMIN_USERNAME:
        return f"https://t.me/{ADMIN_USERNAME}"
    for aid in ADMINS:
        if aid > 0:
            return f"tg://user?id={aid}"
    return None

def kb_support_row():
    url = support_url()
    if url:
        return [InlineKeyboardButton(text="🆘 Besoin d’aide", url=url)]
    return [InlineKeyboardButton(text="🆘 Besoin d’aide", callback_data="help")]

def cat_kb():
    cats = list_categories()
    rows = [[InlineKeyboardButton(text=c, callback_data=f"cat:{c}:0")] for c in cats]
    rows.append([InlineKeyboardButton(text="Tout voir", callback_data="cat::0")])
    rows.append([InlineKeyboardButton(text="📦 Panier", callback_data="cart:view")])
    rows.append([InlineKeyboardButton(text="📸 Envoyer une photo de modèle", callback_data="custom:askphoto")])
    rows.append(kb_support_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def safe_edit(cb_or_msg, text, reply_markup=None, parse_mode="Markdown"):
    if isinstance(cb_or_msg, Message):
        await cb_or_msg.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)
        return
    m = cb_or_msg.message
    try:
        if m.content_type in ("photo", "video", "animation", "document"):
            await m.edit_caption(caption=text, parse_mode=parse_mode, reply_markup=reply_markup)
        else:
            await m.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except TelegramBadRequest:
        await m.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)

def post_add_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Continuer les achats", callback_data="browse")],
        [InlineKeyboardButton(text="📦 Voir panier", callback_data="cart:view"),
         InlineKeyboardButton(text="✅ Commander", callback_data="checkout:start")],
        kb_support_row()
    ])

# --------- PayPal.me ----------
def paypal_link(order_id: int, total_cents: int) -> str | None:
    """
    Lien PayPal.me avec montant pré-rempli.
    (Le type « Entre proches » ne peut pas être imposé par URL ; le client le choisit dans l'app si disponible.)
    """
    if not PAYPAL_ME:
        return None
    amount = f"{total_cents/100:.2f}"  # "12.34"
    return f"https://www.paypal.me/{PAYPAL_ME}/{amount}"

def payment_kb(order_id: int, total_cents: int):
    url = paypal_link(order_id, total_cents)
    if url:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💸 Payer via PayPal (entre proches)", url=url)],
            [InlineKeyboardButton(text="ℹ️ Comment faire ?", callback_data=f"paypal:howto:{order_id}")],
            kb_support_row()
        ])
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⚙️ Configurer PAYPAL_ME dans .env", url="https://www.paypal.me/")
    ], kb_support_row()])

def phone_kb():
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        one_time_keyboard=True,
        keyboard=[[KeyboardButton(text="📞 Envoyer mon numéro", request_contact=True)]],
        input_field_placeholder="Ex: 06 12 34 56 78"
    )

def format_user(m: Message) -> str:
    u = m.from_user
    handle = f"@{u.username}" if u.username else "(sans pseudo)"
    name = f"{(u.first_name or '').strip()} {(u.last_name or '').strip()}".strip()
    return f"{name} {handle} • id:{u.id}"

# --------- checkout helpers ---------

def _get_or_init_checkout(uid: int):
    return user_checkout.setdefault(uid, {"_active": True})

def stage_get(uid: int):
    u = user_checkout.get(uid)
    return u.get("_stage") if (u and u.get("_active")) else None

def stage_set(uid: int, stage: str):
    u = _get_or_init_checkout(uid)
    u["_active"] = True
    u["_stage"] = stage
    user_checkout[uid] = u
    checkout_prompt[uid] = stage  # filet de sécurité

async def prompt_name(m: Message):
    stage_set(m.from_user.id, "name")
    await m.answer("🧾 *Étape 1/3* — Indique ton *nom complet* :", parse_mode="Markdown",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                       [InlineKeyboardButton(text="⬅️ Retour au catalogue", callback_data="browse")],
                       kb_support_row()
                   ]))

async def prompt_name_for(uid: int, chat_id: int):
    stage_set(uid, "name")
    await bot.send_message(
        chat_id,
        "🧾 *Étape 1/3* — Indique ton *nom complet* :",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Retour au catalogue", callback_data="browse")],
            kb_support_row()
        ])
    )

async def prompt_phone(m: Message):
    stage_set(m.from_user.id, "phone")
    await m.answer("☎️ *Étape 2/3* — Partage ton *numéro* (bouton ci-dessous) ou écris-le :",
                   parse_mode="Markdown", reply_markup=phone_kb())

async def prompt_address(m: Message):
    stage_set(m.from_user.id, "address")
    await m.answer("🏠 *Étape 3/3* — Envoie maintenant ton *adresse complète* :", parse_mode="Markdown",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                       [InlineKeyboardButton(text="⬅️ Retour au catalogue", callback_data="browse")],
                       kb_support_row()
                   ]))

# --------- admin notifications ---------

async def notify_admins_text(text: str, parse_mode: str = "Markdown") -> int:
    ok = 0
    for admin in ADMINS:
        try:
            await bot.send_message(admin, text, parse_mode=parse_mode)
            ok += 1
        except Exception as e:
            print(f"[ADMIN NOTIFY ERROR text -> {admin}] {e}")
    return ok

async def notify_admins_photo_url(url_or_file_id: str, caption: str, parse_mode: str = "Markdown") -> int:
    ok = 0
    for admin in ADMINS:
        try:
            await bot.send_photo(admin, photo=url_or_file_id, caption=caption, parse_mode=parse_mode)
            ok += 1
        except Exception as e:
            print(f"[ADMIN NOTIFY ERROR photo -> {admin}] {e}")
            try:
                await bot.send_message(admin, caption + f"\n(photo: {url_or_file_id})", parse_mode=parse_mode)
                ok += 1
            except Exception as e2:
                print(f"[ADMIN NOTIFY ERROR fallback -> {admin}] {e2}")
    return ok

async def notify_admins_order_with_photos(order: dict, items) -> int:
    header = (
        f"🆕 Nouvelle commande #{order['order_id']}\n"
        f"{order['name']} — {order['phone']}\n"
        f"Adresse: {order['address']}\n"
        f"Total: {money(order['total_cents'])}"
    )
    sent = await notify_admins_text(header)
    for it in items:
        p = get_product(it["id"])
        img = get_image_for(p, it.get("color"))
        cap = (
            f"{it['name']}"
            f"{(' • ' + it['color']) if it.get('color') else ''} • T.{it['size']}\n"
            f"Qté: {it['qty']} — {money(it['price_cents']*it['qty'])}"
        )
        if img:
            sent += await notify_admins_photo_url(img, cap)
        else:
            sent += await notify_admins_text(cap)
    return sent

# ---------------------------- Commands ----------------------------

@dp.message(Command("whoami"))
async def whoami(m: Message):
    await m.answer(f"Ton ID Telegram : `{m.from_user.id}`", parse_mode="Markdown")

@dp.message(Command("help"))
async def help_cmd(m: Message):
    url = support_url()
    txt = "🆘 *Besoin d’aide ?* Contacte-nous en MP."
    if url:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Ouvrir la conversation", url=url)]])
        await m.answer(txt, parse_mode="Markdown", reply_markup=kb)
    else:
        await m.answer(txt + "\n\n(Configure `ADMIN_USERNAME` ou `SUPPORT_URL` dans `.env`.)", parse_mode="Markdown")

@dp.message(Command("debug_admins"))
async def debug_admins(m: Message):
    await m.answer(f"ADMINS lus : `{ADMINS}`", parse_mode="Markdown")

# ---------------------------- Handlers ----------------------------

@dp.message(CommandStart())
async def start(m: Message):
    if m.from_user.id in ADMINS:
        await m.answer("✅ Admin reconnu : vous recevrez les notifications en MP.")
    await m.answer(
        "👟 *Bienvenue à L’atelier de la chaussure !*\n\n"
        "🛍️ Feuillette le catalogue et passe commande directement ici.\n"
        "🚚 *Expédition* : entre **10 et 15 jours** après confirmation.\n"
        "📸 Tu peux aussi m’envoyer la *photo d’un modèle* + ta *taille* : je te confirme la dispo.\n\n"
        "Commandes utiles :\n"
        "• /catalogue – Voir les catégories\n"
        "• /panier – Voir le panier\n"
        "• /commander – Finaliser la commande\n"
        "• /help – Contacter un conseiller",
        parse_mode="Markdown",
        reply_markup=cat_kb()
    )

@dp.message(Command("catalogue"))
async def cmd_catalog(m: Message):
    await m.answer("Choisis une catégorie :", reply_markup=cat_kb())

@dp.message(Command("commander"))
async def cmd_commander(m: Message):
    await start_checkout(m)

@dp.callback_query(F.data == "help")
async def cb_help(cb: CallbackQuery):
    await help_cmd(cb.message)

@dp.callback_query(F.data == "browse")
async def browse_again(cb: CallbackQuery):
    await cb.message.answer("Choisis une catégorie :", reply_markup=cat_kb())

@dp.callback_query(F.data == "custom:askphoto")
async def ask_photo(cb: CallbackQuery):
    await cb.message.answer(
        "📸 Envoie *une photo du modèle* qui t’intéresse.\n"
        "Je te demanderai ensuite *ta taille*.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Retour au catalogue", callback_data="browse")],
            kb_support_row()
        ])
    )

# ---------- Catalogue / Produits ----------

@dp.callback_query(F.data.startswith("cat:"))
async def cat_list(cb: CallbackQuery):
    _, category, off = cb.data.split(":")
    offset = int(off or 0)
    prods, total = list_products(category if category else None, offset=offset, limit=PAGE_SIZE)
    if not prods:
        await safe_edit(cb, "Aucun produit.", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Catalogue", callback_data="browse")], kb_support_row()]
        ))
        return

    p = prods[0]
    colors_line = f"\nColoris: {', '.join(p['colors'])}" if p.get("colors") else ""
    caption = (
        f"**{p['name']}**\n"
        f"Catégorie: {p['category']}\n"
        f"Prix: {money(p['price_cents'])}\n"
        f"Tailles: {p['sizes'] or '—'}{colors_line}"
    )
    next_offset = offset + 1 if (offset + 1) < total else 0
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Ajouter (choisir options)", callback_data=f"add:{p['id']}")],
        [InlineKeyboardButton(text="Changer d’article", callback_data=f"cat:{category}:{next_offset}")],
        [InlineKeyboardButton(text="📦 Panier", callback_data="cart:view")],
        [InlineKeyboardButton(text="⬅️ Retour au catalogue", callback_data="browse")],
        kb_support_row()
    ])

    img = get_image_for(p, None)
    if img:
        try:
            await cb.message.edit_media(InputMediaPhoto(media=img, caption=caption, parse_mode="Markdown"))
            await cb.message.edit_reply_markup(reply_markup=kb)
        except TelegramBadRequest:
            await safe_edit(cb, caption, reply_markup=kb)
    else:
        await safe_edit(cb, caption, reply_markup=kb)

# ---------- Sélection produit: coloris -> saisie manuelle de taille ----------

@dp.callback_query(F.data.startswith("add:"))
async def add_choose_options(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    p = get_product(pid)
    if not p:
        await cb.answer("Produit introuvable", show_alert=True); return

    if p.get("colors"):
        rows = [[InlineKeyboardButton(text=c, callback_data=f"color:{pid}:{urllib.parse.quote(c, safe='')}")] for c in p["colors"]]
        rows.append([InlineKeyboardButton(text="⬅️ Retour au catalogue", callback_data="browse")])
        rows.append(kb_support_row())
        await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await cb.answer("Choisis un coloris", show_alert=False)
    else:
        manual_size_wait[cb.from_user.id] = {"pid": pid, "color": None}
        await cb.message.answer("✍️ *Étape 1/1* — Écris ta *taille* (ex: `42 EU`, `27.5`, `M`, etc.) :", parse_mode="Markdown")
        await cb.message.answer("Ou reviens au catalogue :", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Retour au catalogue", callback_data="browse")], kb_support_row()]
        ))

@dp.callback_query(F.data.startswith("color:"))
async def choose_color(cb: CallbackQuery):
    _, pid_str, color_enc = cb.data.split(":")
    pid = int(pid_str)
    color = urllib.parse.unquote(color_enc)
    p = get_product(pid)
    if not p:
        await cb.answer("Produit introuvable", show_alert=True); return

    img = get_image_for(p, color)
    caption = (
        f"**{p['name']}**\nCouleur: {color}\n"
        f"Prix: {money(p['price_cents'])}\n"
        f"✍️ Maintenant, entre ta *taille* au clavier."
    )
    try:
        if img:
            await cb.message.edit_media(InputMediaPhoto(media=img, caption=caption, parse_mode="Markdown"))
        else:
            await safe_edit(cb, caption)
    except TelegramBadRequest:
        await safe_edit(cb, caption)

    manual_size_wait[cb.from_user.id] = {"pid": pid, "color": color}
    await cb.message.answer("✍️ *Étape 1/1* — Écris ta *taille* (ex: `42 EU`, `27.5`, `M`, etc.) :", parse_mode="Markdown")
    await cb.message.answer("Ou reviens au catalogue :", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Retour au catalogue", callback_data="browse")], kb_support_row()]
    ))

# ---------- Réception d'une PHOTO (modèle perso) ----------
@dp.message(F.photo & ~F.via_bot)
async def on_photo(m: Message):
    file_id = m.photo[-1].file_id
    custom_model_wait[m.from_user.id] = {"file_id": file_id, "caption": (m.caption or "").strip()}
    await m.answer(
        "📸 Photo bien reçue.\n"
        "✍️ Indique maintenant ta *taille* pour ce modèle (ex: `42 EU`, `27.5`, `M`, etc.).",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Retour au catalogue", callback_data="browse")],
            kb_support_row()
        ])
    )

# ---------- Réception des MESSAGES TEXTE ----------
@dp.message(F.text & ~F.via_bot)
async def on_text(m: Message):
    uid = m.from_user.id
    stage_hint = checkout_prompt.get(uid)

    # 1) Taille manuelle PRODUIT (si pas en checkout)
    if uid in manual_size_wait and not stage_hint:
        entry = manual_size_wait.pop(uid)
        pid = entry["pid"]; color = entry.get("color")
        p = get_product(pid)
        if not p:
            await m.answer("Produit introuvable."); return
        size_text = m.text.strip()
        add_to_cart(uid, {
            "id": p["id"], "name": p["name"],
            "color": color, "size": size_text,
            "qty": 1, "price_cents": p["price_cents"]
        })
        couleur_txt = ("" if not color else f"{color} • ")
        await m.answer(f"Ajouté ✅ {p['name']} • {couleur_txt}Taille {size_text}")
        await m.answer("Que souhaites-tu faire ?", reply_markup=post_add_kb())
        return

    # 2) Taille pour PHOTO modèle perso (si pas en checkout)
    if uid in custom_model_wait and not stage_hint:
        entry = custom_model_wait.pop(uid)
        size_text = m.text.strip()
        who = format_user(m)
        cap_user = (
            "📥 *Demande modèle (photo)*\n"
            f"De: {who}\n"
            f"Taille demandée: {size_text}\n"
            f"Commentaire: {entry.get('caption') or '—'}"
        )
        ok = 0
        for admin in ADMINS:
            try:
                await bot.send_photo(admin, photo=entry["file_id"], caption=cap_user, parse_mode="Markdown"); ok += 1
            except Exception:
                try:
                    await bot.send_message(admin, cap_user, parse_mode="Markdown"); ok += 1
                except Exception as e2:
                    print(f"[ADMIN NOTIFY ERROR] {e2}")
        if ok == 0:
            await m.answer(
                "⚠️ Je n’ai pas pu notifier l’admin. Vérifie `ADMINS` et que l’admin a bien *démarré le bot* (/start).",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Retour au catalogue", callback_data="browse")], kb_support_row()])
            ); return
        await m.answer(
            "👍 Bien reçu ! On vérifie la disponibilité et on te répond rapidement.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Retour au catalogue", callback_data="browse")],
                [InlineKeyboardButton(text="📦 Voir panier", callback_data="cart:view")],
                kb_support_row()
            ])
        ); return

    # 3) CHECKOUT
    if user_checkout.get(uid, {}).get("_active") or stage_hint:
        stage = stage_get(uid) or stage_hint
        if stage == "name":
            _get_or_init_checkout(uid); user_checkout[uid]["name"] = m.text.strip()
            await prompt_phone(m); return
        if stage == "phone":
            if any(ch.isdigit() for ch in m.text):
                _get_or_init_checkout(uid); user_checkout[uid]["phone"] = m.text.strip()
                await prompt_address(m)
            else:
                await m.answer("Merci d'envoyer un *numéro de téléphone* valide ou d'utiliser le bouton ci-dessous.",
                               parse_mode="Markdown", reply_markup=phone_kb())
            return
        if stage == "address":
            _get_or_init_checkout(uid); user_checkout[uid]["address"] = m.text.strip()
            await finalize_order(m, uid); return

    # 4) Message libre
    await m.answer(
        "Besoin d’aide ? /help\n\n• /catalogue – Catégories\n• /panier – Voir le panier\n• /commander – Finaliser",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Retour au catalogue", callback_data="browse")], kb_support_row()])
    )

# ---------- Panier ----------
@dp.message(Command("panier"))
async def cart_cmd(m: Message):
    await cart_view(m)

@dp.callback_query(F.data == "cart:view")
async def cart_view_cb(cb: CallbackQuery):
    await cart_view(cb)

async def cart_view(ev):
    uid = ev.from_user.id
    items = carts[uid]
    if not items:
        await safe_edit(ev, "Ton panier est vide.\n\nRetour au catalogue :", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Catalogue", callback_data="browse")], kb_support_row()]
        )); return
    lines = ["🧺 *Ton panier:*"]
    for idx, i in enumerate(items):
        color_txt = f" • {i['color']}" if i.get("color") else ""
        lines.append(f"{idx+1}. {i['name']}{color_txt} • T.{i['size']} x{i['qty']} – {money(i['price_cents']*i['qty'])}")
    lines.append(f"\nTotal: *{money(cart_total_cents(uid))}*")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➖ Retirer le 1er", callback_data="cart:rm0"),
         InlineKeyboardButton(text="🗑 Vider", callback_data="cart:empty")],
        [InlineKeyboardButton(text="➕ Continuer les achats", callback_data="browse"),
         InlineKeyboardButton(text="✅ Commander", callback_data="checkout:start")],
        kb_support_row()
    ])
    await safe_edit(ev, "\n".join(lines), reply_markup=kb)

@dp.callback_query(F.data == "cart:rm0")
async def cart_rm0(cb: CallbackQuery):
    remove_from_cart(cb.from_user.id, 0)
    await cart_view(cb)

@dp.callback_query(F.data == "cart:empty")
async def cart_empty(cb: CallbackQuery):
    empty_cart(cb.from_user.id)
    await cart_view(cb)

# ---------- Checkout ----------
@dp.callback_query(F.data == "checkout:start")
async def chk_start(cb: CallbackQuery):
    uid = cb.from_user.id
    chat_id = cb.message.chat.id
    user_checkout[uid] = {"_active": True}
    await prompt_name_for(uid, chat_id)

async def start_checkout(m: Message):
    user_checkout[m.from_user.id] = {"_active": True}
    await prompt_name(m)

@dp.message(F.contact)
async def got_contact(m: Message):
    uid = m.from_user.id
    if not user_checkout.get(uid, {}).get("_active"):
        user_checkout[uid] = {"_active": True}
        await prompt_name(m); return
    user_checkout[uid]["phone"] = m.contact.phone_number
    if "name" not in user_checkout[uid]:
        await prompt_name(m)
    else:
        await prompt_address(m)

# ---------- Finalisation ----------
async def finalize_order(m: Message, uid: int):
    u = user_checkout.get(uid, {})
    items = carts[uid]
    if not items:
        await m.answer("Ton panier est vide.", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Catalogue", callback_data="browse")], kb_support_row()])
        )
        user_checkout.pop(uid, None); checkout_prompt.pop(uid, None)
        return
    total = cart_total_cents(uid)
    order_id = int(time.time())
    order = {
        "order_id": order_id,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "user_id": uid,
        "name": u.get("name", ""),
        "phone": u.get("phone", ""),
        "address": u.get("address", ""),
        "items_json": items,
        "total_cents": total,
        "status": "new",
    }
    append_order(order)

    sent = await notify_admins_order_with_photos(order, items)

    await m.answer(
        f"✅ Commande #{order_id} enregistrée.\n"
        f"Total: *{total/100:.2f} €*\n\n"
        "Clique pour *payer via PayPal.me*. "
        "Si possible, sélectionne **Entre proches** dans l’app et ajoute la note:\n"
        f"`Commande #{order_id}`",
        parse_mode="Markdown",
        reply_markup=payment_kb(order_id, total)
    )
    if sent == 0:
        await m.answer(
            "ℹ️ Note : je n’ai pas pu notifier l’admin en MP. Il devra *démarrer le bot* et vérifier `ADMINS`.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[kb_support_row()])
        )

    empty_cart(uid)
    user_checkout.pop(uid, None); checkout_prompt.pop(uid, None)
    await m.answer("Merci pour ta commande !", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Retour au catalogue", callback_data="browse")], kb_support_row()])
    )

# ---------------------------- Run ----------------------------

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
