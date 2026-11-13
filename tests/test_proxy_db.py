"""Тесты SQLite-хранилища прокси (proxy_db) и интеграции с пулом (proxy_pool).

Требование: «прокси сохраняются в БД, а не в .env» — проверяем:
* CRUD ProxyDB (add/remove/set_active/clear/list) поверх tmp-файла;
* миграцию PROXY_URL из .env и proxies.json в таблицу + зачистку старых мест;
* сохранение через pool.add()/remove() попадает именно в БД (temp-BD),
  а .env остаётся нетронутым.
"""

import json

import pytest

import proxy_db


# ───────────────────────────── helpers ──────────────────────────────────────


@pytest.fixture()
def pdb(tmp_path):
    db = proxy_db.ProxyDB(str(tmp_path / "proxies.db"))
    yield db
    db.close()


U1 = "socks5://u1:p1@10.0.0.1:1080"
U2 = "socks5h://u2:p2@10.0.0.2:1080"
U3 = "http://10.0.0.3:3128"


# ───────────────────────────── normalize/mask ───────────────────────────────


def test_normalize_url_ok():
    assert proxy_db.normalize_url(f"  {U1} ") == U1
    assert proxy_db.normalize_url('"socks4://h:1080"') == "socks4://h:1080"


def test_normalize_url_rejects_junk():
    for bad in ("", None, "   ", "ftp://host/x", "not a url",
                "socks5://host:notaport", "://x"):
        assert proxy_db.normalize_url(bad) is None, bad


def test_mask_url_hides_password():
    m = proxy_db.mask_url(U1)
    assert "p1" not in m and "u1" in m and "10.0.0.1:1080" in m


# ───────────────────────────── CRUD ─────────────────────────────────────────


def test_add_list_remove(pdb):
    assert pdb.add(U1) is True
    assert pdb.add(U1) is False          # дубликат (UNIQUE NOCASE)
    assert pdb.add("мусор") is False     # не прокси-URL
    assert pdb.add(U2) is True
    assert pdb.list_urls() == [U1, U2]
    assert pdb.count() == 2
    assert pdb.remove(U1) is True
    assert pdb.remove(U1) is False
    assert pdb.list_urls() == [U2]


def test_first_added_becomes_active(pdb):
    assert pdb.get_active() is None
    pdb.add(U1)
    assert pdb.get_active() == U1        # первый — активный (эвристика)
    pdb.add(U2)
    assert pdb.get_active() == U1        # второй уже не перехватывает active


def test_set_active_single_invariant(pdb):
    pdb.add(U1)
    pdb.add(U2)
    assert pdb.set_active(U2) is True
    assert pdb.get_active() == U2
    assert pdb.set_active(None) is True
    assert pdb.get_active() is None
    # set_active умеет добавить отсутствующий URL
    assert pdb.set_active(U3) is True
    assert pdb.list_urls() == [U1, U2, U3]
    assert pdb.get_active() == U3


def test_clear(pdb):
    pdb.add(U1)
    pdb.add(U2)
    assert pdb.clear() == 2
    assert pdb.list_urls() == []
    assert pdb.get_active() is None


def test_save_proxies_replaces_and_keeps_active(pdb, monkeypatch):
    pdb.add(U1)
    monkeypatch.setattr(proxy_db, "db", pdb)
    assert proxy_db.save_proxies([U1, U2]) is True
    assert pdb.list_urls() == [U1, U2]
    assert pdb.get_active() == U1        # прежний активный сохранён
    assert proxy_db.save_proxies([U2]) is True
    assert pdb.list_urls() == [U2]
    assert pdb.get_active() == U2        # old active выпал → первый новый


def test_load_proxies_uses_module_db(pdb, monkeypatch):
    pdb.add(U1)
    monkeypatch.setattr(proxy_db, "db", pdb)
    assert proxy_db.load_proxies() == [U1]


# ───────────────────────────── миграция ─────────────────────────────────────


def test_migrate_legacy_from_env_and_json(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        'TOKEN = "1:AA"\n'
        f'PROXY_URL = "{U1}"  # старый секрет в env\n'
        "MAX_CONCURRENT_PINGS = 3\n",
        encoding="utf-8",
    )
    legacy_json = tmp_path / "proxies.json"
    legacy_json.write_text(json.dumps([U1, U2]), encoding="utf-8")

    fresh = proxy_db.ProxyDB(str(tmp_path / "proxies.db"))
    monkeypatch.setattr(proxy_db, "db", fresh)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    urls = proxy_db.migrate_legacy(env_file=str(env), json_path=str(legacy_json))

    # перенесены оба уникальных прокси, порядок — env первым
    assert urls == [U1, U2]
    assert fresh.list_urls() == [U1, U2]
    assert fresh.get_active() == U1
    # PROXY_URL удалён из .env, TOKEN и остальные ключы целы
    text = env.read_text(encoding="utf-8")
    assert "PROXY_URL" not in text
    assert 'TOKEN = "1:AA"' in text and "MAX_CONCURRENT_PINGS" in text
    # временный JSON удалён
    assert not legacy_json.exists()
    fresh.close()


def test_migrate_legacy_idempotent(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(f'PROXY_URL="{U1}"\n', encoding="utf-8")
    fresh = proxy_db.ProxyDB(str(tmp_path / "proxies.db"))
    monkeypatch.setattr(proxy_db, "db", fresh)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    first = proxy_db.migrate_legacy(env_file=str(env),
                                    json_path=str(tmp_path / "none.json"))
    second = proxy_db.migrate_legacy(env_file=str(env),
                                     json_path=str(tmp_path / "none.json"))
    assert first == second == [U1]       # без дублей и ошибок
    fresh.close()


def test_migrate_legacy_empty_env_noop(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('TOKEN = "1:AA"\nPROXY_URL=\n', encoding="utf-8")
    fresh = proxy_db.ProxyDB(str(tmp_path / "proxies.db"))
    monkeypatch.setattr(proxy_db, "db", fresh)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert proxy_db.migrate_legacy(env_file=str(env),
                                   json_path=str(tmp_path / "x.json")) == []
    assert 'PROXY_URL=' not in env.read_text()   # пустой ключ тоже вычищается
    fresh.close()


# ─────────────────────── пул пишет в БД, а не в .env ────────────────────────


@pytest.mark.asyncio
async def test_pool_add_remove_persist_to_db(pdb, monkeypatch, tmp_path):
    import proxy_pool

    env = tmp_path / ".env"
    env.write_text('TOKEN = "1:AA"\n', encoding="utf-8")
    monkeypatch.setattr(proxy_db, "db", pdb)
    # глобальный пул в этом тесте не трогаем — свой экземпляр поверх temp-BD
    pr = proxy_pool.ProxyPool(factory=lambda url: None, storage=pdb)
    monkeypatch.setattr(proxy_pool, "save_proxies", lambda urls, path=None:
                        proxy_db.save_proxies(urls))
    pr.configure([])
    assert await pr.add(U1) is True
    assert await pr.add(U1) is False                 # дубликат
    assert await pr.add("bad value") is False        # не URL
    assert pdb.list_urls() == [U1]                   # записано В БД
    assert env.read_text() == 'TOKEN = "1:AA"\n'     # .env не тронут

    assert await pr.remove(U1) is True
    assert await pr.remove(U1) is False
    assert pdb.list_urls() == []
    assert pr.urls == []


@pytest.mark.asyncio
async def test_pool_replace_all_writes_db(pdb, monkeypatch):
    import proxy_pool

    monkeypatch.setattr(proxy_db, "db", pdb)
    pr = proxy_pool.ProxyPool(factory=lambda url: None, storage=pdb)
    pr.configure([U1])
    await pr.replace_all([U2, U3])
    assert pdb.list_urls() == [U2, U3]
    assert pr.urls == [U2, U3]
