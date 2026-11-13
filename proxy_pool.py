"""Пул прокси бота: список в SQLite (data/proxies.db) + рандомный прокси на запрос.

Как это работает (требование пользователя):
* `/get_proxy` — генерирует логин/пароль и команду `docker run ... serjs/go-socks5-proxy`;
  пользователь запускает контейнер на своём хосте;
* админ добавляет полученный `socks5://user:pass@host:1080` в список прокси
  (админ-панель → «🌐 Прокси»);
* прокси хранятся В БД (sqlite3, proxy_db.py), а не в .env — ключ PROXY_URL из
  .env мигрирует в таблицу при первом старте и больше там не используется;
* дальше бот выбирает СЛУЧАЙНЫЙ прокси из пула для каждого запроса к Telegram API;
* если Telegram ответил ошибкой соединения/таймаутом — берётся ДРУГОЙ прокси и
  запрос повторяется (не более MAX_SESSIONS_PER_REQUEST попыток за запрос).

Flood-control (TelegramRetryAfter) ротацию НЕ вызывает: лимит сообщений — не
проблема выхода в интернет, повтор через другой прокси его только усугубит.

Реализация: `RotatingSessionProxy` подменяет `bot.session` и при каждом запросе
подбирает сессию aiogram со случайным прокси. Сессии переиспользуются: коннектор
aiohttp у AiohttpSession создаётся лениво, поэтому смена `_connector_init` +
`_should_reset_connector = True` до первого запроса безопасно задаёт нужный прокси
(см. ProxyPool._apply). Если структура сессии не распознана — прокси НЕ
подменяется наугад, а используется базовая сессия как есть (без падения бота).

Модуль низкоуровневый: не импортирует config/delivery (нет циклических импортов),
фабрика сессий передаётся параметром.
"""

from __future__ import annotations

import asyncio
import logging
import random
import secrets
import string
from html import escape as _html_escape
from typing import Any

import certifi
import ssl as _ssl
from aiohttp import TCPConnector
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import TelegramMethod

# Хранилище прокси — SQLite (proxy_db), НЕ .env и НЕ proxies.json.
from proxy_db import (  # noqa: F401  (реэкспорт API для совместимости)
    ProxyDB, db as proxy_db, normalize_url, mask_url,
    load_proxies, save_proxies, migrate_legacy,
)

logger = logging.getLogger(__name__)

# Максимум разных прокси за один запрос (первый + ротации после сетевых ошибок).
MAX_SESSIONS_PER_REQUEST = 3

_USER_CHARS = string.ascii_lowercase + string.digits


def generate_credentials() -> tuple[str, str]:
    """Генерирует пару (user, password) для go-socks5-proxy.

    Только [a-z0-9] — значения безопасны без экранирования в URL прокси,
    docker run -e и .env.
    """
    user = "u" + "".join(secrets.choice(_USER_CHARS) for _ in range(7))   # 8 символов
    password = "".join(secrets.choice(_USER_CHARS) for _ in range(20))    # 20 символов
    return user, password


def build_docker_run(user: str, password: str, port: int = 1080) -> str:
    """Команда запуска SOCKS5-контейнера (шаблон из ТЗ, креды — сгенерированные).

    Имя контейнера уникализировано логином, чтобы несколько прокси можно было
    запускать на одном хосте с разными -p.
    """
    return (
        f"docker run -d --name socks5-{user} --restart always "
        f"-p {port}:{port} "
        f"-e PROXY_USER={user} -e PROXY_PASSWORD={password} "
        f"serjs/go-socks5-proxy"
    )


# ───────────────────────────── Пул сессий ───────────────────────────────────


class ProxyPool:
    """Кэш прокси-URL (источник истины — SQLite proxy_db) + выдача сессий."""

    def __init__(self, factory, storage: ProxyDB | None = None):
        self._factory = factory                      # callable(url|None) -> BaseSession
        self._db = storage if storage is not None else proxy_db
        self._urls: list[str] = []
        self._sessions: dict[str, BaseSession] = {}  # url -> кэшированная сессия
        self._active: set[int] = set()               # id() сессий в запросе сейчас
        self._lock = asyncio.Lock()
        self.enabled = False                         # False → работать через base (деградация)

    # ── управление списком ──

    def configure(self, urls: list[str]) -> None:
        """Синхронная загрузка списка (на старте бота, до первых запросов)."""
        self._urls = [u.strip() for u in (urls or []) if u and u.strip()]
        self.enabled = True
        keep = set(self._urls)
        for gone in [k for k in self._sessions if k not in keep]:
            sess = self._sessions.pop(gone)
            self._schedule_close(sess)

    @property
    def db(self) -> ProxyDB:
        """БД прокси (для админ-хендлеров: get_active/set_active/count)."""
        return self._db

    @property
    def urls(self) -> list[str]:
        return list(self._urls)

    async def add(self, url: str) -> bool:
        """Добавляет прокси в пул (с сохранением в БД). False — дубликат/пусто.

        Первым добавленным прокси становится активным (если активного нет) —
        решение принимает proxy_db.add().
        """
        normalized = normalize_url(url)
        if not normalized:
            return False
        async with self._lock:
            if not self._db.add(normalized):
                return False
            if normalized not in self._urls:
                self._urls.append(normalized)
        return True

    async def remove(self, url: str) -> bool:
        async with self._lock:
            if not self._db.remove(url):
                # строки в БД нет — возможно, рассинхрон кэша; чиним его
                if url in self._urls:
                    self._urls.remove(url)
                return False
            if url in self._urls:
                self._urls.remove(url)
            sess = self._sessions.pop(url, None)
        if sess is not None and id(sess) not in self._active:
            await self._close(sess)
        return True

    async def replace_all(self, urls: list[str]) -> None:
        """Заменяет весь список в БД и кэше (админский «clear»/массовая замена)."""
        cleaned = [u for u in (normalize_url(x) for x in (urls or [])) if u]
        async with self._lock:
            save_proxies(cleaned)          # запись в SQLite (proxy_db.db)
            self._urls = cleaned
            keep = set(cleaned)
            stale = [k for k in self._sessions if k not in keep]
            for k in stale:
                sess = self._sessions.pop(k)
                if id(sess) not in self._active:
                    self._schedule_close(sess)

    # ── выдача сессий для запросов ──

    def pick_urls(self, exclude: set[str] | None = None) -> list[str]:
        """Случайный порядок прокси: первый — «рандомный выбор», остальные — ротация."""
        pool = [u for u in self._urls if u not in (exclude or set())]
        random.shuffle(pool)
        return pool

    async def get_session(self, url: str) -> BaseSession | None:
        """Сессия под прокси `url` (кэш по URL). None — прокси не применим (base-fallback)."""
        async with self._lock:
            sess = self._sessions.get(url)
            if sess is None:
                sess = self._factory(url)
                if not self._apply(sess, url):
                    # структура сессии не распознана — не кэшируем битую пару
                    sess = self._factory(None)
                    self._sessions[url] = sess
                    return sess
                self._sessions[url] = sess
            else:
                # переиспользование: коннектор ещё не создан (сессии ленивы) —
                # на всякий случай убеждаемся, что прокси соответствует URL ключа
                self._apply(sess, url)
            self._active.add(id(sess))
            return sess

    def release(self, sess: BaseSession) -> None:
        self._active.discard(id(sess))

    @staticmethod
    def _apply(sess: BaseSession, url: str | None) -> bool:
        """Задаёт прокси сессии ДО создания коннектора (сессии ленивы).

        Использует штатный AiohttpSession._setup_proxy_connector — тот сам
        разбирает socks5://user:pass@host:port через aiosocks и выставляет
        _connector_type/_connector_init. Сброс _should_reset_connector гарантирует,
        что ClientSession будет пересоздан с новым коннектором при первом запросе.

        Возвращает False, если структура сессии не распознана — вызывающий код
        тогда НЕ подменяет прокси наугад (работаем через базовую сессию).
        """
        if not hasattr(sess, "_connector_init"):
            return False
        try:
            if url:
                if hasattr(sess, "_setup_proxy_connector"):
                    sess._setup_proxy_connector(url)
                else:  # нестандартная сессия без поддержки прокси
                    return False
            else:
                sess._connector_type = TCPConnector
                sess._connector_init = {
                    "ssl": _ssl.create_default_context(cafile=certifi.where()),
                    "limit": sess._connector_init.get("limit", 100),
                    "ttl_dns_cache": 3600,
                }
                sess._proxy = None
            sess._should_reset_connector = True
            return True
        except Exception as e:  # pragma: no cover - защита от внутренних изменений aiogram
            logger.warning("Не удалось применить прокси %s к сессии: %r",
                           mask_url(url) if url else "None", e)
            return False

    def _schedule_close(self, sess: BaseSession) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._close(sess))

    @staticmethod
    async def _close(sess: BaseSession) -> None:
        try:
            await sess.close()
        except Exception as e:  # pragma: no cover - сетевые шумы закрытия
            logger.debug("Ошибка при закрытии сессии: %r", e)


# Глобальный пул; фабрика внедряется setup_rotation() из main/config-слоя.
pool = ProxyPool(factory=lambda url: None)


def configure_rotation(bot: Bot, session_factory) -> RotatingSessionProxy:
    """Точка входа для config.py: миграция старых данных + пул из БД + ротация.

    Порядок:
    1. migrate_legacy() — перенос PROXY_URL из .env и proxies.json в SQLite,
       зачистка ключа PROXY_URL из .env (прокси дальше живут только в БД);
    2. базовая сессия бота пересоздаётся под активный прокси из БД
       (если он задан) — иначе запросы шли бы через env-прокси в обход пула;
    3. setup_rotation() подключает обёртку с случайным выбором/ротацией.
    """
    urls = migrate_legacy() or load_proxies()
    active = proxy_db.get_active()
    base = getattr(bot.session, "_base", bot.session)
    if active and getattr(base, "proxy", None) != active:
        new_base = session_factory(active)
        if isinstance(bot.session, RotatingSessionProxy):
            bot.session._base = new_base
        else:
            bot.session = new_base
        try:
            import asyncio as _aio
            loop = _aio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:  # старая сессия ещё не открывала коннектор, но закрываем корректно
            async def _close_later(s=base):
                try:
                    await s.close()
                except Exception as e:  # pragma: no cover
                    logger.debug("Ошибка закрытия старой базовой сессии: %r", e)
            loop.create_task(_close_later())
    return setup_rotation(bot, session_factory, urls)


def setup_rotation(bot: Bot, session_factory, urls: list[str]) -> RotatingSessionProxy:
    """Инициализирует пул и подключает ротацию к боту (вызывается один раз на старте).

    `session_factory(url|None) -> BaseSession` — обычно config.build_session.
    Возвращает обёртку, установленную в bot.session.
    """
    pool._factory = session_factory
    pool.configure(urls)
    wrapper = RotatingSessionProxy(bot=bot, base=bot.session, pr=pool)
    bot.session = wrapper
    return wrapper


class RotatingSessionProxy(BaseSession):
    """Подмена bot.session: случайный прокси на запрос + ротация при сетевых ошибках.

    Наследуется от BaseSession, чтобы пройти isinstance-проверки aiogram
    (Polling session.api и т.п.). api/json_loads/json_dumps/timeout копируются
    с базовой сессии, close()/stream_content() делегируются ей же (закрытие
    пуловых сессий — ответственность ProxyPool).
    """

    def __init__(self, bot: Bot | None, base: BaseSession, pr: ProxyPool,
                 max_sessions: int = MAX_SESSIONS_PER_REQUEST):
        super().__init__(api=base.api, json_loads=base.json_loads,
                         json_dumps=base.json_dumps, timeout=base.timeout)
        self._base = base
        self._pool = pr
        self._max = max(1, max_sessions)

    async def close(self) -> None:
        await self._base.close()

    def stream_content(self, *args: Any, **kwargs: Any):
        return self._base.stream_content(*args, **kwargs)

    async def make_request(self, bot: Bot, method: TelegramMethod[TelegramMethod],
                           timeout: int | None = None):
        # Пул выключен/пуст или нет токена (тестовые заглушки) → прозрачно через base.
        if not (self._pool.enabled and self._pool.urls) or bot is None:
            return await self._base.make_request(bot, method, timeout=timeout)

        urls = self._pool.pick_urls()
        tried: set[str] = set()
        last_exc: Exception | None = None
        for url in urls[: self._max]:
            tried.add(url)
            sess = await self._pool.get_session(url)
            try:
                return await sess.make_request(bot, method, timeout=timeout)
            except (TelegramNetworkError, asyncio.TimeoutError) as e:
                last_exc = e
                logger.warning("Прокси %s не ответил (%s) — пробую другой из пула",
                               mask_url(url), type(e).__name__)
            finally:
                self._pool.release(sess)
        assert last_exc is not None  # цикл выполнялся минимум раз
        raise last_exc


def format_proxy_command_html(user: str, password: str, cmd: str) -> str:
    """HTML-текст сообщения /get_proxy (экранирование обязательного пользовательского ввода нет — значения генерируются, но escape для симметрии)."""
    return (
        "🔌 <b>Ваш личный SOCKS5-прокси</b>\n\n"
        "1️⃣ Запустите эту команду на сервере с Docker:\n"
        f"<code>{_html_escape(cmd)}</code>\n\n"
        "2️⃣ Данные доступа:\n"
        f"👤 Логин: <code>{_html_escape(user)}</code>\n"
        f"🔑 Пароль: <code>{_html_escape(password)}</code>\n"
        "🌐 Порт: <code>1080</code>\n\n"
        "3️⃣ Пришлите админу адрес вашего сервера — прокси вида\n"
        "<code>socks5://user:pass@IP:1080</code> будет добавлен в список, "
        "и бот начнёт выбирать прокси случайно для каждого запроса "
        "(при ошибке Telegram автоматически возьмёт другой)."
    )
