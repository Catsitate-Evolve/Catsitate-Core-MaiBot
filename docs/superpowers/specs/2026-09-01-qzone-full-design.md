# QQ空间模块 完整设计稿（三期终稿）

> 本文档是 QQ空间模块的完整设计规格，面向零上下文的开发者和运维者。
> 实现进度见 `milestone-map.md`。

## 1. 系统概述

将 QQ空间映射为一个虚拟聊天平台，bot 通过主程序的聊天流程（planner→replyer）体验空间内容，通过工具（qzone_comment/qzone_reply/qzone_like/qzone_post）执行空间动作。

**核心设计原则**：
- bot 看到什么（信息流）与 bot 做什么（工具调用）完全分离
- 所有动作由 bot 自主决策（不预设路由），插件只提供执行通道
- API 调用量与好友数量解耦（统一时间线发现层，1 次调用覆盖全好友）
- 动作正文由 planner 直写草稿、表达润色层按人设与表达方式顺成 bot 平时的样子——「说什么」归 planner，「怎么说」归润色层（与主程序 planner/replyer 的分工同构）

## 2. 架构

```
QzoneClient（协议封装）
├── 发现 API: get_unified_timeline(count) → FeedDiscovery 列表
│   └── 端点 feeds3_html_more?scope=0（全好友时间线索引）
├── 赞通知 API: get_like_events(count) → 赞事件列表
│   └── 端点 feeds3_html_more?scope=1（与我相关通知流）
├── 内容 API: get_user_feeds(uin) / get_own_feed_comments(bot_uin)
│   └── 端点 emotion_cgi_msglist_v6（指定用户说说+评论）
├── 写 API: do_like / do_comment / do_reply / do_publish
│   └── 端点 internal_dolike_app / emotion_cgi_re_feeds / emotion_cgi_publish_v6
└── 基础: CookieManager（NapCat adapter 取证+节流+持久化 0600）
```

```
虚拟流（platform="qzone-qq"，receive-only 网关）
├── 信息注入（bot 看到的）
│   ├── 浏览流: 统一时间线→新动态→充实→串行注入（read_qzone 窗口内）
│   └── 通知流: 评论/回复/赞通知→P1 优先注入（醒着即可，不依赖窗口）
├── 动作执行（bot 做的——全部通过工具）
│   ├── qzone_comment(feed_id, content, at_user_id?)
│   ├── qzone_reply(feed_id, comment_id, content)
│   ├── qzone_like(feed_id?)
│   └── qzone_post(content)
├── 表达润色层
│   └── planner 直写 content 草稿 → 旁路人设 LLM 按人设+表达方式顺一遍 → 写 API
└── 说说发布（主动触发）
    ├── read+send 同窗: 首轮浏览注入完成后 proactive.trigger
    └── 仅 send 窗口: 窗口开始时 proactive.trigger（冷启动种子自举）
```

## 3. 信息流设计

### 3.1 消息格式

每条注入消息包含：正文（自然语言）+ 参数行（工具引用）。

**浏览动态**：
```
作者：XXX
内容：今天天气好
[说说ID=ee3396c49d38 发布于今天14:30]
```

**通知·评论**：
```
小明 评论了你的说说:好棒 → 用quote引用原说说
[说说ID=ee3396c49d38 评论ID=2 评论者QQ=10001 评论于今天14:30]
```

**通知·楼中楼回复**（含 bot 原评论上下文）：
```
小明 回复了你的评论「测试收到~」:说得对 → 用quote引用原说说
[说说ID=ee3396c49d38 评论ID=2 评论者QQ=10001 回复于今天14:30]
```

**通知·点赞**：
```
小明 赞了你的说说「今天天气好」 → 过长可截断，用quote引用原说说
[说说ID=ee3396c49d38 点赞于今天14:30]
```

设计规则：
- 正文是 bot 的阅读信息（谁做了什么），参数行是工具引用（feed_id/comment_id/at_user_id 的来源）
- 时间标注发布/动作时间（今天 HH:MM / M月d日 HH:MM）：浏览动态标发布时间、通知标动作时间（评论于/回复于/点赞于）——防 bot 把老内容或旧互动当刚发生
- `@{uin,nick}` 格式解析为 `@昵称`（保留 @bot 自己，标记这是对 bot 的回复）
- 纯图说说也有文本段（否则 ID 锚丢失）
- bot 自己的说说/日记以 self 消息回注（无 is_mentioned，仅入历史不触发轮），回注带〔说说ID=xxx〕参数行并登记 FeedContext

### 3.2 发现层

端点 `feeds3_html_more?scope=0`，1 次调用返回全好友统一时间线（按时间排序的轻量索引：tid/uin/nickname/abstime/appid）。API 量与好友数量无关。

翻页：单页 count=discovery_count（默认 50），若本页存在新 tid 则以 begin 偏移取下一页，直到某页无新 tid 或达 discovery_max_pages（默认 3）。稳态（无新动态）恒为 1 次调用；长时间离线后的积压靠翻页补全——该端点永远只返回最新 N 条，不翻页时超出单页的旧新动态会永久错过且无告警。

解析：正则提取 JS 对象中的 `key/appid/abstime/opuin/nickname` 字段，窗口边界为下一个 `key:'` 锚点（防跨条目字段借用）。解析失败告警回退逐好友旧路径。

### 3.3 充实层

仅对发现层标记为新的 tid 按作者分组拉取完整内容（正文+图片+评论列表）。典型 0-3 次 API/周期。

### 3.4 通知通道

三源检测（始终运行，醒着即可，默认 120 秒间隔）：

| 源 | 检测内容 | API |
|---|---|---|
| A | 自己说说下的新评论 | get_own_feed_comments（扫自己最近 own_feed_scan_count 条说说，默认 20——好友翻旧账评论也要能发现） |
| B | 自己在他人说说下评论被回复 | 统一时间线交集 + get_user_feeds_raw + parse_feed_replies |
| C | 有人赞了 bot 的说说 | feeds3_html_more?scope=1 解析赞事件 |

通知走 P1 优先级队列（插队于浏览动态之前），串行注入。**通知不依赖浏览窗口**——bot 醒着且泵空闲即可注入（推送语义：像手机通知一样随时到，不要求正在刷空间）；浏览动态（P2）仍仅在 read_qzone 窗口内注入。单轮 ≤3 条。新鲜度截断（默认 3 天）。

源C去重：like_key=`{liker}_{owner}_{hash}`（赞的人_说说主人_说说哈希），同一人同一条说说只通知一次（取消赞再赞不重复通知）；点赞时间来自赞事件自身的时间戳，随参数行注入。

### 3.5 注入节奏

串行注入（一次一条），泵在每条消息的 planner 轮完成后推进下一条。阅读顺序新→旧（信息流降序，与 QQ空间 App 一致）。超时兜底 150 秒（须大于最坏 planner 轮延迟）。

## 4. 动作系统

### 4.1 工具清单

**虚拟流专用**（仅 qzone-qq 虚拟流内可用；真实流自动隐藏 qzone_* 前缀工具）：

| 工具 | 参数 | 描述 |
|---|---|---|---|
| qzone_comment | feed_id*(必填), content*(必填), at_user_id? | 评论说说；content 直接写你想说的，发出前自动按口吻润色；回应评论时填 at_user_id 自动@TA |
| qzone_reply | feed_id*(必填), comment_id*(必填), content*(必填) | 楼中楼回复评论；comment_id 二元组来自通知参数行 |
| qzone_like | feed_id?（缺省=当前浏览的说说） | 点赞 |
| qzone_post | content*(必填) | 发布说说；content 直接写，发出前自动按口吻润色 |

**全域可用**（真实聊天流和虚拟流均可调用，不以 qzone_ 为前缀；真实流内 view_friend_feeds 仅供获取信息——空间动作工具在真实流隐藏）：

| 工具 | 参数 | 描述 |
|---|---|---|
| view_friend_feeds | qq*(必填), count?(默认 3, 上限 10) | 查看指定好友最近 n 条说说，返回正文+图片 |
| inspect_image | message_id?, image_index?, question*, image_hash? | 带具体问题重看图片；message_id 路径搜消息（真实/虚拟流均可），image_hash 路径按前缀匹配 Images 表（8 位前缀即可，覆盖 view_friend_feeds 等非消息路径入库的图片） |

`view_friend_feeds` 说明：
- 调用 `client.get_user_feeds` 复用现有解析
- **图片返回走主程序原生 tool result media 通道**：工具返回 dict（非 str），`content` 为格式化文本摘要，`content_items` 为图片列表（content_type=image + base64 + mime_type）。主程序自动处理：文本 planner 后台 VLM 识图，视觉 planner 直接看图。图片体积用现有压缩阶梯治理。
- 主程序 image_manager 的 sha256 去重自动跳过已识图过的图片，插件侧不维护缓存。
- **hash 随摘要透出**：tool result media 的元数据不向 planner 透出图片 hash——`content` 文本摘要逐图列出（如「图1(ab12cd34)」，为图片 sha256 的前 8 位），planner 需要重看时把该串传给 inspect_image(image_hash=...)
- 成功返回时登记 FeedContextRegistry（tid→owner），虚拟流内看完即可续 qzone_comment/qzone_like

`inspect_image` 的 image_hash 路径说明：
- 现有链路（message_id → find_image_segment → hash）保持不变，仍为默认路径
- 新增可选参数 `image_hash`：按前缀（8 位即可）匹配 Images 表取 full_path，跳过消息搜索；零命中/多命中显式报错（多命中列候选提示加长前缀），不回退消息搜索
- hash 来源：消息流内图片走 message_id 路径即可；view_friend_feeds 返回的图片用 content 摘要中列出的 hash 前缀

工具白名单（`qzone.tool_whitelist`）语义：**虚拟流内可使用的工具集**，表外工具一律不可用。主程序 `reply` 工具不在白名单内——虚拟流是 receive-only 网关，replyer 生成的文本无出站路径可投递，放行只会产生必然失败的调用。真实流侧反向隔离：自动隐藏 qzone_* 前缀工具（全域工具不受影响）。

### 4.2 表达润色层

动作工具的 `content` 由 planner 直写草稿（planner 持有全量上下文与表达意图，写什么由它决定），发出前经旁路人设 LLM 润色成 bot 平时的样子——「说什么」归 planner、「怎么说」归润色层，与主程序 planner/replyer 的分工同构。

润色要素：
- **人设前置**：bot 人设正文（主程序全局配置 `personality.personality`）与表达方式（`personality.reply_style`）作为稳定上下文前两段注入，均经 config.get 读取、带缓存、bot 配置变更自动失效
- 草稿以【待发内容】素材段传入；模板 `catsitate_qzone_expression`（WebUI 可编辑）只承载润色指令

输出纪律：
- 评论/回复 ≤200 字、说说 ≤500 字：入参即校验，润色超长时带字数要求重新润色一次，仍超截断并告警
- 输出卫生：剥首尾引号，直接输出润色结果；at_user_id 的 @ 前缀由写路径表单层附加在润色结果前
- **润色失败不阻断动作**：告警后以草稿直发（草稿本身即 planner 的完整表达，显式回退不静默）

### 4.3 楼中楼 API 参数

`commentId` + `commentUin` 二元组必须精确匹配主评论（被回复的那条评论的 tid + 该评论作者的 uin）。QQ空间的 commentlist 中 tid 是显示序号，不是数据库 ID；二元组匹配序号+作者即可定位唯一评论。

### 4.4 说说发布（主动触发机制）

发布触发由日程窗口的 `send_qzone` 属性激活，触发时点按窗口形态分两种：
- **read+send 同窗**：首轮浏览注入完成后触发——bot 刚看完好友动态，分享有上下文（intent 提示含「刚看完好友动态」语义）
- **仅 send 窗口**：窗口开始时触发——忙里偷闲只想发一条，无浏览上下文（intent 提示不引用「刚看过」）

流程：
1. Plugin 调 `maisaka.proactive.trigger(虚拟流, intent=...)`
2. Planner 看到触发指示（同窗形态下还有刚注入的动态上下文）
3. Planner 自主决定：想发 → 调 qzone_post(content=想分享的内容)；不想发 → 沉默（正常结束，不重试）

冷启动自举：proactive.trigger 要求虚拟流会话已存在，而会话在第一条消息进入后才诞生——开机后尚未浏览过则触发必失败。处理：触发返回「未找到已存在的聊天流」时，先注入一条种子消息（无 is_mentioned，仅建会话不触发决策轮）再重试一次；仍失败告警跳过，等下个窗口。

### 4.5 目标解析（FeedContextRegistry）

内存 LRU（128 条，48 小时过期），tid→owner 映射登记点有三处：
- 泵注入成功后（浏览动态与通知）
- view_friend_feeds 成功返回时（看过即可续评论/点赞）
- qzone_post / 日记直发成功后（tid 来自发布响应，owner=bot 自己）

工具按 feed_id 参数查 registry→seen_store 回退→awaiting 回退，全 miss 显式报「未找到该说说」。发布响应中的新 tid 必须保留——丢弃会让「发布→好友评论→回应」链路在 registry 断开（bot 发的说说自己却无法回应）。

## 5. 日记系统

### 5.1 触发

入睡确认瞬间（与生成次日日程同任务），旁路 LLM 生成日记 → API 直发（保留响应中的新说说 tid）→ 醒来后补注虚拟流 self 消息（带〔说说ID=xxx〕参数行并登记 FeedContext）。

### 5.2 素材

| 素材 | 来源 |
|---|---|
| 当日日程概览 | schedule_data 活动窗口 |
| 到期备忘 | memo.due_on(today) |
| 空间见闻 | qzone_seen.recent_seen（当日已注入动态） |
| 聊天时间线 | 主程序消息（按小时分段，bot 标「我:」、他人标昵称、单条截 50 字） |
| 真实天气 | time_aware 模块缓存 |

不包含近期日记参考（日记的语义是「今天的回顾」；如后续出现连续重复内容，可加 config 开关引入昨日摘要作为去重提示，默认关闭）。

### 5.3 生成 prompt

模板 `catsitate_qzone_diary.prompt`（WebUI 可编辑），要点：
- **人设前置**：bot 人设正文（主程序全局配置 `personality.personality`，经 config.get 读取、带缓存）作为稳定上下文首段注入，模板指示以该人设的身份与口吻书写——日记是 bot 自己的声音，不是第三人称转述
- 第一人称、80~200 字（随机化目标字数避免模板感）
- 像睡前随手写的感觉，轻松自然，不要流水账
- 有趣的事重点写，平淡的一天简单记录
- 可以提到聊天中的事或看到的动态
- 输出卫生：不加前后缀/引号/表情/@，直接输出正文
- 内容完全基于素材，不编造

## 6. 与现有模块的集成

| 模块 | 集成点 |
|---|---|
| 日程 | 窗口属性分 `read_qzone`（激活浏览流；窗口结束时生成见闻）和 `send_qzone`（激活发布触发），两者独立可同窗，均仅 daily 窗口合法；日程注入块对 read_qzone 窗口追加「(正在刷QQ空间)」 |
| 睡眠 | 睡眠中通知轮询静默；入睡时生成日记；醒来后补注；空间活动刷新静默入睡计时 |
| 好感度 | 空间互动（评论/被评论/点赞/被点赞）写入 fav_events → 并入日终结算候选；衰减计时含空间事件基准 |
| 备忘录 | 备忘按人存储（主QQ+附带QQ 跨流可见）；虚拟流上写的备忘挂虚拟流维度（当前限制） |
| 场景替换 | 虚拟流的群聊场景提示词替换为空间场景（WebUI 可编辑 catsitate_qzone_scene） |
| 工具隔离 | 双向：虚拟流按 tool_whitelist 过滤（白名单=虚拟流内可用工具集，表外一律不可用）/ 真实流隐藏 qzone_* 工具（view_friend_feeds、inspect_image 全域可用不受隔离） |

## 7. 见闻系统

### 7.1 设计

将 QQ空间虚拟流的历史经验摘要为自然语言见闻，注入真实聊天流。Bot 自己发布的说说（日记/日常）做好回注后，同样纳入见闻素材。

### 7.2 实现路径

插件旁路 LLM 摘要（唯一路径）。主程序的会话摘要由 bot 发言后的回写服务生成，而虚拟流是 receive-only 网关——bot 从不产生发言投递，主程序摘要服务不会为虚拟流产出内容，插件也无 API 读取记忆段落。因此见闻由插件在窗口边界自行摘要，方法与主程序记忆摘要一致（素材→摘要→存储→注入）。

触发时点：read_qzone 窗口结束的瞬间，素材为该窗口注入的动态/通知与 bot 的互动行为；同日多个 read 窗口时，后生成的见闻覆盖前一份（当日见闻始终反映最新一轮浏览）。当日无 read 窗口则不生成。生成失败告警并保留上一份。

### 7.3 素材范围

该 read_qzone 窗口注入的全部内容（浏览动态+通知），bot 的互动行为（评论/点赞/发布记录，含 fav_events 当日条目），以及 bot 自己发布内容的回注。

### 7.4 注入

生成的见闻存入注入框架新增的「空间见闻」块（排序在环境块之后），真实聊天流输出 `[空间见闻] {digest}`——bot 在群聊/私聊中自然引用空间经历。

## 8. 数据模型

| 表 | 键 | 用途 |
|---|---|---|
| qzone_feeds | tid PK | 动态去重（queued/seen/interacted）+ 注入消息 ID + 作者昵称 |
| qzone_comments | comment_key PK | 评论/回复去重 + bot 评论追踪（friend_uin/retry_count） |
| qzone_fav_events | 自增 PK | 好感度事件（day/user_id/kind/text），同日同事件去重 |
| qzone_likes | like_key PK | 赞事件去重（`{liker}_{owner}_{hash}`，同一人同一条说说只通知一次） |

## 9. 配置面（qzone 节）

| 字段 | 默认 | 说明 |
|---|---|---|
| enabled | true | 模块总开关（含写动作） |
| poll_interval_minutes | 15 | 浏览流轮询间隔（read_qzone 窗口内） |
| notification_interval_seconds | 120 | 通知轮询间隔（最小 30） |
| decision_window_seconds | 150 | 注入后等待 planner 轮完成的超时 |
| tool_whitelist | qzone_* 四工具 + view_friend_feeds / inspect_image 等 | 虚拟流可用工具集（表外一律不可用） |
| comment_poll_enabled | true | 通知轮询开关 |
| discovery_count | 50 | 发现层单页拉取条数 |
| discovery_max_pages | 3 | 发现层翻页上限 |
| own_feed_scan_count | 20 | 源A扫描自己最近 N 条说说的评论 |
| digest_enabled | true | 见闻摘要开关 |
| digest_llm_model / timeout | memory / 0 | 见闻摘要模型 |
| diary_enabled | true | 日记开关 |
| diary_llm_model / timeout | memory / 0 | 日记生成模型 |
| expression_llm_model / timeout | memory / 0 | 表达润色模型（评论/回复/说说正文按人设口吻润色；失败以草稿直发） |
| virtual_group_id / name | qzone_feed / QQ空间 | 虚拟流标识 |

## 10. 风控注意

- 写路径（评论/点赞/发布）有真实不可逆副作用
- 表达润色失败时以草稿直发（草稿即 planner 的完整表达，显式回退）；写路径动作失败不重试，告警后由 planner 决定是否再次调用
- cookie 约 24 小时过期，在过期前务必自动经 NapCat adapter 重取
- 图片下载域名白名单（*.qpic.cn / *.qq.com），非白名单不带 Cookie
- 好友间拉取间隔 2 秒（防风控）
