"""Центральная конфигурация бота — runtime-слой (Этап 1, рефакторинг Этапа 8).

Вся чистая логика (парсер .env, PingThrottle, ActiveUserRegistry, пинг,
валидация целей, история, дерево /tree, форматирование отчётов) вынесена в
`config_core.py` — модуль без Telegram-объектов, покрытый юнит-тестами.
Здесь остаются только: загрузка .env, нормализация env-значений и СОЗДАНИЕ
рантайм-объектов (Bot/Dispatcher/throttle/сессия), а также set_proxy().

Публичный API не изменился: все символы core реэкспортируются отсюда,
поэтому `from config import ...` во всех handlers работает как раньше.

Что исправлено (исторически):
- PingThrottle — мутируемая инкапсуляция семафора (Б11: админский лимит
  реально действует на все модули; Б13: публичный API вместо sem._value);
- async_ping_ip / ping_many — единая реализация пинга без дублей (Б19);
- is_valid_ipv4 / expand_targets — строгая валидация IP через ipaddress
  с проверкой диапазона октетов 0–255 и блокировкой x.x.0.0 (Б4, Б3);
- result_path_for() — пер-юзерские файлы data/results/result_<uid>.txt (Б18);
- ALERT_CHAT_ID / ALERT_THREAD_ID — жёсткая нормализация в int|None (Б20).
"""

import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

# Реэкспорт всего чистого ядра: ранние версии кода и handlers импортируют
# эти имена из config — API сохраняется без дублирования логики.
from config_core import (  # noqa: F401
    DATA_DIR, RESULTS_DIR, ENV_FILE, HISTORY_FILE, TRACKED_IPS_FILE,
    USERS_FILE, ADMIN_ID, MAX_CONCURRENT_PINGS, HISTORY_MAX_ENTRIES,
    MAX_BLOCK_LEN, result_path_for, parse_env_line, iter_env_pairs,
    load_env, save_env_value, normalize_int, PingThrottle, ActiveUserRegistry,
    active_users, build_session, init_files, async_ping_ip, ping_many,
    is_valid_ipv4, expand_targets, classify_targets, save_to_history,
    build_tree_blocks, format_monitor_report, format_alert, register_user,
    CLEANUP_CB_PREFIX, DEAD_IPS_MAX, load_tracked_ips, save_tracked_ips,
    encode_dead_payload, decode_dead_payload, remove_dead_ips,
)


def setup_logging(level: str | None = None) -> None:
    """Централизованная настройка логирования (Этап 8).

    Уровень берётся из LOG_LEVEL (по умолчанию INFO); формат содержит имя
    модуля и уровень — вместо разбросанных print(). Повторные вызовы
    идемпотентны (не навешивают дубли handler'ов).
    """
    root = logging.getLogger()
    if not any(getattr(h, "_ping_bot", False) for h in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
        ))
        handler._ping_bot = True  # sentinel для идемпотентности
        root.addHandler(handler)
    root.setLevel((level or os.getenv("LOG_LEVEL", "INFO")).upper())


setup_logging()  # до load_env(): LOG_LEVEL из окружения уже учтён, .env — ниже
logger = logging.getLogger("config")

load_env()

TOKEN = os.getenv("TOKEN")
PROXY_URL = os.getenv("PROXY_URL") or None

ALERT_CHAT_ID = normalize_int(os.getenv("ALERT_CHAT_ID"), "ALERT_CHAT_ID")
ALERT_THREAD_ID = normalize_int(os.getenv("ALERT_THREAD_ID"), "ALERT_THREAD_ID")

if not TOKEN:
    raise ValueError("❌ Не найден TOKEN в переменных окружения или файле .env!")

# ─────────────────────────── Runtim-объекты ─────────────────────────────────

throttle = PingThrottle(MAX_CONCURRENT_PINGS)

session = build_session(PROXY_URL)
bot = Bot(token=TOKEN, session=session)
dp = Dispatcher(storage=MemoryStorage())


def set_proxy(new_url):
    """Атомарная смена прокси/сессии бота (используется админ-панелью).

    Обновляет config.PROXY_URL, пересоздаёт сессию через build_session()
    (валидный AiohttpSession даже при отключённом прокси — Б12) и подменяет
    bot.session живого Bot без перезапуска процесса. Возвращает СТАРУЮ сессию
    (или None) — вызывающая сторона ОБЯЗАНА закрыть её асинхронно через
    `await old.close()` (Этап 6), чтобы не оставлять висящих соединений.
    """
    global PROXY_URL, session
    new_url = (new_url or "").strip() or None
    old_session = session
    new_session = build_session(new_url)
    bot.session = new_session
    session = new_session
    PROXY_URL = new_url
    save_env_value("PROXY_URL", new_url or "")
    return old_session
