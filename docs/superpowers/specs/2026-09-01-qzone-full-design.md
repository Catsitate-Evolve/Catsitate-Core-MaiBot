# QQ空间模块 完整设计稿（三期终稿）

> 本文档是 QQ空间模块的完整设计规格，面向零上下文的开发者和运维者。
> 实现进度见 `milestone-map.md`。

## 1. 系统概述

将 QQ空间映射为一个虚拟聊天平台，bot 通过主程序的聊天流程（planner→replyer）体验空间内容，通过工具（qzone_comment/qzone_reply/qzone_like/qzone_post）执行空间动作。

**核心设计原则**：
- bot 看到什么（信息流）与 bot 做什么（工具调用）完全分离
- 所有动作由 bot 自主决策（不预设路由），插件只提供执行通道
- API 调用量与好友数量解耦（统一时间线发现层，1 次调用覆盖全好友）

## 2. 架构

```
QzoneClient（协议封装）
├── 发现 API: get_unified_timeline(count) → FeedDiscovery 列表
│   └── 端点 feeds3_html_more?scope=0（全好友时间线索引）
├── 通知 API: get_notification_feed(count) → 赞/评论通知
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
│   ├── 浏览流: 统一时间线→新动态→充实→串行注入
│   └── 通知流: 评论/回复/点赞通知→P1 优先注入
├── 动作执行（bot 做的——全部通过工具）
│   ├── qzone_comment(feed_id, content, at_user_id?)
│   ├── qzone_reply(feed_id, comment_id, content)
│   ├── qzone_like(feed_id?)
│   └── qzone_post(content) 
└── 说说发布（主动触发机制）
    └── 浏览窗口首轮后 proactive.trigger → planner 自主决定 → qzone_post 执行
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
[说说ID=ee3396c49d38 评论ID=2 评论者QQ=10001 回复于今天14:30]
```

**通知·楼中楼回复**（含 bot 原评论上下文）：
```
小明 回复了你的评论「测试收到~」:说得对 → 用quote引用原说说
[说说ID=ee3396c49d38 评论ID=2 评论者QQ=10001 回复于今天14:30]

**通知·点赞**：
```
小明 赞了你的说说「今天天气好」 → 过长可截断，用quote引用原说说
[说说ID=ee3396c49d38 点赞于今天14:30]
```

设计规则：
- 正文是 bot 的阅读信息（谁做了什么），参数行是工具引用（feed_id/comment_id/at_user_id 的来源）
- 时间前缀标注发布时间（今天 HH:MM / M月d日 HH:MM），防 bot 把老内容当刚发生
- `@{uin,nick}` 格式解析为 `@昵称`（保留 @bot 自己，标记这是对 bot 的回复）
- 纯图说说也有文本段（否则 ID 锚丢失）
- bot 自己的说说/日记以 self 消息回注（无 is_mentioned，仅入历史不触发轮）

### 3.2 发现层

端点 `feeds3_html_more?scope=0`，1 次调用返回全好友统一时间线（按时间排序的轻量索引：tid/uin/nickname/abstime/appid）。API 量与好友数量无关。

解析：正则提取 JS 对象中的 `key/appid/abstime/opuin/nickname` 字段，窗口边界为下一个 `key:'` 锚点（防跨条目字段借用）。解析失败告警回退逐好友旧路径。

### 3.3 充实层

仅对发现层标记为新的 tid 按作者分组拉取完整内容（正文+图片+评论列表）。典型 0-3 次 API/周期。

### 3.4 通知通道

双源检测（始终运行，醒着即可，默认 120 秒间隔）：

| 源 | 检测内容 | API |
|---|---|---|
| A | 自己说说下的新评论 | get_own_feed_comments |
| B | 自己在他人说说下评论被回复 | get_user_feeds_raw + parse_feed_replies |
| C | 有人说说了 bot 的说说/评论 | feeds3_html_more?scope=1 解析赞事件 |

通知走 P1 优先级队列（插队于浏览动态之前），串行注入。单轮 ≤3 条。新鲜度截断（默认 3 天）。

### 3.5 注入节奏

串行注入（一次一条），泵在每条消息的 planner 轮完成后推进下一条。阅读顺序新→旧（信息流降序，与 QQ空间 App 一致）。超时兜底 150 秒（须大于最坏 planner 轮延迟）。

## 4. 动作系统

### 4.1 工具清单

| 工具 | 参数 | 描述 |
|---|---|---|
| qzone_comment | feed_id*(必填), content*(必填), at_user_id? | 评论说说；回应评论时填 at_user_id 会自动@TA |
| qzone_reply | feed_id*(必填), comment_id*(必填), content*(必填) | 回复评论（楼中楼，直接在被回复评论下） |
| qzone_like | feed_id?（缺省=当前浏览的说说） | 点赞 |
| qzone_post | content*(必填) | 发布说说（仅表达触发轮） |

所有工具硬门控：仅虚拟流内可用（stream_id 校验）。频控：同说说评论 ≤3 次/窗口。内容长度 ≤200 字（说说 ≤500 字）。

### 4.2 楼中楼 API 参数

`commentId` + `commentUin` 二元组必须精确匹配主评论（被回复的那条评论的 tid + 该评论作者的 uin）。QQ空间的 commentlist 中 tid 是显示序号，不是数据库 ID；二元组匹配序号+作者即可定位唯一评论。

### 4.3 说说发布（主动触发机制）

浏览窗口首轮拉取完成后：
1. Plugin 调 `maisaka.proactive.trigger(虚拟流, intent="你正在<日程事件>，想分享点什么吗？可以用 qzone_post 发一条说说")`
2. Planner 看到触发指示 + 刚看过的动态上下文
3. Planner 自主决定：想发 → 调 qzone_post(content)；不想发 → 沉默

### 4.4 目标解析（FeedContextRegistry）

内存 LRU（128 条，48 小时过期），泵注入成功后登记 tid→owner 映射。工具按 feed_id 参数查 registry→seen_store 回退→awaiting 回退，全 miss 显式报「未找到该说说」。

## 5. 日记系统

### 5.1 触发

入睡确认瞬间（与生成次日日程同任务），旁路 LLM 生成日记 → API 直发 → 醒来后补注虚拟流 self 消息。

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
- 第一人称、80~200 字（随机化目标字数避免模板感）
- 像睡前随手写的感觉，轻松自然，不要流水账
- 有趣的事重点写，平淡的一天简单记录
- 可以提到聊天中的事或看到的动态
- 输出卫生：不加前后缀/引号/表情/@，直接输出正文
- 内容完全基于素材，不编造

## 6. 与现有模块的集成

| 模块 | 集成点 |
|---|---|
| 日程 | qzone=true 标记日常窗口→激活浏览流；日程注入块对 qzone 窗口追加「(正在刷QQ空间)」 |
| 睡眠 | 睡眠中通知轮询静默；入睡时生成日记；醒来后补注；空间活动刷新静默入睡计时 |
| 好感度 | 空间互动（评论/被评论/点赞/被点赞）写入 fav_events → 并入日终结算候选；衰减计时含空间事件基准 |
| 备忘录 | 备忘按人存储（主QQ+附带QQ 跨流可见）；虚拟流上写的备忘挂虚拟流维度（当前限制） |
| 场景替换 | 虚拟流的群聊场景提示词替换为空间场景（WebUI 可编辑 catsitate_qzone_scene） |
| 工具隔离 | 双向：虚拟流白名单过滤 / 真实流隐藏 qzone_* 工具 |

## 7. 数据模型

| 表 | 键 | 用途 |
|---|---|---|
| qzone_feeds | tid PK | 动态去重（queued/seen/interacted）+ 注入消息 ID + 作者昵称 |
| qzone_comments | comment_key PK | 评论/回复去重 + bot 评论追踪（friend_uin/retry_count） |
| qzone_fav_events | 自增 PK | 好感度事件（day/user_id/kind/text），同日同事件去重 |
| qzone_likes | like_key PK | 赞事件去重（liker_owner_hash） |

## 8. 配置面（qzone 节）

| 字段 | 默认 | 说明 |
|---|---|---|
| enabled | true | 模块总开关（含写动作） |
| poll_interval_minutes | 15 | 浏览流轮询间隔（发现层+充实层） |
| notification_interval_seconds | 120 | 通知轮询间隔（最小 30） |
| decision_window_seconds | 150 | 注入后等待 planner 轮完成的超时 |
| tool_whitelist | 含 10 工具 | 虚拟流可用工具（qzone_post 不在默认中） |
| comment_poll_enabled | true | 通知轮询开关 |
| summary_count / summary_days | 5 / 3 | 见闻摘要条数/回溯天数 |
| diary_enabled | true | 日记开关 |
| diary_llm_model / timeout | memory / 0 | 日记生成模型 |
| virtual_group_id / name | qzone_feed / QQ空间 | 虚拟流标识 |

## 9. 风控注意

- 写路径（评论/点赞/发布）有真实不可逆副作用
- 通知轮询约 1000 次 API/天（常量，与好友数无关）
- cookie 约 24 小时过期，自动经 NapCat adapter 重取
- 图片下载域名白名单（*.qpic.cn / *.qq.com），非白名单不带 Cookie
- 好友间拉取间隔 2 秒（防风控）
