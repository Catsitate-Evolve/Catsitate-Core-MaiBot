"""管理页数据服务测试:好感度/备忘读写、只读列表、校验与不变量。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from catsitate_core.admin import AdminService
from catsitate_core.config import FavorabilitySection, MemoSection
from catsitate_core.favorability import BatchEngine
from catsitate_core.memo import MemoService
from catsitate_core.storage import SQLiteStore

_ISO = "%Y-%m-%dT%H:%M:%S"


def _now(offset_hours: int = 0) -> str:
    return (datetime.now() + timedelta(hours=offset_hours)).strftime(_ISO)


@pytest.fixture()
def admin(tmp_path):
    store = SQLiteStore(tmp_path / "catsitate.db")
    BatchEngine(store, FavorabilitySection()).ensure_schema()
    MemoService(store, MemoSection()).ensure_schema()
    store.execute(
        """
        CREATE TABLE IF NOT EXISTS qzone_fav_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day TEXT NOT NULL DEFAULT '',
            user_id TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    return AdminService(store, MemoSection(), FavorabilitySection())


def _insert_fav(store, user_id, score, level, note=""):
    store.execute(
        "INSERT INTO favorability (user_id, level, score, note, window_start, judged_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, level, score, note, _now(), _now()),
    )


def test_favorability_list_search_and_pagination(admin):
    _insert_fav(admin.store, "10001", 15, 1, "老朋友")
    _insert_fav(admin.store, "10002", 65, 3, "常聊")
    _insert_fav(admin.store, "10003", 5, 0, "陌生人")
    result = admin.list_favorability(page=1, page_size=2)
    assert result["ok"] and result["total"] == 3 and len(result["items"]) == 2
    assert result["items"][0]["user_id"] == "10002"  # 分数降序
    hit = admin.list_favorability(keyword="老朋")
    assert [i["user_id"] for i in hit["items"]] == ["10001"]
    by_qq = admin.list_favorability(keyword="10003")
    assert [i["user_id"] for i in by_qq["items"]] == ["10003"]


def test_favorability_update_score_recomputes_level(admin):
    _insert_fav(admin.store, "10001", 5, 0)
    result = admin.update_favorability("10001", score=65)
    assert result["ok"] and result["level"] == 3 and result["level_name"] == "挚友"
    row = admin.store.query("SELECT score, level, note FROM favorability WHERE user_id='10001'")[0]
    assert row[0] == 65 and row[1] == 3


def test_favorability_update_note_limit_and_empty_clear(admin):
    _insert_fav(admin.store, "10001", 15, 1, "旧注记")
    too_long = admin.update_favorability("10001", note="x" * 41)
    assert too_long["ok"] is False and "上限" in too_long["error"]
    cleared = admin.update_favorability("10001", note="")
    assert cleared["ok"] and cleared["note"] == ""
    assert admin.store.query("SELECT note FROM favorability WHERE user_id='10001'")[0][0] == ""


def test_favorability_exclusive_level_guard(admin):
    _insert_fav(admin.store, "10001", 90, 3)
    _insert_fav(admin.store, "10002", 100, 4)
    blocked = admin.update_favorability("10001", score=100)
    assert blocked["ok"] is False and "独占" in blocked["error"]
    # 现有持有者自己可以维持 100
    assert admin.update_favorability("10002", score=100)["ok"] is True


def test_favorability_reset_clears_batch(admin):
    _insert_fav(admin.store, "10001", 65, 3, "常聊")
    admin.store.execute(
        "INSERT INTO batch_counter (user_id, stream_id, count, last_bump) VALUES (?, ?, ?, ?)",
        ("10001", "s1", 7, _now()),
    )
    result = admin.reset_favorability("10001")
    assert result["ok"] and result["cleared_batch_rows"] == 1
    row = admin.store.query(
        "SELECT score, level, note, window_start, judged_at FROM favorability WHERE user_id='10001'"
    )[0]
    assert row[0] == 0 and row[1] == 0 and row[2] == ""
    assert admin.store.query("SELECT COUNT(*) FROM batch_counter WHERE user_id='10001'")[0][0] == 0


def test_memo_create_list_update_delete(admin):
    created = admin.create_memo("10001", "周五交报告", ttl_hours=72, remind_at="2026-10-21T09:00")
    assert created["ok"] and created["id"] > 0
    listing = admin.list_memo(keyword="报告")
    assert listing["ok"] and listing["total"] == 1
    item = listing["items"][0]
    assert item["user_id"] == "10001" and item["expired"] is False

    updated = admin.update_memo(item["id"], content="周一交报告", ttl_hours=96, remind_at="")
    assert updated["ok"]
    row = admin.store.query("SELECT content, remind_at, expires_at FROM memo WHERE id=?", (item["id"],))[0]
    assert row[0] == "周一交报告" and row[1] == ""
    assert row[2] > _now(offset_hours=90)

    assert admin.delete_memo(item["id"])["ok"]
    assert admin.store.query("SELECT COUNT(*) FROM memo")[0][0] == 0


def test_memo_validation_errors(admin):
    assert admin.create_memo("", "x")["ok"] is False
    assert admin.create_memo("10001", " ")["ok"] is False
    assert admin.create_memo("10001", "x" * 81)["ok"] is False
    assert admin.create_memo("10001", "x", ttl_hours=9999)["ok"] is False
    assert admin.create_memo("10001", "x", remind_at="明天九点")["ok"] is False
    created = admin.create_memo("10001", "有效备忘")
    assert created["ok"]
    assert admin.update_memo(created["id"], content="")["ok"] is False
    assert admin.update_memo(created["id"], remind_at="2026-13-01T09:00")["ok"] is False
    assert admin.update_memo(999999, content="x")["ok"] is False


def test_memo_batch_delete(admin):
    first = admin.create_memo("10001", "a")["id"]
    second = admin.create_memo("10002", "b")["id"]
    result = admin.batch_delete_memos(f"{first},{second}")
    assert result["ok"] and result["deleted"] == 2
    assert admin.batch_delete_memos("")["ok"] is False
    assert admin.batch_delete_memos("1,abc")["ok"] is False


def test_readonly_lists_filter_and_paginate(admin):
    admin.store.execute(
        "INSERT INTO favorability_log (judge_id, user_id, delta, note, judged_at) VALUES (?,?,?,?,?)",
        ("j1", "10001", 2, "聊得不错", "2026-10-01T10:00:00"),
    )
    admin.store.execute(
        "INSERT INTO favorability_log (judge_id, user_id, delta, note, judged_at) VALUES (?,?,?,?,?)",
        ("j2", "10002", -1, "冷淡", "2026-10-05T10:00:00"),
    )
    logs = admin.list_favorability_log(user_id="10002")
    assert logs["ok"] and logs["total"] == 1 and logs["items"][0]["judge_id"] == "j2"
    ranged = admin.list_favorability_log(date_from="2026-10-04", date_to="2026-10-06")
    assert [i["judge_id"] for i in ranged["items"]] == ["j2"]
    assert admin.list_favorability_log(date_from="2026-10-06", date_to="2026-10-04")["ok"] is False

    admin.store.execute(
        "INSERT INTO qzone_fav_events (day, user_id, kind, text, created_at) VALUES (?,?,?,?,?)",
        ("2026-10-05", "10001", "COMMENT", "评论了你的说说", "2026-10-05T12:00:00"),
    )
    events = admin.list_qzone_events(user_id="10001")
    assert events["ok"] and events["total"] == 1
    assert events["items"][0]["kind"] == "COMMENT"
    missing = admin.list_qzone_events(user_id="99999")
    assert missing["ok"] and missing["total"] == 0


def test_page_args_reject_invalid_values(admin):
    assert admin.list_favorability(page=0)["ok"] is False
    assert admin.list_memo(page=1, page_size=51)["ok"] is False
    assert admin.list_favorability(page="x")["ok"] is False
    assert admin.list_qzone_events(page_size=0)["ok"] is False


def test_memo_negative_ttl_and_remind_after_expiry_rejected(admin):
    assert admin.create_memo("10001", "x", ttl_hours=-1)["ok"] is False
    assert admin.create_memo("10001", "x", ttl_hours=1, remind_at="2030-01-01T00:00")["ok"] is False
    created = admin.create_memo("10001", "有效备忘")
    assert created["ok"]
    assert admin.update_memo(created["id"], ttl_hours=-1)["ok"] is False
    assert admin.update_memo(created["id"], ttl_hours=1, remind_at="2030-01-01T00:00")["ok"] is False


def test_memo_extra_ids_truncation_message_surfaced(admin):
    extras = ",".join(str(900000000 + i) for i in range(8))
    result = admin.create_memo("10001", "带附带", extra_user_ids=extras)
    assert result["ok"] is True
    assert "仅保留前 5 个" in result["message"]


def test_admin_sql_touches_only_allowed_tables():
    """源码守卫:管理服务只能触碰四类用户数据表(+重置清 batch_counter)。"""
    import re
    from pathlib import Path

    source = Path("catsitate_core/admin.py").read_text(encoding="utf-8")
    tables = set(re.findall(r"(?:FROM|INTO|UPDATE|DELETE\s+FROM)\s+([a-z_]+)", source))
    assert tables <= {"favorability", "favorability_log", "memo", "batch_counter", "qzone_fav_events"}, tables


def test_admin_call_wraps_service_construction_errors():
    from catsitate_core.admin_api import AdminAPIMixin

    class _Boom(AdminAPIMixin):
        def _admin_service(self):
            raise RuntimeError("boom")

    result = object.__new__(_Boom)._admin_call("list_favorability")
    assert result["ok"] is False and "内部错误" in result["error"]
