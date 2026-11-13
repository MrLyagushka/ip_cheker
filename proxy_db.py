"""SQLite-хранилище прокси бота (замена .env / JSON для списка прокси).

Требование пользователя: «прокси сохраняются в БД, а не в .env».

Схема:
    proxies(id INTEGER PK, url TEXT UNIQUE NOT NULL, active INTEGER DEFAULT 0,
            created_at TEXT DEFAULT <ISO UTC>)

Инвариант: активным может быть не более одного прокси (`active=1`) — он задаёт
`config.PROXY_URL` и базовую сессию бота при старте; если активных нет, бот
работает напрямую. Пул ротации (proxy_pool) всегда = все строки таблицы.

Почему sqlite3 синхронно: операции — единичные короткие запросы к локальному
файлу data/proxies.db (микросекунды), событийный цикл aiogram не блокируется
заметно; отдельная connection-обёртка на каждую операцию делает модуль
потокобезопасным и пригодным для тестов. `check_same_thread=False` + кэш
соединений нужны потому, что asyncio.to_thread / executor могут сменить тред
между вызовами.

Миграция (вызывается один раз при старте из proxy_pool.configure_rotation):
* строка PROXY_URL из .env → INSERT OR IGNORE в таблицу;
* data/proxies.json (временное хранилище прошлой итерации) → те же строки;
* после переноса ключ PROXY_URL удаляется из .env, файл proxies.json удаляется.
Дальнейшие изменения прокси в .env НЕ пишутся вообще.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Разрешённые схемы прокси-URL (aiohttp/aiosocks понимают эти четыре).
_SCHEMES = ("socks5://", "socks5h://", "socks4://", "http://", "https://")


def _default_db_path() -> str:
    data_dir = os.getenv("DATA_DIR", "data")
    return str(Path(data_dir) / "proxies.db")


def normalize_url(url: str | None) -> str | None:
    """Возвращает чистый proxy-URL или None, если строка не является прокси."""
    if not url:
        return None
    u = url.strip().strip('"').strip("'")
    if not u or any(ch.isspace() for ch in u):
        return None
    low = u.lower()
    if not any(low.startswith(s) for s in _SCHEMES):
        return None
    try:
        rest = u.split("://", 1)[1]
        hostport = rest.rsplit("@", 1)[-1]
        host, _, port = hostport.partition(":")
    except Exception:
        return None
    if not host:
        return None
    if port and not port.isdigit():
        return None
    return u


def mask_url(url: str) -> str:
    """Proxy-URL без пароля — для логов и сообщений."""
    try:
        if "://" in url and "@" in url:
            scheme, rest = url.split("://", 1)
            creds, host = rest.rsplit("@", 1)
            user = creds.split(":", 1)[0]
            return f"{scheme}://{user}:***@{host}"
    except Exception:
        pass
    return "***"


class ProxyDB:
    """Хранилище пула прокси в SQLite (см. docstring модуля)."""

    def __init__(self, path: str | None = None):
        self._path = path or _default_db_path()
        self._conn: sqlite3.Connection | None = None

    # ── низкоуровневое ──

    @property
    def path(self) -> str:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            p = Path(self._path)
            if p.parent and str(p.parent) not in ("", "."):
                p.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self._path, timeout=5.0,
                                         check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS proxies (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    url        TEXT UNIQUE NOT NULL COLLATE NOCASE,
                    active     INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_proxies_active "
                "ON proxies(active) WHERE active = 1"
            )
            self._conn.commit()
        return self._conn

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # pragma: no cover - шум закрытия
                pass
            self._conn = None

    # ── CRUD ──

    def list_urls(self) -> list[str]:
        """Все прокси в порядке добавления (пул ротации)."""
        try:
            cur = self._connect().execute("SELECT url FROM proxies ORDER BY id")
            return [r[0] for r in cur.fetchall()]
        except sqlite3.Error as e:
            logger.error("ProxyDB.list_urls: %r", e)
            return []

    def count(self) -> int:
        try:
            cur = self._connect().execute("SELECT COUNT(*) FROM proxies")
            return int(cur.fetchone()[0])
        except sqlite3.Error:
            return 0

    def get_active(self) -> str | None:
        try:
            cur = self._connect().execute(
                "SELECT url FROM proxies WHERE active = 1 LIMIT 1")
            row = cur.fetchone()
            return row[0] if row else None
        except sqlite3.Error as e:
            logger.error("ProxyDB.get_active: %r", e)
            return None

    def add(self, url: str, *, active: bool | None = None) -> bool:
        """Добавляет прокси. False — пусто/некорректный URL/дубликат.

        active=None — авто: первым добавленным прокси становится активный
        (если активного нет), чтобы поведение совпадало со старой схемой
        «PROXY_URL = первый прокси».
        """
        u = normalize_url(url)
        if not u:
            return False
        make_active = active
        try:
            conn = self._connect()
            if make_active is None:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM proxies WHERE active = 1")
                make_active = cur.fetchone()[0] == 0
            conn.execute(
                "INSERT INTO proxies (url, active, created_at) VALUES (?, ?, ?)",
                (u, 1 if make_active else 0, self._now()))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
        except sqlite3.Error as e:
            logger.error("ProxyDB.add(%s): %r", mask_url(u), e)
            return False

    def remove(self, url: str) -> bool:
        """Удаляет прокси по точному URL (NOCASE)."""
        u = (url or "").strip()
        if not u:
            return False
        try:
            conn = self._connect()
            cur = conn.execute("DELETE FROM proxies WHERE url = ?", (u,))
            conn.commit()
            return cur.rowcount > 0
        except sqlite3.Error as e:
            logger.error("ProxyDB.remove(%s): %r", mask_url(u), e)
            return False

    def set_active(self, url: str | None) -> bool:
        """Делает `url` активным (единственный в таблице); None — снять всех.

        Успех True, даже если url уже был активным. Если URL есть в .env, но
        ещё не в БД — сначала добавляется.
        """
        u = normalize_url(url) if url else None
        try:
            conn = self._connect()
            if u is None:
                conn.execute("UPDATE proxies SET active = 0")
                conn.commit()
                return True
            cur = conn.execute("SELECT 1 FROM proxies WHERE url = ?", (u,))
            if cur.fetchone() is None:
                try:
                    conn.execute(
                        "INSERT INTO proxies (url, active, created_at) "
                        "VALUES (?, 0, ?)", (u, self._now()))
                except sqlite3.IntegrityError:
                    pass
            # Индекс ux_proxies_active UNIQUE: снимаем старых до включения
            # нового — иначе IntegrityError при двух активных.
            conn.execute("UPDATE proxies SET active = 0")
            conn.execute("UPDATE proxies SET active = 1 WHERE url = ?", (u,))
            conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error("ProxyDB.set_active(%s): %r", mask_url(u) if u else "None", e)
            return False

    def clear(self) -> int:
        """Удаляет все прокси (кнопка «clear» в админке). Возвращает число."""
        try:
            conn = self._connect()
            cur = conn.execute("DELETE FROM proxies")
            conn.commit()
            return cur.rowcount
        except sqlite3.Error as e:
            logger.error("ProxyDB.clear: %r", e)
            return 0


# ─────────────────────────── Глобальный экземпляр ───────────────────────────

db = ProxyDB()


def load_proxies(path: str | None = None) -> list[str]:
    """API-совместимая обёртка: список URL из БД (или temp-БД — для тестов)."""
    if path is not None:
        return ProxyDB(path).list_urls()
    return db.list_urls()


def save_proxies(urls: list[str], path: str | None = None) -> bool:
    """Заменяет содержимое БД списком URL (атомарно в одной транзакции).

    Активным остаётся прежний активный URL, если он есть в новом списке,
    иначе — первый элемент списка (совместимо со старой семантикой).
    """
    cleaned = [u for u in (normalize_url(x) for x in (urls or [])) if u]
    target = ProxyDB(path) if path is not None else db
    try:
        conn = target._connect()
        prev_active = target.get_active()
        new_active = prev_active if prev_active in cleaned else (
            cleaned[0] if cleaned else None)
        conn.execute("DELETE FROM proxies")
        for u in cleaned:
            conn.execute(
                "INSERT INTO proxies (url, active, created_at) VALUES (?, ?, ?)",
                (u, 1 if u == new_active else 0, target._now()))
        conn.commit()
        return True
    except sqlite3.Error as e:
        logger.error("save_proxies(BD): %r", e)
        return False


# ───────────────────────────── Миграция ─────────────────────────────────────

_ENV_KEY_RE = re.compile(r"^PROXY_URL\s*=", re.IGNORECASE)


def migrate_legacy(env_file: str | None = None,
                   json_path: str | None = None) -> list[str]:
    """Однократный перенос прокси из .env (PROXY_URL) и proxies.json в SQLite.

    Возвращает список URL, оказавшихся в БД после миграции. Ключ PROXY_URL
    удаляется из .env (секрет больше не должен лежать в env-файле), временный
    proxies.json удаляется. Ошибки ФС логируются, но не валят старт бота.
    """
    env_file = env_file if env_file is not None else os.getenv("ENV_FILE", ".env")
    json_path = json_path or str(Path(_default_db_path()).parent / "proxies.json")

    urls: list[str] = []

    # 1) .env: PROXY_URL=...
    env_value: str | None = None
    try:
        with open(env_file, "r", encoding="utf-8") as fh:
            for line in fh:
                parsed_key = line.split("=", 1)[0].strip().upper()
                if parsed_key == "PROXY_URL":
                    env_value = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    except OSError:
        pass
    normalized_env = normalize_url(env_value)
    if normalized_env:
        urls.append(normalized_env)
        if db.add(normalized_env):
            logger.info("Миграция: PROXY_URL из .env перенесён в БД (%s)",
                        mask_url(normalized_env))

    # 2) старый JSON-список (прошлая итерация хранения)
    try:
        with open(json_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            for item in data:
                u = normalize_url(str(item))
                if u and u not in urls:
                    urls.append(u)
                    db.add(u)
    except (OSError, ValueError):
        pass

    # 3) зачистить старые хранилища
    try:
        with open(env_file, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        kept = [ln for ln in lines if not _ENV_KEY_RE.match(ln.strip())]
        if len(kept) != len(lines):
            tmp = f"{env_file}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.writelines(kept)
            os.replace(tmp, env_file)
            logger.info("Миграция: ключ PROXY_URL удалён из %s", env_file)
    except OSError as e:
        logger.warning("Не удалось убрать PROXY_URL из %s: %r "
                       "(прокси уже в БД — обновите файл вручную)", env_file, e)

    try:
        if os.path.exists(json_path):
            os.remove(json_path)
            logger.info("Миграция: %s удалён (данные в %s)",
                        json_path, db.path)
    except OSError as e:  # pragma: no cover
        logger.warning("Не удалось удалить %s: %r", json_path, e)

    return db.list_urls() or urls
