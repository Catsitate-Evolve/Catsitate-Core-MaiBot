# QQ空间 M2.1（统一通知通道）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 将评论轮询重构为统一通知通道（模拟推送通知）：高频短间隔(120s)双源检测(自己说说新评论+自己在他人说说下评论收到的新回复)，通知优先于浏览动态注入（双优先级队列），模拟人类「刷着动态→弹通知→先看通知→回完继续刷」的注意力模型。

**Architecture:** FeedInjector 扩为双优先级队列（P1=通知/P2=浏览动态，串行注入不变）；wire.py 补 ReplyItem 与 parse_feed_replies(楼中楼解析)；comment_seen 存 friend_uin 支持反查；统一通知轮询替代 comment_poll_tick；场景文案反映双内容形态。

**Spec:** `docs/superpowers/specs/2026-08-30-phase3-qzone-design.md` §3.7（需追加统一通知设计）

## Global Constraints
- 只改插件目录；简体中文；错误显式暴露；SDK-only；测试无网络无主程序
- 串行注入语义不变（一次一条、轮完成推进、超时兜底、wait 硬上限）
- 意图一次性消费语义不变（远端成功即刻置 None）
- 动作 API 不重试（QzoneAuthError→invalidate 例外）

---

### Task 9: wire.py 楼中楼解析 + comment_seen 扩展

**Files:**
- Create: `catsitate_core/qzone/wire.py`（追加）
- Modify: `catsitate_core/qzone/comment_seen.py`
- Test: `tests/test_qzone_wire.py`, `tests/test_qzone_comment_seen.py`

**Interfaces:**
- Produces:
  - `@dataclass ReplyItem: reply_tid: str; parent_comment_tid: str; uin: str; nickname: str; content: str; create_time: str; feed_tid: str; friend_uin: str`
  - `parse_feed_replies(payload: dict, *, bot_uin: str) -> list[ReplyItem]` — 在 msglist→commentlist 中找 uin==bot_uin 的条目，解析其 list_3 数组中的新回复
  - `CommentSeenStore.note_bot_comment(feed_tid, friend_uin, bot_text, at_iso)` — 签名扩展（原3参→4参，第2参 friend_uin）
  - `CommentSeenStore.bot_commented_friends() -> list[str]` — DISTINCT friend_uin

- [ ] **Step 1:** 失败测试（楼中楼样本+friend_uin 存储/反查）
- [ ] **Step 2:** FAIL
- [ ] **Step 3:** 实现（list_3 解析容错：非 list/缺字段跳过；reply_tid 数值归一字符串；comment_seen 表 qzone_comments 增列 friend_uin TEXT DEFAULT ''，PRAGMA 迁移）
- [ ] **Step 4:** PASS + 全量
- [ ] **Step 5:** Commit

### Task 10: FeedInjector 双优先级队列

**Files:**
- Modify: `catsitate_core/qzone/injector.py`
- Test: `tests/test_qzone_injector.py`

**Interfaces:**
- `enqueue(feeds) -> int` — 语义不变（P2，浏览动态）
- `enqueue_priority(items: list[FeedItem]) -> int` — 新方法（P1，通知）
- `next_to_inject(now) -> FeedItem | None` — 优先取 P1，P1 空取 P2
- 其余接口（window_started/ended/mark_injected/on_turn_complete/on_wait_state/awaiting_timed_out/force_release/awaiting_tid/awaiting_feed/awaiting_author/describe_current/stats）签名与语义**全部不变**

- [ ] **Step 1:** 失败测试（P1 插队/P2 队列保序/混合场景）
- [ ] **Step 2:** FAIL
- [ ] **Step 3:** 实现（两个内部队列 `_queue_p1: list[FeedItem]` / `_queue_p2: list[FeedItem]`，各自入队时按 abstime 升序排；next_to_inject 弹 P1 首条，P1 空弹 P2；queue_size 返回两者之和；describe_current 区分「通知队列 N 条/浏览队列 M 条」；window_ended 清两者）
- [ ] **Step 4:** PASS + 全量（既有用例全不破——enqueue 语义不变）
- [ ] **Step 5:** Commit

### Task 11: 统一通知轮询重构 + plugin 接线

**Files:**
- Modify: `plugin.py`（重写 `_qzone_comment_poll_tick`→`_qzone_notify_poll_tick`），`catsitate_core/qzone/scene.py`（场景文案），`catsitate_core/config.py`（新字段）
- Test: `tests/test_qzone_gateway.py`（源码断言更新）

**核心改动：**
1. **on_load**：`qzone_comment_poll` 注册间隔改 `max(self.config.qzone.notification_interval_seconds, 30)` 秒；config 新字段 `notification_interval_seconds: int = _f(120, "统一通知轮询间隔(秒,模拟推送通知的检查频率)", label="通知间隔(秒)")`
2. **`_qzone_notify_poll_tick`**（替代原 comment_poll_tick）：
   - 守卫：available / enabled / 非睡眠（**不再检查浏览窗口**——通知始终运行）
   - 意图互斥：`if self._qzone_outbound_intent is not None: return`（通知等下轮，不叠加）
   - 源 A：`get_own_feed_comments(bot_uin)` → 新评论（is_new 判重）→ 新鲜度截断 → 构造通知 FeedItem → `enqueue_priority`
   - 源 B：`bot_commented_friends()` → 每好友 `get_user_feeds(friend_uin)` → `parse_feed_replies(payload, bot_uin)` → 新回复（is_new 键 `{feed_tid}:{parent_comment_tid}:reply:{reply_tid}`）→ 新鲜度截断 → 构造通知 FeedItem → `enqueue_priority`
   - 注入泵推进：`await self._qzone_pump()`（泵会从 P1 优先取）
3. **通知 FeedItem 构造**：
   ```python
   # 源 A(自己说说新评论)
   FeedItem(tid=f"notify_comment_{feed_tid}_{c.comment_tid}", abstime=c.create_time, uin=c.uin, nickname=c.nickname,
            content=f"(通知) {c.nickname} 评论了你的说说「{feed_summary[:30]}」\n{c.nickname}: {c.content}", image_urls=[])
   # 源 B(他人说说下我的评论被回复)
   FeedItem(tid=f"notify_reply_{feed_tid}_{reply.reply_tid}", abstime=reply.create_time, uin=reply.uin, nickname=reply.nickname,
            content=f"(通知) {reply.nickname} 回复了你在 {friend_nick} 说说下的评论\n你: {bot_comment_text[:40]}\n{reply.nickname}: {reply.content}", image_urls=[])
   ```
4. **意图设定**：泵注入 P1 项后，`_qzone_outbound_intent` 按内容前缀判定——`(通知) ... 评论了`→comment_reply(自己说说,comment_tid)；`(通知) ... 回复了`→comment_reply(他人说说,parent_comment_tid)。**注入泵不知道意图**，意图在 pump 的注入成功后由 plugin 设定（需在 pump 中区分 P1/P2 来源——FeedItem 增 `source: str = "feed"` 字段，P1 通知带 `source="notify"`，pump 注入后据 source 设意图）。
5. **场景文案**：QZONE_SCENE_TEXT 末尾改「…你对说说的回复→评论；对通知中评论/回复的回应→楼中楼回复。可用 qzone_like 给当前说说点赞。」
6. **旧字段**：`comment_poll_interval_minutes` 标注「(废弃,由 notification_interval_seconds 替代)」——config 不删字段（向后兼容），代码不再消费。
7. **源码断言**：`test_m2_wiring_source_assertions` 更新——`"_qzone_notify_poll_tick" in src`、`"enqueue_priority" in src`、`"parse_feed_replies" in src`、`'"通知" in src'` 场景断言。

- [ ] **Step 1:** 源码断言 RED
- [ ] **Step 2:** 按锚定实现（先读 plugin.py 对应方法再改）
- [ ] **Step 3:** 全量回归 + ast.parse
- [ ] **Step 4:** Commit

### Task 12: 收尾——spec/manual/CHANGELOG + 全量回归

**Files:**
- Modify: spec §3.7 追加统一通知设计段落；manual §3.13 评论轮询→统一通知；CHANGELOG 追加 M2.1 条目
- Test: 全量

- [ ] 文档三处 → 全量 → Commit

## Self-Review
- 冲突 1(意图)→双优先级队列+source 字段区分；冲突 2(场景)→文案更新；冲突 3(反查)→friend_uin 列；冲突 4(楼中楼)→ReplyItem；冲突 5(间隔)→notification_interval_seconds；冲突 6(队列)→FeedInjector 双队列
- FeedItem.source 字段是新增（默认 "feed"），不破既有 T1-T8 消费
- comment_seen 旧签名调用点（T6 的 note_bot_comment 两处）需同步补 friend_uin 参数
