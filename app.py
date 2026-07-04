import sys
sys.stdout.reconfigure(encoding='utf-8')

from flask import Flask, request, render_template_string
from groq import Groq
from datetime import datetime, timedelta
from dotenv import load_dotenv
import os
import re
import json
import difflib
import sqlite3
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

DB_FILE = os.path.join(os.path.dirname(__file__), "orders.db")

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                phone TEXT,
                order_text TEXT,
                total TEXT,
                location TEXT,
                payment_status TEXT DEFAULT 'Pending'
            )
        """)
        # Safe, idempotent migrations for existing databases that predate these columns.
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(orders)")}
        migrations = {
            "order_status": "ALTER TABLE orders ADD COLUMN order_status TEXT DEFAULT 'Pending'",
            "alert_wamid": "ALTER TABLE orders ADD COLUMN alert_wamid TEXT",
            "alert_status": "ALTER TABLE orders ADD COLUMN alert_status TEXT DEFAULT 'none'",
            "alert_retries": "ALTER TABLE orders ADD COLUMN alert_retries INTEGER DEFAULT 0",
            "alert_last_sent": "ALTER TABLE orders ADD COLUMN alert_last_sent TEXT",
        }
        for col, ddl in migrations.items():
            if col not in existing_cols:
                conn.execute(ddl)
        conn.commit()

init_db()

def save_order(phone, order_text, total, location):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.execute("""
            INSERT INTO orders (timestamp, phone, order_text, total, location, payment_status, order_status, alert_status)
            VALUES (?, ?, ?, ?, ?, 'Pending', 'Pending', 'none')
        """, (timestamp, phone, order_text, total, location or "Not shared"))
        conn.commit()
        return cur.lastrowid

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
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM orders
                WHERE alert_status IN ('sent', 'failed')
                  AND alert_retries < 3
                  AND (alert_last_sent IS NULL OR alert_last_sent < ?)
            """, (cutoff,)).fetchall()

        for row in rows:
            wamid = send_meta_message(OWNER_NUMBER, build_order_alert_text(row, is_reminder=True))
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with sqlite3.connect(DB_FILE) as conn:
                conn.execute(
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
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("UPDATE orders SET alert_status=? WHERE alert_wamid=?", (status, wamid))
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

# Flat lookup of every menu item (built once at startup) used by the
# LLM-based order parser to resolve fuzzy / broken-language item names
# back to an exact item + price.
ITEM_LOOKUP = {}
for _cat_num, _cat in CATEGORIES.items():
    for _name, _price in _cat["items_list"]:
        ITEM_LOOKUP[_name.lower()] = {"name": _name, "price": _price, "category": _cat_num}

def _build_menu_reference():
    lines = []
    for cat_num, cat in CATEGORIES.items():
        item_names = ", ".join(name for name, _ in cat["items_list"])
        lines.append(f"Category {cat_num} - {cat['name']}: {item_names}")
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

def new_session():
    return {
        "history": [],
        "location": None,
        "last_order": "",
        "stage": "new",
        "current_category": None,
        "cart": []
    }

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

ORDER_PARSE_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": ["greeting", "menu", "category", "order", "clear_cart", "cart", "confirm", "cancel", "faq", "back", "unknown"]
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
        },
        "clear_cart_first": {"type": "boolean"},
        "clarification_message": {"type": "string"}
    },
    "required": ["intent", "category_number", "items", "clear_cart_first", "clarification_message"],
    "additionalProperties": False
}

def parse_message_with_llm(incoming_msg, stage, current_category, cart):
    """Use Groq to understand free-form / broken-language / Hinglish customer
    messages: direct item orders, category requests by name, confirmations,
    etc. Returns a dict matching ORDER_PARSE_SCHEMA, or None if the call fails
    (caller should fall back to the simple keyword matcher)."""
    cart_summary = ", ".join(f"{i['name']} x{i['qty']}" for i in cart) or "empty"
    current_cat_name = CATEGORIES.get(current_category, {}).get("name", "none")

    system_prompt = f"""You are the order-understanding brain for an Indian restaurant's WhatsApp bot (Tandoori Junction). Customers write in English, Hindi, Hinglish, or broken/misspelled language. Understand their intent, and if they're ordering, match what they say to the EXACT item names from the menu below (fix typos, understand Hinglish, be forgiving).

MENU:
{MENU_REFERENCE_TEXT}

Conversation state: stage={stage}, current_category={current_cat_name}, current_cart={cart_summary}

Return strict JSON only, following this logic:
- intent "category": customer wants to browse/see a specific category (by name or number), even mentioned casually (e.g. "chinese kuch dikhao", "pizza hai kya", "biryani wala menu")
- intent "order": customer is EXPLICITLY naming specific food item(s) they want, with or without quantity (e.g. "2 chicken biryani aur ek paneer tikka", "mujhe butter naan chahiye")
- intent "clear_cart": customer wants to empty/reset their cart without necessarily ordering anything new (e.g. "cart clear karo", "sab hata do", "cart khali karo")
- intent "cart": customer wants to see their cart/total
- intent "confirm": customer is agreeing / saying yes / confirming something (e.g. "haan", "yes", "ha", "confirm", "confirm karo", "done", "bilkul", "sahi hai", "ok kar do", "theek hai kar do"). IMPORTANT: whenever stage is "confirming" (the bot just asked the customer to confirm their order), ANY short affirmative reply - including just "ok", "haan", "yes", "theek hai", "thik hai", "bas kar do" - MUST be classified as "confirm", not "unknown".
- intent "cancel": customer is saying no / wants to cancel / start over (e.g. "nahi", "no", "nhi", "cancel")
- intent "faq": asking about address, timings, phone, delivery
- intent "back": wants to go back to the main category menu
- intent "greeting": hi/hello/namaste etc with no other content
- intent "unknown": ONLY use this when stage is NOT "confirming" and the message is a vague filler/acknowledgment (e.g. "ok", "thik hai", "bas", "accha", "hmm") that doesn't fit any intent above and isn't a reply to a yes/no question

CRITICAL ANTI-HALLUCINATION RULE: Only ever put something in "items" if the customer's message EXPLICITLY names that specific food item (or an unambiguous typo/Hinglish version of it). NEVER invent, assume, or guess items the customer did not actually mention, even if they are real menu items and even if the conversation state suggests they might want more food. A vague/filler message like "thik hai", "bas", "ok", "done" with no food words in it, sent when stage is NOT "confirming", MUST be classified as intent "unknown" with an EMPTY items array - it is NOT an order.

category_number: "1"-"6" matching the MENU above if intent is "category", else ""
items: array of {{name, quantity}} using EXACT item names copied from the MENU above, only if intent is "order" AND those exact items were named in the message. Default quantity to 1 if not specified. If you can't confidently match a named item, omit it from items and explain in clarification_message instead.
clear_cart_first: true if the customer's wording implies REPLACING their current order rather than adding to it (e.g. "sirf X aur kuch nahi", "only X", "bas itna hi chahiye", "cart clear karke X daal do") - this clears the existing cart before adding the new items. Also set true whenever intent is "clear_cart". Otherwise false.
clarification_message: a short, friendly Hinglish message ONLY if the message is genuinely ambiguous, is a filler/unknown message, or an item couldn't be matched confidently; otherwise an empty string"""

    try:
        completion = groq_client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": incoming_msg}
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "order_parse",
                    "strict": True,
                    "schema": ORDER_PARSE_SCHEMA
                }
            },
            temperature=0,
            max_completion_tokens=500
        )
        return json.loads(completion.choices[0].message.content)
    except Exception as e:
        print(f"LLM parse error: {e}")
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
    added_lines = []
    not_found = []
    for it in items:
        resolved = resolve_item(it.get("name", ""))
        try:
            qty = max(1, min(MAX_ITEM_QUANTITY, int(it.get("quantity") or 1)))
        except (TypeError, ValueError):
            qty = 1
        if resolved:
            session["cart"].append({"name": resolved["name"], "price": resolved["price"], "qty": qty})
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

    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()

    wamid = send_meta_message(OWNER_NUMBER, build_order_alert_text(row))
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
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

@app.route("/dashboard")
def dashboard():
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        orders = conn.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()

    def parse_total(t):
        try:
            return int(str(t).replace("Rs", "").strip())
        except (TypeError, ValueError):
            return 0

    # Every order is its own independent, individually-billed record - a customer
    # ordering twice in one day produces two separate rows with two separate totals,
    # never a running/lifetime balance. Daily summary just counts+sums per calendar day.
    daily = {}
    for o in orders:
        day = (o["timestamp"] or "")[:10] or "Unknown"
        d = daily.setdefault(day, {"date": day, "order_count": 0, "day_total": 0})
        d["order_count"] += 1
        d["day_total"] += parse_total(o["total"])
    daily_list = sorted(daily.values(), key=lambda d: d["date"], reverse=True)

    today_str = datetime.now().strftime("%Y-%m-%d")
    today_orders = daily.get(today_str, {"order_count": 0, "day_total": 0})

    pending_count = sum(1 for o in orders if (o["order_status"] or "Pending") == "Pending")
    dispatched_count = sum(1 for o in orders if o["order_status"] == "Dispatched")
    delivered_count = sum(1 for o in orders if o["order_status"] == "Delivered")

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
        .header { background: #e74c3c; color: white; padding: 20px; text-align: center; }
        .header h1 { font-size: 24px; }
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
        table { width: 100%; border-collapse: collapse; background: white; border-radius: 10px; overflow: hidden; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        th, td { text-align: left; padding: 12px 15px; font-size: 13px; border-bottom: 1px solid #eee; }
        th { background: #fafafa; color: #666; }
    </style>
</head>
<body>
    <div class="header">
        <h1>Tandoori Junction Dashboard</h1>
        <p>Order Management</p>
    </div>
    <div class="stats">
        <div class="stat-card"><h2>{{ orders|length }}</h2><p>Total Orders</p></div>
        <div class="stat-card"><h2>{{ pending_count }}</h2><p>Pending</p></div>
        <div class="stat-card"><h2>{{ dispatched_count }}</h2><p>Dispatched</p></div>
        <div class="stat-card"><h2>{{ delivered_count }}</h2><p>Delivered</p></div>
        <div class="stat-card"><h2>{{ today_orders['order_count'] }}</h2><p>Today's Orders</p></div>
        <div class="stat-card"><h2>Rs{{ today_orders['day_total'] }}</h2><p>Today's Collection</p></div>
    </div>
    <div class="section">
        <h2>Recent Orders</h2>
        <button class="refresh-btn" onclick="location.reload()">Refresh</button>
        <div style="clear:both"></div>
        {% if orders %}
            {% for order in orders %}
            <div class="order-card">
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
                    <form method="POST" action="/order/{{ order['id'] }}/status" style="display:inline;">
                        <input type="hidden" name="status" value="Pending">
                        <button type="submit" class="status-btn {{ 'active p' if (order['order_status'] or 'Pending') == 'Pending' else '' }}">Pending</button>
                    </form>
                    <form method="POST" action="/order/{{ order['id'] }}/status" style="display:inline;">
                        <input type="hidden" name="status" value="Dispatched">
                        <button type="submit" class="status-btn {{ 'active d' if order['order_status'] == 'Dispatched' else '' }}">Dispatched</button>
                    </form>
                    <form method="POST" action="/order/{{ order['id'] }}/status" style="display:inline;">
                        <input type="hidden" name="status" value="Delivered">
                        <button type="submit" class="status-btn {{ 'active v' if order['order_status'] == 'Delivered' else '' }}">Delivered</button>
                    </form>
                </div>
            </div>
            {% endfor %}
        {% else %}
            <div class="no-orders"><p>No orders yet!</p></div>
        {% endif %}
    </div>
    <div class="section">
        <h2>Daily Summary</h2>
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
    <script>setTimeout(() => location.reload(), 30000);</script>
</body>
</html>
    """, orders=orders, daily_list=daily_list, today_orders=today_orders, pending_count=pending_count,
         dispatched_count=dispatched_count, delivered_count=delivered_count)

@app.route("/order/<int:order_id>/status", methods=["POST"])
def update_order_status(order_id):
    new_status = request.form.get("status", "Pending")
    if new_status not in ("Pending", "Dispatched", "Delivered"):
        new_status = "Pending"
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("UPDATE orders SET order_status=? WHERE id=?", (new_status, order_id))
        conn.commit()
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
                session["cart"].append({"name": item_name, "price": item_price, "qty": qty})
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

        # --- Smart path: understand natural language / broken language / direct item or category names ---
        else:
            parsed = parse_message_with_llm(incoming_msg, session["stage"], session["current_category"], session["cart"])

            if parsed is None:
                # Groq unavailable - fall back to simple keyword matching so the bot stays responsive
                intent = detect_intent(incoming_msg)
                print(f"Stage: {session['stage']}, Intent (fallback): {intent}")
                reply = legacy_intent_reply(session, phone, incoming_msg, intent)

            else:
                intent = parsed.get("intent", "unknown")
                print(f"Stage: {session['stage']}, LLM intent: {intent}")

                if intent == "greeting" or session["stage"] == "new":
                    reply = GREETING_TEXT
                    session["stage"] = "welcome"

                elif intent in ("menu", "back"):
                    reply = CATEGORY_MENU
                    session["stage"] = "menu"
                    session["current_category"] = None

                elif intent == "category":
                    cat_num = parsed.get("category_number") or ""
                    if CATEGORIES.get(cat_num):
                        reply = show_category(session, cat_num)
                    else:
                        clarification = parsed.get("clarification_message") or "Konsi category chahiye, yeh samajh nahi aaya."
                        reply = f"{clarification}\n\n{CATEGORY_MENU}"
                        session["stage"] = "menu"

                elif intent == "order":
                    items = parsed.get("items") or []
                    if parsed.get("clear_cart_first"):
                        session["cart"] = []
                    if items:
                        reply = add_items_and_reply(session, items)
                    elif parsed.get("clear_cart_first"):
                        reply = "Cart clear kar diya! Ab kya order karna hai? Item ka naam likhein."
                    else:
                        reply = parsed.get("clarification_message") or "Kya order karna hai? Item ka naam likhein, jaise 'chicken biryani' ya 'paneer tikka 2'."

                elif intent == "clear_cart":
                    session["cart"] = []
                    reply = "Cart clear kar diya! Ab kya order karna hai? Item ka naam likhein, ya MENU likhein dekhne ke liye."

                elif intent == "cart":
                    reply = render_cart_reply(session)

                elif intent == "confirm":
                    if session["stage"] == "confirming":
                        reply = finalize_order(session, phone)
                    elif session["cart"]:
                        reply = f"{format_cart(session['cart'])}\n\nOrder confirm karne ke liye pehle apni location share karein!\nWhatsApp mein attachment > Location > Send location"
                    else:
                        reply = "Cart abhi empty hai! Pehle kuch order karein - item ka naam likhein."

                elif intent == "cancel":
                    reply = "Koi baat nahi! Cart clear kar diya. MENU likhein dobara order karne ke liye."
                    session["stage"] = "welcome"
                    session["cart"] = []
                    session["current_category"] = None

                elif intent == "faq":
                    reply = FAQ_TEXT

                else:
                    reply = parsed.get("clarification_message") or "Samajh nahi aaya! MENU likhein dekhne ke liye, ya seedha item ka naam bhejein jaise 'chicken biryani'."

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