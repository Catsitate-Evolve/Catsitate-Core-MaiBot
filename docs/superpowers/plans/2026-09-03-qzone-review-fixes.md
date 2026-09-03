# QQ空间 三期第三次全量复审修复波 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复第三次全量复审(双代理,2026-09-03)的全部新发现:1 High + 8 Medium + 3 Low,均為低成本定点修复,无架构性变更。

**Architecture:** 全部改动限于插件目录。修复项彼此独立,按文件域分 6 个任务;每任务含回归测试,全量 484 基线不得回退。

**Tech Stack:** Python 3.11+ / pytest(离线 stub)。

**Spec:** 本计划自包含——每个任务的「要求」小节即规格,源自复审报告的逐条发现。

## Global Constraints

- 只改插件目录(`MaiBot-dev/plugins/catsitate_core_maibot/`);简体中文;注释写给下一个维护者。
- 错误显式暴露:修复不得引入静默兜底;所有新 except 必须告警日志。
- 每任务完成跑全量:`cd /home/hesitate-p/Catsitate/MaiBot-dev/plugins/catsitate_core_maibot && python3 -m pytest tests/ -q`(基线 484 全绿)。
- 测试先行:每任务先写失败测试再实现。
- 不动既定裁定:截断策略/不硬频控/不剥 emoji/全域工具/锚匹配通知优先等一律不变。

---

### Task 1 (High): _bot_echo_nickname 抛错点移入安全区

**Files:** `plugin.py`(qzone_post 回注段 ~1046-1065;_echo_pending_diary ~3399-3414;_qzone_seed_virtual_session ~1901)

**背景:** `_bot_echo_nickname` 按裁定直接抛 RuntimeError 不兜底,但三处调用点都在各自 try 之外:①qzone_post 在**发布成功后**构造回注时才取昵称——异常会让工具以失败收尾无成功回执,诱导模型重复发布;②_echo_pending_diary 的取昵称在 route try 外——异常时 pending 快照永不清空,且 sleep_tick 每 60s 先补注后判睡,异常会**瘫痪入睡判定**并每分钟刷异常;③种子自举同款(后果轻)。

**要求:**
1. qzone_post:把 `await self._bot_echo_nickname()` 挪到 `do_publish` **之前**(发布前失败无重复发布风险,异常正常上抛给工具层)。
2. _echo_pending_diary:消息构造(含取昵称)纳入既有 route try 内(或整体 try 前移),失败保留快照下轮重试,入睡链不受影响。
3. _qzone_seed_virtual_session:同款纳入 try。
4. 测试:①昵称读取抛错时 qzone_post **零发布调用**(发布前失败);②_echo_pending_diary 昵称抛错时快照保留、异常不外泄(被内层 try 捕获告警)。

### Task 2 (Medium×3): plugin.py 边界三连

**Files:** `plugin.py`(_normalize_ts ~3986;_daily_decay ~3036 附近;日记发布 ~3366)

**要求:**
1. **_normalize_ts**:staticmethod 的 except 分支引用 `self`(触发即 NameError)——去掉 `@staticmethod`(调用点 `self._normalize_ts` 不变);except 元组补 `OverflowError`。测试:坏时间戳(空串/非数值)返回原字符串不抛;超大 epoch 不上抛。
2. **_daily_decay 并发防护**:醒后 spawn 的 _daily_settle 与调度器 tick 的 daily_decay/daily_settle 可并发跑两次衰减(delta 双计,幂等键只同秒去重)——加实例级 `_decaying` 防重入标记(在飞则跳过本轮,try/finally 复位);类属性声明+on_load 实例级重置。测试:标记为 True 时调用零 LLM 零落库。
3. **日记发布接同轮自愈**:`do_publish` 包 `_qzone_auth_retry`(与 qzone_post 同款,裁定 #7 语义),auth_err 非空告警返回。测试:AuthError 一次后重试成功→发布成功;重取失败→告警跳过零发布。

### Task 3 (Medium): mark_seen 空串覆写 message_id 修复

**Files:** `catsitate_core/qzone/seen_store.py`(:74-80)、`plugin.py`(detail ~1247)

**要求:**
1. `mark_seen(tid, injected_at_iso, message_id=None)` 签名第三参改 `str | None = None`;None 时 SQL `message_id = COALESCE(?, message_id)` 保留旧值;空串语义保留(显式清除,现有调用不受影响——全仓检查其它 mark_seen 调用点,注入路径传真实 id 不受影响)。
2. detail 查看路径传 `None`(本意只是置 seen,不该抹 reply 段锚)。
3. 测试:注入落 id → mark_seen(None) → get_message_id 仍返回原 id;显式空串仍覆写。

### Task 4 (改道,2026-09-03 用户裁定 C 方案): 多图拼接合成 + 图片出口公共化

**Files:** `catsitate_core/qzone/imaging.py`(新)、`plugin.py`(三出口)、`catsitate_core/qzone/messages.py`(门控,已在 6923796 落地保留)、`tests/`

**背景(用户裁定)**: 原「≤3 图」截断删除;多图说说(≥2 图)拼接为**一张带序号角标的合成图**进管线(单图直发);单图细看不做懒取层——模型经 inspect_image 对拼图提问(序号角标使「图3是什么」可问),链路保持最简。①②③三个图片出口(浏览注入/view_friend_feeds/view_friend_feed_detail)提取公共代码复用,消除三份拷贝。

**要求:**
1. 新模块 `imaging.py` 纯函数 `compose_numbered_grid(images: list[tuple[int, bytes]], *, cell_px: int = 640) -> bytes`:输入 (原始序号, 图字节) 列表(下载失败的图不进列表),输出 JPEG 合成图——3 列网格、每格 letterbox 白底缩放、左上角圆形底数字角标(PIL 默认字体,纯数字);单图调用方自行直发(不进本函数)。
2. 公共出口助手(放 imaging.py 或 plugin.py,Downloader 经参数注入):统一「逐张下载(失败跳过+告警)→ 单图直返 / 多图合成 → to_thread 压缩预算(fit_images_to_rpc_budget)」;三个出口改为调用助手,仅保留各自打包差异(注入=消息图片段;工具=content_items+mime 探测)。
3. 锚文案:多图时列「图1-图N(拼接,hash=合成图 sha256 前 8)」(不再逐图列 hash);单图维持「图1(hash)」。
4. 删除三处 [:3] 截断(含 6923796 刚加的两处)——合成后恒单图,无 media 爆炸面;QQ 上限 9 图自然封顶。
5. build_feed_message 评论区门控 `if feed.comments or feed.comment_total:`(已落地,保留)。
6. 测试:合成函数(序号/白底/网格布局/失败序号跳格不重排);助手(下载失败跳过、单图直返、多图合成);三出口接入(注入单图片段+锚文案;工具单 content_item);门控用例已在。

### Task 5 (Medium): 源C 时间折算防护 + 调用隔离

**Files:** `catsitate_core/qzone/discovery.py`(_relative_time_to_epoch ~163)、`plugin.py`(源C 调用 ~2158)

**要求:**
1. `_relative_time_to_epoch` 两处 `base.replace(month=…, day=…)`(含跨年回退年份-1)包 `try/except ValueError` → 返回 0(与「create_time 缺失不编造时间」口径一致,调用侧 like_epoch=0 已容忍);测试:非闰年「2月29日」返回 0 不抛。
2. 源C 取数调用点包独立 `try/except Exception`(源B 同款纪律:增量来源不阻断源A/B 已得通知)——异常时 likes 按空+parsed_ok=False+告警。测试:源C 抛 RuntimeError 时源A 通知照常入队注入。

### Task 6 (Low×3): 小修批

**Files:** `plugin.py`(~1843/1849 intent 文案;~1755 registry 登记)、`catsitate_core/qzone/client.py`(~113 saved_at)

**要求:**
1. 发布触发 intent 文案两处删多余的右括号(`内容);` → `内容;`)。
2. cookie 快照 `saved_at` 由 `time.monotonic()` 改 `time.time()`(跨重启可读)。
3. 通知登记 registry 的 `owner_nickname`:notify 分支置空串(该值实为评论者/点赞者昵称,与 owner 语义错位致 qzone_like 回执张冠李戴;消费方已有 `or owner_uin` 兜底,registry 合并保留旧非空值)。
4. 测试:qzone_like 对通知登记(源A)的自己说说→回执昵称回退 owner_uin 不显示评论者昵称。

---

### Task 7 (已撤销,2026-09-03 根因裁定): 注入队列重复项防护

**撤销理由:** 重复注入经取证定性为联调外部直写生产库与插件写事务的竞态(操作事故),非插件缺陷——不加防御补丁。

**Files:** `catsitate_core/qzone/injector.py`、`plugin.py`(泵侧)、tests/

**背景:** 联调实证 ee3396c4d238 被注入两次(队列存在两份,第二份来自 1 分钟后的重新入队;seen 行中途缺位的根因待诊断日志观测)。

**要求:**
1. injector `_enqueue_into` 去重:目标队列、另一队列、awaiting(_awaiting.feed.tid)、_popped 中已存在的 tid 跳过(返回实入数不变语义);告警日志记跳过 tid。
2. 泵侧二道防线: `_qzone_inject_one` 对 source="feed" 的弹出项,注入前查 seen 行 state=="seen" 则丢弃+warning「重复弹出已注入项,丢弃」(source="notify" 不查——通知软回退语义依赖重注入)。
3. 「QQ空间新动态入队 N 条」日志追加 tid 前 12 位列表(逗号分隔,上限 5 个+省略号)。
4. 测试:①同 tid 二次 enqueue 跳过(P2/P1 各一例);②awaiting 中 tid 的 enqueue 跳过;③泵侧 seen 态弹出丢弃(P2)+notify 不受影响;④入队日志含 tid。
