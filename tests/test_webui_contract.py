"""WebUI 声明契约测试:webui.json ↔ @API 绑定 ↔ 页面文件三方一致。

防止改 API 名/参数或页面别名后,宿主在加载期才报「绑定不存在」——把错误左移到测试。
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

from catsitate_core.admin_api import AdminAPIMixin

ROOT = Path(__file__).resolve().parents[1]
_ALLOWED_ICONS = {"puzzle", "chart", "settings", "database", "list"}


def _declared_apis() -> dict[str, object]:
    apis: dict[str, object] = {}
    for attr_name in dir(AdminAPIMixin):
        attr = getattr(AdminAPIMixin, attr_name)
        info = getattr(attr, "__maibot_component_info__", None)
        version = getattr(info, "version", None)
        if info is not None and version:
            apis[f"{info.name}@{version}"] = attr
    return apis


def test_webui_json_declaration_and_page_file():
    data = json.loads((ROOT / "webui.json").read_text(encoding="utf-8"))
    assert data["schema_version"] == 2
    assert "custom_pages" in data["required_capabilities"]
    assert data["workspace_title"]
    pages = data["pages"]
    assert len(pages) == 1
    page = pages[0]
    assert page["mode"] == "custom" and page["placement"] == "workspace"
    assert page["icon"] in _ALLOWED_ICONS
    assert page["layout"] in {"document", "viewport"}
    entry = ROOT / page["entry"]
    assert entry.is_file(), f"页面文件不存在: {entry}"
    assert entry.stat().st_size <= 2 * 1024 * 1024
    assert len(page["queries"]) <= 10 and len(page["actions"]) <= 20
    for binding in list(page["queries"].values()) + list(page["actions"].values()):
        assert len(binding.get("parameters", {})) <= 30


def test_page_aliases_are_declared():
    data = json.loads((ROOT / "webui.json").read_text(encoding="utf-8"))
    page = data["pages"][0]
    html = (ROOT / page["entry"]).read_text(encoding="utf-8")
    used_queries = set(re.findall(r'query\("([a-zA-Z0-9_]+)"', html))
    used_actions = set(re.findall(r'action\("([a-zA-Z0-9_]+)"', html))
    assert used_queries, "页面未调用任何查询"
    assert used_actions, "页面未调用任何操作"
    assert used_queries <= set(page["queries"]), f"页面调用了未声明的查询: {used_queries - set(page['queries'])}"
    assert used_actions <= set(page["actions"]), f"页面调用了未声明的操作: {used_actions - set(page['actions'])}"


def test_bound_apis_exist_with_matching_parameters():
    data = json.loads((ROOT / "webui.json").read_text(encoding="utf-8"))
    page = data["pages"][0]
    apis = _declared_apis()
    for kind in ("queries", "actions"):
        for alias, binding in page[kind].items():
            key = f"{binding['api']}@{binding.get('version', '1')}"
            assert key in apis, f"{kind}.{alias} 绑定的 API 不存在: {key}"
            method = apis[key]
            signature = set(inspect.signature(method).parameters) - {"self"}
            declared = set(binding.get("parameters", {}))
            assert declared <= signature, (
                f"{kind}.{alias} 声明了方法签名中不存在的参数: {declared - signature}"
            )
            for name, parameter in binding.get("parameters", {}).items():
                if parameter.get("required"):
                    assert name in signature


def test_plugin_collects_admin_api_components():
    from maibot_sdk.components import collect_components

    from plugin import CatsitatePlugin

    instance = object.__new__(CatsitatePlugin)
    names = {item["name"] for item in collect_components(instance)}
    assert {"admin_fav_list", "admin_fav_update", "admin_fav_reset"} <= names
    assert {"admin_memo_list", "admin_memo_create", "admin_memo_update"} <= names
    assert {"admin_memo_delete", "admin_memo_batch_delete"} <= names
    assert {"admin_log_list", "admin_event_list"} <= names
