# 测试体系

> 对应代码:`tests/`(34 个文件,602 个用例,全量离线运行约 6 秒)。

## 测试哲学

### 1. 全离线

- **不触网**:没有任何测试发起真实 HTTP——QQ 空间接口、天气/节日源全部经注入的 stub `fetch` / 客户端桩应答。
- **不依赖主程序**:不 import MaiBot,不需要启动宿主——`conftest.py` 只做一件事(把插件根目录插入 `sys.path`,保证任意 cwd 都能导入 `catsitate_core`);plugin 装配通过手工补齐最小属性完成(不跑 `on_load`)。

### 2. stub 为主(依赖注入的回报)

插件代码的 IO 面(HTTP fetch、adapter api_call、网关 route_message、旁路 LLM、ctx 能力调用)全部是**经参数/属性注入的 callable**,测试用桩替换即可驱动任意分支。分层各司其职:

- **纯函数层**(协议解析/表单构造/消息构造/正则护栏):真函数直测,给定输入断言输出。
- **状态机层**(注入泵/registry/seen_store/调度器):真实现直测,IO 不存在所以无需桩。
- **组合层**(plugin 接线):离线装配插件实例(`_make_plugin`),客户端/LLM/网关全桩,断言「接线行为」——工具目标解析、频控回执、AuthError 自愈、通知轮询守卫等。**以行为断言为主,少量防回归断言直接检查源码字符串**。

### 3. 契约锁定(回执文案逐字断言)

面向模型/用户的文本是**行为契约的一部分**——模型按回执文案理解成败原因、按消息尾部锚格式照抄参数。因此测试对回执文案、锚格式、场景文案做逐字断言,例如:

```python
assert text.startswith("(今天") and text.endswith("今天天气好\n〔说说ID=t1〕")
```

模板一致性同款锁定:场景文案/旁路模板的「内置默认、`prompt_templates/` 部署文件、运行时兜底常量」三方逐字一致由测试守护(防漂移);日期无关性(生成文案不随运行日期变化)也有专门守护。

## 运行方法

```bash
cd <本仓库根> && python3 -m pytest tests/ -q
# 602 passed in ~6s(无需网络、无需主程序、无需 QQ 登录态)
```

跑单个模块:

```bash
python3 -m pytest tests/test_guard.py -q          # 单文件
python3 -m pytest tests/test_qzone_wiring.py -k auth_retry -q   # 按关键字筛选用例
```

依赖:pytest + PIL(多图合成链需要真实可解码图字节)。无其它外部依赖。

## 测试文件组织(34 个文件按模块分组)

| 分组 | 文件 | 覆盖 |
|---|---|---|
| 基础设施 | `conftest.py` / `test_scheduler.py` / `test_storage.py` / `test_config.py` | sys.path 注入 / 60s 调度器 / SQLite+JSON 快照 / 配置模型与默认值 |
| 模板与部署 | `test_prompt_deploy.py` / `test_llm_provider.py` | on_load 模板自动部署(首次/幂等/覆盖/缺目录)/ 旁路 prompt 组装与三层链、缓存键、模板版本 |
| 注入与回复链 | `test_inject.py` / `test_reply_guard.py` / `test_image_relook.py` | 注入块装配 / reply 补传与哨兵 / VLM 重看工具 |
| 引擎层 | `test_favorability.py` / `test_settlement.py` / `test_decay.py` / `test_memo.py` / `test_msg_react.py` / `test_poke.py` / `test_sleep.py` / `test_schedule.py` / `test_time_aware.py` | 好感度/结算/衰减/备忘/贴表情/戳一戳/睡眠判定/日程生成与窗口/节日天气 |
| QQ空间·纯函数层 | `test_qzone_client.py` / `test_qzone_discovery.py` / `test_qzone_wire.py` / `test_qzone_gateway.py` | HTTP 客户端(cookie/g_tk/读写通道/错误分类)/ 统一时间线与赞事件解析 / 写路径表单与评论解析 / 注入消息构造(时间前缀/图片段/占位/评论区) |
| QQ空间·状态层 | `test_qzone_seen.py` / `test_qzone_comment_seen.py` / `test_qzone_like_seen.py` / `test_qzone_injector.py` / `test_qzone_registry.py` / `test_qzone_scene.py` | 三张去重表 / 串行注入泵(P1/P2/wait/超时)/ FeedContext 注册表(合并语义/前缀解析)/ 场景替换与白名单 |
| QQ空间·表达与图片 | `test_qzone_expression.py` / `test_qzone_imaging.py` | 润色层(失败回退/超长重润/卫生)/ 图片管线(拼图角标/压缩预算/丢弃占位/锚 hash) |
| QQ空间·组合层 | `test_qzone_wiring.py` / `test_guard.py` / `test_integration.py` | plugin 接线全量行为(最大的一个文件,浏览轮询/六工具/通知三源/日记见闻/回退路径)/ 护栏三拦截点/ 全引擎离线冒烟 |

## 关键 stub(组合层 `_make_plugin` 装配)

| Stub | 职责 |
|---|---|
| `_StubCtx` | 最小 ctx 面:`_CollectLogger`(收集日志供断言)+ `_StubGateway` + `_StubConfig`(异步 config.get 桩,bot.nickname 必答) |
| `_StubGateway` | 记录 `route_message` 注入调用(返回 True),注入消息形态断言的数据源 |
| `_StubWriteClient` | 写路径桩:记录 do_comment/do_reply/do_like/do_publish 全部调用参数,恒成功;`publish_tid` 可置空串模拟「响应缺 tid」形态 |
| `_StubUnifiedClient` | 发现层桩:`get_unified_timeline` 首页返回固定列表、第 2 页起返回空(模拟稳态「更早页无积压」);子类按需覆盖 `get_user_feeds`/`download_image`/`get_like_events` 驱动不同分支 |
| `_StubCookie` | cookie 管理桩:记录 `invalidate` 调用次数;`get_result` 可配置(默认 None=重取失败,驱动同轮自愈链断言) |
| `_fake_side_llm` | 旁路 LLM 桩:识别【待发内容】素材段并回显草稿(等价「润色后等于草稿」),记录调用供断言 prompt 结构(人设前置/场景语/模块名) |
| `fake_fetch` / `fake_api_call`(test_qzone_client.py) | HTTP 层桩:按 URL/方法返回固定响应(bytes),断言请求参数集/请求头/g_tk/cookie 注入——协议契约逐字段锁定 |
| `_patch_sleep` | 把 `asyncio.sleep` 换成记录桩——源B/充实层好友间隔 2 秒的防风控纪律可断言,测试不真等 |
| `_png_bytes` | 真实可解码的纯色 PNG(合成链需要合法图字节,多图互异防误判) |

**桩的纪律**:桩记录调用参数而非只回放结果——测试据此断言「插件向远端发了什么」(写路径参数、@ 前缀格式、全量 tid 回填、好友间隔),这是组合层测试的核心价值。
