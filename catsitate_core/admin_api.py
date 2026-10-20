"""管理页 @API 组件:AdminService 的薄封装,统一 ``{ok, error}`` 信封。

页面只通过 webui.json 声明的短名调用这些 API;业务校验在 AdminService 内完成,
本层只负责把意外异常转成显式错误并记录日志(不静默、不返回半截数据)。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from maibot_sdk import API

from .admin import AdminService

logger = logging.getLogger("catsitate.core.admin")


class AdminAPIMixin:
    """为插件类提供管理页 API(继承自 MaiBotPlugin 的组件收集会扫描 dir(instance),mixin 有效)。"""

    def _admin_service(self) -> AdminService:
        return AdminService(self.store, self.config.memo, self.config.favorability, logger=logger)

    def _admin_run(self, fn: Callable[..., dict[str, Any]], /, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[管理页] API 执行异常")
            return {"ok": False, "error": f"内部错误({type(exc).__name__}),详情见插件日志"}

    @API("admin_fav_list", description="好感度列表(分页,可按 QQ/注记搜索)", version="1")
    async def admin_fav_list(self, page: int = 1, page_size: int = 20, keyword: str = "") -> dict[str, Any]:
        return self._admin_run(self._admin_service().list_favorability, page, page_size, keyword)

    @API("admin_fav_update", description="修改好感度分数/注记", version="1")
    async def admin_fav_update(
        self, user_id: str, score: int | None = None, note: str | None = None
    ) -> dict[str, Any]:
        return self._admin_run(self._admin_service().update_favorability, user_id, score, note)

    @API("admin_fav_reset", description="重置好感度(分数/等级/注记归零并清理批次)", version="1")
    async def admin_fav_reset(self, user_id: str) -> dict[str, Any]:
        return self._admin_run(self._admin_service().reset_favorability, user_id)

    @API("admin_memo_list", description="备忘录列表(分页,可按 QQ/内容搜索)", version="1")
    async def admin_memo_list(self, page: int = 1, page_size: int = 20, keyword: str = "") -> dict[str, Any]:
        return self._admin_run(self._admin_service().list_memo, page, page_size, keyword)

    @API("admin_memo_create", description="新增备忘", version="1")
    async def admin_memo_create(
        self, user_id: str, content: str, ttl_hours: float = 0,
        remind_at: str = "", extra_user_ids: str = "",
    ) -> dict[str, Any]:
        return self._admin_run(
            self._admin_service().create_memo, user_id, content, ttl_hours, remind_at, extra_user_ids
        )

    @API("admin_memo_update", description="修改备忘内容/提醒时间/有效期", version="1")
    async def admin_memo_update(
        self, memo_id: int, content: str | None = None,
        ttl_hours: float = 0, remind_at: str | None = None,
    ) -> dict[str, Any]:
        return self._admin_run(self._admin_service().update_memo, memo_id, content, ttl_hours, remind_at)

    @API("admin_memo_delete", description="删除单条备忘", version="1")
    async def admin_memo_delete(self, memo_id: int) -> dict[str, Any]:
        return self._admin_run(self._admin_service().delete_memo, memo_id)

    @API("admin_memo_batch_delete", description="批量删除备忘(逗号分隔 ID)", version="1")
    async def admin_memo_batch_delete(self, ids: str) -> dict[str, Any]:
        return self._admin_run(self._admin_service().batch_delete_memos, ids)

    @API("admin_log_list", description="好感度判定日志(只读,分页)", version="1")
    async def admin_log_list(
        self, page: int = 1, page_size: int = 20,
        user_id: str = "", date_from: str = "", date_to: str = "",
    ) -> dict[str, Any]:
        return self._admin_run(
            self._admin_service().list_favorability_log, page, page_size, user_id, date_from, date_to
        )

    @API("admin_event_list", description="QQ空间好感事件(只读,分页)", version="1")
    async def admin_event_list(
        self, page: int = 1, page_size: int = 20,
        user_id: str = "", date_from: str = "", date_to: str = "",
    ) -> dict[str, Any]:
        return self._admin_run(
            self._admin_service().list_qzone_events, page, page_size, user_id, date_from, date_to
        )
