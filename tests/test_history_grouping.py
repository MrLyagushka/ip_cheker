"""Юнит-тесты группировки истории и пакетирования /tree (Б9, Б3, Б23, Б8)."""
import json

import config_core as cc


def test_history_plan_host_vs_subnet():
    _, _, plan = cc.classify_targets(["8.8.8.8", "1.2.3.0"])
    assert {p["target"]: p["type"] for p in plan} == {"8.8.8.8": "host", "1.2.3.0": "subnet"}


def test_forbidden_subnet_not_in_history():
    targets, invalid, plan = cc.classify_targets(["66.86.0.0"])
    assert invalid == ["66.86.0.0"] and targets == [] and plan == []


def test_save_to_history_dedupe_and_cap(tmp_path, monkeypatch):
    hist = tmp_path / "networks_history.json"
    monkeypatch.setattr(cc, "HISTORY_FILE", str(hist))

    def e(t, ty="host", a=1):
        return {"type": ty, "target": t, "alive": a, "total": 1,
                "percent": a * 100.0, "time": "2026-11-12 00:00:00", "scanned_by": 1}

    cc.save_to_history([e("8.8.8.8"), e("1.1.1.1")])
    cc.save_to_history([e("8.8.8.8", a=0)])  # dedupe: replaces previous slice
    stored = json.loads(hist.read_text())
    assert [x["target"] for x in stored] == ["1.1.1.1", "8.8.8.8"]
    assert stored[-1]["alive"] == 0

    # cap HISTORY_MAX_ENTRIES (с отключённой миграцией legacy из корня репо)
    monkeypatch.setattr(cc, "init_files", lambda: None)
    many = [e(f"10.0.{i // 256}.{i % 256}") for i in range(cc.HISTORY_MAX_ENTRIES + 50)]
    cc.save_to_history(many)
    stored = json.loads(hist.read_text())
    assert len(stored) == cc.HISTORY_MAX_ENTRIES
    assert stored == many[-cc.HISTORY_MAX_ENTRIES:]


def test_legacy_entries_migrate_to_subnet_type(tmp_path, monkeypatch):
    hist = tmp_path / "networks_history.json"
    hist.write_text(json.dumps([{"target": "1.2.3.0", "alive": 2, "total": 3,
                                 "percent": 66.7, "time": "t"}]))
    monkeypatch.setattr(cc, "HISTORY_FILE", str(hist))
    cc.save_to_history([{"type": "host", "target": "9.9.9.9", "alive": 1,
                         "total": 1, "percent": 100.0, "time": "t2", "scanned_by": 5}])
    stored = json.loads(hist.read_text())
    legacy = next(x for x in stored if x["target"] == "1.2.3.0")
    assert legacy["type"] == "subnet"


# ── /tree: epsilon-пороги и пакеты <=3500 ───────────────────────────────────

def test_status_icon_epsilon_thresholds():
    assert cc._status_icon(100.0) == "🟢"
    assert cc._status_icon(99.99999999999999) == "🟢"   # float-артефакт полного обхода
    assert cc._status_icon(99.9) == "🔵"
    assert cc._status_icon(0.0000000000001) == "🔴"     # float-артефакт нуля
    assert cc._status_icon(0.0) == "🔴"
    assert cc._status_icon(0.1) == "🔵"


def _entry(i):
    return {"type": "subnet", "target": f"10.{i // 256}.{i % 256}.0", "alive": 1,
            "total": 256, "percent": 0.39, "time": "2026-11-12 00:00:00"}


def test_tree_blocks_under_limit():
    history = [_entry(i) for i in range(500)]
    blocks = cc.build_tree_blocks(history)
    assert len(blocks) > 1                       # large output MUST split (Б23)
    assert all(len(b) <= cc.MAX_BLOCK_LEN for b in blocks)
    joined = "\n".join(blocks)
    for h in history:
        assert h["target"] in joined             # nothing lost between blocks


def test_tree_small_history_single_block():
    blocks = cc.build_tree_blocks([_entry(1)])
    assert len(blocks) == 1
    assert "Легенда статусов" in blocks[0]


def test_tree_html_escaping():
    evil = {"type": "host", "target": "<script>x</script>", "alive": 1,
            "total": 1, "percent": 100.0, "time": "<b>t</b>"}
    out = "\n".join(cc.build_tree_blocks([evil]))
    assert "&lt;script&gt;" in out and "<script>" not in out


def test_format_alert_is_list_safe_regression(cfg):
    """Б1: format_alert не должен быть строкой с .append() — регресс на живом импорте."""
    text = cfg.format_alert(1424727277, ["8.8.8.8", "1.1.1.1"])
    assert "tg://user?id=1424727277" in text
    assert text.count("🔴") == 2
