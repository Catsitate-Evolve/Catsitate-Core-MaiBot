"""备忘录测试:TTL 参数、长度强制、读取与清理。"""

from datetime import datetime, timedelta

from catsitate_core.config import MemoSection
from catsitate_core.memo import MemoService
from catsitate_core.storage import SQLiteStore

NOW = datetime(2026, 8, 14, 12, 0, 0)


def make_service(tmp_path):
    store = SQLiteStore(tmp_path / "memo.db")
    svc = MemoService(store, MemoSection())
    svc.ensure_schema()
    return svc, store


def test_write_and_read(tmp_path):
    svc, _ = make_service(tmp_path)
    ok, msg = svc.write("周四要交作业", stream_id="s1", user_id="u1", ttl_hours=None, now=lambda: NOW)
    assert ok, msg
    rows = svc.read("s1", "u1", 5, now=lambda: NOW)
    assert len(rows) == 1
    assert rows[0]["content"] == "周四要交作业"
    assert 23 < rows[0]["remaining_hours"] <= 24


def test_write_too_long_rejected(tmp_path):
    svc, _ = make_service(tmp_path)
    ok, msg = svc.write("长" * 81, stream_id="s", user_id="u", ttl_hours=None, now=lambda: NOW)
    assert not ok
    assert "80" in msg
    assert svc.read("s", "u", 5, now=lambda: NOW) == []


def test_write_ttl_over_max_rejected(tmp_path):
    svc, _ = make_service(tmp_path)
    ok, msg = svc.write("内容", stream_id="s", user_id="u", ttl_hours=999, now=lambda: NOW)
    assert not ok
    assert "168" in msg


def test_read_empty_both_dims_returns_empty(tmp_path):
    svc, _ = make_service(tmp_path)
    svc.write("无归属", stream_id="", user_id="", ttl_hours=None, now=lambda: NOW)
    svc.write("A", stream_id="s1", user_id="u1", ttl_hours=None, now=lambda: NOW)
    assert svc.read("", "", 5, now=lambda: NOW) == []
    # 单维度非空时,另一维度为空不匹配空值行(审查 M1)
    assert {r["content"] for r in svc.read("s1", "", 5, now=lambda: NOW)} == {"A"}


def test_read_by_user_across_streams(tmp_path):
    svc, _ = make_service(tmp_path)
    svc.write("A", stream_id="s1", user_id="u1", ttl_hours=None, now=lambda: NOW)
    svc.write("B", stream_id="s2", user_id="u1", ttl_hours=None, now=lambda: NOW)
    rows = svc.read("s3", "u1", 5, now=lambda: NOW)
    assert {r["content"] for r in rows} == {"A", "B"}


def test_cleanup_removes_expired(tmp_path):
    svc, _ = make_service(tmp_path)
    svc.write("过期", stream_id="s", user_id="u", ttl_hours=1, now=lambda: NOW)
    removed = svc.cleanup(now=lambda: NOW + timedelta(hours=2))
    assert removed == 1
    assert svc.read("s", "u", 5, now=lambda: NOW + timedelta(hours=2)) == []


def test_memo_remind_at_write_and_due(tmp_path):
    svc, _ = make_service(tmp_path)
    svc.write("周四交作业", stream_id="s1", user_id="u1", remind_at="2026-08-16T19:00:00", ttl_hours=None, now=lambda: NOW)
    due = svc.due_on("2026-08-16", now=lambda: NOW)
    assert [e["content"] for e in due] == ["周四交作业"]
    assert svc.due_on("2026-08-17", now=lambda: NOW) == []


def test_memo_remind_at_optional_default_empty(tmp_path):
    svc, _ = make_service(tmp_path)
    svc.write("普通备忘", stream_id="s1", user_id="u1", ttl_hours=None, now=lambda: NOW)
    assert svc.due_on("2099-01-01", now=lambda: NOW) == []


def test_memo_remind_at_invalid_format_rejected(tmp_path):
    """M-10:remind_at 格式非法拒绝写入(显式中文错误),合法格式(可带秒)正常写入。"""
    svc, _ = make_service(tmp_path)
    ok, msg = svc.write("内容", stream_id="s1", user_id="u1", ttl_hours=None, remind_at="明天19点", now=lambda: NOW)
    assert not ok and "提醒时间格式非法" in msg
    ok, msg = svc.write("内容", stream_id="s1", user_id="u1", ttl_hours=None, remind_at="2026-08-16 19:00", now=lambda: NOW)
    assert not ok and "提醒时间格式非法" in msg  # 空格分隔同样非法
    assert svc.due_on("2099-01-01", now=lambda: NOW) == []  # 非法项未落库
    ok, msg = svc.write("内容", stream_id="s1", user_id="u1", ttl_hours=None, remind_at="2026-08-16T19:00:00", now=lambda: NOW)
    assert ok, msg
    assert [e["content"] for e in svc.due_on("2026-08-16", now=lambda: NOW)] == ["内容"]
