"""写路径纯函数测试:表单构造(Maizone 实证参数集)/评论解析/发布表单。"""

from catsitate_core.qzone.wire import (
    CommentItem, build_comment_form, build_like_form, build_publish_form, build_reply_form,
    extract_publish_tid, parse_feed_comments,
)


def test_build_publish_form():
    """发表说说表单:参数集对照上游开源实现 Maizone 的 publish_emotion 核实
    ——纯文本说说不带 pic_bo/richtype/richval(带图发布需先走图片上传通道,
    当前不支持);who 是「以自己身份发表」的固定标志 "1",不是 QQ 号;
    format=json 表示响应为纯 JSON(无 callback 包裹)。"""
    form = build_publish_form(content="今天天气很好", bot_uin="3545773341")
    # 全量键值比对,防拼写/取值漂移
    assert form == {
        "syn_tweet_verson": "1",
        "paramstr": "1",
        "who": "1",
        "con": "今天天气很好",
        "feedversion": "1",
        "ver": "1",
        "ugc_right": "1",
        "to_sign": "0",
        "hostuin": "3545773341",
        "code_version": "1",
        "format": "json",
        "qzreferrer": "https://user.qzone.qq.com/3545773341",
    }


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


# ---- 发布响应 tid 提取(回注锚/seen/registry 登记的输入) ----


def test_extract_publish_tid_variants():
    """发布响应 tid 提取:键形态按端点历史版本逐层尝试(顶层 tid / data.tid /
    data.newtid / 顶层 newtid / content[0].tid);取不到返回空串——发布已成功,
    仅回注缺锚,由调用方告警,不误报发布失败。"""
    assert extract_publish_tid({"data": {"tid": "abc123"}}) == "abc123"
    assert extract_publish_tid({"tid": "def456"}) == "def456"
    assert extract_publish_tid({"content": [{"tid": "ghi789"}]}) == "ghi789"
    assert extract_publish_tid({"code": 0, "data": {}}) == ""
    # newtid 变体(端点历史版本的另一键名)与空白归一
    assert extract_publish_tid({"data": {"newtid": "n1"}}) == "n1"
    assert extract_publish_tid({"newtid": " n2 "}) == "n2"
    assert extract_publish_tid({"tid": 12345}) == "12345"  # 数值 tid 归一字符串
    assert extract_publish_tid({"content": []}) == ""


def test_parse_feed_comments_empty():
    assert parse_feed_comments({"code": 0, "msglist": None}) == {}


def test_parse_feed_replies():
    from catsitate_core.qzone.wire import ReplyItem, parse_feed_replies
    # usrinfo=被拉取者(说说主人),logininfo=访客(bot)——联调实测样本形态(test_qzone_client.MSGLIST_JSONP)
    payload = {"code": 0, "logininfo": {"name": "Catsitate-dev", "uin": 3545773341},
               "usrinfo": {"name": "好友甲", "uin": 8888},
               "msglist": [{"tid": "f1", "commentlist": [
        {"tid": "c1", "uin": 3545773341, "name": "bot", "content": "bot的评论", "list_3": [
            {"tid": "r1", "uin": 10001, "name": "小明", "content": "回复bot", "create_time": 1750000001},
            {"tid": "r2", "uin": 3545773341, "name": "bot", "content": "bot自己回的", "create_time": 1750000002},
        ]},
        {"tid": "c2", "uin": 10002, "name": "别人", "content": "不是bot的评论", "list_3": [
            {"tid": "r3", "uin": 10001, "name": "小明", "content": "不相关", "create_time": 1750000003},
        ]},
    ]}]}
    items = parse_feed_replies(payload, bot_uin="3545773341")
    assert len(items) == 1  # 只有 r1(bot 自己的 r2 被跳过,c2 不是 bot 的评论)
    r = items[0]
    assert isinstance(r, ReplyItem)
    assert (r.reply_tid, r.parent_comment_tid, r.feed_tid, r.friend_uin) == ("r1", "c1", "f1", "8888")
    assert (r.uin, r.nickname, r.content) == ("10001", "小明", "回复bot")
    assert r.create_time == "1750000001"


def test_parse_feed_replies_numeric_tid_normalized():
    """数值 tid/uin 归一字符串;bot 评论 uin 为数值时与字符串 bot_uin 可比对。"""
    from catsitate_core.qzone.wire import parse_feed_replies
    payload = {"usrinfo": {"uin": 8888}, "msglist": [{"tid": "f1", "commentlist": [
        {"tid": 55, "uin": 3545773341, "name": "bot", "content": "x", "list_3": [
            {"tid": 77, "uin": 10001, "name": "小明", "content": "hi", "create_time": 1750000001},
        ]},
    ]}]}
    items = parse_feed_replies(payload, bot_uin="3545773341")
    assert len(items) == 1
    r = items[0]
    assert (r.reply_tid, r.parent_comment_tid) == ("77", "55")
    assert (r.uin, r.friend_uin) == ("10001", "8888")


def test_parse_feed_replies_missing_usrinfo_degrades_friend_uin(caplog):
    """载荷缺 usrinfo(非实测形态):回复仍解析,friend_uin 降级空串并告警(不静默)。"""
    import logging

    from catsitate_core.qzone.wire import parse_feed_replies
    payload = {"msglist": [{"tid": "f1", "commentlist": [
        {"tid": "c1", "uin": 3545773341, "name": "bot", "content": "x", "list_3": [
            {"tid": "r1", "uin": 10001, "name": "小明", "content": "回复", "create_time": 1750000001},
        ]},
    ]}]}
    with caplog.at_level(logging.WARNING, logger="catsitate_core.qzone.wire"):
        items = parse_feed_replies(payload, bot_uin="3545773341")
    assert len(items) == 1 and items[0].friend_uin == ""
    assert "usrinfo" in caplog.text  # 告警显式暴露,不静默降级


def test_parse_feed_replies_empty():
    from catsitate_core.qzone.wire import parse_feed_replies
    assert parse_feed_replies({"msglist": None}, bot_uin="3545773341") == []
    assert parse_feed_replies({}, bot_uin="3545773341") == []
    # bot 评论存在但无 list_3:无楼中楼,空列表
    assert parse_feed_replies({"usrinfo": {"uin": "8"}, "msglist": [
        {"tid": "f1", "commentlist": [{"tid": "c1", "uin": 3545773341, "name": "bot", "content": "x"}]}
    ]}, bot_uin="3545773341") == []


# ---- @ 解析与楼中楼父评论上下文(提示词可读性优化 2026-09-01) ----


def test_parse_qzone_mentions_replaces_with_nick():
    """@{uin,nick,...} → @昵称(后接空格,拟 QQ 客户端 @ 展示形态);缺 nick 回退 uin。"""
    from catsitate_core.qzone.wire import parse_qzone_mentions

    assert parse_qzone_mentions("@{uin:123,nick:小明,auto:1}你好", bot_uin="10000") == "@小明 你好"
    assert parse_qzone_mentions("@{uin:123,auto:1}早", bot_uin="10000") == "@123 早"  # 缺 nick 回退 uin
    assert parse_qzone_mentions("开头@{uin:456,nick:小红}结尾", bot_uin="10000") == "开头@小红 结尾"


def test_parse_qzone_mentions_passthrough_when_no_mention_or_no_uin():
    """无 @ 原样;无 uin 的畸形花括号原样保留(不吞文本)。"""
    from catsitate_core.qzone.wire import parse_qzone_mentions

    assert parse_qzone_mentions("普通文本没有花括号", bot_uin="10000") == "普通文本没有花括号"
    assert parse_qzone_mentions("@{auto:1}畸形", bot_uin="10000") == "@{auto:1}畸形"
    assert parse_qzone_mentions("", bot_uin="10000") == ""


def test_parse_qzone_mentions_keeps_bot_self_mention():
    """Q2=a:纯格式转换,@bot 自己也保留(bot_uin 仅作语境,不过滤)。"""
    from catsitate_core.qzone.wire import parse_qzone_mentions

    assert parse_qzone_mentions(
        "@{uin:10000,nick:我自己,auto:1}在吗", bot_uin="10000"
    ) == "@我自己 在吗"


def test_parse_feed_replies_carries_parent_comment_content():
    """楼中楼上下文(Q3=a):ReplyItem 带 bot 主评论正文 parent_comment_content。"""
    from catsitate_core.qzone.wire import parse_feed_replies

    payload = {"usrinfo": {"uin": 8888}, "msglist": [{"tid": "f1", "commentlist": [
        {"tid": "c1", "uin": 3545773341, "name": "bot", "content": "bot 的主评论",
         "list_3": [{"tid": "r1", "uin": 10001, "name": "小明", "content": "回复bot",
                     "create_time": 1750000001}]},
    ]}]}
    items = parse_feed_replies(payload, bot_uin="3545773341")
    assert len(items) == 1
    assert items[0].parent_comment_content == "bot 的主评论"


def test_parse_feed_replies_parent_comment_content_defaults_empty():
    """主评论条目缺 content(非实测形态容错):parent_comment_content 降级空串,
    通知正文构造侧以「你之前的评论」兜底。"""
    from catsitate_core.qzone.wire import parse_feed_replies

    payload = {"usrinfo": {"uin": 8888}, "msglist": [{"tid": "f1", "commentlist": [
        {"tid": "c1", "uin": 3545773341, "name": "bot", "list_3": [
            {"tid": "r1", "uin": 10001, "name": "小明", "content": "回复", "create_time": 1750000001},
        ]},
    ]}]}
    items = parse_feed_replies(payload, bot_uin="3545773341")
    assert len(items) == 1 and items[0].parent_comment_content == ""


# ---- 「与我相关」流赞事件解析(源C) ----


def _scope1_like_feed(liker_uin: str, owner_uin: str, nick: str, tid: str, fhash: str,
                      action: str = "赞了我的说说", when: str = "昨天 13:20") -> str:
    """构造实机形态的「与我相关」条目:外层 JSON 内嵌 JS 对象,HTML 片段以 \\xHH/\\t 转义存储;
    条目内顺序=头部(user-info:昵称/动作/时间)在前,data-key 与 data-fkey/data-tid 在后。"""
    fkey = f"{liker_uin}_{owner_uin}_{fhash}"
    return (
        '\\x3Cdiv class=\\x22user-info\\x22\\x3E\\x3Cdiv class=\\x22f-nick\\x22\\x3E'
        '\\x3Ca class=\\x22f-name q_namecard \\x22 link=\\x22nameCard_' + liker_uin + '\\x22\\x3E' + nick + '\\x3C/a\\x3E'
        '\\x3Cspan  class=\\x22 ui-mr10 state\\x22 \\x3E\\t\\t' + action + '\\t\\t\\x3C/span\\x3E'
        '\\x3Cspan  class=\\x22 ui-mr8 state\\x22 \\x3E\\t\\t' + when + '\\t\\t\\x3C/span\\x3E'
        '\\x3C/div\\x3E\\x3C/div\\x3E'
        '\\x3Cli data-key=\\x22' + fkey + '\\x22\\x3E'
        '\\x3Cdiv data-fkey=\\x22' + fkey + '\\x22 data-tid=\\x22' + tid + '\\x22 data-uin=\\x22' + owner_uin + '\\x22\\x3E'
    )


def test_parse_like_events_real_shape():
    """实机形态:转义归一后按 data-fkey/data-tid 锚提取,相对时间折算 epoch。"""
    from datetime import datetime, timedelta

    from catsitate_core.qzone.discovery import parse_like_events

    html = _scope1_like_feed("11111", "22222", "小明", "1d3558d0a9b1", "e478ef4cf")
    events = parse_like_events(html)
    assert len(events) == 1
    ev = events[0]
    assert ev.like_key == "11111_22222_e478ef4cf" and ev.liker_uin == "11111"
    assert ev.owner_uin == "22222" and ev.target_tid == "1d3558d0a9b1"
    assert ev.liker_nickname == "小明"
    # 昨天 13:20 → epoch(天级精度折算,允许 ±2 分钟误差)
    expect = datetime.now().replace(hour=13, minute=20, second=0, microsecond=0) - timedelta(days=1)
    assert abs(int(ev.create_time) - int(expect.timestamp())) < 120


def test_parse_like_events_excludes_tips_and_nonlike():
    """推广位(LikeTipsFeeds,无三元组形态)与评论类条目不产出赞事件。"""
    from catsitate_core.qzone.discovery import parse_like_events

    html = (
        '<div data-fkey="LikeTipsFeeds" data-tid="LikeTipsFeeds" data-uin="">'
        '<p>好友点赞通知设置</p></div>'
        + _scope1_like_feed("11111", "22222", "小明", "aa11", "ff00", action="评论了我的说说")
    )
    assert parse_like_events(html) == []


def test_parse_like_events_multi_entries_no_cross_borrow():
    """多条目:昵称/时间不跨条目借用(窗口至下一 fkey 锚)。"""
    from datetime import datetime, timedelta

    from catsitate_core.qzone.discovery import parse_like_events

    html = (
        _scope1_like_feed("11111", "22222", "小明", "ee11", "aaaa", when="今天 09:00")
        + _scope1_like_feed("33333", "44444", "小红", "ee22", "bbbb", when="前天 20:30")
    )
    events = parse_like_events(html)
    assert len(events) == 2
    first = next(e for e in events if e.liker_nickname == "小明")
    second = next(e for e in events if e.liker_nickname == "小红")
    assert first.target_tid == "ee11" and first.owner_uin == "22222"
    assert second.target_tid == "ee22" and second.owner_uin == "44444"
    expect1 = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    expect2 = datetime.now().replace(hour=20, minute=30, second=0, microsecond=0) - timedelta(days=2)
    assert abs(int(first.create_time) - int(expect1.timestamp())) < 120
    assert abs(int(second.create_time) - int(expect2.timestamp())) < 120


def test_parse_like_events_no_nick_fallback_and_empty():
    """异常形态容错:无昵称锚回退 fkey 前段(不静默丢事件);无锚文本零事件。"""
    from catsitate_core.qzone.discovery import parse_like_events
    from catsitate_core.qzone.wire import LikeEvent

    html = (
        '\\x3Cspan  class=\\x22 ui-mr10 state\\x22 \\x3E\\t\\t赞了我的说说\\t\\t\\x3C/span\\x3E'
        '\\x3Cli data-key=\\x22100_200_ab12\\x22\\x3E'
        '\\x3Cdiv data-fkey=\\x22100_200_ab12\\x22 data-tid=\\x22ee33\\x22 data-uin=\\x22200\\x22\\x3E'
    )
    events = parse_like_events(html)
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, LikeEvent)
    assert (ev.liker_uin, ev.liker_nickname) == ("100", "100")
    assert parse_like_events("") == []
    assert parse_like_events("普通文本无锚点") == []
    assert parse_like_events('<li data-fkey="畸形" data-tid="x"></li>') == []


def test_parse_feed_comments_full_structured():
    """结构化评论区块:顶层评论+楼中楼(list_3)+总数(cmtnum)解析;畸形条目跳过;
    cmtnum 未标注时回退列表长度(不臆造)。"""
    from catsitate_core.qzone.wire import parse_feed_comments_full

    payload = {"msglist": [{"tid": "t1", "cmtnum": 5, "commentlist": [
        {"tid": "c1", "uin": "20000", "name": "小红", "content": "好文", "create_time": "1750000000",
         "list_3": [{"tid": "r1", "uin": "30000", "name": "小刚", "content": "同感", "create_time": "1750000100"}]},
        {"tid": "c2", "uin": "40000", "name": "小蓝", "content": "顶", "create_time": "1750000200", "list_3": []},
        {"uin": "50000", "name": "畸形无tid", "content": "x"},  # 缺 tid 跳过
    ]}]}
    blocks = parse_feed_comments_full(payload)
    assert set(blocks) == {"t1"}
    b = blocks["t1"]
    assert b.total == 5  # cmtnum
    assert [c.comment_tid for c in b.comments] == ["c1", "c2"]
    c1 = b.comments[0]
    assert c1.nickname == "小红" and c1.replies and c1.replies[0].nickname == "小刚"
    assert c1.reply_total == 1 and b.comments[1].reply_total == 0
    # cmtnum 缺失:回退列表长度
    payload2 = {"msglist": [{"tid": "t2", "commentlist": [{"tid": "c9", "uin": "1", "name": "n", "content": "c"}]}]}
    assert parse_feed_comments_full(payload2)["t2"].total == 1
