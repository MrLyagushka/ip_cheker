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
