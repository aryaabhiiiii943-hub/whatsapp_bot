# Restaurant WhatsApp Ordering MVP

This is a one-restaurant MVP for taking local food orders through WhatsApp, recording every order in SQLite, and alerting the shopkeeper.

## What it includes

- WhatsApp webhook endpoint for Twilio: `/webhook`
- Menu-backed order parsing and server-side price calculation
- SQLite database for customers, orders, order items, menu items, and chat sessions
- Shopkeeper dashboard: `/dashboard`
- Order status updates: pending, accepted, preparing, out_for_delivery, completed, cancelled
- Optional Groq integration for better natural-language order extraction

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python app.py
```

Open:

```text
http://127.0.0.1:5000/dashboard
```

## Twilio setup

In Twilio WhatsApp Sandbox or WhatsApp sender settings, set the incoming message webhook to:

```text
https://YOUR_PUBLIC_URL/webhook
```

For local testing, expose your Flask server with a tunnel like ngrok:

```powershell
ngrok http 5000
```

Then use the generated HTTPS URL as the webhook base.

## Meta WhatsApp Cloud API setup

Use this for the demo if Twilio sandbox limits block replies.

1. Go to Meta for Developers and create/select an app.
2. Add the WhatsApp product.
3. In WhatsApp > API Setup, copy:
   - Phone number ID
   - Temporary or permanent access token
   - Test recipient setup instructions
4. In this project `.env`, set:

```env
WHATSAPP_PROVIDER=meta
META_API_VERSION=v20.0
META_PHONE_NUMBER_ID=your_phone_number_id
META_ACCESS_TOKEN=your_access_token
META_VERIFY_TOKEN=restaurant-order-bot
OWNER_WHATSAPP_NUMBER=91your_owner_number
```

5. Run the app and ngrok:

```powershell
python app.py
ngrok http 5000
```

6. In Meta webhook configuration, use:

```text
Callback URL: https://YOUR-NGROK-URL.ngrok-free.app/webhook
Verify token: restaurant-order-bot
```

7. Subscribe the webhook to WhatsApp messages.

For Meta test mode, add your own WhatsApp number as a test recipient before sending messages.

## Demo messages

```text
hi
menu
2 chicken biryani aur 1 butter naan
yes confirm
```

The app will save the order and alert the owner when the customer confirms.
