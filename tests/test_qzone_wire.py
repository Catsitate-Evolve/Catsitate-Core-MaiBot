"""写路径纯函数测试:表单构造(Maizone 实证参数集)/评论解析。"""

from catsitate_core.qzone.wire import (
    CommentItem, build_comment_form, build_like_form, build_reply_form, parse_feed_comments,
)


def test_build_like_form():
    form = build_like_form(fid="tidA", target_qq="8888", bot_uin="3545773341", now_epoch=1750000000.0)
    assert form["unikey"] == "http://user.qzone.qq.com/8888/mood/tidA"
    assert form["curkey"] == form["unikey"]
    assert form["opuin"] == "3545773341" and form["fid"] == "tidA"
    assert form["qzreferrer"] == "https://user.qzone.qq.com/3545773341"
    assert form["appid"] == 311 and form["format"] == "json" and int(form["abstime"]) == 1750000000


def test_build_comment_form():
    form = build_comment_form(fid="tidA", target_qq="8888", bot_uin="3545773341", content="好看!")
    assert form["topicId"] == "8888_tidA__1"
    assert form["hostUin"] == "8888" and form["uin"] == "3545773341"
    assert form["content"] == "好看!" and form["feedsType"] == 100 and form["format"] == "fs"


def test_build_reply_form():
    form = build_reply_form(fid="tidA", target_qq="8888", bot_uin="3545773341",
                            comment_tid="777", comment_uin="10001", comment_nick="小明", content="谢谢")
    assert form["topicId"] == "8888_tidA__1"
    assert form["commentId"] == "777" and form["commentUin"] == "10001"
    assert form["content"] == "@{uin:10001,nick:小明,auto:1}谢谢"
    assert form["paramstr"] == "1"


def test_parse_feed_comments():
    payload = {
        "code": 0,
        "msglist": [
            {"tid": "tidA", "content": "我的说说", "commentlist": [
                {"tid": "c1", "uin": 10001, "name": "小明", "content": "第一条", "create_time": 1750000001},
                {"tid": 2, "uin": 10002, "name": "小红", "content": "第二条", "create_time": 1750000002},
            ]},
            {"tid": "tidB", "content": "无评论", "commentlist": None},
        ],
    }  # 直接构造 dict,不走截取(brief 原 json.loads('_CB_(...)') 非法 JSON,自注释即此意图)
    out = parse_feed_comments(payload)
    assert set(out) == {"tidA"} and len(out["tidA"]) == 2
    c1 = out["tidA"][0]
    assert (c1.comment_tid, c1.uin, c1.nickname, c1.content, c1.create_time) == (
        "c1", "10001", "小明", "第一条", "1750000001")
    assert out["tidA"][1].comment_tid == "2"  # 数值 tid 归一为字符串


def test_parse_feed_comments_empty():
    assert parse_feed_comments({"code": 0, "msglist": None}) == {}


def test_parse_feed_replies():
    from catsitate_core.qzone.wire import ReplyItem, parse_feed_replies
    payload = {"msglist": [{"tid": "f1", "commentlist": [
        {"tid": "c1", "uin": 3545773341, "name": "bot", "content": "bot的评论", "list_3": [
            {"tid": "r1", "uin": 10001, "name": "小明", "content": "回复bot", "create_time": 1750000001},
            {"tid": "r2", "uin": 3545773341, "name": "bot", "content": "bot自己回的", "create_time": 1750000002},
        ]},
        {"tid": "c2", "uin": 10002, "name": "别人", "content": "不是bot的评论", "list_3": [
            {"tid": "r3", "uin": 10001, "name": "小明", "content": "不相关", "create_time": 1750000003},
        ]},
    ]}]}
    items = parse_feed_replies(payload, bot_uin="3545773341", friend_uin="3298178030")
    assert len(items) == 1  # 只有 r1(bot 自己的 r2 被跳过,c2 不是 bot 的评论)
    r = items[0]
    assert isinstance(r, ReplyItem)
    assert (r.reply_tid, r.parent_comment_tid, r.feed_tid, r.friend_uin) == ("r1", "c1", "f1", "3298178030")
    assert (r.uin, r.nickname, r.content) == ("10001", "小明", "回复bot")


def test_parse_feed_replies_empty():
    from catsitate_core.qzone.wire import parse_feed_replies
    assert parse_feed_replies({"msglist": None}, bot_uin="3545773341", friend_uin="x") == []
