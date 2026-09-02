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


def test_enqueue_keeps_global_descending_time_order():
    """T11 阅读顺序改降序:信息流降序——QQ 空间 App 实际形态,最新在上。
    入队即全局按发布时间降序(新→旧),跨好友/跨轮次合并保序——按好友分组
    入队会让 bot 先读完 A 的全部再读 B(联调缺陷#6 的保序要求不变)。"""
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
    assert order == ["a3", "a2", "b2", "a1", "b1"]  # 全局按 abstime 降序(尾号即时间,同刻按入队稳定序)


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


def test_priority_queue_preempts_browse():
    """P1(通知)插队 P2(浏览):P1 非空时优先弹出,P1 清空后回到 P2。"""
    inj = FeedInjector(decision_window_s=75)
    inj.window_started()
    inj.enqueue([_f("a1"), _f("a2")])           # P2:两条浏览动态
    inj.enqueue_priority([_f("n1")])             # P1:一条通知
    # 第一条应是 P1 的 n1(通知优先)
    f = inj.next_to_inject(now=1.0)
    assert f.tid == "n1"
    inj.mark_injected("n1", 1.0)
    inj.on_turn_complete(now=2.0)
    # P1 清空后回到 P2
    f2 = inj.next_to_inject(now=3.0)
    assert f2.tid == "a1"
    inj.mark_injected("a1", 3.0)
    inj.on_turn_complete(now=4.0)
    f3 = inj.next_to_inject(now=5.0)
    assert f3.tid == "a2"


def test_priority_queue_sorted_by_abstime_descending():
    """P1(通知)按发布时间降序(新→旧),与 P2 阅读顺序一致(T11 计划裁定:
    非按到达 FIFO——多条通知积压时最新先看,信息流降序同款语义)。"""
    inj = FeedInjector(decision_window_s=75)
    inj.window_started()
    inj.enqueue_priority([FeedItem(tid="late", abstime="200", uin="u", nickname="n", content="c")])   # 先到(abstime 晚)
    inj.enqueue_priority([FeedItem(tid="early", abstime="1", uin="u", nickname="n", content="c")])    # 后到但 abstime 更早
    f = inj.next_to_inject(now=1.0)
    assert f.tid == "late"  # P1 内按 abstime 降序(最新在上),不按到达序
    inj.mark_injected("late", 1.0)
    inj.on_turn_complete(now=2.0)
    assert inj.next_to_inject(now=3.0).tid == "early"


def test_priority_preempts_mid_browse_and_late_arrivals_wait():
    """混合场景:P1 到达即插队;awaiting 未释放时新 P1 只入队不抢当前;
    P1 连续清空后才回到 P2,且 P2 原有顺序不受 P1 插队影响。"""
    inj = FeedInjector(decision_window_s=75)
    inj.window_started()
    inj.enqueue([_f("a1"), _f("a2")])            # P2:两条浏览动态
    f = inj.next_to_inject(now=1.0)
    assert f.tid == "a1"                          # P1 尚空:浏览照常
    inj.mark_injected("a1", 1.0)
    inj.enqueue_priority([_f("n1")])              # awaiting 中:入队不抢 a1 的回复轮
    assert inj.next_to_inject(now=2.0) is None    # 串行语义不变:awaiting 未释放
    inj.on_turn_complete(now=3.0)
    assert inj.next_to_inject(now=4.0).tid == "n1"  # 轮完成即插队
    inj.mark_injected("n1", 4.0)
    inj.enqueue_priority([_f("n0")])              # 二条通知再插队
    inj.on_turn_complete(now=5.0)
    assert inj.next_to_inject(now=6.0).tid == "n0"
    inj.mark_injected("n0", 6.0)
    inj.on_turn_complete(now=7.0)
    assert inj.next_to_inject(now=8.0).tid == "a2"  # P1 清空回到 P2,a2 保序未丢
    inj.mark_injected("a2", 8.0)
    inj.on_turn_complete(now=9.0)
    assert inj.next_to_inject(now=10.0) is None


def test_queue_size_counts_both_queues():
    inj = FeedInjector(decision_window_s=75)
    inj.window_started()
    inj.enqueue([_f("a1"), _f("a2")])
    assert inj.queue_size() == 2
    inj.enqueue_priority([_f("n1")])
    assert inj.queue_size() == 3  # P1+P2 合计


def test_describe_current_distinguishes_queues():
    inj = FeedInjector(decision_window_s=75)
    inj.window_started()
    assert inj.describe_current() == "暂无新动态"
    inj.enqueue([_f("a1"), _f("a2")])
    inj.enqueue_priority([_f("n1")])
    d = inj.describe_current()
    assert "通知队列 1 条" in d and "浏览队列 2 条" in d  # 双队列分计呈现
    inj2 = FeedInjector(decision_window_s=75)
    inj2.window_started()
    inj2.enqueue([_f("a1")])
    assert "浏览队列 1 条" in inj2.describe_current()  # 仅 P2 时不虚报通知队列


def test_stats_p1_p2_breakdown():
    inj = FeedInjector(decision_window_s=75)
    inj.window_started()
    inj.enqueue([_f("a1"), _f("a2")])
    inj.enqueue_priority([_f("n1")])
    st = inj.stats()
    assert st["queued"] == 3           # 合计(与 queue_size 一致)
    assert st["p1_queued"] == 1 and st["p2_queued"] == 2


def test_mark_injected_resolves_item_from_priority_queue():
    """不经弹出的直接标记(mark_injected 回退路径)也要能从 P1 找到条目。"""
    inj = FeedInjector(decision_window_s=75)
    inj.window_started()
    inj.enqueue([_f("a1")])
    inj.enqueue_priority([_f("n1")])
    inj.mark_injected("n1", 1.0)  # 未走 next_to_inject 弹出
    assert inj.awaiting_tid == "n1" and inj.awaiting_feed is not None
    assert inj.queue_size() == 1  # n1 已从 P1 移除,剩 P2 的 a1
    inj.on_turn_complete(now=2.0)
    assert inj.next_to_inject(now=3.0).tid == "a1"


def test_window_end_clears_p2_and_awaiting_keeps_p1():
    """窗口结束:浏览队列(P2)与 awaiting 状态一并丢弃,通知队列(P1)保留
    (M3-r2 通知推送语义:通知不隶属任何窗口,等注入条件满足后继续)。"""
    inj = FeedInjector(decision_window_s=75)
    inj.window_started()
    inj.enqueue([_f("a1"), _f("a2")])
    popped = inj.next_to_inject(now=1.0)  # 弹出 P2 首条(a1)
    assert popped is not None and popped.tid == "a1"
    inj.mark_injected(popped.tid, 1.0)    # awaiting 中:模拟窗口在注入等待期结束
    inj.enqueue_priority([_f("n")])       # awaiting 期间通知到达(P1 入队不抢当前)
    inj.window_ended()
    assert inj.stats()["p2_queued"] == 0 and inj.stats()["p1_queued"] == 1  # a2 清,n 保留
    assert inj.awaiting_tid == "" and inj.awaiting_timed_out(now=2.0) is False  # awaiting 已清


def test_p1_injectable_without_window():
    """通知是推送语义:窗口未开也能注入。"""
    inj = FeedInjector(decision_window_s=10)
    inj.enqueue_priority([_f("n1")])
    assert inj.next_to_inject(0.0) is not None


def test_p2_blocked_without_window():
    """浏览动态只在窗口内注入。"""
    inj = FeedInjector(decision_window_s=10)
    inj.enqueue([_f("f1")])
    assert inj.next_to_inject(0.0) is None
    inj.window_started()
    assert inj.next_to_inject(0.0) is not None


def test_window_ended_keeps_p1():
    """窗口结束清浏览队列,通知队列保留等待注入条件。"""
    inj = FeedInjector(decision_window_s=10)
    inj.window_started()
    inj.enqueue([_f("f1")])
    inj.enqueue_priority([_f("n1")])
    inj.window_ended()
    st = inj.stats()
    assert st["p2_queued"] == 0 and st["p1_queued"] == 1
    assert inj.next_to_inject(0.0) is not None  # P1 仍可注入


def test_requeue_popped_restores_cancelled_item():
    """取消回队(2026-09-02 终审修复):泵在弹出与标记之间被取消时,在途项
    回原队列队首(P1 回 P1/P2 回 P2),不静默丢失;无弹出项时 no-op。"""
    from catsitate_core.qzone.protocol import FeedItem

    inj = FeedInjector(decision_window_s=60)
    inj.window_started()
    notify = FeedItem(tid="n1", abstime="1", uin="1", nickname="a", content="c", source="notify")
    feed = FeedItem(tid="f1", abstime="2", uin="2", nickname="b", content="d")
    inj.enqueue_priority([notify])
    inj.enqueue([feed])

    popped = inj.next_to_inject(0.0)
    assert popped.tid == "n1"
    inj.requeue_popped()
    assert inj.next_to_inject(0.0).tid == "n1"  # 回 P1 队首,可重新弹出
    inj.requeue_popped()  # 回队后再弹出→再回队
    assert inj.queue_size() == 2  # P1 一条 + P2 一条
    inj.requeue_popped()  # 无在途弹出项:no-op
    assert inj.queue_size() == 2
