"""Общая фикстура: изолированные данные/окружение до импорта config (Этап 8).

Модуль config создаёт Bot()/Dispatcher() на импорте, поэтому:
1) env-переменные подставляются ДО первого `import config`;
2) валидный формат тестового токена ("digits:secret");
3) для юнит-тестов чистые функции вынесены в config_core (без Telegram-объектов).
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEST_TOKEN = "123456:AAHtesttokenfortests"


@pytest.fixture(scope="session", autouse=True)
def isolated_env(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    env = data / ".env"
    env.write_text(
        f'TOKEN = "{TEST_TOKEN}"\n'
        'PROXY_URL = ""\n'
        'MAX_CONCURRENT_PINGS = 3\n'
        'ADMIN_ID = 1424727277\n'
        'ALERT_CHAT_ID = "-1001234567890"  # inline comment must be stripped\n'
        'ALERT_THREAD_ID = 42\n',
        encoding="utf-8",
    )
    os.environ["DATA_DIR"] = str(data)
    os.environ["ENV_FILE"] = str(env)
    for var in ("TOKEN", "PROXY_URL", "MAX_CONCURRENT_PINGS", "ADMIN_ID",
                "ALERT_CHAT_ID", "ALERT_THREAD_ID"):
        os.environ.pop(var, None)
    # Боевой .env из каталога проекта не должен попадать в окружение тестов:
    # aiogram-зависимые модули (config, handlers.*) могут быть импортированы
    # раньше этой фикстуры (например, плагинами pytest) и вызвать load_env().
    for var in ("TOKEN", "PROXY_URL", "MAX_CONCURRENT_PINGS", "ADMIN_ID",
                "ALERT_CHAT_ID", "ALERT_THREAD_ID"):
        os.environ.pop(var, None)
    # config.py при импорте вызывает load_env(); если в рабочем каталоге
    # лежит настоящий .env (с боевым TOKEN/ALERT_CHAT_ID), он перетирает
    # тестовые переменные окружения и ломает изоляцию (Б20-тест видел
    # реальный ALERT_CHAT_ID). Временная заглушка ENV_FILE на время импорта
    # гарантирует, что читаётся только тестовый .env из tmp_path.
    real_env = os.environ.get("ENV_FILE")
    # ВАЖНО: config_core читает ENV_FILE один раз при импорте в модульную
    # константу, поэтому патчим её тоже — иначе load_env() внутри config.py
    # прочитает настоящий боевой .env из каталога проекта (реальный
    # ALERT_CHAT_ID/TOKEN) и изоляция тестов сломается.
    import config_core as _cc
    _cc.ENV_FILE = str(env)
    os.environ["ENV_FILE"] = str(env)
    try:
        import config  # noqa: F401 — прогрев с тестовым .env
    finally:
        if real_env is not None:
            os.environ["ENV_FILE"] = real_env
    # load_env() внутри config.py перечитал тестовый .env в os.environ —
    # убираем его обратно, чтобы боевые/тестовые значения не смешивались
    # при повторных импортах в других сессиях.
    for var in ("TOKEN", "PROXY_URL", "MAX_CONCURRENT_PINGS", "ADMIN_ID",
                "ALERT_CHAT_ID", "ALERT_THREAD_ID"):
        os.environ.pop(var, None)
    yield {"data": data, "env": env}


@pytest.fixture()
def cfg():
    """Фикстура боевого модуля config (с Bot/Dispatcher — только для smoke)."""
    import config
    return config


@pytest.fixture()
def core():
    """Чистое ядро без Telegram-объектов."""
    import config_core
    return config_core


@pytest.fixture()
def test_env(tmp_path, monkeypatch):
    """Изолированные data-файлы для тестов, трогающих ФС (пишут в tmp)."""
    import config_core as cc
    for name in ("HISTORY_FILE", "TRACKED_IPS_FILE", "USERS_FILE"):
        monkeypatch.setattr(cc, name, str(tmp_path / f"{name}.json"))
    monkeypatch.setattr(cc, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cc, "RESULTS_DIR", tmp_path / "results")
    return tmp_path
