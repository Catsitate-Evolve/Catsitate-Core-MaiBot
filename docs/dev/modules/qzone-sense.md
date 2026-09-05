# QQ空间·感知(浏览好友动态 → 注入虚拟流)

> 对应代码:`plugin.py` 的 `_qzone_poll_tick` / `_qzone_poll_feeds` / `_qzone_shared_discovery` / `_qzone_inject_one` / `_qzone_pump` / `qzone_next` 段,`catsitate_core/qzone/` 的 `client.py`(读路径)、`discovery.py`、`messages.py`、`imaging.py`、`protocol.py`。

## 一、职责与生命周期

感知层回答一个问题:**bot 怎么「看到」好友的 QQ 空间动态**。它把 QQ 空间的网页 cgi 接口拉到的说说,伪装成一条条群聊消息注入给主程序 planner——bot 像躺在沙发上刷手机一样「浏览」好友动态,而不是以机器方式轮询数据。

生命周期:

1. **启动**:`on_load` 里 `_qzone_selfcheck()` 自检(开关 → person 别名折叠 → focus_mode/talk_value 前置检测),通过后 `_qzone_gateway_ready()` 向宿主上报虚拟网关就绪;随后注册 `qzone_poll` 调度任务(间隔 `qzone.poll_interval_minutes`,默认 15 分钟,语义=窗口内**两次拉取的间距**而非与窗口无关的固定节奏:进入 read/send 窗口时由 `_schedule_tick` 立即派发首轮拉取,之后的刷新由定间隔任务承担;`_qzone_poll_feeds` 的间距判定保证距上次实际拉取不足间隔时跳过拉取段,防两路相邻撞车)。
2. **运行**:调度器每 tick 触发 `_qzone_poll_tick`——防重入后把长 IO 派发为后台任务 `_qzone_poll_feeds`(不阻塞调度器 60s tick 里的其它任务)。发现层取数统一走 `_qzone_shared_discovery` 入口(**单飞+共享缓存+限流退避**):浏览层与通知源B(120 秒/次的楼中楼检测)原本各自直调同一端点(同口径、首页均无游标),合计约 860 次/天持续触发服务端限流(-10001 network busy);合并为一次请求源后,并发调用经单飞锁只放行一次真实拉取(等待者共享结果),首页列表进实例级共享缓存 600 秒(`DISCOVERY_CACHE_TTL_SECONDS`,命中即免请求,端点调用降到约 144 次/天),撞限流则进入 30 分钟共享退避(`DISCOVERY_RATE_LIMIT_BACKOFF_SECONDS`,进入时打单条 warning,期间两消费方零请求静默返态,退避过期后首次成功真实拉取打 info「限流退避结束,恢复拉取」并复位退避与告警标记)。`QzoneAuthError` 与其他异常原样上抛(浏览层/源B 各自显式告警后跳过本轮);带游标的翻页不经过本层——浏览层积压补全时持与首页列表同源的续页游标直发。配置热重载会失效共享缓存(discovery_count 口径变更)但**不清退避态**(限流是服务端状态,清退避会在风控窗口内重新打 API)。
3. **窗口守卫**:只有当日程窗口标记了 `read_qzone`(kind=daily)时才拉取;窗口开始时激活注入泵,窗口结束时收泵并把未读浏览动态回退(`revert_pending`),同时触发见闻生成。
4. **睡眠静默**:睡眠中绝对静默,窗口守卫与泵都跳过。

拉取架构是「发现层 + 充实层」两级混合:先 1 次统一时间线调用发现全好友有什么新动态,再只对有新动态的作者逐人拉完整实体。API 量从 O(好友数) 降为 O(1 + 新动态作者数)。

## 二、完整逻辑

### 2.1 虚拟流:qzone-qq 伪群聊

- **平台名** `qzone-qq`(常量,不可配置):主程序 `get_person_id` 对含 `-` 的平台名取连字符后段计算 person 命名空间,因此虚拟流里的好友与真实 QQ 聊天里的同一人折叠为同一个 person——bot 在空间和私聊里认识的是同一个人。启动自检会验证这个折叠,失效则整个 QQ 空间模块硬停用(人物分裂不可接受,不降级)。
- **伪群**:`group_id="qzone_feed"`、`group_name="QQ空间"`(常量固化——可配置会被改成与真实群号相同的值,会话路由随之漂移)。主程序把它当一个普通群聊会话处理,session_id 由 md5(platform+account+group_id) 公式决定。
- **网关**:`@MessageGateway("receive")` 只进不出——虚拟流里 bot 直接打字发不出去,一切动作经工具(见 [qzone-act.md](qzone-act.md))。
- **场景替换**:虚拟流会话的 planner/replyer 两侧 system 段中,群聊场景提示词被原位替换为空间场景文案(`scene.py`,模板 `qzone_scene`,经三层链可被 WebUI 覆盖),告诉 bot「你在刷QQ空间,〔〕里是工具参数,不感兴趣保持沉默」。
- **浏览者身份**:注入消息的 `is_mentioned` 强制为 1.0(嵌在 `message_info.additional_config`——主程序只读该位置)——每条说说注入后必然触发一轮 planner 决策,这是「串行浏览决策环」的设计依赖:bot 看一条、决定要不要互动、再看下一条。

### 2.2 发现层:统一时间线(scope=2 好友动态流 + begintime 游标)

发现层用 **feeds3_html_more** 端点一次调用覆盖全部好友动态(`client.get_unified_timeline`;首页调用经统一入口 `_qzone_shared_discovery`,单飞+共享缓存+限流退避见「运行」段,通知源B 同源共用):

- **scope=2** 是全好友动态流(7 天窗口);scope=0 实为 2 天小窗口且本账号只见自己动态,scope=1 是「与我相关」流(互动通知在用,见 qzone-act.md)。
- **响应形态**:外层是 JSON(`{"code":0,...}`),内层 `data.main` 是 JS 对象字面量(单引号字符串、无引号键名),**不能** `json.loads`——`discovery.parse_unified_timeline` 用鲁棒正则解析:以 `key:'十六进制tid'` 定位条目,在「至下一 key 锚点」的窗口内提取 `abstime/opuin/nickname/appid`;缺任一必需字段的条目跳过(不阻断后续)。`appid` 缺失时回退解析同值 `appiconid`(窗口边界条目的真实形态)。
- **产物**是 `FeedDiscovery` 轻量索引(tid/作者 uin/昵称/时间戳/appid),不是完整实体——只回答「谁发了新东西」,正文/图片/评论由充实层补。
- **翻页是 begintime 游标**:续页唯一必需参数=顶层 `begintime`(取上页响应的 `main.begintime`,`extract_timeline_cursor` 提取,externparam 的 basetime 交叉校验),页大小 `discovery_count`(默认 50),翻页上限 `discovery_max_pages`(默认 3)。
- **四重终止**(任一命中即止步):① 空页;② 本页无新 tid(appid=311 且 seen 未登记);③ 游标耗尽(空串=到底);④ 页数上限。稳态下首页全旧即止步——恒 1 次调用;长时间离线后的积压靠游标逐页回溯补全。
- **过滤**:只认说说(`appid==311`),排除 bot 自己发的;`is_new_candidate` 纯查不登记(发现≠注入,登记留给充实层 `mark_queued`,防预占主键让充实层判重跳过)。

### 2.3 充实层:按作者拉完整实体

对发现层筛出的新动态**按作者 uin 分组**(保发现顺序),每组 1 次 `client.get_user_feeds(target_uin, num=len(group)+2)`:

- 同一原始 msglist 载荷两用不发第二次请求:`protocol.parse_msglist` 出说说实体(正文走回退链 content → 转发原文 `[转发自xx]` → `[视频]`,图片取 `pic[].url1`),`wire.parse_feed_comments_full` 出结构化评论区块。
- **结构化评论**:`FeedComment`(顶层评论,含 `comment_tid` 锚)/`FeedReplyEntry`(楼中楼)/`CommentBlock`(comments + total)。评论总数取 `cmtnum`;楼中楼取 `commentlist[].list_3`,`total` 缺失回退列表长度。评论与楼中楼正文里的 `@{uin,nick,…}` 机器格式解析为可读 `@昵称`。
- 充实页里可能混着同好友的旧动态——只注入发现层认定的 tid(集合匹配);未匹配的 tid(已被删除/超出 num 窗口)debug 留痕跳过。
- 匹配成功的动态经 `seen_store.mark_queued` 登记(幂等主键 tid,重复返回 False 跳过)后入浏览队列(P2)。
- **好友间固定 2 秒间隔**(`asyncio.sleep(2.0)`,防风控)。

### 2.4 图片管线:下载 → 拼接/直发 → 压缩预算

三个图片出口(浏览注入 / `view_friend_feeds` / `view_friend_feed_detail`)共用 `imaging.run_feed_image_pipeline`:

1. **逐张下载**(`client.download_image`):失败(返回 None 或抛错)跳过并告警,**序号保持原始位置**。域名白名单 `*.qpic.cn` / `*.qq.com`——动态载荷里的 URL 不可信,非白名单域拒绝下载(防登录 Cookie 外带与内网探测)。CDN 瞬态失败单次重试(读路径例外,动作 API 的「失败不重试」纪律不适用)。
2. **单图直返**:只有一张幸存图时直接送出,不合成、不画角标(原始序号由工具侧锚文案承担)。
3. **多图(≥2)拼一张**:`compose_numbered_grid`——3 列网格、每格 640×640 白底 letterbox(保持纵横比、只缩不放、居中),**每格左上角画圆形底数字角标,数字=入参原始序号**(失败序号跳格不重排,角标空位即诚实示缺)。输出 JPEG。拼图后恒单图注入:VLM token 与上下文成本 O(N)→O(1),角标保住「图3是什么」的可问性。
4. **压缩预算**(`messages.fit_images_to_rpc_budget`):RPC 帧物理硬限 16MB(base64 后),图片 base64 总预算 12MB。超预算走 7 档压缩阶梯(最长边 4096→512、质量 85→45)全员逐级收紧;仍超则从最大的图开始置 None(丢弃保帧限,逐次告警)。压缩移出事件循环(`asyncio.to_thread`)。PIL 缺失时跳过压缩直接走丢弃路径(显式告警)。

### 2.5 注入:build_feed_message

泵弹出一条动态后经 `messages.build_feed_message` 构造注入消息,关键契约:

- **时间语义(方案 B)**:`timestamp` = **注入时刻(阅读时间)**——消息流时钟单调递增,主程序时序机制(get_recent 24h 窗/间隔样本/连发过滤)拿到正确的到达语义;**发布时间由正文相对时间前缀承载**(`(今天HH:MM)` / `(M月d日 HH:MM)` / 跨年补年份),abstime 非法/缺失时不加前缀。
- **图片段对齐 napcat-adapter**:`data` 必须**留空**(描述槽,填占位文本会让主程序跳过 VLM 描述链)、`hash`=sha256、载荷在 `binary_data_base64`。下载/合成/丢弃失败的图不给段——由文本占位补:部分失败 `[图片]`,全失败 `[图片×N]`(N=该说说图数,模型可知图数)。
- **评论区**:正文后空行分隔拼 `format_comment_block`(首行「评论区(N条):」,QQ 截断时「前N/共M」;每条顶层评论一行「昵称(QQ):内容 (相对时间)〔评论ID=…〕」,楼中楼缩进 `↳`;每条评论楼中楼最多展开 10 条+总数标注;整块超 6000 字截断并标注)。commentlist 缺失但总数>0 时出诚实提示「本次响应未包含评论内容」。
- **锚行**:文本段末尾换行 + `〔说说ID={tid前12位}〕` 独立成行——模型照抄锚值即可调用 qzone_comment/qzone_like;纯图说说也保留文本段承载锚。
- 注入成功后 `mark_injected`(占用 awaiting)+ `mark_seen`(message_id 落库,后续通知的 reply 段引用锚)+ registry 登记 FeedContext(见 qzone-act.md)。

### 2.6 串行注入泵

`_qzone_pump` 整体持 `_qzone_pump_lock` 串行执行:超时兜底(awaiting 超 `decision_window_seconds` 强制推进,wait 态延长 3 倍硬上限)→ `next_to_inject` 弹出队首 → 构造注入 → `route_message` → 标记。泵有两个常规入口(浏览窗口 tick 与 planner 轮完成信号)加通知扫描的驱动入口,锁保证「一次只有一条动态在等决策」。取消落在「弹出→标记」间隙时 `requeue_popped` 回队首,防静默丢失。

## 三、限制与回退清单

| 场景 | 行为 |
|---|---|
| 发现层调用失败(非登录态,如超时/HTTP 5xx/响应畸形) | 告警「统一时间线拉取失败」,跳过本轮,下轮拉取间距后自然重试——发现层任何失败都不回退(逐好友 1→N+1 放大是风控帮凶,该路径已移除) |
| 发现层服务限流(-10001,QzoneRateLimitError) | 进入 30 分钟共享退避(单条告警),期间浏览与源B 零请求,期满自动恢复探测;浏览侧另打原文案告警跳过本轮 |
| 发现层登录态失效(QzoneAuthError) | 作废 cookie 下轮重取,本轮终止 |
| 充实层服务限流 | 告警后**终止本轮充实**(逐作者重试只会加重),下轮再拉 |
| 充实层单好友失败 | 逐人隔离:告警后该好友本轮跳过,不中止整轮 |
| 充实层 tid 未匹配(说说已删/超出窗口) | debug 留痕,该条跳过(不臆造) |
| 图片下载失败 | 该图跳过+告警,其余图继续;全失败按图数 `[图片×N]` 占位 |
| 拼图合成失败(含 PIL 缺失、损坏字节) | 显式告警 + 回退全占位(segments 空列表),不静默兜底 |
| 压缩后仍超 RPC 帧预算 | 从最大的图开始丢弃(逐次告警),丢弃项走占位 |
| 图片管线/消息构造极端异常 | 降级全占位注入(逐图 (url, None),正文按图给 `[图片]`) |
| route_message 被宿主拒绝(返回 False) | 不标记已见——DB 仍是 queued,窗口尾 `revert_pending` 回退未读,下窗口可重试;通知项走重试上限(见 qzone-act.md) |
| 注入在途被取消(热重载/任务回收) | 该项回队首(`requeue_popped`),告警,取消语义原样上抛 |
| commentlist 缺失但 cmtnum>0 | 评论区显示「共N条,本次响应未包含评论内容」(诚实提示,不伪装没评论) |
| 零新动态轮 | 仍然驱动泵(超时推进兜底),且算作首轮浏览完成(发布触发语义完整) |
| 窗口结束 | 持泵锁收窗:浏览队列清空、queued 行回退未读(可计数);通知队列保留;派发见闻生成(素材取近 24h 滚动窗,跨零点会话昨晚素材自然衔接;见闻语义与素材口径见 [qzone-express.md](qzone-express.md) §2.3) |
| 启动时跨窗口/跨重启的 queued 残留 | 窗口开始时 `revert_pending` 回收(重新拉取),防 seen 表判重跳过导致动态永久不可见 |
| 数据保留期 | comment_seen/like_seen 30 天、seen 表 seen 行 7 天(每日清理任务);queued 行不动(回退语义归窗口收泵) |

**已知边界**:发现层为 7 天窗口+游标回溯(上限页数);单条说说比该好友最近 N+2 条更早时充实层匹配不到(无查看通路);纯文本说说不支持带图发布(带图需图片上传通道,当前不支持)。
