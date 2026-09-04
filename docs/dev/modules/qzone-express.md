# QQ空间·表达(润色层 / 日记 / 见闻)

> 对应代码:`plugin.py` 的 `_qzone_polish` / `_qzone_expression_call` / `qzone_post` 回注段 / `_generate_and_publish_diary` / `_echo_pending_diary` / `_diary_chat_timeline` / `_qzone_generate_digest` / `_qzone_block` 段,`catsitate_core/qzone/expression.py`、`imaging.py`(图片出口,见 qzone-sense.md),`catsitate_core/llm_provider.py`(`SIDE_TEMPLATES` / `build_side_prompt` / `load_side_system`)。

## 一、职责与生命周期

表达层回答一个问题:**bot 在 QQ 空间「说什么」时,怎么说得像自己**。它与主程序 planner/replyer 的分工同构——「说什么」归 planner(它持有全量上下文与表达意图,直接写 content 草稿),「怎么说」归表达层(旁路 LLM 按人设与表达方式把草稿顺成 bot 平时的样子,不改含义)。

三条产物线:

- **表达润色**:三动作工具(评论/回复/发说说)的 content 统一经润色——模型自主决定是否/何时/写什么,插件只管口吻。
- **日记**:入睡时以当日真实素材生成第一人称日记并发布为空间说说,醒来回注。
- **见闻**:read_qzone 浏览窗口结束时把近 24h 滚动窗内的浏览与互动摘要为一段「空间见闻」,注入真实聊天。

生命周期:润色随工具调用即时发生;日记与「次日日程生成」同属入睡任务(睡眠期旁路 LLM 与发布 API 均不经消息链,深夜直发);见闻在窗口边界触发(后台任务)。

## 二、完整逻辑

### 2.1 表达润色

调用链:`qzone_comment/qzone_reply/qzone_post` → `_qzone_polish(draft, limit, scene)` → `expression.polish_action_text`:

- **prompt 组装**(`build_side_prompt("qzone_expression", stable_ctx, variable_tail)`):稳定上下文=「bot 人设」(`personality.personality`)+「你平时说话的方式」(`personality.reply_style`)+场景语(如「你正在QQ空间里,想给好友的说说写一条评论」);变量素材=【待发内容】草稿。
- **模板指令**(`qzone_expression` v6,仿主程序改写器):完全重组许可+「不要修改关键事实部分:人名、数字、时间、地点,以及明确说过的话、做过的事,都保持原样,只调整说法和语气」——顺口吻不等于改事实。
- **输出卫生**:`_sanitize` 剥首尾引号与空白(模型偶尔给正文套引号);不做字符级 emoji 过滤(会误伤表达,交给措辞约束)。
- **超长软性重润**:润色结果超 limit 时只做一次带「这次改短一些,不超过 N 字」的重润;重润仍超长就按模型原样发出——**不硬截断**(工具入参的 200/500 字校验不变,那是 planner 草稿的门槛)。
- **失败回退**:LLM 调用异常(RPC 超时等)/success=False/返回空文本——告警后以**草稿直发**(草稿本身即 planner 的完整表达,显式回退不静默);重润异常沿用首次润色结果。
- 模型与超时配置:`qzone.expression_llm_model`(默认 `replyer`,与主程序回复模型同源,口吻一致性更好)/ `expression_llm_timeout_ms`。
- 润色后的最终文本(含草稿直发形态)交内容护栏匹配(guard.md),命中即拦截。

### 2.2 日记(入睡生成 → API 直发 → 醒来回注)

`_generate_and_publish_diary`(入睡时派发,开关 `qzone.diary_enabled`):

1. **素材只取当日真实数据**(模板明令不得编造,防日记虚构没发生的事):
   - 蓝本头部:「我的名字是{bot 昵称}」+ 人设(第二人称散文体)+「今天是{年月日},回顾一下到现在为止的聊天记录:{时间线}」。
   - **聊天时间线**(`_diary_chat_timeline`):经 `message.get_by_time` **全局**拉当日 00:00 起全部消息(跨全部聊天流,不限条数;空间虚拟流消息按平台剔除——日记素材=真实聊天);逐条「[HH:MM] 谁:内容」时间序铺开,单条截 100 字尾加"...",总量超 300 条保留最近并标注「(更早的聊天已略)」;bot 标「我」,纯图/表情等无文本消息不进时间线。能力失败显式告警后回退旧逐流取数(get_by_time 不可用时)。
   - 其余素材行:当日日程活动(只取活动窗口)、到期备忘(前 3 条)、看到的好友动态(seen 表当日 3 条摘要)、当前真实天气(time_aware 快照,无数据/读取异常时**省略该行,不臆造天气**)、篇幅区间(`diary_word_count_min~max` 配置直接进素材行作字数指导,不做随机化——2026-09-04 用户裁定:目标字数随机化违反设计哲学,对齐 diary_plugin `qzone_min/max_word_count` 的配置指导形态)。
   - 素材尾「日记内容:」收尾作生成引导。
2. **生成**:旁路 LLM(模板 `qzone_diary` v7:指令含日期天气开头/像睡前随手写/反流水账要有重点和感情色彩/第一人称/输出卫生;长度口径引用素材行的篇幅区间,不硬编码数值)。生成温度可配置(`qzone.diary_llm_temperature`,0~2;-1=不传走主程序任务默认,经 `_side_llm_call` 的 temperature 参数透传给主机 `llm.generate`)。
3. **发布**:`client.do_publish` 直发为说说(不经消息链,不受睡眠拦截)。发布前经内容护栏匹配,命中即拦截(不发布不落快照)。登录态失效走同轮自愈(与 qzone_post 同款——入睡任务无用户回执,失败只有日志可见)。不设长度硬上限(长度完全由素材行的篇幅区间软约束);**空文本跳过发布**(空日记没有发布意义)。
4. **回注延迟到醒来**:正文+发布时刻+tid 存 pending 快照(`qzone_pending_diary.json`)——睡眠期 route_message 会被睡眠拦截链拦进回顾缓冲,白注入。醒态 `_echo_pending_diary`:以 self 消息补注「我昨晚发布的日记:{全文}」(+tid 锚)进虚拟流,不设 is_mentioned(仅入历史不触发决策轮);带 tid 时锚定三连(seen + registry,与 qzone_post 同款);route 失败保留快照下个 tick 重试。

**说说发布回注(qzone_post)同款语义**:发布成功后立刻以 self 消息回注「我发布了一条说说:{全文}」+〔说说ID=锚〕——后续好友评论此说说时,bot 需要这段历史才知道自己发过什么;回注失败不影响成功回执(远端已发布,谎报失败会诱导重复发布)。

### 2.3 见闻(窗口结束摘要 → 注入真实聊天)

`_qzone_generate_digest`(read_qzone 窗口结束时派发,开关 `qzone.digest_enabled`):

- **动机**:主程序会话摘要由 bot 发言后的回写服务生成,虚拟流 receive-only 无发言投递,主程序记忆层不会为虚拟流产出内容——故由插件在窗口边界自行摘要(素材→摘要→存储→注入)。
- **素材**:近 24h 滚动窗统一锚点(浏览 `recent_seen(days=1)` 与互动 `fav_events_window(now-24h)` 同窗;2026-09-04 翻案 H-2 自然日旧裁定)——浏览侧 seen 表近 1 天动态按注入时间倒序取最新 15 条(「昵称发了「摘要20字」」,纯图以「图片」占位),互动侧全部用户近 24h 好感度事件(`qzone_fav_events`)按时刻升序取尾保留最新 10 条(单条截 40 字)。跨零点窗口结束时昨晚素材自然衔接,见闻语义为「近 24h 滚动印象」而非自然日印象。窗内无素材不生成,保留旧见闻。
- **生成**:旁路 LLM(模板 `qzone_digest` v3:「回想一下最近在QQ空间的事……写成一段空间见闻」,60~150 字,一段话)。
- **存储与注入**:文本存当日快照(`qzone_digest.json`,date+text);真实聊天流的注入块(`_qzone_block`)优先取**当日**见闻(`[空间见闻] {text}`);无当日见闻回退既有「近期刷到」叙事列表(近 `summary_count` 条/`summary_days` 天,摘要截 100 字)。虚拟流分支只注入动态状态,不注入见闻。

### 2.4 prompt 模板三层链

旁路模板的 system 段经 `llm_provider.load_side_system(template_id)` 读取,三层依次:

1. `data/custom_prompts/zh-CN/catsitate_{id}.prompt`(WebUI 编辑产物,最高优先)
2. `prompts/zh-CN/catsitate_{id}.prompt`(内置部署层)
3. 插件内置默认(`SIDE_TEMPLATES`,兜底)

部署:`on_load` 时 `prompt_deploy.sync_prompt_templates()` 把 `prompt_templates/catsitate_*.prompt` 自动同步到主程序 `prompts/zh-CN/`(内容一致跳过、变更覆盖);主程序 `load_prompts()` 在插件启动后调用,同次启动即生效,无需重启。mtime 缓存+版本标签(内置版本号+文本哈希)参与缓存键——模板或占位符替换值变更即缓存失效。全部缺失时告警一次后回退内置(不静默)。

prompt 组装纪律(`build_side_prompt`,规格 §4.9/§4.10):**稳定段在前、变量素材在后**(缓存友好)——[system=任务指令+输出格式][稳定上下文=人设背景等配置数据][变量素材=每次不同的待处理内容]。

空间相关模板清单:`qzone_scene`(虚拟流场景说明)/ `qzone_expression`(润色)/ `qzone_diary`(日记)/ `qzone_digest`(见闻)。

## 三、限制与回退清单

| 场景 | 行为 |
|---|---|
| 润色 LLM 调用异常(含 RPC 超时) | 告警(异常简报,E_TIMEOUT 以「RPC 超时」显式标出)后以草稿直发,不阻断动作 |
| 润色返回失败/空文本 | 同上,草稿直发 |
| 润色超长 | 一次「改短一些」软性重润;仍超长按模型原样发出,不硬截断;重润异常沿用首次结果 |
| 日记 LLM 生成失败 | 告警跳过本轮(当晚无日记) |
| 日记内容为空 | 跳过发布 |
| 日记天气无快照/读取失败 | 素材行省略(不臆造天气),主链路不被拖垮 |
| 日记聊天素材 get_by_time 失败 | 显式告警后回退旧逐流取数;仍无消息返回空串(素材行省略) |
| 日记发布登录态失效 | 同轮自愈(作废重取 cookie 原地重试一次);自愈失败告警「内容已生成,发布跳过」 |
| 日记发布成功但响应缺 tid | 回注缺锚——告警,不误报发布失败(空 tid 同存快照,旧快照兼容口径) |
| 醒来补注 route 失败 | 保留快照,醒态 sleep_tick 下轮重试 |
| 见闻生成失败/LLM 失败/文本异常(空或超 400 字) | 告警并**保留上一份**见闻 |
| 见闻窗内无素材 | 不生成,保留旧份 |
| 模板文件缺失 | 三层链回退内置默认,告警一次(部署后自动恢复) |
| 模板文件存在但读取异常/为空 | 显式告警后回退内置模板 |
| 场景文案三层链读取异常 | 回退内置常量(与内置模板逐字一致,测试锁定) |

**已知边界**:日记只支持纯文本说说(带图需图片上传通道);见闻为单份当日快照(非逐窗口累积);润色不改变草稿长度门槛(超限草稿在工具入参校验阶段即被拒)。
