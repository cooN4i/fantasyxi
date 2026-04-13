import os
import json
import logging
import hashlib
import requests
import time
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

# ========== ENV ==========
load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", 0))

TERMINAL_KEY = os.getenv("TERMINAL_KEY")
PASSWORD = os.getenv("TINKOFF_PASSWORD")
TINKOFF_INIT_URL = os.getenv("TINKOFF_INIT_URL")

DADATA_TOKEN = os.getenv("DADATA_API_KEY")

# ========== BOT ==========
bot = telebot.TeleBot(TOKEN, threaded=False)

# БОЛЕЕ СТАБИЛЬНЫЕ TIMEOUT (важно)
telebot.apihelper.CONNECT_TIMEOUT = 10
telebot.apihelper.READ_TIMEOUT = 10

# ========== FLASK ==========
app = Flask(__name__)
CORS(app)

# ========== LOGGING ==========
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== TELEGRAM QUEUE SYSTEM ==========
telegram_queue = Queue()


def telegram_worker():
    """Фоновый воркер Telegram отправки"""
    while True:
        job = telegram_queue.get()
        try:
            job()
        except Exception as e:
            logger.error(f"❌ Telegram worker error: {e}")
        finally:
            telegram_queue.task_done()


threading.Thread(target=telegram_worker, daemon=True).start()


def safe_telegram_send(fn, retries=3):
    """Retry wrapper для Telegram API"""
    def wrapper():
        last_error = None
        for i in range(retries):
            try:
                return fn()
            except Exception as e:
                last_error = e
                logger.warning(f"Telegram retry {i+1}/{retries}: {e}")
                time.sleep(1)
        raise last_error
    return wrapper


def send_async(fn):
    """Кладём задачу в очередь"""
    telegram_queue.put(safe_telegram_send(fn))


# ========== TOKEN ==========
def generate_token(data: dict, password: str):
    data_for_token = {}

    for k, v in data.items():
        if isinstance(v, (dict, list)):
            continue
        data_for_token[k] = v

    data_for_token["Password"] = password
    sorted_items = sorted(data_for_token.items())
    concat = "".join(str(v) for k, v in sorted_items)

    return hashlib.sha256(concat.encode()).hexdigest()


# ========== PAYMENT ==========
@app.route('/init-payment', methods=['POST'])
def init_payment():
    logger.info("💳 INIT PAYMENT")

    body = request.json
    order_id = body.get("order_id")
    amount = body.get("amount", 1000)
    customer_phone = body.get("phone") or "79999999999"

    payload = {
        "TerminalKey": TERMINAL_KEY,
        "Amount": amount,
        "OrderId": str(order_id),
        "Description": "Football Dream Team",
        "Receipt": {
            "Phone": customer_phone,
            "Taxation": "usn_income",
            "Items": [{
                "Name": "Футбольный состав",
                "Price": amount,
                "Quantity": 1,
                "Amount": amount,
                "Tax": "none"
            }]
        }
    }

    payload["Token"] = generate_token(payload, PASSWORD)

    try:
        r = requests.post(TINKOFF_INIT_URL, json=payload, timeout=10)
        data = r.json()
        return jsonify({"PaymentURL": data.get("PaymentURL")})
    except Exception as e:
        logger.error(f"❌ PAYMENT ERROR: {e}")
        return jsonify({"error": str(e)}), 500


# ========== DADATA ==========
@app.route('/get-dadata-token', methods=['GET'])
def get_dadata_token():
    return jsonify({"token": DADATA_TOKEN})


@app.route('/config', methods=['GET'])
def get_config():
    return jsonify({
        "dadataToken": DADATA_TOKEN,
        "backendUrl": os.getenv("BACKEND_URL")
    })


# ========== WEBHOOK ==========
@app.route('/webhook', methods=['POST'])
def webhook():
    update_json = request.get_json(silent=True)

    if not update_json:
        return jsonify({'ok': True})

    logger.info("🔥 WEBHOOK RECEIVED")

    # ===== WEBAPP DATA =====
    if 'message' in update_json and 'web_app_data' in update_json['message']:
        try:
            message = update_json['message']
            chat_id = message['chat']['id']

            data = json.loads(message['web_app_data']['data'])

            order_id = data.get("order_id", "—")
            order_date = data.get("order_date", "—")
            team = data.get("team", "—")
            customer = data.get("customer", {})
            players = data.get("players", [])

            from_user = message.get('from', {})

            tg_id = customer.get("telegram_id") or chat_id
            tg_username = customer.get("telegram") or (
                "@" + from_user.get("username")
                if from_user.get("username") else None
            )

            customer_text = (
                f"{customer.get('surname', '')} "
                f"{customer.get('name', '')} "
                f"{customer.get('patronymic', '')}"
            ).strip()

            players_text = "\n".join(
                [f"• {p.get('position')}: {p.get('name')}" for p in players]
            )

            admin_message = (
                f"📦 <b>Новый заказ №{order_id}</b>\n\n"
                f"📅 {order_date}\n"
                f"⚽ {team}\n\n"
                f"👤 {customer_text}\n"
                f"📱 {tg_username or '—'}\n"
                f"🆔 {tg_id}\n"
                f"📞 {customer.get('phone', '—')}\n"
                f"📍 {customer.get('address', '—')}\n\n"
                f"{players_text}"
            )

            # ===== ADMIN MESSAGE (ASYNC) =====
            if ADMIN_CHAT_ID:
                send_async(lambda: bot.send_message(
                    ADMIN_CHAT_ID,
                    admin_message,
                    parse_mode="HTML"
                ))

            # ===== USER MESSAGE (ASYNC) =====
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton(
                "📩 Написать в поддержку",
                url="https://t.me/kylo_gg"
            ))

            send_async(lambda: bot.send_message(
                chat_id,
                f"✅ <b>Спасибо за заказ!</b>\n\n📦 №{order_id}",
                parse_mode="HTML",
                reply_markup=markup
            ))

        except Exception as e:
            logger.error(f"❌ web_app_data error: {e}")

    # ===== /start =====
    elif 'message' in update_json and 'text' in update_json['message']:
        try:
            message = update_json['message']
            chat_id = message['chat']['id']
            text = message['text']

            if text == '/start':
                markup = ReplyKeyboardMarkup(resize_keyboard=True)
                web_app = WebAppInfo(url="https://fantasyxi.abrdns.com/")
                button = KeyboardButton(
                    text="⚽ Открыть конструктор",
                    web_app=web_app
                )
                markup.add(button)

                send_async(lambda: bot.send_message(
                    chat_id,
                    "Нажмите кнопку ниже 👇",
                    reply_markup=markup
                ))

        except Exception as e:
            logger.error(f"❌ start error: {e}")

    return jsonify({'ok': True})


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
    logger.info("✅ Webhook set")


if __name__ == "__main__":
    set_webhook()
    app.run(host="0.0.0.0", port=10000)
