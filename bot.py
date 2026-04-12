import os
import json
import logging
import hashlib
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
import telebot
from telebot.types import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    WebAppInfo,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

# ---------------- CONFIG ----------------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", 0))

TERMINAL_KEY = os.getenv("TERMINAL_KEY")
PASSWORD = os.getenv("TINKOFF_PASSWORD")
TINKOFF_INIT_URL = os.getenv("TINKOFF_INIT_URL")

# DaData токен (будет отдаваться фронтенду)
DADATA_TOKEN = os.getenv("DADATA_API_KEY")

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)
CORS(app)  # Включаем CORS для всех маршрутов

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------- PAYMENT ----------------

def generate_token(data: dict, password: str):
    data_for_token = {}

    for k, v in data.items():
        if isinstance(v, dict) or isinstance(v, list):
            continue
        data_for_token[k] = v

    data_for_token["Password"] = password

    sorted_items = sorted(data_for_token.items())
    concat = "".join(str(v) for k, v in sorted_items)

    return hashlib.sha256(concat.encode()).hexdigest()


@app.route('/init-payment', methods=['POST'])
def init_payment():
    logger.info("💳 INIT PAYMENT CALLED")
    body = request.json
    logger.info(f"📦 body: {body}")

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
        response = requests.post(TINKOFF_INIT_URL, json=payload)
        data = response.json()
        logger.info(f"💰 Tinkoff response: {data}")

        return jsonify({"PaymentURL": data.get("PaymentURL")})
    except Exception as e:
        logger.error(f"❌ PAYMENT ERROR: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/get-dadata-token', methods=['GET'])
def get_dadata_token():
    """Отдаёт токен DaData для фронтенда"""
    return jsonify({"token": DADATA_TOKEN})


@app.route('/config', methods=['GET'])
def get_config():
    """Отдаёт публичные настройки для фронтенда"""
    return jsonify({
        "dadataToken": DADATA_TOKEN,
        "backendUrl": os.getenv("BACKEND_URL", "https://football-dreamteam.onrender.com")
    })


# ---------------- WEBHOOK ----------------

@app.route('/webhook', methods=['POST'])
def webhook():
    logger.info("🔥 WEBHOOK RECEIVED")
    update_json = request.get_json(silent=True)
    logger.info(f"📦 Full update_json: {update_json}")

    if not update_json:
        return jsonify({'ok': True})

    web_app_data = None
    chat_id = None

    if 'message' in update_json and 'web_app_data' in update_json['message']:
        web_app_data = update_json['message']['web_app_data']['data']
        chat_id = update_json['message']['chat']['id']
        logger.info("✅ web_app_data найден в message")
    elif 'callback_query' in update_json and 'message' in update_json['callback_query'] and 'web_app_data' in update_json['callback_query']['message']:
        web_app_data = update_json['callback_query']['message']['web_app_data']['data']
        chat_id = update_json['callback_query']['message']['chat']['id']
        logger.info("✅ web_app_data найден в callback_query")

    if web_app_data:
        try:
            data = json.loads(web_app_data)
            logger.info(f"📊 Parsed data: {data}")

            order_id = data.get("order_id", "—")
            order_date = data.get("order_date", "—")
            team = data.get("team", "—")
            customer = data.get("customer", {})
            players = data.get("players", [])

            from_user = update_json.get('message', {}).get('from', {})
            tg_id = customer.get("telegram_id") or chat_id
            tg_username = customer.get("telegram")
            if not tg_username and from_user.get("username"):
                tg_username = "@" + from_user.get("username")

            customer_text = (
                f"{customer.get('surname', '')} "
                f"{customer.get('name', '')} "
                f"{customer.get('patronymic', '')}"
            ).strip()

            telegram_line = tg_username if tg_username else "не указан"
            telegram_id_line = tg_id if tg_id else "не указан"

            players_text = "\n".join(
                [f"• {p.get('position')}: {p.get('name')}" for p in players]
            )

            admin_message = (
                f"📦 <b>Новый заказ №{order_id}</b>\n\n"
                f"📅 <b>Дата:</b> {order_date}\n\n"
                f"⚽ <b>Команда:</b> {team}\n\n"
                f"👤 <b>Клиент:</b>\n"
                f"{customer_text}\n"
                f"📱 {telegram_line}\n"
                f"🆔 {telegram_id_line}\n"
                f"📞 {customer.get('phone', '—')}\n"
                f"📍 {customer.get('address', '—')}\n\n"
                f"🧩 <b>Состав:</b>\n{players_text}"
            )

            if ADMIN_CHAT_ID:
                bot.send_message(ADMIN_CHAT_ID, admin_message,
                                 parse_mode="HTML")
                logger.info("✅ Отправлено админу")

            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton(
                "📩 Написать в поддержку", url="https://t.me/kylo_gg"))

            bot.send_message(
                chat_id,
                f"✅ <b>Спасибо за заказ!</b>\n\n📦 №{order_id}",
                parse_mode="HTML",
                reply_markup=markup
            )
        except Exception as e:
            logger.error(f"❌ Ошибка обработки: {e}")

    try:
        update = Update.de_json(update_json)
        bot.process_new_updates([update])
    except Exception as e:
        logger.error(f"❌ Update error: {e}")

    return jsonify({'ok': True})


# ---------------- START ----------------

@bot.message_handler(commands=['start'])
def start(message):
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    web_app = WebAppInfo(url="https://fantasyxi.abrdns.com/constructor.html")
    button = KeyboardButton(text="⚽ Открыть конструктор", web_app=web_app)
    markup.add(button)
    bot.send_message(
        message.chat.id, 
        "Нажмите кнопку ниже 👇\n\n⚠️ ВНИМАНИЕ! После загрузки мини-приложения необходимо отключить VPN для нормальной работы",
        reply_markup=markup
    )


# ---------------- HEALTH ----------------

@app.route('/health', methods=['GET'])
def health():
    return "OK", 200


@app.route('/')
def index():
    return jsonify({"status": "ok", "message": "Football Dream Team Bot is running"})


# ---------------- WEBHOOK SET ----------------

def set_webhook():
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    logger.info("✅ Webhook set")


# ---------------- MAIN ----------------
if __name__ == "__main__":
    set_webhook()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)