import sys
sys.stdout.reconfigure(encoding='utf-8')

from flask import Flask, request, render_template_string
from groq import Groq
from datetime import datetime
from dotenv import load_dotenv
import os
import re
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
        conn.commit()

init_db()

def save_order(phone, order_text, total, location):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            INSERT INTO orders (timestamp, phone, order_text, total, location, payment_status)
            VALUES (?, ?, ?, ?, ?, 'Pending')
        """, (timestamp, phone, order_text, total, location or "Not shared"))
        conn.commit()

def send_meta_message(to_phone, text):
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
            return True
        else:
            print(f"Meta send FAILED ({response.status_code}): {response.text}")
            return False
    except Exception as e:
        print(f"Meta send error: {e}")
        return False

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

@app.route("/dashboard")
def dashboard():
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        orders = conn.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()

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
        .stat-card { background: white; border-radius: 10px; padding: 20px; flex: 1; min-width: 150px; text-align: center; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        .stat-card h2 { font-size: 32px; color: #e74c3c; }
        .stat-card p { color: #666; font-size: 14px; }
        .orders-section { padding: 0 20px 20px; }
        .order-card { background: white; border-radius: 10px; padding: 15px; margin-bottom: 15px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); border-left: 4px solid #e74c3c; }
        .order-header { display: flex; justify-content: space-between; margin-bottom: 10px; }
        .order-id { font-weight: bold; color: #e74c3c; }
        .order-time { color: #999; font-size: 13px; }
        .order-phone { color: #333; font-size: 14px; margin-bottom: 8px; }
        .order-text { background: #f9f9f9; padding: 10px; border-radius: 5px; font-size: 13px; color: #444; margin-bottom: 8px; white-space: pre-wrap; }
        .order-footer { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; }
        .total { font-weight: bold; color: #27ae60; font-size: 16px; }
        .status { padding: 4px 10px; border-radius: 20px; font-size: 12px; font-weight: bold; background: #fff3cd; color: #856404; }
        .location { color: #3498db; font-size: 13px; text-decoration: none; }
        .no-orders { text-align: center; padding: 50px; color: #999; }
        .refresh-btn { background: #e74c3c; color: white; border: none; padding: 10px 20px; border-radius: 5px; cursor: pointer; margin-bottom: 15px; float: right; }
    </style>
</head>
<body>
    <div class="header">
        <h1>Tandoori Junction Dashboard</h1>
        <p>Order Management</p>
    </div>
    <div class="stats">
        <div class="stat-card">
            <h2>{{ orders|length }}</h2>
            <p>Total Orders</p>
        </div>
        <div class="stat-card">
            <h2>{{ orders|selectattr('payment_status', 'equalto', 'Pending')|list|length }}</h2>
            <p>Pending</p>
        </div>
    </div>
    <div class="orders-section">
        <h2 style="padding: 0 0 15px;">Recent Orders</h2>
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
                    <span class="status">{{ order['payment_status'] }}</span>
                    {% if order['location'] and order['location'] != 'Not shared' %}
                    <a class="location" href="{{ order['location'] }}" target="_blank">View Location</a>
                    {% endif %}
                </div>
            </div>
            {% endfor %}
        {% else %}
            <div class="no-orders"><p>No orders yet!</p></div>
        {% endif %}
    </div>
    <script>setTimeout(() => location.reload(), 30000);</script>
</body>
</html>
    """, orders=orders)

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
        messages = value.get("messages", [])
        if not messages:
            return "ok", 200

        message = messages[0]
        phone = message.get("from", "")
        msg_type = message.get("type", "")

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
        intent = detect_intent(incoming_msg)
        print(f"Stage: {session['stage']}, Intent: {intent}")

        # Greeting
        if intent == "greeting" or session["stage"] == "new":
            reply = """Tandoori Junction mein swagat hai!
Good Food - Good Mood - Great Times

Main Riya hoon, aapki help ke liye!

MENU likhein menu dekhne ke liye
Ya seedha order kar sakte hain!"""
            session["stage"] = "welcome"

        # Menu
        elif intent == "menu":
            reply = CATEGORY_MENU
            session["stage"] = "menu"
            session["current_category"] = None

        # Back to menu
        elif intent == "back":
            reply = CATEGORY_MENU
            session["stage"] = "menu"
            session["current_category"] = None

        # Main category selected
        elif intent == "category" or (session["stage"] == "menu" and incoming_msg in ["1","2","3","4","5","6"]):
            cat = CATEGORIES.get(incoming_msg)
            if cat:
                session["current_category"] = incoming_msg
                reply = f"{cat['name']}\n\n{cat['display']}\n\nItem number aur quantity likhein!\nJaise: 3 2 (item 3, quantity 2)\nYa sirf number (quantity 1 hogi)\n\nCART - cart dekhein\n0 - wapas menu pe"
                session["stage"] = "subcategory"
            else:
                reply = CATEGORY_MENU
                session["stage"] = "menu"

        # Subcategory item selection
        elif session["stage"] == "subcategory":
            cat = CATEGORIES.get(session["current_category"])

            if intent == "cart":
                if session["cart"]:
                    reply = f"{format_cart(session['cart'])}\n\nAur add karna hai? Item number likhein.\nLocation share karein delivery ke liye."
                else:
                    reply = "Cart empty hai! Pehle kuch add karein."

            elif intent == "menu":
                reply = CATEGORY_MENU
                session["stage"] = "menu"
                session["current_category"] = None

            elif cat:
                items_list = cat["items_list"]
                added = False

                # Format: "3 2" = item 3, qty 2
                match = re.match(r'^(\d+)\s+(\d+)$', incoming_msg.strip())
                if match:
                    item_num = int(match.group(1))
                    qty = int(match.group(2))
                    if 1 <= item_num <= len(items_list):
                        item_name, item_price = items_list[item_num - 1]
                        session["cart"].append({"name": item_name, "price": item_price, "qty": qty})
                        reply = f"{item_name} x{qty} cart mein add!\n\nAur add karna hai? Item number likhein.\nCART - cart dekhein\n0 - wapas menu pe"
                        added = True
                    else:
                        reply = f"Invalid number! 1 se {len(items_list)} ke beech likhein."
                        added = True

                # Format: "3" = item 3, qty 1
                if not added:
                    match2 = re.match(r'^(\d+)$', incoming_msg.strip())
                    if match2:
                        item_num = int(match2.group(1))
                        if 1 <= item_num <= len(items_list):
                            item_name, item_price = items_list[item_num - 1]
                            session["cart"].append({"name": item_name, "price": item_price, "qty": 1})
                            reply = f"{item_name} x1 cart mein add!\n\nAur add karna hai? Item number likhein.\nCART - cart dekhein\n0 - wapas menu pe"
                            added = True
                        else:
                            reply = f"Invalid number! 1 se {len(items_list)} ke beech likhein."
                            added = True

                if not added:
                    reply = f"Number likhein item select karne ke liye!\n\n{cat['display']}\n\nCART - cart dekhein\n0 - wapas menu pe"

        # Cart checkout
        elif intent == "cart":
            if session["cart"]:
                reply = f"{format_cart(session['cart'])}\n\nDelivery ke liye location share karein!\nWhatsApp mein attachment > Location > Send location"
            else:
                reply = "Cart empty hai! MENU likhein order karne ke liye."

        # FAQ
        elif intent == "faq":
            reply = """Tandoori Junction
Nayatola, Kalyani Road, Maharajpur
Sahibganj 816109

Timings: 10 AM - 10 PM
Phone: 9523087860
Home Delivery available"""

        # Confirm order
        elif intent == "confirm" and session["stage"] == "confirming":
            total_match = re.search(r'TOTAL:\s*Rs(\d+)', session["last_order"])
            total = f"Rs{total_match.group(1)}" if total_match else "N/A"

            save_order(phone, session["last_order"], total, session["location"])

            alert_msg = (
                f"NEW ORDER - Tandoori Junction\n\n"
                f"Customer: {phone}\n"
                f"Order:\n{session['last_order']}\n"
                f"Total: {total}\n"
                f"Location: {session['location'] or 'Not shared'}\n"
                f"Time: {datetime.now().strftime('%I:%M %p')}\n\n"
                f"Call customer to confirm delivery!"
            )

            send_meta_message(OWNER_NUMBER, alert_msg)

            reply = """Order Confirmed - Tandoori Junction!

Aapka order place ho gaya!
Delivery time: 30-45 minutes
Humare staff aapko call karenge

Shukriya!"""

            sessions[phone] = new_session()

        # Cancel
        elif intent == "cancel":
            reply = "Koi baat nahi! MENU likhein dobara order karne ke liye."
            session["stage"] = "welcome"

        # Fallback
        else:
            reply = "MENU likhein menu dekhne ke liye ya seedha order karein!"
            session["stage"] = "welcome"

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