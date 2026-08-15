"""好感度批次引擎测试:计数/触发/材料构造/等级。"""

from datetime import datetime

from catsitate_core.config import FavorabilitySection
from catsitate_core.favorability import BatchEngine, LEVELS
from catsitate_core.storage import SQLiteStore

NOW = datetime(2026, 8, 14, 12, 0, 0)


def make_engine(tmp_path):
    store = SQLiteStore(tmp_path / "fav.db")
    engine = BatchEngine(store, FavorabilitySection())
    engine.ensure_schema()
    return engine, store


def test_count_and_early_trigger(tmp_path):
    engine, _ = make_engine(tmp_path)
    for _ in range(20):
        engine.count_message("u1", "s1", now=lambda: NOW)
    stat = engine.count_message("u1", "s1", now=lambda: NOW)
    assert stat["messages"] == 21
    assert stat["reached_early_threshold"] is True
    assert engine.check_trigger("u1", "s1", now=lambda: NOW) == "early"


def test_early_settle_daily_cap(tmp_path):
    engine, _ = make_engine(tmp_path)
    for i in range(3):
        judged_at = NOW.strftime("%Y-%m-%dT%H:%M:%S")
        engine.reset_batch("u1", "s1", judged_at)
        for _ in range(20):
            engine.count_message("u1", "s1", now=lambda: NOW)
        engine.apply_delta("u1", "s1", 1, f"第{i}次", judged_at=judged_at, judge_id=f"early-{i}")
    engine.reset_batch("u1", "s1", NOW.strftime("%Y-%m-%dT%H:%M:%S"))
    for _ in range(20):
        engine.count_message("u1", "s1", now=lambda: NOW)
    assert engine.check_trigger("u1", "s1", now=lambda: NOW) is None  # 当日提前结算已达 3 次


def test_group_material_anchored_by_user_messages(tmp_path):
    engine, _ = make_engine(tmp_path)
    history = [
        {"role": "user", "user_id": "u9", "stream_id": "g", "text": "x", "seq": 1, "ts": "2026-08-14T10:00:01"},
        {"role": "user", "user_id": "u1", "stream_id": "g", "text": "你好", "seq": 2, "ts": "2026-08-14T10:00:02"},
        {"role": "bot", "user_id": "bot", "stream_id": "g", "text": "你好呀", "seq": 3, "ts": "2026-08-14T10:00:03"},
        {"role": "user", "user_id": "u2", "stream_id": "g", "text": "y", "seq": 4, "ts": "2026-08-14T10:00:04"},
        {"role": "user", "user_id": "u1", "stream_id": "g", "text": "在吗", "seq": 5, "ts": "2026-08-14T10:00:05"},
    ]
    material = engine.build_material("u1", "g", history)
    text = "\n".join(material)
    assert "你好" in text and "在吗" in text  # 锚定用户消息
    assert "你好呀" in text  # bot 发言随附
    assert "x" in text and "y" in text  # 紧邻上下文
    assert text.index("你好") < text.index("在吗")  # 时间正序


def test_material_respects_batch_window(tmp_path):
    """结算后的旧批次消息不进入新批次素材(规格 §4.3「批次内」)。"""

    engine, _ = make_engine(tmp_path)
    old = "2026-08-14T09:00:00"
    newer = "2026-08-14T11:00:00"
    history = [
        {"role": "user", "user_id": "u1", "stream_id": "p", "text": "旧批次", "seq": 1, "ts": old},
        {"role": "user", "user_id": "u1", "stream_id": "p", "text": "新批次", "seq": 2, "ts": newer},
    ]
    # 首次(无窗口)两条都在
    assert len(engine.build_material("u1", "p", history)) == 2
    # 结算开新批次(window_start = 10:00),旧消息被排除(先计数建批次行,与生产流程一致)
    engine.count_message("u1", "p", now=lambda: NOW)
    engine.reset_batch("u1", "p", "2026-08-14T10:00:00")
    material = engine.build_material("u1", "p", history)
    text = "\n".join(material)
    assert "新批次" in text and "旧批次" not in text


def test_material_truncates_long_single_message(tmp_path):
    engine, _ = make_engine(tmp_path)
    history = [
        {"role": "user", "user_id": "u1", "stream_id": "p", "text": "长" * 300, "seq": 1, "ts": "2026-08-14T10:00:01"},
    ]
    material = engine.build_material("u1", "p", history)
    assert len(material[0].rsplit(") ", 1)[-1]) <= 200 + 1  # 截断后 ≤200 字符(含省略号)


def test_private_material_contains_bot_and_user(tmp_path):
    engine, _ = make_engine(tmp_path)
    history = [
        {"role": "user", "user_id": "u1", "stream_id": "p", "text": "早", "seq": 1, "ts": "2026-08-14T10:00:01"},
        {"role": "bot", "user_id": "bot", "stream_id": "p", "text": "早安", "seq": 2, "ts": "2026-08-14T10:00:02"},
    ]
    material = engine.build_material("u1", "p", history)
    assert "早" in "\n".join(material) and "早安" in "\n".join(material)


def test_apply_delta_level_and_note_truncation(tmp_path):
    engine, _ = make_engine(tmp_path)
    engine.apply_delta("u1", "s1", 8, "注" * 60, judged_at="early-x")
    row = engine.get_level("u1", "s1")
    assert row["level"] == 0  # 8 分 → 陌生
    assert row["score"] == 8
    assert len(row["note"]) == 40
    assert engine.get_best_level_for_user("u1")["level"] == 0


def test_material_anchor_at_stream_head_no_wraparound(tmp_path):
    """锚点在流首时,前邻居不存在,不得因负索引回绕取流尾消息。"""

    engine, _ = make_engine(tmp_path)
    history = [
        {"role": "user", "user_id": "u1", "stream_id": "g", "text": "我的消息", "seq": 0, "ts": "2026-08-14T10:00:00"},
        {"role": "user", "user_id": "u2", "stream_id": "g", "text": "后文1", "seq": 1, "ts": "2026-08-14T10:00:01"},
        {"role": "user", "user_id": "u2", "stream_id": "g", "text": "后文2", "seq": 2, "ts": "2026-08-14T10:00:02"},
        {"role": "user", "user_id": "u2", "stream_id": "g", "text": "后文3", "seq": 3, "ts": "2026-08-14T10:00:03"},
    ]
    material = engine.build_material("u1", "g", history)
    text = "\n".join(material)
    assert "我的消息" in text
    assert "后文1" in text  # 紧邻后文应选中
    assert "后文3" not in text  # 流尾消息不得作为"前邻居"回绕选中


def test_levels_order():
    assert LEVELS == ["陌生", "熟悉", "亲近", "挚友", "特别"]
