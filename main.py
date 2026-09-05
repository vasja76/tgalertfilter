import asyncio
import os
import threading
from flask import Flask
from telethon import TelegramClient, events
from telethon.sessions import StringSession
import requests

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running 24/7!", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MY_TELEGRAM_ID = int(os.environ.get("MY_TELEGRAM_ID"))
SESSION_STRING = os.environ.get("SESSION_STRING")

KEYWORDS = [
    "загроза балістики",
    "київ — спуск балістики",
    "нивки",
    "нивок"
]

TARGET_CHANNELS = [
    "@war_monitor",
    "@kievreal1",
    "@truexanewsua",
    "@tgalertfilter"
]

client = TelegramClient(
    StringSession(SESSION_STRING), 
    API_ID, 
    API_HASH,
    connection_retries=5,     # Количество попыток переподключения
    retry_delay=3,            # Задержка между попытками в секундах
    auto_reconnect=True       # Автоматическое переподключение
)

def send_telegram_alert(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": MY_TELEGRAM_ID,
        "text": text
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Ошибка отправки: {e}", flush=True)

@client.on(events.NewMessage(chats=TARGET_CHANNELS))
async def handle_new_message(event):
    message_text = event.raw_text
    text_lower = message_text.lower()
    
    if any(keyword in text_lower for keyword in KEYWORDS):
        chat = await event.get_chat()
        username = getattr(chat, 'username', None)
        
        # Если есть юзернейм — используем его, если нет — короткое имя
        channel_id = f"@{username}" if username else getattr(chat, 'title', 'Канал')
        
        alert_msg = f">>{channel_id}\n{message_text}"
        send_telegram_alert(alert_msg)

async def start_telethon():
    print("Запуск Telethon клиента...", flush=True)
    await client.start()
    print("Telethon успешно запущен и прослушивает каналы!", flush=True)
    await client.run_until_disconnected()

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.run(start_telethon())
