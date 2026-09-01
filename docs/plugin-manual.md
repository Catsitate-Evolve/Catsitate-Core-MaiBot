# Catsitate Core MaiBot 插件 — 公测使用手册

- 插件 ID:`catsitate.core`
- 仓库:https://github.com/Catsitate-Evolve/Catsitate-Core-MaiBot(目录 `plugins/catsitate_core_maibot/`)
- 文档日期:2026-09-01(对应 v0.8.0,QQ空间 M3 表达——`qzone_post` 说说发布/睡前日记旁路生成+API 直发+醒来延迟回注/真实聊天见闻摘要叙事格式;叠加 v0.7.0 工具驱动架构(评论/楼中楼回复/点赞改为 `qzone_comment`/`qzone_reply`/`qzone_like` 工具直调,网关改 receive 只进不出)/v0.6.0 统一时间线/v0.5.x 互动与统一通知;见 §3.13)
- 适用对象:公测使用方、复审人员
- 依据:设计规格(`docs/superpowers/specs/2026-08-14-catsitate-core-maibot-design.md`、`docs/superpowers/specs/2026-08-15-phase2-design.md`,其中全局决策 #7/#8/#9 为最终裁定;`docs/superpowers/specs/2026-08-30-phase3-qzone-design.md` 为 QQ空间 M1 依据)、`docs/acceptance-checklist.md`(全部已验收行为)、当前代码。本文内容与代码不一致处,以代码为准。

---

## 1. 概述

### 1.1 插件定位

Catsitate 是部署在 MaiBot 上的 QQ 聊天机器人人设(伪三无猫耳少女,信息海意识体)。本插件是 Catsitate 的**核心人格行为插件**,负责在**不修改主程序**的前提下:

- 为 bot 提供"生活感":好感度(按人)、睡眠作息、每日日程、节日/天气/时间感知、主动发言与主动问候、QQ空间好友动态见闻与互动;
- 扩展行为能力:备忘录(工具+命令)、贴表情、主动戳一戳、图片重看、reply 上下文补传与可选哨兵层、QQ空间动态浏览与评论/点赞(见 §3.13);
- 优化请求结构使提示词缓存命中率尽量高(插件侧缓存纪律,见 §2.4)。

### 1.2 核心设计哲学

| 原则 | 含义与落地 |
|---|---|
| **拟人化** | 一切行为围绕"像活人一样"设计:按人积累好感、睡觉即一天的结束、睡前规划次日日程、醒来回顾、久不联系关系自然降温、城市/节日/天气注入生活感。 |
| **按人好感度** | 好感度的**唯一标识是 QQ 号(user_id)**,不按聊天流分账;跨流聚合判定、聚合结算、聚合衰减(全局决策 #7 最终裁定)。 |
| **睡眠即日程** | 睡眠是日程中的一个窗口(恰好 1 个),不是独立开关;睡眠期间**绝对静默**(拦截一切入站消息含命令、@ 不唤醒、提醒不执行);入睡任务(次日日程生成+日记生成发布)是睡眠期间唯一允许的旁路工作(不经消息链)。 |
| **主动发言交还主程序** | 插件在日程窗口到达时只做**指示**(`maisaka.proactive.trigger` 传 intent 上下文),是否说话、说什么、话术全部由主程序结合人设/记忆/上下文决定;插件不 send.text、不生成话术。 |
| **错误显式暴露** | 不实现任何"偷偷兜底":所有跳过、降级、钳制、失败都有日志痕迹;LLM 失败跳过本轮但记录日志;配置错误 on_load 报错拒绝加载;数据损坏告警并忽略。 |
| **无纯概率** | 摒弃单纯概率行为:发言/入睡/衰减/结算等决策交给 LLM 判定或确定性规则,随机数只允许出现在工程护栏(冷却、限频)。 |

### 1.3 版本与里程碑

- v1.0.0(2026-08-15):一期联调完成(注入/好感度/备忘录/贴表情/戳一戳/reply 补传/图片重看/时间感知/旁路记账)。
- v0.2.0(2026-08-15):二期(自然衰减/睡眠/日程/主动问候/备忘 remind_at)。
- v0.3.0(2026-08-16):**按人重构**(好感度以 QQ 号唯一标识、特别独占、主动问候统一、配置清理)。
- v0.3.1(2026-08-18):公测修复集(睡眠窗口语义对齐、跨午夜睡眠窗口保留、超时字段 None→0、RPC 帧超限修复)。
- v0.3.2(2026-08-18):旁路模板自动部署(插件加载时同步到主程序 `prompts/zh-CN/`,WebUI「提示词管理」可编辑)。
- v0.4.0(2026-08-30):三期 M1 QQ空间感知(日程浏览窗口内刷好友动态/`qzone-qq` 虚拟流注入/真实聊天见闻摘要,见 §3.13)。
- v0.5.0(2026-08-31):三期 M2 QQ空间互动(出站评论路由/`qzone_like` 点赞工具/好感度显式事件/memo 按人重构,见 §3.13)。
- v0.5.1(2026-08-31):三期 M2.1 QQ空间统一通知通道(评论轮询重构为高频双源检测+P1 通知插队+阅读顺序新→旧+工具双向隔离,见 §3.13)。
- v0.5.2(2026-08-31):QQ空间二轮深度审查修复(通知注入重试上限/事件同日去重/结算候选并集等,见 CHANGELOG)。
- v0.6.0(2026-08-31):三期 M3 QQ空间统一时间线架构(浏览流发现层+充实层重构/通知源B搭便车/API 量与好友数无关,见 §3.13)。
- v0.7.0(2026-09-01):QQ空间**工具驱动架构**(出站意图系统删除,`qzone_like`/`qzone_comment`/`qzone_reply` 三工具直调+`FeedContextRegistry` 目标解析+消息 ID 锚+receive 网关,见 §3.13)。
- v0.8.0(2026-09-01):三期 **M3 表达**(`qzone_post` 说说发布/睡前日记旁路生成+API 直发+醒来延迟回注/self 消息回注/真实聊天见闻摘要叙事格式,见 §3.13),即当前公测版本。

---

## 2. 架构

### 2.1 模块清单与职责

| 文件 | 职责(一句话) |
|---|---|
| `plugin.py` | 薄接线层:插件生命周期、8 个 Hook、10 个工具、1 个命令、11 个后台调度任务、全部 SDK 适配与日志(业务逻辑不在此)。 |
| `catsitate_core/config.py` | 配置模型(`PluginConfigBase` 嵌套 13 节,中文 label 供 WebUI 渲染)。 |
| `catsitate_core/storage.py` | 存储层:`SQLiteStore`(sqlite3 薄封装,WAL 模式)+ `JsonSnapshot`(轻量 JSON 快照,原子写)。 |
| `catsitate_core/inject.py` | 注入框架唯一出口:注入块组装、`BLOCK_ORDER` 固定排序、字节级版本化缓存复用。 |
| `catsitate_core/favorability.py` | 好感度引擎:`BatchEngine`(按人批次账本/触发判定/apply_delta/特别独占钳制)+ `SettleExecutor`(素材构造→LLM 判定→落库/顺延)+ 好感度块渲染。 |
| `catsitate_core/decay.py` | 自然衰减引擎:互动时间判定(私聊全收/群聊 @/quote)+ LLM 衰减判定 + apply_delta。 |
| `catsitate_core/sleep.py` | 睡眠状态机(全局唯一,JSON 持久化)+ clamp 醒来时刻 + 晚安短句过滤 + 入睡判定解析。 |
| `catsitate_core/schedule.py` | 日程引擎:数据模型/校验/钳制修复/默认作息模板/生成器/窗口判定/update_schedule 工具全部修改语义(压缩/上限/睡眠窗口保护)。 |
| `catsitate_core/memo.py` | 备忘录:读写(单条 TTL/remind_at 校验)、到期筛选 `due_on`、过期清理、注入取数。 |
| `catsitate_core/msg_react.py` | 贴表情引擎:每流冷却(JSON 快照)+ 选表情 prompt 组装;表情表在 `qq_emoji.py`。 |
| `catsitate_core/qq_emoji.py` | 内置 30 项精选 QQ 表情表(id→描述,替代可配置白名单的联调裁定)。 |
| `catsitate_core/poke.py` | 主动戳引擎:仅每用户冷却前置校验(好感度门槛已取消)。 |
| `catsitate_core/reply_guard.py` | reply 补传规则层(三条件判定、工具结果合并摘要)+ 哨兵 prompt 组装与解析。 |
| `catsitate_core/image_relook.py` | 图片重看:图片段定位(按 message_id 或倒数 index)+ MIME 魔数嗅探 + VLM prompt 组装。 |
| `catsitate_core/time_aware.py` | 时间感知:节日数据回退链(在线→库→内置)、lunar-python 农历节日/节气实算、天气码中文映射、环境块文本组装。 |
| `catsitate_core/llm_provider.py` | 旁路 LLM 统一出口:11 个内置 prompt 模板、主程序 prompt 管理覆盖加载、稳定段前置组装、模板版本化缓存键。 |
| `catsitate_core/qzone/` | QQ空间模块包(感知+互动+统一通知,工具驱动 v0.7):协议纯函数(g_tk 签名/callback 解析/说说与评论解析)、HTTP 客户端与 cookie 管理、动态去重存储(`qzone_feeds`)、评论去重与好感度事件存储(`qzone_comments`/`qzone_fav_events`)、注入消息构造(带工具 ID 锚)、串行注入状态机、注入上下文登记表(`registry.py`,工具目标解析)、表达生成层(`expression.py`,评论/回复/说说正文两段式人设生成)与场景纯函数(网关注册/启动自检/拉取与统一通知调度/发布 API `do_publish`/四工具接线在 `plugin.py`,见 §3.13)。 |
| `catsitate_core/services/scheduler.py` | 后台 asyncio 任务引擎:60s tick,任务异常隔离(记录日志不中断主循环)。 |

### 2.2 消息数据流概览

```
入站消息 ── chat.receive.before_process(BLOCKING EARLY)──> sleep_gate:睡眠拦截(abort+缓冲)/唤醒态记活动时间
        ── chat.receive.after_process(OBSERVE)──────────> fav_count:好感度计数+提前结算触发(睡眠期跳过)
主链路 planner ── maisaka.planner.before_request(BLOCKING LATE)──> inject:注入块前插 system 之后、历史之前
              ── maisaka.planner.after_response(BLOCKING LATE)──> reply_backfill:reply 上下文补传
replyer 出站 ── maisaka.replyer.after_response(BLOCKING LATE)──> goodnight_check(晚安入睡判定)
             ── maisaka.replyer.after_response(BLOCKING LATE)──> sentinel_check(LLM 哨兵,默认关)
发送链路(proactive)── maisaka.proactive.trigger 由调度器在日程窗口调用,表达权交主程序
QQ空间虚拟流(qzone-qq)── 网关注入(route_message;receive 只进不出——动作经 qzone_* 工具发出)+ planner/replyer 两侧场景手术与工具白名单(§3.13)
                        ── planner.after_response(OBSERVE)──> qzone 轮完成信号:释放注入泵推进下一条动态
后台调度器(60s tick)── 天气/节日/备忘清理/日终结算/衰减/睡眠/日程窗口/提醒兜底/QQ空间拉取/统一通知(见 §6.2)
```

### 2.3 旁路 LLM prompt 纪律(§4.10)

- **模型路由**:所有旁路请求统一经 `ctx.call_capability("llm.generate", prompt=messages, model=<task 名>)`;`model` 填**主程序 `model_task_config` 的 task 名**(节名),填模型标识会报「未找到名为 … 的模型配置」;留空=主程序默认(取首个可用 task,不可控,不推荐)。
- **统一结构**:`[任务指令+输出格式(system,固定模板+版本化)] [稳定上下文(5 级规则/白名单/人设背景,配置数据)] [变量素材(时间正序)]`——稳定段在前、变量段在后。
- **模板加载顺序**:主程序数据目录 `data/custom_prompts/zh-CN/catsitate_<id>.prompt`(WebUI「提示词管理」编辑产物)→ 主程序 `prompts/zh-CN/catsitate_<id>.prompt`(内置层)→ 插件内置默认(`llm_provider.SIDE_TEMPLATES`)。模板缺失时告警一次并回退内置,部署后自动恢复;模板内容变化即缓存键失效。
- **WebUI 管理前提(自动部署)**:主程序「提示词管理」只扫描主程序 `prompts/` 与 `data/custom_prompts/` 目录,**不会自动扫描插件的 `prompt_templates/`**——插件加载时(`on_load`)自动把 `prompt_templates/catsitate_*.prompt`(11 个)同步到主程序 `prompts/zh-CN/`(`prompt_deploy.sync_prompt_templates`:内容一致跳过、变更覆盖;主程序 `load_prompts()` 在插件启动后调用,**同次启动即生效,无需重启**,见 §8.1 第 4 步)。生效后:主程序加载为内置层 → WebUI 可编辑 → 编辑产物写 `data/custom_prompts/zh-CN/` → 插件旁路调用优先读取(闭环)。插件不在 `plugins/` 下或主程序 `prompts/zh-CN/` 缺失时跳过并告警,插件回退内置默认。插件 `prompt_templates/` 目录保留作源模板。
- **11 个旁路模板**:`catsitate_favorability`、`catsitate_msg_react`、`catsitate_sentinel`、`catsitate_image_relook`、`catsitate_decay`、`catsitate_schedule_generate`、`catsitate_sleep_confirm`、`catsitate_sleep_review`、`catsitate_qzone_scene`、`catsitate_qzone_diary`、`catsitate_qzone_expression`(与 `prompt_templates/` 下 11 个文件一一对应;前 8 个含 `{{delta_max}}`/`{{decay_max}}` 占位符,`catsitate_qzone_scene` 为空间虚拟流场景文案——场景替换运行时读取,WebUI 改完即生效;`catsitate_qzone_diary` 为睡前日记生成模板;`catsitate_qzone_expression` 为空间动作表达生成模板——评论/回复/说说正文共用,见 §3.13.1)。
- **记账**:每次旁路调用写入 `llm_usage` 表(day/module/calls/tokens 按模块分列)。

### 2.4 主链路注入纪律(§4.1)

- 注入点:**system 之后、历史之前**前插(目标顺序:`[system][环境块][日程块][空间见闻块][备忘块][好感度块][历史][主程序动态注入][时间][tail][注意事项]`),绝不改动 system prompt 与历史。
- 注入块按波动频率分层排序(`BLOCK_ORDER = level_rule → environment → schedule → qzone → memo → favorability`,等级规则块在实现上并入好感度块首行;qzone 块为三期新增,插日程块之后);任一后部块变化不影响前部块缓存。
- 空块跳过;同 `(module, content_key, text)` 内容未变时字节级复用上一轮渲染结果;每模块每轮仅允许一块(重复即抛错,显式暴露)。
- 长度在源头控制(注入管线不截断):备忘 ≤80 字符/合计 ≤5 条,注记 ≤40 字符,环境块天然短小,规则文本由配置自控。
- 任一注入源出错仅记录日志并跳过该小节,不阻塞主链路。

---

## 3. 功能详解

## 3.1 好感度(按人,`favorability.py`)

### 3.1.1 数据模型与 5 级制

- **按人单行**:`favorability` 表主键 `user_id`(QQ 号),一个用户全局一行,跨流聚合(全局决策 #7)。
- **分数→等级**:`0-9 陌生 / 10-29 熟悉 / 30-59 亲近 / 60-99 挚友 / ≥100 特别`(5 级)。
- `batch_counter` 保留 `(user_id, stream_id)` 行**仅作活跃度记录**(count、last_bump),不再承担结算窗口语义;结算窗口走人级 `favorability.window_start`。
- 开发期裁定:检测旧形状(含 stream_id 列 / window_start 死列)直接重建,不做数据迁移;重建前请留意数据丢失。

### 3.1.2 计数与批次

- `chat.receive.after_process`(`catsitate_fav_count`)记录消息:**通知类消息(`is_notify`,如戳一戳)不计数**;睡眠期不计数(绝对静默);无 user_id/stream_id 跳过。
- 每次计数 = `batch_counter` 按 (user, stream) bump;触发判定按人取跨流 SUM。

### 3.1.3 结算(纯计数触发,消息永不丢弃)

| 类型 | 触发条件 | 说明 |
|---|---|---|
| **early 提前结算** | 该人跨流总计数 ≥ `early_settle_threshold`(默认 20),且当日 early 次数 < `daily_max_early_settle`(默认 3) | 结算后该人所有流计数清零并开新批次;每用户每日提前结算 ≤3 次 |
| **daily 日终结算** | `daily_settle` 调度(默认每 24h)扫描**当日有消息(batch 活跃)∪ 当日有空间事件(qzone_fav_events)且未日终结算**的用户(深度审查 C-N1 并集:纯空间互动好友无 batch 行也进兜底;bot 自身排除) | 不计提前结算上限;若素材中该用户本人消息数 < `daily_settle_min`(默认 3)→ **顺延**(不结算、不清零,消息保留继续累积,待再次活跃进入后续日终检查) |

- 每用户每日判定次数硬上限 = 提前 ≤3 + 日终 ≤1 = **≤4 次**;日终兜底保证最后一次提前结算之后的批次也被判定。
- 结算并发防护:`_settling` 集合,同一用户任一结算已在飞即跳过(防 delta 双计),日志「好感度结算[%s] %s 已在结算中,跳过本轮」。
- **LLM 判定**:prompt 结构 = 判定指令(system)+ 人设/行为风格(主程序 `personality.*`,非硬编码)+ 5 级规则表(配置)为稳定段,批次素材为变量尾;输出 `{"delta": 整数(-delta_max~+delta_max), "note": "一句话注记"}`;delta 按 `delta_max`(默认 5)钳制;解析失败/LLM 失败跳过本轮并记日志。
- **落库**:`apply_delta` 累加分数、重算等级、注记强制截断至 `note_max_chars`(默认 40)字符、写 `favorability_log`(judge_id=`early-{时间}` / `daily-{时间}`,幂等防重)。素材为空时**不调 LLM 不落库**(返回 failed)。
- 日志关键词:
  - `好感度结算 {user}:early delta={n}` / `好感度结算 {user}:daily delta={n}`
  - `好感度结算失败 {user}:{kind} {原因}`(含「素材为空,跳过结算」「用户消息不足 3 条,顺延」「LLM 调用异常…」「判定 JSON 解析失败」等)
  - `结算取数: 共 {N} 条,其中 bot 发言 {M} 条(bot_user_id={id})`(需配置 bot_user_id)

### 3.1.4 结算素材构造(私聊/群聊差异化,按人跨流聚合)

- 取 `window_start`(上次结算时间)之后的消息,时间正序;以该人用户消息为锚取最近 `material_max_messages`(默认 30)条,每条紧邻上下文前后各 1 条;bot 消息仅随附目标用户发过言的流(私聊全收,群聊仅 **@ 该人或 quote 解析出原发送者为该人** 的 bot 消息)。
- 单条素材超过 `material_message_max_chars`(默认 200)在**单条尾部**截断加「…」;每条素材格式 `[user_id](私聊/群聊·user/bot) 文本`。
- **空间互动事件并入(M2)**:当日该人的QQ空间评论/点赞显式事件(§3.13)以 `[空间互动]` 前缀追加进素材(至多 5 条,LLM 计权),事件按原始时刻过滤防同日多次结算重判。
- 关键前提:`bot_user_id` 必须配置为 bot 自身账号 id(实机 3545773341),否则 bot 发言全部被当作用户素材、bot 识别与 quote 归属全部失效。

### 3.1.5 「特别」等级独占(全局决策 #8)

- 全表任意时刻**最多 1 人**处于「特别」(≥100 分)。
- 他人分数变动试图升入时,若特别之位已被占据 → 钳制在 **99 分(挚友)** 并显式日志「结算升特别被独占钳制(user=…)」;`apply_delta` 返回状态 `clamped_exclusive`。
- 独占者本人继续加分不受限(`is_exclusive_holder` 排除自己);独占者因衰减/结算掉出(score<100)后空位释放,他人**下一次分数变动**时方可升入(判定在 `apply_delta` 统一入口,无需额外扫描)。
- 注:`favorability_log.delta` 记录判定意图,钳制时与实际落库分数变化可能有差。

### 3.1.6 注入块

- 好感度块(`[好感度]`):私聊=对端用户,群聊=当前消息发送者(最近非 bot 消息发送者,取 3 条内解析);`include_rule=True` 时块首行注入**当前等级的对应单条规则**(联调决定:5 级全量注入改为按等级单条,缓存友好)。
- 文本示例:`[好感度] 规则「熟悉」:认识一段时间,可自然闲聊。\n[好感度] 3341299096:等级「熟悉」(累计 42),注记:最近主动关心过你。`
- 无结算记录的用户显示默认等级「陌生」、0 分、无注记(内容稳定统一,新说话人不引入波动);等级/注记变化(结算)才更新该块。

## 3.2 好感度自然衰减(`decay.py`)

- **互动定义**(按流类型区分,群聊防误判;按人聚合后跨流取最近一次):
  - 私聊:流内任意 bot 消息时间(流内只有 bot 与用户两人,任何 bot 消息即回应);
  - 群聊:流内最近一条 **@ 该用户或 quote 了该用户** 的 bot 消息时间(bot 回应 A 不重置 B 的计时;reply 段实机为纯消息 id,经主机能力 `message.get_by_id`(message_id=reply 段,chat_id=当前流)解析原发送者后与目标用户比对;解析失败按未 quote 命中并显式告警,每轮至多一条);
  - 从未被 bot 直接回应:以该人 `favorability.judged_at`(上次结算时间)为计时基准。
- **计时基准**(判定后重置):`max(各活跃流内最近 bot 直接互动时间, 最近一次空间互动事件时间(M2,§3.13), 最近一次 decay 判定时间)`——衰减判定本身即一次「想起」,7 天内不重复衰减(每人每日最多一次的天然去重)。
- **触发**:`daily_decay` 调度(24h,与日终结算同 tick **先衰减后结算**);扫描 `favorability` 表 **score>0 的人**(单行 per user),距基准 > `decay_after_days`(默认 7 天)进入衰减判定;睡眠期跳过。
- **流消亡处理**:`batch_counter` 中流已不在 `chat.get_all_streams` 结果 → 显式跳过该流并告警「衰减候选流不存在(user=…,stream=…),跳过该流」;单流取消息失败只跳过该流,不中止整轮。
- **LLM 判定**:prompt 稳定段 = 人设 + 行为风格 + 上次等级/分数/注记 + 未互动天数;输出 `{"delta": 整数(-decay_max~0), "note": "新注记(≤40 字)"}`;delta=0 表示关系稳定不减;delta>0 直接拒绝(衰减不可加分);结果按 `decay_max`(默认 3)钳制后 `apply_delta` 落库(judge_id=`decay-{时间}-{user}`,同秒多用户判重)。
- 日志关键词:`好感度衰减 {user}:delta={n}`、`衰减判定 LLM 失败(user=…)`、`衰减扫描异常,本轮跳过`、`quote 发送者解析失败(stream=…)`、`quote 发送者解析: 成功 {n}/{m}(stream=…)`。

## 3.3 睡眠(`sleep.py` + plugin 接线)

### 3.3.1 状态机

- 全局唯一状态 `awake / sleep`,持久化 `sleep_state.json`(state、sleep_at、wake_at);重启恢复:睡眠中则继续睡至醒来时刻(醒来时刻按 clamp 公式以持久化的入睡时刻重算,**不依赖日程文件**);日程缺失不影响睡眠状态。
- `is_sleeping()` = 状态 sleep 且 now < wake_at。

### 3.3.2 入睡通道(睡眠窗口 = 可入睡时间)

| 通道 | 条件 | 细节 |
|---|---|---|
| **① 晚安判定入睡** | bot 出站晚安短句 + **处于睡眠窗口内** + AI 判定 `SLEEP` | 判定**与静默开关无关**;睡眠窗口外不判定不入睡(无提前入睡通路,不侵占其它日程);短句须过 `is_goodnight_utterance` 过滤(≤12 字、无 @、无「」、不含「你好/再见」、逗号仅允许「大家/各位」群体收尾、含「睡/晚安/安眠/就寝」关键词);再经旁路 LLM(`sleep_confirm` 模板,**模型固定 memory,不可配**)三值判定 `SLEEP / NOT_SLEEP / UNSURE`,`SLEEP` 才入睡——防诱导,只接受短句、自我入睡 |
| **② 静默关:窗口起点直接入睡** | 睡眠窗口起点已到仍未睡,`silent_sleep_enabled=false` | `_sleep_tick` 检查 `now >= 睡眠窗口.start` → 直接入睡,日志「睡眠窗口起点已到(静默睡眠关闭),直接入睡」 |
| **③ 静默开:安静满 N 分钟入睡**(默认开) | 睡眠窗口起点后,无任何入站/出站活动满 `silent_sleep_minutes`(默认 60)分钟 | 不调 LLM,日志「静默入睡:安静 60 分钟」;计时基准 = `max(窗口起点, 最后活动时刻)`(`_last_activity_ts` 由入站消息(非睡眠时)与出站回复刷新,无活动记录从窗口起点起算) |

- 入睡 = `_enter_sleep()`:幂等(已睡直接返回,防交错二次生成);计划醒来时刻 = 日程睡眠窗口的 end(无日程则 now+8h);醒来时刻 `clamp(计划醒来, 入睡+min_sleep_minutes, 入睡+max_sleep_minutes)`(默认 240/660)——正常等于计划醒来,**提前入睡不改变醒来时间**(拟人),仅实际时长越界时以约束为准(最短顺延/最长提前醒);**无唤醒浮动**。日志「已入睡:醒来 %s」。
- 入睡成功瞬间触发**入睡任务**(§3.4/§3.13:次日日程生成 + 日记生成发布,旁路 LLM 与发布 API 均不经消息链)。
- **窗口终点未入睡**(静默开且一直有活动):**不入睡**,但补执行入睡任务——生成次日日程与日记(每窗口一次,入睡过的窗口不重复,`_sleep_window_settled` 标记),日志「睡眠窗口已过未入睡:补执行入睡任务(不入睡)」;跨午夜时旧日程睡眠窗口仍在进行则保留旧日程(不删除/不换模板)直至窗口结束。

### 3.3.3 睡眠期间(绝对静默)

- `chat.receive.before_process`(BLOCKING EARLY)拦截**一切入站消息含命令**:消息记入睡眠回顾缓冲(`_sleep_review_buffer`,持久化 `sleep_review_buffer.json` 防重启丢失)后 `abort`;**@ 不唤醒、提醒不执行、不计数、不结算、不衰减、环境不刷新、日程窗口不触发**——任何其它 hook/调度任务在睡眠期一律直接返回。**唯一例外是入睡任务**(次日日程生成+日记生成发布):两者走旁路 LLM 与发布 API,不经消息链,睡眠期可执行。
- 醒来:`_sleep_tick` 检测 now ≥ wake_at → 「自然醒来」→ `_wake_up()`(状态置 awake + 可选睡醒回顾 + 醒来补跑当日结算(内部先衰减后结算))+ 补注昨晚日记(以 self 消息回注虚拟流,见 §3.13;醒态 tick 兜底重试)。

### 3.3.4 睡醒回顾(默认开)

- 醒来时对睡眠期被拦截消息生成**单份聚合报告文件**:`data/plugins/catsitate.core/sleep_review/reports/sleep_review_{YYYYMMDD_HHMMSS}.md`,按流分组,每流 LLM 摘要(≤100 字要求,落盘截断 200 字;失败记「摘要生成失败」);报告末尾**静态附列睡眠期到期的备忘提醒**(不占 LLM 额度,延续备忘不丢失原则);不补发历史回复、不注入对话上下文。
- 日志:「睡醒回顾已生成: {路径}」「回顾摘要失败(流 {id})」。
- 今日回顾材料:次日日程生成的「今天执行情况回顾」取最新一篇回顾报告前 200 字。

### 3.3.5 配置

`sleep` 节:`enabled`(默认 true)、`min_sleep_minutes`(240)、`max_sleep_minutes`(660)、`silent_sleep_enabled`(true)、`silent_sleep_minutes`(60)、`review_enabled`(true)、`review_llm_model`(memory)、`review_llm_timeout_ms`(0=主程序默认)。**无窗口时间字段**——时间在日程里。

## 3.4 日程(`schedule.py`)

### 3.4.1 生成时机(入睡确认)

- **任何入睡状态切换成功的瞬间**(晚安判定/静默关到点入睡/静默入睡)→ 生成**次日**日程(入睡任务的日程侧,旁路 LLM 不经消息链)。**例外补生成**:睡眠窗口终点未入睡 → 不入睡但补执行入睡任务(每窗口一次,见 §3.3.2)——两条生成路径。
- 首日无有效日程(启动后):`_schedule_tick` 用**内置默认作息模板**撑场(不生成当天日程,避免"已过大半天"浪费):`23:00-07:30 睡觉 / 09:00-12:00 发呆 / 15:00-18:00 随便做点什么 / 22:00-23:00 洗漱准备睡(greeting)`。
- 入睡生成失败:沿用默认作息模板撑过次日,**醒来不补生成当天**,下次入睡确认时正常生成(告警「次日日程生成:…(模板兜底)」)。

### 3.4.2 生成内容与校验

- 次日日程 = **恰好 1 个睡眠窗口 + 1~8 个活动窗口**(时间段+活动描述+是否计划发言+发言主题+kind 标注 `greeting`=问候类/`daily`=日常),窗口按活动排列、**允许空白时间**(空白=自由时间)、不重叠;默认作息 23:00/7:30 为软基准(模板提示,LLM 可结合当天活动调整)。
- **生成 prompt 输入**:人设+行为风格(主程序 `personality.*`,非硬编码)+ 今天执行情况回顾(最新睡醒回顾前 200 字)+ 明天天气/节日(weather_snapshot 快照)+ 重要用户好感度概况(全表按等级降序)+ **日程对应日到期的备忘提醒(remind_at)** + 睡眠约束(min/max)+ 目标日。
- **校验规则**(与工具修改共用):恰好 1 睡眠窗口、活动 1~8、窗口不重叠、睡眠时长在 [min,max]、kind ∈ {greeting, daily}、时间精确到分钟(带秒容忍解析后归一化)、`qzone` 标记仅 daily 窗口合法(非 daily 窗口的 qzone 标记校验拒绝,钳制修复时清除,见 §3.13)。
- **失败兜底链**:LLM 失败 → 默认模板+告警;JSON 解析失败/校验失败 → 重生成(`max_regenerate` 次,默认 1)→ 确定性钳制修复(`fix_schedule`:缺睡眠窗口插模板睡眠段、多睡眠窗口只留第一个、活动裁到 8、睡眠时长钳边界、重叠顺延)→ 仍无效 → 默认模板+告警。
- 日志:「次日日程已生成:{JSON 前 200 字}」「次日日程生成:{err}(模板兜底)」「次日日程生成异常,使用默认作息模板」。
- 重启恢复:`schedule.json` 的 date == 今天时恢复日程/编辑历史/生成标记;过期文件删除并告警;损坏/结构非法告警忽略。

### 3.4.3 注入(日程块)

- 独立注入块,插在环境块之后、备忘块之前(`BLOCK_ORDER` 第 3 位),窗口切换才变化(半天级稳定)。
- 内容 = 当前活动 + 下一活动预览 + 该窗口到期的备忘提醒:`[日程] 午后:发呆看雨(至16:00);接下来:买菜;备忘:周四交作业`(空白时间显示「自由时间」;窗口已触发过附加「(该窗口已过)」;当天到期备忘取当前说话人/当前流相关,最多 3 条)。
- 注:日程块构造要求 `schedule.enabled and time_aware.enabled and memo.enabled` 同时开启。

### 3.4.4 update_schedule 工具

- 入口 `@Tool("update_schedule")`(visible),planner 可自主调用;**无频率上限**(联调决定)。
- 操作:`view`(每行带序号的全天概览)/ `move`(窗口挪到新时段,保留属性)/ `add`(新增活动)/ `delete`(删除活动);建议流程 view→改→view 确认。
- **约束与语义**:
  - 活动窗口 1~8 上限,超限拒绝并返回固定拟人化文案:`今天的日程已经排得满满当当了,再排下去会累坏的,明天再安排吧。`;
  - **睡眠窗口不可删**(「睡眠窗口不可删除」)、不可新增睡眠(「新增仅支持活动窗口」);睡眠窗口 update 时 kind 强制纠正为 sleep(联调:LLM 常误传 daily);
  - 时间修改受 [min_sleep, max_sleep] 校验;时间格式 `HH:MM`(如 11:45),end≤start 自动跨午夜 +1 天;
  - **重叠压缩语义**(`compress_with_anchor`):新操作窗口为锚点保持完整;锚点前窗口 end 提前到锚点 start(尾部压缩)、锚点后窗口 start 推迟到前一窗 end(头部压缩,链式);与锚点重叠的睡眠窗口**入睡推迟到锚点 end、醒来时间不变**;任一窗口被压至 start≥end 即拒绝(不自动删除),返回压缩明细警告(「日程已更新。注意:与已有安排重叠,已自动调整:…」);
  - add 活动描述截断 40 字符。
- 修改落盘(`schedule.json`)、立即反映到注入块,并记录**修改历史**(time/action/before/after,存 `schedule.json.edit_history`)。

## 3.5 主动发言(2.1 窗口触发 + 主动问候)

### 3.5.1 窗口触发(表达权交主程序)

- `_schedule_tick`(60s)对**当前所处非睡眠窗口**触发一次(去重标记 `day|start`,内存态,跨天清理):
  1. 睡眠期跳过(绝对静默);
  2. 无日程或日期不符 → 当日默认模板撑场(`_schedule_generated=False`);
  3. 同窗口已触发 → 跳过;当日 `_speak_counts` ≥ `daily_speak_limit`(默认 5)→ 跳过;
- **daily 窗口流程**:
  1. **门槛过滤**:该流说话人好感度(按人判定)≥ `speak_threshold_level`(默认「熟悉」);等级不足不 trigger;
  2. **候选流收集**:活跃流 = `batch_counter.last_bump` 近 24 小时内有消息的流(确定性收集),说话人解析失败(空 user_id)跳过;
  3. **排序取前 n**:按(说话人好感度等级, 最近活动)降序,取前 `speak_max_streams_per_window`(默认 1)个;
  4. **trigger**:`maisaka.proactive.trigger(stream_id, intent=指示prompt, reason=日程窗口活动)`——intent 含日程窗口活动/计划发言/主题/全天概览/目标流等级注记,要求 bot 结合日程与好感度**自然决定是否主动发言**;**是否说话、说什么全部由主程序决定**;插件不 send.text、不生成话术。
- **执行后状态**:窗口触发即更新触发标记(无论主程序是否实际发言),防同窗口重复触发;**每次 trigger 计 1**(主程序沉默也计,触发即消耗)。日志:`主动触发[{day}] -> {stream}:{活动}`、`主动任务触发失败(stream=…)`。

### 3.5.2 主动问候(仅特别者,全局决策 #9)

- 仅在 **kind=greeting 窗口**触发,**窗口起点即触发**;只走私聊通道,强制要求私聊流存在。
- **触发条件**(全部满足):(1) 存在「特别」等级的人(全表唯一,查询 level≥4 LIMIT 1);(2) 该人存在私聊流(非群流且流 user_id == 该人)。
- 满足则对其私聊流 `proactive.trigger`(问候语境 intent:日程活动 + 特别级 + 注记,话术由主程序生成);**无每日一次限制**——每个 greeting 窗口都问候(受 `daily_speak_limit` 全局上限约束);不满足条件不触发、**不群内替代**。计数与日志:`主动问候触发[{day}] -> {user}`、`主动问候跳过:特别者({user})无私聊流`、`主动问候触发失败(user=…)`。
- 睡眠期跳过;窗口触发标记在 greeting 分支同样置位(同窗口不重复)。

## 3.6 备忘录与提醒(`memo.py`)

### 3.6.1 按人语义(M2 重构,spec §3.10)

- **条目维度 = 主 QQ + 附带 QQ 列表**(≤5 个,超限截断并提示):备忘不再以聊天流为唯一归属,任一牵连 QQ 命中当前对话对象即可见——真实私聊/群聊/虚拟流**跨流可见**(与好感度按人对齐)。
- **写入**:`memo_write` 增 `related_user_ids` 参数(逗号分隔 QQ 号,兼容中文逗号/顿号);主 QQ 缺省取当前说话人——私聊用官方 kwargs `user_id`(可靠),群聊官方 kwargs 常为空,经 fav_count 维护的 `stream_id → 最近发言者` 映射解析(`get_recent` 回溯作兜底)。
- **流维度保留**:`stream_id` 仍是元数据列与命中条件之一(stream 精确命中 OR 主 QQ/附带 QQ 命中任一即返回),虚拟流上写的备忘在刷到该人动态时可见;读取与注入取数两维度 OR 语义不变(各取 3 条、合计 ≤`inject_max`)。
- 旧库 memo 表自动补 `extra_user_ids` 列(默认空 JSON 数组),无数据迁移损失。

### 3.6.2 读写

- 双通道:① `@Tool("memo_write"/"memo_read")`(planner 自主);② `@Command("/记一下", pattern=^/记一下\s+…, aliases=["/备忘"])`(用户显式,默认 TTL,超长直接提示精简)。
- 写入参数:内容(≤`entry_max_chars` 80 字符,超长返回错误让 LLM 重写/提示用户)、主 QQ/附带 QQ 列表(`related_user_ids`,≤5,按人跨流可见,见 §3.6.1)、关联流、`ttl_hours`(单条有效期,缺省 `default_ttl_hours` 24h,上限 `max_ttl_hours` 168h)、`remind_at`(可选 ISO 绝对时间如 `2026-08-16T19:00`,格式非法显式拒绝,防静默丢提醒)。
- 读取:当前流相关 + 当前说话人相关(说话人含主 QQ 与附带 QQ,OR 语义,两维度各取 3 条、合计 ≤`inject_max` 5 条),返回剩余有效时间;双空维度返回空。
- 清理:`memo_cleanup` 调度每小时删除过期项,日志「备忘清理:{n} 条过期」。

### 3.6.3 提醒两级(remind_at 联动)

1. **日程收录**:生成日程时,日程对应日 remind_at 到期的备忘并入生成 prompt 的「到期备忘」段;执行窗口时,当日到期且属于当前说话人/当前流的备忘附在日程块注入中(最多 3 条)。
2. **独立兜底**:无可用生成日程时(默认模板撑场当天、或日程缺失),`remind_fallback` 调度(300s)在 remind_at 到点时经 `maisaka.context.append` 注入**备忘归属流**上下文(`[备忘提醒] {content}`;不主动发言);去重持久化 `remind_fired.json`(key=`remind:{id}`,防重启后旧备忘重复注入,跨天清理);注入失败不标记、留重试机会。日志:「备忘提醒兜底注入(stream={id}):{content}」。

**睡眠期间两级都不执行**(绝对静默),醒后不补执行过期提醒;过期提醒在睡醒回顾报告末尾静态附列。

## 3.7 注入框架(`inject.py`)

- 固定顺序 `BLOCK_ORDER = ("level_rule", "environment", "schedule", "qzone", "memo", "favorability")`;实现上等级规则块并入好感度块首行(联调决定),实际渲染顺序 = 环境 → 日程 → 空间 → 备忘 → 好感度(qzone 块见 §3.13)。
- 每模块每轮仅允许一块(重复抛错,显式暴露);空块跳过;内容未变字节级复用(`InjectAssembler._cache`),热重载时 `reset()`。
- 块文本示例:`[环境] 今天 8月16日 周日,珠海:晴,29°C;节日:…;临近:…。` / `[日程] …` / `[备忘] 内容1;内容2` / `[好感度] …`。
- 各块独立开关:`inject.level_rule_enabled / environment_enabled / memo_enabled / favorability_enabled`(等级规则开关=好感度块首行规则条);qzone 块开关在 `qzone` 节且要求模块自检通过(见 §3.13)。

## 3.8 时间/节日/天气感知(`time_aware.py`)

- **公历节日回退链**:在线 holiday-cn(jsDelivr gh master 每年 JSON → raw.githubusercontent)→ `holiday-calendar` 库(manifest 依赖自动安装)→ 内置公历静态表(元旦/情人节/妇女节/劳动节/儿童节/国庆节/平安夜/圣诞节)。`holiday_online` 关闭时跳过在线源。
- **农历节日+节气**:`lunar-python` 库实时计算(无需网络、无预生成表);「今天」节日单独列,临近 3 天节日带日期前缀单独拼「临近:」段(联调发现:混入当天文本会误以为今天就是那个节日);双源重名(「七夕」vs「七夕节」)按名去重。
- **天气**:Open-Meteo(免 key),`city`/`city_lat`/`city_lon`(默认珠海 22.279410,113.528098),后台 `weather_refresh_minutes`(默认 45)分钟刷新;天气码→中文映射内置;快照落 `weather_snapshot` 表(供日程生成联动);失败静默跳过该片段(日志「天气获取失败,本轮环境块省略天气」)。
- 环境块由 `_refresh_environment` 后台任务维护(注册了 weather 45 分钟 + holiday 24h 两个调度);`_environment_block` 按 `_env_fetched_at` 缓存 45 分钟;睡眠期禁网络调用,下一 tick 重试。
- 注入文本格式:`[环境] 今天 8月16日 周日,珠海:晴,29°C;节日:…;临近:8月19日 七夕节。`(拟人感:城市名出现,bot 表现"生活在这个城市")。

## 3.9 reply 补传与哨兵(`reply_guard.py`)

### 3.9.1 上下文补传(规则层,默认开,零成本)

- 锚点 `maisaka.planner.after_response`(BLOCKING LATE,可改写 output_items)。
- **触发三条件全真**:本轮 planner 调用过上下文工具(内置常量 `CONTEXT_TOOLS = (query_memory, query_person_profile, fetch_history, view_forward_message, memo_read)`,**不再可配置**)且该 reply 调用的 `reply_reference` 为空且本轮 planner 的 reasoning 为空。
- 动作:把本轮被调用的上下文工具结果合并为文本摘要(按工具名排序、条目边界截断,默认 400 字符)填入该 reply 调用的 `reply_reference`;不改动其它工具调用。
- 日志:`reply 补传:{工具名列表}`。

### 3.9.2 LLM 哨兵层(默认关)

- 锚点 `maisaka.replyer.after_response`;判定「本次回复是否与聊天上下文明显不符/是否不该回复」;输出 `{"should_send": true/false, "reason": "…"}`;不符则撤回并闭环反馈 planner(corpus-callosum 式)。
- **当前实现:撤回动作仅日志**(spike ④ 验证结论前不承诺撤回);LLM 失败放行并告警。
- 日志:`哨兵判定:放行回复` / `哨兵判定:撤回回复:{reason}` / `哨兵层 LLM 调用失败,放行回复:…`。

## 3.10 贴表情(`msg_react.py`,仅群聊)

- `@Tool("msg_react")`:参数目标消息 ID + 贴表情意图;私聊调用返回「贴表情仅限群聊(QQ 私聊不支持贴表情)」。
- 执行链:每流冷却检查(`per_stream_cooldown_seconds` 默认 30 秒,JSON 快照 `msg_react_cooldown.json`)→ 取目标消息文本(`_fetch_recent` 50 条内)→ 旁路 LLM 从**内置 30 项 QQ 表情表**(`qq_emoji.py`,`emoji_id` 为 napcat `set_msg_emoji_like` 的 id)选最合适 → 必须命中表内 id,否则失败显式返回 → `adapter.napcat.message.set_msg_emoji_like` → 标记冷却。
- **无任何概率旁路**;防刷仅工程护栏。默认模型 `replyer`。

## 3.11 主动戳(`poke.py`)

- `@Tool("poke_user")`:参数目标用户/群号;**仅每用户冷却前置校验**(`cooldown_seconds` 默认 600 秒,JSON 快照 `poke_cooldown.json`;**好感度等级门槛已取消**——联调裁定);调用 `adapter.napcat.message.send_poke(user_id, group_id, target_id, qq_id)`(实测签名)。
- **入站戳一戳解析已按联调结论删除**(改写在实机中效果不及理想);被戳反应逻辑不实现(规格剔除)。

## 3.12 图片重看(`image_relook.py`)

- `@Tool("inspect_image")`:参数目标消息 ID(可选)或 `image_index`(倒数第几张含图消息,默认 1)+ 具体问题;另可选 `image_hash`(M3-r2 Task7 起)——图片 hash 前缀(8 位即可,来源 `view_friend_feeds` 返回的图标注),给定时**跳过消息搜索**,直接按前缀查 Images 表,覆盖非消息来源(view_friend_feeds 等经 tool result media 入库)的图片。
- 执行链:`message.get_recent(include_binary_data=True)` 解析 image 段(实机仅 hash 无 data)→ 经 `database.get`(Images 表按 image_hash)补读 `full_path` → 读 `/MaiMBot` 下文件补 base64 → 按字节魔数嗅探 MIME(PNG/JPEG/GIF/WEBP,兜底 png)→ 旁路 VLM 回答(文本前缀稳定;图片块追加尾部,无前缀缓存意义);hash 路径为 `database.get` 拉表(单结果=False)后插件侧前缀匹配,命中唯一复用同一条读文件→重看链。
- hash 前缀零命中/多命中**显式报错不猜**(多命中列候选前 3 提示加长前缀),且不回退消息搜索;目标太旧/取不到/补图失败均**显式报错并记录日志**(不静默)。默认模型 `utils`(轻量任务;VLM 较慢建议超时 120000)。

## 3.13 QQ空间(感知+互动)(`catsitate_core/qzone/`)

三期 M1 范围 = **感知**:bot 在日程标记的浏览窗口内"刷"好友动态(拉取说说 → 虚拟流注入 → 主链自主反应);三期 M2/M2.1 范围 = **互动**(评论/楼中楼回复/点赞/统一通知通道);v0.7(2026-09-01)将互动重构为**工具驱动架构**——出站意图系统整体删除,网关改 **receive**(只进不出,直接打字发不出去),bot 对说说的动作一律经 `qzone_comment`/`qzone_reply`/`qzone_like` 工具**自主决定、显式发出**;v0.8(2026-09-01)交付 **M3 表达**——`qzone_post` 工具发布自己的说说、睡前日记(入睡任务旁路生成+API 直发+醒来延迟回注)、发布内容 self 消息回注虚拟流、真实聊天见闻摘要叙事注入。

### 3.13.1 功能概览

- **浏览窗口内刷好友动态(M1)**:日程生成模板 v3 支持为 daily 窗口标记 `qzone=true`(通常一天 1~2 个,适合搭配轻松的独处活动如「窝着刷手机」);`qzone_poll` 调度(默认 15 分钟)在标记窗口内拉取好友说说——**拉取架构(统一时间线,M3 重构 2026-08-31)**:**发现层** 1 次 `feeds3_html_more` 统一时间线调用覆盖全好友(轻量索引 tid/uin/nickname/abstime/appid,FeedDiscovery),**充实层**仅对发现层标记为新的 tid 按作者 uin 分组拉 `emotion_cgi_msglist_v6` 完整实体(典型 0-3 次/周期)——API 量从 O(好友数) 降为 O(1+新动态数),好友 24 或 240 成本相同;发现层非登录态失败告警回退旧逐好友路径(好友列表走 adapter OneBot API `adapter.napcat.account.get_friend_list`,remark 优先作昵称,逐好友拉最近 3 条,好友间固定 2 秒间隔防风控)。窗口切换时收泵,未注入的队列**回退未读**(queued 行删除,下个窗口重新可见,不丢动态)。
- **工具驱动互动(v0.7,替代 M2 出站意图路由)**:注入消息的文本段末尾带**参数独立尾行**(可读性优化 2026-09-01,换行+〔〕独立成行,消除与正文的行内语义混淆)——浏览动态「…\n〔说说ID=xxx〕」(tid 前 12 位),通知「评论了你的说说:…\n〔说说ID=xx 评论ID=xx 评论者QQ=xx 评论于(今天HH:MM)〕」(完整语义键名;末段为动作时间「评论于/回复于(今天HH:MM)/(M月d日 HH:MM)」,M3-r2:让 bot 分得清互动新旧,`create_time` 缺失则省略该段;与工具参数名 `feed_id`/`comment_id`/`at_user_id` 的映射由场景 prompt 解释);泵注入成功后把该说说的上下文(主人/评论者/主评论二元组)登记进 **FeedContextRegistry**(内存 LRU,上限 128 条/48h 过期);planner 想互动就调工具,目标按 feed_id 三级解析:registry(精确/前缀)→ seen_store(7 天浏览窗前缀)→ awaiting(当前浏览项)——解析出的**全量 tid** 回填发 API(锚前缀不可直接用),三级全miss显式失败提示模型核对锚值。**在虚拟流直接打字是发不出去的**(receive 网关无出站路径)——不感兴趣就沉默(无工具调用轮),动作只能通过工具完成,意愿与目标选择权全部归模型。
- **四个空间工具(v0.7 互动 + v0.8 表达,M3-r2 起评论/回复/发布两段式)**:`qzone_comment(feed_id, reply_reference, reply_style?, at_user_id?)` 评论说说;`qzone_reply(feed_id, comment_id, reply_reference, reply_style?)` 楼中楼回复——**commentId+commentUin 二元组精确匹配主评论**(源A=好友评论/源B=bot 自己被回复的评论,@ 目标与二元组解耦由 wire 层承载);`qzone_like(feed_id?)` 点赞(缺省对当前浏览的说说,通知项自动锚定其原说说);`qzone_post(reply_reference, reply_style?)` 发布自己的说说(无频控——是否发/表达什么由模型自主决定)。四工具均方法内 stream_id 硬门控——仅虚拟流会话可用(SDK Tool 无类级 allowed_session 通道,联调实证,真实聊天流调用直接拒绝)。动作 API 失败不重试(告警跳过);登录态失效自动作废 cookie 下轮重取(自愈链)。
- **全域查看工具 `view_friend_feeds(qq, count?)`(M3-r2 Task7)**:拉指定好友最近说说(默认 3 条,上限 10),**任何聊天流都可用**(不限虚拟流会话——真实流仅供获取信息,空间动作工具在真实流隐藏);回 dict——`content` 文本摘要(作者/正文/逐图标注 sha256 前 8 位/〔说说ID=锚〕)+ `content_items` 图片媒体项(base64,宿主按 tool result media 入 Images 表,后续可经 `inspect_image` 的 `image_hash` 前缀反查重看);单条说说最多带 3 图,体积治理与浏览注入同链(压缩优先,极端丢弃保帧并告警);成功即 registry 登记(content_summary/recent_comments=表达生成层场景素材);登录态失效/拉取失败与动作工具同款自愈链与显式告警。
- **表达生成层(M3-r2,两段式正文)**:planner 只决定「是否动作、表达什么方向」——`reply_reference` 填表达方向(要点/事实/关系/语气倾向),`reply_style` 选篇幅(简短表达/正常回复/长回复,默认正常回复);正文由带**完整 bot 人设**的旁路 LLM(`qzone_expression` 模板,`expression_llm_model`)产出——planner 提示词只含行为风格摘要,完整人设在表达层,空间动作与真实聊天共享同一个人设出口。场景素材随 prompt 进变量段:评论带说说作者/正文摘要(registry `content_summary`,前 100 字)/近期评论摘要(`recent_comments`,get_user_feeds 合并的「昵称:内容」前 3 条,防复读);回复带说说正文摘要;发布不带场景素材。护栏:评论/回复正文上限 200 字、说说上限 500 字(超长带硬约束重生成一次,仍超长截断);生成失败/空素材显式返回错误回执(可重试口径)+告警,不静默兜底;同说说评论频控上限 **3 条**(窗口边界重置)与 @ 前缀拼装(napcat 同格式,registry 有该评论者昵称则用昵称)沿用不变。
- **日记(M3 表达,入睡任务旁路生成+API 直发+延迟回注)**:入睡时与次日日程生成同属入睡任务——用旁路 LLM(`qzone_diary` 模板,`diary_llm_model`)基于**当日真实素材**(日程活动概览+到期备忘+当日空间见闻)生成 80~200 字第一人称日记,经发布 API(`do_publish`)直发为说说;旁路 LLM 与发布 API 均不经消息链,**不受睡眠拦截(深夜直发)**;睡眠窗口终点未入睡时由 tick 补执行(每窗口至多一篇)。护栏:模板明令「内容必须完全基于给到的当日素材,不要编造」;空/超 300 字视为输出异常跳过;发布失败不落回注快照(没发出去的日记不该回注成「昨晚发过」的假上下文)。**回注延迟到醒来**:正文+发布时刻存 `qzone_pending_diary.json`,醒态 `sleep_tick` 以 self 消息「我昨晚发布的日记:{前 60 字}」补注进虚拟流(睡眠期注入会被 sleep_gate 拦进回顾缓冲,白注入);补注失败保留快照下轮重试。开关 `diary_enabled`(默认开)。
- **统一通知通道(M2.1,spec §3.7)**:模拟推送通知——`qzone_notify_poll` 调度(默认 120 秒,`notification_interval_seconds`,注册时下限 30 秒;始终运行,醒着即可,与浏览窗口无关)**双源检测**:源A=好友在 bot 自己说说下的新评论;源B=bot 在他人说说下的评论收到的新楼中楼回复(list_3,目标好友由 bot 评论留痕反查圈定)。通知走**双优先级队列 P1**(插队于浏览动态 P2 之前),模拟「刷着动态→弹通知→先看通知→回完继续刷」注意力模型;**通知注入是推送语义(M3-r2 P1/P2 分治):任何时刻可注入(仅 awaiting 互斥),不依赖浏览窗口——浏览窗口结束只清浏览队列 P2,通知队列保留等待注入条件**,浏览动态仅 read_qzone 窗口内注入;单轮至多 3 条(防通知风暴),早于 `summary_days` 的过旧通知截断不注入,阅读顺序新→旧(信息流降序,QQ 空间 App 实际形态)。通知正文自然可读并带参数独立尾行(可读性优化 2026-09-01)——源A「评论了你的说说:…\n〔说说ID=xx 评论ID=xx 评论者QQ=xx 评论于(今天HH:MM)〕」、源B「回复了你的评论「{bot原评论前20字}」:…\n〔说说ID=xx 评论ID=xx 评论者QQ=xx 回复于(今天HH:MM)〕」(两处参数行末段均为动作时间,`create_time` 缺失则省略不编造;楼中楼上下文=bot 被回复的原评论前 20 字,缺内容回退「你之前的评论」;评论内 `@{uin,nick}` 机器格式解析为「@昵称 」可读形态),带 is_mentioned 强制触发;注入消息带 reply 段引用**原说说**的注入消息(napcat quote 式,引用内容=原说说正文前 60 字;原说说未注入过则省略 reply 段回退纯文本);上一条注入仍在等待轮完成(awaiting)时不取新通知(不叠加,下轮再取)。通知项的注入被宿主拒绝或异常时**回退去重键**(下轮通知轮询重新发现,不因一次拒绝永久丢失;深度审查 B-4),但**重试上限 3 次**(深度审查 A-N1:软回退保留行,计数跨重发现累计;满 3 次仍被拒则保留登记放弃——防宿主持续拒绝时每 120 秒无限重注入);同一通知事件的 `fav_events` 同日去重(重发现不重复入库)。
- **好感度显式事件(M2,spec §3.9)**:好友评论 bot 说说 / bot 评论·回复·点赞好友,双向记入 `qzone_fav_events` 事件表——结算时按 `[空间互动]` 前缀并入该人日终结算素材(LLM 计权,事件按原始时刻去重防同日 early→daily 重判),并参与自然衰减计时基准;**不依赖 batch_counter**(虚拟流消息计数已被豁免)。
- **memo 按人语义联动(M2)**:虚拟流上写的备忘与真实聊天跨流可见(主 QQ+附带 QQ,见 §3.6.1)。
- **虚拟流注入(主链可引用, M1)**:每条动态构造为一条消息注入 `qzone-qq` 虚拟群聊流,复用主程序 planner→replyer 链;注入带 `is_mentioned=1.0` 强制触发;消息时间戳=阅读时刻(注入时刻,消息流时钟单调),发布时间以相对时间前缀写入正文(今天 HH:MM/M月d日 HH:MM,bot 不会把老说说当成刚发生;方案 B 2026-08-31);文本段末尾带「〔说说ID=xxx〕」参数独立尾行(v0.7 锚,v0.7.1 起换行独立成行,**纯图说说也保留文本段承载锚**);「刷到但懒得理」由 planner 自主沉默(无工具调用轮)表达——意愿判断权归模型。图片带 base64 交主流水线处理(描述/落 Images 表/可 `inspect_image` 重看);体积治理=压缩到 RPC 帧限内(用户裁定 2026-08-31 终案:超 12MB base64 预算按压缩阶梯收紧,极端不达标丢弃最大图保帧并告警;主程序入站链路兜底),下载失败的图以 `[图片]` 占位。
- **说说发布回注(qzone_post)**:发布成功的说说立即以 self 消息注入虚拟流(`qzone_self_` 前缀 message_id,user=bot 自己,**无 is_mentioned**——不触发 planner 决策轮,仅入历史)——后续好友评论该说说时,bot 需要这段历史才知道自己发过什么;正文只带前 60 字预览(全文已真实发布在空间,回注只是上下文锚);回注失败不影响发布回执(说说已远端发布,谎报失败会诱导重复发布)。
- **真实聊天见闻摘要注入(M3 表达叙事格式)**:真实聊天流的注入块附带 `[空间] 近期刷到: 昵称发了「摘要」;…`(近 `summary_days` 天已 seen 的 `summary_count` 条动态,叙事格式与浏览动态的自然文本一致;摘要截 20 字,纯图说说以「图片」占位,缺昵称回退QQ号)——bot 在真实聊天中可自然引用「我看到你发的说说」类见闻。
- **串行注入**:一次只允许一条动态处于「已注入待主链处理」状态;推进信号 = 轮完成(planner 响应无工具调用);超时兜底 `decision_window_seconds`(默认 150 秒,须大于最坏轮延迟;慢模型实测 31-53s,150 留余量),wait 态延长至 3 倍硬上限(自注入时刻起算,防 wait 期间注入下一条并入批处理);超时强制推进不清 registry——上下文(48h TTL)保留,后续轮次仍可对已注入说说调工具。

### 3.13.2 虚拟流与 person 统一(`qzone-qq`)

- 虚拟平台名常量 `qzone-qq`,伪群号 `virtual_group_id`(默认 `qzone_feed`,勿与真实群号相同)、显示名 `virtual_group_name`(默认「QQ空间」)。
- **person 统一(连字符别名)**:主程序 `get_person_id` 对含 `-` 的平台名取连字符后段(split 后第 2 段,如 `qzone-qq` → `qq`)计算命名空间,`qzone-qq` 与真实 `qq` 折叠为**同一 person**——好友画像/人物记忆跨空间与聊天聚合共享(空间流 `query_person_profile` 直接命中统一账本;内容来自好友真实说说,是统一而非混杂)。路由/账号按原始字符串 `qzone-qq` 分键,与真实 qq 平台零接触。
- **启动自检**(任一硬性失败则模块停用并显式告警):① `person.get_id("qzone-qq", 探针)` 与 `person.get_id("qq", 探针)` 折叠一致性——**不等/返回异常形态/调用异常均硬停用模块并告警**(用户裁定 2026-08-30:人物分裂不可接受,折叠失效宁可不用,不降级为分裂模式;自检校验返回为非空 str,防双侧同形失败的假阴性);② 主程序 `experimental.focus_mode` 必须关闭(focus 槽会吞掉注入的强制触发);③ 主程序 `chat.reply_timing.talk_value` 必须 >0(为 0 时注入消息被主程序静默消费);④ `favorability.bot_user_id` 非空(虚拟平台 bot 账号注册依赖);网关就绪上报失败同样停用(重载插件重试)。

### 3.13.3 拉取链路与去重

- **cookie(唯一合规路径)**:空间 cookie 经 adapter 能力 `adapter.napcat.account.get_cookies`(domain=`user.qzone.qq.com`)获取;持久化 `qzone_cookies.json` + `cookie_refresh_minutes`(默认 60 分钟)节流;获取失败或响应缺 `p_skey` 显式告警并跳过本轮拉取(有旧 cookie 时沿用旧值)。
- **协议**:经典空间网页 cgi(`emotion_cgi_msglist_v6`;g_tk=hash33(p_skey) 签名;`frameElement.callback({...})` 响应截取解析)。msglist 条目即说说;转发说说走回退链([转发自XX]原文),纯图说说以图段承载,视频以 [视频] 占位。
- **重试语义**:`max_retries` 默认 0——图片下载固定单次重试(读路径例外,联调实证 CDN 瞬态 404);动作 API 不重试(M2 起 max_retries 约束);`request_timeout_ms` 默认 10000。
- **去重(`qzone_feeds` 表,tid 主键)**:入队=queued,注入成功=seen;同 tid 任意状态存在即跳过(不重复注入)。
- **数据保留期清理(每日一次,深度审查 D-1)**:`qzone_comments` 保留 30 天(与源B反查时间下界对齐),`qzone_feeds` 的 seen 行保留 7 天(`recent_seen` 只需 `summary_days≤3`,留余量);queued 行不清理(回退未读语义由窗口收泵负责)。
- 睡眠期不拉取不注入(绝对静默);`qzone.enabled` 关闭或模块停用(`_qzone_available=False`)时一切拉取/注入/场景手术/见闻注入跳过。

### 3.13.4 虚拟流专属处理(场景手术与模块豁免)

- **场景替换**:planner(`before_request`)与 replyer(`before_model_request`)两侧,把 system 文本中的群聊场景提示词(主程序 `chat.reply_style.group_chat_prompt` 当前值,1 小时缓存)**原位精确替换**为空间场景文案(按配置值匹配,用户改过配置也能命中;说明〔〕参数行格式与四工具用法(含 qzone_post 发说说),并明示「直接打字是发不出去的」)。**场景文案可配置(2026-09-01)**:模板 `catsitate_qzone_scene` 走三层链——主程序 `data/custom_prompts/zh-CN/`(WebUI 编辑产物,优先)→ `prompts/zh-CN/`(插件 on_load 自动部署,prompt_templates 为权威源)→ 插件内置;WebUI 改完即生效(mtime 缓存失效自动重读)。虚拟流注入块已去重只留动态状态(场景全文只在 system 段出现一次);配置为空 / 未命中(主程序模板改版风险)→ 每类每进程告警一次,本轮无场景说明(工具链仍可用)。
- **工具白名单与双向隔离(M2.1)**:虚拟流 planner 轮按 `tool_whitelist` 过滤工具定义,默认 `wait/query_memory/query_person_profile/memo_write/memo_read/inspect_image/view_friend_feeds/qzone_like/qzone_comment/qzone_reply/qzone_post`(**不含 tool_search/msg_react/poke_user;v0.7 起 `reply` 已移除**——receive 网关无出站路径,白名单里的 reply 无效;v0.8 起 `qzone_post` 进默认值;M3-r2 起 `view_friend_feeds` 进默认值——**全域查看工具**,真实流也可用,真实流仅供获取信息,空间动作工具在真实流隐藏);硬门控不随此配置放松(空间工具的虚拟流限定为方法内硬门控)。**反向隔离**:真实聊天流(非 qzone 会话)自动隐藏全部 `qzone_*` 前缀工具——防模型在真实 QQ 流误调空间工具(白名单只管 qzone 流侧,双向隔离是对侧护栏)。**旧配置兼容**:持久化白名单缺 `qzone_comment`/`qzone_reply`/`qzone_post` 时 on_load 告警提示补入(不静默改配置)。
- **deferred reminder 剥除**:剥除主程序追加的 deferred 工具提醒 user 项(`<system-reminder>` 开头)。
- **模块豁免**:虚拟流消息**不计好感度**(好友发说说 ≠ 与 bot 互动,空间互动走 M2 显式事件路径);虚拟流出站文本**不进晚安判定**(防深夜短评论触发全局入睡);daily 窗口主动发言候选**排除虚拟流**(空间表达走 qzone 窗口本身);流缓存纳入 `qzone-qq` 平台;贴表情/戳一戳工具在虚拟流平台自检拒用(返回「当前是QQ空间动态流,这个动作用不上哦」)。

### 3.13.5 配置与数据

配置见 §4.12(`qzone` 节全字段);数据文件见 §5.1(`qzone_feeds` 表)与 §5.2(`qzone_cookies.json`)。

### 3.13.6 日志关键词

- `QQ空间窗口开始,注入泵激活` / `QQ空间浏览窗口结束,浏览队列回退未读(N 条);通知队列保留等待注入`(M3-r2:窗口结束只清浏览队列 P2,通知队列 P1 保留——推送语义等注入条件)
- `QQ空间新动态入队 N 条`;`QQ空间窗口开始,注入泵激活;回收跨启动 queued 残留 N 条(重新拉取)`
- `QQ空间登录态失效(code=-3000/-10005),cookie 已作废,下轮重取`;`QQ空间说说拉取失败(uin=…),该好友本轮跳过`
- `QQ空间图片下载异常(…),以占位注入`;`空间图片下载失败(重试后仍失败): …`;`空间图片域名不在白名单(…),拒绝下载`(深度审查 E-1:仅 *.qpic.cn/*.qq.com,防 Cookie 外带);`QQ空间图片压缩后仍超 RPC 帧预算,丢弃保帧: …`
- `QQ空间动态注入被宿主拒绝(tid=…,adapter policy 或网关状态),跳过且不标记已见`;`QQ空间通知被拒,回退去重键待下轮重试(第 N 次)`(深度审查 B-4+A-N1:通知项注入失败软回退 is_new 登记键,计数跨重发现累计);`QQ空间通知重试 3 次仍被拒(dedup_key=…),放弃不再重试`(A-N1 上限:保留登记不再重注入)
- `QQ空间动态已注入(tid=…,作者=…)`;`QQ空间注入等待轮完成超时(tid=…),强制推进`(超时推进不清 registry,上下文 48h TTL 内仍可解析)
- `QQ空间评论失败(feed_id=…)`(v0.7 `qzone_comment` 动作 API 失败,不重试);`QQ空间楼中楼回复失败(feed=…,comment=…)`(`qzone_reply`);`QQ空间点赞失败(tid=…)`(`qzone_like`);`QQ空间评论记账失败(远端已成功,仅告警)`(远端成功后本地记账异常,不影响回执)
- `QQ空间说说发布成功: …`(v0.8 `qzone_post`/日记发布成功,前 30 字预览);`QQ空间说说发布失败`(`qzone_post` 动作 API 失败);`QQ空间说说回注失败(发布已成功,仅上下文注入失败)`;`QQ空间说说发布遇登录态失效,cookie 已作废,下轮重取`
- `QQ空间评论/回复/说说正文生成失败(原因)`(M3-r2 表达生成层:旁路人设生成失败显式报错,回执可重试口径,零写调用);`QQ空间表达生成超长(N 字>上限 …),带字数硬约束重新生成一次` / `QQ空间表达生成重生成仍超长(N 字),截断至 N 字`(护栏路径,不失败)
- `QQ空间日记发布成功: …`(入睡任务日记直发);`QQ空间日记 LLM 生成失败,跳过本轮` / `QQ空间日记 LLM 失败(success=…),跳过`(旁路生成失败,当夜无日记);`QQ空间日记内容异常(长度=…),跳过发布`(空/超 300 字护栏);`QQ空间日记发布失败(内容已生成,发布跳过)`;`QQ空间日记醒来补注完成`;`QQ空间日记补注失败(下个 tick 重试)`(快照保留,醒态 tick 重试)
- `QQ空间登录态失效,cookie 已作废,下轮重取`(空间工具遇登录态失效,作废 cookie 自愈,该次动作不重试);`QQ空间点赞遇登录态失效,cookie 已作废,下轮重取`
- `QQ空间网关收到意外出站回调(receive 模式无出站路径,文本预览=…)`(v0.7 防御分支:receive 网关不应被回调,出现即主程序行为变化,回执 success=False)
- `QQ空间通知入队 N 条(源A+B,P1 插队)`(M2.1 统一通知:双源检测到新评论/楼中楼回复入 P1 队列);`QQ空间通知轮询源A失败,本轮跳过`;`QQ空间通知轮询源B失败(好友 …),该好友跳过`;`QQ空间通知轮询源B好友反查失败,本轮跳过源B`;`QQ空间评论过旧跳过(…)` / `QQ空间楼中楼回复过旧跳过(…)`(早于 `summary_days` 截断);`QQ空间登录态失效(通知轮询源A/源B…),cookie 已作废,下轮重取`(自愈链,该轮不重试);`QQ空间动态注入被宿主拒绝`/`QQ空间动态已注入`(通知注入与浏览注入同走串行泵日志)
- `QQ空间点赞遇登录态失效,cookie 已作废,下轮重取`(v0.7 `qzone_like` 自愈链)
- `QQ空间数据清理:评论去重 N 行,seen 保留 7 天`(深度审查 D-1:每日 prune 任务)
- `QQ空间场景回退:…`(「群聊场景提示词配置为空…」/「群聊场景提示词替换未命中…」)
- `QQ空间模块停用:person 别名折叠自检失败(qzone-qq 与 qq 未折叠到同一命名空间,或自检调用返回异常形态 a=… b=…),主程序 get_person_id 折叠机制可能已改版`;`person 别名自检调用失败,QQ空间模块停用`
- `QQ空间模块停用:…`(focus_mode 开启 / talk_value=0 / bot_user_id 为空 / person 别名折叠自检失败 / 网关就绪上报失败)
- `QQ空间虚拟平台就绪(platform=qzone-qq,伪群=…)`

### 3.13.7 已知限制

1. **日记生成频率与素材边界**:日记属入睡任务,每睡眠窗口至多一篇(入睡或窗口终点补执行,二者只走其一);素材只取当日日程/备忘/空间见闻三源,聊天正文不进素材(日记只回顾「做了什么/看到什么」,内容保真由模板「不得编造」约束+模型决定);LLM 失败当夜无日记,不补生成。
2. **cookie 依赖 adapter**:空间 cookie 只经 `adapter.napcat.account.get_cookies` 获取,NapCat 不响应该 API(或响应缺 `p_skey`)时无法拉取,显式告警跳过;cookie 链路不做重试循环(登录态失效走 invalidate 下轮重取自愈)。
3. **动态形态**:msglist 条目即说说;转发说说走回退链([转发自XX]原文),纯图说说文本段仅承载工具 ID 锚(图段承载内容),视频以 [视频] 占位。
4. **注入图片压缩到 RPC 帧限内**(用户裁定 2026-08-31 终案):base64 总量超 12MB 预算触发压缩阶梯(PIL 降分辨率×降质量),极端不达标丢弃最大图保帧;下载失败以 `[图片]` 占位。
5. **备忘跨流可见性(I-1)已随 M2 memo 按人重构解决**(spec §3.10,见 §3.6.1):条目=主QQ+附带QQ(≤5)跨流可见,流维度保留;旧库自动补列迁移。
6. **写路径有真实副作用**:评论/回复/点赞/发布说说/日记一经工具或入睡任务成功即真实发布(无撤回通路);动作 API 失败不重试,点赞的 own-feed 枚举无 API——好友对 bot 说说点赞的好感度事件无法经轮询检测(仅 bot 主动点赞经工具路径计入)。`qzone_post` 无频控(正文生成上限 500 字护栏之外全凭模型自主),日记每窗口至多一篇。
7. **registry 为内存态(工具驱动 v0.7)**:FeedContextRegistry 重启即空——重启后已注入但未互动的说说只能靠 seen_store(7 天浏览窗)或消息锚重新解析,主评论二元组(comment_uin)丢失时 `qzone_reply` 回退 bot 自己作二元组(通知场景的常规形态);不影响评论/点赞主路径。

### 3.13.8 生产部署注意事项

- **NapCat 需可响应 `adapter.napcat.account.get_cookies`**(domain=`user.qzone.qq.com`):不可用时模块只能依赖持久化旧 cookie,过期即拉取失败。
- **`experimental.focus_mode` 必须关闭**:开启时启动自检直接停用 qzone 模块(focus 槽会吞掉注入的 is_mentioned 强制触发)。
- **主程序 `get_person_id` 连字符折叠是 person 统一的前提**:自检发现 `qzone-qq` 与 `qq` 未折叠到同一命名空间(或自检返回异常形态)时,模块**硬停用并告警,不降级为分裂模式**(人物分裂不可接受)——升级主程序后若见「QQ空间模块停用:person 别名折叠自检失败」告警,说明折叠机制可能已改版,需插件适配后再启用。
- **回复频率 talk_value 必须 >0(硬停用条件,自检检测)**:`chat.reply_timing.talk_value=0` 时启动自检直接停用 qzone 模块——注入消息会被主程序静默消费(bot「刷到但永不理」),感知完全失效。
- **`favorability.bot_user_id` 必须配置(非空)**:虚拟平台 bot 账号注册与虚拟流 session_id 计算依赖它,为空则模块停用。
- **勿配置 `*:*` 全局表达共享组**:虚拟流学习落在自身 session(对真实流无污染),全局共享组会把空间流表达泄入真实流;不希望空间评论喂养虚拟流表达库时,可在主程序 WebUI 对该会话关学习(插件无法代设)。
- **`schedule_generate` 模板升 v3 后,WebUI 自定义过的该模板需手动同步**:插件自动部署只覆盖主程序 `prompts/zh-CN/` 内置层,`data/custom_prompts/` 下的 WebUI 编辑产物优先级更高且不会被覆盖——旧版自定义模板不含 `qzone` 属性说明,日程将不产生 qzone 浏览窗口(需在 WebUI 手动更新或删除该自定义模板)。**`qzone_scene` 模板同理(v0.8 升 v3)**:WebUI 自定义过的该模板需手动同步,否则场景说明不含 `qzone_post` 用法(模型在虚拟流里不知道可以发说说)。
- **写路径有真实副作用(风险提示)**:评论/回复/点赞/发布说说一经 `qzone_comment`/`qzone_reply`/`qzone_like`/`qzone_post` 调用成功即真实发布到QQ空间(无撤回通路);**是否互动、表达什么方向、是否发说说,完全由模型自主决定**(正文由表达生成层按人设产出;插件只做工程护栏——同说说评论频控上限 3、正文生成上限 200/500 字(超长重生成仍超长则截断)、虚拟流会话硬门控、目标三级解析失败显式拒绝、生成失败显式报错不静默兜底);入睡任务还会每晚自动发布一篇日记(`diary_enabled` 可单独关闭)。`qzone.enabled` 关闭即整体停用(读+写一起);不希望写动作时关闭该开关或停用模块,不存在"只读不禁写"的中间档。
- **通知轮询源B API 量(M3 统一时间线重构 2026-08-31)**:源B搭发现层便车——每轮 1 次统一时间线发现层调用+仅对「发现层有新活动 ∩ bot 近 30 天评论过(深度审查 D-1 反查时间下界)」的好友拉楼中楼(每人间隔 2 秒),**量与好友数无关**(零交集时零源B拉取);源B好友数硬上限(10)已随重构删除,不再截断。
- **旧配置升级(v0.7/v0.8)**:持久化的 `qzone.tool_whitelist` 若含已废弃的 `reply` 或缺 `qzone_comment`/`qzone_reply`/`qzone_post`,on_load 会告警提示——需手动更新配置补入(reply 项无效可移除);不更新则虚拟流内无法评论/回复/发说说。

## 3.14 LLM 用量记账与旁路调用(`plugin.py`)

- `_side_llm_call` 是全部旁路 LLM 统一出口:经 `llm.generate` 能力直调,`model` = 主程序 task 名,超时由各能力节 `*_timeout_ms` 传入(填 0=主程序默认 30s;联调实测 utils 模型 31-53s 会触发默认超时,慢模型建议 120000)。
- 每次调用按模块记账 `llm_usage(day, module, calls, tokens)`;模块分列:`favorability` / `decay` / `msg_react` / `image_relook` / `sentinel` / `schedule_generate` / `sleep_confirm` / `sleep_review` / `qzone_diary`。
- 当日旁路调用合计达到或超过 `plugin.llm_daily_call_warning_threshold`(默认 50)时告警:「旁路 LLM 当日调用次数已达或超过阈值 50,请注意用量」。

---

## 4. 配置项全表

> WebUI 插件配置页按节渲染(中文 label)。所有 LLM 字段为**平铺字段**:填主程序 `model_task_config` 的 **task 名**(填模型标识会报「未找到名为 … 的模型配置」),留空=主程序默认(不推荐)。`*_timeout_ms` 填 **0** = 主程序默认(30s)。

### 4.1 plugin 节(插件)

| 字段 | 默认 | 说明 |
|---|---|---|
| enabled | **false** | 插件总开关(须手动打开) |
| config_version | "1.0.0" | 配置版本(仅标识) |
| llm_daily_call_warning_threshold | 50 | 旁路 LLM 每日调用告警阈值 |

### 4.2 inject 节(注入框架)

| 字段 | 默认 | 说明 |
|---|---|---|
| enabled | true | 注入管线总开关 |
| level_rule_enabled | true | 好感度块内按等级单条注入规则开关 |
| environment_enabled | true | 环境块(节日/天气)注入开关 |
| memo_enabled | true | 备忘块注入开关 |
| favorability_enabled | true | 好感度块注入开关 |

### 4.3 time_aware 节(时间感知)

| 字段 | 默认 | 说明 |
|---|---|---|
| enabled | true | 节日/天气感知开关 |
| city | "珠海" | 城市名(注入文本用) |
| city_lat | 22.279410 | 纬度(Open-Meteo) |
| city_lon | 113.528098 | 经度(Open-Meteo) |
| weather_refresh_minutes | 45 | 天气后台刷新间隔(分钟) |
| holiday_online | true | 节日数据在线刷新开关 |

### 4.4 favorability 节(好感度)

| 字段 | 默认 | 说明 |
|---|---|---|
| enabled | true | 模块开关 |
| window_hours | 24 | 日终结算周期(小时) |
| early_settle_threshold | 20 | 提前结算消息数阈值 |
| daily_max_early_settle | 3 | 每用户每日提前结算上限 |
| daily_settle_min | 3 | 日终结算最小消息数(不足顺延) |
| delta_max | 5 | 单次结算 delta 变化上限(±,判定结果钳制在此范围) |
| decay_enabled | true | 自然衰减开关 |
| decay_after_days | 7 | 未互动 N 天后开始衰减 |
| decay_max | 3 | 单次衰减幅度上限(-decay_max 到 0) |
| decay_llm_model | "memory" | 衰减判定模型(task 名) |
| decay_llm_timeout_ms | 0 | 衰减判定超时(毫秒;0=主程序默认) |
| level_rule_stranger / familiar / close / best_friend / special | 见 §4.9 默认文案 | 5 级行为准则文本(独立字段,可自改) |
| note_max_chars | 40 | 关系注记最大字符数(落库强制) |
| material_max_messages | 30 | 结算素材锚定的用户消息条数 |
| material_message_max_chars | 200 | 单条素材截断长度 |
| bot_user_id | ""(留空=不识别) | bot 自身账号 id(实机 napcat 账号,如 3545773341);结算素材中该 id 发言标记为 bot 随附;**必须配置,否则 bot 发言识别与 quote 归属全部失效** |
| llm_model | "memory" | 结算判定模型(task 名) |
| llm_timeout_ms | 0 | 判定超时(毫秒;0=主程序默认) |

5 级规则默认文案:陌生=「仅按普通网友对待,保持礼貌与距离」;熟悉=「认识一段时间,可自然闲聊」;亲近=「关系较好,可主动关心」;挚友=「非常信任,可分享心事」;特别=「最重要的人,格外在意其感受」。

### 4.5 memo 节(备忘录)

| 字段 | 默认 | 说明 |
|---|---|---|
| enabled | true | 模块开关 |
| tool_enabled | true | memo_write/memo_read 工具开关 |
| command_enabled | true | /记一下 命令开关 |
| default_ttl_hours | 24 | 单条缺省有效期(小时) |
| max_ttl_hours | 168 | 单条有效期上限(小时) |
| entry_max_chars | 80 | 备忘内容最大字符数(写入强制) |
| inject_max | 5 | 备忘注入合计条数上限 |

### 4.6 msg_react 节(贴表情)

| 字段 | 默认 | 说明 |
|---|---|---|
| enabled | true | 工具开关 |
| per_stream_cooldown_seconds | 30 | 每流冷却秒数 |
| llm_model | "replyer" | 选表情模型(task 名) |
| llm_timeout_ms | 0 | 选表情超时(毫秒;0=主程序默认) |

注:表情表为**内置 30 项**精选 QQ 表情表(联调裁定,原规划的可配置 `emoji_whitelist` 未实现)。

### 4.7 poke 节(戳一戳)

| 字段 | 默认 | 说明 |
|---|---|---|
| enabled | true | 模块开关 |
| poke_tool_enabled | true | 主动戳工具开关 |
| cooldown_seconds | 600 | 每用户冷却秒数 |

### 4.8 reply_guard 节(reply 补传)

| 字段 | 默认 | 说明 |
|---|---|---|
| enabled | true | 模块开关 |
| context_backfill_enabled | true | 上下文补传开关 |
| sentinel_enabled | **false** | LLM 哨兵层开关(默认关;开启后每句回复多一次旁路判定) |
| sentinel_model | "planner" | 哨兵模型(task 名) |
| sentinel_timeout_ms | 0 | 哨兵超时(毫秒;0=主程序默认) |

### 4.9 image_relook 节(图片重看)

| 字段 | 默认 | 说明 |
|---|---|---|
| enabled | true | 工具开关 |
| llm_model | "utils" | 重看模型(task 名;VLM 较慢建议超时 120000) |
| llm_timeout_ms | 0 | 重看超时(毫秒;0=主程序默认) |

### 4.10 sleep 节(睡眠)

| 字段 | 默认 | 说明 |
|---|---|---|
| enabled | true | 模块开关 |
| min_sleep_minutes | 240 | 最短睡眠分钟(不足顺延醒来) |
| max_sleep_minutes | 660 | 最长睡眠分钟(超过提前醒) |
| silent_sleep_enabled | true | 静默入睡开关(睡眠窗口内生效:关=窗口起点直接入睡,开=安静满 N 分钟入睡) |
| silent_sleep_minutes | 60 | 静默入睡:无消息满 N 分钟 |
| review_enabled | true | 睡醒回顾开关(生成聚合报告文件) |
| review_llm_model | "memory" | 回顾总结模型(task 名) |
| review_llm_timeout_ms | 0 | 回顾超时(毫秒;0=主程序默认) |

### 4.11 schedule 节(日程)

| 字段 | 默认 | 说明 |
|---|---|---|
| enabled | true | 模块开关 |
| max_regenerate | 1 | 生成校验失败重生成次数 |
| speak_threshold_level | "熟悉" | 日常发言最低好感度等级(仅 daily 窗口) |
| speak_max_streams_per_window | 1 | 每窗口最多主动触发流数(按等级+活跃度排序取前 n) |
| schedule_llm_model | "memory" | 日程生成模型(task 名) |
| schedule_llm_timeout_ms | 0 | 日程生成超时(毫秒;0=主程序默认) |
| daily_speak_limit | 5 | 全天主动发言次数上限(每次 trigger 计 1,主程序沉默也计) |

### 4.12 qzone 节(QQ空间)

| 字段 | 默认 | 说明 |
|---|---|---|
| enabled | true | QQ空间模块开关(含评论/点赞等写动作);关闭或启动自检失败时拉取/注入/见闻注入/互动工具/统一通知全部跳过 |
| poll_interval_minutes | 15 | 统一时间线发现层轮询间隔(分钟,含充实层新动态拉取) |
| comment_poll_enabled | true | 统一通知轮询开关(双源:自己说说新评论+他人说说楼中楼新回复,始终运行醒着即可;M2.1 沿用作总开关) |
| notification_interval_seconds | 120 | 统一通知轮询间隔(秒,模拟推送通知的检查频率;注册时下限 30 秒;M2.1) |
| comment_poll_interval_minutes | 30 | **废弃**(M2 评论轮询间隔,分钟;M2.1 起由 `notification_interval_seconds` 替代,不再消费;保留仅为兼容旧配置,可安全删除) |
| decision_window_seconds | 150 | 注入后等待 planner 轮完成的超时兜底(秒;须大于最坏轮延迟,慢模型实测 53s,150 留余量;wait 态延长至 3 倍硬上限) |
| tool_whitelist | ["wait","query_memory","query_person_profile","memo_write","memo_read","inspect_image","view_friend_feeds","qzone_like","qzone_comment","qzone_reply","qzone_post"] | 虚拟流 planner 工具白名单(按名过滤;硬门控不随此配置放松,默认不含 tool_search/msg_react/poke_user;v0.7 起 reply 移除、qzone_comment/qzone_reply 进默认值,v0.8 起 qzone_post 进默认值,M3-r2 起 view_friend_feeds 进默认值——全域工具(view_friend_feeds/inspect_image)同时在此列,表外工具一律不可用;旧配置缺新工具时 on_load 告警) |
| virtual_group_id | "qzone_feed" | 虚拟群聊流伪群号(勿与真实群号相同) |
| virtual_group_name | "QQ空间" | 虚拟群聊流显示名 |
| summary_count | 5 | 真实聊天注入的近期已见动态条数 |
| summary_days | 3 | 见闻摘要回溯天数 |
| diary_enabled | true | 日记功能开关(入睡时生成并发布空间日记说说;关闭则入睡任务只生成次日日程) |
| diary_llm_model | "memory" | 日记生成模型(task 名) |
| diary_llm_timeout_ms | 0 | 日记生成超时(毫秒;0=主程序默认) |
| expression_llm_model | "memory" | 表达生成模型(评论/回复/说说正文的旁路人设生成,task 名;M3-r2 两段式) |
| expression_llm_timeout_ms | 0 | 表达生成超时(毫秒;0=主程序默认) |
| request_timeout_ms | 10000 | 空间 HTTP 请求超时(毫秒) |
| max_retries | 0 | 空间动作 API(评论/点赞/发布,M2 生效)失败重试次数;0=失败即告警跳过。M1 读路径(图片下载)固定单次重试(联调实证 CDN 瞬态 404),不受此配置影响 |
| cookie_refresh_minutes | 60 | cookie 刷新节流(分钟,间隔内跳过重取) |

注:QQ空间动态注入复用主程序 planner→replyer 链不占旁路记账;QQ空间模块的旁路 LLM 调用有二——日记(`qzone_diary` 模块)与表达生成层(`qzone_expression` 模块,评论/回复/发布说说的正文生成,M3-r2 起),各自按模块记账。

### 4.13 debug 节(调试)

| 字段 | 默认 | 说明 |
|---|---|---|
| enabled | false | debug 日志开关:开启后 `catsitate.core` 的 debug 级日志写入数据目录 `logs/catsitate-{YYYYMMDD}.log`(公测复审用),热生效;文件权限 0600(日志含用户标识,仅属主可读),关闭时恢复 logger 原级别 |

### 4.14 已删除字段(历史遗留,勿再配置)

| 字段 | 归属 | 删除原因 |
|---|---|---|
| `greet_threshold_level` | schedule | 主动问候门槛与「特别」等级绑定,无独立配置(全局决策 #9) |
| `private_threshold_level` | schedule | 同上 |
| `context_tools` | reply_guard | 上下文工具列表改为内置常量 `CONTEXT_TOOLS`,不再可配置(联调裁定) |
| `enhance_notice_text` / `inject_to_context` | poke | 入站戳一戳通知解析整体删除(联调结论:改写效果不及理想) |
| `min_level_for_poke` | poke | 主动戳好感度门槛已取消(仅冷却) |
| `emoji_whitelist` | msg_react | 表情白名单改为内置 30 项精选表 |

---

## 5. 数据文件

数据根目录:`data/plugins/catsitate.core/`(MaiBot 插件数据目录)。

### 5.1 catsitate.db(sqlite, WAL 模式)

| 表 | 列 | 说明 |
|---|---|---|
| favorability | user_id(PK), level, score, note, window_start, judged_at | 好感度**按人单行**;window_start=当前批次起点,judged_at=上次结算时间 |
| favorability_log | judge_id(PK), user_id, delta, note, judged_at | 判定日志,幂等防重;judge_id 前缀 `early-`/`daily-`/`decay-{时间}-{user}` |
| batch_counter | user_id, stream_id(PK 联合), count, last_bump | 活跃度账本(计数 + 近 24h 活跃判定、衰减流定位);结算窗口不在此 |
| memo | id, content, stream_id, user_id, extra_user_ids, expires_at, created_at, remind_at | 备忘录(按人重构 M2:主QQ+附带QQ 跨流可见);remind_at 为二期加列、extra_user_ids 为 M2 加列(均自动补列) |
| llm_usage | day, module, calls, tokens(PK 联合) | 旁路 LLM 用量按日/模块分列 |
| weather_snapshot | id(=1), city, fetched_at, data | 天气快照(JSON),供日程生成联动 |
| qzone_feeds | tid(PK), abstime, author_uin, author_nickname, summary, state, interacted, injected_at, created_at | QQ空间动态去重:state=queued(已入队)/seen(已成功注入);interacted=点赞评论过;author_nickname 为 M2 加列(见闻摘要带作者,旧库自动迁移);窗口结束 queued 行删除回退未读 |
| qzone_comments | comment_key(PK), friend_uin, created_at, retry_count, pending_retry | 评论/楼中楼去重与 bot 评论留痕(M2.1 统一通知):好友评论 key=`feed_tid:comment_tid:uin`、楼中楼回复 key=`feed_tid:parent_comment_tid:reply:reply_tid`,发现即登记(通知项注入被拒/异常时**软回退**——置 pending_retry 待下轮重检,retry_count 跨重发现累计、满 3 次放弃,深度审查 B-4+A-N1;两列均为 A-N1 加列,旧库自动迁移);bot 自评留痕 key=`feed_tid:bot:{文本}`(friend_uin=说说主人,供源B反查圈定与通知正文引用,反查只认近 30 天登记——深度审查 D-1);表保留 30 天(每日清理任务) |
| qzone_fav_events | id, day, user_id, kind, text, created_at | M2 好感度显式事件(spec §3.9):kind=COMMENT(好友评论 bot)/OUT_COMMENT(bot 评论好友)/OUT_LIKE(bot 点赞);并入日终结算素材与衰减计时（无清理,只增不减——刻意保留作衰减计时基准）;**同日同 user+kind+text 去重**(A-N1:通知回退重发现不重复入库);当日事件 user 并入日终结算候选(C-N1) |

### 5.2 JSON 快照(JsonSnapshot,原子写)

| 文件 | 内容 | 用途 |
|---|---|---|
| msg_react_cooldown.json | `{stream_id: 上次贴表情时刻}` | 贴表情每流冷却 |
| poke_cooldown.json | `{user_id: 上次戳时刻}` | 主动戳每用户冷却 |
| sleep_state.json | `{state, sleep_at, wake_at}` | 睡眠状态持久化(重启恢复不依赖日程) |
| sleep_review_buffer.json | `{"messages": [拦截消息…]}` | 睡眠期拦截消息缓冲(dict 包装,防重启丢失) |
| remind_fired.json | `{"remind:{memo_id}": 触发时刻}` | 备忘兜底注入去重(防重启重复注入) |
| qzone_cookies.json | `{"cookies": {…}, "saved_at": …}` | QQ空间 cookie 持久化(进程重启垫底,按 cookie_refresh_minutes 节流重取) |
| qzone_pending_diary.json | `{"text": 日记正文, "published_at": 发布时刻}` | 入睡任务已发布的日记(醒来回注虚拟流的待办;回注成功清空,失败保留重试;持久化防重启丢失) |

### 5.3 schedule.json(日程落盘)

`{"data": {date, windows}, "edit_history": [{time, action, before, after}], "generated": bool, "saved_at": …}`;重启恢复仅当 `data.date == 今天`;过期文件删除并告警,损坏/非法告警忽略。

### 5.4 sleep_review/reports/

`sleep_review_{YYYYMMDD_HHMMSS}.md`:睡醒回顾聚合报告(每流消息数+LLM 摘要+睡眠期到期备忘静态附列)。

### 5.5 logs/

`catsitate-{YYYYMMDD}.log`:debug.enabled 开启时的插件 debug 级日志落盘。

---

## 6. 运行时链路(带日志关键词)

### 6.1 消息处理流

```
入站消息
 ├─ chat.receive.before_process(catsitate_sleep_gate, BLOCKING EARLY)
 │    ├─ 睡眠中 → 记缓冲 → abort(绝对静默,无任何后续处理)
 │    └─ 唤醒态 → 刷新 _last_activity_ts(静默入睡计时)→ 放行
 ├─ chat.receive.after_process(catsitate_fav_count, OBSERVE)
 │    ├─ 睡眠/通知类/缺 id → 跳过
 │    ├─ count_message(batch_counter bump)
 │    └─ check_trigger == "early" → spawn 结算(kind=early)
 │         └─ 日志:「好感度结算 {user}:early delta={n}」/「好感度结算失败 …」
主链路 planner 请求
 └─ maisaka.planner.before_request(catsitate_inject, BLOCKING LATE)
      ├─ 构造注入块(环境/日程/备忘/好感度)→ 前插 system 之后
      └─ 失败仅日志:「注入块构造失败,本轮跳过注入」;定位失败:「注入定位失败:…已回退追加尾部」
planner 输出
 └─ maisaka.planner.after_response(catsitate_reply_backfill, BLOCKING LATE)
      └─ 三条件满足 → 补 reply_reference → 「reply 补传:[…]」
replyer 出站
 ├─ maisaka.replyer.after_response(catsitate_goodnight, BLOCKING LATE)
 │    ├─ 刷新活动计时;晚安短句 + 睡眠窗口内(可入睡时间)→ sleep_confirm LLM → SLEEP → 入睡+入睡任务(次日日程+日记)
 │    └─ 日志:「已入睡:醒来 {wake_at}」「次日日程已生成:…」
 └─ maisaka.replyer.after_response(catsitate_sentinel, BLOCKING LATE, 默认关)
      └─ 日志:「哨兵判定:放行/撤回回复」
```

### 6.2 每日调度任务表(Scheduler,60s tick)

| 任务名 | 间隔 | 行为 | 关键日志 |
|---|---|---|---|
| weather | max(weather_refresh_minutes,1)×60 s(默认 45 分钟) | 拉节日+天气,刷新环境块缓存,天气落库 | 「holiday-cn 数据源…获取失败」「天气获取失败,本轮环境块省略天气」 |
| holiday | 24h | 节日数据日级刷新(同上任务) | 同上 |
| memo_cleanup | 1h | 删除过期备忘 | 「备忘清理:{n} 条过期」 |
| daily_settle | max(window_hours,1)×3600(默认 24h) | **先衰减后结算**:逐人日终结算(候选=batch 当日活跃 ∪ 当日空间事件,C-N1) | 「好感度衰减 …」「好感度结算 {user}:daily delta={n}」「好感度结算失败 …:用户消息不足 3 条,顺延」 |
| daily_decay | 24h | 自然衰减(单独注册,与日终同 tick 顺序由 daily_settle 内部先调用保证) | 「好感度衰减 {user}:delta={n}」 |
| sleep_tick | 60s | 睡眠状态机:自然醒(now≥wake_at→wake+回顾+补跑结算+日记补注)/ 静默关=窗口起点直接入睡 / 静默开=安静满 N 分钟入睡检查 / 窗口终点未入睡→补执行入睡任务(日程+日记)/ 醒态兜底补注待回注日记(失败下轮重试) | 「自然醒来: {t}」「QQ空间日记醒来补注完成」「睡眠窗口起点已到(静默睡眠关闭),直接入睡」「静默入睡:安静 {n} 分钟」「睡眠窗口已过未入睡:补执行入睡任务(不入睡)」 |
| schedule_tick | 60s | 日程窗口触发:greeting→主动问候;daily→门槛过滤+候选流排序→proactive.trigger | 「主动问候触发[{day}] -> {user}」「主动触发[{day}] -> {stream}:{活动}」 |
| remind_fallback | 5 分钟 | 备忘提醒兜底注入(仅无生成日程日;睡眠期跳过) | 「备忘提醒兜底注入(stream={id}):{content}」 |
| qzone_poll | max(poll_interval_minutes,1)×60 s(默认 15 分钟;统一时间线架构下即**发现层间隔**——每轮 1 次发现层调用,仅对发现的新动态按好友充实,量与好友总数无关) | 空间窗口(kind=daily 且 qzone=true)内发现层统一时间线→新动态按作者分组充实→去重入队→串行注入 `qzone-qq` 虚拟流(发现层失败告警回退旧逐好友路径);窗口切换收泵回退未读+评论频控计数重置(v0.7);睡眠期/模块停用跳过;长 IO(发现/充实 HTTP)在后台任务执行,不阻塞调度器 tick(深度审查 A-2,防重入标记跳过重叠轮) | 「QQ空间窗口开始,注入泵激活」「QQ空间新动态入队 {n} 条」「QQ空间动态已注入(tid=…,作者=…)」「QQ空间统一时间线拉取失败,回退逐好友旧路径」「QQ空间浏览窗口结束,浏览队列回退未读({n} 条);通知队列保留等待注入」 |
| qzone_notify_poll | max(notification_interval_seconds,30) s(默认 120 秒) | 统一通知通道(M2.1,替代 M2 评论轮询):双源高频检测(源A=自己说说新评论/源B=近 30 天评论过的好友的楼中楼新回复)→P1 优先级队列插队注入→bot 回复路由为楼中楼;始终运行(醒着即可,与浏览窗口无关),单轮≤3 条,过旧通知截断;睡眠期/开关关闭/模块停用/awaiting 占用时跳过(debug 日志);长 IO 后台执行不阻塞调度器 tick(深度审查 A-2) | 「QQ空间通知入队 N 条(源A+B,P1 插队)」「QQ空间通知轮询源A失败,本轮跳过」「QQ空间通知轮询源B失败(好友 …),该好友跳过」「QQ空间评论过旧跳过(…)」「QQ空间登录态失效(通知轮询源A/源B…),cookie 已作废,下轮重取」 |
| qzone_data_prune | 24 h | qzone 数据保留期清理(深度审查 D-1):`qzone_comments` 30 天+`qzone_feeds` seen 行 7 天(queued 行不动) | 「QQ空间数据清理:评论去重 {n} 行,seen 保留 7 天」;失败:「qzone_feeds 清理失败」 |

### 6.3 睡眠全链路

```
睡眠窗口(23:00 起点 ~ 07:30 终点,即可入睡时间)
 ├─ 睡眠窗口内:bot 出站晚安短句(≤12 字,含睡/晚安/安眠/就寝)→ sleep_confirm LLM → SLEEP → 入睡(与静默开关无关)
 ├─ 静默关:窗口起点已到 → 直接入睡(「睡眠窗口起点已到(静默睡眠关闭),直接入睡」)
 └─ 静默开:窗口起点后安静满 silent_sleep_minutes 分钟(基准 = max(窗口起点, 最后活动))→ 静默入睡
入睡(任意通道)→ 计算 clamp 醒来时刻 → sleep_state.json 落盘 →「已入睡:醒来 {t}」
 → spawn 入睡任务:次日日程生成 + 日记生成(qzone_diary 旁路 LLM,基于当日素材)→ 日记经发布 API 深夜直发
   →「次日日程已生成:…」「QQ空间日记发布成功: …」→ 正文存 qzone_pending_diary.json 待醒来回注
未入睡(静默开且一直有活动)→ 窗口终点:不入睡,补执行入睡任务(每窗口一次)→「睡眠窗口已过未入睡:补执行入睡任务(不入睡)」
睡眠期间:一切入站消息被拦截进缓冲(sleep_review_buffer.json)+ abort;所有调度与 hook 空转(入睡任务的旁路 LLM 与发布 API 不经消息链,不受影响)
醒来(now ≥ wake_at)→「自然醒来」→ 状态置 awake
 → 日记补注(self 消息入虚拟流,失败醒态 tick 重试)→「QQ空间日记醒来补注完成」
 → 睡醒回顾(可选):按流 LLM 摘要 + 到期备忘静态附列 →「睡醒回顾已生成: {path}」
 → 补跑当日结算(内部先衰减后结算)→「好感度结算 …」
```

### 6.4 结算/衰减链路

```
计数(after_process)→ check_trigger(SUM≥20 且 early 当日<3)→ spawn 结算(early)
结算:聚合该人全部流消息(每流取 50 条,群聊解析 quote 发送者)→ build_material(窗口过滤/锚 30 条/邻居/截断)
 → 旁路 LLM(稳定段:人设+风格+5 级规则;变量尾:素材)→ 解析/钳制 delta → apply_delta(+注记截断 40 字)
 → 升特别被占位 → 钳 99/挚友(clamped_exclusive,「结算升特别被独占钳制」)
 → 落库+日志 → reset_batch(该人全流清零)
日终(daily_settle 调度):iter_today_active(当日有消息)∪ 当日 qzone_fav_events 的 user(纯空间互动好友,C-N1;排除 bot 自身)→ 未日终者 → 结算(daily;素材不足 daily_settle_min → 顺延不清零)
衰减(daily_decay):扫 favorability score>0 全表 → 跨流取最近 bot 直接互动(@/quote 解析)
 → 基准 = max(互动时间, 最近 decay 判定时间)→ 超 decay_after_days → LLM 判定(-decay_max~0)
 → apply_delta(judge_id=decay-{时间}-{user})→「好感度衰减 {user}:delta={n}」→ 基准即被重置
```

---

## 7. 已知限制与观察项

1. **reply 补传实机少触发(thinking 模型)**:补传三条件要求「调用过上下文工具 ∧ reply_reference 为空 ∧ reasoning 为空」;thinking 模型恒有推理文本,第三个条件实机几乎不满足(验收中 memo_read 调用后按设计跳过),行为由单元测试守护,非缺陷。观测:主程序日志无「reply 补传」不代表失效。
2. **quote 解析依赖主机能力 `message.get_by_id`**:reply 段实机为纯消息 id 不含发送者;能力缺失/调用失败/返回无 user_id 时该条按未 quote 命中(群聊防误判退化为仅 @ 判定),每轮至多一条告警「quote 发送者解析失败(stream=…)」。
3. **衰减恒不触发独占钳制**:衰减 delta 被强制 ≤0(正 delta 直接拒绝),而独占钳制只发生在升入「特别」(正 delta 场景),故衰减路径 `exclusive_clamped` 恒 False 属设计必然。
4. **回复/内容保真由 LLM 决定**:备忘内容、日程活动、关系注记、主动发言话术的语义保真均取决于模型;如用户说「记得喝水」,模型工具传参可能记成「看书」——插件只做格式/长度校验,不校验语义。
5. **睡眠期到期提醒不补执行**:过期提醒仅在睡醒回顾报告末尾静态列出,醒后不补注入。
6. **首日无生成日程**:启动当日用默认作息模板撑场(不生成当天日程),主动发言/备忘兜底按模板日路径工作,当晚入睡确认才生成次日完整日程。
7. **触发即计数**:`daily_speak_limit` 在 trigger 调用成功即计 1,主程序最终保持沉默也消耗配额。
8. **重启后当日窗口可能重复触发**:`_schedule_tick_fired` / `_speak_counts` 为内存态(仅跨天清理),重启恢复当天 schedule.json 后,重启前已触发过的窗口可能再次触发且配额清零;备忘兜底去重(`remind_fired.json`)是持久化的,不受影响。
9. **缓存命中率现状**:实测 57.91%,断点在主程序每轮变化的动态块(时间/记忆检索),插件侧纪律已全部落实(详见 `docs/cache-baseline.md`)。
10. **sleep_confirm 判定模型固定 memory**:晚安判定器不随配置走,不可换 task。
11. **静默入睡计时口径**:入站消息(非睡眠期)与出站回复(经 replyer after_response)刷新 `_last_activity_ts`;长时间仅有 bot 单方活动(如纯主动发言)时计时可能不刷新。
12. **bot_user_id 留空的连锁影响**:结算素材中 bot 发言被当作普通用户素材、群聊 @/quote 归属与 bot 消息随附全部失效、衰减互动判定失效——实机必须配置。
13. **群聊说话人近似**:注入与候选流按「最近非 bot 消息发送者」解析(取近 3 条),群聊换人即换好感度块(缓存分层预期内)。
14. **poke 无好感度门槛**:主动戳只受每用户 600s 冷却限制(联调裁定,如后续需要恢复门槛需改动代码)。
15. **备忘按人跨流可见(M2 已交付)**:条目=主QQ+附带QQ(≤5),任一牵连 QQ 命中当前对话对象即可见(原 I-1 跨流不可见问题已随 M2 memo 按人重构解决);附带 QQ 由写入时显式传参或从上下文推断,语义保真由模型决定。

---

## 8. 公测注意事项

### 8.1 启用与配置恢复基准值

1. 插件目录放入 `plugins/` 并重启;WebUI「插件」页确认 `catsitate.core` 已加载,日志出现 **`catsitate_core 已加载`**。
2. 打开 `plugin.enabled = true`(总开关,默认关);按需调整各模块节。
3. **必须配置**:`favorability.bot_user_id` = bot 自身 QQ 号(实机 3545773341)。留空则好感度判定/衰减/注入的 bot 识别全部失效。
4. **旁路模板自动部署**(无需手动):插件加载时自动把 `prompt_templates/catsitate_*.prompt`(10 个)同步到主程序 `prompts/zh-CN/`(内容一致跳过、变更覆盖;主程序 `load_prompts()` 在插件启动后调用,同次启动即生效,无需重启)。此后 WebUI「提示词管理」页显示并可编辑这 10 个模板(编辑产物写 `data/custom_prompts/zh-CN/`,插件优先读取);插件不在 `plugins/` 下或主程序 `prompts/zh-CN/` 目录缺失时跳过并告警(日志「旁路模板自动部署跳过」),插件回退内置默认,功能不受影响。
5. 插件旁路 LLM 使用主程序内置 task 名(如 `planner`/`memory`/`replyer`),不支持新增自定义 task;主程序 task 集合固定(replyer/planner/memory/mid_memory/utils/learner/expression_use,WebUI「功能分配」页可见),各能力 `llm_model` 填对应 task 名。
6. 慢模型建议配置超时:utils 实测 31-53s 会触发默认 30s 超时,`image_relook.llm_timeout_ms` 建议 120000。
7. 验收/短时测试期间改动过的临时值必须恢复基准:`early_settle_threshold`=20、`silent_sleep_minutes`=60、`decay_after_days`=7、`window_hours`=24、`daily_speak_limit`=5、`max_regenerate`=1。
8. 热重载:WebUI 修改配置后 `on_config_update` 生效,日志「**catsitate_core 配置已刷新,派生缓存已重置**」;weather/daily_settle 调度周期随配置重注册。
9. 复审观测建议:打开 `debug.enabled` → 数据目录 `logs/catsitate-{date}.log` 落盘插件 debug 级日志(注入构造/插入位置等),关闭/文件失败自动回退主日志。

### 8.2 观察日志关键词(按功能)

| 功能 | 应出现的关键日志 |
|---|---|
| 加载成功 | `catsitate_core 已加载` |
| 注入 | `注入完成: 插入 N 条(system 尾 …)`(debug);告警:`注入定位失败:items 中无 SystemMessageItem,已回退追加尾部` |
| 好感度结算 | `好感度结算 {user}:early/daily delta={n}`;失败:`好感度结算失败 …`;钳制:`结算升特别被独占钳制(user=…)` |
| 衰减 | `好感度衰减 {user}:delta={n}`;`衰减判定 LLM 失败(user=…)` |
| 睡眠 | `已入睡:醒来 {t}`;`睡眠窗口起点已到(静默睡眠关闭),直接入睡`;`静默入睡:安静 N 分钟`;`睡眠窗口已过未入睡:补执行次日日程生成(不入睡)`;`自然醒来: {t}`;`睡醒回顾已生成: {path}` |
| 日程 | `次日日程已生成:…`;`次日日程生成:{err}(模板兜底)`;`已从 schedule.json 恢复日程({date})`;`schedule.json 为过期日程…删除并忽略恢复` |
| 主动发言/问候 | `主动触发[{day}] -> {stream}:{活动}`;`主动问候触发[{day}] -> {user}`;`主动问候跳过:特别者({user})无私聊流` |
| 备忘提醒 | `备忘提醒兜底注入(stream={id}):{content}`;`备忘清理:{n} 条过期` |
| reply 补传/哨兵 | `reply 补传:[…]`;`哨兵判定:放行回复` / `哨兵判定:撤回回复:{reason}` |
| 旁路记账 | `旁路 LLM 当日调用次数已达或超过阈值 {n},请注意用量`;llm_usage 表按模块分列 |
| quote 解析 | `quote 发送者解析: 成功 {n}/{m}(stream=…)`;`quote 发送者解析失败(stream=…):…` |
| QQ空间 | `QQ空间窗口开始,注入泵激活`;`QQ空间新动态入队 {n} 条`;`QQ空间动态已注入(tid=…,作者=…)`;v0.7 工具:`QQ空间评论失败(feed_id=…)`、`QQ空间楼中楼回复失败(feed=…,comment=…)`、`QQ空间点赞失败(tid=…)`;告警/停用:`QQ空间模块停用:…`、`QQ空间模块停用:person 别名折叠自检失败(…)`、`QQ空间网关收到意外出站回调(…)`(receive 网关防御分支)、`QQ空间场景回退:…`、`QQ空间说说拉取失败(uin=…),该好友本轮跳过` |

### 8.3 常见问题排查

| 现象 | 排查路径 |
|---|---|
| 插件未加载 | 检查主程序版本满足 manifest(min 1.2.0)、插件目录是否在 plugins/ 下、重启后日志有无 on_load 报错(配置错误会拒绝加载,不静默) |
| 无节日/农历信息 | 日志「lunar-python 未安装:农历节日/节气不可用」→ 安装依赖;「holiday-cn 数据源…获取失败」→ 网络受限时回退链自动生效(库/内置表),环境块仍含日期与城市 |
| 无天气 | 「天气获取失败,本轮环境块省略天气」→ Open-Meteo 可达性/城市坐标是否正确(默认珠海 22.279410,113.528098) |
| 旁路 LLM 报「未找到名为 … 的模型配置」 | `llm_model` 填了模型标识而非 task 名;改为 model_task_config 节名 |
| WebUI「提示词管理」看不到 8 个旁路模板 | 插件加载时自动部署(无需手动);看不到则查日志有无「旁路模板自动部署跳过」(插件不在 `plugins/` 下或主程序 `prompts/zh-CN/` 目录缺失/未识别)→ 插件放回 `plugins/` 后重启;重启后仍看不到再查「旁路模板 … 部署失败」写入告警 |
| 旁路调用超时 | 慢模型超时默认 30s(`*_timeout_ms` 填 0=默认);按 §8.1 配置 120000 |
| 好感度一直不结算 | 检查 bot_user_id 是否配置;素材为空判定「素材为空,跳过结算」;daily 素材不足 3 条「顺延」属预期 |
| 睡眠中一切无响应 | 绝对静默设计预期,不是故障;入站消息被拦截记入回顾缓冲,醒来生成报告 |
| 日程不是预期作息 | 默认作息为软基准,LLM 结合当天活动自主排布;生成失败会有模板兜底告警;可用 update_schedule 工具修改 |
| 主动发言没有出现 | 检查 speak_threshold_level 门槛(默认熟悉)、近 24h 活跃流、daily_speak_limit 配额、当日是否模板撑场、睡眠期跳过 |
| 数据异常想重置 | 删除 `data/plugins/catsitate.core/catsitate.db` 与各 JSON 快照(重启重建);注意 favorability 重建即清零(开发期裁定不做迁移) |
| QQ空间没有动态注入 | 查启动日志有无「QQ空间模块停用」(focus_mode 开启 / talk_value=0 / bot_user_id 为空 / person 自检失败)与「QQ空间虚拟平台就绪」;确认日程有 qzone 标记的 daily 窗口且当前处于窗口内;「QQ空间说说拉取失败(uin=…)」→ NapCat 可否响应 `adapter.napcat.account.get_cookies`;msglist 条目即说说(转发走回退链/纯图以图段承载/视频占位);WebUI 自定义过 schedule_generate 旧模板需手动同步(§3.13.8) |
| 空间评论/回复/点赞没发出 | 工具返回「未找到说说 …」= 目标三级解析全miss(registry 过期+seen 超窗+非当前浏览项),让 bot 核对消息尾部〔〕参数行里的说说ID 或等下轮注入;「这条说说你已经评论过 3 次了」= 同说说频控上限(窗口边界重置);「QQ空间评论失败/楼中楼回复失败/点赞失败」→ 多为登录态失效(看有无「cookie 已作废,下轮重取」,自愈不重试);虚拟流里 bot 打字发不出去属设计(receive 网关,动作只能经工具);统一通知不出 → 确认 comment_poll_enabled 开启、bot 醒着(睡眠期静默)、上一条通知未被 awaiting 占用(不叠加)、bot 有历史说说且有人评论(源B还需 bot 曾评论过好友说说) |
| 复审证据链 | 打开 debug.enabled 落盘日志;`catsitate.db` 查 `llm_usage`/`favorability_log` 核旁路记账与结算/衰减记录;`schedule.json` 查日程修改历史;`sleep_review/reports/` 查回顾报告 |

---

*本文档面向公测使用方与复审人员;功能描述以当前代码为最终依据,规格文档(各期设计)与实现存在差异处以本文与代码为准。*
