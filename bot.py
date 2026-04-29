import gevent
import os
import json
import logging
import hashlib
import random
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
BACKEND_URL = os.getenv("BACKEND_URL")

# ========== LOGGING ==========
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot")

# ========== FLASK ==========
app = Flask(__name__)
CORS(app)

# ========== REQUESTS SESSION ==========
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

# ========== ХРАНЕНИЕ ЗАКАЗОВ ==========
orders = {}

# ========== HELPERS ==========


def safe_send_message(chat_id, text, **kwargs):
    """Безопасная отправка сообщения с игнорированием ошибок."""
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")
        return None


def generate_token(data: dict, password: str) -> str:
    """
    Генерация токена (подписи) по документации Т-Банка.
    Учитывает, что булевы JSON-значения приводятся к "true"/"false".
    """
    pairs = {}
    for key, value in data.items():
        if isinstance(value, (dict, list)):
            continue
        if isinstance(value, bool):
            pairs[key] = "true" if value else "false"
        else:
            pairs[key] = str(value)

    pairs["Password"] = password

    sorted_keys = sorted(pairs.keys())
    concat = "".join(pairs[k] for k in sorted_keys)

    return hashlib.sha256(concat.encode("utf-8")).hexdigest()


# ========== PAYMENT ==========

@app.route('/init-payment', methods=['POST'])
def init_payment():
    logger.info("💳 INIT PAYMENT")

    body = request.json
    customer_phone = body.get("phone") or "79999999999"
    success_url = body.get("success_url")
    fail_url = body.get("fail_url")
    order_data = body.get("order_data")
    logger.info(f"Order data received: {order_data}")
    logger.info(f"Customer ID: {order_data.get('customer', {}).get('telegram_id')}")
    order_id = str(random.randint(1, 15000))
    AMOUNT = 1000  # копейки

    payload = {
        "TerminalKey": TERMINAL_KEY,
        "Amount": AMOUNT,
        "OrderId": order_id,
        "Description": "Football Dream Team",
        "NotificationURL": f"{BACKEND_URL}/payment-notification",
        "Receipt": {
            "Phone": customer_phone,
            "Taxation": "usn_income",
            "Items": [
                {
                    "Name": "Футбольный состав",
                    "Price": AMOUNT,
                    "Quantity": 1,
                    "Amount": AMOUNT,
                    "Tax": "none"
                }
            ]
        }
    }

    if success_url:
        payload["SuccessURL"] = success_url
    if fail_url:
        payload["FailURL"] = fail_url

    payload["Token"] = generate_token(payload, PASSWORD)

    try:
        resp = session.post(TINKOFF_INIT_URL, json=payload, timeout=5)
        data = resp.json()

        orders[order_id] = {
            "status": "pending",
            "data": order_data
        }

        return jsonify({
            "PaymentURL": data.get("PaymentURL"),
            "order_id": order_id
        })
    except Exception as e:
        logger.error(f"❌ PAYMENT ERROR: {e}")
        return jsonify({"error": str(e)}), 500


# ========== WEBHOOK ОПЛАТЫ ==========
@app.route('/payment-notification', methods=['POST'])
def payment_notification():
    data = request.json
    logger.info(f"💰 PAYMENT NOTIFICATION: {data}")

    if not data:
        return "OK", 200

    received_token = data.get("Token")
    if not received_token:
        logger.warning("❌ No Token in notification")
        return "OK", 200

    # Исключаем Token из данных для подписи
    sign_data = {k: v for k, v in data.items() if k != "Token"}
    generated_token = generate_token(sign_data, PASSWORD)

    logger.info(f"Received token: {received_token}")
    logger.info(f"Generated token: {generated_token}")
    logger.info(f"Sign data: {sign_data}")

    if received_token != generated_token:
        logger.warning("❌ INVALID TOKEN")
        return "OK", 200

    status = data.get("Status")
    order_id = data.get("OrderId")

    logger.info(f"Current orders keys: {list(orders.keys())}")
    if not order_id or order_id not in orders:
        logger.warning(f"❌ Order {order_id} not found")
        return "OK", 200

    if status == "CONFIRMED":
        order = orders[order_id]["data"]

        if not order:
            logger.warning("Order data is empty")
            return "OK", 200

        customer = order.get("customer", {})
        logger.info(f"Customer data: {customer}")
        logger.info(f"Customer telegram_id: {customer.get('telegram_id')}")
        players = order.get("players", [])

        customer_text = (
            f"{customer.get('surname', '')} "
            f"{customer.get('name', '')} "
            f"{customer.get('patronymic', '')}"
        ).strip()

        players_text = "\n".join(
            [f"• {p.get('position')}: {p.get('name')}" for p in players]
        )

        message_text = (
            f"📦 <b>Новый заказ №{order_id}</b>\n\n"
            f"📅 {order.get('order_date', '—')}\n"
            f"⚽ {order.get('team', '—')}\n\n"
            f"👤 {customer_text}\n"
            f"📱 {customer.get('telegram') or '—'}\n"
            f"🆔 {customer.get('telegram_id')}\n"
            f"📞 {customer.get('phone', '—')}\n"
            f"📍 {customer.get('address', '—')}\n\n"
            f"{players_text}"
        )

        chat_id = customer.get("telegram_id")

        if chat_id:
            safe_send_message(
                chat_id,
                message_text,
                parse_mode="HTML"
            )

        if ADMIN_CHAT_ID:
            safe_send_message(
                ADMIN_CHAT_ID,
                message_text,
                parse_mode="HTML"
            )

    return "OK", 200


@app.route('/get-dadata-token', methods=['GET'])
def get_dadata_token():
    return jsonify({"token": DADATA_TOKEN})


@app.route('/config', methods=['GET'])
def get_config():
    return jsonify({
        "dadataToken": DADATA_TOKEN,
        "backendUrl": os.getenv("BACKEND_URL"),
        "terminalKey": TERMINAL_KEY
    })


# ========== TELEGRAM PROCESSORS ==========

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
            f"✅ Заказ №{order_id} успешно оформлен!\n\n"
            f"📩 При возникновении вопросов напишите в поддержку.\n\n"
            f"Спасибо за выбор Fantasy XI 🫶",
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


def process_unknown_message(message):
    try:
        chat_id = message['chat']['id']
        safe_send_message(
            chat_id,
            "Используйте кнопку меню или команду /start для начала работы"
        )
    except Exception as e:
        logger.error(f"❌ unknown message error: {e}")


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

    if 'web_app_data' in message:
        gevent.spawn(process_webapp_data, message)
    elif 'text' in message and message.get('text') == '/start':
        gevent.spawn(process_start, message)
    else:
        gevent.spawn(process_unknown_message, message)

    return jsonify({'ok': 'True'})


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
