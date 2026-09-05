import asyncio
import os
import threading
from flask import Flask
from telethon import TelegramClient, events
import requests

# Инициализация Flask для поддержки активности (Render + cron-job)
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running 24/7!", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# Данные авторизации из переменных окружения
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MY_TELEGRAM_ID = int(os.environ.get("MY_TELEGRAM_ID"))

# Список ключевых слов для отслеживания (в нижнем регистре)
KEYWORDS = ["алерт", "тревога", "срочно", "внимание", "skynex"]

# Список отслеживаемых каналов (username или ID)
TARGET_CHANNELS = ["@dtek_kem"]

client = TelegramClient('user_session', API_ID, API_HASH)

def send_telegram_alert(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": MY_TELEGRAM_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Ошибка отправки сообщения: {e}")

@client.on(events.NewMessage(chats=TARGET_CHANNELS))
async def handle_new_message(event):
    message_text = event.raw_text
    text_lower = message_text.lower()
    
    if any(keyword in text_lower for keyword in KEYWORDS):
        alert_msg = f"<b>🚨 Найдено совпадение!</b>\n\n{message_text}"
        send_telegram_alert(alert_msg)

async def start_telethon():
    await client.start()
    print("Telethon клиент запущен и отслеживает каналы...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    # Запуск Flask в отдельном потоке
    threading.Thread(target=run_flask, daemon=True).start()
    
    # Запуск Telethon
    asyncio.run(start_telethon())
