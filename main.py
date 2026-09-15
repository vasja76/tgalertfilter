import asyncio
import os
import re
import threading
from datetime import datetime, timedelta
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

TARGET_CHANNELS = [
    "@war_monitor",
    "@kievreal1",
    "@truexanewsua"
    # "@tgalertfilter"
]

client = TelegramClient(
    StringSession(SESSION_STRING), 
    API_ID, 
    API_HASH,
    connection_retries=5,
    retry_delay=3,
    auto_reconnect=True
)

HEARTBEAT_MESSAGE_ID = None

def normalize_text(text):
    if not text:
        return ""
    return re.sub(r'[^\w\s]', ' ', text.lower())

def is_alert_triggered(text_raw):
    if not text_raw:
        return False
        
    text = normalize_text(text_raw)
    
    # 1. Проверка локации Нивки (учитываем любые окончания)
    if any(word in text for word in ["нивки", "нивок", "нивкам", "нивках"]):
        return True

    # 2. Угроза баллистики
    if "загроза" in text and "баліст" in text:
        return True
        
    # 3. Киев + спуск + баллистика
    if ("київ" in text or "києв" in text) and "спуск" in text and "баліст" in text:
        return True

    # 4. Киев + Циркон
    if ("київ" in text or "києв" in text) and "циркон" in text:
        return True

    # 5. Киев + Крылатые ракеты (КР)
    if ("київ" in text or "києв" in text) and (" кр " in f" {text} " or "кр!" in text_raw.lower() or "кр." in text_raw.lower()):
        return True

    return False

def forward_telegram_message(from_chat_id, message_id):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/forwardMessage"
    payload = {
        "chat_id": MY_TELEGRAM_ID,
        "from_chat_id": from_chat_id,
        "message_id": message_id
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Forward err: {e}", flush=True)

def update_heartbeat(tick_count):
    global HEARTBEAT_MESSAGE_ID
    now_kyiv = (datetime.utcnow() + timedelta(hours=3)).strftime("%H:%M")
    text = f"🟢 {now_kyiv}"
    
    if HEARTBEAT_MESSAGE_ID is None:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": MY_TELEGRAM_ID,
            "text": text,
            "disable_notification": True  # Первый пульс отправляется без звука
        }
        try:
            res = requests.post(url, json=payload, timeout=10).json()
            if res.get("ok"):
                HEARTBEAT_MESSAGE_ID = res["result"]["message_id"]
        except Exception as e:
            print(f"Pulse err: {e}", flush=True)
    else:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
        payload = {
            "chat_id": MY_TELEGRAM_ID,
            "message_id": HEARTBEAT_MESSAGE_ID,
            "text": text
        }
        try:
            res = requests.post(url, json=payload, timeout=10).json()
            if not res.get("ok"):
                HEARTBEAT_MESSAGE_ID = None
        except Exception as e:
            print(f"Pulse err: {e}", flush=True)

async def heartbeat_loop():
    tick = 0
    while True:
        tick += 1
        update_heartbeat(tick)
        await asyncio.sleep(600)

async def process_message(event):
    message_text = event.message.message if event.message else event.raw_text
    
    if is_alert_triggered(message_text):
        forward_telegram_message(event.chat_id, event.id)

@client.on(events.NewMessage(chats=TARGET_CHANNELS))
async def handle_new_message(event):
    await process_message(event)

@client.on(events.MessageEdited(chats=TARGET_CHANNELS))
async def handle_edited_message(event):
    await process_message(event)

async def start_telethon():
    print("Запуск Telethon...", flush=True)
    try:
        await client.start()
        print("Telethon запущен!", flush=True)
        asyncio.create_task(heartbeat_loop())
        await client.run_until_disconnected()
    except Exception as e:
        print(f"Telethon err: {e}", flush=True)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.run(start_telethon())
