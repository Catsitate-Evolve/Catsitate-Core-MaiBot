"""赞事件去重存储测试():is_new 发现即登记 + 30 天修剪。"""

from datetime import datetime, timedelta


def test_like_seen_dedup_and_prune(tmp_path):
    from catsitate_core.storage import SQLiteStore
    from catsitate_core.qzone.like_seen import LikeSeenStore

    store = LikeSeenStore(SQLiteStore(tmp_path / "t.db"))
    assert store.is_new("100_200_ab12", liker_uin="100", target_tid="t1") is True
    assert store.is_new("100_200_ab12", liker_uin="100", target_tid="t1") is False
    assert store.is_new("101_200_ab12", liker_uin="101", target_tid="t1") is True


def test_like_seen_prune_cutoff_by_created_at(tmp_path):
    """修剪按 created_at 时间下界:过期行删除、窗口内行保留,返回删除条数。"""
    from catsitate_core.storage import SQLiteStore
    from catsitate_core.qzone.like_seen import LikeSeenStore

    store = LikeSeenStore(SQLiteStore(tmp_path / "t.db"))
    now = datetime.now()
    store.is_new("100_200_old1", liker_uin="100", target_tid="t1")
    store.is_new("101_200_old2", liker_uin="101", target_tid="t2")
    store.is_new("102_200_new1", liker_uin="102", target_tid="t3")
    # 把前两行回写为 31 天前(直接改表,模拟存量过期数据)
    stale = (now - timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%S")
    store.store.execute(
        "UPDATE qzone_likes SET created_at = ? WHERE like_key IN ('100_200_old1', '101_200_old2')",
        (stale,),
    )
    assert store.prune(days=30) == 2
    assert store.is_new("100_200_old1", liker_uin="100", target_tid="t1") is True  # 已删,重登记
    assert store.is_new("102_200_new1", liker_uin="102", target_tid="t3") is False  # 窗口内保留
