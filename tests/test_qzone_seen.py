"""qzone seen 去重存储测试:两层状态/窗口回退/近期摘要。"""
import sqlite3
from datetime import datetime, timedelta

from catsitate_core.qzone.seen_store import SeenStore
from catsitate_core.storage import SQLiteStore


def _store(tmp_path):
    return SQLiteStore(tmp_path / "t.db")


def test_mark_queued_dedup(tmp_path):
    s = SeenStore(_store(tmp_path))
    s.ensure_schema()
    assert s.mark_queued("t1", abstime="1750000000", author_uin="10001", summary="今天天气好") is True
    assert s.mark_queued("t1", abstime="1750000000", author_uin="10001", summary="今天天气好") is False
    assert s.mark_queued("t2", abstime="1750000001", author_uin="10002", summary="") is True


def test_is_new_candidate_pure_query_no_registration(tmp_path):
    """M3 发现层:is_new_candidate 纯查存在性不登记——发现≠注入,提前标 queued
    会让充实层 mark_queued 判重跳过(发现层阶段不得有副作用)。"""
    s = SeenStore(_store(tmp_path))
    s.ensure_schema()
    assert s.is_new_candidate("t1") is True
    assert s.is_new_candidate("t1") is True  # 重复查询仍新:查询本身不写行
    # 查过的 tid 仍可正常登记(证明 is_new_candidate 未预占主键)
    assert s.mark_queued("t1", abstime="1750000000", author_uin="10001", summary="a") is True
    assert s.is_new_candidate("t1") is False  # 已登记(任意状态)→ 不是新候选
    s.mark_seen("t1", "2026-08-31T10:00:00")
    assert s.is_new_candidate("t1") is False  # seen 状态同样判非新
    assert s.is_new_candidate("") is True  # 空 tid 不存在行(与 mark_queued 空 tid 拒登记对称)


def test_seen_only_after_injection_and_revert(tmp_path):
    s = SeenStore(_store(tmp_path))
    s.ensure_schema()
    s.mark_queued("t1", abstime="", author_uin="u", summary="a")
    s.mark_queued("t2", abstime="", author_uin="u", summary="b")
    # 窗口结束未注入 → 回退未读(行删除),下窗口 mark_queued 返回 True
    assert s.revert_pending() == 2
    assert s.mark_queued("t1", abstime="", author_uin="u", summary="a") is True
    # 注入成功 → seen;此后 mark_queued 为 False,不回退
    s.mark_seen("t1", "2026-08-30T20:00:00")
    assert s.revert_pending() == 0
    assert s.mark_queued("t1", abstime="", author_uin="u", summary="a") is False


def test_recent_seen_filters_by_days_and_limit(tmp_path):
    s = SeenStore(_store(tmp_path))
    s.ensure_schema()
    now = datetime(2026, 8, 30, 12, 0, 0)
    s.mark_queued("a", abstime="", author_uin="u1", summary="旧动态")
    s.mark_seen("a", (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S"))
    s.mark_queued("b", abstime="", author_uin="u2", summary="新动态1")
    s.mark_seen("b", now.strftime("%Y-%m-%dT%H:%M:%S"))
    s.mark_queued("c", abstime="", author_uin="u3", summary="新动态2")
    s.mark_seen("c", now.strftime("%Y-%m-%dT%H:%M:%S"))
    rows = s.recent_seen(limit=1, days=3, now=now)
    assert [r["tid"] for r in rows] == ["b"] or [r["tid"] for r in rows] == ["c"]  # limit=1
    rows = s.recent_seen(limit=5, days=3, now=now)
    assert {r["tid"] for r in rows} == {"b", "c"} and "a" not in {r["tid"] for r in rows}


def test_mark_interacted_flag(tmp_path):
    s = SeenStore(_store(tmp_path))
    s.ensure_schema()
    s.mark_queued("t", abstime="", author_uin="u", summary="")
    s.mark_seen("t", "2026-08-30T20:00:00")
    s.mark_interacted("t")  # 不抛错即可(M2 消费)


def test_mark_seen_message_id_and_get_message_id(tmp_path):
    """通知 reply 段关联(联调修正):mark_seen 第三参记录注入消息 id,
    get_message_id 按 tid 查回——通知注入时据此构造 reply 段引用原说说。
    旧签名(不传 message_id)兼容为空串;未知 tid 回退空串;旧库 PRAGMA 迁移补列。"""

    s = SeenStore(_store(tmp_path))
    s.ensure_schema()
    s.mark_queued("t1", abstime="", author_uin="u", summary="a")
    s.mark_seen("t1", "2026-09-01T10:00:00")  # 不传=保留旧值;新登记行旧值即空串
    assert s.get_message_id("t1") == ""
    s.mark_queued("t2", abstime="", author_uin="u", summary="b")
    s.mark_seen("t2", "2026-09-01T10:00:01", "qzone_t2_7")
    assert s.get_message_id("t2") == "qzone_t2_7"
    assert s.get_message_id("unknown") == ""  # 未登记 tid 回退空串

    # 旧库迁移:预建缺 message_id 列的表(如实复刻 M-2 原始列集)→ ensure_schema 补列
    store2 = SQLiteStore(tmp_path / "old.db")
    store2.execute(
        "CREATE TABLE qzone_feeds (tid TEXT PRIMARY KEY, abstime TEXT NOT NULL DEFAULT '', "
        "author_uin TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', "
        "state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'seen')), "
        "interacted INTEGER NOT NULL DEFAULT 0, injected_at TEXT NOT NULL DEFAULT '', "
        "created_at TEXT NOT NULL DEFAULT '')"
    )
    s2 = SeenStore(store2)
    s2.ensure_schema()  # ALTER 补列不抛
    s2.mark_queued("x", abstime="", author_uin="u", summary="")
    s2.mark_seen("x", "2026-09-01T12:00:00", "qzone_x_1")
    assert s2.get_message_id("x") == "qzone_x_1"


def test_mark_seen_none_preserves_message_id_empty_string_clears(tmp_path):
    """2026-09-03 复审修复(Q10=B):mark_seen 第三参 None = 只置 seen 不动 reply 段锚
    (注入落的注入消息 id 不被 detail 查看静默抹掉);空串语义保留(显式清除);
    缺省(不传)同 None——保留旧值。"""

    s = SeenStore(_store(tmp_path))
    s.ensure_schema()
    s.mark_queued("t1", abstime="", author_uin="u", summary="a")
    s.mark_seen("t1", "2026-09-01T10:00:00", "qzone_t1_1")  # 注入路径落真实 id
    # detail 查看路径:传 None → 只置 seen,注入锚保留
    s.mark_seen("t1", "2026-09-02T11:00:00", None)
    assert s.get_message_id("t1") == "qzone_t1_1"
    # 缺省(不传第三参)同 None:保留旧值
    s.mark_seen("t1", "2026-09-03T12:00:00")
    assert s.get_message_id("t1") == "qzone_t1_1"
    # 空串语义保留:显式清除覆写
    s.mark_seen("t1", "2026-09-04T13:00:00", "")
    assert s.get_message_id("t1") == ""


def test_author_nickname_column_and_summary(tmp_path):
    """M-2:author_nickname 列(新库直接建列,旧库 PRAGMA 迁移补列)。"""

    s = SeenStore(_store(tmp_path))
    s.ensure_schema()
    s.mark_queued("t1", abstime="", author_uin="10001", summary="a", author_nickname="小明")
    s.mark_seen("t1", "2026-08-31T12:00:00")
    rows = s.recent_seen(limit=5, days=3, now=datetime(2026, 8, 31, 12, 0, 0))
    assert rows[0]["author_nickname"] == "小明"
    # 旧列缺失迁移:预建缺列表(如实复刻 T2 原始列集)→ ensure_schema 补列不抛
    store2 = SQLiteStore(tmp_path / "old.db")
    store2.execute(
        "CREATE TABLE qzone_feeds (tid TEXT PRIMARY KEY, abstime TEXT NOT NULL DEFAULT '', "
        "author_uin TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', "
        "state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'seen')), "
        "interacted INTEGER NOT NULL DEFAULT 0, injected_at TEXT NOT NULL DEFAULT '', "
        "created_at TEXT NOT NULL DEFAULT '')"
    )
    s2 = SeenStore(store2)
    s2.ensure_schema()  # ALTER 补列
    s2.mark_queued("x", abstime="", author_uin="u", summary="", author_nickname="旧库")
    s2.mark_seen("x", "2026-08-31T12:00:00")
    assert s2.recent_seen(limit=1, days=1, now=datetime(2026, 8, 31, 12, 0, 0))[0]["author_nickname"] == "旧库"
