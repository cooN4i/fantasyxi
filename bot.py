import os
import json
import logging
import hashlib
import requests
import threading

from queue import Queue
from flask import Flask, request, jsonify
from flask_cors import CORS

import telebot
from telebot.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    WebAppInfo,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)

from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ========== ENV ==========
load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", 0))

TERMINAL_KEY = os.getenv("TERMINAL_KEY")
PASSWORD = os.getenv("TINKOFF_PASSWORD")
TINKOFF_INIT_URL = os.getenv("TINKOFF_INIT_URL")

DADATA_TOKEN = os.getenv("DADATA_API_KEY")

# ========== LOGGING ==========
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot")

# ========== FLASK ==========
app = Flask(__name__)
CORS(app)

# ========== REQUEST SESSION ==========
session = requests.Session()

retry = Retry(
    total=2,
    backoff_factor=0.2,
    status_forcelist=[500, 502, 503, 504],
    allowed_methods=["POST", "GET"]
)

adapter = HTTPAdapter(
    max_retries=retry,
    pool_connections=20,
    pool_maxsize=20
)

session.mount("https://", adapter)
session.mount("http://", adapter)

# ========== TELEGRAM ==========
bot = telebot.TeleBot(TOKEN)

# ========== QUEUE SYSTEM ==========
task_queue = Queue()


def worker():
    while True:
        fn, msg = task_queue.get()
        try:
            fn(msg)
        except Exception as e:
            logger.error(f"Worker error: {e}")
        finally:
            task_queue.task_done()


threading.Thread(target=worker, daemon=True).start()

# ========== HELPERS ==========


def safe_send_message(chat_id, text, **kwargs):
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")
        return None


def generate_token(data: dict, password: str):
    clean = {}

    for k, v in data.items():
        if isinstance(v, (dict, list)):
            continue
        clean[k] = v

    clean["Password"] = password
    sorted_items = sorted(clean.items())
    concat = "".join(str(v) for _, v in sorted_items)

    return hashlib.sha256(concat.encode()).hexdigest()


# ========== PAYMENT ==========
@app.route('/init-payment', methods=['POST'])
def init_payment():
    body = request.json
    order_id = body.get("order_id")
    amount = body.get("amount", 1000)
    phone = body.get("phone") or "79999999999"

    payload = {
        "TerminalKey": TERMINAL_KEY,
        "Amount": amount,
        "OrderId": str(order_id),
        "Description": "Football Dream Team",
        "Receipt": {
            "Phone": phone,
            "Taxation": "usn_income",
            "Items": [
                {
                    "Name": "Футбольный состав",
                    "Price": amount,
                    "Quantity": 1,
                    "Amount": amount,
                    "Tax": "none"
                }
            ]
        }
    }

    payload["Token"] = generate_token(payload, PASSWORD)

    try:
        resp = session.post(TINKOFF_INIT_URL, json=payload, timeout=5)
        return jsonify({"PaymentURL": resp.json().get("PaymentURL")})
    except Exception as e:
        logger.error(f"Payment error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/get-dadata-token', methods=['GET'])
def get_dadata_token():
    return jsonify({"token": DADATA_TOKEN})


@app.route('/config', methods=['GET'])
def get_config():
    return jsonify({
        "dadataToken": DADATA_TOKEN,
        "backendUrl": os.getenv("BACKEND_URL")
    })


# ========== BUSINESS LOGIC ==========
def process_webapp_data(message):
    try:
        chat_id = message['chat']['id']
        data = json.loads(message['web_app_data']['data'])

        order_id = data.get("order_id", "—")
        order_date = data.get("order_date", "—")
        team = data.get("team", "—")
        customer = data.get("customer", {})
        players = data.get("players", [])

        tg = message.get('from', {})

        tg_username = customer.get("telegram") or (
            "@" + tg.get("username") if tg.get("username") else None
        )

        customer_text = f"{customer.get('surname', '')} {customer.get('name', '')} {customer.get('patronymic', '')}"

        players_text = "\n".join(
            [f"• {p.get('position')}: {p.get('name')}" for p in players]
        )

        admin_message = (
            f"📦 <b>Новый заказ №{order_id}</b>\n\n"
            f"⚽ {team}\n"
            f"👤 {customer_text}\n"
            f"📱 {tg_username or '—'}\n"
            f"📞 {customer.get('phone', '—')}\n"
            f"📍 {customer.get('address', '—')}\n\n"
            f"{players_text}"
        )

        if ADMIN_CHAT_ID:
            safe_send_message(ADMIN_CHAT_ID, admin_message, parse_mode="HTML")

        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton(
            "📩 Поддержка", url="https://t.me/kylo_gg"))

        safe_send_message(
            chat_id,
            f"✅ Заказ №{order_id} оформлен!",
            reply_markup=markup
        )

    except Exception as e:
        logger.error(f"web_app_data error: {e}")


def process_start(message):
    try:
        chat_id = message['chat']['id']

        markup = ReplyKeyboardMarkup(resize_keyboard=True)
        web_app = WebAppInfo(url="https://fantasyxi.abrdns.com/")
        markup.add(KeyboardButton("⚽ Открыть конструктор", web_app=web_app))

        safe_send_message(
            chat_id,
            "👋 Привет! Открой конструктор команды 👇",
            reply_markup=markup
        )

    except Exception as e:
        logger.error(f"start error: {e}")


# ========== WEBHOOK ==========
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        update = request.get_data(cache=False, as_text=True)

        if not update:
            return "OK"

        message = json.loads(update).get('message')

        if message:
            task_queue.put(message)

    except Exception as e:
        logger.error(f"Webhook error: {e}")

    return "OK"


# ========== HEALTH ==========
@app.route('/health')
def health():
    return "OK", 200


@app.route('/')
def index():
    return jsonify({"status": "ok"})


# ========== WEBHOOK SET ==========
def set_webhook():
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    logger.info("Webhook set")


if __name__ == "__main__":
    set_webhook()
    app.run(host="0.0.0.0", port=10000)
