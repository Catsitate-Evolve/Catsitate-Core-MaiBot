# QQ空间 发现层游标翻页改造（scope=2 + begintime）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复发现层的两个实证缺陷：① 统一时间线（feeds3_html_more）此前固定 `scope=0`，实机证明该取值实为「2 天小窗口」且本账号窗口内仅 bot 自己的动态——好友动态发现形同虚设；② 翻页用的 `begin` 条目偏移被服务端无视（count/begin 均不受控），离线积压回看从未真正生效。改为 `scope=2`（全好友动态流，7 天窗口）+ `begintime` 游标翻页（续页唯一必需参数），并修复解析层窗口边界丢条缺陷。

**Architecture:** 全部改动限于插件目录。发现层三段式结构不变（时间线发现 → seen 判重过滤 → 充实层按好友拉 msglist），仅时间线通道的取数协议更新：`_fetch_unified` 携带游标、`get_unified_timeline` 返回 (条目, 下一页游标)、轮询侧翻页循环由偏移改为游标推进。通知源B 的单页搭车调用适配新返回形态。

**Tech Stack:** Python 3.11+ / maibot-plugin-sdk / pytest（离线 stub）。

**Spec:** 本计划自包含（协议实证结论内嵌于各任务），无独立设计稿。

## Global Constraints

- 只改插件目录；简体中文；错误显式暴露（回退必须告警）；只用 SDK 声明 API。
- 协议结论以实机探针为准（2026-09-03，容器内经插件同款请求构造逐项验证）：
  - scope=2 = 全好友动态流（7 天窗口，首屏即含好友条目）；scope=0 = 小窗口流（实测 2 天、本账号仅自己）；scope=1 = 「与我相关」（赞事件，维持现状）。
  - 续页游标 = 顶层参数 `begintime`（epoch 秒），取上页响应 `data.main.begintime`（与 externparam 的 basetime 恒等）；refresh/pagenum/externparam/g_tk 均非必需。
  - 终止信号：空页（hasMoreFeeds 实测不可靠）；页大小不受 count 控制（观测 5/3/8/条）；游标链实测 4 页 22 条、深至约两个月。
  - 解析层：窗口边界条目可能缺 `appid` 但带同值 `appiconid`，不回退则整条丢失。
- 每任务完成即跑全量：`cd /home/hesitate-p/Catsitate/MaiBot-dev/plugins/catsitate_core_maibot && python3 -m pytest tests/ -q`。

---

### Task 1: client 统一时间线游标协议（已完成，2026-09-03）

**Files:** `catsitate_core/qzone/client.py`

**Interfaces:**
- `_fetch_unified(*, count, scope=0, begintime=None) -> str`：删除 `begin` 参数；携带 `begintime` 时作为续页游标。
- `QzoneClient.extract_timeline_cursor(text) -> str`：静态方法，取 `main.begintime`（回退 externparam 的 basetime），无游标返回空串。
- `get_unified_timeline(*, count=20, begintime=None, scope=2) -> tuple[list[FeedDiscovery], str]`：返回（条目列表, 下一页游标）；scope 默认 2。
- `_fetch_likes_raw`（源C）不受影响（scope=1，不带游标）。

**Evidence:** 实测游标链 4 页 22 条（首页 11 条含 5 条好友动态 → 游标 1785644405 → …→ 空页终止）；count=20/50/100/200 均返回同页。

- [x] 实现与单测（参数透传/游标提取/scope 默认值/首页不携带游标）

### Task 2: discovery 解析回退（已完成，2026-09-03）

**Files:** `catsitate_core/qzone/discovery.py`

- [x] `_APPID_RE` 未命中时回退 `_APPICONID_RE`（窗口边界条目缺 appid 但带同值 appiconid，实测一条真实说说被整条丢弃）

### Task 3: plugin 发现层游标翻页 + 源B 适配（已完成，2026-09-03）

**Files:** `plugin.py`（`_qzone_poll_feeds` 发现段；`_qzone_notify_scan` 源B）

**Interfaces:** 翻页循环四重终止——空页 / 本页无新 tid（seen 软终止，稳态恒 1 次调用）/ 游标耗尽 / `discovery_max_pages` 上限。源B 单页调用解包新返回形态。

- [x] 实现与 wiring 测试同步（全部发现层桩改游标协议，478 测试全绿）

### Task 4: 文档/CHANGELOG/版本

**Files:** `docs/plugin-manual.md`（浏览轮询节：scope=2 语义/游标翻页/删除 begin 表述/已知限制更新）、`CHANGELOG.md`、`_manifest.json`（v0.9.1）

- [ ] Step 1: 手册浏览轮询节重写协议描述（scope=2、begintime 游标、四重终止、页大小不受控），配置表 `discovery_count`/`discovery_max_pages` 描述同步（页大小语义改为「请求上限，服务端按窗口决定实际条数」）。
- [ ] Step 2: CHANGELOG v0.9.1 条目（发现层 scope 修正+游标翻页+解析回退）。
- [ ] Step 3: manifest 版本号 0.9.0 → 0.9.1。

### Task 5: 提交与部署

- [ ] Step 1: 插件仓库 commit（含计划文档）。
- [ ] Step 2: tar 同步到运行时目录 → `docker restart maim-bot-core` → 启动日志无告警。

### Task 6: 实机验收

前置：测试日程已铺 10:53-11:10 read_qzone 窗口（已过期则重铺短窗）；seen 表中两条好友说说（`ee3396c49d38…`/`ee3396c4d238…`）保持回退未读态。

- [ ] Step 1: 浏览轮触发后，日志出现「QQ空间新动态入队 N 条(统一时间线发现 …)」且注入消息含好友说说（此前 scope=0 下恒零产出）。
- [ ] Step 2: `mai_messages` 检查注入消息正文含「评论区(…条):」块、评论行带〔评论ID=…〕锚、楼中楼 `↳` 缩进、无 `@{uin:…}` 机器格式泄漏（评论内容 @ 解析为可读形态）。
- [ ] Step 3: registry 校验 comment_map 已按浏览注入填充（评论级锚）。
- [ ] Step 4: 若 planner 对注入动态调 qzone_reply：@ 目标=评论作者（comment_map 三级解析第二级）。

### Task 7: 测试环境清理

- [ ] Step 1: `poll_interval_minutes` 确认已回 15（生产值）；测试日程（10:53 短窗）替换为当日正常日程或等次日 LLM 生成。
- [ ] Step 2: 清理宿主 /tmp 探针残留（若余）。
