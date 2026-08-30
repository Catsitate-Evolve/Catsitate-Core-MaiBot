"""串行注入状态机测试(spec §2.4):轮完成推进/超时兜底/wait 延长/窗口回退。"""
from catsitate_core.qzone.injector import FeedInjector
from catsitate_core.qzone.protocol import FeedItem


def _f(tid):
    return FeedItem(tid=tid, abstime="1", uin="u", nickname="n", content=f"c{tid}")


def test_serial_one_at_a_time_until_turn_complete():
    inj = FeedInjector(decision_window_s=75)
    inj.window_started()
    assert inj.enqueue([_f("a"), _f("b")]) == 2
    a = inj.next_to_inject(now=100.0)
    assert a.tid == "a"
    inj.mark_injected("a", 100.0)
    assert inj.next_to_inject(now=101.0) is None  # awaiting 未释放:不注入下一条
    inj.on_turn_complete(now=160.0)  # 轮完成信号(planner 无 tool_calls)
    b = inj.next_to_inject(now=161.0)
    assert b.tid == "b"


def test_timeout_fallback_allows_advance():
    inj = FeedInjector(decision_window_s=75)
    inj.window_started()
    inj.enqueue([_f("a"), _f("b")])
    inj.next_to_inject(now=100.0) and inj.mark_injected("a", 100.0)
    assert inj.awaiting_timed_out(now=100.0 + 74) is False
    assert inj.awaiting_timed_out(now=100.0 + 76) is True  # 常规超时
    inj.force_release(now=100.0 + 76)
    assert inj.next_to_inject(now=100.0 + 77).tid == "b"


def test_wait_state_extends_hard_cap_3x():
    inj = FeedInjector(decision_window_s=75, hard_cap_multiplier=3)
    inj.window_started()
    inj.enqueue([_f("a")])
    inj.mark_injected("a", 100.0)
    inj.on_wait_state(now=110.0)  # planner 调 wait:窗口延长
    assert inj.awaiting_timed_out(now=100.0 + 200) is False  # 3x 内不算超时(wait 态)
    assert inj.awaiting_timed_out(now=100.0 + 230) is True   # 3x 硬上限
    inj.force_release(now=100.0 + 230)
    assert inj.awaiting_tid == ""


def test_window_end_discards_queue_state():
    inj = FeedInjector(decision_window_s=75)
    inj.window_started()
    inj.enqueue([_f("a"), _f("b")])
    inj.window_ended()
    assert inj.window_active is False
    assert inj.queue_size() == 0 and inj.next_to_inject(now=1.0) is None


def test_describe_current_and_stats():
    inj = FeedInjector(decision_window_s=75)
    inj.window_started()
    inj.enqueue([_f("a")])
    inj.mark_injected("a", 100.0)
    d = inj.describe_current()
    assert "a" in d and "c" in d  # tid 与内容摘要出现在状态行
    st = inj.stats()
    assert st["injected"] == 1 and st["queued"] == 0
