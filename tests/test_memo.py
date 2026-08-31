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


# ---------- §3.10 memo 按人重构:主 QQ + 附带 QQ 人维度,流维度保留为元数据 ----------


def test_write_with_extra_user_ids_and_cross_stream_visibility(tmp_path):
    """§3.10:条目=主QQ+附带QQ 列表;任一牵连 QQ 命中当前对话对象即可见(跨流);流维度保留;无关人不可见。"""

    svc, _ = make_service(tmp_path)
    ok, msg = svc.write("A 和 B 一起出去玩", stream_id="s_qq_group", user_id="10001", ttl_hours=None, extra_user_ids=["10002"], now=lambda: NOW)
    assert ok, msg
    # 主 QQ 在别的流可见(跨流);附带 QQ 同样可见(跨流)
    assert any("出去玩" in e["content"] for e in svc.read("s_private_10001", "10001", 10, now=lambda: NOW))
    assert any("出去玩" in e["content"] for e in svc.read("s_private_10002", "10002", 10, now=lambda: NOW))
    # 流维度保留(元数据,流内仍可见)
    assert any("出去玩" in e["content"] for e in svc.read("s_qq_group", "", 10, now=lambda: NOW))
    # 无关人不可见
    assert all("出去玩" not in e["content"] for e in svc.read("s_x", "99999", 10, now=lambda: NOW))


def test_extra_user_ids_dedup_and_self_removal(tmp_path):
    """§3.10:附带 QQ 去重且剔除主 QQ 自身(同一条目不重复牵连)。"""

    svc, _ = make_service(tmp_path)
    ok, msg = svc.write("约饭", stream_id="s", user_id="10001", ttl_hours=None, extra_user_ids=["10001", "10002", "10002"], now=lambda: NOW)
    assert ok, msg
    rows = svc.store.query("SELECT extra_user_ids FROM memo")
    import json

    assert json.loads(rows[0][0]) == ["10002"]


def test_extra_user_ids_limit_and_invalid(tmp_path):
    """§3.10:附带 QQ 上限 5 个,超出截断并在返回消息中提示。"""

    svc, _ = make_service(tmp_path)
    ok, msg = svc.write("x", stream_id="s", user_id="1", ttl_hours=None, extra_user_ids=["2", "3", "4", "5", "6", "7"], now=lambda: NOW)
    assert ok
    assert "附带" in msg and "5" in msg  # 超出 5 个截断并提示
    import json

    stored = json.loads(svc.store.query("SELECT extra_user_ids FROM memo")[0][0])
    assert stored == ["2", "3", "4", "5", "6"]  # 截断为前 5 个


def test_migration_adds_extra_user_ids_column(tmp_path):
    """§3.10:旧库 memo 表缺 extra_user_ids 列时 ensure_schema 自动补列,不抛异常。"""

    store = SQLiteStore(tmp_path / "m.db")
    store.execute(
        "CREATE TABLE memo (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT, stream_id TEXT, "
        "user_id TEXT, expires_at TEXT, created_at TEXT, remind_at TEXT)"
    )
    svc = MemoService(store, MemoSection())
    svc.ensure_schema()  # 不抛,补列
    cols = {r[1] for r in store.query("PRAGMA table_info(memo)")}
    assert "extra_user_ids" in cols

