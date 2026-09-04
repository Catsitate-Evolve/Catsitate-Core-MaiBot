# QQ空间·互动(工具驱动架构 + 通知三源)

> 对应代码:`plugin.py` 的 `qzone_like` / `qzone_comment` / `qzone_reply` / `qzone_post` / `view_friend_feeds` / `view_friend_feed_detail` 六个工具与 `_qzone_notify_scan` / `_qzone_resolve_feed` / `_qzone_auth_retry` / `_qzone_notify_retry_backoff` 段,`catsitate_core/qzone/` 的 `injector.py`(P1/P2 队列)、`registry.py`、`wire.py`(写路径表单)、`comment_seen.py` / `like_seen.py` / `seen_store.py`。

## 一、职责与生命周期

互动层回答两个问题:**bot 怎么对好友的说说做出动作**(评论/回复/点赞/发说说),以及**好友对 bot 的空间做了什么时 bot 怎么知道**(评论/楼中楼回复/赞 通知)。

架构核心是**工具驱动**:插件不预判「bot 看到说说了就该评论」,只把说说注入上下文并提供工具——是否互动、何时互动、互动说什么,全部由 planner(持有全量上下文与表达意图)自主决定。虚拟流网关是 receive 模式(只进不出),bot 在空间流里直接打字发不出去,动作一律经 `qzone_*` 工具显式发出。

生命周期:

- **四个动作工具全域可用**:`qzone_like` / `qzone_comment` / `qzone_reply` / `qzone_post` 不受 `tool_whitelist` 管理、不可剔除——`view_friend_feeds` 结果里带说说ID,任何聊天流(真实 QQ 流与空间虚拟流)里都能互动。
- **两个查看工具**:`view_friend_feeds`(指定好友最近说说,支持翻页)与 `view_friend_feed_detail`(单条说说完整信息含评论区)——是动作工具在真实聊天流里的参数来源。
- **通知轮询**:`qzone_notify_poll` 调度任务(默认 120 秒,注册下限 30s),始终运行、醒着即可——通知是推送语义,不隶属任何浏览窗口。

## 二、完整逻辑

### 2.1 四动作工具与写路径

| 工具 | 端点 | 关键参数 |
|---|---|---|
| `qzone_like(feed_id?)` | internal_dolike_app | unikey/curkey=说说 URL,appid=311;缺省 feed_id 时对「当前正在浏览的说说」点赞(awaiting) |
| `qzone_comment(feed_id, content, at_user_id?)` | emotion_cgi_re_feeds | topicId=`{主人}_{fid}__1`;at_user_id 触发 `@{uin,nick,auto:1}` 前缀(自动 @ 对方) |
| `qzone_reply(feed_id, comment_id, content)` | 同评论端点 + commentId/commentUin | 二元组精确匹配主评论;@ 前缀目标与二元组解耦 |
| `qzone_post(content)` | emotion_cgi_publish_v6 | 纯文本;发布成功后回注虚拟流(见 qzone-express.md) |

写路径共同纪律:`content` 由 planner 直写草稿、发出前经表达润色层顺口吻(qzone-express.md)与内容护栏(guard.md);长度硬校验在工具入参(评论/回复 ≤200 字、说说 ≤500 字);**失败不重试**(登录态失效的同轮自愈除外)。

### 2.2 目标解析三级(feed_id 从哪还原成全量 tid)

消息尾部锚只展示 tid 前 12 位,直接拿锚值发 API 会构造畸形 unikey/topicId——`_qzone_resolve_feed` 负责把锚值还原成全量 tid + 说说主人:

1. **registry(精确 → 前缀)**:`FeedContextRegistry` 内存 LRU(上限 128 条,48h TTL)。键=真实说说 tid(通知项登记 origin_tid);resolve 先精确命中,未命中再按「键以查询串为前缀」回退(同前缀多键取最近使用项)。
2. **seen_store(7 天浏览窗前缀)**:近 7 天已见动态(至多 200 条)里按 tid 前缀匹配,返回全量 tid + 作者 uin(无 registry 上下文)。
3. **awaiting(当前浏览项)**:正在等决策轮的动态;通知项取真实说说 tid(origin_tid),无 origin_tid 的畸形通知不可解析。

三级全 miss → 返回空 tid,调用方显式拒绝(「未找到说说…可能已过期,请核对消息尾部的说说ID」)。

### 2.3 comment_map:评论级锚

`qzone_reply` 需要知道 `comment_id` 对应的主评论作者是谁(楼中楼二元组的 commentUin)。`FeedContext.comment_map` 是 `comment_tid → (作者uin, 昵称)` 映射,三路填充:

- **浏览注入**:泵注入成功后,从充实层的结构化评论区全量填充(`{c.comment_tid: (c.uin, c.nickname)}`)。
- **详情查看**:`view_friend_feed_detail` 拉到完整评论区后登记(同款全量)。
- **通知**:通知项只带主评论二元组({feed.comment_tid: (feed.comment_uin, "")},昵称未知留空)。

registry 的 `register` 是**字段级合并**(新值非空覆盖旧值,空值保留旧值;kind=feed 不清掉通知语义)——同一说说会被多种来源先后登记,整体覆盖会把通知里的评论者信息冲掉。comment_map 是**键级合并**(新评论并入、旧评论锚保留;同键时昵称取非空侧)。

`qzone_reply` 的二元组解析顺序:① 通知上下文且 `ctx.comment_tid == comment_id`(锚精确匹配)→ 用通知的 comment_uin(源A=评论好友,源B=bot 自己);② comment_map 命中 → 用评论作者;③ 全 miss → **显式拒绝+指引**(「先用 view_friend_feed_detail 查看该说说,照抄最新评论ID再回复」),不猜测回退。@ 目标与二元组解耦:@ 正在对话的评论者/回复者(仅锚匹配的通知有评论者语境,否则 @ 主评论作者)。

### 2.4 通知三源

`_qzone_notify_scan`(每 120s,醒着即可,睡眠中跳过)三源检测,单轮上限 3 条(防通知风暴):

- **源A(自己说说下的新评论,含 bot 评论下的楼中楼回复)**:`get_own_feed_comments`(单次 msglist 请求三用:评论映射 + 正文上下文 + bot 评论的 list_3 楼中楼回复,第三视图同载荷补跑 `parse_feed_replies`,不发第二次请求)。逐条以 `feed_tid:comment_tid:uin` 为去重键 `is_new` 登记判新;自己发出的评论重见即幂等登记不注入;早于 `summary_days`(默认 3 天)的过旧通知跳过。正文=「评论了你的说说:…」+ 参数行;楼中楼回复正文=「回复了你的评论「{bot原评论前20字}」:…」(与源B 同形态,去重键 `{feed_tid}:{parent_comment_tid}:reply:{reply_tid}`)——「好友评论→bot 楼中楼回复→好友再回复」在自己说说下的线程由此覆盖(源B 名单交叉显式排除 bot 自己)。好友回复另一好友的旁听线程不通知(bot 不插话他人对话)。
- **源B(自己在他人说说下的评论收到楼中楼回复)**:搭发现层便车——本地反查近 30 天 bot 评论过的好友(`bot_commented_friends`),与统一时间线单页的活跃作者取交集,只对「有新活动且评论过」的好友拉原始载荷(`get_user_feeds_raw`),`parse_feed_replies` 解析 bot 评论的 list_3。零交集时零源B 拉取。正文=「回复了你的评论「{bot原评论前20字}」:…」+ 参数行;二元组 comment_uin=bot 自己(主评论作者是 bot)。
- **源C(有人赞了我的说说)**:「与我相关」流(`feeds3_html_more?scope=1`),`parse_like_events` 解析赞事件(条目锚=data-key 三元组,头部窗口须含「赞了我的说说」;相对时间「今天/昨天/N月N日 HH:MM」折算 epoch,非法时间置 0 不编造)。去重走 `qzone_likes` 表(键=liker_owner_hash,取消赞再赞不重复通知)。正文=「赞了你的说说「{标题}」」(标题素材取 seen 表 summary——自己发布的说说才有,未登记则无标题)+ 参数行。

三源共同行为:评论正文里的 `@{uin,nick}` 机器格式解析为 `@昵称`;事件同步计入好感度显式事件(`fav_event`,同日同 user+kind+text 去重);awaiting 占用时先驱动泵(防「占用→跳过扫描」死锁);入队前异常回退本轮已登记的去重键(通知不静默丢失)。

### 2.5 P1/P2 队列与串行注入泵

`FeedInjector` 是纯状态机双优先级队列:

- **P1=通知**(评论/楼中楼回复/赞):推送语义,**任何时刻可注入**,不依赖浏览窗口;窗口结束时保留。
- **P2=浏览动态**:仅 read_qzone 窗口内可注入;窗口结束时清空(未读回退 seen 表)。
- 两队列各自按发布时间(abstime)降序——信息流降序,最新先看。
- `next_to_inject`:awaiting 未释放时返回 None(串行语义);P1 非空优先,否则窗口内取 P2。一次只允许一条动态处于 awaiting(已注入待轮完成),推进条件=**轮完成信号**(planner.after_response 无 tool_calls 的响应,经 `qzone_turn_signal` 钩子转发 `on_turn_complete`);`wait` 工具调用切换上限档位(常规 `decision_window_s` → 3 倍硬上限,起点锚定注入时刻不重置),防 wait 期间注入下一条并入批处理导致出站错靶。

### 2.6 通知消息构造

通知走 `messages.build_notify_message`(与浏览注入的 `build_feed_message` 分工:不走图片/时间前缀管线):

- **reply 段置首**:引用**原说说**的注入消息(napcat quote 式上下文关联)——引用目标经 `seen_store.get_message_id(origin_tid)` 查原说说注入时记录的 message_id,`target_message_content`=原说说正文前 60 字(bot 一眼看到「这条评论发生在哪条说说下」)。原说说未注入过(窗口外通知/已被清理)时查无 id → reply 段省略,回退纯文本,不静默臆造。
- **正文**:由通知扫描侧精简构造(见 2.4 各源形态)+ 参数独立尾行 `format_comment_param_line`——`〔说说ID=xx 评论ID=xx 评论者QQ=xx 评论于(今天HH:MM)〕`,参数名与工具参数(feed_id/comment_id/at_user_id)的映射由场景 prompt 解释;动作时间让 bot 分得清互动新旧,create_time 缺失则省略不编造。
- **不设 is_mentioned**:通知走主程序自然回复概率——bot 看到通知但不必然回应(拟人化留白);浏览注入保留强制触发(串行浏览决策环的设计依赖)。

### 2.7 回执契约

工具回执行为如实告知模型:成功回执带回发出的内容与锚(`评论成功,已发出:「…」` / `点赞成功:{昵称} 的说说(说说ID=…)`);失败回执说明原因;远端已成功但本地记账失败时只告警、回执仍报成功(谎报失败会诱导重复发布);发布成功但响应缺 tid 时回执报成功+告警「回注缺锚」。

## 三、限制与回退清单

| 场景 | 行为 |
|---|---|
| feed_id 目标解析三级全 miss | 显式拒绝:「未找到说说 {锚} ,可能已过期,请核对消息尾部的说说ID」——不猜测回退 |
| comment_id 锚过期/未查过详情 | 显式拒绝+指引(先用 view_friend_feed_detail 查看再回复) |
| 写请求业务错误 -10049(操作频繁) | 回执带限制说明:「操作太频繁……先歇一歇别重试,等下次浏览时再互动」——不做硬频控,限制写进工具返回让模型自行收敛 |
| 其它业务码 QzoneBizError | 回执带 code 劝阻立即重试 |
| 登录态失效 QzoneAuthError | **同轮自愈**(`_qzone_auth_retry`):作废 cookie → 经 adapter 强制重取(NapCat 在线会话,免扫码)→ 原地重试一次;重取失败/重试仍失效才返回显式失败回执(「登录态失效且 cookie 重取失败——请检查 NapCat 的 QQ 登录状态」);覆盖四动作工具/两查看工具/通知三源 |
| 通知注入被拒/异常(源A/B) | 软回退:去重键置 pending_retry 令下轮重新发现(通知不因一次拒绝永久丢失);重试上限 **3 次**(`QZONE_NOTIFY_MAX_RETRIES`,计数跨「回退→重发现」循环累计),满 3 次保留登记放弃 |
| 通知注入被拒(源C) | 显式告警放弃——源C 去重在 qzone_likes 表,无软回退通道,不误报「待下轮重试」 |
| 源B 发现层失败/登录态失效 | 源B 仅是增量来源:告警后按空处理,不阻断源A 已得通知入队 |
| 源C 相对时间折算遇非法时间 | 告警后 create_time 置 0(时间前缀省略、新鲜度不误截断),不上抛 |
| 源C 连续 3 轮取数成功但零事件 | 锚点漂移 warn-once 告警(恢复有事件即复位) |
| 扫描级异常(登记后入队前) | 回退本轮已登记且未入队的去重键,下轮重新发现;CancelledError 回退后原样上抛 |
| view_friend_feeds 翻页空页 | 「没有更多了(第 N 页为空)」;第 1 页空=「最近没有可见的说说」(分言,不编造) |
| view_friend_feed_detail 目标不在最近 20 条内 | 显式提示「可能已删除,或比这更早(更早的没有查看通路)」 |
| awaiting 占用期间新通知 | 先驱动泵(超时强制推进),未超时才维持不叠加(下轮再取) |
| 动作 API 失败 | 不重试(动作 API 失败即告警的固定纪律);远端已成功的记账/锚定失败仅告警不误报失败 |

**已知边界**:`qzone_like` 缺省 feed_id 依赖浏览态(awaiting),非浏览流调用须显式带 feed_id;通知项无 origin_tid 的畸形形态不可点赞/解析(合成 tid 发 API 必畸形,显式拒绝)。
