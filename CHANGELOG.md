# Changelog

## v0.8.0(2026-09-01) M3 表达:说说发布/日记/回注/见闻摘要

- **`qzone_post` 工具(说说发布)**:bot 在浏览QQ空间动态时想分享自己的心情/见闻,自主调用 `qzone_post(content)` 发布说说(≤500 字,仅虚拟流会话可用,与三互动工具同款硬门控)。是否发、发什么完全由模型决定,插件只做长度护栏;发布失败不重试,登录态失效自动作废 cookie 下轮重取。
- **说说发布 API**:`wire.build_publish_form` + `client.do_publish`——经典空间网页 cgi 表单直发,不经消息链。
- **回注**:bot 自己发布的说说以 **self 消息**注入虚拟流(`qzone_self_` 前缀 message_id,user=bot 自己,不设 is_mentioned——仅入历史不触发决策轮)——后续好友评论该说说时,bot 需要这段历史才知道自己发过什么;正文只带前 60 字预览(全文已真实发布在空间,超长挤占虚拟流)。回注失败不影响发布回执(谎报失败会诱导重复发布)。
- **日记(入睡任务旁路生成+API 直发+延迟回注)**:入睡时(与次日日程生成同属入睡任务;睡眠窗口终点未入睡时补执行,每窗口至多一篇)以旁路 LLM 基于当日真实素材(日程活动/到期备忘/当日空间见闻)生成 80~200 字第一人称日记,经发布 API 直发为说说——旁路 LLM 与发布 API 均不经消息链,不受睡眠拦截(深夜直发);模板明令不得编造素材外内容,超 300 字视为输出异常跳过。回注延迟到醒来:正文+发布时刻存 `qzone_pending_diary.json`,醒态 `sleep_tick` 以 self 消息补注进虚拟流(「我昨晚发布的日记:…」,截 60 字),补注失败保留快照下轮重试。
- **真实聊天见闻摘要叙事格式**:真实聊天流注入块的「[空间] 近期刷到」改用叙事格式「昵称发了「摘要」」(摘要截 20 字,纯图说说以「图片」占位,缺昵称回退QQ号)——与浏览动态的自然文本一致,更像转述见闻。
- **场景 prompt v3**(`qzone_scene`):互动工具清单追加 qzone_post——「想分享自己的心情就调 qzone_post(填内容)」;模板三处同步(内置/`prompt_templates/`/scene 兜底常量)。
- **配置**:`qzone` 节新增 `diary_enabled`(默认 true)/`diary_llm_model`(默认 memory)/`diary_llm_timeout_ms`(默认 0=主程序默认);工具白名单默认值并入 `qzone_post`(旧持久化配置缺该工具时 on_load 告警提示补入)。
- 旁路模板清单 9→10(新增 `catsitate_qzone_diary`,on_load 自动部署);旁路用量记账新增 `qzone_diary` 模块。
- docs:手册 §3.13 增 M3 表达内容(qzone_post/日记/回注/见闻摘要),§4.12/§5/§6 同步;CONTEXT 词汇表增术语。

## v0.7.1(2026-09-01) QQ空间提示词可读性五项优化

- **@ 解析**(`wire.parse_qzone_mentions`):通知正文里的 QQ 空间 `@{uin:xxx,nick:xxx,…}` 机器格式解析为「@昵称 」可读形态(缺 nick 回退 @uin,无 uin 畸形原样保留;@bot 自己保留不过滤,用户裁定 Q2=a)。
- **参数独立尾行**(Q1=a+Q4=a):浏览动态/通知的尾部参数从行内「(说说 xxx · 评论 x · QQ x)」改为换行+独立行「〔说说ID=xx 评论ID=xx 评论者QQ=xx〕」(浏览动态为「〔说说ID=xx〕」,tid 前 12 位)——消除与正文/时间前缀的行内语义混淆;纯图说说仍保留文本段承载参数行。参数键名用完整语义,与工具参数名(feed_id/comment_id/at_user_id)的映射由场景 prompt 解释。
- **楼中楼上下文**(Q3=a):`ReplyItem` 增 `parent_comment_content`(bot 被回复的主评论正文);源B通知正文改「回复了你的评论「{bot原评论前20字}」:…」,缺内容回退「你之前的评论」。
- **场景 prompt 可配置**:`SIDE_TEMPLATES` 增 `qzone_scene`(v2);`scene.py` 场景文案运行时经 `load_side_system("qzone_scene")` 三层链读取(WebUI custom_prompts → 主程序 prompts → 插件内置;硬编码常量降级为兜底,与内置逐字一致由测试锁定);`prompt_templates/catsitate_qzone_scene.prompt` 入列,on_load 自动部署 8→9 个模板,WebUI 可编辑、改完即生效。
- **注入块去重**:虚拟流 qzone 注入块不再拼场景全文(场景已由 apply_scene_surgery 进 system 段),只保留 `describe_current()` 动态状态——免同轮双份场景说明互相漂移;真实聊天摘要分支不变。
- docs:手册 §3.13 同步参数行格式/楼中楼上下文/场景可配置与 9 模板清单。

## v0.7.0(2026-09-01) QQ空间工具驱动架构重构

- **BREAKING:出站意图系统整体删除**(OutboundIntent/route_outbound/网关回调路由/意图绑定校验/出站计数/窗口首尾意图作废/超时清意图)。网关 `duplex`→`receive`(只进不出)——虚拟流里直接打字发不出去,互动一律经工具显式发出,是否互动/评论什么完全由模型自主决定。`routing.py`/`outbound.py` 文件保留但标记废弃(无生产调用点,便于 revert)。
- 新增 `qzone_comment` 工具:评论说说(feed_id 照抄消息尾部锚);@ 前缀支持(at_user_id,napcat 同格式,registry 有昵称用昵称);同说说频控上限 3 条(窗口边界重置);内容 ≤200 字。
- 新增 `qzone_reply` 工具:**真实楼中楼**——commentId+commentUin 二元组精确匹配主评论(@ 目标与二元组解耦,`wire.build_reply_form` 增 at_uin/at_nick);通知源A的 comment_uin=评论好友(回复他人评论的二元组=该评论+其作者,Maizone 实证),替代 M2 的「楼中楼降级头评+@」。
- `qzone_like` 改造:增 feed_id 参数(锚解析,全量 tid 回填);删 message_id 前缀校验与通知拒赞——有 origin_tid 的通知可点其原说说,无 origin_tid 的畸形通知显式拒绝。
- 新增 `FeedContextRegistry`(内存 LRU,128 条/48h):泵注入成功后登记说说上下文(主人/评论者/主评论二元组);工具目标三级解析 registry(精确/前缀)→ seen_store(7 天浏览窗)→ awaiting,解析失败显式拒绝。
- 消息格式带 ID 锚:浏览动态文本段末尾「(说说 xxx)」(tid 前 12 位,**纯图说说也保留文本段**);通知正文改「评论了你的说说:…(说说 xx · 评论 xx · QQ xx)」/「回复了你的评论:…」自然可读+锚。
- 场景 prompt v2:说明 ID 锚格式与三工具用法,明示沉默自由与「打字发不出去」。
- 白名单默认值:删 `reply`(receive 网关下无效)增 `qzone_comment`/`qzone_reply`;旧配置 on_load 兼容告警(缺新工具/含废弃 reply)。
- 测试:意图/网关路由测试重写为工具行为测试(成功/@前缀/频控/解析失败/AuthError/二元组/通知登记);`test_qzone_routing.py`/`test_qzone_outbound.py` 删除(模块已废弃)。

## v0.6.0(2026-08-31) M3:统一时间线架构重构

- 浏览流重构为「发现层+充实层」两层混合:feeds3_html_more 1 次调用覆盖全好友统一时间线,仅对新动态按好友拉取充实。API 量从 O(N好友) 降为 O(1+新动态),24/240 好友成本相同。
- 通知源B搭发现层便车:仅对「有新活动+bot 评论过」的好友拉楼中楼,零交集零 API。
- QzoneClient API 分层封装(发现/内容/写/基础);发现层失败回退逐好友旧路径(告警)。
- 源B好友数硬上限(10)已删除——不再需要截断。

## v0.5.2(2026-08-31) 二轮深度审查修复(QQ空间)

- fix:通知注入重试上限 3 次(深度审查 A-N1)——被拒/异常回退改**软回退**(登记行保留,计数跨重发现累计),满 3 次仍被拒保留登记放弃,防宿主持续拒绝时同一通知每 120 秒无限重注入;`qzone_comments` 增 `retry_count`/`pending_retry` 两列(旧库自动迁移)。
- fix:`qzone_fav_events` 同日同 user+kind+text 去重(A-N1)——通知回退后重发现不重复入库,防结算素材被重复放大。
- fix:日终结算候选改并集(batch 当日活跃 ∪ 当日空间事件,深度审查 C-N1)——纯空间互动好友(无 batch 行)原实现永不结算;bot 自身排除。
- fix:`qzone_cookies.json` 存量文件收紧为 0600(凭据属主可读,比照 SQLiteStore 纪律)。
- fix:通知轮询源B好友数硬上限 10(F4,防 API 量随好友数线性失控,超出截断告警)。
- fix:后台任务异常日志显式传 `exc_info`(done 回调无异常上下文,裸 exception() 不附栈)。
- docs:手册 §3.13.8 补意图绑定校验依赖 `enable_reply_quote=true` 与多段回复仅首段成评论;§3.7/§5.1/§6 同步软回退与并集语义。

## v0.5.1(2026-08-31) M2.1:统一通知通道

- 评论轮询重构为统一通知通道:高频短间隔(默认 120 秒)双源检测(自己说说新评论+他人说说楼中楼回复),模拟推送通知体验。
- 注入队列改双优先级(P1 通知插队/P2 浏览动态),阅读顺序改新→旧(信息流降序)。
- 工具双向隔离:qzone_like 仅在 qzone 虚拟流可见,真实 QQ 流自动隐藏。
- 通知 FeedItem 意图路由:源A→楼中楼回复(bot 自己说说)/源B→楼中楼回复(好友说说);生产注意:源B API 量随曾评论好友数线性增长。

## v0.5.0(2026-08-31) 三期 M2:QQ空间互动

- 虚拟流出站经意图状态机路由为真实动作:浏览窗口内对当前说说的回复→空间评论;窗口外评论轮询(好友在 bot 说说下的新评论→注入→楼中楼回复)。动作 API 失败不重试,登录态失效自动作废 cookie 下轮重取。
- `qzone_like` 工具:planner 浏览时可对当前说说点赞(方法内 stream_id 硬门控——SDK Tool 无类级 allowed_session 通道,联调实证)。
- 好感度显式事件(spec §3.9):空间评论双向计入日终结算素材(LLM 计权,事件按原始时刻去重防同日重判)并参与衰减计时;不依赖 batch_counter。
- memo 按人重构(spec §3.10):条目=主QQ+附带QQ(≤5),跨流可见;流维度保留;memo_write 增 related_user_ids 参数;群聊说话人经消息映射解析。
- 注入缓存 LRU 上限(assembler 512/快照 256);见闻摘要带作者昵称(旧库自动迁移)。
- 评论轮询配置:comment_poll_enabled(默认开)/comment_poll_interval_minutes(默认 30);工具白名单默认并入 qzone_like。
- 生产注意:写路径有真实副作用(评论/点赞发布);点赞的 own-feed 枚举无 API,好感度点赞事件仅工具路径。

## v0.4.0(2026-08-30) 三期 M1:QQ空间感知

- 新增 `qzone` 配置节与 `catsitate_core/qzone/` 模块包(协议客户端/去重存储/消息构造/注入状态机/场景纯函数)。
- QQ空间动态映射为 `qzone-qq` 虚拟群聊流(连字符别名与真实 QQ 统一 person),串行注入复用主程序 planner→replyer 链;注入带 is_mentioned 强制触发;消息时间戳=阅读时刻,发布时间以相对时间前缀写入正文(方案 B),图片带 base64 交主流水线处理。
- 图片组件对齐 napcat-adapter(data 描述槽留空),VLM 描述链实证打通。
- 日程窗口新增 `qzone` 属性(仅 daily 合法),`schedule_generate` 模板升 v3;标记窗口内按 `poll_interval_minutes` 拉取。
- 虚拟流专属:群聊场景提示词原位替换(planner+replyer 两侧,失败告警回退注入块语义说明)、工具白名单过滤(默认不含 tool_search/msg_react/poke_user)、deferred reminder 剥除。
- 模块豁免:好感度计数/晚安判定/daily 窗口候选排除虚拟流;流缓存纳入 qzone-qq;贴表情/戳一戳平台自检拒用。
- 出站一律显式拒发(评论路由 M2 交付);动作 API 不重试(图片下载读路径单次重试例外)。
- 生产注意:NapCat 需可响应 `adapter.napcat.account.get_cookies`;`experimental.focus_mode` 必须关闭(否则模块自检停用);person 折叠自检失败将硬停用模块(不降级——人物分裂不可接受);talk_value=0 时模块自检停用(注入会被主程序静默消费);虚拟流学习落在自身 session,勿配置 `*:*` 全局表达共享组;模板 v3 变更后 WebUI 自定义的 schedule_generate 需手动同步。

## v0.3.2(2026-08-18,旁路模板自动部署)

- **旁路模板自动部署**:插件加载时(`on_load`)自动把 `prompt_templates/catsitate_*.prompt`(8 个)同步到主程序 `prompts/zh-CN/`(内容一致跳过、变更覆盖;主程序 `load_prompts()` 在插件启动后调用,同次启动即生效,无需手动复制/重启)。WebUI「提示词管理」即可查看/编辑这 8 个模板(编辑产物写 `data/custom_prompts/zh-CN/`,插件优先读取)。插件不在 `plugins/` 下或主程序 `prompts/zh-CN/` 缺失时显式告警跳过,不阻断加载,插件回退内置默认
- 含单元测试(首次部署/幂等跳过/变更覆盖/目录缺失跳过/无关文件不动/写入失败显式告警)

## v0.3.1(2026-08-18,公测修复集)

- **睡眠窗口语义对齐**(联调裁定):睡眠窗口 = 可入睡时间——静默关闭 = 窗口起点直接入睡;静默开启 = 窗口起点后安静满 `silent_sleep_minutes` 分钟入睡(计时基准 = max(窗口起点, 最后活动时刻));晚安判定仅在睡眠窗口内有效(与静默开关无关);窗口终点仍未入睡 → 不入睡,但补执行入睡时的任务(次日日程生成,每窗口一次)
- **跨午夜睡眠窗口保留**:过期日程(早于今天)若其睡眠窗口仍覆盖当前时刻则保留恢复;换日 tick 时旧日程睡眠窗口仍在进行则保留旧日程直至窗口结束(公测发现:直接删除/替换会导致当天无法入睡)
- **配置修复**:7 个 `*_timeout_ms` 字段默认值 `None`→`0`(0=主程序默认超时),修复主机 tomlkit 无法序列化 None 导致的配置回写崩溃、插件激活失败(含回归测试)
- **RPC 帧超限修复**:取消息不再携带二进制数据(大附件消息曾撑爆 16MB RPC 帧),结算/衰减取数按流隔离(单流失败只跳过该流)
- 命令修复:命令别名多余斜杠;新增 GPL-3.0 许可证与 config_back 忽略规则

## v0.3.0(2026-08-16,按人重构)

- 好感度按人唯一标识(user_id):favorability 单行按人存储与判定(跨流聚合),日终结算按人合并一次,衰减计时按人跨流取最近互动;`batch_counter` 保留 (user, stream) 行仅作活跃度记录
- 「特别」等级独占:全表任意时刻最多 1 人「特别」(≥100 分),他人升入被占位时钳制 99 分(挚友)并显式日志;独占者掉出后空位释放
- 主动问候统一(原 2.3 与 greeting 合并):仅「特别」等级 + 私聊流存在,greeting 窗口起点触发,无每日一次限制;删除 `greet_threshold_level`/`private_threshold_level` 配置
- 配置字段清理:删 `speak_threshold_level` 以外旧门槛配置;`batch_counter` 移除 `window_start` 死列(旧形状检测自动重建)
- 公测前最终审查修复:好感度分数负分钳制(最低 0,规格 §3.1);次日日程夜间重启恢复(date ∈ 今天/明天,过期才删);LLM 异常日志只记异常类型(防请求体/PII 入日志);日终结算逐用户 try/except 隔离;debug 日志卸载清理(on_unload 移除并 close handler、恢复 logger 级别);manifest 能力对账(删除零调用能力)与版本号 0.3.0;衰减天数判定改浮点(「距今 > N 天」语义);early 幂等键加用户后缀;remind_fired 兜底 tick 同步日键清理;fav_count 同查睡眠模块开关;catsitate.db 与睡醒回顾报告 0600 权限;旁路告警阈值 `==` 改 `>=`

## v0.2.0(2026-08-15,二期)

- 好感度自然衰减:LLM 判定拟人化衰减(未互动 N 天,0~-decay_max),群聊 quote/@ 防误判
- 睡眠管理:睡眠=日程窗口(LLM 自主作息)、绝对静默拦截、晚安判定入睡、睡醒回顾报告
- 日程:入睡时生成次日动态活动日程(1 睡眠+1~8 活动)、日程块注入、日程窗口 trigger 主动发言(表达权交主程序)、update_schedule 工具
- 主动私聊:挚友级私聊问候(2.3)
- 备忘录提醒时间 remind_at:日程收录 + 无日程独立兜底注入
- 旁路模板:二期新增 4 个(decay/sleep_confirm/schedule_generate/sleep_review),随一期机制接入主程序「提示词管理」页统一编辑

## v1.0.0(2026-08-15,一期联调完成)

主要功能:
- 注入框架:环境/备忘/好感度块前插 system 之后(缓存友好分层注入);等级规则按等级单条注入好感度块最前(联调决定,省 token 且提升缓存命中)
- 好感度:批次结算制(提前结算 + 日终兜底顺延)、LLM 判定结合 bot 人设背景、delta 上限可配置(delta_max)、5 级规则独立配置字段、bot 发言识别(bot_user_id)
- 备忘录:双通道(工具+命令)、单条 TTL、注入当前流+说话人两维度
- 贴表情:内置 30 项精选 QQ 表情表(替代可配置白名单,联调决定)+ 每流冷却,仅群聊可用
- 戳一戳:主动戳工具(仅冷却限制);入站通知解析已按联调结论删除
- reply 上下文补传(规则层)+ LLM 哨兵层(可选,默认关)
- 图片重看工具(VLM)
- 时间感知:节日/节气/天气环境块(默认珠海;lunar-python 缺失时公历回退链不受影响)
- 旁路 LLM:经主程序 model_task_config 的 task 名路由(各能力独立配置 + 独立超时);4 个能力 prompt 模板接入主程序「提示词管理」页统一编辑(custom_prompts 覆盖优先,模板变更即缓存失效)

细节:
- 旁路 LLM 调用记账(llm_usage)+ 每日告警阈值
- 60s 后台调度器(天气/节日/备忘清理/日终结算),周期随配置热重载
- 92+ 单元测试;实机联调验收通过(见 docs/acceptance-checklist.md)
