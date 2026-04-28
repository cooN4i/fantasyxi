import os
import json
import logging
import hashlib
import requests

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
from concurrent.futures import ThreadPoolExecutor
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

# ========== THREAD POOL (КЛЮЧЕВОЕ УСКОРЕНИЕ) ==========
executor = ThreadPoolExecutor(max_workers=10)

# ========== REQUESTS SESSION (CONNECTION POOL + RETRY) ==========
session = requests.Session()

retry = Retry(
    total=3,
    backoff_factor=0.3,
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

# ========== TELEGRAM BOT ==========
bot = telebot.TeleBot(TOKEN, threaded=False)
telebot.apihelper.session = session

# ========== HELPERS ==========


def safe_send_message(chat_id, text, **kwargs):
    """Telegram send with retry-safe session"""
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")
        return None


def async_telegram_task(fn, *args, **kwargs):
    """Запуск Telegram задач в фоне"""
    executor.submit(fn, *args, **kwargs)


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
        data = resp.json()
        return jsonify({"PaymentURL": data.get("PaymentURL")})
    except Exception as e:
        logger.error(f"❌ PAYMENT ERROR: {e}")
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


# ========== TELEGRAM PROCESSORS (ASYNC) ==========
def process_webapp_data(message):
    try:
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

        if ADMIN_CHAT_ID:
            safe_send_message(
                ADMIN_CHAT_ID,
                admin_message,
                parse_mode="HTML"
            )

        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton(
            "📩 Написать в поддержку",
            url="https://t.me/kylo_gg"
        ))

        safe_send_message(
            chat_id,
            f"✅ Заказ №{order_id} успешно оформлен!\n\n📩 При возникновении вопросов напишите в поддержку.\n\nСпасибо за выбор Fantasy XI 🫶",
            parse_mode="HTML",
            reply_markup=markup
        )

    except Exception as e:
        logger.error(f"❌ web_app_data error: {e}")


def process_start(message):
    try:
        chat_id = message['chat']['id']

        markup = ReplyKeyboardMarkup(resize_keyboard=True)
        web_app = WebAppInfo(url="https://fantasyxi.abrdns.com/")
        button = KeyboardButton(
            text="⚽ Открыть конструктор",
            web_app=web_app
        )
        markup.add(button)

        safe_send_message(
            chat_id,
            "👋 Привет!\n\n"
            "Добро пожаловать в Fantasy Constructor - бот для создания футбольных составов.\n\n"
            "⬇️ Нажми на кнопку, чтобы открыть конструктор и собрать свою команду.",
            reply_markup=markup
        )

    except Exception as e:
        logger.error(f"❌ start error: {e}")


# ========== WEBHOOK ==========
@app.route('/webhook', methods=['POST'])
def webhook():
    update_json = request.get_json(silent=True)

    if not update_json:
        return jsonify({'ok': True})

    logger.info("🔥 WEBHOOK RECEIVED")

    message = update_json.get('message')
    if not message:
        return jsonify({'ok': True})

    # ⚡ ВАЖНО: ВСЁ УХОДИТ В ФОН
    if 'web_app_data' in message:
        executor.submit(process_webapp_data, message)

    elif 'text' in message and message.get('text') == '/start':
        executor.submit(process_start, message)

    return jsonify({'ok': True})  # ⚡ мгновенный ответ Telegram


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
