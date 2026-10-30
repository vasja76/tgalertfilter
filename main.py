import asyncio
import os
import time
from collections import deque
import threading
from datetime import datetime, timezone, timedelta
from flask import Flask
from telethon import TelegramClient, events
from telethon.sessions import StringSession
import requests
from alerts_in_ua import AsyncClient as AlertsClient
from supabase import create_client, Client

# ============================================================
# FLASK — веб-сервер для Render
# ============================================================

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running 24/7!", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

# ============================================================
# ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
# ============================================================

API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MY_TELEGRAM_ID = int(os.environ.get("MY_TELEGRAM_ID"))
SESSION_STRING = os.environ.get("SESSION_STRING")
ALERTS_API_TOKEN = os.environ.get("ALERTS_API_TOKEN")

# SUPABASE_KEY — это service_role ключ (Project Settings -> API).
# Он обходит RLS и хранится только на сервере.
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

supabase: Client = (
    create_client(SUPABASE_URL, SUPABASE_KEY)
    if SUPABASE_URL and SUPABASE_KEY else None
)
if not supabase:
    print("ВНИМАНИЕ: SUPABASE_URL/SUPABASE_KEY не заданы, список слов будет пустым!", flush=True)

# yellow_silent = on  -> сообщения о жёлтой тревоге приходят без звука
# (красные тревоги и «Відбій» всегда со звуком)
DEFAULT_SETTINGS = {"alerts": "on", "yellow_silent": "off"}

# Результат ПОСЛЕДНЕГО реального обращения к БД (старт, команды).
# Пульс только читает этот флаг и сам в БД не ходит.
DB_OK = supabase is not None

def _db_ok(flag):
    global DB_OK
    DB_OK = flag

# ============================================================
# РАБОТА С SUPABASE
# ============================================================
# Функции чтения возвращают None при ошибке (а не пустой список),
# чтобы сбой БД не стирал рабочий список слов в памяти.

def db_get_settings():
    if not supabase:
        return dict(DEFAULT_SETTINGS)
    try:
        res = supabase.table("bot_settings").select("key, value").execute()
        _db_ok(True)
        settings = dict(DEFAULT_SETTINGS)
        settings.update({row["key"]: row["value"] for row in res.data})
        return settings
    except Exception as e:
        print(f"DB settings err: {e}", flush=True)
        _db_ok(False)
        return None

def db_get_keywords():
    if not supabase:
        return []
    try:
        res = supabase.table("keywords").select("word").execute()
        _db_ok(True)
        # Пустые слова отбрасываем: "" содержится в любой строке
        # и бот реагировал бы на каждое сообщение.
        return [
            row["word"].lower()
            for row in res.data
            if row.get("word") and row["word"].strip()
        ]
    except Exception as e:
        print(f"DB keywords err: {e}", flush=True)
        _db_ok(False)
        return None

def db_add_keyword(word):
    if not supabase:
        return False
    try:
        supabase.table("keywords").upsert({"word": word}, on_conflict="word").execute()
        _db_ok(True)
        return True
    except Exception as e:
        print(f"DB add keyword err: {e}", flush=True)
        _db_ok(False)
        return False

def db_del_keyword(word):
    # Возвращает количество удалённых строк или None при ошибке.
    if not supabase:
        return None
    try:
        res = supabase.table("keywords").delete().eq("word", word).execute()
        _db_ok(True)
        return len(res.data or [])
    except Exception as e:
        print(f"DB del keyword err: {e}", flush=True)
        _db_ok(False)
        return None

def db_set_setting(key, value):
    if not supabase:
        return False
    try:
        supabase.table("bot_settings").upsert({"key": key, "value": value}, on_conflict="key").execute()
        _db_ok(True)
        return True
    except Exception as e:
        print(f"DB set setting err: {e}", flush=True)
        _db_ok(False)
        return False

def db_ping():
    # Лёгкий запрос раз в сутки: бесплатный Supabase усыпляет проект
    # после недели без активности. Заодно обновляет флаг DB_OK для пульса.
    if not supabase:
        return False
    try:
        supabase.table("bot_settings").select("key").limit(1).execute()
        _db_ok(True)
        return True
    except Exception as e:
        print(f"DB ping err: {e}", flush=True)
        _db_ok(False)
        return False

# ============================================================
# ГЛОБАЛЬНЫЕ СОСТОЯНИЯ
# ============================================================

KEYWORDS = db_get_keywords() or []
BOT_SETTINGS = db_get_settings() or dict(DEFAULT_SETTINGS)

def yellow_is_silent():
    return BOT_SETTINGS.get("yellow_silent", "off") == "on"

TARGET_CHANNELS = [
    "@war_monitor",
    "@kievreal1",
    "@truexanewsua",
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
# Время последнего успешного запроса к API тревог (для пульса).
ALERTS_LAST_OK = time.time()

# Клиент API тревог (создаётся в alerts_monitoring_loop, им же пользуется /now).
ALERTS_CLIENT = None

# Время всех запросов к API тревог за последнюю минуту (цикл + команда /now).
# Нужен, чтобы вместе они не превысили лимит API.
API_REQUEST_TIMES = deque()
API_LIMIT_PER_MIN = 10

# Числовой ID бота — начало токена до двоеточия.
# Он же chat_id личного чата с ботом в Telethon (откуда приходят команды).
BOT_USER_ID = int(BOT_TOKEN.split(":")[0])

# ID вашего аккаунта. Ответы на команды отправляются сюда,
# то есть в личный чат с ботом, а не в общую группу.
# Заполняется при старте.
ADMIN_CHAT_ID = None

# Статус тревог для эмодзи пульса: None (отбой), "yellow" или "red".
# Это худший статус среди отслеживаемых районов.
PULSE_ALERT_LEVEL = None

# Последний список проблем из проверки состояния. Нужен, чтобы цикл тревог
# мог обновить пульс сразу при смене статуса, не дожидаясь проверки.
LAST_HEALTH_PROBLEMS = []

# Текст последнего пульса и блокировка: пульс обновляют два цикла.
LAST_PULSE_TEXT = None
HEARTBEAT_LOCK = threading.Lock()

# Событие «первый опрос тревог выполнен» (создаётся при запуске).
# Пульс при старте ждёт его, чтобы сразу показать правильный цвет.
ALERTS_FIRST_POLL_DONE = None

def send_telegram_alert(text, silent=False, chat_id=None):
    # По умолчанию — в общий чат/канал (MY_TELEGRAM_ID).
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id if chat_id is not None else MY_TELEGRAM_ID,
        "text": text
    }
    if silent:
        payload["disable_notification"] = True
    try:
        res = requests.post(url, json=payload, timeout=10)
        if not res.ok:
            print(f"Send fail: {res.text}", flush=True)
    except Exception as e:
        print(f"Send err: {e}", flush=True)

# ============================================================
# МОНИТОРИНГ ВОЗДУШНЫХ ТРЕВОГ
# ============================================================

# Районы, за которыми следим.
# uid — код региона в API, title_part — запасной поиск по названию.
WATCHED_DISTRICTS = [
    {"key": "kyiv", "name": "Київ", "uid": 31, "title_part": "м. київ"},
    {"key": "fastiv", "name": "Фастівський р-н", "uid": 77, "title_part": "фастів"},
]

# Опасность статуса: чем больше число, тем хуже.
DANGER = {None: 0, "yellow": 1, "red": 2}

def worst_level(current):
    # Худший статус среди районов: None, "yellow" или "red".
    worst = None
    for level in current.values():
        if DANGER.get(level, 0) > DANGER.get(worst, 0):
            worst = level
    return worst

def api_requests_last_minute():
    now = time.time()
    while API_REQUEST_TIMES and now - API_REQUEST_TIMES[0] > 60:
        API_REQUEST_TIMES.popleft()
    return len(API_REQUEST_TIMES)

def register_api_request():
    API_REQUEST_TIMES.append(time.time())

def compute_current_status(active_alerts):
    # Текущий статус каждого района: None, "yellow" или "red".
    current = {d["key"]: None for d in WATCHED_DISTRICTS}
    for alert in active_alerts:
        uid = int(alert.location_uid) if alert.location_uid else 0
        title = (alert.location_title or "").lower()
        for d in WATCHED_DISTRICTS:
            if uid == d["uid"] or d["title_part"] in title:
                current[d["key"]] = alert.alert_level
                break
    return current

STATUS_TEXT = {
    "red": "🔴 красная тревога",
    "yellow": "🟡 жёлтая тревога",
    None: "🟢 тревоги нет",
}

def format_status(current):
    lines = ["📡 Статус тревог сейчас:"]
    for d in WATCHED_DISTRICTS:
        level = current[d["key"]]
        lines.append(f'{d["name"]}: {STATUS_TEXT.get(level, "❓ " + str(level))}')
    if BOT_SETTINGS.get("alerts", "on") == "off":
        lines.append("\n⚠️ Автомониторинг выключен (/alerts on)")
    return "\n".join(lines)

def build_alert_messages(changes):
    # changes: {ключ района: новый статус}. Районы с одинаковым новым
    # статусом объединяются в одно сообщение, разные — отдельные сообщения.
    groups = {}
    for d in WATCHED_DISTRICTS:
        if d["key"] in changes:
            groups.setdefault(changes[d["key"]], []).append(d["name"])

    messages = []  # список (текст, без_звука)
    for level, names in groups.items():
        joined = " і ".join(names)
        if level == "red":
            messages.append((f"🔴 Тривога {joined} / Червоний рівень", False))
        elif level == "yellow":
            messages.append((f"🟡 Тривога {joined} / Жовтий рівень", yellow_is_silent()))
        elif level is None:
            # Відбій всегда со звуком.
            messages.append((f"🟢 Відбій {joined}", False))
    return messages

async def alerts_monitoring_loop():
    global ALERTS_LAST_OK
    global ALERTS_CLIENT
    global PULSE_ALERT_LEVEL
    if not ALERTS_API_TOKEN:
        print("ALERTS_API_TOKEN не задан, мониторинг тревог отключен.", flush=True)
        return
    alerts_client = AlertsClient(token=ALERTS_API_TOKEN)
    ALERTS_CLIENT = alerts_client
    last_status = {d["key"]: None for d in WATCHED_DISTRICTS}

    while True:
        # Настройки берём из памяти — их обновляют команды /alerts и /reload.
        if BOT_SETTINGS.get("alerts", "on") == "off":
            # Пока мониторинг выключен, не считаем это поломкой.
            ALERTS_LAST_OK = time.time()
            if ALERTS_FIRST_POLL_DONE is not None:
                ALERTS_FIRST_POLL_DONE.set()
            await asyncio.sleep(10)
            continue

        try:
            # Один запрос возвращает все активные тревоги по стране.
            register_api_request()
            active_alerts = list(await alerts_client.get_active_alerts())
            ALERTS_LAST_OK = time.time()

            # Пустой список — это нормальный ответ: тревог по стране нет,
            # значит в наших районах отбой. Ошибки запроса (сеть, токен)
            # выбрасывают исключение и статусы не меняют.

            current = compute_current_status(active_alerts)

            # Сообщения только по тем районам, где статус изменился.
            changes = {k: v for k, v in current.items() if v != last_status[k]}
            for text, silent in build_alert_messages(changes):
                send_telegram_alert(text, silent=silent)

            # Пульс: эмодзи = худший текущий статус. Сообщение редактируется,
            # если какой-то район стал опаснее или изменился общий статус
            # (в том числе когда оба района перешли в отбой).
            new_level = worst_level(current)
            escalated = any(
                DANGER.get(current[k], 0) > DANGER.get(last_status[k], 0)
                for k in current
            )
            if escalated or new_level != PULSE_ALERT_LEVEL:
                PULSE_ALERT_LEVEL = new_level
                await asyncio.to_thread(update_heartbeat, LAST_HEALTH_PROBLEMS)

            last_status.update(current)

            if ALERTS_FIRST_POLL_DONE is not None:
                ALERTS_FIRST_POLL_DONE.set()
        except Exception as e:
            print(f"Alerts API err: {e}", flush=True)

        await asyncio.sleep(12)

# ============================================================
# ПУЛЬС / HEARTBEAT
# ============================================================

PULSE_EMOJI = {None: "🟢", "yellow": "🟡", "red": "🔴"}

def update_heartbeat(problems=None):
    # Пульс обновляют два места (цикл проверки и цикл тревог),
    # поэтому обновление идёт под блокировкой.
    with HEARTBEAT_LOCK:
        _update_heartbeat(problems)

def _update_heartbeat(problems=None):
    global HEARTBEAT_MESSAGE_ID
    global LAST_PULSE_TEXT

    now_kyiv = (
        datetime.now(timezone.utc) + timedelta(hours=2)
    ).strftime("%H:%M")

    if problems:
        # Есть проблемы: в сообщении только их список, например «БД, Telegram».
        text = ", ".join(problems)
    else:
        # Всё в порядке: эмодзи статуса тревог + время.
        # Если мониторинг тревог выключен (/alerts off) или не настроен —
        # серый кружок вместо статуса.
        if BOT_SETTINGS.get("alerts", "on") == "off" or not ALERTS_API_TOKEN:
            emoji = "⚪"
        else:
            emoji = PULSE_EMOJI.get(PULSE_ALERT_LEVEL, "🟢")
        text = f"{emoji} {now_kyiv}"

    # Если текст не изменился (например, два обновления в одну минуту),
    # Telegram ответил бы «message is not modified» — просто пропускаем.
    if HEARTBEAT_MESSAGE_ID is not None and text == LAST_PULSE_TEXT:
        return

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
                LAST_PULSE_TEXT = text
                try:
                    requests.post(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/unpinAllChatMessages",
                        json={"chat_id": MY_TELEGRAM_ID},
                        timeout=10
                    )
                except Exception:
                    pass

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
    else:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
        payload = {
            "chat_id": MY_TELEGRAM_ID,
            "message_id": HEARTBEAT_MESSAGE_ID,
            "text": text
        }
        try:
            res = requests.post(url, json=payload, timeout=10).json()
            if res.get("ok") or "not modified" in str(res.get("description", "")):
                LAST_PULSE_TEXT = text
            else:
                # Сообщение недоступно (например, удалено) — создадим новое.
                HEARTBEAT_MESSAGE_ID = None
        except Exception as e:
            print(f"Pulse err: {e}", flush=True)

async def check_health():
    # Возвращает список проблем. Пустой список — всё в порядке.
    problems = []

    # 1. Telegram: реальный запрос к серверам, а не просто флаг подключения.
    try:
        if not client.is_connected():
            problems.append("Telegram")
        else:
            await asyncio.wait_for(client.get_me(), timeout=20)
    except Exception as e:
        print(f"Health: Telegram err: {e}", flush=True)
        problems.append("Telegram")

    # 2. База данных: смотрим итог последнего реального обращения
    # (старт, команды), сами в БД не ходим.
    if not DB_OK:
        problems.append("БД")

    # 3. Список слов не пуст (иначе уведомления не придут никогда).
    if not KEYWORDS:
        problems.append("нет слов")

    # 4. API тревог отвечает (если мониторинг включён).
    if ALERTS_API_TOKEN and BOT_SETTINGS.get("alerts", "on") != "off":
        if time.time() - ALERTS_LAST_OK > 180:
            problems.append("тревоги")

    return problems

async def heartbeat_loop():
    global LAST_HEALTH_PROBLEMS

    # При запуске ждём первый опрос тревог (не дольше 30 секунд), чтобы
    # первое закреплённое сообщение сразу показало правильный цвет.
    if ALERTS_API_TOKEN and BOT_SETTINGS.get("alerts", "on") != "off":
        try:
            await asyncio.wait_for(ALERTS_FIRST_POLL_DONE.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass

    # Состояние проверяется раз в 5 минут. Сообщение пульса обновляется
    # раз в 10 минут, а при смене набора проблем — сразу после проверки.
    # Смена статуса тревог обновляет пульс отдельно (в цикле тревог).
    last_problems = None
    checks_since_update = 0

    while True:
        problems = await check_health()
        LAST_HEALTH_PROBLEMS = problems

        if problems != last_problems or checks_since_update >= 2:
            # Отправка идёт через requests, выносим из цикла событий.
            await asyncio.to_thread(update_heartbeat, problems)
            last_problems = problems
            checks_since_update = 0

        checks_since_update += 1
        await asyncio.sleep(300)

async def db_keepalive_loop():
    # Первый запрос не нужен: при старте база уже читалась.
    while True:
        await asyncio.sleep(86400)
        await asyncio.to_thread(db_ping)

# ============================================================
# КОМАНДЫ УПРАВЛЕНИЯ ИЗ ЧАТА С БОТОМ
# ============================================================

async def reply(text):
    # Ответ уходит от имени бота (через Bot API) в ЛИЧНЫЙ чат с вами.
    # В общую группу ответы никогда не попадают.
    if ADMIN_CHAT_ID is None:
        print("reply: ADMIN_CHAT_ID не определён, ответ не отправлен", flush=True)
        return
    await asyncio.to_thread(send_telegram_alert, text, False, ADMIN_CHAT_ID)

def parse_word(arg):
    # Слово можно передать в кавычках, чтобы сохранить пробелы по краям:
    #   /add "на київ "
    # Telegram сам обрезает пробелы в конце сообщения, поэтому без кавычек
    # пробел в конце слова не сохранится.
    if len(arg) >= 2 and arg[0] in "\"'" and arg[-1] == arg[0]:
        word = arg[1:-1]
    else:
        word = arg.strip()
    # Все слова хранятся в нижнем регистре, т.к. текст постов тоже приводится к нему.
    return word.lower()

HELP_TEXT = (
    "📖 Команды:\n\n"
    "/add слово — добавить ключевое слово\n"
    '(в кавычках, если нужны пробелы по краям: /add "на київ ")\n\n'
    "/del слово — удалить ключевое слово\n\n"
    "/list — настройки и список слов\n\n"
    "/alerts on | off — мониторинг воздушных тревог\n\n"
    "/yellow silent | sound — жёлтые тревоги без звука / со звуком\n\n"
    "/now — статус тревог прямо сейчас (внеочередной запрос)\n\n"
    "/reload — перечитать данные из Supabase (после ручной правки таблиц)\n\n"
    "/help — эта справка"
)

# outgoing=True — только сообщения, отправленные С ВАШЕГО аккаунта.
# Если кто-то другой напишет боту, для вашей Telethon-сессии это входящее
# сообщение, и сюда оно не попадёт. Так что команды доступны только вам.
@client.on(events.NewMessage(outgoing=True))
async def handle_my_commands(event):
    global KEYWORDS
    global BOT_SETTINGS
    global ALERTS_LAST_OK

    # Команды принимаются только в личном чате с ботом.
    if event.chat_id != BOT_USER_ID:
        return

    text = event.raw_text or ""

    if text.startswith("/add "):
        word = parse_word(text[5:])
        if not word.strip():
            await reply("❌ Пустое слово")
            return
        if await asyncio.to_thread(db_add_keyword, word):
            new_keywords = await asyncio.to_thread(db_get_keywords)
            if new_keywords is not None:
                KEYWORDS = new_keywords
            await reply(f'✅ Добавлено: "{word}"')
        else:
            await reply("❌ Ошибка добавления в БД")

    elif text.startswith("/del "):
        word = parse_word(text[5:])
        deleted = await asyncio.to_thread(db_del_keyword, word)
        if deleted is None:
            await reply("❌ Ошибка удаления из БД")
        elif deleted == 0:
            await reply(f'⚠️ Не найдено: "{word}"')
        else:
            new_keywords = await asyncio.to_thread(db_get_keywords)
            if new_keywords is not None:
                KEYWORDS = new_keywords
            await reply(f'🗑 Удалено: "{word}"')

    elif text == "/list":
        header = (
            f"⚙️ Настройки:\nalerts = {BOT_SETTINGS.get('alerts', 'on')}\n"
            f"yellow = {'silent' if yellow_is_silent() else 'sound'}\n\n"
            f"🔑 Ключевые слова ({len(KEYWORDS)}):\n"
        )
        # Слова показываем в кавычках, чтобы были видны пробелы по краям.
        lines = [f'• "{kw}"' for kw in KEYWORDS]

        # Лимит Telegram — 4096 символов, поэтому режем на части.
        chunks = []
        current = header
        for line in lines:
            if len(current) + len(line) + 1 > 3800:
                chunks.append(current)
                current = ""
            current += line + "\n"
        chunks.append(current)

        for chunk in chunks:
            await reply(chunk)

    elif text.startswith("/alerts "):
        val = text[8:].strip().lower()
        if val in ["on", "off"]:
            if await asyncio.to_thread(db_set_setting, "alerts", val):
                BOT_SETTINGS = {**BOT_SETTINGS, "alerts": val}
                # Пульс меняется сразу: ⚪ при off, статус тревог при on.
                await asyncio.to_thread(update_heartbeat, LAST_HEALTH_PROBLEMS)
                await reply(f"✅ Настройка alerts: {val}")
            else:
                await reply("❌ Ошибка записи в БД")
        else:
            await reply("❌ Используйте /alerts on или /alerts off")

    elif text == "/yellow" or text.startswith("/yellow "):
        val = text[7:].strip().lower()
        if val in ["silent", "sound"]:
            flag = "on" if val == "silent" else "off"
            if await asyncio.to_thread(db_set_setting, "yellow_silent", flag):
                BOT_SETTINGS = {**BOT_SETTINGS, "yellow_silent": flag}
                if flag == "on":
                    await reply("🔕 Жёлтые тревоги теперь приходят без звука")
                else:
                    await reply("🔔 Жёлтые тревоги теперь приходят со звуком")
            else:
                await reply("❌ Ошибка записи в БД")
        else:
            state = "без звука" if yellow_is_silent() else "со звуком"
            await reply(
                f"Сейчас жёлтые тревоги приходят {state}.\n"
                "Используйте /yellow silent или /yellow sound"
            )

    elif text == "/now":
        # Внеочередной запрос статуса тревог. Работает независимо от цикла
        # опроса (ждать 12 секунд не нужно) и только показывает статус:
        # цикл опроса продолжает работать по обычным правилам и сам
        # отправит в группу сообщение, если статус изменился.
        if ALERTS_CLIENT is None:
            await reply("❌ ALERTS_API_TOKEN не задан, запрос невозможен")
        elif api_requests_last_minute() >= API_LIMIT_PER_MIN - 1:
            # Запас в 1 запрос до лимита API.
            await reply("⏳ Лимит запросов к API исчерпан, повторите через 10–15 секунд")
        else:
            try:
                register_api_request()
                active = list(await ALERTS_CLIENT.get_active_alerts())
                ALERTS_LAST_OK = time.time()
                await reply(format_status(compute_current_status(active)))
            except Exception as e:
                print(f"/now err: {e}", flush=True)
                await reply(f"❌ Не удалось получить статус: {e}")

    elif text == "/help":
        await reply(HELP_TEXT)

    elif text == "/reload":
        # Ручное перечитывание данных из Supabase
        # (на случай, если вы правили таблицы напрямую).
        new_keywords = await asyncio.to_thread(db_get_keywords)
        new_settings = await asyncio.to_thread(db_get_settings)
        if new_keywords is None or new_settings is None:
            await reply("❌ Не удалось прочитать данные из БД")
        else:
            KEYWORDS = new_keywords
            BOT_SETTINGS = new_settings
            await asyncio.to_thread(update_heartbeat, LAST_HEALTH_PROBLEMS)
            await reply(
                f"🔄 Перечитано из БД: слов {len(KEYWORDS)}, "
                f"alerts = {BOT_SETTINGS.get('alerts', 'on')}"
            )

# ============================================================
# ОТСЛЕЖИВАНИЕ СООБЩЕНИЙ В КАНАЛАХ
# ============================================================

@client.on(events.NewMessage(chats=TARGET_CHANNELS))
async def handle_new_message(event):
    message_text = event.raw_text
    text_lower = message_text.lower()

    if any(keyword in text_lower for keyword in KEYWORDS):
        chat = await client.get_entity(event.chat_id)
        username = (
            chat.username
            or (
                chat.usernames[0].username
                if getattr(chat, 'usernames', None)
                else None
            )
        )
        channel_id = (
            f"@{username}"
            if username
            else getattr(chat, 'title', 'Канал')
        )
        alert_msg = f"▶ {channel_id}\n{message_text}"
        send_telegram_alert(alert_msg)

# ============================================================
# ЗАПУСК
# ============================================================

async def start_telethon():
    global ADMIN_CHAT_ID
    global ALERTS_FIRST_POLL_DONE
    print("Запуск Telethon...", flush=True)
    try:
        await client.start()
        me = await client.get_me()
        ADMIN_CHAT_ID = me.id
        print(f"Вошли как: {me.first_name} (id={me.id})", flush=True)
        print(
            f"Настройки: {BOT_SETTINGS} | Ключевые слова из БД ({len(KEYWORDS)} шт.)",
            flush=True
        )
        if not KEYWORDS:
            print("ВНИМАНИЕ: список ключевых слов пуст — уведомления приходить не будут.", flush=True)

        ALERTS_FIRST_POLL_DONE = asyncio.Event()
        asyncio.create_task(heartbeat_loop())
        asyncio.create_task(alerts_monitoring_loop())
        asyncio.create_task(db_keepalive_loop())

        await client.run_until_disconnected()
    except Exception as e:
        print(f"Telethon err: {e}", flush=True)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.run(start_telethon())