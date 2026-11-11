"""Юнит-тесты строгой валидации целей (Б4, Б3) — Этап 8."""
import pytest

from config import is_valid_ipv4, expand_targets, classify_targets

VALID = ["8.8.8.8", "0.0.0.0", "255.255.255.255", "192.168.1.1", " 10.0.0.1 "]
INVALID = ["999.999.999.5", "256.1.1.1", "1.2.3", "01.2.3.4", "1.2.3.4.5",
           "abc", "", "8.8.8.", "1.2.3.-4"]


def test_valid_ipv4():
    for ip in VALID:
        assert is_valid_ipv4(ip), ip


def test_invalid_ipv4():
    for bad in INVALID:
        assert not is_valid_ipv4(bad), bad


def test_expand_single_and_dedupe():
    targets, invalid = expand_targets(["8.8.8.8", "1.1.1.1", "8.8.8.8"])
    assert targets == ["8.8.8.8", "1.1.1.1"]
    assert invalid == []


def test_expand_subnet_256():
    targets, invalid = expand_targets(["66.86.15.0"])
    assert len(targets) == 256
    assert targets[0] == "66.86.15.0" and targets[-1] == "66.86.15.255"
    assert invalid == []


def test_forbidden_x_x_0_0_blocked_from_targets_and_history():
    """Б3: x.x.0.0 не сканируется и НЕ попадает ни в какой выход."""
    targets, invalid, plan = classify_targets(["66.86.0.0", "8.8.8.8"])
    assert "66.86.0.0" in invalid
    assert all(not t.startswith("66.86.0.") for t in targets)
    assert [p["target"] for p in plan] == ["8.8.8.8"]


def test_invalid_octet_goes_to_invalid_list():
    targets, invalid = expand_targets(["999.1.1.1", "1.1.1.1"])
    assert targets == ["1.1.1.1"]
    assert invalid == ["999.1.1.1"]


def test_classify_types_host_vs_subnet():
    _, _, plan = classify_targets(["8.8.8.8", "1.2.3.0"])
    kinds = {p["target"]: p["type"] for p in plan}
    assert kinds == {"8.8.8.8": "host", "1.2.3.0": "subnet"}


def test_normalize_int_alert_chat_id(cfg):
    """Б20: ALERT_* нормализованы в int несмотря на кавычки/комментарий в .env."""
    assert cfg.ALERT_CHAT_ID == -1001234567890
    assert isinstance(cfg.ALERT_CHAT_ID, int)
    assert cfg.ALERT_THREAD_ID == 42
