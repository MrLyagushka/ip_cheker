"""Фича 2: компактное кодирование dead-IP в callback_data (<64 байт).

Требования из плана:
* roundtrip encode→decode сохраняет адреса и порядок;
* итоговый payload вместе с CLEANUP_CB_PREFIX СТРОГО меньше лимита
  Telegram (64 байта);
* дедупликация, отсев невалидных адресов, обрезка до DEAD_IPS_MAX;
* битый/пустой payload декодируется в [] без исключений.
"""
import base64

from config_core import (
    CLEANUP_CB_PREFIX, DEAD_IPS_MAX,
    decode_dead_payload, encode_dead_payload,
)

CALLBACK_DATA_LIMIT = 64


def test_roundtrip_single():
    payload = encode_dead_payload(["8.8.8.8"])
    assert payload
    assert decode_dead_payload(payload) == ["8.8.8.8"]


def test_roundtrip_preserves_order():
    ips = ["1.2.3.4", "255.0.0.1", "192.168.1.1", "0.0.0.0"]
    assert decode_dead_payload(encode_dead_payload(ips)) == ips


def test_payload_under_64_bytes_with_prefix():
    """Максимальный список + префикс гарантированно влезает в callback_data."""
    # детерминированный набор валидных уникальных IPv4 (10.x.y.7)
    ips = [f"10.{(i * 37) % 256}.{(i * 91 + 5) % 256}.7" for i in range(DEAD_IPS_MAX)]
    assert len(set(ips)) == DEAD_IPS_MAX, "набор должен быть уникальным"
    payload = encode_dead_payload(ips)
    full = CLEANUP_CB_PREFIX + payload
    assert len(full.encode("ascii")) < CALLBACK_DATA_LIMIT, \
        f"callback_data {len(full)} байт — превышен лимит Telegram"
    assert decode_dead_payload(payload) == ips


def test_dedupe_keeps_first_occurrence():
    ips = ["8.8.8.8", "1.1.1.1", "8.8.8.8"]
    assert decode_dead_payload(encode_dead_payload(ips)) == ["8.8.8.8", "1.1.1.1"]


def test_invalid_ips_skipped_not_crash():
    payload = encode_dead_payload(["999.1.1.1", "", "abc", "8.8.8.8"])
    assert decode_dead_payload(payload) == ["8.8.8.8"]


def test_truncates_to_max():
    ips = [f"172.{i}.0.1" for i in range(100)]
    encoded = decode_dead_payload(encode_dead_payload(ips))
    assert len(encoded) == DEAD_IPS_MAX
    assert encoded == ips[:DEAD_IPS_MAX]


def test_empty_inputs():
    assert encode_dead_payload([]) == ""
    assert encode_dead_payload(["не-ip"]) == ""
    assert decode_dead_payload("") == []


def test_decode_broken_payload_returns_empty():
    # не-base64 символы
    assert decode_dead_payload("!!!not-base64!!!") == []
    # длина не кратна 4 байтам после декода
    raw = base64.urlsafe_b64encode(b"\x01\x02\x03").decode().rstrip("=")
    assert decode_dead_payload(raw) == []          # 3 байта — битый payload


def test_all_254_hosts_roundtrip_chunk_safe():
    """Проверка граничных октетов: 0.0.0.0 и 255.255.255.255."""
    ips = ["0.0.0.0", "255.255.255.255", "1.2.3.4"]
    assert decode_dead_payload(encode_dead_payload(ips)) == ips
