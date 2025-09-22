# sheets.py
import os, time, json, re
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

# Charge .env
load_dotenv()

SHEET_ID = os.getenv("SHEET_ID")
PRODUCTS_TAB = os.getenv("PRODUCTS_TAB", "Products")
ORDERS_TAB = os.getenv("ORDERS_TAB", "Orders")

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_gc = None
_sh = None
_cache = {"products": ([], 0)}
TTL = 5  # secondes (cache court pour voir vite les MAJ)

# --- Helpers Google Drive -----------------------------------------------------

_RX_DRIVE_FILE = re.compile(r"https?://drive\.google\.com/file/d/([^/]+)/?")
_RX_DRIVE_ID_QS = re.compile(r"[?&]id=([^&]+)")

def _extract_gdrive_id(value: str) -> str | None:
    if not value:
        return None
    v = str(value).strip()

    if v.lower().startswith("gdrive:"):
        return v.split(":", 1)[1].strip()

    m = _RX_DRIVE_FILE.search(v)
    if m:
        return m.group(1)

    m = _RX_DRIVE_ID_QS.search(v)
    if m:
        return m.group(1)

    # heuristique: un ID Drive est une longue chaîne sans espace/"/"
    if len(v) >= 20 and "/" not in v and " " not in v and "http" not in v:
        return v

    return None

def _to_direct_gdrive_url(value: str) -> str:
    fid = _extract_gdrive_id(value) if value else None
    if fid:
        # "view" donne un rendu image direct
        return f"https://drive.google.com/uc?export=view&id={fid}"
    return value

# -----------------------------------------------------------------------------

def _ensure_client():
    global _gc, _sh
    if _gc is None:
        creds = Credentials.from_service_account_file("service_account.json", scopes=_SCOPES)
        _gc = gspread.authorize(creds)
    if _sh is None:
        if not SHEET_ID:
            raise RuntimeError("SHEET_ID manquant (vérifie ton .env).")
        try:
            _sh = _gc.open_by_key(SHEET_ID)
        except gspread.SpreadsheetNotFound as ex:
            raise RuntimeError(
                "Google Sheet introuvable (404). Vérifie :\n"
                " - l'ID (entre /d/ et /edit),\n"
                " - le PARTAGE avec l'email du service account (Éditeur)."
            ) from ex

def _ws(name: str):
    _ensure_client()
    return _sh.worksheet(name)

def _norm_key(k: str) -> str:
    # normalise les clés d'en-tête: "Image Color Map JSON " => "image_color_map_json"
    return re.sub(r"\s+", "_", (k or "").strip().lower())

def _normalize_row_keys(r: dict) -> dict:
    return {_norm_key(k): v for k, v in r.items()}

def _parse_colors(val: str):
    if not val:
        return []
    return [c.strip() for c in str(val).split(",") if c.strip()]

def _parse_image_color_map_json(val: str):
    if not val:
        return {}
    try:
        data = json.loads(val)
        out = {}
        for k, v in data.items():
            out[str(k).strip()] = _to_direct_gdrive_url(str(v).strip())
        return out
    except Exception:
        return {}

def get_products(force: bool = False):
    now = time.time()
    if not force and (now - _cache["products"][1] < TTL):
        return _cache["products"][0]

    rows = _ws(PRODUCTS_TAB).get_all_records()  # liste de dicts
    products = []
    for raw in rows:
        r = _normalize_row_keys(raw)  # <--- normalisation des en-têtes
        if str(r.get("active", 1)).strip().lower() not in ("1", "true", "vrai", "yes", "oui"):
            continue
        try:
            base_image = _to_direct_gdrive_url(str(r.get("image_url", "")).strip())
            products.append({
                "id": int(r.get("id")),
                "name": str(r.get("name", "")).strip(),
                "price_cents": int(r.get("price_cents", 0)),
                "sizes": str(r.get("sizes", "")).strip(),  # gardé pour affichage
                "category": str(r.get("category", "")).strip(),
                "image": base_image,
                "stock": int(r.get("stock", 0)),
                "colors": _parse_colors(r.get("colors", "")),
                "image_color_map": _parse_image_color_map_json(r.get("image_color_map_json", "")),
            })
        except Exception:
            continue

    _cache["products"] = (products, now)
    return products

def list_categories():
    return sorted({p["category"] for p in get_products() if p["category"]})

def list_products(category=None, offset=0, limit=6):
    prods = get_products()
    if category:
        prods = [p for p in prods if p["category"] == category]
    total = len(prods)
    return prods[offset: offset+limit], total

def get_product(pid: int):
    for p in get_products():
        if p["id"] == pid:
            return p
    return None

def get_image_for(product: dict, color: str | None = None):
    """Essaye clé exacte, puis match insensible casse/espaces."""
    if color:
        img_map = product.get("image_color_map") or {}
        # exact
        if color in img_map and img_map[color]:
            return img_map[color]
        # insensible à la casse/espaces
        norm = color.lower().strip()
        for k, v in img_map.items():
            if k.lower().strip() == norm and v:
                return v
    return product.get("image") or ""

def append_order(order_dict: dict):
    ws = _ws(ORDERS_TAB)
    row = [
        order_dict.get("order_id", ""),
        order_dict.get("timestamp", ""),
        order_dict.get("user_id", ""),
        order_dict.get("name", ""),
        order_dict.get("phone", ""),
        order_dict.get("address", ""),
        json.dumps(order_dict.get("items_json", []), ensure_ascii=False),
        order_dict.get("total_cents", 0),
        order_dict.get("status", "new"),
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")
