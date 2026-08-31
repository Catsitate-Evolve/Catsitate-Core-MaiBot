# QQ空间 M3（统一时间线架构重构）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 浏览流从「逐好友拉取」重构为「统一时间线发现→仅新动态充实」两层混合架构（feeds3_html_more 1 次调用覆盖全好友，O(1) 与好友数无关）；源B搭发现层便车（交叉匹配被评论好友，仅对活跃说说拉评论）；QQ空间 API 全面封装为清晰分层。

**Architecture:**
```
QzoneClient（API 封装层）
├── 发现 API: get_unified_timeline(count) -> list[FeedDiscovery]  # feeds3_html_more
├── 内容 API: get_user_feeds(uin, nick, num) / get_own_feed_comments(bot_uin)  # emotion_cgi_msglist_v6
├── 写 API: do_like / do_comment / do_reply
└── 基础: _fetch_msglist / _fetch_unified / _post / CookieManager
```

**Spec:** spec §3.7 将追加统一时间线设计

## Global Constraints
- 只改插件目录；简体中文；错误显式暴露；SDK-only；测试无网络
- 串行注入/意图一次性消费/动作不重试等 M2+M2.1 固化语义全部不变
- 发现层解析失败→告警回退到逐好友旧路径（不静默）

---

### Task 1: 统一时间线解析器（纯函数）

**Files:** Create `catsitate_core/qzone/discovery.py`; Test `tests/test_qzone_discovery.py`

**Interfaces:**
- `@dataclass FeedDiscovery: tid: str; uin: str; nickname: str; abstime: str; appid: int`
- `parse_unified_timeline(text: str) -> list[FeedDiscovery]` — 从 feeds3_html_more 响应提取全部动态条目

**解析策略（鲁棒正则，不依赖 bs4/json5）：**
feeds3_html_more 返回 JSON 外壳 + JS 对象内嵌数据。每个动态条目含一组连续字段：
```
key:'{tid}', appid:{int}, abstime:{int}, opuin:'{uin}', nickname:'{name}'
```
解析器逐条提取（按 `key:'非空十六进制'` 定位条目起点，向后取 abstime/opuin/nickname），容错跳过畸形条目。

- [ ] Step 1: 失败测试（含实证样本：多作者混合、空 key 跳过、JS 转义解码、appid!=311 过滤标记）
- [ ] Step 2: FAIL
- [ ] Step 3: 实现
- [ ] Step 4: PASS
- [ ] Step 5: Commit

### Task 2: QzoneClient API 封装重构

**Files:** Modify `catsitate_core/qzone/client.py`; Test `tests/test_qzone_client.py`

**改动：**
- 新增 `async def get_unified_timeline(self, *, count: int = 20) -> list[FeedDiscovery]`
  - 调 `_fetch_unified(count)` 取原始响应文本 → `parse_unified_timeline(text)`
  - 端点 `https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more`
  - 参数 `{uin: bot_uin, g_tk, format: json, begin: 0, count, update: 1, scope: 0, filter: all}`
  - Referer = bot 空间首页
- 新增 `async def _fetch_unified(self, *, count: int) -> str` — 原始文本（不走 callback 截取，直接返回）
- 既有方法签名**全部不变**（get_user_feeds/get_own_feed_comments/do_like/do_comment/do_reply）
- 类 docstring 重写为 API 分层说明

- [ ] Step 1: 失败测试（fake fetch 断言 URL/参数/Referer；返回 FeedDiscovery 列表）
- [ ] Step 2: FAIL → Step 3: 实现 → Step 4: PASS → Step 5: Commit

### Task 3: 浏览流重构（plugin.py `_qzone_poll_feeds`）

**Files:** Modify `plugin.py`; Test `tests/test_qzone_wiring.py`（追加行为测试）

**核心重写：**
```python
async def _qzone_poll_feeds(self) -> None:
    """浏览流:统一时间线发现→仅新动态充实(M3 架构,1+N 次API,N=新动态数)。"""
    # ① 发现层:1 次调用
    try:
        discoveries = await self.qzone_client.get_unified_timeline(count=20)
    except Exception:
        self.ctx.logger.exception("QQ空间统一时间线拉取失败,回退逐好友旧路径")
        await self._qzone_poll_feeds_legacy()
        return
    if not discoveries:
        return
    # ② 过滤:新 tid（不在 seen store 中）
    new_items = [d for d in discoveries if d.appid == 311
                 and self.qzone_seen.is_new_candidate(d.tid)]
    if not new_items:
        return
    # ③ 充实层:按 uin 分组,每组 1 次 get_user_feeds
    by_uin: dict[str, list[FeedDiscovery]] = {}
    for d in new_items:
        by_uin.setdefault(d.uin, []).append(d)
    added_total = 0
    for uin, discoveries_for_uin in by_uin.items():
        nickname = discoveries_for_uin[0].nickname
        try:
            feeds = await self.qzone_client.get_user_feeds(target_uin=uin, nickname=nickname, num=len(discoveries_for_uin) + 2)
        except Exception:
            self.ctx.logger.exception("QQ空间充实层拉取失败(uin=%s),该好友本轮跳过", uin)
            continue
        # 匹配发现层 tid → 只注入新动态
        discovered_tids = {d.tid for d in discoveries_for_uin}
        new_feeds = [f for f in feeds if f.tid in discovered_tids]
        for f in new_feeds:
            if self.qzone_seen.mark_queued(f.tid, abstime=f.abstime, author_uin=f.uin,
                                            summary=f.content[:60], author_nickname=f.nickname):
                self.qzone_injector.enqueue([f])
                added_total += 1
        await asyncio.sleep(2.0)  # 好友间间隔
    if added_total:
        self.ctx.logger.info("QQ空间新动态入队 %d 条(统一时间线发现 %d 条)", added_total, len(new_items))
    await self._qzone_pump()
```

**配套改动：**
- `SeenStore.is_new_candidate(tid) -> bool`：SELECT 查存在性（已有 mark_queued 是「登记并返回是否新」，is_new_candidate 是纯查不登记——避免发现层阶段误标 queued）
- `_qzone_poll_feeds_legacy()`：原逐好友逻辑保留为回退路径（发现层失败时调用，加告警）
- `_qzone_friend_list()` 保留（源A不需要了，但 legacy 路径仍用；`msg_react`/`poke` 的 `allowed_session` 等不依赖此）
- on_config_update 热重载不变

- [ ] Step 1: 行为测试（统一时间线→新 tid→充实→入队；发现失败→回退 legacy）
- [ ] Step 2: FAIL → Step 3: 实现 → Step 4: PASS+全量 → Step 5: Commit

### Task 4: 源B重构——搭发现层便车

**Files:** Modify `plugin.py` `_qzone_notify_scan`; Test `tests/test_qzone_wiring.py`

**核心重写（源B部分）：**
```python
    # 源B:自己在他人说说下的评论被回复——搭统一时间线便车
    # 只对「发现层显示有新活动 + bot 评论过该好友」的说说拉评论
    try:
        discoveries = await self.qzone_client.get_unified_timeline(count=20)
    except Exception:
        discoveries = []  # 发现层失败不阻断源A
    commented_friends = set(self.qzone_comment_seen.bot_commented_friends(days=30))
    # 交叉:发现层中 opuin ∈ 被评论好友 → 该好友有新活动
    active_commented_uins = {d.uin for d in discoveries if d.uin in commented_friends and d.uin != bot_uin}
    for friend_uin in active_commented_uins:
        if len(notifications) >= 3:
            break
        await asyncio.sleep(2.0)
        try:
            raw = await self.qzone_client.get_user_feeds_raw(target_uin=friend_uin, num=10)
        except Exception:
            continue
        for r in parse_feed_replies(raw, bot_uin=bot_uin):
            ...  # 现有楼中楼检测逻辑不变
```

**移除**：`QZONE_SOURCE_B_FRIEND_CAP = 10` 硬上限（不再逐好友轮询全量）；`bot_commented_friends()` 的 days 参数保留但不再用于截断。

- [ ] Step 1: 行为测试（发现层有活跃被评论好友→源B拉取；无→零 API 调用）
- [ ] Step 2: FAIL → Step 3: 实现 → Step 4: PASS → Step 5: Commit

### Task 5: 收尾——清理+文档+版本

**Files:** Modify spec/manual/CHANGELOG/_manifest; Test 全量

- spec §3.7 追加统一时间线设计段落（两层混合/源B搭便车/回退路径）
- manual §3.13 浏览流描述更新;§4.12 补 `poll_interval_minutes` 语义说明（发现层间隔）
- CHANGELOG v0.6.0
- _manifest 0.5.2→0.6.0
- 全量回归

- [ ] 文档四处+全量→Commit

## Self-Review
- 冲突排查：串行注入不变/意图路由不变/睡眠门不变/好感度事件不变/通知P1优先不变
- 发现层失败回退：legacy 逐好友路径保留（降级不静默，加告警）
- SeenStore 增 is_new_candidate 纯查方法：发现层用（不误标 queued），充实层 mark_queued 照旧
- 源A（自己说说评论）完全不变——仍走 get_own_feed_comments
- FeedDiscovery 与 FeedItem 的关系：Discovery 是轻量索引（发现层产物），FeedItem 是完整实体（充实层产物）
