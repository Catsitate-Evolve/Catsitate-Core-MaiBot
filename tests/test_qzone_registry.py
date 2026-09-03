"""FeedContextRegistry 测试(工具驱动架构 2026-09-01,Step 1):LRU 逐出/TTL 过期/
精确与前缀解析——工具目标解析的数据源,替代意图绑定。"""

from __future__ import annotations

import time

from catsitate_core.qzone.registry import FeedContext, FeedContextRegistry


def _ctx(tid: str, **kw) -> FeedContext:
    args = dict(tid=tid, owner_uin="10001", owner_nickname="小明")
    args.update(kw)
    return FeedContext(**args)


def test_register_and_resolve_exact_match():
    reg = FeedContextRegistry()
    reg.register(_ctx("tid_full_0001", commenter_uin="20000", comment_tid="c1"))
    ctx = reg.resolve("tid_full_0001")
    assert ctx is not None and ctx.owner_uin == "10001" and ctx.commenter_uin == "20000"


def test_resolve_prefix_match_for_message_anchor():
    """消息尾部锚只展示 tid 前 12 位,模型照抄调用工具——键为全量 tid 时
    resolve 须按前缀回退命中(qzone_like 内部缺省路径传全量 tid 则走精确)。"""

    reg = FeedContextRegistry()
    reg.register(_ctx("abcdef1234567890xyz"))
    assert reg.resolve("abcdef123456") is not None  # 12 位锚前缀命中
    assert reg.resolve("abcdef1234567890xyz") is not None  # 全量 tid 精确命中
    assert reg.resolve("zzz_no_match") is None
    assert reg.resolve("") is None  # 空串显式失败(不臆造)


def test_resolve_prefix_prefers_most_recently_used():
    """同前缀多键(理论上 12 位碰撞):取最近使用端,不臆取任意条。"""

    reg = FeedContextRegistry()
    reg.register(_ctx("prefixAAAA_0001", owner_uin="1"))
    reg.register(_ctx("prefixAAAA_0002", owner_uin="2"))
    ctx = reg.resolve("prefixAAAA")
    assert ctx is not None and ctx.owner_uin == "2"  # 后注册的在 MRU 端


def test_lru_evicts_oldest_beyond_max_entries():
    reg = FeedContextRegistry(max_entries=2)
    reg.register(_ctx("t1"))
    reg.register(_ctx("t2"))
    reg.resolve("t1")  # t1 触底(变最近使用)
    reg.register(_ctx("t3"))  # 超上限:逐出最久未用的 t2
    assert reg.resolve("t2") is None
    assert reg.resolve("t1") is not None and reg.resolve("t3") is not None


def test_ttl_expiry_removes_entry(monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    reg = FeedContextRegistry(ttl_seconds=50)
    reg.register(_ctx("t1"))
    clock["now"] = 130.0  # 30s 后:仍在窗内
    assert reg.resolve("t1") is not None
    clock["now"] = 200.0  # 100s 后:超 TTL
    assert reg.resolve("t1") is None
    assert reg.resolve("t1") is None  # 已删除,重复解析同样失败(非每次都过期的慢性泄漏)


def test_reregister_refreshes_entry_and_ttl(monkeypatch):
    """同 tid 重复登记(同说说再次注入通知场景):非空新值覆盖旧值并刷新
    TTL+LRU 位(空字段保留旧值,见合并语义测试)。"""

    clock = {"now": 100.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    reg = FeedContextRegistry(ttl_seconds=100)
    reg.register(_ctx("t1", owner_uin="old"))
    clock["now"] = 160.0  # 60s 后重新登记(覆盖旧值+刷新 TTL)
    reg.register(_ctx("t1", owner_uin="new"))
    clock["now"] = 250.0  # 距刷新 90s(<100)存活;距首次 150s(若无刷新已过期)
    ctx = reg.resolve("t1")
    assert ctx is not None and ctx.owner_uin == "new"


def test_register_field_merge_preserves_comment_context():
    """字段级合并(2026-09-02 联调缺陷修复):通知登记的 commenter/comment
    二元组,不被后来的浏览/view_friend_feeds 登记(kind=feed,无评论者信息)
    冲掉——实机缺陷:好友回复后 bot 查看了其说说,qzone_reply 解析时评论者
    回退成 bot 自己(@错人);新通知仍可更新评论者信息。"""

    reg = FeedContextRegistry()
    reg.register(_ctx("tidX", commenter_uin="3298178030", commenter_nickname="可回收飞舞",
                      comment_tid="8", comment_uin="3545773341", kind="notify_reply"))
    # 浏览/查看再登记:owner 同、无评论者字段、正文与近评有新值
    reg.register(_ctx("tidX", kind="feed", content_summary="测试二",
                      comment_map={"8": ("3545773341", "")}))
    ctx = reg.resolve("tidX")
    assert ctx is not None
    assert ctx.commenter_uin == "3298178030"  # 评论者保留(不被浏览条目清空)
    assert ctx.commenter_nickname == "可回收飞舞" and ctx.comment_tid == "8"
    assert ctx.content_summary == "测试二" and ctx.comment_map["8"] == ("3545773341", "")  # 通知评论锚保留
    assert ctx.kind == "notify_reply"  # 浏览条目不清掉通知语义
    # 新通知(另一位评论者)仍更新评论者信息
    reg.register(_ctx("tidX", commenter_uin="40000", commenter_nickname="新评论者",
                      comment_tid="9", kind="notify_comment"))
    ctx = reg.resolve("tidX")
    assert ctx.commenter_uin == "40000" and ctx.comment_tid == "9"
    assert ctx.content_summary == "测试二"  # 通知条目未带的字段保留旧值


def test_clear_drops_all_entries():
    reg = FeedContextRegistry()
    reg.register(_ctx("t1"))
    reg.clear()
    assert reg.resolve("t1") is None


def test_feed_context_new_fields_default():
    """content_summary/comment_map 带默认值——旧构造点(通知/回退路径)不传
    也不炸;recent_comments 死字段已删(Q7 裁定 2026-09-02)。"""
    ctx = FeedContext(tid="t", owner_uin="1")
    assert ctx.content_summary == "" and ctx.comment_map == {}
    assert not hasattr(ctx, "recent_comments")  # 死字段已删
