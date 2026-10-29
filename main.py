
import asyncio
import os
import re
import threading
from datetime import datetime, timezone, timedelta
from flask import Flask
from telethon import TelegramClient, events
from telethon.sessions import StringSession
import requests
from alerts_in_ua import AsyncClient as AlertsClient

# ============================================================
# FLASK — простой веб-сервер для проверки, что приложение работает
# ============================================================

app = Flask(__name__)


@app.route('/')
def home():
    # Render/другой хостинг получает ответ от этого адреса,
    # поэтому приложение считается работающим.
    return "Bot is running 24/7!", 200


def run_flask():
    # Порт берём из переменной окружения.
    # Если переменная PORT отсутствует — используется 10000.
    port = int(os.environ.get("PORT", 10000))

    # Запускаем Flask отдельно от Telethon.
    app.run(host="0.0.0.0", port=port, use_reloader=False)


# ============================================================
# TELEGRAM / ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
# ============================================================

API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")

# BOT_TOKEN используется для отправки уведомлений через Bot API.
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# Telegram ID пользователя, которому отправляются уведомления.
MY_TELEGRAM_ID = int(os.environ.get("MY_TELEGRAM_ID"))

# StringSession пользовательского Telegram-аккаунта для Telethon.
SESSION_STRING = os.environ.get("SESSION_STRING")

# Токен для API воздушных тревог.
ALERTS_API_TOKEN = os.environ.get("ALERTS_API_TOKEN")


# ============================================================
# СООБЩЕНИЕ С КЛЮЧЕВЫМИ СЛОВАМИ В ИЗБРАННОМ
# ============================================================

# ID управляющего сообщения в Избранном Telegram.
KEYWORDS_MESSAGE_ID = 3154651

# ID пользовательского аккаунта, авторизованного через Telethon.
# Определяется автоматически после подключения.
TELETHON_USER_ID = None


# ============================================================
# ДЕФОЛТНЫЙ СПИСОК КЛЮЧЕВЫХ СЛОВ
# ============================================================

DEFAULT_KEYWORDS = [
    "загроза балістики",
    "балістична загроза",
    "київ — спуск балістики",
    "київ - спуск балістики",
    "нивки",
    "нивок",
    "нивка",
    "онікс—м",
    "онікс-м",
    "циркони київ",
    "циркон київ",
    "циркон — київ",
    "київ кр",
    "кр київ",
    "зліт міг",
    "кинджал",
    "на київ ",
    "на київ!",
    "далі київ ",
    "на столицю",
    "вектор київ ",
    "вектор київ!",
    # "у київ ",
    # "до києва "
]


# ============================================================
# ТЕКУЩИЙ СПИСОК КЛЮЧЕВЫХ СЛОВ И НАСТРОЙКИ
# ============================================================

KEYWORDS = list(DEFAULT_KEYWORDS)

# Мониторинг тревог включён по умолчанию.
BOT_SETTINGS = {"alerts": "on"}


# ============================================================
# КАНАЛЫ, КОТОРЫЕ НЕОБХОДИМО ОТСЛЕЖИВАТЬ
# ============================================================

TARGET_CHANNELS = [
    "@war_monitor",
    "@kievreal1",
    "@truexanewsua",
    # "@tgalertfilter"
]


# ============================================================
# TELETHON CLIENT
# ============================================================

client = TelegramClient(
    StringSession(SESSION_STRING),
    API_ID,
    API_HASH,
    connection_retries=5,
    retry_delay=3,
    auto_reconnect=True
)


# ============================================================
# СОСТОЯНИЕ ПУЛЬСА / HEARTBEAT
# ============================================================

HEARTBEAT_MESSAGE_ID = None

# 🟢 — обычная работа.
# 🟡 — ключевые слова успешно изменены.
# 🔴 — ошибка обработки ключевых слов.
CURRENT_PULSE_COLOR = "🟢"

CURRENT_TICK = 0


# ============================================================
# ОТПРАВКА УВЕДОМЛЕНИЯ
# ============================================================

def send_telegram_alert(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": MY_TELEGRAM_ID,
        "text": text
    }

    try:
        requests.post(
            url,
            json=payload,
            timeout=10
        )
    except Exception as e:
        print(f"Send err: {e}", flush=True)


# ============================================================
# РАЗБОР СООБЩЕНИЯ С КЛЮЧЕВЫМИ СЛОВАМИ
# ============================================================

def parse_keywords(text):
    parsed = []

    # Обрабатываем сообщение построчно.
    for line in text.splitlines():
        line_str = line.strip()

        # Пустые строки и строки-комментарии пропускаем.
        if not line_str or line_str.startswith("#"):
            continue

        # Вытаскиваем содержимое между кавычками.
        matches = re.findall(r'["\'](.*?)["\']', line_str)

        if matches:
            for match in matches:
                cleaned = match.lower()
                if cleaned:
                    parsed.append(cleaned)
        else:
            # Если кавычек нет, используем всю строку.
            cleaned = line_str.lower()
            if cleaned:
                parsed.append(cleaned)

    if not parsed:
        raise ValueError("Пустой список ключевых слов")

    return parsed


def parse_config_and_keywords(text):
    settings = {"alerts": "on"}
    keyword_lines = text

    if "---" in text:
        parts = text.split("---", 1)
        settings_part = parts[0]
        keyword_lines = parts[1]

        for line in settings_part.splitlines():
            line_str = line.strip()

            if not line_str or line_str.startswith("#"):
                continue

            if "=" in line_str:
                key, val = line_str.split("=", 1)
                settings[key.strip().lower()] = val.strip().lower()

    parsed_keywords = parse_keywords(keyword_lines)

    return settings, parsed_keywords


# ============================================================
# МОНИТОРИНГ ВОЗДУШНЫХ ТРЕВОГ (Киев и Фастовский район)
# ============================================================

async def alerts_monitoring_loop():
    if not ALERTS_API_TOKEN:
        print(
            "ALERTS_API_TOKEN не задан, мониторинг тревог отключен.",
            flush=True
        )
        return

    alerts_client = AlertsClient(token=ALERTS_API_TOKEN)

    last_kyiv_status = None
    last_boyarka_status = None

    while True:
        # Если в конфигурации выставлено alerts = off,
        # пропускаем мониторинг.
        if BOT_SETTINGS.get("alerts") == "off":
            await asyncio.sleep(10)
            continue

        try:
            active_alerts = await alerts_client.get_active_alerts()

            kyiv_alert = None
            boyarka_alert = None

            for alert in active_alerts:
                uid = int(alert.location_uid) if alert.location_uid else 0
                title = (alert.location_title or "").lower()

                # Київ (UID 31).
                if uid == 31 or "м. київ" in title:
                    kyiv_alert = alert

                # Фастівський район (UID 77).
                elif uid == 77 or "фастів" in title:
                    boyarka_alert = alert

            kyiv_current_level = (
                kyiv_alert.alert_level if kyiv_alert else None
            )

            if kyiv_current_level != last_kyiv_status:
                if kyiv_current_level == "red":
                    send_telegram_alert(
                        "🔴 Тривога Київ / Червоний рівень"
                    )
                elif kyiv_current_level == "yellow":
                    send_telegram_alert(
                        "🟡 Тривога Київ / Жовтий рівень"
                    )
                elif (
                    kyiv_current_level is None
                    and last_kyiv_status is not None
                ):
                    send_telegram_alert("🟢 Відбій Київ")

                last_kyiv_status = kyiv_current_level

            boyarka_current_level = (
                boyarka_alert.alert_level if boyarka_alert else None
            )

            if boyarka_current_level != last_boyarka_status:
                if boyarka_current_level == "red":
                    send_telegram_alert(
                        "🔴 Тривога Фастівський р-н / Червоний рівень"
                    )
                elif boyarka_current_level == "yellow":
                    send_telegram_alert(
                        "🟡 Тривога Фастівський р-н / Жовтий рівень"
                    )
                elif (
                    boyarka_current_level is None
                    and last_boyarka_status is not None
                ):
                    send_telegram_alert("🟢 Відбій Фастівський р-н")

                last_boyarka_status = boyarka_current_level

        except Exception as e:
            print(f"Alerts API err: {e}", flush=True)

        await asyncio.sleep(12)


# ============================================================
# ОБНОВЛЕНИЕ ПУЛЬСА
# ============================================================

def update_heartbeat(color_symbol=None):
    global HEARTBEAT_MESSAGE_ID
    global CURRENT_PULSE_COLOR
    global CURRENT_TICK

    # Если передан новый цвет — меняем текущее состояние.
    if color_symbol:
        CURRENT_PULSE_COLOR = color_symbol

    # Сохраняем исходную логику расчёта времени.
    now_kyiv = (
        datetime.now(timezone.utc) + timedelta(hours=2)
    ).strftime("%H:%M")

    text = f"{CURRENT_PULSE_COLOR} {now_kyiv} ({CURRENT_TICK})"

    # ========================================================
    # ЕСЛИ ПУЛЬС ЕЩЁ НЕ СОЗДАН
    # ========================================================

    if HEARTBEAT_MESSAGE_ID is None:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

        payload = {
            "chat_id": MY_TELEGRAM_ID,
            "text": text,
            "disable_notification": True
        }

        try:
            res = requests.post(
                url,
                json=payload,
                timeout=10
            ).json()

            if res.get("ok"):
                HEARTBEAT_MESSAGE_ID = res["result"]["message_id"]

                try:
                    requests.post(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/unpinAllChatMessages",
                        json={"chat_id": MY_TELEGRAM_ID},
                        timeout=10
                    )
                except Exception:
                    pass

                # Закрепляем сообщение пульса.
                requests.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/pinChatMessage",
                    json={
                        "chat_id": MY_TELEGRAM_ID,
                        "message_id": HEARTBEAT_MESSAGE_ID,
                        "disable_notification": True
                    },
                    timeout=10
                )

        except Exception as e:
            print(f"Pulse err: {e}", flush=True)

    # ========================================================
    # ЕСЛИ ПУЛЬС УЖЕ СУЩЕСТВУЕТ
    # ========================================================

    else:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"

        payload = {
            "chat_id": MY_TELEGRAM_ID,
            "message_id": HEARTBEAT_MESSAGE_ID,
            "text": text
        }

        try:
            res = requests.post(
                url,
                json=payload,
                timeout=10
            ).json()

            if not res.get("ok"):
                HEARTBEAT_MESSAGE_ID = None

        except Exception as e:
            print(f"Pulse err: {e}", flush=True)


# ============================================================
# ОБРАБОТКА СООБЩЕНИЯ С КЛЮЧЕВЫМИ СЛОВАМИ
# ============================================================

async def process_keywords_message(text):
    global KEYWORDS
    global BOT_SETTINGS

    try:
        # Разбираем настройки и ключевые слова.
        new_settings, new_keywords = parse_config_and_keywords(text)

        BOT_SETTINGS = new_settings
        KEYWORDS = new_keywords

        print(
            f"Настройки: {BOT_SETTINGS} | "
            f"Ключевые слова загружены ({len(KEYWORDS)} шт.): {KEYWORDS}",
            flush=True
        )

        # Жёлтый пульс означает успешную загрузку настроек.
        update_heartbeat("🟡")

    except Exception as e:
        print(
            f"Ошибка парсинга конфига/ключей: {e}",
            flush=True
        )

        # При ошибке возвращаем настройки по умолчанию.
        KEYWORDS = list(DEFAULT_KEYWORDS)
        BOT_SETTINGS = {"alerts": "on"}

        update_heartbeat("🔴")


# ============================================================
# ОТСЛЕЖИВАНИЕ РЕДАКТИРОВАНИЯ СООБЩЕНИЯ В ИЗБРАННОМ
# ============================================================

@client.on(events.MessageEdited)
async def handle_message_edit(event):
    # TELETHON_USER_ID — ID аккаунта, под которым запущен Telethon.
    # В Избранном chat_id соответствует собственному аккаунту.
    if (
        TELETHON_USER_ID is not None
        and event.chat_id == TELETHON_USER_ID
        and event.id == KEYWORDS_MESSAGE_ID
    ):
        print(
            f"Обнаружено редактирование управляющего сообщения "
            f"{KEYWORDS_MESSAGE_ID}.",
            flush=True
        )

        await process_keywords_message(event.raw_text)


# ============================================================
# ПЕРВОНАЧАЛЬНАЯ ЗАГРУЗКА КЛЮЧЕВЫХ СЛОВ ИЗ ИЗБРАННОГО
# ============================================================

async def load_initial_keywords():
    try:
        # Читаем сообщение с заданным ID из Избранного.
        msg = await client.get_messages(
            "me",
            ids=KEYWORDS_MESSAGE_ID
        )

        if msg and msg.raw_text:
            print(
                f"Управляющее сообщение {KEYWORDS_MESSAGE_ID} "
                "найдено в Избранном.",
                flush=True
            )

            await process_keywords_message(msg.raw_text)
        else:
            print(
                f"Сообщение {KEYWORDS_MESSAGE_ID} "
                "не найдено в Избранном или не содержит текста.",
                flush=True
            )

    except Exception as e:
        print(
            f"Ошибка при считывании сообщения "
            f"{KEYWORDS_MESSAGE_ID}: {e}",
            flush=True
        )


# ============================================================
# ЦИКЛ HEARTBEAT / ПУЛЬСА
# ============================================================

async def heartbeat_loop():
    global CURRENT_TICK
    global CURRENT_PULSE_COLOR

    while True:
        CURRENT_TICK += 1

        # После жёлтого пульса возвращаем зелёный.
        if CURRENT_PULSE_COLOR == "🟡":
            CURRENT_PULSE_COLOR = "🟢"

        update_heartbeat()

        # Следующее обновление через 10 минут.
        await asyncio.sleep(600)


# ============================================================
# ОТСЛЕЖИВАНИЕ НОВЫХ СООБЩЕНИЙ В ЦЕЛЕВЫХ КАНАЛАХ
# ============================================================

@client.on(events.NewMessage(chats=TARGET_CHANNELS))
async def handle_new_message(event):
    message_text = event.raw_text

    # Поиск ключевых слов без учёта регистра.
    text_lower = message_text.lower()

    if any(keyword in text_lower for keyword in KEYWORDS):
        chat = await client.get_entity(event.chat_id)

        # Получаем username канала.
        username = (
            chat.username
            or (
                chat.usernames[0].username
                if getattr(chat, "usernames", None)
                else None
            )
        )

        # Если username отсутствует, используем название канала.
        channel_id = (
            f"@{username}"
            if username
            else getattr(chat, "title", "Канал")
        )

        alert_msg = f"▶ {channel_id}\n{message_text}"

        # Отправляем уведомление в личный чат.
        send_telegram_alert(alert_msg)


# ============================================================
# ЗАПУСК TELETHON
# ============================================================

async def start_telethon():
    global TELETHON_USER_ID

    print("Запуск Telethon...", flush=True)

    try:
        # Подключаем пользовательский Telegram-аккаунт.
        await client.start()

        # Определяем реальный ID авторизованного аккаунта.
        me = await client.get_me()
        TELETHON_USER_ID = me.id

        print(
            f"Telethon запущен! ID пользовательского аккаунта: "
            f"{TELETHON_USER_ID}",
            flush=True
        )

        # Сначала загружаем управляющее сообщение из Избранного.
        await load_initial_keywords()

        # Запускаем пульс.
        asyncio.create_task(heartbeat_loop())

        # Запускаем мониторинг воздушных тревог.
        asyncio.create_task(alerts_monitoring_loop())

        # Остаёмся подключёнными до отключения клиента.
        await client.run_until_disconnected()

    except Exception as e:
        print(f"Telethon err: {e}", flush=True)


# ============================================================
# ГЛАВНАЯ ТОЧКА ЗАПУСКА
# ============================================================

if __name__ == "__main__":
    # Flask запускается в отдельном потоке.
    threading.Thread(
        target=run_flask,
        daemon=True
    ).start()

    # Основной asyncio-цикл для Telethon.
    asyncio.run(start_telethon())
