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

# Пул прокси: список в data/proxies.json, рандомный прокси на каждый запрос к
# Telegram API и ротация на другой прокси при ошибке соединения (см. proxy_pool).
import proxy_pool
from proxy_pool import ProxyPool, pool as proxy_pool_instance

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


def _initial_proxy_urls() -> list[str]:
    """Список прокси на старте: data/proxies.json; если пусто — PROXY_URL из .env."""
    urls = proxy_pool.load_proxies()
    if not urls and PROXY_URL:
        urls = [PROXY_URL]
        proxy_pool.save_proxies(urls)
    return urls


# Ротация прокси: bot.session заменяется обёрткой, выбирающей случайный прокси
# из пула на каждый запрос и переключающейся на другой при ошибке соединения.
proxy_pool.setup_rotation(bot, build_session, _initial_proxy_urls())


def set_proxy(new_url):
    """Атомарная смена ЕДИНСТВЕННОГО прокси бота (используется админ-панелью).

    Обновляет config.PROXY_URL, пересоздаёт сессию через build_session()
    (валидный AiohttpSession даже при отключённом прокси — Б12), подменяет
    базовую сессию живого Bot без перезапуска процесса и синхронизирует пул
    ротации (список прокси = [new_url], либо пуст при отключении). Возвращает
    СТАРУЮ сессию (или None) — вызывающая сторона ОБЯЗАНА закрыть её
    асинхронно через `await old.close()` (Этап 6).

    Для накопительного списка прокси (рандомный выбор + ротация при ошибках)
    служит pool.add()/pool.replace_all() — см. handlers/admin.py «🌐 Пул прокси».
    """
    global PROXY_URL, session
    new_url = (new_url or "").strip() or None
    base = getattr(bot.session, "_base", bot.session)
    old_session = base
    new_session = build_session(new_url)
    if isinstance(bot.session, proxy_pool.RotatingSessionProxy):
        bot.session._base = new_session
    else:  # ротация не установлена — меняем сессию напрямую (старое поведение)
        bot.session = new_session
    session = new_session
    PROXY_URL = new_url
    save_env_value("PROXY_URL", new_url or "")
    # Пул должен пережить смену: иначе запросы пойдут через старый (закрываемый) прокси.
    try:
        import asyncio
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        loop.create_task(proxy_pool.pool.replace_all([new_url] if new_url else []))
    else:  # pragma: no cover - только вне event loop (тесты/CLI)
        proxy_pool.pool.configure([new_url] if new_url else [])
    return old_session
