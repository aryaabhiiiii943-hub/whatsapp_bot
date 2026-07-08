import sys
sys.stdout.reconfigure(encoding='utf-8')

from flask import Flask, request, render_template_string
from groq import Groq
from datetime import datetime, timedelta
from dotenv import load_dotenv
from functools import wraps
import os
import re
import json
import difflib
import secrets
import psycopg2
import psycopg2.extras
import requests

load_dotenv()

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
META_PHONE_NUMBER_ID = os.environ.get("META_PHONE_NUMBER_ID")
META_ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN")
META_API_VERSION = os.environ.get("META_API_VERSION", "v20.0")
META_VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN", "restaurant-order-bot")  # not a secret, fine as default
OWNER_NUMBER = os.environ.get("OWNER_WHATSAPP_NUMBER", "918935842629")  # not a secret, fine as default

required_env_vars = {
    "GROQ_API_KEY": GROQ_API_KEY,
    "META_PHONE_NUMBER_ID": META_PHONE_NUMBER_ID,
    "META_ACCESS_TOKEN": META_ACCESS_TOKEN,
}
missing = [k for k, v in required_env_vars.items() if not v]
if missing:
    raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

groq_client = Groq(api_key=GROQ_API_KEY)

# ---------------------------------------------------------------------------
# Dashboard login (HTTP Basic Auth - no session/cookie machinery needed; the
# browser caches the credentials after the first prompt, which is all a
# single shared restaurant tablet/PC needs). Fails CLOSED: if
# DASHBOARD_PASSWORD isn't set in the environment, the dashboard stays locked
# rather than silently falling back to being public.
# ---------------------------------------------------------------------------
DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD")

def require_dashboard_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        auth = request.authorization
        ok = bool(
            DASHBOARD_PASSWORD
            and auth
            and secrets.compare_digest(auth.username or "", DASHBOARD_USERNAME)
            and secrets.compare_digest(auth.password or "", DASHBOARD_PASSWORD)
        )
        if not ok:
            return (
                "Login required.",
                401,
                {"WWW-Authenticate": 'Basic realm="Tandoori Junction Dashboard"'},
            )
        return view(*args, **kwargs)
    return wrapped

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("Missing required environment variable: DATABASE_URL")

def get_db():
    """Single place all DB access goes through. Render Postgres (not the old
    local SQLite file) - this is what makes order history survive service
    restarts/redeploys/spin-downs, which the free tier's ephemeral filesystem
    would otherwise wipe. cursor_factory gives dict-like row access
    (row['col']) matching how the rest of this file already reads rows."""
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=10,
                             cursor_factory=psycopg2.extras.RealDictCursor)
    return conn

def _q(conn, sql, params=()):
    """Thin sqlite3-compatibility shim: the rest of this file was written
    against sqlite3's conn.execute(sql, params) shortcut and '?' placeholders.
    psycopg2 needs an explicit cursor and '%s' placeholders - this one-line
    translation keeps every call site below almost unchanged."""
    cur = conn.cursor()
    cur.execute(sql.replace("?", "%s"), params)
    return cur

def init_db():
    with get_db() as conn:
        _q(conn, """
            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                timestamp TEXT,
                phone TEXT,
                order_text TEXT,
                total TEXT,
                location TEXT,
                payment_status TEXT DEFAULT 'Pending'
            )
        """)
        # Safe, idempotent migrations for existing databases that predate these
        # columns - Postgres supports IF NOT EXISTS directly, no need to check first.
        migrations = [
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS order_status TEXT DEFAULT 'Pending'",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS alert_wamid TEXT",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS alert_status TEXT DEFAULT 'none'",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS alert_retries INTEGER DEFAULT 0",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS alert_last_sent TEXT",
        ]
        for ddl in migrations:
            _q(conn, ddl)
        conn.commit()

init_db()

def save_order(phone, order_text, total, location):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        cur = _q(conn, """
            INSERT INTO orders (timestamp, phone, order_text, total, location, payment_status, order_status, alert_status)
            VALUES (?, ?, ?, ?, ?, 'Pending', 'Pending', 'none')
            RETURNING id
        """, (timestamp, phone, order_text, total, location or "Not shared"))
        new_id = cur.fetchone()["id"]
        conn.commit()
        return new_id

def send_meta_message(to_phone, text):
    """Sends a WhatsApp text message. Returns the message's WhatsApp id
    (wamid) on success so delivery can be tracked, or None on failure."""
    clean_to = str(to_phone).replace("whatsapp:", "").replace("+", "").strip()
    url = f"https://graph.facebook.com/{META_API_VERSION}/{META_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": clean_to,
        "type": "text",
        "text": {"preview_url": False, "body": text[:4096]}
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        if response.status_code == 200:
            print(f"Meta send OK: {response.status_code}")
            try:
                return response.json()["messages"][0]["id"]
            except (KeyError, IndexError, ValueError):
                return "unknown"
        else:
            print(f"Meta send FAILED ({response.status_code}): {response.text}")
            return None
    except Exception as e:
        print(f"Meta send error: {e}")
        return None

def build_order_alert_text(row, is_reminder=False):
    header = "REMINDER - Order Not Yet Confirmed Seen!" if is_reminder else "NEW ORDER - Tandoori Junction"
    return (
        f"{header}\n\n"
        f"Customer: {row['phone']}\n"
        f"Order:\n{row['order_text']}\n"
        f"Total: {row['total']}\n"
        f"Location: {row['location'] or 'Not shared'}\n"
        f"Time: {row['timestamp']}\n\n"
        f"Call customer to confirm delivery!"
    )

def resend_pending_alerts():
    """Runs on every request (see before_request hook below). Any owner alert
    that hasn't been confirmed 'delivered'/'read' by WhatsApp within a few
    minutes gets resent automatically, up to 3 attempts, so a new order can
    never silently go unnoticed just because a WhatsApp message got lost."""
    try:
        cutoff = (datetime.now() - timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M:%S")
        with get_db() as conn:
            rows = _q(conn, """
                SELECT * FROM orders
                WHERE alert_status IN ('sent', 'failed')
                  AND alert_retries < 3
                  AND (alert_last_sent IS NULL OR alert_last_sent < ?)
            """, (cutoff,)).fetchall()

        for row in rows:
            wamid = send_meta_message(OWNER_NUMBER, build_order_alert_text(row, is_reminder=True))
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with get_db() as conn:
                _q(conn,
                    "UPDATE orders SET alert_wamid=?, alert_status=?, alert_retries=alert_retries+1, alert_last_sent=? WHERE id=?",
                    (wamid, "sent" if wamid else "failed", now_str, row["id"])
                )
                conn.commit()
    except Exception as e:
        print(f"resend_pending_alerts error: {e}")

def handle_status_update(status_event):
    """Processes a WhatsApp delivery-status webhook event (sent/delivered/
    read/failed) for a previously sent owner alert, so we know whether it
    still needs to be resent."""
    wamid = status_event.get("id")
    status = status_event.get("status")
    if not wamid or not status:
        return
    with get_db() as conn:
        _q(conn, "UPDATE orders SET alert_status=? WHERE alert_wamid=?", (status, wamid))
        conn.commit()

CATEGORIES = {
    "1": {
        "name": "Tandoor & Starters",
        "display": """1. Paneer Tikka (6pcs) - Rs230
2. Chicken Tikka (5pcs) - Rs220
3. Chicken Reshmi Tikka - Rs250
4. Chicken Tandoori Half - Rs250
5. Chicken Tandoori Full - Rs450
6. Chicken Seekh Kebab - Rs230
7. Veg Seekh Kebab - Rs180
8. Chhola Bhatura - Rs50
9. Onion Pakoda - Rs100
10. Paneer Pakoda - Rs200
11. Chicken Pakoda - Rs210
12. Chicken Lollipop - Rs150""",
        "items_list": [
            ("Paneer Tikka", 230),
            ("Chicken Tikka", 220),
            ("Chicken Reshmi Tikka", 250),
            ("Chicken Tandoori Half", 250),
            ("Chicken Tandoori Full", 450),
            ("Chicken Seekh Kebab", 230),
            ("Veg Seekh Kebab", 180),
            ("Chhola Bhatura", 50),
            ("Onion Pakoda", 100),
            ("Paneer Pakoda", 200),
            ("Chicken Pakoda", 210),
            ("Chicken Lollipop", 150),
        ]
    },
    "2": {
        "name": "Indo-Chinese & Chowmein",
        "display": """1. Chicken Chilli Dry - Rs220
2. Chicken Chilli Gravy - Rs220
3. Chicken Manchurian Dry - Rs250
4. Chicken Manchurian Gravy - Rs250
5. Paneer Chilli Dry - Rs290
6. Paneer Chilli Gravy - Rs290
7. Veg Manchurian - Rs120
8. Veg Chowmein - Rs60
9. Egg Chowmein - Rs80
10. Egg Chicken Chowmein - Rs120""",
        "items_list": [
            ("Chicken Chilli Dry", 220),
            ("Chicken Chilli Gravy", 220),
            ("Chicken Manchurian Dry", 250),
            ("Chicken Manchurian Gravy", 250),
            ("Paneer Chilli Dry", 290),
            ("Paneer Chilli Gravy", 290),
            ("Veg Manchurian", 120),
            ("Veg Chowmein", 60),
            ("Egg Chowmein", 80),
            ("Egg Chicken Chowmein", 120),
        ]
    },
    "3": {
        "name": "Biryani & Main Course",
        "display": """1. Chicken Biryani - Rs150
2. Egg Biryani - Rs130
3. Mutton Biryani - Rs250
4. Paneer Masala - Rs200
5. Paneer Butter Masala - Rs220
6. Mix Veg - Rs200
7. Chicken Masala - Rs226
8. Chicken Butter Masala - Rs250
9. Dal Tadka - Rs80""",
        "items_list": [
            ("Chicken Biryani", 150),
            ("Egg Biryani", 130),
            ("Mutton Biryani", 250),
            ("Paneer Masala", 200),
            ("Paneer Butter Masala", 220),
            ("Mix Veg", 200),
            ("Chicken Masala", 226),
            ("Chicken Butter Masala", 250),
            ("Dal Tadka", 80),
        ]
    },
    "4": {
        "name": "Roti, Rice & Thali",
        "display": """1. Tawa Roti - Rs10
2. Tandoori Roti - Rs15
3. Plain Naan - Rs30
4. Butter Naan - Rs40
5. Garlic Naan - Rs60
6. Laacha Paratha - Rs50
7. Steam Rice - Rs40
8. Jeera Rice - Rs70
9. Veg Fried Rice - Rs100
10. Egg Fried Rice - Rs110
11. Veg Thali - Rs120
12. Mutton Thali - Rs220""",
        "items_list": [
            ("Tawa Roti", 10),
            ("Tandoori Roti", 15),
            ("Plain Naan", 30),
            ("Butter Naan", 40),
            ("Garlic Naan", 60),
            ("Laacha Paratha", 50),
            ("Steam Rice", 40),
            ("Jeera Rice", 70),
            ("Veg Fried Rice", 100),
            ("Egg Fried Rice", 110),
            ("Veg Thali", 120),
            ("Mutton Thali", 220),
        ]
    },
    "5": {
        "name": "Rolls & Momos",
        "display": """1. Veg Roll - Rs50
2. Egg Roll - Rs60
3. Paneer Roll - Rs90
4. Egg Chicken Roll - Rs100
5. Chicken Spring Roll - Rs80
6. Veg Momo Steamed - Rs150
7. Veg Momo Fried - Rs150
8. Chicken Momo Steamed - Rs150
9. Chicken Momo Fried - Rs150
10. Veg Tandoori Momo - Rs120
11. Chicken Tandoori Momo - Rs150""",
        "items_list": [
            ("Veg Roll", 50),
            ("Egg Roll", 60),
            ("Paneer Roll", 90),
            ("Egg Chicken Roll", 100),
            ("Chicken Spring Roll", 80),
            ("Veg Momo Steamed", 150),
            ("Veg Momo Fried", 150),
            ("Chicken Momo Steamed", 150),
            ("Chicken Momo Fried", 150),
            ("Veg Tandoori Momo", 120),
            ("Chicken Tandoori Momo", 150),
        ]
    },
    "6": {
        "name": "Pizza, Burgers & Dosa",
        "display": """1. Masala Dosa - Rs60
2. Paneer Dosa - Rs100
3. Plain Dosa - Rs50
4. Cheese Pizza - Rs140
5. Paneer Pizza - Rs160
6. Mushroom Pizza - Rs210
7. Veg Street Burger - Rs45
8. Eggy Burger - Rs95
9. Chicken Burger - Rs120""",
        "items_list": [
            ("Masala Dosa", 60),
            ("Paneer Dosa", 100),
            ("Plain Dosa", 50),
            ("Cheese Pizza", 140),
            ("Paneer Pizza", 160),
            ("Mushroom Pizza", 210),
            ("Veg Street Burger", 45),
            ("Eggy Burger", 95),
            ("Chicken Burger", 120),
        ]
    }
}

# Flat lookup of every menu item (built once at startup). This is the ONLY
# source of truth for item names and prices: everything the LLM proposes is
# resolved through here before it can touch the cart, and anything that
# doesn't resolve is rejected - so a hallucinated item/price can never be
# added, stated in a cart summary, or billed.
ITEM_LOOKUP = {}
for _cat_num, _cat in CATEGORIES.items():
    for _name, _price in _cat["items_list"]:
        ITEM_LOOKUP[_name.lower()] = {"name": _name, "price": _price, "category": _cat_num}

def _build_menu_reference():
    """Numbered menu (with real prices) injected into the agent's system
    prompt, so any item/price the LLM ever mentions comes from CATEGORIES,
    and 'item 3 wala' style references can be resolved per category."""
    lines = []
    for cat_num, cat in CATEGORIES.items():
        lines.append(f"Category {cat_num} - {cat['name']}:")
        for idx, (name, price) in enumerate(cat["items_list"], 1):
            lines.append(f"  {idx}. {name} - Rs{price}")
    return "\n".join(lines)

MENU_REFERENCE_TEXT = _build_menu_reference()

CATEGORY_MENU = """Tandoori Junction Menu
Good Food - Good Mood - Great Times

Konsi category chahiye?

1 - Tandoor & Starters
2 - Indo-Chinese & Chowmein
3 - Biryani & Main Course
4 - Roti, Rice & Thali
5 - Rolls & Momos
6 - Pizza, Burgers & Dosa

Number bhejo!"""

app = Flask(__name__)
sessions = {}

# WhatsApp will retry the webhook call if our server doesn't answer fast
# enough - very likely on this free Render tier, which can take 50+ seconds
# to wake from a cold start. Without dedup, a slow cold-start response can
# cause the SAME customer message (e.g. "haan") to be processed twice,
# creating a duplicate order and a duplicate owner alert. We remember the
# last N WhatsApp message ids we've already handled and skip repeats.
_processed_message_ids = []
_processed_message_ids_set = set()
_MAX_PROCESSED_IDS = 500

def _already_processed(message_id):
    if not message_id:
        return False
    if message_id in _processed_message_ids_set:
        return True
    _processed_message_ids.append(message_id)
    _processed_message_ids_set.add(message_id)
    if len(_processed_message_ids) > _MAX_PROCESSED_IDS:
        oldest = _processed_message_ids.pop(0)
        _processed_message_ids_set.discard(oldest)
    return False

MAX_ITEM_QUANTITY = 20  # sane per-item cap so a stray "50" typo doesn't create a huge accidental order

@app.before_request
def _check_pending_alerts():
    # Piggybacks on any incoming HTTP traffic (webhook calls, keep-alive
    # pings, dashboard loads) to opportunistically resend any owner order
    # alert that WhatsApp hasn't confirmed as delivered yet.
    resend_pending_alerts()

# ---------------------------------------------------------------------------
# Conversation memory
# ---------------------------------------------------------------------------
# history is a plain list of {"role": "user"|"assistant", "content": str}
# entries - exactly the shape the LLM chat API expects - kept per phone
# number in the in-memory session (same durability as before: lost on
# process restart, which is the accepted free-tier limitation).

MAX_HISTORY_MESSAGES = 16   # ~8 exchanges of context; enough for "wahi wala" / corrections without blowing up tokens
MAX_HISTORY_ENTRY_CHARS = 900  # big enough to keep a category listing readable in context

def new_session():
    return {
        "history": [],
        "location": None,
        "last_order": "",
        "stage": "new",
        "current_category": None,
        "cart": []
    }

def history_append(session, role, content):
    """Record one conversation turn (both customer messages and everything
    the bot sends, including deterministic fast-path replies) so the agent
    always reasons over the real, complete conversation."""
    if not content:
        return
    content = str(content).strip()
    if len(content) > MAX_HISTORY_ENTRY_CHARS:
        content = content[:MAX_HISTORY_ENTRY_CHARS] + " ...[truncated]"
    session["history"].append({"role": role, "content": content})
    if len(session["history"]) > MAX_HISTORY_MESSAGES:
        session["history"] = session["history"][-MAX_HISTORY_MESSAGES:]

def detect_intent(msg):
    msg = msg.lower().strip()
    greetings = ["hi", "hello", "hii", "hey", "namaste", "helo", "hlo"]
    menu_words = ["menu", "kya hai", "food", "khaana", "dishes", "list"]
    confirm_words = ["haan", "yes", "ha", "confirm", "done", "bilkul", "hna", "han"]
    cancel_words = ["nahi", "no", "nhi", "cancel"]
    faq_words = ["address", "timing", "time", "kab", "kahan", "delivery", "phone", "contact"]
    cart_words = ["cart", "checkout", "order karo", "place order", "total"]

    if any(w == msg for w in greetings):
        return "greeting"
    if any(w in msg for w in menu_words):
        return "menu"
    if any(w in msg for w in confirm_words):
        return "confirm"
    if any(w in msg for w in cancel_words):
        return "cancel"
    if any(w in msg for w in faq_words):
        return "faq"
    if any(w in msg for w in cart_words):
        return "cart"
    if msg in ["1", "2", "3", "4", "5", "6"]:
        return "category"
    if msg == "0":
        return "back"
    return "order"

def format_cart(cart):
    if not cart:
        return "Cart empty hai!"
    lines = ["Your Cart:"]
    total = 0
    for item in cart:
        subtotal = item["price"] * item["qty"]
        total += subtotal
        lines.append(f"- {item['name']} x{item['qty']} = Rs{subtotal}")
    lines.append(f"TOTAL: Rs{total}")
    return "\n".join(lines)

GREETING_TEXT = """Tandoori Junction mein swagat hai!
Good Food - Good Mood - Great Times

Main Riya hoon, aapki help ke liye!

MENU likhein menu dekhne ke liye
Ya seedha order kar sakte hain - jaise 'chicken biryani' ya 'do paneer tikka'!"""

FAQ_TEXT = """Tandoori Junction
Nayatola, Kalyani Road, Maharajpur
Sahibganj 816109

Timings: 10 AM - 10 PM
Phone: 9523087860
Home Delivery available"""

RESTAURANT_FACTS = """Tandoori Junction
Address: Nayatola, Kalyani Road, Maharajpur, Sahibganj 816109
Timings: 10 AM - 10 PM, open every day
Phone: 9523087860
Home delivery: available (usually 30-45 minutes)"""

# ---------------------------------------------------------------------------
# Deterministic cart operations (the ONLY code that ever mutates the cart)
# ---------------------------------------------------------------------------

def add_to_cart(session, name, price, qty):
    """Add a validated menu item. Merges quantities for an item already in
    the cart (so 'ek aur' cleanly bumps qty instead of duplicating lines,
    and removals can target one line per item)."""
    for entry in session["cart"]:
        if entry["name"] == name:
            entry["qty"] = min(MAX_ITEM_QUANTITY, entry["qty"] + qty)
            return
    session["cart"].append({"name": name, "price": price, "qty": min(MAX_ITEM_QUANTITY, qty)})

def remove_from_cart(session, name, qty):
    """Remove qty of an item from the cart. qty <= 0 means remove the item
    entirely. Returns True if anything was removed."""
    for entry in list(session["cart"]):
        if entry["name"] == name:
            if qty <= 0 or entry["qty"] <= qty:
                session["cart"].remove(entry)
            else:
                entry["qty"] -= qty
            return True
    return False

# ---------------------------------------------------------------------------
# The conversational agent
# ---------------------------------------------------------------------------
# One LLM call per customer message. The model sees the system prompt (persona
# + real menu + rules), the rolling conversation history, and an authoritative
# state block (cart/category/location computed by code). It returns:
#   reply   - the actual Hinglish text to send (composed, not templated)
#   actions - zero or more operations, executed IN ORDER by code below,
#             every one of them validated against ITEM_LOOKUP / CATEGORIES.
# The LLM never mutates the cart, never computes totals, and can only
# "state" prices that exist in the menu text we gave it.

AGENT_TURN_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["show_menu", "show_category", "add_items", "remove_items",
                                 "clear_cart", "show_cart", "confirm_order", "cancel_order", "none"]
                    },
                    "category_number": {
                        "type": "string",
                        "enum": ["1", "2", "3", "4", "5", "6", ""]
                    },
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "quantity": {"type": "integer"}
                            },
                            "required": ["name", "quantity"],
                            "additionalProperties": False
                        }
                    }
                },
                "required": ["type", "category_number", "items"],
                "additionalProperties": False
            }
        }
    },
    "required": ["reply", "actions"],
    "additionalProperties": False
}

AGENT_SYSTEM_PROMPT = f"""You are Riya, a friendly staff member taking orders on WhatsApp for Tandoori Junction, an Indian restaurant. Customers write in Hindi, English, Hinglish, with typos and slang. You can see the whole conversation so far - use it. Write short WhatsApp-style replies (1-3 short lines), warm and casual, mirroring the customer's language (Hinglish by default). Never mention being a bot, AI, or system.

RESTAURANT INFO (answer address/timing/phone/delivery questions from this only):
{RESTAURANT_FACTS}

MENU (the ONLY items and prices that exist):
{MENU_REFERENCE_TEXT}

You respond with JSON: {{"reply": "<text sent to the customer>", "actions": [...]}}.
Actions are executed by the system in order, top to bottom. Types:
- "show_menu": show the category list (customer asks what's available / wants the menu)
- "show_category": show one category's items - set category_number "1"-"6"
- "add_items": add to cart - fill "items" with {{"name": EXACT menu name, "quantity": n}}
- "remove_items": remove from cart - quantity 0 means remove that item completely
- "clear_cart": empty the cart (use BEFORE add_items when the customer wants to REPLACE the order: "sirf X chahiye", "only X", "baaki sab hata do")
- "show_cart": show cart with total (customer asks cart / total / kitna hua)
- "confirm_order": place the order - ONLY when the customer clearly agrees to place it
- "cancel_order": customer wants to cancel / start over
- "none": no action needed (greeting, chitchat, question, clarification)
For fields an action doesn't need, set category_number to "" and items to [].

HARD RULES:
1. NEVER invent menu items or prices. Every "name" in items must be copied EXACTLY from the MENU above. If they ask for something not on the menu, say it's not available and suggest the closest real item(s).
2. Only add items the customer EXPLICITLY asked for in this conversation. Fillers like "ok", "thik hai", "hmm", "accha" are NEVER an order.
3. NEVER write totals or do price arithmetic in "reply" - after any cart change the system automatically appends the exact cart with prices and total. Keep "reply" short and don't re-list items or prices in it. You may quote a single item's price only if copied from the MENU above.
4. Use the conversation history to resolve short replies: "ek aur" = one more of the item just discussed; "wahi wala" = the item mentioned earlier; "woh hata do" = remove the item just added; "chicken nahi paneer bola tha" = remove the chicken item and add the paneer equivalent.
5. "ok"/"haan"/"thik hai" RIGHT AFTER you asked them to confirm the order = confirm_order. The same words as a mere acknowledgment mid-conversation = no action - reply naturally (e.g. ask if they'd like anything else).
6. If the message is genuinely unclear (ambiguous item, veg/chicken/egg version not specified where the menu has several, unclear reference), take NO cart action and ask ONE short, specific clarifying question grounded in what they said - never a generic "samajh nahi aaya".
7. Order flow: items in cart -> delivery location (WhatsApp: attachment > Location > Send location) -> confirm. If they want to order/confirm but location isn't shared yet, the system will ask them for it.
8. If the conversation is just starting and they greet you, welcome them to Tandoori Junction, introduce yourself as Riya, and mention they can type MENU or just name a dish directly."""

def _agent_state_block(session):
    """Authoritative, code-computed snapshot injected fresh on every call, so
    the model never has to guess (or hallucinate) cart contents or totals."""
    if session["cart"]:
        total = sum(i["price"] * i["qty"] for i in session["cart"])
        cart_desc = "; ".join(f"{i['name']} x{i['qty']} (Rs{i['price']} each)" for i in session["cart"])
        cart_line = f"{cart_desc} | TOTAL so far: Rs{total}"
    else:
        cart_line = "empty"
    cat_name = CATEGORIES.get(session.get("current_category") or "", {}).get("name", "none")
    loc = "yes" if session.get("location") else "NO - must be shared before the order can be placed"
    return (
        "CURRENT STATE (authoritative, computed by the system - trust this over memory):\n"
        f"- Cart: {cart_line}\n"
        f"- Category being browsed: {cat_name}\n"
        f"- Delivery location shared: {loc}"
    )

# gpt-oss-20b on Groq has a well-documented quirk: when its answer involves a
# non-trivial structured payload (i.e. our "actions" array is non-empty), it
# frequently emits its response through its internal tool-calling channel
# instead of respecting response_format=json_schema - even though no tool was
# registered. Groq's API then hard-rejects the whole call with
# "Tool choice is none, but model called a tool" (400), which silently drops
# the turn to the dumb legacy keyword fallback. Fix: stop fighting this -
# register agent_turn as a REAL tool and force the model to call it, which is
# exactly what it already wants to do. This is deterministic, not a retry hack.
AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "agent_turn",
            "description": "Compose the reply to the WhatsApp customer and specify the cart/menu actions (if any) to perform for this turn.",
            "parameters": AGENT_TURN_SCHEMA,
        },
    }
]
AGENT_TOOL_CHOICE = {"type": "function", "function": {"name": "agent_turn"}}

def _call_groq_agent(messages):
    kwargs = dict(
        model="openai/gpt-oss-20b",
        messages=messages,
        tools=AGENT_TOOLS,
        tool_choice=AGENT_TOOL_CHOICE,
        temperature=0.3,
        max_completion_tokens=1200,  # gpt-oss spends some of this on reasoning tokens
    )
    try:
        # Low reasoning effort keeps webhook latency down on this model.
        return groq_client.chat.completions.create(reasoning_effort="low", **kwargs)
    except TypeError:
        # Older groq SDK without reasoning_effort support - call without it.
        return groq_client.chat.completions.create(**kwargs)

def run_conversation_agent(session, incoming_msg):
    """Single LLM call over the REAL conversation: system prompt + rolling
    history (which already ends with the current customer message) + fresh
    state block. Returns a dict matching AGENT_TURN_SCHEMA, or None if the
    call fails (caller falls back to the keyword-matching legacy path)."""
    messages = [{"role": "system", "content": AGENT_SYSTEM_PROMPT + "\n\n" + _agent_state_block(session)}]
    for h in session["history"]:
        messages.append({"role": h["role"], "content": h["content"]})
    # Defensive: make sure the current message is the last thing the model sees
    # even if history recording was somehow skipped.
    if not session["history"] or session["history"][-1]["role"] != "user":
        messages.append({"role": "user", "content": incoming_msg})
    try:
        completion = _call_groq_agent(messages)
        msg = completion.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            args_str = tool_calls[0].function.arguments
        else:
            # Defensive fallback in case a future SDK/model version answers
            # in plain content instead of a tool call.
            args_str = msg.content
        return json.loads(args_str)
    except Exception as e:
        print(f"LLM agent error: {e}")
        return None

def resolve_item(name):
    """Match a (possibly fuzzy / typo'd) item name to an exact menu item."""
    if not name:
        return None
    key = name.strip().lower()
    if key in ITEM_LOOKUP:
        return ITEM_LOOKUP[key]
    close = difflib.get_close_matches(key, ITEM_LOOKUP.keys(), n=1, cutoff=0.8)
    if close:
        return ITEM_LOOKUP[close[0]]
    return None

def show_category(session, category_number):
    cat = CATEGORIES.get(category_number)
    if not cat:
        session["stage"] = "menu"
        session["current_category"] = None
        return CATEGORY_MENU
    session["current_category"] = category_number
    session["stage"] = "subcategory"
    return f"{cat['name']}\n\n{cat['display']}\n\nItem number ya naam + quantity likhein!\nJaise: 3 2 (item 3, qty 2) ya 'chicken biryani 2'\n\nCART - cart dekhein\n0 - wapas menu pe"

def add_items_and_reply(session, items):
    """Legacy-fallback helper: validate + add items, reply with a template.
    (The agent path uses execute_agent_actions instead.)"""
    added_lines = []
    not_found = []
    for it in items:
        resolved = resolve_item(it.get("name", ""))
        try:
            qty = max(1, min(MAX_ITEM_QUANTITY, int(it.get("quantity") or 1)))
        except (TypeError, ValueError):
            qty = 1
        if resolved:
            add_to_cart(session, resolved["name"], resolved["price"], qty)
            added_lines.append(f"{resolved['name']} x{qty}")
        else:
            not_found.append(it.get("name", "") or "?")

    parts = []
    if added_lines:
        parts.append("Cart mein add ho gaya:\n" + "\n".join(f"- {l}" for l in added_lines))
    if not_found:
        parts.append("Yeh menu mein nahi mila: " + ", ".join(not_found) + "\nMENU likhein poora menu dekhne ke liye.")
    if not parts:
        parts.append("Kuch samajh nahi aaya. MENU likhein poora menu dekhne ke liye.")
    parts.append("Aur kuch chahiye? Item ka naam likhein, ya CART likhein dekhne ke liye.")
    return "\n\n".join(parts)

def render_cart_reply(session):
    if session["cart"]:
        return f"{format_cart(session['cart'])}\n\nDelivery ke liye location share karein!\nWhatsApp mein attachment > Location > Send location"
    return "Cart empty hai! Item ka naam bhejein ya MENU likhein order karne ke liye."

def finalize_order(session, phone):
    # Build the order text/total fresh from the current cart (not the frozen
    # snapshot taken when location was shared) so that any items added or
    # changed after sharing location - e.g. "aur ek naan" right before saying
    # "haan" - are correctly included in what gets saved and sent to the owner.
    if not session["cart"]:
        return "Cart abhi empty hai! Pehle kuch order karein - item ka naam likhein."

    order_text = format_cart(session["cart"])
    total_num = sum(item["price"] * item["qty"] for item in session["cart"])
    total = f"Rs{total_num}"

    order_id = save_order(phone, order_text, total, session["location"])

    with get_db() as conn:
        row = _q(conn, "SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()

    wamid = send_meta_message(OWNER_NUMBER, build_order_alert_text(row))
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        _q(conn,
            "UPDATE orders SET alert_wamid=?, alert_status=?, alert_last_sent=? WHERE id=?",
            (wamid, "sent" if wamid else "failed", now_str, order_id)
        )
        conn.commit()

    reply = """Order Confirmed - Tandoori Junction!

Aapka order place ho gaya!
Delivery time: 30-45 minutes
Humare staff aapko call karenge

Shukriya!"""

    sessions[phone] = new_session()
    return reply

def execute_agent_actions(session, phone, parsed):
    """Execute the agent's proposed actions with hard validation, then
    assemble the outgoing message: the LLM's composed reply first, followed
    by code-rendered, authoritative blocks (menu / category / cart with real
    prices and deterministic totals). The LLM's words never decide what gets
    billed - only resolved ITEM_LOOKUP entries do."""
    llm_reply = (parsed.get("reply") or "").strip()
    if len(llm_reply) > 700:
        llm_reply = llm_reply[:700].rstrip()
    actions = parsed.get("actions")
    if not isinstance(actions, list):
        actions = []
    actions = actions[:5]  # sanity cap

    blocks = []
    cart_changed = False
    cart_shown = False
    wants_confirm = False
    not_found = []
    not_in_cart = []

    for action in actions:
        if not isinstance(action, dict):
            continue
        a_type = action.get("type", "none")

        if a_type == "show_menu":
            session["stage"] = "menu"
            session["current_category"] = None
            blocks.append(CATEGORY_MENU)

        elif a_type == "show_category":
            cat_num = action.get("category_number") or ""
            if CATEGORIES.get(cat_num):
                blocks.append(show_category(session, cat_num))
            else:
                session["stage"] = "menu"
                session["current_category"] = None
                blocks.append(CATEGORY_MENU)

        elif a_type == "add_items":
            for it in (action.get("items") or [])[:10]:
                if not isinstance(it, dict):
                    continue
                resolved = resolve_item(it.get("name", ""))
                try:
                    qty = max(1, min(MAX_ITEM_QUANTITY, int(it.get("quantity") or 1)))
                except (TypeError, ValueError):
                    qty = 1
                if resolved:
                    add_to_cart(session, resolved["name"], resolved["price"], qty)
                    cart_changed = True
                else:
                    not_found.append(it.get("name") or "?")

        elif a_type == "remove_items":
            for it in (action.get("items") or [])[:10]:
                if not isinstance(it, dict):
                    continue
                resolved = resolve_item(it.get("name", ""))
                try:
                    qty = int(it.get("quantity") or 0)
                except (TypeError, ValueError):
                    qty = 0
                target = resolved["name"] if resolved else (it.get("name") or "")
                if target and remove_from_cart(session, target, qty):
                    cart_changed = True
                else:
                    not_in_cart.append(it.get("name") or "?")

        elif a_type == "clear_cart":
            if session["cart"]:
                cart_changed = True
            session["cart"] = []

        elif a_type == "show_cart":
            blocks.append(render_cart_reply(session))
            cart_shown = True

        elif a_type == "cancel_order":
            session["cart"] = []
            session["current_category"] = None
            session["stage"] = "welcome"
            cart_changed = False
            if not llm_reply:
                llm_reply = "Koi baat nahi, cart clear kar diya! MENU likhein dobara shuru karne ke liye."

        elif a_type == "confirm_order":
            wants_confirm = True

    if not_found:
        blocks.append("Yeh menu mein nahi mila: " + ", ".join(not_found[:5]) + "\nMENU likhein poora menu dekhne ke liye.")
    if not_in_cart:
        blocks.append("Yeh cart mein nahi tha: " + ", ".join(not_in_cart[:5]))

    if wants_confirm:
        if not_found:
            # Something they asked for couldn't be matched - never place a
            # partially-understood order; the not-found note above asks first.
            pass
        elif not session["cart"]:
            blocks.append("Cart abhi empty hai! Pehle kuch order karein - item ka naam likhein ya MENU likhein.")
        elif not session["location"]:
            blocks.append(f"{format_cart(session['cart'])}\n\nOrder confirm karne ke liye pehle apni location share karein!\nWhatsApp mein attachment > Location > Send location")
            cart_shown = True
        else:
            confirmation = finalize_order(session, phone)
            parts = [p for p in ([llm_reply] + blocks + [confirmation]) if p]
            return "\n\n".join(parts)

    if cart_changed and not cart_shown:
        # Always show the authoritative cart (real names, real prices,
        # code-computed total) after any change the agent made.
        blocks.append(format_cart(session["cart"]))

    reply = "\n\n".join(p for p in ([llm_reply] + blocks) if p).strip()
    if not reply:
        reply = "Ji, bataiye kya order karna hai? MENU likhein dekhne ke liye, ya seedha item ka naam bhejein."
    return reply

def legacy_parse_items(msg):
    """Very simple, LLM-free splitter used only when the Groq call fails:
    breaks a message like '2 chicken biryani aur ek paneer tikka' into
    candidate item phrases + quantities, so resolve_item()/add_items_and_reply()
    can still try to match them (or correctly report them as not on the menu),
    instead of the bot giving up with a generic 'having trouble' message."""
    parts = re.split(r'\b(?:aur|and)\b|[,+]', msg, flags=re.IGNORECASE)
    items = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        qty = 1
        m = re.match(r'^(\d+)\s+(.*)$', part)
        if m:
            qty = int(m.group(1))
            part = m.group(2).strip()
        else:
            m2 = re.match(r'^(.*?)\s+(\d+)$', part)
            if m2:
                part = m2.group(1).strip()
                qty = int(m2.group(2))
        if part:
            items.append({"name": part, "quantity": qty})
    return items

def legacy_intent_reply(session, phone, incoming_msg, intent):
    """Fallback used only if the Groq call fails, so the bot stays responsive
    using simple keyword matching instead of natural language understanding."""
    if intent == "greeting" or session["stage"] == "new":
        session["stage"] = "welcome"
        return GREETING_TEXT
    if intent in ("menu", "back"):
        session["stage"] = "menu"
        session["current_category"] = None
        return CATEGORY_MENU
    if intent == "cart":
        return render_cart_reply(session)
    if intent == "faq":
        return FAQ_TEXT
    if intent == "confirm" and session["stage"] == "confirming":
        return finalize_order(session, phone)
    if intent == "cancel":
        session["stage"] = "welcome"
        session["cart"] = []
        session["current_category"] = None
        return "Koi baat nahi! Cart clear kar diya. MENU likhein dobara order karne ke liye."
    if intent == "order":
        items = legacy_parse_items(incoming_msg)
        if items:
            return add_items_and_reply(session, items)
        return f"'{incoming_msg}' samajh nahi paaye. MENU likhein poora menu dekhne ke liye, ya item ka sahi naam bhejein."
    return "Abhi thoda dikkat ho rahi hai samajhne mein. MENU likhein ya item ka number/naam likhein."

def get_latest_order_id():
    """Single cheap query used by the dashboard's lightweight polling alert -
    just the max id, not the full order rows."""
    with get_db() as conn:
        row = _q(conn, "SELECT MAX(id) AS max_id FROM orders").fetchone()
    return (row["max_id"] if row else None) or 0

def _parse_total(t):
    try:
        return int(str(t).replace("Rs", "").strip())
    except (TypeError, ValueError):
        return 0

def _compute_dashboard_data(detail_limit=200):
    """Everything the dashboard needs, computed fresh from the DB. Split into
    a cheap full-table scan (just timestamp/total/status - used for the daily
    summary and status counters, which must always reflect EVERY order ever
    placed) plus a capped detailed fetch (the last `detail_limit` full order
    rows shown as cards) so the page doesn't get slower/heavier every single
    day the restaurant stays open - it stays fast on day 1 and a year from now."""
    with get_db() as conn:
        light_rows = _q(conn, "SELECT timestamp, total, order_status FROM orders").fetchall()
        total_count = len(light_rows)
        orders = _q(conn,
            "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (detail_limit,)
        ).fetchall()

    daily = {}
    pending_count = dispatched_count = delivered_count = 0
    for o in light_rows:
        day = (o["timestamp"] or "")[:10] or "Unknown"
        d = daily.setdefault(day, {"date": day, "order_count": 0, "day_total": 0})
        d["order_count"] += 1
        d["day_total"] += _parse_total(o["total"])
        status = o["order_status"] or "Pending"
        if status == "Dispatched":
            dispatched_count += 1
        elif status == "Delivered":
            delivered_count += 1
        else:
            pending_count += 1
    daily_list = sorted(daily.values(), key=lambda d: d["date"], reverse=True)

    today_str = datetime.now().strftime("%Y-%m-%d")
    today_orders = daily.get(today_str, {"order_count": 0, "day_total": 0})

    orders_json = [dict(o) for o in orders]

    return {
        "orders": orders,
        "orders_json": orders_json,
        "daily_list": daily_list,
        "today_orders": today_orders,
        "pending_count": pending_count,
        "dispatched_count": dispatched_count,
        "delivered_count": delivered_count,
        "total_count": total_count,
        "latest_order_id": orders[0]["id"] if orders else 0,
    }

@app.route("/api/latest_order_id")
def api_latest_order_id():
    # Never let a transient DB hiccup crash this - the dashboard's alert
    # polling depends on this endpoint always returning *something* valid.
    try:
        return {"id": get_latest_order_id(), "ok": True}
    except Exception as e:
        print(f"api_latest_order_id error: {e}")
        return {"id": -1, "ok": False}

@app.route("/api/dashboard_data")
@require_dashboard_auth
def api_dashboard_data():
    """JSON version of everything /dashboard shows, used by the page's own
    JS to refresh itself in place (no full navigation) whenever a new order
    arrives or on a periodic timer - so an in-progress order never gets
    interrupted by a page reload, and the connection-status indicator has
    something real to report on."""
    try:
        data = _compute_dashboard_data()
        return {
            "ok": True,
            "orders": data["orders_json"],
            "daily_list": data["daily_list"],
            "today_orders": data["today_orders"],
            "pending_count": data["pending_count"],
            "dispatched_count": data["dispatched_count"],
            "delivered_count": data["delivered_count"],
            "total_count": data["total_count"],
            "latest_order_id": data["latest_order_id"],
        }
    except Exception as e:
        print(f"api_dashboard_data error: {e}")
        return {"ok": False, "error": str(e)}, 200

@app.route("/dashboard")
@require_dashboard_auth
def dashboard():
    try:
        data = _compute_dashboard_data()
    except Exception as e:
        print(f"dashboard error: {e}")
        return (
            "<html><body style='font-family:Arial;text-align:center;padding:60px;'>"
            "<h2>Dashboard temporarily unavailable</h2>"
            "<p>Could not read the orders database. This page will keep retrying "
            "automatically - please wait a few seconds and tap Refresh.</p>"
            "<button onclick='location.reload()' style='padding:10px 20px;font-size:16px;'>Refresh</button>"
            "</body></html>", 200
        )

    orders = data["orders"]
    daily_list = data["daily_list"]
    today_orders = data["today_orders"]
    pending_count = data["pending_count"]
    dispatched_count = data["dispatched_count"]
    delivered_count = data["delivered_count"]
    total_count = data["total_count"]

    return render_template_string("""
<!DOCTYPE html>
<html>
<head>
    <title>Tandoori Junction Dashboard</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: Arial, sans-serif; background: #f5f5f5; }
        .header { background: #e74c3c; color: white; padding: 20px; text-align: center; position: relative; }
        .header h1 { font-size: 24px; }
        #connStatus { position: absolute; top: 10px; right: 14px; font-size: 12px; background: rgba(0,0,0,0.2); padding: 4px 10px; border-radius: 12px; }
        #connStatus.ok { background: rgba(0,0,0,0.2); }
        #connStatus.bad { background: #c0392b; font-weight: bold; }
        .stats { display: flex; gap: 15px; padding: 20px; flex-wrap: wrap; }
        .stat-card { background: white; border-radius: 10px; padding: 20px; flex: 1; min-width: 130px; text-align: center; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        .stat-card h2 { font-size: 28px; color: #e74c3c; }
        .stat-card p { color: #666; font-size: 13px; }
        .section { padding: 0 20px 20px; }
        .section h2 { padding: 15px 0; }
        .order-card { background: white; border-radius: 10px; padding: 15px; margin-bottom: 15px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); border-left: 4px solid #e74c3c; }
        .order-header { display: flex; justify-content: space-between; margin-bottom: 10px; }
        .order-id { font-weight: bold; color: #e74c3c; }
        .order-time { color: #999; font-size: 13px; }
        .order-phone { color: #333; font-size: 14px; margin-bottom: 8px; }
        .order-text { background: #f9f9f9; padding: 10px; border-radius: 5px; font-size: 13px; color: #444; margin-bottom: 8px; white-space: pre-wrap; }
        .order-footer { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; }
        .total { font-weight: bold; color: #27ae60; font-size: 16px; }
        .status { padding: 4px 10px; border-radius: 20px; font-size: 12px; font-weight: bold; }
        .status-pending { background: #fff3cd; color: #856404; }
        .status-dispatched { background: #cce5ff; color: #004085; }
        .status-delivered { background: #d4edda; color: #155724; }
        .alert-hint { font-size: 11px; color: #999; margin-top: 4px; }
        .location { color: #3498db; font-size: 13px; text-decoration: none; }
        .no-orders { text-align: center; padding: 50px; color: #999; }
        .refresh-btn { background: #e74c3c; color: white; border: none; padding: 10px 20px; border-radius: 5px; cursor: pointer; margin-bottom: 15px; float: right; }
        .status-btns { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 10px; }
        .status-btn { border: 1px solid #ddd; background: white; padding: 6px 12px; border-radius: 6px; font-size: 12px; cursor: pointer; }
        .status-btn.active { color: white; border: none; }
        .status-btn.active.p { background: #f1b400; }
        .status-btn.active.d { background: #007bff; }
        .status-btn.active.v { background: #28a745; }
        .status-btn:disabled { opacity: 0.6; cursor: default; }
        table { width: 100%; border-collapse: collapse; background: white; border-radius: 10px; overflow: hidden; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        th, td { text-align: left; padding: 12px 15px; font-size: 13px; border-bottom: 1px solid #eee; }
        th { background: #fafafa; color: #666; }
        #newOrderBanner { display: none; position: sticky; top: 0; z-index: 999; background: #28a745; color: white; text-align: center; font-size: 22px; font-weight: bold; padding: 18px; animation: flash 0.6s infinite alternate; cursor: pointer; }
        @keyframes flash { from { background: #28a745; } to { background: #1e7e34; } }
        #stopAlarmBtn { display: none; position: sticky; top: 0; z-index: 1000; width: 100%; background: #c0392b; color: white; text-align: center; font-size: 20px; font-weight: bold; padding: 22px; border: none; cursor: pointer; animation: alarmFlash 0.4s infinite alternate; }
        @keyframes alarmFlash { from { background: #c0392b; } to { background: #8e2117; } }
        #enableSoundBtn { background: #222; color: white; border: none; padding: 10px 18px; border-radius: 6px; font-size: 14px; cursor: pointer; margin: 10px auto; display: block; }
        #soundStatusBtn { background: #28a745; color: white; border: none; padding: 6px 14px; border-radius: 14px; font-size: 12px; cursor: pointer; margin: 6px auto; display: block; }
        #staleWarning { display: none; background: #fff3cd; color: #856404; text-align: center; padding: 10px; font-size: 13px; }
    </style>
</head>
<body>
    <button id="stopAlarmBtn" onclick="confirmOrderReceived()">NEW ORDER RECEIVED! - tap here to stop the alarm</button>
    <div id="newOrderBanner" onclick="confirmOrderReceived()">NEW ORDER RECEIVED! (tap to confirm)</div>
    <div id="staleWarning">Connection lost - alerts may be delayed. Retrying automatically... you can also tap Refresh below.</div>
    <button id="enableSoundBtn" onclick="enableSound()">Tap once to enable order alerts (sound + notifications)</button>
    <button id="soundStatusBtn" onclick="enableSound()" style="display:none;">Alerts ON - tap to test</button>
    <div class="header">
        <span id="connStatus" class="ok">Connecting...</span>
        <h1>Tandoori Junction Dashboard</h1>
        <p>Order Management</p>
    </div>
    <div class="stats" id="statsContainer">
        <div class="stat-card"><h2 id="statTotal">{{ total_count }}</h2><p>Total Orders</p></div>
        <div class="stat-card"><h2 id="statPending">{{ pending_count }}</h2><p>Pending</p></div>
        <div class="stat-card"><h2 id="statDispatched">{{ dispatched_count }}</h2><p>Dispatched</p></div>
        <div class="stat-card"><h2 id="statDelivered">{{ delivered_count }}</h2><p>Delivered</p></div>
        <div class="stat-card"><h2 id="statTodayCount">{{ today_orders['order_count'] }}</h2><p>Today's Orders</p></div>
        <div class="stat-card"><h2 id="statTodayTotal">Rs{{ today_orders['day_total'] }}</h2><p>Today's Collection</p></div>
    </div>
    <div class="section">
        <h2>Recent Orders</h2>
        <button class="refresh-btn" onclick="refreshDashboard(true)">Refresh</button>
        <div style="clear:both"></div>
        <div id="ordersContainer">
        {% if orders %}
            {% for order in orders %}
            <div class="order-card" data-order-id="{{ order['id'] }}">
                <div class="order-header">
                    <span class="order-id">Order #{{ order['id'] }}</span>
                    <span class="order-time">{{ order['timestamp'] }}</span>
                </div>
                <div class="order-phone">Phone: {{ order['phone'] }}</div>
                <div class="order-text">{{ order['order_text'] }}</div>
                <div class="order-footer">
                    <span class="total">{{ order['total'] }}</span>
                    <span class="status status-{{ (order['order_status'] or 'Pending')|lower }}">{{ order['order_status'] or 'Pending' }}</span>
                    {% if order['location'] and order['location'] != 'Not shared' %}
                    <a class="location" href="{{ order['location'] }}" target="_blank">View Location</a>
                    {% endif %}
                </div>
                {% if order['alert_status'] and order['alert_status'] not in ('delivered', 'read') %}
                <div class="alert-hint">Owner alert not yet confirmed delivered ({{ order['alert_status'] }}, {{ order['alert_retries'] }} retries) - auto-retrying.</div>
                {% endif %}
                <div class="status-btns">
                    <button type="button" class="status-btn {{ 'active p' if (order['order_status'] or 'Pending') == 'Pending' else '' }}" data-order-id="{{ order['id'] }}" data-status="Pending">Pending</button>
                    <button type="button" class="status-btn {{ 'active d' if order['order_status'] == 'Dispatched' else '' }}" data-order-id="{{ order['id'] }}" data-status="Dispatched">Dispatched</button>
                    <button type="button" class="status-btn {{ 'active v' if order['order_status'] == 'Delivered' else '' }}" data-order-id="{{ order['id'] }}" data-status="Delivered">Delivered</button>
                </div>
            </div>
            {% endfor %}
        {% else %}
            <div class="no-orders"><p>No orders yet!</p></div>
        {% endif %}
        </div>
    </div>
    <div class="section">
        <h2>Daily Summary</h2>
        <div id="dailyContainer">
        {% if daily_list %}
        <table>
            <tr><th>Date</th><th>Orders</th><th>Total Collected</th></tr>
            {% for d in daily_list %}
            <tr>
                <td>{{ d['date'] }}</td>
                <td>{{ d['order_count'] }}</td>
                <td>Rs{{ d['day_total'] }}</td>
            </tr>
            {% endfor %}
        </table>
        {% else %}
            <div class="no-orders"><p>No orders yet!</p></div>
        {% endif %}
        </div>
    </div>
    <script>
        // ---------------------------------------------------------------
        // Reliability layer. Design goals, in order of priority:
        //   1. Taking/updating an order must NEVER be interrupted by a
        //      page navigation - so after first load, nothing here calls
        //      location.reload() during normal operation. All updates are
        //      done by re-fetching JSON and patching the DOM in place.
        //   2. A single fetch failure (cold start, wifi blip) must never
        //      stop the polling loop - every network call is wrapped so
        //      failures just get logged into the on-screen status pill.
        //   3. Staff must always be able to tell, at a glance, whether the
        //      alert system is actually working (connStatus pill) instead
        //      of silently trusting "no alert = no new orders".
        // ---------------------------------------------------------------
        // Deliberately NOT seeded from localStorage. A cached "last seen
        // order id" surviving across a database migration/reset (order ids
        // starting back at 1) would otherwise silently block every future
        // alarm forever, since every real new id would stay below the old
        // stale cached number - this is exactly what happened after the
        // SQLite -> Postgres migration reset ids. The only trustworthy
        // baseline is what the server just rendered into this page load:
        // everything already visible in the order list below is, by
        // definition, already seen.
        var lastSeenId = parseInt('{{ latest_order_id }}', 10) || 0;
        var soundEnabled = localStorage.getItem('soundEnabled') === '1';
        var audioCtx = null;
        var consecutiveFailures = 0;
        var wakeLock = null;

        function escapeHtml(s) {
            return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
                return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
            });
        }

        var originalTitle = document.title;
        var alarmActive = false;
        var alarmRepeatInterval = null;
        var titleFlashInterval = null;
        var titleFlashOn = false;

        function enableSound() {
            soundEnabled = true;
            localStorage.setItem('soundEnabled', '1');
            document.getElementById('enableSoundBtn').style.display = 'none';
            document.getElementById('soundStatusBtn').style.display = 'block';
            try {
                if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
                audioCtx.resume();
            } catch (e) {}
            beep(1, 0.15); // quiet confirmation blip so staff know it's on
            // Ask for OS-level notification permission at the same time -
            // once granted, a new order shows a real system notification
            // (like a Gmail/Slack toast) even if this tab isn't focused or
            // visible, as long as it's still open somewhere.
            try {
                if (window.Notification && Notification.permission === 'default') {
                    Notification.requestPermission();
                }
            } catch (e) {}
        }
        if (soundEnabled) {
            // Sound was already enabled in an earlier session (localStorage
            // persists across reloads). We do NOT auto-recreate the audio
            // context here - a fresh AudioContext made without a real tap
            // stays browser-suspended and silently never plays anything.
            // Instead, show a small always-visible "Sound ON" pill (tapping
            // it re-arms things) AND wire up a page-wide tap-to-resume
            // listener below, so literally any tap anywhere on the
            // dashboard (Refresh, a status button, etc.) is enough to
            // silently un-suspend the audio if it ever goes quiet - staff
            // never have to hunt for a specific button once sound is on.
            document.getElementById('enableSoundBtn').style.display = 'none';
            document.getElementById('soundStatusBtn').style.display = 'block';
        }

        // Any tap anywhere on the page counts as a user gesture - use it to
        // silently resume the audio context if it has gone suspended. This
        // is the real fix for "sound just stops working after a while":
        // there is always another chance to recover on the very next tap,
        // without staff needing to find and press a specific button.
        document.addEventListener('click', function () {
            if (soundEnabled) {
                try {
                    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
                    if (audioCtx.state === 'suspended') audioCtx.resume();
                } catch (e) {}
            }
        }, true);

        function beep(times, volume) {
            try {
                if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
                if (audioCtx.state === 'suspended') audioCtx.resume();
                for (var i = 0; i < times; i++) {
                    (function (i) {
                        setTimeout(function () {
                            var o = audioCtx.createOscillator();
                            var g = audioCtx.createGain();
                            o.type = 'square';
                            o.frequency.value = 880;
                            g.gain.value = volume;
                            o.connect(g); g.connect(audioCtx.destination);
                            o.start();
                            setTimeout(function () { o.stop(); }, 350);
                        }, i * 500);
                    })(i);
                }
            } catch (e) {}
        }

        function announceNewOrder() {
            beep(4, 0.9);
            try {
                var msg = new SpeechSynthesisUtterance('New order received! New order received!');
                msg.volume = 1; msg.rate = 1;
                speechSynthesis.speak(msg);
            } catch (e) {}
        }

        // The alarm keeps ringing on a repeat timer - every ~4 seconds -
        // until a human explicitly taps Stop inside this tab. It does NOT
        // stop on its own, does not stop when the tab loses focus, and does
        // not stop just because another order poll succeeds - only the
        // Stop button (or the banner) clears it. This is deliberate: the
        // point is that staff can be anywhere (another app, another room
        // within earshot of the speaker) and the alarm won't go quiet on
        // its own before someone has actually seen the order.
        function startAlarm(orderId) {
            var btn = document.getElementById('stopAlarmBtn');
            btn.setAttribute('data-order-id', orderId);
            btn.disabled = false;
            btn.textContent = 'ORDER #' + orderId + ' RECEIVED! - tap to confirm to customer & stop alarm';
            document.getElementById('newOrderBanner').style.display = 'block';
            btn.style.display = 'block';
            alarmActive = true;

            if (soundEnabled) {
                announceNewOrder();
                if (alarmRepeatInterval) clearInterval(alarmRepeatInterval);
                alarmRepeatInterval = setInterval(function () {
                    if (alarmActive) announceNewOrder();
                }, 4000);
            }

            if (!titleFlashInterval) {
                titleFlashInterval = setInterval(function () {
                    document.title = titleFlashOn ? originalTitle : 'NEW ORDER! - Tandoori Junction';
                    titleFlashOn = !titleFlashOn;
                }, 1000);
            }

            try {
                if (window.Notification && Notification.permission === 'granted') {
                    var n = new Notification('New order received - Tandoori Junction', {
                        body: 'Order #' + orderId + ' - open the dashboard to view it. Tap Stop Alarm on the dashboard once handled.',
                        requireInteraction: true,
                        tag: 'tandoori-order-' + orderId
                    });
                    n.onclick = function () { try { window.focus(); } catch (e) {} };
                }
            } catch (e) {}
        }

        function stopAlarm() {
            alarmActive = false;
            if (alarmRepeatInterval) { clearInterval(alarmRepeatInterval); alarmRepeatInterval = null; }
            if (titleFlashInterval) { clearInterval(titleFlashInterval); titleFlashInterval = null; }
            document.title = originalTitle;
            document.getElementById('newOrderBanner').style.display = 'none';
            document.getElementById('stopAlarmBtn').style.display = 'none';
            try { speechSynthesis.cancel(); } catch (e) {}
        }

        // Staff must explicitly acknowledge a new order before the alarm goes
        // quiet - tapping the alarm bar/button does NOT just silence it
        // locally. It first calls the backend to send the customer a "your
        // order is received" WhatsApp message, and only stops ringing once
        // that call actually succeeds. If it fails (network blip etc.) the
        // alarm keeps ringing and the button re-enables so staff can retry -
        // this guarantees a customer is never silently left without
        // confirmation just because someone tapped the button once.
        var ackInFlight = false;
        function confirmOrderReceived() {
            if (ackInFlight) return;
            var btn = document.getElementById('stopAlarmBtn');
            var orderId = btn.getAttribute('data-order-id');
            if (!orderId) { stopAlarm(); return; }
            ackInFlight = true;
            btn.disabled = true;
            btn.textContent = 'Confirming with customer...';
            fetchWithTimeout('/order/' + orderId + '/acknowledge', {
                method: 'POST',
                headers: { 'X-Requested-With': 'fetch' }
            }, 8000).then(function (r) { return r.json(); }).then(function (data) {
                ackInFlight = false;
                btn.disabled = false;
                if (!data.ok) throw new Error(data.error || 'failed');
                stopAlarm();
            }).catch(function () {
                ackInFlight = false;
                btn.disabled = false;
                btn.textContent = 'Could not notify customer - tap to retry';
            });
        }

        function setConnStatus(ok) {
            var el = document.getElementById('connStatus');
            var now = new Date();
            var t = now.toLocaleTimeString();
            if (ok) {
                el.className = 'ok';
                el.textContent = 'Live - synced ' + t;
                document.getElementById('staleWarning').style.display = 'none';
            } else {
                el.className = 'bad';
                el.textContent = 'Reconnecting... (' + consecutiveFailures + ')';
                if (consecutiveFailures >= 3) {
                    document.getElementById('staleWarning').style.display = 'block';
                }
            }
        }

        function fetchWithTimeout(url, opts, ms) {
            opts = opts || {};
            var controller = new AbortController();
            var id = setTimeout(function () { controller.abort(); }, ms || 8000);
            opts.signal = controller.signal;
            return fetch(url, opts).finally(function () { clearTimeout(id); });
        }

        function renderStats(d) {
            document.getElementById('statTotal').textContent = d.total_count;
            document.getElementById('statPending').textContent = d.pending_count;
            document.getElementById('statDispatched').textContent = d.dispatched_count;
            document.getElementById('statDelivered').textContent = d.delivered_count;
            document.getElementById('statTodayCount').textContent = d.today_orders.order_count;
            document.getElementById('statTodayTotal').textContent = 'Rs' + d.today_orders.day_total;
        }

        function statusClass(s) { return (s || 'Pending').toLowerCase(); }

        function renderOrders(orders) {
            var container = document.getElementById('ordersContainer');
            if (!orders || orders.length === 0) {
                container.innerHTML = '<div class="no-orders"><p>No orders yet!</p></div>';
                return;
            }
            var html = '';
            for (var i = 0; i < orders.length; i++) {
                var o = orders[i];
                var status = o.order_status || 'Pending';
                html += '<div class="order-card" data-order-id="' + o.id + '">';
                html += '<div class="order-header"><span class="order-id">Order #' + o.id + '</span>';
                html += '<span class="order-time">' + escapeHtml(o.timestamp) + '</span></div>';
                html += '<div class="order-phone">Phone: ' + escapeHtml(o.phone) + '</div>';
                html += '<div class="order-text">' + escapeHtml(o.order_text) + '</div>';
                html += '<div class="order-footer"><span class="total">' + escapeHtml(o.total) + '</span>';
                html += '<span class="status status-' + statusClass(status) + '">' + escapeHtml(status) + '</span>';
                if (o.location && o.location !== 'Not shared') {
                    html += '<a class="location" href="' + escapeHtml(o.location) + '" target="_blank">View Location</a>';
                }
                html += '</div>';
                if (o.alert_status && ['delivered', 'read'].indexOf(o.alert_status) === -1) {
                    html += '<div class="alert-hint">Owner alert not yet confirmed delivered (' + escapeHtml(o.alert_status) + ', ' + o.alert_retries + ' retries) - auto-retrying.</div>';
                }
                html += '<div class="status-btns">';
                html += '<button type="button" class="status-btn ' + (status === 'Pending' ? 'active p' : '') + '" data-order-id="' + o.id + '" data-status="Pending">Pending</button>';
                html += '<button type="button" class="status-btn ' + (status === 'Dispatched' ? 'active d' : '') + '" data-order-id="' + o.id + '" data-status="Dispatched">Dispatched</button>';
                html += '<button type="button" class="status-btn ' + (status === 'Delivered' ? 'active v' : '') + '" data-order-id="' + o.id + '" data-status="Delivered">Delivered</button>';
                html += '</div></div>';
            }
            container.innerHTML = html;
        }

        function renderDaily(dailyList) {
            var container = document.getElementById('dailyContainer');
            if (!dailyList || dailyList.length === 0) {
                container.innerHTML = '<div class="no-orders"><p>No orders yet!</p></div>';
                return;
            }
            var html = '<table><tr><th>Date</th><th>Orders</th><th>Total Collected</th></tr>';
            for (var i = 0; i < dailyList.length; i++) {
                var d = dailyList[i];
                html += '<tr><td>' + escapeHtml(d.date) + '</td><td>' + d.order_count + '</td><td>Rs' + d.day_total + '</td></tr>';
            }
            html += '</table>';
            container.innerHTML = html;
        }

        // Event delegation: one listener handles every status button, even
        // ones that get created later by renderOrders() re-rendering the
        // container - so this never needs re-attaching.
        document.getElementById('ordersContainer').addEventListener('click', function (e) {
            var btn = e.target.closest('.status-btn');
            if (!btn) return;
            var orderId = btn.getAttribute('data-order-id');
            var status = btn.getAttribute('data-status');
            var group = btn.parentElement.querySelectorAll('.status-btn');
            group.forEach(function (b) { b.disabled = true; });
            fetchWithTimeout('/order/' + orderId + '/status', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'X-Requested-With': 'fetch'
                },
                body: 'status=' + encodeURIComponent(status)
            }, 8000).then(function (r) {
                if (!r.ok) throw new Error('bad response');
                return refreshDashboard();
            }).catch(function () {
                group.forEach(function (b) { b.disabled = false; });
                alert('Could not update status - check connection and try again.');
            });
        });

        // Single source of truth for "has this order id already triggered the
        // alarm". Both polling loops below funnel through this instead of
        // touching lastSeenId directly, so whichever one notices a new order
        // first is guaranteed to actually ring the alarm for it.
        function maybeAlarm(id) {
            if (id && id > lastSeenId) {
                lastSeenId = id;
                localStorage.setItem('lastSeenOrderId', String(lastSeenId));
                startAlarm(id);
                return true;
            }
            return false;
        }

        function refreshDashboard() {
            return fetchWithTimeout('/api/dashboard_data', {}, 8000).then(function (r) { return r.json(); }).then(function (data) {
                if (!data.ok) throw new Error('server reported error');
                consecutiveFailures = 0;
                setConnStatus(true);
                renderStats(data);
                renderOrders(data.orders);
                renderDaily(data.daily_list);
                maybeAlarm(data.latest_order_id);
            }).catch(function (e) {
                consecutiveFailures++;
                setConnStatus(false);
            });
        }

        function trySelfHealAudio() {
            // Best-effort, silent - resume() outside a gesture will simply
            // no-op/reject in some browsers, which is fine; this just gives
            // the audio a chance to recover on its own every poll cycle
            // in addition to the tap-anywhere listener above.
            if (soundEnabled && audioCtx && audioCtx.state === 'suspended') {
                audioCtx.resume().catch(function () {});
            }
        }

        function checkNewOrders() {
            trySelfHealAudio();
            fetchWithTimeout('/api/latest_order_id', {}, 8000).then(function (r) { return r.json(); }).then(function (data) {
                if (!data.ok) throw new Error('server reported error');
                consecutiveFailures = 0;
                setConnStatus(true);
                if (maybeAlarm(data.id)) {
                    refreshDashboard();
                }
            }).catch(function (e) {
                consecutiveFailures++;
                setConnStatus(false);
            });
        }

        async function requestWakeLock() {
            try {
                if ('wakeLock' in navigator) {
                    wakeLock = await navigator.wakeLock.request('screen');
                }
            } catch (e) { /* not supported / denied - fine, not critical */ }
        }
        requestWakeLock();

        document.addEventListener('visibilitychange', function () {
            if (document.visibilityState === 'visible') {
                if (audioCtx && audioCtx.state === 'suspended') audioCtx.resume();
                requestWakeLock();
                checkNewOrders();
            }
        });

        setInterval(checkNewOrders, 5000);
        setInterval(refreshDashboard, 30000); // safety-net sync so status changes made on another device still show up here
        refreshDashboard();
    </script>
</body>
</html>
    """, orders=orders, daily_list=daily_list, today_orders=today_orders, pending_count=pending_count,
         dispatched_count=dispatched_count, delivered_count=delivered_count, total_count=total_count,
         latest_order_id=(orders[0]["id"] if orders else 0))

@app.route("/order/<int:order_id>/acknowledge", methods=["POST"])
@require_dashboard_auth
def acknowledge_order(order_id):
    """Fires when staff taps the alarm button on the dashboard to silence
    it. Deliberately NOT the same thing as the automatic "Order Confirmed"
    message the bot already sends the instant a customer places an order -
    this one specifically tells the customer the restaurant has actually
    seen and is acting on it, and it only gets sent once a human has taken
    that action, never automatically."""
    try:
        with get_db() as conn:
            row = _q(conn, "SELECT phone FROM orders WHERE id=?", (order_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "order not found"}, 200
        phone = row["phone"]
        msg = "Your order has been received by our kitchen and is being prepared! - Tandoori Junction"
        wamid = send_meta_message(phone, msg)
        if not wamid:
            return {"ok": False, "error": "message send failed"}, 200
        return {"ok": True, "order_id": order_id}
    except Exception as e:
        print(f"acknowledge_order error: {e}")
        return {"ok": False, "error": str(e)}, 200

@app.route("/order/<int:order_id>/status", methods=["POST"])
@require_dashboard_auth
def update_order_status(order_id):
    new_status = request.form.get("status", "Pending")
    if new_status not in ("Pending", "Dispatched", "Delivered"):
        new_status = "Pending"
    is_ajax = request.headers.get("X-Requested-With") == "fetch"
    try:
        with get_db() as conn:
            _q(conn, "UPDATE orders SET order_status=? WHERE id=?", (new_status, order_id))
            conn.commit()
    except Exception as e:
        print(f"update_order_status error: {e}")
        if is_ajax:
            return {"ok": False, "error": str(e)}, 200
        return ("", 303, {"Location": "/dashboard"})
    if is_ajax:
        return {"ok": True, "order_id": order_id, "status": new_status}
    return ("", 303, {"Location": "/dashboard"})

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == META_VERIFY_TOKEN:
            print("Webhook verified!")
            return challenge, 200
        return "Verification failed", 403

    data = request.get_json(silent=True) or {}

    try:
        value = data["entry"][0]["changes"][0]["value"]

        # Delivery-status updates (sent/delivered/read/failed) for owner alerts
        for status_event in value.get("statuses", []):
            handle_status_update(status_event)

        messages = value.get("messages", [])
        if not messages:
            return "ok", 200

        message = messages[0]
        phone = message.get("from", "")
        msg_type = message.get("type", "")

        # WhatsApp retries webhook delivery if we don't respond fast enough
        # (very possible on this free tier's cold starts) - skip anything
        # we've already handled so the customer never gets double-processed.
        if _already_processed(message.get("id")):
            return "ok", 200

        incoming_msg = ""
        latitude = None
        longitude = None

        if msg_type == "text":
            incoming_msg = message.get("text", {}).get("body", "").strip()
        elif msg_type == "location":
            loc = message.get("location", {})
            latitude = loc.get("latitude")
            longitude = loc.get("longitude")
            incoming_msg = "[location]"
        else:
            # Sticker/image/audio/document/etc - let the customer know we
            # noticed instead of going silent, which otherwise looks like
            # the bot is broken.
            if phone:
                send_meta_message(phone, "Hume abhi sirf text messages ya location samajh aati hai. Order karne ke liye item ka naam likhein, ya MENU likhein!")
            return "ok", 200

    except (KeyError, IndexError):
        return "ok", 200

    print(f"From {phone}: {incoming_msg}")

    if phone not in sessions:
        sessions[phone] = new_session()

    session = sessions[phone]
    reply = ""

    # Handle location
    if latitude and longitude:
        session["location"] = f"https://maps.google.com/?q={latitude},{longitude}"
        history_append(session, "user", "[customer shared their delivery location]")
        if session["cart"]:
            cart_text = format_cart(session["cart"])
            session["last_order"] = cart_text
            reply = f"Location mil gayi!\n\n{cart_text}\n\nConfirm karna hai? HAAN likhein"
        else:
            reply = "Location mil gayi!\n\nOrder confirm karna hai? HAAN likhein"
        session["stage"] = "confirming"

    else:
        stripped_msg = incoming_msg.strip()
        numeric_pair = re.match(r'^(\d+)\s+(\d+)$', stripped_msg)
        numeric_single = re.match(r'^(\d+)$', stripped_msg)

        # Every customer turn goes into the rolling history - including ones
        # handled by the deterministic fast paths below - so the agent always
        # sees the true conversation when it's next consulted.
        history_append(session, "user", incoming_msg)

        # --- Fast, free, deterministic path: picking an item by number while browsing a category ---
        if session["stage"] == "subcategory" and (numeric_pair or numeric_single) and stripped_msg != "0":
            cat = CATEGORIES.get(session["current_category"])
            items_list = cat["items_list"] if cat else []
            if numeric_pair:
                item_num, qty = int(numeric_pair.group(1)), min(MAX_ITEM_QUANTITY, int(numeric_pair.group(2)))
            else:
                item_num, qty = int(numeric_single.group(1)), 1

            if cat and 1 <= item_num <= len(items_list):
                item_name, item_price = items_list[item_num - 1]
                add_to_cart(session, item_name, item_price, qty)
                reply = f"{item_name} x{qty} cart mein add!\n\nAur add karna hai? Item number ya naam likhein.\nCART - cart dekhein\n0 - wapas menu pe"
            else:
                reply = f"Invalid number! 1 se {len(items_list)} ke beech likhein, ya item ka naam bhi likh sakte hain."

        # --- Fast, free, deterministic path: picking a top-level category by digit ---
        elif numeric_single and stripped_msg in ["1", "2", "3", "4", "5", "6"] and session["stage"] != "subcategory":
            reply = show_category(session, stripped_msg)

        # --- Explicit "0" = back to menu, from anywhere ---
        elif stripped_msg == "0":
            reply = CATEGORY_MENU
            session["stage"] = "menu"
            session["current_category"] = None

        # --- Agent path: the LLM reasons over the full conversation and decides what to do ---
        else:
            parsed = run_conversation_agent(session, incoming_msg)

            if parsed is None:
                # Groq unavailable - fall back to simple keyword matching so the bot stays responsive
                intent = detect_intent(incoming_msg)
                print(f"Stage: {session['stage']}, Intent (fallback): {intent}")
                reply = legacy_intent_reply(session, phone, incoming_msg, intent)

            else:
                action_types = [a.get("type") for a in (parsed.get("actions") or []) if isinstance(a, dict)]
                print(f"Stage: {session['stage']}, LLM actions: {action_types}")
                reply = execute_agent_actions(session, phone, parsed)

    # Record what we actually sent. Note: if an order was just finalized,
    # sessions[phone] is a fresh session - the confirmation lands in the new
    # history so the next conversation starts with correct context.
    history_append(sessions.get(phone) or session, "assistant", reply)
    send_meta_message(phone, reply)
    return "ok", 200

@app.route("/privacy")
def privacy():
    return render_template_string("""
<!DOCTYPE html>
<html>
<head>
    <title>Privacy Policy - Tandoori Junction</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: Arial, sans-serif; max-width: 720px; margin: 40px auto; padding: 0 20px; line-height: 1.6; color: #222; }
        h1 { color: #e74c3c; }
        h2 { margin-top: 32px; font-size: 18px; }
        p, li { color: #333; }
    </style>
</head>
<body>
    <h1>Privacy Policy</h1>
    <p><strong>Tandoori Junction</strong> ("we", "us", "our") operates a WhatsApp ordering assistant to help customers browse our menu and place food orders. This policy explains what information we collect through that service and how we use it.</p>

    <h2>Information we collect</h2>
    <ul>
        <li>Your WhatsApp phone number, so we can respond to your messages and process your order.</li>
        <li>The contents of the messages you send us (e.g. menu selections, quantities).</li>
        <li>Your delivery location, only if you choose to share it with us via WhatsApp's location-sharing feature.</li>
    </ul>

    <h2>How we use this information</h2>
    <ul>
        <li>To take and confirm your food order.</li>
        <li>To arrange delivery to the address or location you provide.</li>
        <li>To contact you about the status of your order.</li>
    </ul>

    <h2>How we store this information</h2>
    <p>Order details are stored in a private database used only by Tandoori Junction staff to fulfil orders. We do not sell or share your information with third parties for marketing purposes.</p>

    <h2>Third parties</h2>
    <p>Messages are sent and received using Meta's WhatsApp Business Platform (Cloud API). Meta's own privacy policy also applies to how they handle message transport: <a href="https://www.whatsapp.com/legal/privacy-policy">https://www.whatsapp.com/legal/privacy-policy</a></p>

    <h2>Your choices</h2>
    <p>You can stop receiving messages from us at any time by no longer messaging our WhatsApp number, or by asking us to delete your order history.</p>

    <h2>Contact us</h2>
    <p>{{ restaurant_name }}<br>
    {{ restaurant_address }}<br>
    Phone: {{ restaurant_phone }}</p>
</body>
</html>
    """, restaurant_name=os.environ.get("RESTAURANT_NAME", "Tandoori Junction"),
         restaurant_address=os.environ.get("RESTAURANT_ADDRESS", ""),
         restaurant_phone=os.environ.get("RESTAURANT_PHONE", ""))

@app.route("/reset")
def reset():
    sessions.clear()
    return "All sessions reset!"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
