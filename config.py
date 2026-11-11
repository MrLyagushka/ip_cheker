"""Центральная конфигурация бота (Этап 1 рефакторинга).

Что исправлено:
- PingThrottle — мутируемая инкапсуляция семафора (Б11: админский лимит
  реально действует на все модули; Б13: публичный API вместо sem._value);
- async_ping_ip / ping_many — единая реализация пинга без дублей (Б19);
- is_valid_ipv4 / expand_targets — строгая валидация IP через ipaddress
  с проверкой диапазона октетов 0–255 и блокировкой x.x.0.0 (Б4, Б3);
- result_path_for() — пер-юзерские файлы data/results/result_<uid>.txt (Б18);
- ALERT_CHAT_ID / ALERT_THREAD_ID — жёсткая нормализация в int|None (Б20).
"""

import asyncio
import ipaddress
import json
import os
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import MemoryStorage

ENV_FILE = ".env"

# ADMIN_ID: из окружения (.env), fallback — прежний захардкоженный id
ADMIN_ID = int(os.getenv("ADMIN_ID", "1424727277") or 1424727277)

# ─────────────────────────── Файлы данных (data/) ───────────────────────────
DATA_DIR = Path("data")
RESULTS_DIR = DATA_DIR / "results"
HISTORY_FILE = str(DATA_DIR / "networks_history.json")
TRACKED_IPS_FILE = str(DATA_DIR / "tracked_ips.json")
USERS_FILE = str(DATA_DIR / "users.json")


def result_path_for(user_id: int) -> Path:
    """Уникальный файл отчёта для каждого пользователя (Б18)."""
    return RESULTS_DIR / f"result_{user_id}.txt"

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
PROXY_URL = os.getenv("PROXY_URL") or None


def _normalize_int(raw: str | None, name: str) -> int | None:
    """Жёсткая нормализация числовых env-переменных: int или None (Б20)."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        print(f"[config] {name}={raw!r} не является числом — значение проигнорировано")
        return None


ALERT_CHAT_ID = _normalize_int(os.getenv("ALERT_CHAT_ID"), "ALERT_CHAT_ID")
ALERT_THREAD_ID = _normalize_int(os.getenv("ALERT_THREAD_ID"), "ALERT_THREAD_ID")

try:
    MAX_CONCURRENT_PINGS = max(1, int(os.getenv("MAX_CONCURRENT_PINGS", "3")))
except (TypeError, ValueError):
    MAX_CONCURRENT_PINGS = 3

if not TOKEN:
    raise ValueError("❌ Не найден TOKEN в переменных окружения или файле .env!")

# ─────────────────────────── Семафор (Б11, Б13) ─────────────────────────────


class PingThrottle:
    """Мутируемая обёртка над asyncio.Semaphore.

    Все модули работают с одним и тем же объектом `throttle` (импорт имени
    объекта, а не привязка к конкретному Semaphore), поэтому админская смена
    лимита через set_limit() реально применяется «на лету» ко всем сканам
    (Б11). Публичное свойство `free` заменяет хрупкий доступ к sem._value (Б13).
    """

    def __init__(self, limit: int) -> None:
        self.limit = max(1, int(limit))
        self._sem = asyncio.Semaphore(self.limit)

    def set_limit(self, new_limit: int) -> None:
        """Изменение лимита на лету без пересоздания объекта.

        Увеличение — освобождаем недостающие слоты; уменьшение — забираем
        свободные (занятые сверх нового лимита доедут естественным образом).
        Активные `async with throttle()` при этом не ломаются.
        """
        new_limit = max(1, int(new_limit))
        delta = new_limit - self.limit
        self.limit = new_limit
        if delta > 0:
            for _ in range(delta):
                self._sem.release()
        elif delta < 0:
            for _ in range(-delta):
                if self._sem.locked():
                    break
                # Единственное место в проекте, где допустимо обращение к
                # внутреннему полю: инкапсулировано внутри класса.
                self._sem._value -= 1  # noqa: SLF001

    @property
    def free(self) -> int:
        """Свободные слоты (публичная замена sem._value)."""
        return getattr(self._sem, "_value", 0)

    @property
    def busy(self) -> int:
        """Занятые слоты."""
        return max(0, self.limit - self.free)

    def __call__(self):
        """Использование как контекстный менеджер: `async with throttle():`."""
        return self._sem


throttle = PingThrottle(MAX_CONCURRENT_PINGS)

active_users = set()

session = AiohttpSession(proxy=PROXY_URL) if PROXY_URL else None
bot = Bot(token=TOKEN, session=session)
dp = Dispatcher(storage=MemoryStorage())


def init_files():
    """Создаёт структуру data/ и файлы по умолчанию; мигрирует старые данные."""
    DATA_DIR.mkdir(exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    # Однократная миграция legacy-файлов из корня репозитория в data/
    for legacy, dest in (
        ("networks_history.json", HISTORY_FILE),
        ("tracked_ips.json", TRACKED_IPS_FILE),
        ("users.json", USERS_FILE),
    ):
        if os.path.exists(legacy) and not os.path.exists(dest):
            Path(legacy).rename(dest)

    default_content = {HISTORY_FILE: "[]", TRACKED_IPS_FILE: "{}", USERS_FILE: "[]"}
    for f, empty in default_content.items():
        if not os.path.exists(f):
            with open(f, "w", encoding="utf-8") as fh:
                fh.write(empty)


# ─────────────────────────── Единый пинг (Б19) ──────────────────────────────


async def async_ping_ip(ip: str, timeout: float = 1.0) -> bool:
    """Единая асинхронная проверка доступности хоста.

    Использует мутируемый `throttle` — админский лимит действует мгновенно
    для всех вызывающих модулей (scan, monitor, check_ip).
    """
    async with throttle():
        try:
            proc = await asyncio.create_subprocess_exec(
                "ping", "-c", "1", "-W", str(max(1, int(timeout))), ip,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout + 2)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                return False
            return proc.returncode == 0
        except Exception:
            return False


async def ping_many(targets: list[str], timeout: float = 1.0) -> dict[str, bool]:
    """Параллельный обход целей через единый пул слотов throttle.

    Возвращает {ip: alive}. Задача целиком, а не чанк-в-чанк, — очередь
    слотов продвигается без «холостых» ожиданий самого медленного хоста.
    """
    results = await asyncio.gather(
        *(async_ping_ip(t, timeout) for t in targets),
        return_exceptions=True,
    )
    return {t: (r is True) for t, r in zip(targets, results)}


# ─────────────────────────── Валидация IP (Б4, Б3) ──────────────────────────


def is_valid_ipv4(target: str) -> bool:
    """Строгая проверка IPv4: формат XXX.XXX.XXX.XXX и октеты 0–255.

    ipaddress.IPv4Address отвергает ведущие нули («01.2.3.4»), строки вида
    «1.2.3», hex-формат и значения октетов > 255 — ровно то, что требовалось
    в Б4 (старая проверка `o.isdigit()` пропускала 999.999.999.5).
    """
    try:
        ipaddress.IPv4Address(str(target).strip())
        return True
    except (ValueError, ipaddress.AddressValueError):
        return False


def expand_targets(raw_args: list[str]) -> tuple[list[str], list[str]]:
    """Разворачивает аргументы команды в плоский список целей (Б4, Б3).

    Возвращает (targets, invalid):
      * одиночные валидные IP (дедуп);
      * подсети x.x.x.0 → 256 адресов x.x.x.0..x.x.x.255;
      * x.x.0.0 запрещены (попадают в invalid согласно ТЗ);
      * всё, не прошедшее строгую валидацию, — в invalid.
    """
    targets: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()

    for item in raw_args:
        item = item.strip().rstrip(",;")
        if not item:
            continue
        if not is_valid_ipv4(item):
            invalid.append(item)
            continue
        if item.endswith(".0"):
            if item.endswith(".0.0"):
                invalid.append(item)
                continue
            base = item[:-2]  # отрезаем ".0"
            for i in range(256):
                ip = f"{base}.{i}"
                if ip not in seen:
                    seen.add(ip)
                    targets.append(ip)
        elif item not in seen:
            seen.add(item)
            targets.append(item)

    return targets, invalid


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
