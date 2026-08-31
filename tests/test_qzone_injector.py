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


def _tf(tid):
    """时间序测试辅助:tid 形如 a1/a2/b1,abstime 取尾号(跨好友乱序入队)。"""
    return FeedItem(tid=tid, abstime=str(tid[-1]), uin="u" + tid[0], nickname="n", content="c")


def test_enqueue_keeps_global_ascending_time_order():
    """联调缺陷#6(队列无序):入队即全局按发布时间升序(补叙式阅读,从旧到新),
    跨好友/跨轮次合并保序——按好友分组入队会让 bot 先读完 A 的全部再读 B。"""
    inj = FeedInjector(decision_window_s=75)
    inj.window_started()
    inj.enqueue([_tf("a3"), _tf("a1"), _tf("a2")])  # 同好友乱序
    inj.enqueue([_tf("b2"), _tf("b1")])  # 第二批(另一好友)
    order = [inj.next_to_inject(now=1.0).tid]
    inj.mark_injected(order[0], 1.0)
    while True:
        inj.on_turn_complete(now=2.0)
        f = inj.next_to_inject(now=3.0)
        if f is None:
            break
        order.append(f.tid)
        inj.mark_injected(f.tid, 3.0)
    assert order == ["a1", "b1", "a2", "b2", "a3"]  # 全局按 abstime 升序(尾号即时间,同刻按入队稳定序)


def test_awaiting_feed_and_author():
    """awaiting_feed/awaiting_author 暴露当前动态完整引用(注入块按人上下文/说话人交叉校验用)。"""
    inj = FeedInjector(decision_window_s=75)
    inj.window_started()
    inj.enqueue([_tf("x1")])
    assert inj.awaiting_author == ""  # 未注入时无当前作者
    f = inj.next_to_inject(now=1.0)
    inj.mark_injected(f.tid, 1.0)
    assert inj.awaiting_tid == "x1"
    assert inj.awaiting_feed is not None and inj.awaiting_feed.uin == "ux"
    assert inj.awaiting_author == "ux"
    inj.on_turn_complete(now=2.0)
    assert inj.awaiting_feed is None and inj.awaiting_author == ""
