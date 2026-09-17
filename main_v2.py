import asyncio
import os
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

raw_my_id = os.environ.get("MY_TELEGRAM_ID", "")
try:
    MY_TELEGRAM_ID = int(raw_my_id)
except ValueError:
    MY_TELEGRAM_ID = raw_my_id

SESSION_STRING = os.environ.get("SESSION_STRING")

TARGET_CHANNELS = [
    "@war_monitor",
    "@kievreal1",
    "@truexanewsua",
    "@tgalertfilter"
]

# KEYWORDS = [
#    "нивк",            # нивки, нивок, нивкам, нивках
#    "баліст",          # балістика, балістична, балістику
#    "циркон",          # циркон, циркони
#    "київ кр",         # київ кр
#    "кр київ",         # кр київ
#    "киев кр", 
#    "кр киев",
#    "пуск кр",
#    "спуск баліст"
#]

KEYWORDS = [
    "загроза балістики",
    "київ — спуск балістики",
    "нивки",
    "нивок",
    "Циркон на Київ",
    "Циркони Київ",
    "Київ КР",
    "КР Київ",
    "Загроза балістики",
    "Циркони",
    "Циркони спуск на Київ",
    "Київ — спуск балістики",
    "Київ — спуск балістики",
    "Київ - спуск балістики",
    "вектор Київ",
    "Зліт МІГ",
    "Кинджал",
    "Пуски Кинджала",
    "Ракета на Київ",
    "на Київ",
    "Балістик на Київ",
    "Ракета на Київ",
    "Спуск",
    " б"
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

def send_telegram_reply(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": MY_TELEGRAM_ID,
        "text": text
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Reply err: {e}", flush=True)

def update_heartbeat():
    global HEARTBEAT_MESSAGE_ID
    now_kyiv = (datetime.utcnow() + timedelta(hours=3)).strftime("%H:%M")
    text = f"🟢 {now_kyiv}"
    
    if HEARTBEAT_MESSAGE_ID is None:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": MY_TELEGRAM_ID,
            "text": text,
            "disable_notification": True
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
    while True:
        update_heartbeat()
        await asyncio.sleep(600)

def is_alert_triggered(text_raw):
    if not text_raw:
        return False
        
    text_lower = text_raw.lower()
    
    for kw in KEYWORDS:
        if kw in text_lower:
            return True
            
    return False

async def process_message(event):
    message_text = event.message.message if event.message else event.raw_text
    if not message_text:
        return

    chat = await event.get_chat()
    chat_username = f"@{chat.username}" if getattr(chat, 'username', None) else None
    
    # Проверка: является ли текущий чат тестовым каналом @tgalertfilter
    target_str = str(MY_TELEGRAM_ID).lower()
    is_test_channel = (chat_username and chat_username.lower() == "@tgalertfilter") or (str(event.chat_id) == target_str)

    if is_test_channel:
        if is_alert_triggered(message_text):
            send_telegram_reply("тест ок")
        return

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
