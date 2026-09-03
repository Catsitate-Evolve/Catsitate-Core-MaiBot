# 内容护栏(可配置正则拦截)

> 对应代码:`catsitate_core/guard.py`(纯匹配器),`plugin.py` 的 `GuardSection` 配置(`catsitate_core/config.py`)、`_assemble_guard` / `_guard_compiled`、三个空间动作工具与日记里的拦截点、`content_guard_replyer` 钩子。

## 一、职责与生命周期

内容护栏是**内容级的底线拦截**:把「绝不允许 bot 说出/发出」的文本模式(如涉政敏感词、隐私信息、定向违规内容)配置为正则列表,在文本**真正发出之前**拦截——取消发布或撤下回复。它是底线不是导航:只拦已知违规模式,不引导模型「应该说什么」;是否合规地表达仍由模型与润色层负责。

关键设计取舍:

- **全局单列表**:不区分模块/场景——同一份 `guard.patterns` 对三个空间动作工具、日记、全部聊天的 replyer 回复生效(内容级护栏,不分流)。
- **默认关闭**:`guard.enabled` 默认 `false`——未启用时零编译、零匹配、零行为变化。
- **纯函数无 IO**:`guard.py` 只做编译与命中判定,拦截决策(取消什么、回执怎么写)在消费方。

生命周期:`on_load` 时 `_assemble_guard()` 按 `guard.enabled` 编译正则到实例级 `_guard_compiled`;配置热更新(`on_config_update`,scope=self)时重新执行 `_assemble_guard()`——enabled/patterns 变更即重编译,立即生效。

## 二、完整逻辑

### 2.1 配置

```toml
[guard]
enabled = true                 # 总开关(默认 false)
patterns = ["正则1", "正则2"]   # 拦截正则列表(全局单列表)
```

### 2.2 纯匹配器(guard.py)

- `compile_guard(patterns) -> (编译后 Pattern 列表, 错误串)`:逐条 `re.compile`;**任一条失败返回 `([], 错误串)` 整组拒绝加载**——错误串含首个坏规则的 1 基序号/原文/异常类型。不做「跳过坏的那条、其余可用」的部分兜底(部分护栏=假安全感)。
- `match_guard(compiled, text) -> int`:按序 `re.search`,返回**首个命中规则的 1 基编号**,无命中返回 0。`re.search` 语义=部分命中即中;**无 flags,大小写敏感**(要拦 `Word` 与 `word` 需显式写 `(?i)` 或两条)。
- 未启用时 `_guard_compiled` 为空列表,`match_guard` 恒 0——所有拦截点自然短路,零行为变化。

### 2.3 三个拦截点

拦截点选取原则:**在文本最终形态上、且在真实副作用发生之前**匹配。

| 拦截点 | 时机 | 动作 |
|---|---|---|
| **三个空间动作工具**(`qzone_comment` / `qzone_reply` / `qzone_post`) | 表达润色**之后**(润色改写可能引入命中;草稿直发形态同样覆盖)、写 API 调用**之前** | 取消发布:零 API 调用零记账零 seen,回执 `内容被拦截(命中规则N),未发布。` |
| **日记**(`_generate_and_publish_diary`) | LLM 生成文本落定后、发布 API **之前** | 取消发布:不发布不落快照,告警即止(入睡任务无用户回执) |
| **replyer 回复**(`content_guard_replyer` 钩子) | `maisaka.replyer.after_response`,HookMode.BLOCKING + HookOrder.EARLY(先于哨兵等 LATE 钩子) | `response` 改写为空串——主程序 reply 工具拿空文本走失败结果,planner 看到 [失败] 即真沉默;`output_items` 原样保留(主程序自行处理正文投影,手工改 items 会形态错误)。全部会话生效 |

replyer 侧的重复拦截天然成立:模型若自主重调 reply,每次生成的新文本都会再过本钩子,不漏发。

### 2.4 热重载

`on_config_update(scope="self")` 末尾调用 `_assemble_guard()`——WebUI 修改 enabled/patterns 后无需重启,下一条消息/动作即按新规则集拦截。重编译同样遵守「编译失败整组置空+告警」纪律。

### 2.5 可观测性

每次命中都打 warning 日志:`内容护栏拦截:{拦截点} 命中规则{N},{未发布/置空未发送}(文本:{前60字}...)`——拦截动作可见、可审计,与「错误显式暴露」原则一致。

## 三、限制与回退清单

| 场景 | 行为 |
|---|---|
| `guard.enabled=false` | `_guard_compiled` 空列表,匹配恒 0,三拦截点原样放行(零行为变化) |
| 任一正则非法 | **整组护栏拒绝加载**:warning(含坏规则序号/原文/原因)后 `_guard_compiled` 置空——护栏失效但**不阻断插件加载**(修正配置即恢复);不做部分可用兜底 |
| 工具拦截命中 | 取消发布+回执明示规则编号(模型知道哪里出了问题) |
| 日记拦截命中 | 取消发布,告警(无回执通道) |
| replyer 拦截命中 | response 置空(真沉默),不改 output_items;模型重调 reply 时逐次再拦 |
| 配置热更新 | 立即重编译生效(enabled/patterns 任一变更) |

**已知边界**:`re.search` 无 flags(大小写敏感);命中即整条取消,不支持「替换/改写命中片段」的软化处置;回执只报规则编号,不回显命中片段细节(前 60 字仅进日志)。
