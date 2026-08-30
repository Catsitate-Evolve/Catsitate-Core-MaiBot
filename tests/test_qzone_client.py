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
