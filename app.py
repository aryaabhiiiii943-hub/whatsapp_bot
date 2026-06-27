from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from groq import Groq
from datetime import datetime
import os
import re
import csv
from dotenv import load_dotenv
load_dotenv()

# Groq setup
groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

# CSV setup
CSV_FILE = r"C:\Users\KIIT\Documents\GitHub\Autonomous-vehicle-perception\automation\orders.csv"

if not os.path.exists(CSV_FILE):
    with open(CSV_FILE, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["Timestamp", "Phone Number", "Order", "Total"])

PRODUCT_CATALOG = """
Available products:

BISCUITS:
- Parle G (Small 100g, Large 200g) - 5rs, 10rs
- Britannia Good Day (Small, Large) - 10rs, 20rs
- Oreo (Small, Large) - 15rs, 30rs

NOODLES:
- Maggi Noodles (Small 70g, Large 140g) - 12rs, 20rs
- Yippee Noodles (Single, Family Pack) - 12rs, 45rs

DRINKS:
- Pepsi (250ml, 500ml, 1L) - 20rs, 35rs, 60rs
- Coca Cola (250ml, 500ml, 1L) - 20rs, 35rs, 60rs
- Frooti (Small, Large) - 10rs, 20rs

SNACKS:
- Lays (Small, Large) - 10rs, 20rs
- Kurkure (Small, Large) - 10rs, 20rs

DAIRY:
- Amul Butter (100g, 500g) - 50rs, 225rs
- Amul Milk (500ml, 1L) - 25rs, 48rs

HOUSEHOLD:
- Surf Excel (Small 200g, Large 500g) - 30rs, 70rs
- Colgate Toothpaste (Small 50g, Large 150g) - 30rs, 75rs
"""

app = Flask(__name__)

# Store conversation history per phone number
conversation_history = {}

@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_msg = request.values.get("Body", "")
    phone = request.values.get("From", "")
    print(f"Received from {phone}: {incoming_msg}")

    # Get or create history for this phone number
    if phone not in conversation_history:
        conversation_history[phone] = [
            {
                "role": "system",
                "content": f"""You are an order taking assistant for a wholesale shop in India.
You talk in Hinglish (Hindi+English mix) only.
Never add English translations in brackets.
Be friendly and conversational like a real shop assistant.
This is a wholesale ordering system. Orders are recorded and dispatched later.
Never discuss payment methods unless shopkeeper brings it up.

Here is the product catalog:
{PRODUCT_CATALOG}

If someone greets you, greet back and ask what they need.
If someone places an order, confirm it clearly in this format:
ORDER RECEIVED:
- [Product name] x [quantity] = [total price]
TOTAL: [total amount]

If something is not in catalog, politely tell them and suggest alternatives.
If someone says nhi/nahi/no to adding more items, confirm the final order and close politely."""
            }
        ]

    # Add new message to history
    conversation_history[phone].append({
        "role": "user",
        "content": incoming_msg
    })

    # Call Groq with full history
    chat = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=conversation_history[phone]
    )
    reply = chat.choices[0].message.content

    # Add reply to history
    conversation_history[phone].append({
        "role": "assistant",
        "content": reply
    })

    # Extract total and save to CSV only if it's an actual order
    # Save every order message to CSV
    total_match = re.search(r'TOTAL:\s*(\d+\s*rs)', reply, re.IGNORECASE)
    total = total_match.group(1) if total_match else "N/A"
    
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(CSV_FILE, 'a', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([timestamp, phone, incoming_msg, total])
    print(f"Order saved to CSV")

    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)

if __name__ == "__main__":
    app.run(debug=False, port=5000)