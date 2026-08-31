# Catsitate 三期设计规格（QQ 空间模块）

> 一期基线：docs/superpowers/specs/2026-08-14-catsitate-core-maibot-design.md；二期基线：2026-08-15-phase2-design.md（均已验收，本规格不改既有模块行为，memo 作用域重构除外——见 §3.10）
> 本规格经 grilling 对齐（2026-08-30，Q1–Q22 二十二项决策 + 九路源码调查，结论锚点见 §11）。
> 2026-08-30 冲突审计修订（调查⑦入站链路/⑧出站与共存/⑨记忆机制）：强制触发、轮完成感知决策窗口、模块豁免集、记忆立场、platform 方案 B 否决——见 §2.17–2.19 与各模块修订。
> 目标清单出处：Catsitate-Core-MaiBot开发目标.txt §2.2。

## 1. 范围与里程碑

QQ 空间四功能（阅读好友动态 / 点赞 / 评论 / 发说说 + 记日记），实现为**虚拟聊天平台**复用主程序聊天流程：

- **M1 感知（0.4.0）**：协议客户端（读）+ cookie 链路 + 虚拟流网关 + 串行注入 + seen 去重 + `qzone` 窗口属性接入日程。
- **M2 互动（0.5.0）**：出站意图状态机 + 评论路由（含楼中楼）+ 点赞工具 + 窗口外评论轮询 + 好感度联动 + **memo 按人重构**（前置项，§3.10）。
- **M3 表达（0.6.0）**：说说主动发布 + 日记入睡直发 + 回注 + 真实聊天见闻摘要注入。

各里程碑独立可验收：M1 = 浏览窗口内 bot 在虚拟流「看到」动态且主链可引用；M2 = 评论/点赞经真实 API 落地；M3 = 空间出现 bot 的人格化输出。

## 2. 架构基石与全局决策

1. **虚拟聊天平台**：`@MessageGateway(route_type="duplex", platform="qzone-qq")` 声明网关组件（**平台名为常量 `qzone-qq`，不入配置**——连字符别名经 `get_person_id` 折叠进 `qq` 人物命名空间，实现与真实 QQ 聊天的 person 统一，见 §2.17）；`on_load` 后 `ctx.gateway.update_state(ready=True, account_id=<bot 真实 QQ>)`。每条说说经 `ctx.gateway.route_message()` 注入——与真实 adapter 消息**同一入口**（`chat_bot.receive_message`），完整走 hook→heartflow→planner→replyer 链。`update_state` 的 account_id 会将虚拟平台记为 bot 账号，`is_bot_self` 对虚拟流生效（bot 回注自己的说说可正确标记 self）；account 用真实 bot QQ，经同样折叠统一 bot 自身 person。
2. **单一虚拟群聊流**：全部好友动态按时间线交错进同一个伪群流（`is_group_session=bool(group_id)` 判群，主程序不校验平台真实性）；流内消息 user_id=好友 QQ、昵称=好友昵称；好友在他人说说下的评论以「评 XX：」前缀的独立消息呈现；队列全局按发布时间升序（补叙式阅读）；bot 自己的说说以 self 消息回注。**时间语义（方案 B，用户裁定 2026-08-31）**：注入时间戳=**阅读时刻**（注入时刻——消息流时钟单调递增,主程序时序机制（get_recent 24h 窗/间隔样本/连发过滤）消费正确的到达语义）；**发布时间由正文相对时间前缀承载**（今天 HH:MM / M月d日 HH:MM,防 bot 把老说说当刚发生,联调缺陷#5）；说话人解析用注入泵当前作者（§2.16 交叉校验）。**注入消息必须带 `additional_config.is_mentioned=1.0` 强制触发**（见 §2.18）；message_id 全局唯一（tid+序号派生，去重键=driver_id:message_id）；网关须声明 supports_receive 且 ready 后才可注入；虚拟流由首条消息建立后 proactive 能力才可用。
3. **图片交主程序**：适配器拉取动态图片后作为消息图片段随说说注入，主程序图片摘要/重看链路原生接管（比 Maizone 的旁路 VLM 描述多拿到重看级能力）。**注入 dict 的 image 组件必须带 `binary_data_base64`**——只给 hash 时主流水线静默不描述、不落 Images 表（inspect_image 前提失效）；**体积治理=压缩到 RPC 帧限内（用户裁定 2026-08-31 终案）**：不设质量上限,但图片 base64 总量超过 RPC 帧预算（12MB,物理帧 16MB 留开销）时按压缩阶梯（PIL 降分辨率×降质量）收紧至达标;极端不达标丢弃最大图保帧并告警;下载失败的图以 `[图片]` 占位。主程序入站链路的压缩/丢弃仍在其后兜底。
4. **串行注入 + 出站意图状态机**：浏览窗口内**一次只注入一条动态**，注入后等**轮完成信号**（见下），期间 planner 自由沉默/回复/点赞；轮完成后（或超时兜底）再注入下一条。插件在每个阶段前置设定出站意图，duplex 驱动按意图调空间 API（详见 §3.3）。出站意图一次性消费——首个动作成功即置空，后续出站按无意图拒发（M2 实现，2026-08-31；原「多次评论放行」语义废弃）；窗口结束未消费意图作废并记日志。
   **轮完成信号（冲突审计修订）**：`maisaka.planner.after_response` 且 `output_items` 中无 tool_calls——此后本轮不再有模型调用/出站；wait/switch_chat 暂停是「假结束」（timeout 后同 logical_turn_id 续轮），视为决策窗口延长；`decision_window_seconds` 从固定间隔**降级为超时兜底上限**（默认 75s，需大于最坏 planner 轮延迟）。**wait 态精确规则（回顾修订）**：`wait` 本身是 tool_call，其所在响应不满足完成信号；超时兜底触发时若流处于 wait 态→**不注入下一条**，继续等 wait 超时后续轮的完成信号，硬上限 `3×decision_window_seconds` 后才强制推进并告警（防错靶：wait 期间注入的消息不触发轮、只会在 wait 结束后并入批处理）。planner 打断默认关闭（`planner_interrupt_max_consecutive_count=0`），「等上一轮结束再注入」与主程序天然兼容。
5. **`qzone` 为日程窗口属性（非 kind）**：窗口结构增 `qzone: bool`（默认 false）；**仅 `kind=daily` 窗口可标记**（校验一条：睡眠/greeting 窗口标记即拒绝）；属性=窗口期状态（拉取/注入/反应通路随窗口开合，非起点事件）；表达触发每个标记窗口至多一次，时机=该窗口**首轮拉取完成后**（确定性规则，不掷骰子）。`schedule_generate` 模板升 v3：告知 LLM 可为日常窗口标记空间属性，引导一天 1~2 个，不设硬校验。
6. **混合驱动 + 频率可配置**：窗口内按 `poll_interval_minutes` 拉取；**窗口外仅评论轮询**（`comment_poll_enabled` 默认开，`comment_poll_interval_minutes` 默认 30）——只轮询「自己说说下的新评论/回复」，刷到即注入并回复；好友新动态窗口外不感知。睡眠窗口内绝对静默沿用（评论轮询同样拦）。
7. **无行为护栏**：所有空间动作均为模型决策（planner 决定评/赞/说，replyer 生成内容），频率受窗口与触发时机结构性约束，不设每日计数器/最小间隔。唯一工程纪律：**空间动作 API 失败不自动重试循环**，告警后跳过（「错误显式暴露」原则延伸）。
8. **说说发布与主动问候语义解耦**：说说发布 = qzone 窗口表达触发 → `ctx.maisaka.proactive.trigger(虚拟流, intent=空间表达指示)`——只用主程序 API 通道，不与主动问候的「特别」等级/私聊流/`daily_speak_limit` 语义耦合，不占其预算。是否真说、说什么由主链决定；planner 沉默则本轮不发布（触发即消耗该窗口表达机会）。
9. **日记 = 入睡任务旁路直发**：入睡确认瞬间（生成次日日程的同任务）顺带一次旁路 LLM 调用生成日记 → `do_add_content` 直发（深夜时间戳=睡前写日记的真实感）→ 回注虚拟流 self 消息。日记与说说分工：日记=睡前视角+当日素材回顾（日程执行/聊天见闻/空间见闻/备忘），说说=浏览窗口中的碎片表达。
10. **好感度联动**：用户在 bot 说说下评论/点赞 = 小幅好感度事件（素材并入日终结算，不即时结算）；虚拟流消息经既有 inject 框架携带各自主的好感度上下文（复用 2.3 群聊多好感度方案）。权重档位见 §9。
11. **场景提示词替换（虚拟流专用）**：主程序群聊场景文本来自配置 `chat.reply_style.group_chat_prompt`（选择条件仅 `is_group_session`，platform 不参与）。插件经 `config.get` 读该配置**当前值**，在 planner（现有 `maisaka.planner.before_request` handler）与 replyer（新增 `maisaka.replyer.before_model_request` handler，`before_request` 不带 items 改不了 system）两侧的 system 文本中做**精确字符串替换**——群聊场景原文原位置换为空间场景文案（§9 提案）。按 `session_id` 过滤只作用虚拟流；匹配失败（主程序模板改版等）→ 告警 + 回退追加语义覆盖块。群聊版记忆规则（「不要把私聊隐私带到群聊」）**保留不动**——说说是半公开场域，该规则有益。
12. **虚拟流工具白名单 = 三层防线 + 配置项**：
    - **广告层**：`planner.before_request` handler 在场景替换的同时按白名单过滤 `tool_definitions`（已确认实际生效：改写列表整体替换发给 LLM 的 tools 参数）＋剥除 items 中 deferred 工具 `<system-reminder>`＋白名单排除 `tool_search`（堵发现旁路）；
    - **执行层**：我方 QQ 专属工具（msg_react/poke_user）体内加平台自检（`platform=="qzone-qq"` 即拒绝）；我方空间工具声明 `allowed_session=["qzone-qq:<伪群号>"]`（注册表级硬门控，列举/执行两侧生效——执行侧不校验广告集，此为唯一防「猜名绕过」的现成机制）；
    - **终局层**：duplex 驱动对「无出站意图或非文本消息」一律告警拒发，兜住内置工具（send_emoji 等）的越界输出。
    - 白名单本身为配置项 `qzone.tool_whitelist`（默认值见 §5）；**执行层硬门控与自检不随配置放松**（安全下限不交给配置）。
13. **cookie 链路**：主路径经 adapter API `ctx.api.call("adapter.napcat.account.get_cookies", params={"domain": "user.qzone.qq.com"})`（单关键字透传,adapter API 形态，联调实证）；返回 cookies 容忍 dict/字符串两种形态；业务码 -3000/-10005 → QzoneAuthError → cookie invalidate 下轮重取（失效自愈）；cookie 持久化插件 data 目录（JsonSnapshot 模式），刷新节流（1 小时内跳过）；获取/失效**显式告警**（Maizone 静默降级为反面教材），失效后下轮拉取前重取。不实现扫码/直连 HTTP 降级链（如生产需要另行评审）。
14. **协议层自研**（蓝本 Maizone 3.0.2，2025-10 停更，仅作参考不作依赖；**联调实证修正 2026-08-30**）：`emotion_cgi_msglist_v6` 为**指定用户说说列表**（`uin=目标`、jsonp+`need_comment=1`、Referer 指向目标空间，响应顶层 `msglist`，条目含 tid/created_time/content/pic[].url1/commentlist）——**不存在好友聚合 JSON 端点**（vFeeds 形态系调查期误记，Maizone 的 read_feed 本就是逐指定人读取）；好友列表走 adapter OneBot API `adapter.napcat.account.get_friend_list`（remark 优先作昵称），拉取=逐好友 `get_user_feeds(num=3)`+好友间 2s 间隔（防风控）；发布 `emotion_cgi_publish_v6`、评论/楼中楼 `emotion_cgi_re_feeds`、点赞/取消 `internal_dolike_app`、传图 `cgi_upload_image`；鉴权=`p_skey` cookie + `g_tk`(hash33)+**浏览器 UA**（无 UA 空间 500 空体，联调实证）；响应 `callback(...)` 包裹截取解析。HTML 聚合路径（feeds3_html_more）**弃用**。msglist 条目即说说（appid=311 语义由 M2 互动路径沿用）。
15. **去重两层状态（回顾修订：seen=成功注入进 planner 上下文）**：注入成功才标 `seen`（此后进主聊天摘要注入、不重复注入）；拉取时仅标 `queued`；**qzone 窗口结束仍未注入的队列条目回退为未读**（下个窗口可见）并记日志计数——若拉取即标 seen，窗口尾被丢弃的动态会永久丢失（既没看也没机会再看）。点赞/评论过另记 `interacted`。键=`tid`（辅助 `abstim`），存 SQLiteStore 新表（on_load 建表惯例，幂等主键）。
16. **memo 按人重构（M2，全模块语义变更）**：备忘条目**不再依赖 stream_id**，以「主 QQ + 附带 QQ 列表」的人维度组织；可见性=任一牵连 QQ 命中当前对话对象。取数点：私聊用官方工具 kwargs `user_id`（可靠=对端 QQ）；群聊/虚拟流自建——`chat.receive` hook 维护 `stream_id → 最近发言者QQ` 映射（payload 原始消息含 `user_info.user_id`，群聊不抹除；插件 fav_count/sleep_gate 已走此链路），`get_recent` 回溯（现有 `_resolve_speaker`）作兜底（时间窗放大，§9）；qzone 串行注入的意图状态机天然知道当前动态作者，作交叉校验。
17. **记忆立场（Q22 终案：person 统一 + 结构隔离，冲突审计二次修订）**：虚拟平台名 `"qzone-qq"` 利用主程序官方别名机制（`get_person_id` 对含 `-` 的平台名取连字符后段（split 后第 2 段，如 qzone-qq → qq）后命名空间，person_info.py:48-54）——**person_id 折叠为 `md5("qq_"+QQ)`，与真实 QQ 聊天同一个人**：画像/事实账本/印象跨流聚合共享，空间流的 `query_person_profile` 与画像注入直接命中统一账本，空间互动学到的人物记忆写进同一人（内容来自好友真实说说，是统一而非混杂）。**路由/账号按原始字符串 `qzone-qq` 分键**——BotPlatformAccount、is_bot_self、session_id、路由表全部与真实 qq 平台零接触（方案 B 的 observed 短路/出站劫持路径不存在，其否决结论继续有效）。**启动自检**：验证 `person.get_id("qzone-qq", 探针QQ) == person.get_id("qq", 探针QQ)`，**不等/返回异常形态/调用异常 → 显式告警并停用 qzone 模块**（用户裁定 2026-08-30：人物分裂不可接受，折叠失效宁可不用，不做降级分裂模式；自检同时校验返回为非空 str 防双侧同形失败的假阴性）。学习器仍按 session 隔离（表达风格属聊天风格，不互染）；中期记忆仍按 chat 隔离（空间流聊天回忆留在流内，同「两个真实群」语义）；**不要配置 `*:*` 全局表达共享组**（手册注明）。
18. **注入强制触发（Q21=a）**：注入消息带 `additional_config.is_mentioned=1.0`，绕过回复阈值与空闲退避，保证「一动态一轮」的串行模型完整性；「刷到但懒得理」由 planner 自主沉默（no-tool）表达——意愿判断权归模型。**is_mentioned 不能穿透的三道门另行处理**：focus 槽与 talk_value=0 列为**生产前置条件**（启动检测：`experimental.focus_mode` 非 false 或回复频率为 0 时告警并停用 qzone 模块）；wait 态视为决策窗口延长（等 wait 超时或轮完成信号）。
19. **模块豁免集（冲突审计修订，A 组八条）**：①晚安判定按 session_id 豁免虚拟流（深夜短评论不得触发全局入睡）；②fav_count 豁免虚拟流（好友发自己的说说≠与 bot 互动；空间好感度走 §3.9 显式事件路径）；③daily 窗口候选流查询排除虚拟流（防计划外空间发言）；④日记回注**延迟到醒来后补注**（睡眠拦截与回注同入口，睡眠中 route_message 会被 abort 进回顾缓冲；日记 API 直发不受影响）；⑤流缓存刷新纳入 platform="qzone-qq"（SDK get_all_streams 默认只取 "qq"）、`_stream_is_group`/结算素材语义按群修正（否则虚拟流被当私聊处理）；⑥图片注入带 base64+插件侧限体积（§2.3）；⑦duplex 驱动对出站消息**取 text 段（多段拼接）为评论内容**——reply/at 组件不拒发（引用是自然行为：reply 段的 `target_message_sender_id` 用于意图交叉校验，at 段忽略）；**含 image/emoji 二进制段的消息一律 FAILED+告警**（无映射且有 16MB 帧风险——驱动序列化默认含二进制）；⑧工程门（message_id 唯一/supports_receive/流先建立）。另：sentinel（默认关）开启时会作用于虚拟流出站（每条多一次旁路 LLM），手册注明成本。

## 3. 模块设计

### 3.1 协议客户端（`catsitate_core/qzone/client.py`）

- 纯 httpx 逆向网页 cgi（六端点见 §2.14），请求带 cookie + `g_tk`；响应 callback 截取 + JSON 解析；解析失败显式告警（**禁 eval 式解析**，Maizone 反面教材）。
- `get_user_feeds(*, target_uin, nickname, num=3)`：指定好友说说列表（含 tid/abstim/uin/正文/图片 URL 列表/评论摘要,联调实证参数集）；`get_own_feed_comments()`：自己说说下的新评论（M2 评论轮询用）；`do_comment(tid, content, replyid=0)`、`do_like(tid)` / `undo_like(tid)`、`do_publish(content)`（M3）；`download_image(url)`（注意 qzone 图床防盗链 headers;体积治理=压缩到 RPC 预算,见 §2.3）。
- cookie 管理：`adapter.napcat.account.get_cookies` → 持久化 → 节流刷新 → 失效告警。
- 配置：`request_timeout_ms`（默认 10000）、`max_retries`（默认 0，失败即告警跳过）。

### 3.2 虚拟流网关与注入器（`catsitate_core/qzone/gateway.py` + `feed_injector.py`）

- 网关组件声明（duplex）；`on_load` 后 `update_state(ready=True)`，失败显式告警。
- 注入泵（qzone 窗口期激活）：拉取 → seen 去重（msglist 条目即说说,转发/视频走回退链——[转发自XX]原文/[视频] 占位）→ 图片下载（失败占位+告警；带 `binary_data_base64`，体积治理=压缩到 RPC 帧预算（§2.3））→ 入队（全局按发布时间升序）→ **串行注入**（等上一条的**轮完成信号**——`planner.after_response` 无 tool_calls；wait 暂停视为延长；`decision_window_seconds` 为超时兜底）。
- `message_dict` 构造对齐主程序格式（message_id=稳定去重 id（tid 派生+序号）、platform="qzone-qq"、user_info{user_id=好友QQ, user_nickname=好友昵称}、group_info{group_id=伪群号, group_name=显示名}、is_mentioned 嵌于 message_info.additional_config（主程序只读该位置）、raw_message 组件列表（text+image 段，image 组件结构实现时对齐 `message_utils.py` 的构造器））；**timestamp=阅读时刻（注入时刻）；发布时间由正文相对时间前缀承载**（abstime 非法时不加前缀,debug 日志可观测——方案 B,2026-08-31）。
- 发布时间以相对时间前缀写入正文；消息 timestamp=阅读时刻（方案 B）——两者语义分离,注入块「当前浏览状态」行不承载时间。

### 3.3 出站意图状态机 + duplex 驱动（`catsitate_core/qzone/outbound.py`）

- 状态：`IDLE / AWAIT_REACTION(feed_tid) / AWAIT_COMMENT_REPLY(feed_tid, comment_id) / AWAIT_PUBLISH`。
- 设置点：注入动态前→`AWAIT_REACTION`；注入评论事件前→`AWAIT_COMMENT_REPLY`；表达触发前→`AWAIT_PUBLISH`。
- `PluginPlatformDriver.send_message` 回调实现：按状态调 API——`AWAIT_REACTION`→`do_comment(tid)`、`AWAIT_COMMENT_REPLY`→`do_comment(tid, replyid)`、`AWAIT_PUBLISH`→`do_publish`；**文本提取**=raw_message 的 text 段拼接（reply 段做意图交叉校验、at 段忽略）（M2 交付时经评估推迟：意图一次性消费+窗口开启作废已闭合主要错靶路径,reply 交叉校验留 M3 视风控需要实现,2026-08-31 回写），含 image/emoji 二进制段或 `IDLE` 态出站 → 告警拒发（FAILED）；API 失败 → 告警（不重试循环）。
- 决策窗口结束 → 状态回 `IDLE`，未消费意图记日志作废；wait 态下的推进规则见 §2.4。

### 3.4 场景与上下文注入（`plugin.py` 注入块扩展 + hook 扩展）

- 场景替换：§2.11（planner 现有 handler 扩展 + replyer 新增 `before_model_request` handler；`config.get` 读 `group_chat_prompt` 真值做精确替换——**已核验 `config.get` 读宿主全局配置**（capabilities/core.py:716-737）；保留原 kwargs 键——`item_schema_version` 丢失=修改被静默丢弃，**必须 `{**kwargs, "items": ...}` 展平回传**）。边界情形：用户将 `group_chat_prompt` 留空（场景块为空串）≠ 匹配失败，直接走追加覆盖块，但**仍须告警**（warning 级，每进程节流一次，注明「群聊场景提示词为空，虚拟流以追加覆盖块工作」）——追加即回退，回退必须告警；「配置非空但替换未命中」为另一类告警（模板改版风险）+回退。
- qzone 注入块（inject 框架新模块块，插在日程块之后、备忘块之前）：**虚拟流上**=空间语义说明（动态流语境/回复即评论/点赞工具可用）+当前浏览状态+该好友的按人好感度/关系注记（承担跨域关系连续性，§2.17）；**真实聊天上**=近期已见动态摘要（每条一行，`summary_count`/`summary_days` 控制）。
- 工具白名单过滤：同一 `planner.before_request` handler 内按 `session_id` 判定虚拟流后过滤 `tool_definitions`（元素按 `function.name` 匹配、**原样保留通过项**——重建缺 name 会炸整轮请求）+ 剥 deferred reminder。
- **模块豁免接线（§2.19）**：晚安判定 handler 增加 session_id 豁免（虚拟流的 replyer 输出不进判定）；fav_count 按 platform 豁免虚拟流（platform=="qzone-qq"）；流缓存刷新（`chat.get_all_streams`）显式包含 platform="qzone-qq"（SDK 默认只取 "qq"），`_stream_is_group`/结算素材语义对虚拟流按群修正；daily 窗口候选流查询排除虚拟流 session_id。

### 3.5 日程联动（`schedule.py` 扩展）

- 窗口结构增 `qzone` 字段；校验：仅 daily 合法、解析缺省 false。
- `schedule_generate` 模板升 v3（`SIDE_TEMPLATES` 与 `prompt_templates/` 同步升版，走既有部署链）。
- 窗口执行：`_schedule_tick` 守卫序列照抄（睡眠/已触发/daily_speak_limit 不适用——qzone 属性不触发主动发言，仅激活注入泵与表达机会）。

### 3.6 点赞工具（M2，`plugin.py` @Tool）

- `@Tool("qzone_like", visibility="visible", allowed_session=["qzone-qq:<伪群号>"])`：参数=目标动态标识（从当前浏览状态/消息上下文取）；planner 处理虚拟流消息时自主决定调用；与贴表情同模式（「提供工具让机器人自行在合适时机使用」）。

### 3.7 评论轮询（M2）

- 醒着且不在 qzone 窗口时，周期任务仅拉「自己说说下的新评论」；新评论→注入（「评 XX：」形态，指向 bot 的那条说说作上下文，**同样带 is_mentioned=1.0 与发布时间前缀**）→ planner 回复 → 意图 `AWAIT_COMMENT_REPLY` → 楼中楼 API。睡眠窗口内绝对静默拦截沿用。

### 3.8 发布（M3）

- **说说**：qzone 窗口首轮拉取完成后（**无论该轮有无新动态**——浏览行为本身即表达素材，无新动态也可发），若该窗口未用过表达机会 → `maisaka.context.append(虚拟流, 近期见闻素材段)` + `proactive.trigger(虚拟流, intent=空间表达指示)` → 主链产出 → `AWAIT_PUBLISH` → `do_publish`。沉默则不发布。注：`queued: True` 不代表执行（focus/频率 0 会被吞——生产前置条件覆盖，§2.18）。
- **日记**：入睡任务扩展——生成次日日程的同任务顺带旁路 LLM 调用（模板 `catsitate_qzone_diary.prompt`，进 `SIDE_TEMPLATES` 与部署链）→ `do_publish` 直发（API 不经消息链，不受睡眠拦截影响）→ **回注延迟到醒来后补注**（§2.19④），且回注消息**不带 is_mentioned**（bot 无需对自己的日记强制触发轮，仅入历史供后续轮可见）。**二期语义扩展（须同步 CONTEXT/手册）**：「睡眠期间唯一的 LLM 调用 = 入睡任务」从一次日程生成扩为**入睡任务内的两次调用**（日程+日记）+日记发布 API——绝对静默的例外清单由「次日日程生成」扩为「入睡任务」；窗口终点未入睡的补生成路径同样执行日记（熬夜写日记亦拟人）。
- 回注：bot 说说（含日记）发布后以 self 消息注入虚拟流，保持流上下文完整（后续评论有上下文）；除日记外的说说发布当日若在窗口内可即时回注。

### 3.9 好感度联动（M2）

- **显式事件路径**（fav_count 已豁免虚拟流，§2.19②）：用户评论/点赞 bot 说说 → 该人素材 +小额权重（评论/点赞分开计），并入日终结算；bot 在虚拟流的出站评论对某好友的衰减计时刷新，走同一显式事件（不依赖 batch_counter）。虚拟流消息的好感度上下文注入复用现有群聊方案。点赞的 own-feed 枚举无 API——好感度点赞事件仅 qzone_like 工具路径，轮询不检测（联调实证后收敛，2026-08-31 回写）。

### 3.10 memo 按人重构（M2 前置，独立子项目）

- schema：条目=`主 QQ + 附带 QQ 列表（可空）`，**去 stream_id 依赖**（保留为元数据列，**同时仍是命中条件之一**（流命中 OR 人命中,spec §3.10 实现语义））；`read()` 改为「人维度命中即可见」（任一牵连 QQ = 当前对话对象）；`write()` 增附带 QQ 参数（planner 显式传或从上下文推断，上限见 §9）。
- 取数点：§2.16（私聊官方 kwargs / 群聊自建最近发言者映射 / qzone 意图状态机交叉校验）。
- **流程**：单独一轮语义确认 → CONTEXT 词汇表更新 → TDD → 全量回归（此变更触及全部现有 memo 用例）。

## 4. 数据流与模块边界

- `catsitate_core/qzone/__init__.py`、`client.py`（协议+cookie）、`gateway.py`（网关组件+message_dict 构造）、`feed_injector.py`（拉取/去重/串行注入泵）、`outbound.py`（意图状态机+duplex 驱动）
- `plugin.py` 接线：网关组件声明、hook 扩展（场景替换/白名单过滤/replyer before_model_request/chat.receive 发言者映射）、scheduler 注册（窗口拉取/评论轮询）、@Tool（qzone_like）、注入块（qzone 块）、`_manifest.json` capabilities 增补（gateway 等）
- `schedule.py` 增 `qzone` 属性与校验；`memo.py` 按 §3.10 重构；`storage.py` 增 seen 动态表
- prompt 模板：`catsitate_schedule_generate.prompt` 升 v3（M1）、`catsitate_qzone_diary.prompt` 新增（M3）；说说/评论/点赞**不设旁路模板**（表达权交主程序）

## 5. 配置模型（新 `qzone` 节，中文 label，默认值为提案）

- `enabled`（默认 true；M1 仅读操作，M2 起含写动作——生产部署注意事项注明）
- `poll_interval_minutes`（窗口内拉取间隔，默认 15）、`comment_poll_enabled`（默认 true）、`comment_poll_interval_minutes`（默认 30）
- `decision_window_seconds`（轮完成信号的超时兜底上限，默认 75；须大于最坏 planner 轮延迟）
- `tool_whitelist`（默认 `["wait","reply","query_memory","query_person_profile","memo_write","memo_read","inspect_image"]`；M2 起 `qzone_like` 并入默认）
- `virtual_group_id`（默认 `"qzone_feed"`）、`virtual_group_name`（默认 `"QQ空间"`）
- `summary_count`（默认 5）、`summary_days`（默认 3）
- `request_timeout_ms`（默认 10000）、`max_retries`（默认 0）、`cookie_refresh_minutes`（默认 60）、`speaker_lookup_hours`（get_recent 兜底时间窗，默认 72）
- `diary_enabled`（默认 true）、`diary_llm_model`（默认 memory）、`diary_llm_timeout_ms`（默认 0=主程序默认）

## 6. 错误处理原则（沿用一期 §7）

- 空间 API/HTTP/LLM 失败：显式日志 + 跳过本轮（下窗口/下轮重试）；**不自动重试循环**。读路径例外：图片下载固定单次重试（CDN 瞬态失败实证）；动作 API 的不重试纪律由 max_retries 约束（M2 生效）。
- cookie 失效：告警 + 下轮拉取前重取；持续失败保持告警（每进程节流），不静默停摆。
- 场景替换失败：告警 + 回退追加覆盖块（明示回退路径）。
- 注入/hook 路径异常放行（不阻断主链路）并记录。

## 7. 测试方式

- 引擎层单测（pytest，不依赖主程序）：客户端响应解析（callback 截取/异常）、cookie 节流与失效路径、seen 去重（**注入成功才 seen/窗口尾回退未读**）、注入泵串行节奏（轮完成信号/超时兜底/**wait 态不推进与硬上限**）与timestamp=阅读时刻/发布时间前缀/abstime 缺失可观测（debug 日志）、注入 dict 构造（is_mentioned/base64/唯一 message_id/platform="qzone-qq"/体积限流）、意图状态机全迁移（含 IDLE 出站拒发/**text+reply 混合组件取文本/二进制段拒发**/未消费作废）、场景替换（命中/未命中回退/**空配置走追加且告警**/保留 schema 键）、白名单过滤（元素结构合法/空列表/tools=None）、日程 qzone 属性校验、评论轮询窗口外/睡眠拦截、发布路径（沉默不发布/表达触发不依赖新动态存在）、**日记路径（入睡任务双调用/补生成路径含日记/回注延迟且无 is_mentioned）**、模块豁免（晚安判定豁免虚拟流/fav_count 豁免/daily 候选排除）、**person 别名自检（折叠生效/失效告警停用）**、memo 重构后读写与全量回归。
- 已知工程坑必测：`item_schema_version` 回传、工具元素原样保留、deferred reminder 剥除、`tool_search` 排除。
- 实机验收清单追加三期条目（cookie 获取、虚拟流建流、评论落地、说说/日记发布与回注、真实聊天见闻摘要、生产前置检测告警）。

## 8. 里程碑与交付物

| 里程碑 | 版本 | 交付物 |
|---|---|---|
| M1 感知 | 0.4.0 | client（读+cookie）/gateway/注入泵/seen 表/qzone 属性+模板 v3/白名单+场景替换/单测 |
| M2 互动 | 0.5.0 | 意图状态机+评论路由/点赞工具/评论轮询/好感度联动/memo 按人重构/单测 |
| M3 表达 | 0.6.0 | 说说发布/日记模板+直发+回注/见闻摘要注入/手册+CHANGELOG |

每里程碑：CHANGELOG + `_manifest.json` 版本同步 + 生产部署注意事项（模板变更→WebUI 自定义是否需同步；M2 起写动作的风险提示）。

## 9. 开放项与提案值（实施前可调）

- 场景替换文案（空间语义说明的措辞）、注入摘要行格式——M1 实施时提案评审。
- 好感度空间互动权重（提案：评论≈私聊消息 0.5 倍、点赞≈0.2 倍，入日终结算素材）。
- memo 附带 QQ 上限（提案 5）与写入来源细则。
- ~~`inspect_image` 在虚拟流的可用性~~ **已实证可用**（图片组件对齐 napcat-adapter;timestamp=阅读时刻(方案 B)天然落在宿主 24h 默认窗内）。
- image 组件注入格式的字段细节（对齐 `message_utils.py` 构造器，实现时核对）。
- 虚拟流 session_id 的获取方式：本地按公式计算（md5(platform[+account]+group_id)，需与 `_attach_inbound_route_metadata` 写入的 account 维度一致）或注入后经 `get_all_streams(platform="qzone-qq")` 查询——plan 阶段定，二者皆可。
- `_manifest.json` capabilities 增补核对：gateway（MessageGateway 组件）、`person.get_id`（别名自检）、`config.get`（场景替换与前置检测）——以 SDK 能力声明清单为准逐一核对。
- 学习器注记（可选缓解）：虚拟流学习默认落在自己 session 下（对真实流无污染）；若用户不希望空间评论喂养虚拟流自身的表达库，可在主程序 WebUI 对该会话关学习（插件无法代设，手册注明）；**不要配置 `*:*` 全局表达共享组**（会把虚拟流表达泄入真实流）。

## 10. 主要风险与对策

- **主程序模板改版**→场景替换匹配失败→告警回退追加覆盖块（§6）。
- **cookie 约 24h 过期**→adapter 重取 + 显式告警（§3.1）。
- **QQ 风控**→无计数器但触发时机全由插件掌控（每窗口至多一次表达、串行注入天然限速、窗口外仅评论轮询）；协议端点失效检测告警。
- **执行侧不校验广告集**→硬门控（allowed_session）+工具自检+duplex 终局拒发三层兜底（§2.12）。
- **Maizone 停更**→仅作协议蓝本，全部代码自研实现。
- **生产前置条件（§2.18）**：`experimental.focus_mode=false` 且回复频率 talk_value>0，否则启动告警并停用 qzone 模块（is_mentioned 穿不透 focus 槽与频率 0 静默消费）。
- **方案 B 已否决留档（§2.17）**：未来如再评估「platform="qq" 直连统一」，先重审 `bot_account_service.py:118-119` 的 observed 短路路径；`qzone-qq` 别名折叠是现行安全替代，若主程序改版移除 `get_person_id` 的连字符折叠，启动自检会捕获并**停用模块+告警**。
- **RPC 帧限**：入站注入带图（插件侧压缩阶梯保证 base64 总量 ≤12MB 预算,物理帧 16MB;主程序入站压缩发生在 RPC 之后）、出站驱动序列化默认含二进制（image/emoji 段拒发）、`chat.receive.before_process` 载荷在图片 process 前触发（sleep_gate 等 BLOCKING hook 帧可能含整图 base64——注入泵在大图场景下自知限流即可）。

## 11. 事实锚点（六路调查结论，实施时核对）

- 网关与虚拟流：SDK `capabilities/gateway.py`（update_state/route_message）；Host `plugin_runtime/host/supervisor.py:1642-1732`（路由校验与 inbound policy）、`plugin_runtime/integration.py:137-153`（同入口 receive_message）；`chat/message_receive/chat_manager.py:206-226`（群会话不存最近发言人）；`common/data_models/chat_session_data_model.py:24-42`（is_group_session）。
- 消息格式与去重：`plugin_runtime/host/message_utils.py:491-551`（message_dict 结构）；`platform_io/manager.py:455-484`（platform+message_id 去重）。
- 场景提示词：`src/config/official_configs.py:763-791`（group_chat_prompt/private_chat_prompts）；`maisaka/chat_loop_service.py:798-813`（按 is_group 选择）；模板 `prompts/zh-CN/maisaka_chat.prompt:15`、`maisaka_replyer.prompt:5`。
- hook 改写语义：`maisaka/chat_loop_service.py:1062-1092`（items 深拷贝传入/modified_kwargs 整体替换/tool_definitions 消费点）；`plugin_runtime/host/hook_dispatcher.py:566-599`；`plugin_runtime/hook_payloads.py:136-181`（item_schema_version=1 校验，丢失即静默丢弃）。
- 工具体系：`core/tooling.py:187-197`（availability_context 含 platform）、`302-313`（重名先注册者）；`maisaka/builtin_tool/__init__.py:79-106`（内置清单）；`plugin_runtime/component_query.py:721-737`（可见性过滤）、`848-909`（chat_scope/allowed_session 硬门控，`platform:group_id` 形态）；`maisaka/reasoning_engine.py:2073-2089`（执行侧只查注册表）。
- 说话人 QQ 供给：群会话 kwargs 无 user_id（`component_query.py:740-773` + `chat_session_data_model.py:27`）；`chat.receive` hook payload 含逐消息 user_id（`chat/message_receive/bot.py:761/786`、`plugin_runtime/host/message_utils.py:412-450`）；`Messages.user_id` 逐消息保存（`common/data_models/mai_message_data_model.py:66-101`）；get_recent 默认 24h（`plugin_runtime/capabilities/data.py:498`）。
- 协议蓝本（Maizone 3.0.2，源码级核实）：六端点与参数、`g_tk=hash33(p_skey)`、cookie 四源（adapter `get_cookies` 为合规路径）、`{tid}:{abstime}` 去重、appid=311 限制、请求间隔 2s/点赞 0.1/评论≈0 的保守默认。
- 入站轮触发（调查⑦）：`maisaka/turn_scheduler.py:63-132`（四道门：focus 槽 65/wait 态 72/频率 0 静默 90/强制触发 98/退避 106/阈值 111-132）；强制触发（@/提及）绕过阈值与退避但不穿 focus/wait/频率 0；去抖窗 1.0s（runtime.py:178）；空闲退避（idle_backoff.py:41-95，15s→300s）；轮完成=`planner.after_response` 无 tool_calls（reasoning_engine.py:610-694），无整轮结束 hook；planner 打断默认关（official_configs.py:616-629）。
- 出站链路（调查⑧）：typing=host 侧 sleep（send_service.py:674-679），驱动无需回调；出站双写历史（storage_message+sync_to_maisaka_history，send_service.py:915-931/runtime.py:491-537）；驱动成功契约 `{success, external_message_id}`（plugin_driver.py:174-205）；驱动序列化默认 include_binary_data=True（plugin_driver.py:112）；账号校验 preferred 直通（utils.py:70-81），update_state/入站元数据两路写 BotPlatformAccount 同源（supervisor.py:1611-1624/1698-1708）；图片落盘前提=组件带 binary_data（message_utils.py:341-364、image_manager.py:313-370）。
- 记忆机制（调查⑨）：**person 别名折叠 `get_person_id`：含 `-` 的平台名取连字符后段（split 后第 2 段，如 qzone-qq → qq）后命名空间（person_info.py:48-54，`"qzone-qq"`→qq 命名空间，person 统一的机制依据）**；无跨平台合并（除折叠外）；画像按 person_id 跨流聚合（person_profile_service.py:1096-1229，证据绑定只看 person）；query_memory 硬 chat 隔离（query_memory.py:234-245、search_hit_processing_service.py:466-514）；学习器全带 session 维度、读取限本流+NULL 全局（maisaka_expression_selector.py:95-116、utils_config.py 默认开）；方案 B 否决依据：observed 短路（bot_account_service.py:103-120）+ 路由回退劫持（platform_io/manager.py:399-429、types.py:66-87）。
- 图片组件形态对齐 napcat-adapter：codecs/inbound/message_codec.py:384-411（data 留空+sha256）；data 非空短路 VLM：src/chat/message_receive/message.py:300-303；get_recent hours 参数：src/plugin_runtime/capabilities/data.py:498-505。
