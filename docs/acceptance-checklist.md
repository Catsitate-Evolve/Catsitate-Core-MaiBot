# 手动验收清单(规格 §5)

前置:插件已放入 plugins/ 并启用;WebUI 插件页打开 `plugin.enabled = true`;重启 MaiBot 后日志出现 `catsitate_core 已加载`。

- [x] 命令:`/记一下 周四交作业` → bot 回复已记下;`/记一下` 超 80 字符 → 提示精简
- [x] 工具(planner 自主):「帮我记一下…」→ memo_write;「戳一下他」→ poke_user;「看看刚才那张图里写了什么」→ inspect_image;均已实机确认
- [x] 贴表情(仅群聊):群聊中「给上一条消息贴个表情」→ msg_react;私聊调用 → 返回「贴表情仅限群聊」
- [x] 注入:日志或调试输出中 `[等级规则]`/`[环境]`/`[备忘]`/`[好感度]` 片段出现,且位于 system 之后、历史之前
- [x] 好感度:early 结算与 daily 结算均实机验证(delta/note 落库,顺延分支亦验证);跨天常规运行待观察
- [x] 好感度材料中 bot 发言识别:实机验证结算取数 86 条中 37 条 bot 发言正确标注(bot_user_id=3545773341)
- [x] 主动戳(戳一戳):「戳一下他」→ poke_user 已可用;入站戳一戳解析已按联调结论删除
- [~] 贴表情防刷:同流 30 秒冷却(低优先级,联调决定不单独测试,单测覆盖)
- [x] 哨兵层:实机验证「哨兵判定:放行回复」+ llm_usage sentinel 记账;键名/人设读取已按主程序 payload 修复
- [x] 旁路记账:`data/plugins/catsitate.core/catsitate.db` 中 `llm_usage` 表按模块分列调用数(实测 favorability/msg_react/image_relook 分列)
- [x] 热重载:WebUI 修改配置后 `on_config_update` 生效(`配置已刷新,派生缓存已重置` 日志)
