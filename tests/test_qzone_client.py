"""qzone 协议层测试:g_tk/callback 截取/说说解析/好友列表解析(纯函数,无网络)。

联调修正(2026-08-30):emotion_cgi_msglist_v6 实为「指定用户说说列表」(uin=目标,
响应顶层 msglist,条目含 tid/created_time/content/pic[].url1/commentlist);
好友列表走 adapter OneBot API(vFeeds 形态不存在,Maizone 调查摘要有误)。
"""
from catsitate_core.qzone.protocol import (
    FeedItem, extract_callback_json, generate_gtk, parse_friend_list, parse_msglist,
)

# 响应样本结构对照 Maizone qzone_api.py get_list 的解析路径(实测 2026-08-30)
MSGLIST_JSONP = (
    '_preloadCallback({"code":0,"subcode":0,"message":"",'
    '"logininfo":{"name":"Catsitate-dev","uin":3545773341},'
    '"msglist":['
    '{"tid":"tid_a","appid":311,"created_time":1750000000,"content":"今天天气很好",'
    '"pic":[{"url1":"https://img.example/a.jpg"},{"url1":"https://img.example/b.jpg"}],'
    '"rt_con":{"rt_content":""},'
    '"commentlist":[{"name":"小明","content":"好看","uin":10001,"tid":9001,"createTime":1750000100}]},'
    '{"tid":"tid_b","created_time":1750000500,"content":"","pic":[]}'
    '],"usrinfo":{"name":"好友甲","uin":8888}});'
)


def test_generate_gtk_hash33():
    # hash31/hash33 经典算法:手工演算一个短串
    s, h = "abc", 5381
    for c in s:
        h += (h << 5) + ord(c)
    assert generate_gtk("abc") == (2147483647 & h)


def test_extract_callback_json_tolerates_wrapper():
    payload = extract_callback_json(MSGLIST_JSONP)  # _preloadCallback 包裹
    assert payload["code"] == 0
    assert len(payload["msglist"]) == 2


def test_parse_msglist_maps_fields():
    payload = extract_callback_json(MSGLIST_JSONP)
    items = parse_msglist(payload, target_uin="8888", nickname="好友甲")
    assert [i.tid for i in items] == ["tid_a", "tid_b"]
    a = items[0]
    assert (a.uin, a.nickname, a.content, a.appid) == ("8888", "好友甲", "今天天气很好", 311)
    assert a.image_urls == ["https://img.example/a.jpg", "https://img.example/b.jpg"]
    assert a.abstime == "1750000000"
    b = items[1]
    assert b.content == "" and b.image_urls == []  # 纯图/空文的条目保留(占位注入)


def test_parse_msglist_null_or_missing():
    assert parse_msglist({"code": 0, "msglist": None}, target_uin="1", nickname="n") == []
    assert parse_msglist({"code": 0}, target_uin="1", nickname="n") == []


def test_parse_msglist_forward_and_video_fallback( ):
    """特殊动态(联调缺陷#4 实证形态):转发→rt_con.content+rt_uinname;视频→占位。"""
    payload = {"code": 0, "msglist": [
        # 转发说说(content 空,rt_con 携带原文,rt_uinname=原作者)
        {"tid": "f1", "created_time": 100, "content": "",
         "rt_con": {"conlist": [{"con": "想和所有人暂停在关系最好最舒服的那一刻", "type": 2}],
                    "content": "想和所有人暂停在关系最好最舒服的那一刻"},
         "rt_uinname": "负能墙。", "rt_uin": 281401888},
        # 纯图说说(content 空,pic 非空——文本留空,由消息构造层省略文本段)
        {"tid": "f2", "created_time": 200, "content": "", "pic": [{"url1": "https://img/x.jpg"}]},
    ]}
    items = parse_msglist(payload, target_uin="9", nickname="n")
    assert items[0].content == "[转发自负能墙。]想和所有人暂停在关系最好最舒服的那一刻"
    assert items[1].content == ""  # 纯图:文本保持空,图段承载内容
    payload2 = {"code": 0, "msglist": [{"tid": "v1", "created_time": 1, "content": "", "video": [{"url": "u"}]}]}
    items2 = parse_msglist(payload2, target_uin="9", nickname="n")
    assert items2[0].content == "[视频]"


def test_parse_friend_list_shapes():
    # OneBot get_friend_list 形态:裸 list / {"data": [...]}(信封容忍)
    raw = [{"user_id": 10001, "nickname": "小明", "remark": "明仔"}, {"user_id": 10002, "nickname": "小红", "remark": ""}]
    assert parse_friend_list(raw) == [
        {"user_id": "10001", "nickname": "明仔"},  # remark 优先(好友备注=用户对TA的称呼)
        {"user_id": "10002", "nickname": "小红"},
    ]
    assert parse_friend_list({"data": raw}) == parse_friend_list(raw)
    assert parse_friend_list({"success": False}) == []
    assert parse_friend_list("bad") == []



# ---- CookieManager / QzoneClient(fake 注入,无网络) ----
import asyncio
import json as _json
from catsitate_core.qzone.client import CookieManager, QzoneClient
from catsitate_core.storage import JsonSnapshot


def _cookie_snapshot(tmp_path):
    return JsonSnapshot(tmp_path / "qzone_cookies.json")


def test_cookie_manager_persists_and_throttles(tmp_path):
    calls = []

    async def fake_api_call(method, **params):
        calls.append((method, params))
        return {"cookies": {"p_skey": "SK1", "uin": "o123"}}

    cm = CookieManager(_cookie_snapshot(tmp_path), api_call=fake_api_call, refresh_minutes=60)
    ck = asyncio.run(cm.get())
    assert ck == {"p_skey": "SK1", "uin": "o123"}
    ck2 = asyncio.run(cm.get())
    assert ck2 == ck and len(calls) == 1  # 节流:60 分钟内不重取
    # 持久化可跨实例复用
    cm2 = CookieManager(_cookie_snapshot(tmp_path), api_call=fake_api_call, refresh_minutes=60)
    assert asyncio.run(cm2.get()) == ck and len(calls) == 1


def test_cookie_manager_napcat_envelope_and_params(tmp_path):
    """联调缺陷修正(2026-08-30):adapter API 形态为 params 单关键字;NapCat 返回 cookies 字符串。"""

    calls = []

    async def fake_api_call(method, **kw):
        calls.append(kw)
        return {"status": "ok", "retcode": 0, "data": {"cookies": "p_skey=SK2; uin=oq1; foo=bar"}}

    cm = CookieManager(_cookie_snapshot(tmp_path), api_call=fake_api_call, refresh_minutes=60)
    ck = asyncio.run(cm.get())
    assert ck == {"p_skey": "SK2", "uin": "oq1", "foo": "bar"}
    assert calls == [{"params": {"domain": "user.qzone.qq.com"}}]  # 单 params 关键字


def test_cookie_manager_napcat_data_string(tmp_path):
    """NapCat data 直接为 cookie 字符串的形态。"""

    async def fake_api_call(method, **kw):
        return {"retcode": 0, "data": "p_skey=SK3; uin=oq2"}

    cm = CookieManager(_cookie_snapshot(tmp_path), api_call=fake_api_call, refresh_minutes=60)
    assert asyncio.run(cm.get()) == {"p_skey": "SK3", "uin": "oq2"}


def test_cookie_manager_invalidate_forces_refetch(tmp_path):
    """联调缺陷#7:失效标记后跳过快照与节流,强制重取;失败不回退旧值。"""
    calls = []

    async def fake_api_call(method, **kw):
        calls.append(1)
        if len(calls) == 1:
            return {"cookies": {"p_skey": "OLD", "uin": "o1"}}
        return {"cookies": {"p_skey": "NEW", "uin": "o1"}}

    cm = CookieManager(_cookie_snapshot(tmp_path), api_call=fake_api_call, refresh_minutes=60)
    assert asyncio.run(cm.get()) == {"p_skey": "OLD", "uin": "o1"}
    cm.invalidate()
    assert asyncio.run(cm.get()) == {"p_skey": "NEW", "uin": "o1"} and len(calls) == 2  # 立即重取(不受节流/快照)


def test_client_download_image_retries_once_on_transient_failure():
    """图片下载瞬态失败(联调实证 CDN 偶发 404)单次重试;两次都败才返回 None。"""
    attempts = []

    async def fake_fetch(method, url, *, params, headers, timeout_ms):
        attempts.append(url)
        return (404, "") if len(attempts) == 1 else (200, b"img".decode("latin-1"))

    async def fake_cookie():
        return {"p_skey": "SK"}

    client = QzoneClient(cookie_provider=fake_cookie, fetch=fake_fetch, timeout_ms=1000, max_retries=0)
    assert asyncio.run(client.download_image("https://img/x.jpg")) == b"img"
    assert len(attempts) == 2


def test_client_raises_auth_error_on_neg3000():
    """code=-3000(登录态失效)抛 QzoneAuthError,由调用方触发 cookie 失效重取。"""
    from catsitate_core.qzone.client import QzoneAuthError

    body = '_preloadCallback(' + _json.dumps({"code": -3000, "message": "登录态失效"}) + ');'
    client, _ = _make_client([(200, body)])
    try:
        asyncio.run(client.get_user_feeds(target_uin="1", nickname="n"))
        raised = False
    except QzoneAuthError:
        raised = True
    assert raised


def test_cookie_manager_missing_pskey_warns_none(tmp_path):
    async def fake_api_call(method, **params):
        return {"cookies": {"uin": "o123"}}  # 缺 p_skey

    cm = CookieManager(_cookie_snapshot(tmp_path), api_call=fake_api_call, refresh_minutes=0)
    assert asyncio.run(cm.get()) is None


def _make_client(fetch_responses, cookies=None):
    cookies = cookies if cookies is not None else {"p_skey": "SK", "uin": "o1"}
    seen_params = []

    async def fake_cookie():
        return cookies

    async def fake_fetch(method, url, *, params, headers, timeout_ms):
        seen_params.append({"url": url, "params": dict(params), "headers": dict(headers)})
        assert params.get("g_tk") == generate_gtk("SK")  # cgi 请求自动携带 g_tk
        return fetch_responses.pop(0)

    client = QzoneClient(cookie_provider=fake_cookie, fetch=fake_fetch, timeout_ms=1000, max_retries=0)
    return client, seen_params


def test_client_get_user_feeds_maizone_params_and_headers():
    body = '_preloadCallback(' + _json.dumps({
        "code": 0, "msglist": [
            {"tid": "t1", "appid": 311, "created_time": 1, "content": "hi", "pic": []}
        ]
    }) + ');'
    client, seen = _make_client([(200, body)])
    items = asyncio.run(client.get_user_feeds(target_uin="8888", nickname="好友甲", num=5))
    assert [i.tid for i in items] == ["t1"]
    req = seen[0]
    p = req["params"]
    assert p["uin"] == "8888" and p["format"] == "jsonp" and p["callback"] == "_preloadCallback"
    assert p["need_comment"] == "1" and p["need_private_comment"] == "1"  # Maizone 实证参数集
    assert req["headers"].get("User-Agent", "").startswith("Mozilla/")  # 无 UA 会被空间 500(联调实证)
    assert req["headers"].get("Referer") == "https://user.qzone.qq.com/8888"


def test_client_failure_raises_no_retry_loop():
    client, _ = _make_client([(500, "err")])
    try:
        asyncio.run(client.get_user_feeds(target_uin="1", nickname="n"))
        raised = False
    except Exception:
        raised = True
    assert raised  # max_retries=0:失败直接抛,由调用方告警跳过


def test_client_download_image_no_size_cap_and_no_extra_params():
    """图片下载不加体积上限(用户裁定 2026-08-31:主程序入站链路自有压缩/丢弃);
    且签名 URL 不得附加 g_tk 等查询参数(破坏签名致 404,联调缺陷#8)。"""
    big = b"x" * (2048 * 1024 + 1)
    seen = []

    async def fake_fetch(method, url, *, params, headers, timeout_ms):
        seen.append((url, dict(params)))
        return 200, big.decode("latin-1")

    async def fake_cookie():
        return {"p_skey": "SK"}

    client = QzoneClient(cookie_provider=fake_cookie, fetch=fake_fetch, timeout_ms=1000, max_retries=0)
    assert asyncio.run(client.download_image("https://img/x.jpg")) == big  # 不设上限,原样返回
    assert all(p == {} for _, p in seen)  # 无 g_tk 等附加参数
