"""Юнит-тесты парсера .env и save_env_value (Б20, Этап 7)."""
import os

import config_core as cc


def test_parse_plain_and_spaces():
    assert cc.parse_env_line('TOKEN=abc') == ("TOKEN", "abc")
    assert cc.parse_env_line('PROXY_URL = "socks5://u:p@h:1080"') == \
        ("PROXY_URL", "socks5://u:p@h:1080")


def test_parse_inline_comment_outside_quotes():
    k, v = cc.parse_env_line('ALERT_CHAT_ID = -100123 # группа')
    assert (k, v) == ("ALERT_CHAT_ID", "-100123")


def test_hash_inside_quotes_preserved():
    k, v = cc.parse_env_line('TOKEN = "123:AAH#secret"')
    assert (k, v) == ("TOKEN", "123:AAH#secret")


def test_parse_skips_comments_and_blanks():
    assert cc.parse_env_line("# comment") is None
    assert cc.parse_env_line("") is None
    assert cc.parse_env_line("no equals sign") is None


def test_iter_env_pairs_file(tmp_path):
    f = tmp_path / ".env"
    f.write_text('# header\nA=1\nB = "two"  # trailing\n\nC=\n', encoding="utf-8")
    pairs = dict(cc.iter_env_pairs(str(f)))
    assert pairs == {"A": "1", "B": "two", "C": ""}


def test_normalize_int_variants():
    assert cc.normalize_int(" -100123 ", "X") == -100123
    assert cc.normalize_int(None, "X") is None
    assert cc.normalize_int("", "X") is None
    assert cc.normalize_int("not-a-number", "X") is None   # мусор -> None + warning


def test_save_env_value_replaces_key_with_spaces(tmp_path, monkeypatch):
    """Регресс: «PROXY_URL = "..."» должна ЗАМЕНЯТЬСЯ, а не дублироваться."""
    env = tmp_path / ".env"
    env.write_text('TOKEN = "123:AAHx"\nPROXY_URL = "socks5://old:1080"\n', encoding="utf-8")
    monkeypatch.setattr(cc, "ENV_FILE", str(env))
    cc.save_env_value("PROXY_URL", "http://new:8080")
    lines = [l for l in env.read_text().splitlines() if l.startswith("PROXY_URL")]
    assert lines == ["PROXY_URL=http://new:8080"]           # ровно одна строка
    assert 'TOKEN = "123:AAHx"' in env.read_text()          # остальные не тронуты
    import os
    assert os.environ["PROXY_URL"] == "http://new:8080"
