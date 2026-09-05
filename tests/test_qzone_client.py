"""qzone 协议层测试:g_tk/callback 截取/说说解析(纯函数,无网络)。

联调修正(2026-08-30):emotion_cgi_msglist_v6 实为「指定用户说说列表」(uin=目标,
响应顶层 msglist,条目含 tid/created_time/content/pic[].url1/commentlist)。
"""
from catsitate_core.qzone.protocol import (
    FeedItem, extract_callback_json, generate_gtk, parse_msglist,
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


def test_feeditem_notify_origin_fields_default_empty():
    """通知 reply 段关联字段(联调修正):origin_tid/origin_content/origin_sender
    默认空串——浏览动态与旧形态通知不受影响,不传即无 reply 语义。"""
    item = FeedItem(tid="t1", abstime="1750000000", uin="10001", nickname="小明", content="正文")
    assert item.origin_tid == "" and item.origin_content == "" and item.origin_sender == ""
    assert item.source == "feed" and item.friend_uin == "" and item.dedup_key == ""


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


def test_cookie_snapshot_saved_at_is_epoch_seconds(tmp_path):
    """2026-09-03 复审小修:快照 saved_at 存 epoch 秒(time.time)——原先误存
    time.monotonic()(进程内时钟,跨重启归零,持久化后无意义);epoch 量级
    (~1.7e9)与 monotonic(开机秒数)数量级可区分。"""

    import time as _time

    async def fake_api_call(method, **kw):
        return {"cookies": {"p_skey": "SK9", "uin": "o999"}}

    snapshot = _cookie_snapshot(tmp_path)
    cm = CookieManager(snapshot, api_call=fake_api_call, refresh_minutes=60)
    before = _time.time()
    assert asyncio.run(cm.get()) == {"p_skey": "SK9", "uin": "o999"}
    after = _time.time()
    saved = snapshot.load()
    assert saved["cookies"] == {"p_skey": "SK9", "uin": "o999"}
    assert before <= saved["saved_at"] <= after  # epoch 秒,非 monotonic 量级


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

    async def fake_fetch(method, url, *, params, headers, timeout_ms, data=None):
        attempts.append(url)
        return (404, b"") if len(attempts) == 1 else (200, b"img")

    async def fake_cookie():
        return {"p_skey": "SK"}

    client = QzoneClient(cookie_provider=fake_cookie, fetch=fake_fetch, timeout_ms=1000)
    assert asyncio.run(client.download_image("https://simg.qpic.cn/x.jpg")) == b"img"
    assert len(attempts) == 2


def test_client_download_image_domain_whitelist():
    """深度审查 E-1:图片下载域名白名单(*.qpic.cn / *.qq.com)——非 QQ 系域名
    (外站/内网地址)拒绝下载且零网络请求:动态载荷的 URL 不可信,防把登录
    Cookie 带去任意域与内网探测;白名单内照常下载。"""
    fetches = []

    async def fake_fetch(method, url, *, params, headers, timeout_ms, data=None):
        fetches.append(url)
        return 200, b"img"

    async def fake_cookie():
        return {"p_skey": "SK"}

    client = QzoneClient(cookie_provider=fake_cookie, fetch=fake_fetch, timeout_ms=1000)
    # 白名单外:外站域名/裸域/内网地址/无 host 形态全拒,零请求
    assert asyncio.run(client.download_image("https://evil.example.com/a.jpg")) is None
    assert asyncio.run(client.download_image("https://qpic.cn/a.jpg")) is None  # 裸域(须为子域形态)
    assert asyncio.run(client.download_image("http://127.0.0.1:8080/a.jpg")) is None
    assert asyncio.run(client.download_image("not-a-url")) is None
    assert fetches == []
    # 白名单内:qpic.cn 子域与 qq.com 子域照常
    assert asyncio.run(client.download_image("https://simg.qpic.cn/a.jpg")) == b"img"
    assert asyncio.run(client.download_image("https://user.qzone.qq.com/a.jpg")) == b"img"
    assert len(fetches) == 2


def test_client_raises_auth_error_on_neg3000():
    """code=-3000(登录态失效)抛 QzoneAuthError,由调用方触发 cookie 失效重取。"""
    from catsitate_core.qzone.client import QzoneAuthError

    body = '_preloadCallback(' + _json.dumps({"code": -3000, "message": "登录态失效"}) + ');'
    client, _ = _make_client([(200, body.encode("utf-8"))])
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

    async def fake_fetch(method, url, *, params, headers, timeout_ms, data=None):
        seen_params.append({"url": url, "params": dict(params), "headers": dict(headers)})
        assert params.get("g_tk") == generate_gtk("SK")  # cgi 请求自动携带 g_tk
        return fetch_responses.pop(0)

    client = QzoneClient(cookie_provider=fake_cookie, fetch=fake_fetch, timeout_ms=1000)
    return client, seen_params


def test_client_get_user_feeds_maizone_params_and_headers():
    body = '_preloadCallback(' + _json.dumps({
        "code": 0, "msglist": [
            {"tid": "t1", "appid": 311, "created_time": 1, "content": "hi", "pic": []}
        ]
    }) + ');'
    client, seen = _make_client([(200, body.encode("utf-8"))])
    items = asyncio.run(client.get_user_feeds(target_uin="8888", nickname="好友甲", num=5))
    assert [i.tid for i in items] == ["t1"]
    req = seen[0]
    p = req["params"]
    assert p["uin"] == "8888" and p["format"] == "jsonp" and p["callback"] == "_preloadCallback"
    assert p["need_comment"] == "1" and p["need_private_comment"] == "1"  # Maizone 实证参数集
    assert req["headers"].get("User-Agent", "").startswith("Mozilla/")  # 无 UA 会被空间 500(联调实证)
    assert req["headers"].get("Referer") == "https://user.qzone.qq.com/8888"


def test_client_failure_raises_no_retry_loop():
    client, _ = _make_client([(500, b"err")])
    try:
        asyncio.run(client.get_user_feeds(target_uin="1", nickname="n"))
        raised = False
    except Exception:
        raised = True
    assert raised  # 失败直接抛,由调用方告警跳过(动作 API 固定不重试)


def test_client_download_image_no_size_cap_and_no_extra_params():
    """图片下载不加体积上限(用户裁定 2026-08-31:主程序入站链路自有压缩/丢弃);
    且签名 URL 不得附加 g_tk 等查询参数(破坏签名致 404,联调缺陷#8)。"""
    big = b"x" * (2048 * 1024 + 1)
    seen = []

    async def fake_fetch(method, url, *, params, headers, timeout_ms, data=None):
        seen.append((url, dict(params)))
        return 200, big

    async def fake_cookie():
        return {"p_skey": "SK"}

    client = QzoneClient(cookie_provider=fake_cookie, fetch=fake_fetch, timeout_ms=1000)
    assert asyncio.run(client.download_image("https://simg.qpic.cn/x.jpg")) == big  # 不设上限,原样返回
    assert all(p == {} for _, p in seen)  # 无 g_tk 等附加参数


# ---- 写路径(fake 注入,无网络) ----


def _post_client(responses, bot_uin=""):
    cookies = {"p_skey": "SK", "uin": "o3545773341"}
    seen = []

    async def fake_cookie():
        return cookies

    async def fake_fetch(method, url, *, params, headers, timeout_ms, data=None):
        seen.append({"method": method, "url": url, "params": dict(params), "data": dict(data or {}), "headers": dict(headers)})
        return responses.pop(0)

    client = QzoneClient(cookie_provider=fake_cookie, fetch=fake_fetch, timeout_ms=1000,
                         bot_uin=bot_uin)
    return client, seen


def test_do_like_posts_form_and_parses_plain_json():
    client, seen = _post_client([(200, b'{"code":0}')])
    assert asyncio.run(client.do_like(fid="tidA", target_qq="8888")) is True
    req = seen[0]
    assert req["method"] == "POST"
    assert "internal_dolike_app" in req["url"]
    assert req["params"]["g_tk"] == generate_gtk("SK")
    assert req["data"]["unikey"].endswith("/mood/tidA")
    assert req["headers"]["Origin"] == "https://user.qzone.qq.com"


def test_do_comment_parses_fs_wrapper():
    client, seen = _post_client([(200, 'frameElement.callback({"code":0,"subcode":0});'.encode())])
    assert asyncio.run(client.do_comment(fid="tidA", target_qq="8888", content="好看!")) is True
    assert "emotion_cgi_re_feeds" in seen[0]["url"]
    assert seen[0]["data"]["topicId"] == "8888_tidA__1"


def test_write_failure_raises_no_retry():
    client, seen = _post_client([(200, b'{"code":-3}')])  # 业务错
    try:
        asyncio.run(client.do_comment(fid="t", target_qq="8", content="x"))
        raised = False
    except RuntimeError:
        raised = True
    assert raised and len(seen) == 1  # 不重试


def test_write_garbage_200_raises_unparseable():
    """批①遗留加固:HTTP 200 + 畸形响应体(网关错误页/非对象 JSON)→
    RuntimeError 且文案含「不可解析」,不泄漏原始解析异常类型。"""
    for garbage in (b"<html>gateway error</html>", b"123"):
        client, _ = _post_client([(200, garbage)])
        try:
            asyncio.run(client.do_comment(fid="t", target_qq="8", content="x"))
            raised = ""
        except RuntimeError as e:
            raised = str(e)
        assert "不可解析" in raised, garbage


def test_write_auth_error_invalidates_cookie():
    from catsitate_core.qzone.client import QzoneAuthError

    async def fake_cookie():
        return {"p_skey": "SK"}

    calls = []

    async def fake_fetch(method, url, *, params, headers, timeout_ms, data=None):
        calls.append(1)
        return 200, '_CB({"code":-3000})'.encode()

    invalidated = []

    class _Client(QzoneClient):
        async def do_comment(self, **kw):
            try:
                return await super().do_comment(**kw)
            except QzoneAuthError:
                invalidated.append(1)
                raise

    client = _Client(cookie_provider=fake_cookie, fetch=fake_fetch, timeout_ms=1000)
    try:
        asyncio.run(client.do_comment(fid="t", target_qq="8", content="x"))
    except QzoneAuthError:
        pass
    assert invalidated == [1] and len(calls) == 1


# ---- 发布说说写路径(emotion_cgi_publish_v6,fake 注入,无网络) ----


def test_do_publish_posts_form_with_uin_query():
    """发布说说:emotion_cgi_publish_v6 端点,查询串除 g_tk 外还带 uin(与上游
    Maizone publish_emotion 的请求一致);表单由 wire.build_publish_form 构造;
    format=json 响应走纯 JSON 解析,返回新说说 tid(顶层 tid 形态)。"""
    client, seen = _post_client([(200, b'{"code":0,"subcode":0,"tid":"newtid123"}')], bot_uin="3545773341")
    assert asyncio.run(client.do_publish(content="今天天气很好")) == "newtid123"
    req = seen[0]
    assert req["method"] == "POST"
    assert "emotion_cgi_publish_v6" in req["url"]
    assert req["params"]["g_tk"] == generate_gtk("SK")
    assert req["params"]["uin"] == "3545773341"  # 查询串带 uin(发布端点实证参数)
    assert req["data"]["con"] == "今天天气很好"
    assert req["data"]["hostuin"] == "3545773341" and req["data"]["who"] == "1"
    assert req["headers"]["Referer"] == "https://user.qzone.qq.com/3545773341"
    assert req["headers"]["Origin"] == "https://user.qzone.qq.com"


def test_do_publish_response_without_tid_returns_empty():
    """发布成功但响应无 tid:返回空串,不抛错——发布已远端成功,tid 缺失只影响
    回注锚,由调用方告警,不误报发布失败。"""
    client, seen = _post_client([(200, b'{"code":0,"data":{}}')], bot_uin="3545773341")
    assert asyncio.run(client.do_publish(content="x")) == ""
    assert len(seen) == 1  # 请求确实发出且成功(非静默跳过)


def test_do_publish_business_error_raises():
    """发布失败(非 0 业务码)→ RuntimeError 显式暴露,不静默当成功;不重试。"""
    client, seen = _post_client([(200, '{"code":-3,"message":"内容含敏感词"}'.encode("utf-8"))], bot_uin="3545773341")
    try:
        asyncio.run(client.do_publish(content="x"))
        raised = ""
    except RuntimeError as e:
        raised = str(e)
    assert "业务错误" in raised
    assert len(seen) == 1  # 失败不重试,由调用方告警跳过


def test_get_own_feed_comments():
    body = '_preloadCallback({"code":0,"msglist":[{"tid":"f1","content":"我的说说","commentlist":[{"tid":"c1","uin":10001,"name":"小明","content":"hi","create_time":1750000001}]}]});'.encode()
    client, seen = _post_client([(200, body)])
    comments, ctx, replies = asyncio.run(client.get_own_feed_comments(bot_uin="3545773341"))
    assert comments["f1"][0].comment_tid == "c1"
    assert ctx["f1"] == "我的说说"
    assert replies == []  # 无 bot 评论的载荷:楼中楼视图为空(补跑解析不产假数据)
    assert seen[0]["params"]["uin"] == "3545773341"  # 原占位防误删,换真锚:msglist 请求以 bot_uin 为目标


def test_get_own_feed_comments_three_views_with_bot_comment_replies():
    """自己说说载荷三视图(源A 断链修复):bot 顶层评论+其 list_3 好友回复与
    好友顶层评论同载荷并存——评论映射/正文上下文/楼中楼回复三视图一次请求
    产出(补跑 parse_feed_replies,不发第二次请求),bot 自己的楼中楼回复与
    好友顶层评论下的旁听楼中楼不进回复视图(只解析 bot 评论的 list_3)。"""
    payload = {"code": 0,
               "usrinfo": {"uin": "3545773341"}, "logininfo": {"uin": "3545773341"},
               "msglist": [{"tid": "ownf1", "content": "我的说说正文", "cmtnum": 2,
                            "commentlist": [
                                # 好友的顶层评论(无楼中楼):进评论映射
                                {"tid": "fc1", "uin": 10001, "name": "小明",
                                 "content": "好友评论", "create_time": 1750000001},
                                # bot 的顶层评论+list_3:好友回复进回复视图;
                                # bot 自己的回复被解析层滤掉(不通知自己)
                                {"tid": "bc1", "uin": "3545773341", "name": "我",
                                 "content": "我的评论",
                                 "list_3": [
                                     {"tid": "rr1", "uin": 10001, "name": "小明",
                                      "content": "回复你的评论", "create_time": 1750000002},
                                     {"tid": "rr2", "uin": "3545773341", "name": "我",
                                      "content": "bot 自己的楼中楼", "create_time": 1750000003},
                                 ]},
                            ]}]}
    client, _ = _post_client([(200, ("_preloadCallback(" + _json.dumps(payload) + ")").encode("utf-8"))])
    comments, ctx, replies = asyncio.run(client.get_own_feed_comments(bot_uin="3545773341"))
    # 视图①评论映射:两条顶层评论(好友+bot 自己)原样可判
    assert [c.comment_tid for c in comments["ownf1"]] == ["fc1", "bc1"]
    # 视图②正文上下文:说说显示文本
    assert ctx["ownf1"] == "我的说说正文"
    # 视图③楼中楼:仅好友对 bot 评论的回复(bot 自己的 rr2 被滤);
    # 二元组素材齐全(主评论=bot 的 bc1,feed 归属/上下文文本同视图)
    assert len(replies) == 1
    r = replies[0]
    assert (r.reply_tid, r.uin, r.content) == ("rr1", "10001", "回复你的评论")
    assert (r.feed_tid, r.parent_comment_tid, r.friend_uin) == ("ownf1", "bc1", "3545773341")
    assert r.parent_comment_content == "我的评论" and r.feed_content == "我的说说正文"


# ---- 统一时间线发现层(M3:feeds3_html_more,fake 注入,无网络) ----
from catsitate_core.qzone.discovery import FeedDiscovery

# 实证结构简化样本:外层 JSON + 内层 JS 对象(opuin 为生产实证的单引号形态)
UNIFIED_SAMPLE = '''{"code":0,"data":{main:{
  begintime:'1788164300',
  html:'<div>template</div>',
  key:'ee3396c4d238956ac2f90b00',
  appid:311,
  abstime:1788164306,
  opuin:'3298178030',
  nickname:'Hesitate_P',
}}}'''


def _unified_client(responses):
    """带 bot_uin 的客户端(发现层以 bot 自己的身份拉好友时间线)。"""
    seen = []

    async def fake_cookie():
        return {"p_skey": "SK", "uin": "o3545773341"}

    async def fake_fetch(method, url, *, params, headers, timeout_ms, data=None):
        seen.append({"url": url, "params": dict(params), "headers": dict(headers)})
        return responses.pop(0)

    client = QzoneClient(cookie_provider=fake_cookie, fetch=fake_fetch,
                         timeout_ms=1000, bot_uin="3545773341")
    return client, seen


def test_client_get_unified_timeline_request_and_parse():
    """发现层 API:feeds3_html_more 端点/实证参数集/bot 空间首页 Referer,
    响应经 parse_unified_timeline 返回 FeedDiscovery 列表。"""
    client, seen = _unified_client([(200, UNIFIED_SAMPLE.encode("utf-8"))])
    items, cursor = asyncio.run(client.get_unified_timeline(count=20))
    assert len(items) == 1 and isinstance(items[0], FeedDiscovery)
    assert cursor == "1788164300"  # 下一页游标取自 main.begintime
    assert (items[0].tid, items[0].uin, items[0].nickname, items[0].abstime, items[0].appid) == (
        "ee3396c4d238956ac2f90b00", "3298178030", "Hesitate_P", "1788164306", 311)
    req = seen[0]
    assert req["url"].endswith("/cgi-bin/feeds/feeds3_html_more")
    p = req["params"]
    assert p["uin"] == "3545773341"  # uin=bot 自己(以自己视角看全好友时间线)
    assert p["format"] == "json"  # 非 jsonp:响应无 callback 包裹
    assert "begin" not in p and p["count"] == "20"  # 旧 begin 偏移已删(实证被无视)
    assert p["update"] == "1" and p["scope"] == "2" and p["filter"] == "all"  # scope=2=好友动态流
    assert p["g_tk"] == generate_gtk("SK")  # cgi 读路径自动携带
    assert req["headers"]["Referer"] == "https://user.qzone.qq.com/3545773341/home"
    assert req["headers"]["User-Agent"].startswith("Mozilla/")  # 无 UA 空间接口 500(联调实证)


def test_client_unified_default_count():
    client, seen = _unified_client([(200, UNIFIED_SAMPLE.encode("utf-8"))])
    asyncio.run(client.get_unified_timeline())
    assert seen[0]["params"]["count"] == "20"


def test_client_unified_auth_error_raises():
    """code=-3000 → QzoneAuthError(触发 cookie 失效重取,与读/写路径一致)。"""
    from catsitate_core.qzone.client import QzoneAuthError

    client, _ = _unified_client([(200, b'{"code":-3000,"message":"no login"}')])
    try:
        asyncio.run(client.get_unified_timeline())
        raised = None
    except QzoneAuthError:
        raised = "auth"
    assert raised == "auth"


def test_client_unified_business_error_and_garbage_raise():
    """非 0 业务码 / 畸形 200 响应 → RuntimeError 显式暴露(不静默当空时间线)。"""
    for body, needle in ((b'{"code":-4001}', "业务错误"), (b"<html>gateway error</html>", "不可解析")):
        client, _ = _unified_client([(200, body)])
        try:
            asyncio.run(client.get_unified_timeline())
            raised = ""
        except RuntimeError as e:
            raised = str(e)
        assert needle in raised, body


def test_client_unified_http_failure_raises_no_retry():
    client, seen = _unified_client([(500, b"err")])
    try:
        asyncio.run(client.get_unified_timeline())
        raised = False
    except RuntimeError:
        raised = True
    assert raised and len(seen) == 1  # 读路径失败不重试,由调用方告警/回退


def test_client_fetch_unified_begintime_scope_passthrough():
    """游标透传(2026-09-03 实证改造):begintime=上页 main.begintime 为唯一
    续页参数;scope=1 为「与我相关」流(源C 赞事件复用同通道);缺省不携带。"""
    client, seen = _unified_client([(200, b'{"code":0,"data":{}}'), (200, b'{"code":0,"data":{}}')])
    asyncio.run(client._fetch_unified(count=50, scope=1, begintime=1788164300))
    p = seen[0]["params"]
    assert p["begintime"] == "1788164300" and p["count"] == "50" and p["scope"] == "1"
    asyncio.run(client._fetch_unified(count=50, scope=1))
    assert "begintime" not in seen[1]["params"]  # 首页不携带


def test_client_fetch_unified_cursor_passthrough_via_get_unified_timeline():
    """get_unified_timeline 透传游标(发现层翻页入口),scope 默认 2(好友动态流)。"""
    client, seen = _unified_client([(200, UNIFIED_SAMPLE.encode("utf-8"))])
    items, cursor = asyncio.run(client.get_unified_timeline(count=30, begintime="1788164300"))
    assert len(items) == 1  # 样本时间线照常解析(翻页不改变解析路径)
    p = seen[0]["params"]
    assert p["begintime"] == "1788164300" and p["count"] == "30" and p["scope"] == "2"


def test_client_fetch_likes_raw_scope1():
    """源C 赞事件输入通道:feeds3_html_more?scope=1(「与我相关」流)。
    get_like_events 消费此原始文本;赞事件只取最新一页(不携带翻页游标)。"""
    client, seen = _unified_client([(200, b'{"code":0,"data":{}}')])
    asyncio.run(client._fetch_likes_raw(count=10))
    p = seen[0]["params"]
    assert p["scope"] == "1" and p["count"] == "10" and "begintime" not in p


def _msglist_payload_with_comments():
    """msglist 载荷(含一条带 commentlist 的说说),M3-r2 表达生成层素材源。"""
    return '_preloadCallback(' + _json.dumps({
        "code": 0, "msglist": [
            {"tid": "t1", "appid": 311, "created_time": 1, "content": "今天的心情",
             "pic": [],
             "commentlist": [{"tid": "c1", "uin": 20000, "name": "小红",
                              "content": "好看!", "create_time": 2}]},
        ]
    }) + ');'


def test_get_user_feeds_merges_comments():
    """get_user_feeds 在 parse_msglist 之上用 parse_feed_comments_full 合并结构化
    评论区块(顶层+楼中楼+总数,2026-09-02 设计共识):comment_map/注入评论区/
    详情工具共用此数据;旧「昵称:内容」字符串摘要已随 recent_comments 删除。"""
    client, _ = _make_client([(200, _msglist_payload_with_comments().encode("utf-8"))])
    feeds = asyncio.run(client.get_user_feeds(target_uin="100", nickname="小明", num=3))
    assert feeds and feeds[0].comments
    c0 = feeds[0].comments[0]
    assert c0.nickname == "小红" and c0.uin and c0.comment_tid
    assert isinstance(c0.replies, list)  # 楼中楼列表(载荷有则已解析)


def test_get_user_feeds_parses_mention_markup_in_comments():
    """评论/楼中楼正文里的 @{uin,nick,who,auto} 机器格式解析为可读 @昵称
    (联调实证 2026-09-03:楼中楼机器 @ 原样泄漏进详情输出——detail 与浏览
    注入两路共用此数据,解析收敛在 client 一处)。"""

    payload = {"msglist": [{"tid": "tm1", "uin": "100", "nickname": "小明", "content": "正文",
                            "created_time": "1750000000", "cmtnum": 1,
                            "commentlist": [
                                {"tid": "c1", "uin": "20000", "name": "小红",
                                 "content": "@{uin:100,nick:小明,who:1,auto:1}冒个泡",
                                 "create_time": "1750000100",
                                 "list_3": [{"tid": "r1", "uin": "100", "name": "小明",
                                             "content": "@{uin:20000,nick:小红,who:1,auto:1}收到啦",
                                             "create_time": "1750000200"}]}]}],
               "usrinfo": {"uin": "100"}, "logininfo": {"uin": "3545773341"}, "code": 0}
    client, _ = _make_client([(200, ("_preloadCallback(" + _json.dumps(payload) + ")").encode("utf-8"))])
    feeds = asyncio.run(client.get_user_feeds(target_uin="100", nickname="小明", num=3))
    c = feeds[0].comments[0]
    assert c.content == "@小明 冒个泡"  # 机器 @ 已解析(含 who/auto 额外字段)
    assert c.replies[0].content == "@小红 收到啦"


def test_extract_timeline_cursor_basetime_fallback():
    """游标提取回退分支(终审测试盲区):main.begintime 缺失时取 externparam
    的 basetime;两者皆无返回空串(调用方终止翻页)。"""
    from catsitate_core.qzone.client import QzoneClient

    assert QzoneClient.extract_timeline_cursor("{main:{begintime:'1788164300'}}") == "1788164300"
    assert QzoneClient.extract_timeline_cursor("externparam:'basetime=1785058947&pagenum=3'") == "1785058947"
    assert QzoneClient.extract_timeline_cursor("{}") == ""


def test_rate_limit_code_classified_on_unified_and_msglist():
    """-10001(服务端限流 network busy)→ QzoneRateLimitError,与普通业务
    错误(RuntimeError)分流——调用方据此跳过本轮,禁止回退放大请求的路径。"""

    from catsitate_core.qzone.client import QzoneRateLimitError

    client, _ = _unified_client([(200, b'{"code":-10001,"subcode":-30,"message":"network busy"}')])
    try:
        asyncio.run(client.get_unified_timeline())
        raised = ""
    except QzoneRateLimitError as e:
        raised = str(e)
    assert "限流" in raised and "-10001" in raised

    client2, _ = _make_client([(200, '_preloadCallback({"code":-10001,"message":"network busy"});'.encode("utf-8"))])
    try:
        asyncio.run(client2.get_user_feeds(target_uin="100", nickname="小明", num=3))
        raised = ""
    except QzoneRateLimitError as e:
        raised = str(e)
    assert "限流" in raised
