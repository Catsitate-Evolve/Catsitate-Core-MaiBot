# Catsitate Core MaiBot 插件设计文档

- 日期:2026-08-14
- 插件名:`catsitate_core_maibot`(目录 `MaiBot-dev/plugins/catsitate_core_maibot/`)
- 插件 ID:`catsitate.core`
- 状态:设计已与用户逐轮确认(grilling 收敛)

## 1. 背景与目标

Catsitate 是部署在 MaiBot 上的 QQ 聊天机器人人设(伪三无猫耳少女,信息海意识体)。本插件是 Catsitate 的**核心人格行为插件**,目标是:

1. 通过插件优化扩展 MaiBot 本体功能,不修改主程序(除非单独申请许可);
2. 优化请求结构使提示词缓存命中率尽量 ≥80%(插件侧:缓存友好纪律 + 用主程序既有指标验证,主程序瓶颈单独报告);
3. 实现开发目标文档中的模块(分期交付);
4. **摒弃单纯概率行为**:行为决策交给 LLM/状态,随机数只允许出现在工程护栏(冷却、限频、防刷);
5. 模块化、解耦合、整洁规范;不重复造轮子。

## 2. 已确认决策摘要

| 项 | 决策 |
|---|---|
| 形态 | 单插件多模块(一个 git 仓库,内部按模块拆文件,配置开关独立启停) |
| 分期 | 一期=2.4 全组+2.3 基础;二期=2.1 日程+2.3 主动私聊联动;三期=2.2 QQ空间 |
| 缓存 | 注入分层分条(规则表→环境→备忘→好感度,按稳定性降序)+版本化+长度源头控制;前插 system 后、历史前;**旁路 LLM 请求同样遵守稳定前缀纪律(§4.10)**;旁路 LLM 有预算与计数;测量用主程序日志/统计 |
| 存储 | 标准库 `sqlite3`(插件 data 目录单库);轻量限频状态用 JSON 快照 |
| 好感度 | LLM 判定;v3 窗口结算制(不配对、不语义分类,私聊/群聊差异化材料,纯计数触发);注入=等级(5级)+关系注记(A+C) |
| reply 拦截 | 上下文补传(查了上下文工具、reference 与 reasoning 双空才补)+ LLM 哨兵可选开关;锚点 `maisaka.planner.after_response` |
| 戳一戳 | 入站解析增强(补拟人渲染)+ 主动戳工具(好感度门槛);**被戳反应逻辑剔除** |
| 贴表情 | 仅 `@Tool`,无概率旁路 |
| 备忘录 | 双通道(工具+命令)各自开关;注入含当前流+当前说话人两个维度 |
| 时间感知 | 主程序已注入当前时间;插件只做节日/节气(在线 holiday-cn → holiday-calendar 库 → 内置表;农历/节气内置预生成 2025–2030) |
| 天气 | Open-Meteo(免 key)+ 全局城市配置;注入上下文、落库供联动;失败静默跳过 |
| 图片 | 信息流图片由主程序处理;插件只提供 2.4.6 深看工具 |
| LLM 配置 | `@LLMProvider` 声明 `catsitate_custom`;每个 LLM 能力配置可选主程序模型或自定义端点 |
| QQ空间(三期) | 虚拟聊天平台(`@MessageGateway`)+ 自研 qzone 接口,蓝本为原版 [internetsb/Maizone](https://github.com/internetsb/Maizone)(`maizone_refactored` 停更过久,仅参考) |
| 交互语言 | 所有用户可见文本简体中文 |

## 3. 总体架构

### 3.1 文件结构

```
plugins/catsitate_core_maibot/          # 独立 git 仓库
  plugin.py                             # 薄入口:插件类 + create_plugin()
  _manifest.json
  CHANGELOG.md
  README.md
  .gitignore                            # 含 /config.toml
  docs/superpowers/specs/               # 本设计文档
  catsitate_core/                       # 内部包(绝对导入)
    __init__.py
    config.py                           # 配置模型(PluginConfigBase+Field 嵌套 section)
    storage.py                          # sqlite3 薄封装 + JSON 快照读写
    inject.py                           # 注入框架:所有注入的统一出口(缓存纪律在此保证)
    time_aware.py                       # 2.4.4 节日/节气/天气感知
    favorability.py                     # 2.3 好感度(计数、窗口结算引擎、LLM 判定、注入)
    memo.py                             # 2.4.1 短时备忘录
    msg_react.py                        # 2.4.2 贴表情
    poke.py                             # 2.4.5 戳一戳(解析增强+主动戳工具)
    reply_guard.py                      # 2.4.3 reply 误调用拦截
    image_relook.py                     # 2.4.6 图片重看
    llm_provider.py                     # @LLMProvider 声明(catsitate_custom)+ 旁路 LLM 请求组装辅助(稳定段前置)
    services/
      __init__.py
      scheduler.py                      # 后台 asyncio 任务引擎(60s tick,各模块注册任务)
  tests/
    test_storage.py / test_favorability.py / test_time_aware.py / test_reply_guard.py ...
```

> 实现期 spike 清单:① 插件加载器是否支持子包(插件目录加入 `sys.path` 后绝对导入 `catsitate_core.*`),不支持则退化为同目录平铺多模块;② `before_request` Hook 中 `items` 的插入语义——构造合法 context item(`item_schema_version`)插到 system 之后的容忍度,失败则回退追加尾部。

### 3.2 模块依赖与数据流

```
入站消息 ──chat.receive.before_process(观察)──> poke.py(解析增强)
         ──chat.receive.after_process(观察)───> favorability.py(计数)
chat 主链路 ──maisaka.planner.before_request──> inject.py(尾部注入:节日/天气/好感度/备忘录)
           ──maisaka.planner.after_response──> reply_guard.py(移除误调用)
发送链路 ──(无配对逻辑)── 好感度不依赖发送事件
后台 scheduler.py(60s tick):
    - time_aware.py  天气刷新(30–60min)/节日数据刷新(每日)
    - favorability.py 日终结算/高活跃结算
    - memo.py 过期清理
工具注册:msg_react / poke_user / memo_write / memo_read / inspect_image
LLM 路径:统一经 ctx.llm.generate + 每能力可配模型(含 catsitate_custom provider);旁路请求统一经 llm_provider.py 组装辅助(稳定段前置)
```

模块间通过内部接口调用(如天气/好感度数据都经 `storage.py` 读取),互不 import 实现细节;每个模块可独立启停(配置开关),关闭时其注入与后台任务均不生效。

### 3.3 插件生命周期

- `create_plugin()` 返回插件实例;插件类继承 `MaiBotPlugin`,声明 `config_model`;
- `on_load`:初始化 sqlite、注册后台任务、读取主程序配置(如 personality);各模块按开关初始化;失败报错拒绝加载(不静默);
- `on_unload`:优雅停止后台任务、关闭 sqlite、持久化轻量状态;
- `on_config_update(scope, config_data, version)`:刷新派生缓存与模块开关状态;`config_reload_subscriptions` 订阅 `bot` 全局配置(感知 personality 变化)。

## 4. 一期模块设计

### 4.1 注入框架(`inject.py`)— 缓存纪律的唯一出口

- 唯一入口:`maisaka.planner.before_request` Hook(BLOCKING/LATE,`allow_kwargs_mutation=True`),绝不改动 system prompt 与历史内容;
- **注入位置(缓存关键,排列分析结论)**:主程序请求顺序为 `[system][历史][主程序动态注入][时间][tail][注意事项][assistant提醒]`,其中时间/tail/启发式记忆每轮必变,且历史每轮在**末尾追加**新消息。前缀缓存语义下,插在历史之后的任何内容都无法进入连续请求的公共前缀(上轮 `[历史][注入块]` 与本轮 `[历史][新消息][注入块]` 在注入块处即分叉)。因此注入采用**system 之后、历史之前前插**:
  - 目标顺序:`[system][等级规则块][环境块][备忘块][好感度块][历史][主程序动态注入][时间][tail][注意事项][assistant提醒]`;
  - 效果:注入块与全部旧历史进入公共前缀,动态段全部留在其后——"当前时间"等必变消息不再截断注入块的缓存;
  - 语义无损害:注入块自解释(带标签),紧贴 system 相当于扩展系统上下文;
  - 主程序自有消息**不重排**(收益仅"注意事项"几百 token,语义风险大);
  - spike 验证:system 后紧跟 user item 的协议合法性与主程序反序列化容忍度;插入失败回退追加尾部(仅日志,不阻塞主链路);
- **注入分层分条(缓存分层结构)**:各模块注入内容**不合并**,按更新频率从低到高排列为独立块,每块一条 user 消息:
  - 顺序固定:`[等级规则块] [环境块(节日+天气)] [备忘块] [好感度块]`——规则表仅随配置变化(最稳,几乎永久命中);环境 45 分钟/日级;备忘集合驱动;好感度按当前说话人注入(群聊相邻消息换人率通常 50–90%,几乎每轮变化,故放最后;私聊说话人固定则该块稳定数小时);
  - 效果:任一后部块变化不影响前部块的缓存命中(前缀缓存分层失效);空块跳过不产生消息;
  - framing 开销 ~30 token/请求(4 条消息),相对缓存收益可忽略;
- **注入版本化**:每个块由 (模块, 内容 key, 内容 hash) 标识,内容未变时字节级复用上一轮渲染结果,保证跨请求稳定;
- **长度源头控制(注入管线不设截断)**:各块长度在源头强制——备忘在写入时强制 ≤80 字符(工具描述声明约束+实现校验,超长返回错误让 LLM 重写;命令方式超长直接提示用户),每维度 ≤3 条、合计 ≤5 条;注记在结算落库时强制 ≤40 字符;环境块内容天然短小;规则表为配置文本由用户自控;
- 失败原则:任一注入源出错仅记录日志并跳过该小节,不阻塞主链路;
- 命中率验证:对比主程序日志 `Planner缓存:...hit_rate=xx%` 与 `llm_cache_stats` 诊断,一期交付附基线对比报告。

### 4.2 时间/节日/天气感知(`time_aware.py`)

- **节日**:在线主源 holiday-cn(jsdelivr CDN,备 raw.githubusercontent),每日后台刷新一次,失败回退 holiday-calendar 库(manifest `python_package` 依赖自动安装),再失败用内置静态表(2025–2030);
- **农历节日+节气**:内置预生成表(2025–2030),随插件发版更新;
- **天气**:Open-Meteo(免 key),全局城市配置(默认北京,含坐标),30–60 分钟后台刷新;天气码→中文映射表内置;天气快照落 sqlite(`weather_snapshot` 表)供二期 2.1 联动;
- 注入片段示例:`[环境] 今天 8月14日 周五,北京:晴,29°C;明日:七夕。`(当天+临近 3 天节日/节气);
- 拟人感:城市名出现在注入文本中,bot 表现出"生活在这个城市"的感知;天气失败时静默跳过该片段(日志记录)。

### 4.3 好感度(`favorability.py`)— v3 窗口结算制

**判定单元**:(用户 × 时间窗口),默认窗口 24h(可配)。**不做消息配对、不做语义分类**。

- **计数**:`chat.receive.after_process`(OBSERVE)记录当日每用户消息数(内存计数+定期落库,重启从库恢复);
- **触发(纯计数)**:
  - a) 日终结算:每日定时对当日活跃用户统一结算(每活跃用户 1 次/日);
  - b) 高活跃提前结算:窗口内该用户消息数 ≥ N(默认 20)提前结算并开新窗口;每用户每日上限 K 次(默认 3);
- **材料构造(私聊/群聊差异化)**:
  - 私聊:窗口内对话切片(该流内用户消息+bot 消息,天然交替,归属明确);
  - 群聊:该用户全部消息 + bot 在该群全部发言 + 每条用户消息紧邻上下文(前后各 1–2 条),供 LLM 自行判断 bot 是否在回应 ta;
  - 素材按**条数**计上限:取窗口内该用户相关最近 N 条消息(默认 30,可配);**单条**消息超过长度上限(默认 200 字符,可配)截断该条(加省略标记),截断发生在单条尾部、消息边界之间,不产生跨条切断;所有消息素材**按时间正序拼接**(稳定增量);
- **LLM 判定**:prompt 模板固定,结构 = `[判定指令+输出格式][5 级规则][窗口素材]`(稳定段在前、素材在后,§4.10 旁路规范),输出 JSON `{delta: 整数(-5~+5), note: 一句话关系注记}`;模型可配(默认主程序任务,可选 `catsitate_custom`);失败跳过本轮并记录日志;结果落 sqlite `favorability` 表;
- **注入(Q8 A+C,同一模块的三个组件拆两块)**:好感度模块共有三个注入组件——①5 级行为准则表(陌生/熟悉/亲近/挚友/特别)、②等级+分数、③关系注记。按更新频率拆成两块:规则表仅随配置变化 → 独立"等级规则块"(最稳,几乎永久命中);等级+分数+注记同为 per-user、说话人驱动 → 合并为"好感度块"(注记与等级同频,拆开无缓存收益)。好感度块内容:`[好感度] XXX:等级「熟悉」(累计 42),注记:最近主动关心过你。`;私聊=对端用户,群聊=当前消息发送者;等级/注记变化(结算)才更新该块;
- **存储 schema**:`favorability(user_id TEXT, stream_id TEXT, level INTEGER, score INTEGER, note TEXT, window_start TEXT, judged_at TEXT, PRIMARY KEY(user_id, stream_id))`;判定日志表 `favorability_log(judge_id, user_id, stream_id, delta, note, judged_at)` 幂等防重;
- 二期扩展:达标用户主动私聊(依赖 2.1 调度)。

### 4.4 短时备忘录(`memo.py`)

- 双通道(各自配置开关):
  - `@Tool("memo_write"/"memo_read")`:planner 自主记取;写入参数:内容、关联流/用户、`ttl_hours`(可选**单条有效期**,缺省用 default_ttl_hours;按内容需要延长——如"周四交作业"可设到周四,避免按统一时长提前失效);读取返回:当前流相关未过期备忘及**各自剩余有效时间**;工具描述声明内容 ≤80 字符、`ttl_hours ≤ max_ttl_hours` 约束,实现中校验,超限返回错误让 LLM 重写;
  - `@Command("/记一下", pattern=r"^/记一下\s+(?P<content>.+)$")`:用户显式让 bot 记备忘(使用默认 TTL);超长(>80 字符)直接提示用户精简;
- 存储 sqlite `memo(id, content, stream_id, user_id, expires_at, created_at)`;默认有效期 24h、单条上限 168h(均可配);后台任务清理过期项;
- 注入:**当前流相关 + 当前说话人相关**(user_id 维度,含用户在其它流留下的备忘)的活跃备忘,最近 N 条(默认 3),经注入框架合并追加:`[备忘] 用户说过:周四要交作业。`;

### 4.5 贴表情(`msg_react.py`)

- `@Tool("msg_react", visibility="visible")`,参数:目标消息 ID、贴表情意图(可选文字);
- 执行:表情白名单配置(emoji_id 表)→ 小 prompt LLM 从白名单选最合适的表情(模型可配,prompt 结构 = `[任务指令+输出格式][白名单][目标消息+意图]`,§4.10 旁路规范)→ `ctx.api.call("adapter.napcat.message.set_msg_emoji_like", ...)`;
- 防刷:每流冷却(JSON 快照限频,仅工程护栏);无任何概率旁路。

### 4.6 戳一戳(`poke.py`)

- **入站解析增强**:`chat.receive.before_process`(OBSERVE)读取通知消息 `additional_config.napcat_notice_payload`(适配器已完整保留原始载荷),补全拟人文本:「XXX 拍了拍你,说:"…"」(raw_info 动作文本 + 昵称解析);配置开关:`enhance_notice_text`(改写消息文本)/`inject_to_context`(注入上下文);
- **主动戳工具**:`@Tool("poke_user", visibility="visible")`,参数:目标用户/流;前置校验:好感度等级 ≥ 门槛(默认"熟悉")+ 每用户冷却;调用 `ctx.api.call("adapter.napcat.message.send_poke", ...)`;
- **被戳反应逻辑:不实现**(已剔除)。

### 4.7 reply 上下文补传与拦截(`reply_guard.py`)

锚点:`maisaka.planner.after_response`(在 reply 工具真正执行前触发,可改写 `output_items`,已核实)。

- **上下文补传(规则层,必选,零成本)**:
  - 事实依据:replyer 自带完整聊天历史+被回复消息块;`reply_reason`(planner 的 reasoning,主程序自动提取,`reply.py:303,361`)为空时 replyer 参考块完全缺位;planner 查到的记忆/人物信息(`query_memory`/`query_person_profile`/`fetch_history`/`view_forward_message`/`memo_read` 等上下文工具的结果)只有写入 `reply_reference` 才会进入 replyer,写不写全凭模型自觉;
  - 触发条件(确定性,零误伤):本轮 planner 调用过上述上下文工具(集合可配),**且**该 reply 调用的 `reply_reference` 为空,**且**其关联 reasoning(即主程序将提取为 `reply_reason` 的内容)为空;
  - 动作:**自动补传**——把对应工具结果的文本摘要(截断)填入该 reply 调用的 `reply_reference` 参数;不改动其它工具调用;
  - 不重复主程序的重复回复提醒兜底。
- **LLM 哨兵层(可选,配置开关默认关)**:`maisaka.replyer.after_response` 判定"本次回复是否与聊天上下文不符/是否不该回复",不符则撤回并闭环反馈 planner(corpus-callosum 式);prompt 结构 = `[哨兵指令][人设/等级背景(可选,稳定)][待判定回复+聊天上下文(变量)]`(§4.10 旁路规范);
- 改写动作 = 修改/删除 `output_items` 中对应项(证据:`reasoning_engine` 在 after_response 之后才执行工具调用)。

### 4.8 图片重看(`image_relook.py`)

- `@Tool("inspect_image", visibility="visible")`,参数:目标消息 ID(或 image_index)+ 具体问题;
- 执行:`ctx.message.get_recent(include_binary_data=True)` 解析 image 段;仅 hash 时经 `ctx.db.get(model_name="Images", ...)` 补读原图;VLM(视觉模型配置项)回答具体问题,返回文本结果;prompt 结构 = `[任务指令(稳定)][图片+问题(变量)]`——图片 token 本身无前缀缓存意义,仅保证文本前缀稳定(§4.10 旁路规范);
- 不实现根目录启发式探测(参考插件该做法绕过 SDK 边界,我们不用);若 db 无法补图,报错暴露并记录日志。

### 4.9 LLM Provider(`llm_provider.py`)

- `@LLMProvider(client_type="catsitate_custom", name="Catsitate 自定义端点", ...)`:仅实现 `response` 操作(embedding/audio 返回"不支持");处理器接收 `api_provider` 快照(用户在 model_config 配置的 base_url/key),以 OpenAI 兼容协议自行调用并返回统一格式;
- manifest `llm_providers` 与装饰器声明一致;
- 每个 LLM 能力配置节:`{enabled, model}`——`model` 填主程序 task 名/模型标识(留空=插件默认),或指向用户自定义的 `catsitate_custom` 端点;
- 同文件提供**旁路请求组装辅助**:统一 `build_side_prompt(template_id, stable_ctx, variable_tail)` 渲染(模板固定+版本化、稳定段在前),§4.10 旁路规范的唯一实现落点。

### 4.10 Token 开销与缓存预算

**开销全景(插件引入的所有 token 来源)**:

| 来源 | 路径 | 频次 | 预算与缓存手段 |
|---|---|---|---|
| 环境/好感度/备忘注入 | 进主链路(system 后前插) | 每轮 planner 请求 | 分层 4 条,长度源头控制(无注入截断);每块版本化 |
| 好感度结算判定 | 旁路 `ctx.llm.generate` | 每活跃用户 ≤3 次/日 | 素材按条数上限(默认最近 30 条)+ 单条超长截断(默认 200 字符);固定模板稳定段在前(下方旁路规范) |
| 贴表情选表情 | 旁路 | 每次贴表情 | 极小 prompt(白名单稳定段在前);固定模板 |
| reply_guard 哨兵层 | 旁路 | 默认关 | 配置开关;开启时同样遵守旁路规范 |
| 图片重看 | 旁路 VLM | planner 主动调用 | 需求本身;模型可配;文本前缀稳定 |

**主链路缓存命中优化**(供应商前缀缓存语义下):
- 插件承诺不碰 system prompt 与历史(最长公共前缀不受损);注入块**前插到 system 之后、历史之前**(§4.1 排列分析),使注入块与旧历史整体进入公共前缀;动态段(时间/tail/主程序动态注入)全部在其后,仅损失其后少量 token;
- 块级波动隔离:分层分条后,任一后部块变化只失效自身及之后的块(见 §4.1);环境块 45 分钟级稳定(天气)、日级稳定(节日);好感度块仅在等级/注记变化时更新(群聊按当前说话人,换人即换块内容,故排最后);备忘块仅在活跃备忘集合变化时更新,条数有上限;
- 主程序自带的"当前时间"尾部消息本身每轮变化,插件不再叠加更多动态片段;
- 验证方法:基线(插件关)→ 逐模块开启,观测日志 hit_rate 与 `llm_cache_stats` 诊断,定位波动源后再调更新频率。

**旁路 LLM 请求缓存规范**(不进入主聊天流的所有 LLM 请求,缓存目标同样成立):
- 适用范围:好感度结算、贴表情选表情、哨兵层、图片重看,以及二期/三期全部 LLM 能力;
- 与主聊天流的关系:旁路请求携带的是插件自己的任务指令模板,与主聊天流 system 前缀互不共享(也不必共享);**缓存目标是同类任务内部的稳定前缀命中**——跨用户、跨次调用共享同一稳定段;
- 统一 prompt 结构:`[任务指令+输出格式(固定模板,版本化)][稳定上下文(5 级规则/白名单/人设背景)][变量素材]`——稳定段在前、变量段在后,模板版本变更才改前缀(自然失效,不追求兼容);
- 各能力排布:

  | 能力 | 稳定段(共享前缀) | 变量段(尾部) |
  |---|---|---|
  | 好感度结算 | 判定指令+输出格式、5 级规则 | 窗口素材(时间正序;按条数取最近 N 条,单条超长截断在单条尾部) |
  | 贴表情 | 任务指令+输出格式、表情白名单 | 目标消息+意图 |
  | 哨兵层 | 哨兵指令、人设/等级背景(可选) | 待判定回复+聊天上下文 |
  | 图片重看 | 任务指令 | 图片+问题(图片 token 无前缀缓存意义) |

- 素材纪律:消息类素材一律时间正序拼接(稳定增量);素材边界与截断一律落在消息单元之间(按条数取最近 N 条,单条超长截断在单条尾部),不改变已固化前缀;
- 频次与收益:旁路请求低频(好感度 ≤3 次/用户/日),缓存收益绝对值小,但该纪律近乎零成本,且为二期(日程 LLM)/三期(QQ空间信息流)的高频旁路请求打底;
- 实现落点:`llm_provider.py` 提供统一请求组装辅助(模板渲染+稳定段前置),各模块只填素材段,缓存纪律不散落在各模块。

**可观测性**:旁路 LLM 调用次数与 token 用量计入插件自建 `llm_usage` 表(按模块分列),README 说明查看方式;超过每日调用数阈值(可配)记录告警日志。

## 5. 二期/三期概要(不在本期实现,仅留接口)

- **二期 2.1 生活日常&自主规划日程**:scheduler 注册日程任务(起床/睡觉/日常),触发经 `maisaka.proactive.trigger` 主动发言;读取天气/好感度数据联动;主题由 LLM 基于人设与时间自主决定(无概率抽取);
- **二期 2.3 主动私聊**:好感度达"挚友"级用户,由 2.1 日程窗口触发私聊问候;
- **三期 2.2 QQ空间**:`@MessageGateway(route_type="duplex")` 注册虚拟平台(空间动态→虚拟群聊信息流);数据源自研 qzone 接口模块(蓝本原版 Maizone:napcat `get_cookies` 凭证 + 网页接口,发说说优先 napcat 扩展 action);图片进信息流后交主程序 image_manager 处理,细节开工前三期设计补充。

## 6. 配置模型(一期)

`PluginConfigBase+Field` 嵌套 section,WebUI 生成 schema,热重载支持:

- `plugin`:enabled(总开关)、config_version
- `inject`:enabled(注入管线无截断,长度在源头控制)
- `time_aware`:enabled、city(默认"北京")、weather_refresh_minutes(默认 45)、holiday_online(默认开)
- `favorability`:enabled、window_hours(默认 24)、early_settle_threshold(默认 20)、daily_max_judgments(默认 3)、level_rules(5 级准则文本)、note_max_chars(默认 40,结算落库时强制)、material_max_messages(默认 30,素材条数上限)、material_message_max_chars(默认 200,单条素材截断长度)、llm(`{model}`)
- `memo`:enabled、tool_enabled、command_enabled、default_ttl_hours(默认 24,单条缺省 TTL)、max_ttl_hours(默认 168,单条 TTL 上限)、entry_max_chars(默认 80,写入时强制)、inject_max(默认 5,合计条数)
- `msg_react`:enabled、emoji_whitelist、per_stream_cooldown_seconds、llm
- `poke`:enabled、enhance_notice_text、inject_to_context、poke_tool_enabled、min_level_for_poke(默认"熟悉")、cooldown_seconds
- `reply_guard`:enabled、context_backfill_enabled、context_tools(可配工具名列表)、sentinel_enabled(默认 false)、sentinel_llm
- `image_relook`:enabled、vlm_model、llm

所有 LLM 节结构统一:`{model: str(留空=默认)}`,支持选择主程序已配置模型或 `catsitate_custom` 端点。

## 7. 错误处理原则

- 后台任务异常:捕获、记日志、不终止 tick 循环;连续失败在日志中可见;
- LLM 判定失败:跳过本轮,记录日志,不影响主链路;
- 在线数据源(天气/节日)失败:按回退链降级,最终静默跳过注入片段(日志记录),不阻塞;
- 配置错误:on_load 校验报错,主程序拒绝加载(暴露问题,不用 fallback 掩盖);
- 不实现任何"偷偷兜底":所有跳过与降级都有日志痕迹。

## 8. 测试方式

**单元测试(插件仓库内 `tests/`,pytest,不依赖 MaiBot 运行)**:
- 材料构造器(私聊/群聊切片、按条数取最近 N 条、单条截断、时间正序)、窗口触发逻辑(计数/日终/上限);
- 旁路 prompt 组装辅助(稳定段前置、模板版本化、素材正序);
- 节日数据解析与回退链、天气码映射;
- reply 规则校验器(通知/纯表情/参数缺失);
- sqlite 层与 JSON 快照读写;注入框架的片段缓存与截断优先级;备忘单条 TTL 参数校验与过期清理。

**集成测试(真实环境)**:
1. 插件目录放入 `MaiBot-dev/plugins/`(或插件市场安装),启动 docker compose 的 core 服务;
2. WebUI 插件页启用 → 日志确认 on_load、组件注册成功;
3. 逐项手动验证:命令(`/记一下`)、工具(planner 调 msg_react/poke_user/memo/inspect_image)、通知(戳一戳解析增强)、注入(日志中 `[环境]`/`[好感度]`/`[备忘]` 片段出现且位于请求尾部);
4. 缓存验证:开启 debug 缓存统计,对比插件启用前后命中率,附基线报告;
5. 热重载:修改配置后 WebUI 确认 on_config_update 生效。

## 9. 一期交付物清单

1. `plugin.py` + `_manifest.json`(能力声明、llm_providers、python_package 依赖 holiday-calendar)+ `README.md` + `CHANGELOG.md` + `.gitignore`(含 `/config.toml`);
2. 上文全部一期模块实现(4.1–4.10);
3. `tests/` 单元测试;
4. 缓存命中率基线对比报告;
5. 集成测试步骤文档(README 内)。

## 10. 风险与后续

- 插件加载器是否支持子包(3.1 已列 spike 验证与降级方案);
- `holiday-calendar` 自动安装依赖 Host 侧依赖流水线,安装失败时节日退化为内置表(日志可见);
- 三期 qzone 接口有风控/接口变更风险(方案已选,限流与失败暴露原则覆盖);
- 主程序缓存瓶颈(若有)单独形成报告,经用户许可才动主程序。
