"""Чистое ядро логики бота БЕЗ Telegram-объектов (Этап 8: тестопригодность).

Модуль не создаёт Bot/Dispatcher на импорте, поэтому его можно безопасно
юнит-тестировать. `config.py` импортирует отсюда ВСЕ символы (реэкспорт) —
публичный API не изменился, рантайм-объекты (bot, dp, throttle, сессии)
остались в config.py.

Состав: env-парсер, PingThrottle, ActiveUserRegistry, фабрика сессий,
файловые пути/инициализация, единый пинг (Б19), валидация/разворот целей
(Б4, Б3, Б9), история (save_to_history), сборка дерева /tree (Б8, Б23, Б17),
форматирование отчётов/алертов мониторинга (Б1).
"""

import asyncio
import base64
import ipaddress
import json
import logging
import os
from html import escape as _html_escape
from pathlib import Path

from aiogram.client.session.aiohttp import AiohttpSession

logger = logging.getLogger("config")

# ─────────────────────────── Пути/константы ─────────────────────────────────
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
RESULTS_DIR = DATA_DIR / "results"
ENV_FILE = os.getenv("ENV_FILE", str(Path(__file__).resolve().parent / ".env"))

HISTORY_FILE = str(DATA_DIR / "networks_history.json")
TRACKED_IPS_FILE = str(DATA_DIR / "tracked_ips.json")
USERS_FILE = str(DATA_DIR / "users.json")

ADMIN_ID = int(os.getenv("ADMIN_ID", "1424727277") or 1424727277)

try:
    MAX_CONCURRENT_PINGS = max(1, int(os.getenv("MAX_CONCURRENT_PINGS", "3")))
except (TypeError, ValueError):
    MAX_CONCURRENT_PINGS = 3

HISTORY_MAX_ENTRIES = 500
# Б23: лимит сообщения Telegram — 4096 символов; паккуем с запасом.
MAX_BLOCK_LEN = 3500


def result_path_for(user_id: int) -> Path:
    """Уникальный файл отчёта для каждого пользователя (Б18)."""
    return RESULTS_DIR / f"result_{user_id}.txt"


# ─────────────────────────── Парсер .env ────────────────────────────────────


def parse_env_line(line: str) -> tuple[str, str] | None:
    """Парсит одну строку .env → (key, value) или None (комментарий/пусто).

    Правила: пустые строки и строки с '#' в начале пропускаются; значение в
    кавычках берётся целиком из кавычек (хвост-комментарий отбрасывается),
    некавыченное значение обрезается по инлайн-комментарию ' #'.
    """
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    key, value = line.split("=", 1)
    value = value.strip()
    if value[:1] not in ('"', "'"):
        value = value.split("#", 1)[0].strip()
    else:
        quote = value[0]
        end = value.find(quote, 1)
        value = value[1:end] if end != -1 else value.strip(quote)
    return key.strip(), value


def iter_env_pairs(path=None):
    if path is None:
        path = ENV_FILE
    if not os.path.exists(path):
        return []
    pairs = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            parsed = parse_env_line(line)
            if parsed:
                pairs.append(parsed)
    return pairs


def load_env(path=None):
    for key, value in iter_env_pairs(path):
        os.environ[key] = value


def save_env_value(key_to_update, new_value):
    lines = []
    found = False
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()

    new_lines = []
    for line in lines:
        # Сопоставление по ключу с учётом пробелов вокруг «=»: старый код
        # искал «PROXY_URL=», а в .env строка вида «PROXY_URL = "..."» —
        # из-за этого значение не заменялось, а дублировалось новой строкой.
        stripped = line.strip()
        if stripped.startswith(f"{key_to_update}=") or \
                ("=" in stripped and stripped.split("=", 1)[0].strip() == key_to_update):
            new_lines.append(f"{key_to_update}={new_value}\n")
            found = True
        else:
            new_lines.append(line)

    if not found:
        new_lines.append(f"{key_to_update}={new_value}\n")

    # Этап 7: запись через temp-файл + атомарный replace. Прямое открытие на
    # запись ломается, если ENV_FILE смонтирован как read-only bind-mount
    # (single-file mount в Docker), и оставляет файл обрезанным при сбое
    # диска. При ro-монтаже админская замена значения остаётся активной в
    # текущем процессе (os.environ ниже) — предупреждаем обновить файл на хосте.
    tmp_path = f"{ENV_FILE}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        os.replace(tmp_path, ENV_FILE)
    except OSError as e:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        logger.warning("Не удалось записать %s в %s: %r. Значение применено "
                       "в текущем процессе; обновите файл вручную.",
                       key_to_update, ENV_FILE, e)
    os.environ[key_to_update] = str(new_value)


def normalize_int(raw, name):
    """Жёсткая нормализация числовых env-переменных: int или None (Б20)."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        logger.warning("%s=%r не является числом — значение проигнорировано", name, raw)
        return None


# ─────────────────────────── Семафор (Б11, Б13) ─────────────────────────────


class PingThrottle:
    """Мутируемая обёртка над asyncio.Semaphore.

    Все модули работают с одним и тем же объектом `throttle` (импорт имени
    объекта, а не привязка к конкретному Semaphore), поэтому админская смена
    лимита через resize()/set_limit() реально применяется «на лету» ко всем
    сканам (Б11). Публичные свойства `free`/`busy` заменяют хрупкий доступ к
    sem._value (Б13).
    """

    def __init__(self, limit: int) -> None:
        self.limit = max(1, int(limit))
        self._sem = asyncio.Semaphore(self.limit)

    def resize(self, new_limit: int) -> None:
        """Публичное имя из плана Этапа 6 (эквивалент set_limit)."""
        self.set_limit(new_limit)

    def set_limit(self, new_limit: int) -> None:
        """Изменение лимита на лету без пересоздания объекта.

        Увеличение — освобождаем недостающие слоты; уменьшение — забираем
        только СВОБОДНЫЕ слоты (занятые сверх нового лимита доедут
        естественным образом). Активные `async with throttle()` не ломаются.

        Фикс deadlock (Шаг 0): раньше уменьшение выполнялось в цикле
        `if self._sem.locked(): break` + ручное `_value -= 1`. Если все
        слоты заняты/в очереди, а delta отрицательная, семафор мог уйти в
        состояние, где освобождённый активным holder'ом слот доставался
        «украденному» резерву, и ожидающая задача не просыпалась никогда
        (воспроизводилось тестом test_busy_never_negative_when_shrunk_below_active
        и в бою: админ режет лимит посреди скана — скан зависает). Теперь
        уменьшение делает ровно одну неблокирующую попытку `acquire` через
        публичный API: либо успешно забирает свободный слот, либо ничего не
        делает (овербуки доезжают естественно). Никаких мутаций `_value`,
        никаких фоновых задач-адамонтов.
        """
        new_limit = max(1, int(new_limit))
        delta = new_limit - self.limit
        self.limit = new_limit
        if delta > 0:
            for _ in range(delta):
                self._sem.release()
        elif delta < 0:
            # Забираем не больше (-delta) свободных слотов, и только те,
            # которые реально свободны ПРЯМО СЕЙЧАС (non-blocking acquire).
            for _ in range(-delta):
                if self._sem.locked():
                    break  # свободных нет — занятые доедут сами
                try:
                    self._sem.acquire_nowait()
                except (RuntimeError, OSError):  # pragma: no cover - защита
                    break

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


class ActiveUserRegistry:
    """Реестр активных интерактивных процессов (Б6, Б22).

    Вместо «вечного» set — dict {user_id: started_ts}: если процесс по
    какой-то причине не освободил флаг (упавший контейнер посреди скана),
    запись устаревает через `ttl` секунд и автоматически чистится при
    следующей проверке. Ограничение «1 процесс на пользователя» относится
    только к интерактивным командам (/scan, /check_ip); фоновый ежечасный
    мониторинг реестр не трогает (Б22).
    """

    def __init__(self, ttl: float = 30 * 60) -> None:
        self._started: dict[int, float] = {}
        self.ttl = ttl

    def _is_stale(self, user_id: int, now: float) -> bool:
        ts = self._started.get(user_id)
        return ts is not None and (now - ts) > self.ttl

    def try_acquire(self, user_id: int) -> bool:
        """Занять слот процесса; False — если у пользователя уже активен процесс."""
        now = asyncio.get_running_loop().time()
        if self._is_stale(user_id, now):
            self._started.pop(user_id, None)  # stale-очистка зависшей записи
        if user_id in self._started:
            return False
        self._started[user_id] = now
        return True

    def is_active(self, user_id: int) -> bool:
        now = asyncio.get_running_loop().time()
        if self._is_stale(user_id, now):
            self._started.pop(user_id, None)
            return False
        return user_id in self._started

    def release(self, user_id: int) -> None:
        self._started.pop(user_id, None)

    def __len__(self) -> int:
        return len(self._started)


active_users = ActiveUserRegistry()


def build_session(new_url):
    """Фабрика сессий бота.

    ВАЖНО (Б12): при отключённом прокси ВСЕГДА возвращается новый валидный
    объект AiohttpSession() БЕЗ proxy — никогда None. Присваивание
    bot.session = None ломает aiogram на следующем же запросе.
    """
    return AiohttpSession(proxy=new_url) if new_url else AiohttpSession()


# ─────────────────────────── Инициализация файлов ───────────────────────────


def init_files():
    """Создаёт структуру data/ и файлы по умолчанию; мигрирует старые данные."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    # Однократная миграция legacy-файлов из корня репозитория в data/
    # (Этап 7: раньше данные лежали в корне и монтировались томом всего
    # репозитория; теперь — только ./data).
    repo_root = Path(__file__).resolve().parent
    for legacy, dest in (
        ("networks_history.json", HISTORY_FILE),
        ("tracked_ips.json", TRACKED_IPS_FILE),
        ("users.json", USERS_FILE),
    ):
        legacy_path = repo_root / legacy
        if legacy_path.exists() and not os.path.exists(dest):
            legacy_path.rename(dest)

    default_content = {HISTORY_FILE: "[]", TRACKED_IPS_FILE: "{}", USERS_FILE: "[]"}
    for f, empty in default_content.items():
        if not os.path.exists(f):
            with open(f, "w", encoding="utf-8") as fh:
                fh.write(empty)


# ─────────────────────────── Единый пинг (Б19) ──────────────────────────────


async def async_ping_ip(ip: str, timeout: float = 1.0, throttle_obj=None) -> bool:
    """Единая асинхронная проверка доступности хоста.

    throttle_obj — опциональная подмена ограничителя (для тестов); в бою
    используется глобальный `throttle` из config (импортируется лениво,
    чтобы админский resize() действовал мгновенно — Б11).
    """
    if throttle_obj is None:
        import config  # ленивый импорт: throttle живёт в config (runtime-слой)
        throttle_obj = config.throttle
    async with throttle_obj():
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


async def ping_many(targets, timeout: float = 1.0, throttle_obj=None):
    """Параллельный обход целей через единый пул слотов throttle.

    Возвращает {ip: alive}. Задача целиком, а не чанк-в-чанк, — очередь
    слотов продвигается без «холостых» ожиданий самого медленного хоста.
    """
    results = await asyncio.gather(
        *(async_ping_ip(t, timeout, throttle_obj) for t in targets),
        return_exceptions=True,
    )
    return {t: (r is True) for t, r in zip(targets, results)}


# ─────────────────────────── Валидация IP (Б4, Б3) ──────────────────────────


def is_valid_ipv4(target) -> bool:
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


def expand_targets(raw_args):
    """Разворачивает аргументы команды в плоский список целей (Б4, Б3).

    Возвращает (targets, invalid):
      * одиночные валидные IP (дедуп);
      * подсети x.x.x.0 → 256 адресов x.x.x.0..x.x.x.255;
      * x.x.0.0 запрещены (попадают в invalid согласно ТЗ);
      * всё, не прошедшее строгую валидацию, — в invalid.
    """
    targets, invalid, _ = classify_targets(raw_args)
    return targets, invalid


def classify_targets(raw_args):
    """Как expand_targets, но возвращает ещё и план истории (Б9, Б3).

    Третье значение — список записей для networks_history.json:
      * {"type": "subnet", "target": "x.x.x.0", ...} — только для явно
        запрошенных подсетей (полный обход 256 адресов);
      * {"type": "host", "target": "<ip>", ...} — для одиночных IP.
    Одиночные IP больше НЕ превращаются в псевдо-подсети /24 (Б9), а
    запрещённые сети x.x.0.0 отсекаются на входе и не попадают ни в цели,
    ни в историю (Б3 — закрытие обхода блокировки через историю).
    """
    targets: list[str] = []
    invalid: list[str] = []
    history_plan: list[dict] = []
    seen: set[str] = set()
    seen_subnets: set[str] = set()

    for item in raw_args:
        item = item.strip().rstrip(",;")
        if not item:
            continue
        if not is_valid_ipv4(item):
            invalid.append(item)
            continue
        if item.endswith(".0"):
            if item.endswith(".0.0"):
                # Запрещённая сеть x.x.0.0: не сканируется и не сохраняется (Б3)
                invalid.append(item)
                continue
            base = item[:-2]  # отрезаем ".0"
            if item not in seen_subnets:
                seen_subnets.add(item)
                history_plan.append({"type": "subnet", "target": item})
            for i in range(256):
                ip = f"{base}.{i}"
                if ip not in seen:
                    seen.add(ip)
                    targets.append(ip)
        elif item not in seen:
            seen.add(item)
            targets.append(item)
            history_plan.append({"type": "host", "target": item})

    return targets, invalid, history_plan


# ─────────────────────────── История сетей (Б9, Б3) ─────────────────────────


def save_to_history(entries):
    """Сохранение результатов скана в networks_history.json (Б9, Б3).

    entries — список {"type": "subnet"|"host", "target", "alive", "total",
    "percent", "time", "scanned_by"}. Запись перезаменяет предыдущий срез
    той же цели (dedupe по target), всего хранится не более
    HISTORY_MAX_ENTRIES последних записей.
    """
    history = []
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            # Однократная миграция legacy-записей без поля type → subnet
            for item in loaded:
                if isinstance(item, dict) and "target" in item:
                    item.setdefault("type", "subnet")
                    history.append(item)
        except Exception:
            history = []

    new_targets = {e["target"] for e in entries}
    history = [item for item in history if item.get("target") not in new_targets]
    history.extend(entries)
    history = history[-HISTORY_MAX_ENTRIES:]

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=4)


# ─────────────────────────── Дерево /tree (Б8, Б23, Б17) ────────────────────


def _type_label(item):
    """Метка типа записи истории (Б9). Legacy-записи без поля type — сети."""
    return "🌐 сеть" if item.get("type", "subnet") == "subnet" else "📌 хост"


def _status_icon(percent):
    """Б8: пороги цветов через epsilon-сравнение.

    🟢 — полностью доступна (>= 99.999%), 🔴 — полностью недоступна (<= 0.001%),
    🔵 — всё, что между (0.1%..99.9% по ТЗ). Float-арифметика percent = alive/total*100
    может дать 99.99999999999999 или 1e-14 вместо точных границ, поэтому сравнение
    идёт с допуском, а не `== 100.0` / `== 0`.
    """
    if percent >= 100.0 - 0.001:   # >= 99.999
        return "🟢"
    if percent <= 0.001:            # <= 0.001
        return "🔴"
    return "🔵"


def _format_entry(idx, history_len, item):
    target = _html_escape(str(item["target"]))  # экранирование целей (Б17)
    percent = float(item["percent"])
    alive = item["alive"]
    total = item["total"]
    scan_time = _html_escape(str(item["time"]))
    branch = "└──" if idx == history_len - 1 else "├──"
    return (
        f"    {branch} {_status_icon(percent)} <code>{target}</code> "
        f"[{_type_label(item)}] — <b>{percent:.2f}%</b> ({alive}/{total}) "
        f"<i>[{scan_time}]</i>\n"
    )


TREE_HEADER = "🌳 <b>Дерево отсканированных целей и сетей:</b>\n"
TREE_LEGEND = (
    "\n<b>Легенда статусов:</b>\n"
    "🟢 Полностью доступна (незаблочена)\n"
    "🔵 Частично доступна (наполовину заблочена)\n"
    "🔴 Полностью недоступна (фул заблочена)"
)


def build_tree_blocks(history):
    """Собирает дерево и пакует вывод в блоки <= MAX_BLOCK_LEN символов (Б23)."""
    tree_head = TREE_HEADER + "└── 📁 <b>История проверок</b>\n"
    legend = TREE_LEGEND

    n = len(history)
    blocks = []
    current = tree_head

    for idx, item in enumerate(history):
        entry = _format_entry(idx, n, item)
        if current != tree_head and len(current) + len(entry) > MAX_BLOCK_LEN:
            blocks.append(current.rstrip("\n"))
            current = "└── 📁 <b>История проверок (продолжение)</b>\n"
        current += entry

    if len(current) + len(legend) > MAX_BLOCK_LEN:
        # Легенда не влезает — выносим её отдельным финальным сообщением.
        blocks.append(current.rstrip("\n"))
        blocks.append(legend.lstrip("\n"))
    else:
        blocks.append((current + legend).rstrip("\n"))
    return blocks


# ─────────────────── Форматирование отчётов/алертов мониторинга ─────────────


def format_monitor_report(results) -> str:
    """Персональный ежечасный отчёт: [(ip, alive)] → HTML."""
    lines = ["⏰ <b>Ежечасный автоотчет мониторинга:</b>", "━" * 22]
    alive_count = 0
    for ip, alive in results:
        alive_count += 1 if alive else 0
        lines.append(f"{'🟢' if alive else '🔴'} <code>{_html_escape(str(ip))}</code>")
    dead_count = len(results) - alive_count
    lines.append("")
    lines.append(f"🟢 Доступно: <b>{alive_count}</b> | 🔴 Недоступно: <b>{dead_count}</b>")
    return "\n".join(lines)


def format_alert(user_id, dead_ips) -> str:
    """Формирует текст аварийного алерта с тегом владельца (Б1).

    Исправление критического бага: раньше `alert_text` был строкой, к которой
    вызывался `.append()` — это кидало AttributeError на каждой итерации и
    полностью убивало фоновый цикл мониторинга. Теперь используется список строк.
    """
    mention = f'<a href="tg://user?id={user_id}">Владелец</a>'
    alert_lines = [
        "🚨 <b>Внимание! Обнаружены недоступные хосты!</b>",
        f"👤 Владелец: {mention}",
        "━" * 22,
    ]
    alert_lines += [f"🔴 <code>{_html_escape(ip)}</code>" for ip in dead_ips]
    return "\n".join(alert_lines)


def register_user(user_id: int, users_file=None):
    """Регистрация uid в users.json (база рассылки/статистики)."""
    path = users_file or USERS_FILE
    if not os.path.exists(path):
        users = []
    else:
        try:
            with open(path, "r", encoding="utf-8") as f:
                users = json.load(f)
        except Exception:
            users = []
    if user_id not in users:
        users.append(user_id)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(users, f, ensure_ascii=False, indent=4)


# ─────────────── tracked_ips.json + кнопка «🧹 Убрать неактивные ip» ─────────

# Фича 2: inline-кнопка очистки мёртвых IP из мониторинга. callback_data в
# Telegram ограничен 64 байтами, поэтому dead-IP кодируются компактно: каждый
# IPv4 — 4 байта, base64url без паддинга. Префикс зарезервирован под другие
# колбэки, чтобы хендлер фильтровал «свои» данные.
CLEANUP_CB_PREFIX = "c:"
# 64 байта — жёсткий лимит callback_data. Префикс 'c:' (2) + base64url без
# паддинга: ceil(4N/3). При N=15 выходит ровно 64 — берём на один меньше,
# чтобы гарантированно быть СТРОГО меньше лимита.
DEAD_IPS_MAX = 14


def load_tracked_ips(path=None) -> dict:
    """Чтение tracked_ips.json {uid_str: [ip,...]}; повреждённый файл → {}."""
    p = path or TRACKED_IPS_FILE
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.warning("Не удалось прочитать %s — считаем список пустым", p)
        return {}


def save_tracked_ips(data: dict, path=None) -> None:
    """Атомарная запись tracked_ips.json (temp + replace, как save_env_value)."""
    p = path or TRACKED_IPS_FILE
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.replace(tmp, p)


def encode_dead_payload(dead_ips) -> str:
    """Список dead-IPv4 → base64url-строка без паддинга ('' если пусто/нет лимита).

    Каждый адрес упакован в 4 байта; итог вместе с CLEANUP_CB_PREFIX должен
    гарантированно вписываться в лимит callback_data (64 байта ASCII), поэтому
    список предварительно дедуплицируется с сохранением порядка и режется до
    DEAD_IPS_MAX записей. Невалидные адреса пропускаются, а не роняют кнопку.
    """
    seen: set[str] = set()
    packed = bytearray()
    for ip in dead_ips:
        s = str(ip).strip()
        if s in seen or not is_valid_ipv4(s):
            continue
        seen.add(s)
        packed += ipaddress.IPv4Address(s).packed
        if len(seen) >= DEAD_IPS_MAX:
            break
    if not seen:
        return ""
    return base64.urlsafe_b64encode(bytes(packed)).decode("ascii").rstrip("=")


def decode_dead_payload(payload: str) -> list[str]:
    """Обратная операция к encode_dead_payload; битый payload → []."""
    if not payload:
        return []
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    except Exception:
        logger.warning("Битый callback payload: %r", payload)
        return []
    if len(raw) % 4:
        return []
    return [str(ipaddress.IPv4Address(raw[i:i + 4])) for i in range(0, len(raw), 4)]


def remove_dead_ips(user_id, dead_ips, path=None) -> tuple[list[str], list[str]]:
    """Идемпотентное удаление dead-IP из мониторинга пользователя (фича 2).

    Удаляются ТОЛЬКО адреса, реально присутствующие в списке этого юзера:
    повторный нажатый колбэк ничего не ломает, чужие списки не затрагиваются
    (ключ — строго str(user_id) нажимающего). Пустой список после удаления
    оставляем как [] — структура tracked_ips.json не меняется.

    Возвращает (removed, skipped): фактически удалённые и пропущенные адреса.
    """
    data = load_tracked_ips(path)
    key = str(user_id)
    user_ips = data.get(key, [])
    if not isinstance(user_ips, list):
        user_ips = []
    to_remove = {str(ip).strip() for ip in dead_ips}
    removed = [ip for ip in user_ips if ip in to_remove]
    kept = [ip for ip in user_ips if ip not in to_remove]
    skipped = sorted(to_remove - set(removed))
    if removed:
        data[key] = kept
        save_tracked_ips(data, path)
    return removed, skipped
