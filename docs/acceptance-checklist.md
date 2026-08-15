# 手动验收清单(规格 §5)

前置:插件已放入 plugins/ 并启用;WebUI 插件页打开 `plugin.enabled = true`;重启 MaiBot 后日志出现 `catsitate_core 已加载`。

- [x] 命令:`/记一下 周四交作业` → bot 回复已记下;`/记一下` 超 80 字符 → 提示精简
- [x] 工具(planner 自主):「帮我记一下…」→ memo_write;「戳一下他」→ poke_user;「看看刚才那张图里写了什么」→ inspect_image;均已实机确认
- [ ] 贴表情(仅群聊):群聊中「给上一条消息贴个表情」→ msg_react;私聊调用 → 返回「贴表情仅限群聊」
- [x] 注入:日志或调试输出中 `[等级规则]`/`[环境]`/`[备忘]`/`[好感度]` 片段出现,且位于 system 之后、历史之前
- [x] 好感度:同一用户连续发言至 early_settle_threshold,日志出现 `好感度结算[early]`,delta/note 已落库;daily 结算需连续两天观察
- [ ] 好感度材料中 bot 发言识别:`favorability.bot_user_id` 填入 napcat 账号(3545773341)后,结算素材中 bot 发言标注为 bot 随附
- [x] 主动戳(戳一戳):「戳一下他」→ poke_user 已可用;入站戳一戳解析已按联调结论删除
- [ ] 贴表情防刷:同流 30 秒内二次调用 → 冷却提示
- [ ] 哨兵层(默认关):开启后日志出现哨兵判定调用
- [x] 旁路记账:`data/plugins/catsitate.core/catsitate.db` 中 `llm_usage` 表按模块分列调用数(实测 favorability/msg_react/image_relook 分列)
- [x] 热重载:WebUI 修改配置后 `on_config_update` 生效(`配置已刷新,派生缓存已重置` 日志)
