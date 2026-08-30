"""qzone 协议层测试:g_tk/callback 截取/动态解析(纯函数,无网络)。"""
from catsitate_core.qzone.protocol import FeedItem, extract_callback_json, generate_gtk, parse_msglist

# 响应样本结构对照 Maizone qzone_api.py 的字段访问路径(emotion_cgi_msglist_v6)
MSGLIST_FIXTURE = (
    '_Callback( {"code":0,"subcode":0,"message":"","default":0,"data":{"mode":7,"total":2,"vFeeds":['
    '{"appid":311,"tid":"tid_a","abstime":1750000000,'
    '"userinfo":{"uin":10001,"user":{"nick":"小明"}},'
    '"summary":{"summary":"今天天气很好"},'
    '"pic":{"picList":[{"url1":"https://img.example/a.jpg"},{"url1":"https://img.example/b.jpg"}]}},'
    '{"appid":311,"tid":"tid_b","abstime":1750000100,'
    '"userinfo":{"uin":10002,"user":{}},"summary":{"summary":""}},'
    '{"appid":201,"tid":"tid_c","abstime":1750000200,'
    '"userinfo":{"uin":10003,"user":{"nick":"分享控"}},'
    '"summary":{"summary":"分享了日志"}}]}} );'
)


def test_generate_gtk_hash33():
    # hash31/hash33 经典算法:手工演算一个短串
    s, h = "abc", 5381
    for c in s:
        h += (h << 5) + ord(c)
    assert generate_gtk("abc") == (2147483647 & h)


def test_extract_callback_json_tolerates_space():
    payload = extract_callback_json(MSGLIST_FIXTURE)
    assert payload["code"] == 0
    assert len(payload["data"]["vFeeds"]) == 3


def test_parse_msglist_filters_appid_and_maps_fields():
    items, skipped = parse_msglist(extract_callback_json(MSGLIST_FIXTURE))
    assert skipped == 1  # appid=201 的分享被跳过
    assert [i.tid for i in items] == ["tid_a", "tid_b"]
    a = items[0]
    assert (a.uin, a.nickname, a.content, a.appid) == ("10001", "小明", "今天天气很好", 311)
    assert a.image_urls == ["https://img.example/a.jpg", "https://img.example/b.jpg"]
    assert a.abstime == "1750000000"
    b = items[1]
    assert b.nickname == "10002"  # 昵称缺失回退 uin 字符串
    assert b.image_urls == []


def test_parse_msglist_bad_payload():
    items, skipped = parse_msglist({"code": 0, "data": {}})
    assert items == [] and skipped == 0


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


def test_cookie_manager_missing_pskey_warns_none(tmp_path):
    async def fake_api_call(method, **params):
        return {"cookies": {"uin": "o123"}}  # 缺 p_skey

    cm = CookieManager(_cookie_snapshot(tmp_path), api_call=fake_api_call, refresh_minutes=0)
    assert asyncio.run(cm.get()) is None


def _make_client(fetch_responses, cookies=None):
    cookies = cookies if cookies is not None else {"p_skey": "SK", "uin": "o1"}

    async def fake_cookie():
        return cookies

    async def fake_fetch(method, url, *, params, headers, timeout_ms):
        assert params.get("g_tk") == generate_gtk("SK")  # 请求自动携带 g_tk
        return fetch_responses.pop(0)

    return QzoneClient(cookie_provider=fake_cookie, fetch=fake_fetch, timeout_ms=1000, max_retries=0)


def test_client_get_friend_feeds_parses_response():
    body = '_Callback( ' + _json.dumps({
        "code": 0, "data": {"vFeeds": [
            {"appid": 311, "tid": "t1", "abstime": 1, "userinfo": {"uin": 1, "user": {"nick": "a"}},
             "summary": {"summary": "hi"}}
        ]}
    }) + ' );'
    client = _make_client([(200, body)])
    items, skipped = asyncio.run(client.get_friend_feeds())
    assert [i.tid for i in items] == ["t1"] and skipped == 0


def test_client_failure_raises_no_retry_loop():
    client = _make_client([(500, "err")])
    try:
        asyncio.run(client.get_friend_feeds())
        raised = False
    except Exception:
        raised = True
    assert raised  # max_retries=0:失败直接抛,由调用方告警跳过


def test_client_download_image_respects_size_cap():
    big = b"x" * (2048 * 1024 + 1)  # 2048KB+1 超 2048KB 上限

    async def fake_fetch(method, url, *, params, headers, timeout_ms):
        return 200, big.decode("latin-1")

    async def fake_cookie():
        return {"p_skey": "SK"}

    client = QzoneClient(cookie_provider=fake_cookie, fetch=fake_fetch, timeout_ms=1000, max_retries=0)
    assert asyncio.run(client.download_image("https://img/x.jpg", max_kb=2048)) is None  # 超限→None(占位)
    assert asyncio.run(client.download_image("https://img/x.jpg", max_kb=4096)) == big
