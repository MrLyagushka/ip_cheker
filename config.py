import os
import asyncio
import json
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import MemoryStorage

ADMIN_ID = 1424727277
ENV_FILE = ".env"
HISTORY_FILE = "networks_history.json"
RESULT_FILE = "result.txt"
TRACKED_IPS_FILE = "tracked_ips.json"
USERS_FILE = "users.json"

def load_env():
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ[key.strip()] = value.strip().strip('"\'')

def save_env_value(key_to_update, new_value):
    lines = []
    found = False
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    
    new_lines = []
    for line in lines:
        if line.strip().startswith(f"{key_to_update}="):
            new_lines.append(f"{key_to_update}={new_value}\n")
            found = True
        else:
            new_lines.append(line)
            
    if not found:
        new_lines.append(f"{key_to_update}={new_value}\n")
        
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    os.environ[key_to_update] = str(new_value)

load_env()

TOKEN = os.getenv("TOKEN")
PROXY_URL = os.getenv("PROXY_URL")
MAX_CONCURRENT_PINGS = int(os.getenv("MAX_CONCURRENT_PINGS", "3"))

# Новые переменные для алертов
ALERT_CHAT_ID = os.getenv("ALERT_CHAT_ID", "").strip()
ALERT_THREAD_ID = os.getenv("ALERT_THREAD_ID", "").strip()
if ALERT_THREAD_ID.isdigit():
    ALERT_THREAD_ID = int(ALERT_THREAD_ID)
else:
    ALERT_THREAD_ID = None

if ALERT_CHAT_ID:
    try:
        ALERT_CHAT_ID = int(ALERT_CHAT_ID)
    except ValueError:
        pass

if not TOKEN:
    raise ValueError("❌ Не найден TOKEN в переменных окружения или файле .env!")

# Глобальные управляемые объекты
sem = asyncio.Semaphore(MAX_CONCURRENT_PINGS)
active_users = set()

session = AiohttpSession(proxy=PROXY_URL) if PROXY_URL else None
bot = Bot(token=TOKEN, session=session)
dp = Dispatcher(storage=MemoryStorage())

def init_files():
    if not os.path.exists(RESULT_FILE):
        open(RESULT_FILE, "w", encoding="utf-8").close()
    if not os.path.exists(HISTORY_FILE):
        import json
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump([], f)
    if not os.path.exists(TRACKED_IPS_FILE):
        import json
        with open(TRACKED_IPS_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)
    if not os.path.exists(USERS_FILE):
        import json
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump([], f)

async def async_ping_ip(ip):
    async with sem:
        cmd = ["ping", "-c", "1", "-W", "1", ip]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            await proc.wait()
            return proc.returncode == 0
        except Exception:
            return False

def register_user(user_id: int):
    if not os.path.exists(USERS_FILE):
        users = []
    else:
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                users = json.load(f)
        except Exception:
            users = []
    if user_id not in users:
        users.append(user_id)
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(users, f, ensure_ascii=False, indent=4)
