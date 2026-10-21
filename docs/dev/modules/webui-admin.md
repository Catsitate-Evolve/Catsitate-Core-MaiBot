# WebUI 数据管理页

> 声明:`webui.json`(仓库根);页面:`webui/admin.html`;服务:`catsitate_core/admin.py`;API:`catsitate_core/admin_api.py`。

浏览器里管理插件用户数据的自定义页面(WebUI 顶部工作区「Catsitate」→「数据管理」)。页面运行在宿主隔离 iframe 中,只通过 `window.maibot.query/action` 调本插件在 `webui.json` 里声明过的静态 `@API`,不接触 SQL、不发起外部请求。

## 一、范围

| 表 | 权限 | 说明 |
|---|---|---|
| `favorability` | 读写 | 列表/搜索/分页;改分数(0~100)与注记;重置 |
| `memo` | 读写 | 列表/搜索/分页;新增/编辑/单条删除/勾选批量删除 |
| `favorability_log` | 只读 | 按 QQ/日期过滤、分页 |
| `qzone_fav_events` | 只读 | 按 QQ/日期过滤、分页 |
| `batch_counter` | 仅重置时清理 | 不提供单独管理 |

明确不开放:`qzone_feeds/comments/likes`(去重与注入状态)、`llm_usage`、`weather_snapshot`、`_schema_version`、`qzone_cookies.json`。

## 二、不变量(服务层强制,页面不得绕过)

- **等级派生**:`level` 由 `score` 经 `level_for_score()` 计算,不接受手改;改分数即重算等级。
- **「特别」全表独占**:把某人改到 100 分时,若他人已持有「特别」,显式拒绝并提示先重置持有者。
- **重置语义**:`score=0`、`level=0`、`note=""`、`window_start=now`、`judged_at=now`,并删除该用户 `batch_counter` 行(防积压批次立刻覆盖);`favorability_log` 保留。
- **备忘**:新增必填 `user_id`+`content`;编辑只改内容/提醒/有效期,不碰 `created_at`;有效期按“从现在起”重算;提醒时刻不得晚于到期时间;提醒时间做日历存在性校验。
- **分页参数**:页码/每页条数非法时显式报错,不静默归整;每页上限 `MAX_PAGE_SIZE=50`(与 `webui.json` 绑定一致,由契约测试锁定)。
- **审计**:管理操作写 `catsitate_core.admin` logger(经模块转发进主日志),只记 user_id、字段名、影响行数,不记备忘正文。

## 三、API

| 短名 | 作用 | 关键参数 |
|---|---|---|
| `admin_fav_list` | 好感度列表 | page/page_size/keyword |
| `admin_fav_update` | 改分数/注记 | user_id, score?, note? |
| `admin_fav_reset` | 重置 | user_id |
| `admin_memo_list` | 备忘列表 | page/page_size/keyword |
| `admin_memo_create` | 新增 | user_id, content, ttl_hours?, remind_at?, extra_user_ids? |
| `admin_memo_update` | 编辑 | memo_id, content?, ttl_hours?, remind_at? |
| `admin_memo_delete` | 删除单条 | memo_id |
| `admin_memo_batch_delete` | 批量删除 | ids(逗号分隔) |
| `admin_log_list` | 判定日志(只读) | page/page_size/user_id?/date_from?/date_to? |
| `admin_event_list` | 空间事件(只读) | 同上 |

所有响应统一 `{ok: true, ...}` / `{ok: false, error: "中文原因"}`;业务校验失败不抛异常。`@API` 通过 `AdminAPIMixin` 继承进插件类,`collect_components` 经 `dir(instance)` 可收集。

## 四、页面

`webui/admin.html` 单文件(内联 CSS/JS,无外部依赖),`layout: "viewport"`(弹窗需覆盖整个可视区;`document` 会随内容收缩导致弹窗被裁切)。四个 tab;行内操作 + 弹窗表单 + 页内自定义确认框(不依赖浏览器 `confirm`);数据一律 `textContent` 渲染,无 `innerHTML` 注入路径。

## 五、测试

- `tests/test_admin.py`:CRUD、特别独占、重置清批次、分页/过滤、负 TTL、提醒晚于到期、附带 QQ 截断提示、源码级“只触碰允许表”守卫、异常信封。
- `tests/test_webui_contract.py`:webui.json schema/capabilities/entry 尺寸、页面别名 ⊂ 声明、绑定 API 存在且参数名匹配、page_size 上限与 `MAX_PAGE_SIZE` 一致、mixin 组件可被收集。
