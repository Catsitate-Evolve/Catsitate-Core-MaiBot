"""qzone 统一时间线解析器测试(feeds3_html_more 响应,纯函数,无网络)。

实证结构(2026-08-31 生产验证):外层 JSON(`{"code":0,"data":{...}}`),
内层 data.main 为 JS 对象字面量(单引号字符串、无引号键名,非严格 JSON);
每个动态条目按序出现 key:'{十六进制tid}' / appid:{int} / abstime:{int}
/ opuin:'{uin}' / nickname:'{name}'。测试样本为该结构的简化复刻。
"""
from catsitate_core.qzone.discovery import FeedDiscovery, parse_unified_timeline

# 实证样本(简化):多作者混合 + 非 hex key 条目(应跳过)
# 字段序按生产实证规范:key → appid → abstime → opuin → nickname(审查修复#2:
# 首条目 appid 原置于 key 之前,与生产序矛盾且依赖跨条目借用巧合)
TIMELINE_SAMPLE = '''{"code":0,"data":{main:{
  html:'<div>template</div>',
  ver:1, typeid:0,
  key:'ee3396c4d238956ac2f90b00',
  appid:311,
  flag:0, dataonly:0,
  abstime:1788164306,
  feedstime:' 16:18',
  opuin:3298178030,
  nickname:'Hesitate_P',
  scope:0
}},
{main:{
  key:'982d28c79ec4706afbec0000',
  appid:311,
  abstime:1785775261,
  opuin:3341299096,
  nickname:'可回收飞舞',
}},
{main:{
  key:'_old_deleted',
  appid:201,
  abstime:1783000000,
  opuin:12345678,
  nickname:'Other App',
}}}'''


def test_parse_multi_author_entries():
    """多作者解析:两个合法条目全字段对齐(tid/uin/nickname/abstime/appid)。"""
    items = parse_unified_timeline(TIMELINE_SAMPLE)
    assert [i.tid for i in items] == ["ee3396c4d238956ac2f90b00", "982d28c79ec4706afbec0000"]
    a, b = items
    assert (a.uin, a.nickname, a.abstime, a.appid) == ("3298178030", "Hesitate_P", "1788164306", 311)
    assert (b.uin, b.nickname, b.abstime, b.appid) == ("3341299096", "可回收飞舞", "1785775261", 311)
    assert isinstance(a.appid, int)


def test_parse_skips_non_hex_key():
    """key 非十六进制('_old_deleted')→ 不构成条目定位点,整条跳过。"""
    items = parse_unified_timeline(TIMELINE_SAMPLE)
    assert "_old_deleted" not in [i.tid for i in items]


def test_parse_skips_empty_key():
    assert parse_unified_timeline("{main:{key:'',appid:311,abstime:1,opuin:'1',nickname:'x'}}") == []


def test_parse_keeps_non_shuoshuo_appid_for_caller():
    """appid!=311 的条目解析层不过滤(保留 appid 供调用方决定去留)。"""
    sample = '''{"code":0,"data":{main:{
  key:'deadbeefdeadbeefdeadbeef',
  appid:202,
  abstime:1783000100,
  opuin:'1111111111',
  nickname:'非说说应用',
}}}'''
    items = parse_unified_timeline(sample)
    assert len(items) == 1
    assert items[0].appid == 202 and items[0].tid == "deadbeefdeadbeefdeadbeef"


def test_parse_quoted_opuin_production_form():
    """生产实证 opuin 为单引号字符串(样本里是裸数字)——两种形态都收,统一转 str。"""
    sample = '''{"code":0,"data":{main:{
  key:'0123456789abcdef01234567',
  appid:311,
  abstime:1788169999,
  opuin:'3298178030',
  nickname:'可回收飞舞',
}}}'''
    items = parse_unified_timeline(sample)
    assert items[0].uin == "3298178030"


def test_parse_decodes_js_escapes():
    """JS 转义解码:nickname 内的 \' 与 \" 还原为裸引号。"""
    sample = '''{"code":0,"data":{main:{
  key:'abcdefabcdefabcdefabcdef',
  appid:311,
  abstime:1783000200,
  opuin:'2222222222',
  nickname:'名字\\'带单引号和\\"双引号\\"',
}}}'''
    items = parse_unified_timeline(sample)
    assert len(items) == 1
    assert items[0].nickname == '''名字'带单引号和"双引号"'''


def test_parse_skips_malformed_entry_at_tail():
    """缺必需字段(abstime)的条目跳过(置于末尾:窗口回退固定上界仍无该字段),
    且不阻断后续合法条目的解析。"""
    sample = '''{"code":0,"data":{
{main:{key:'fedcba9876543210fedcba98', appid:311, abstime:1783000300, opuin:'8888888888', nickname:'正常条目'}},
{main:{key:'abc123def4560abc123def456', appid:311, opuin:'9999999999', nickname:'缺abstime'}}
}}'''
    items = parse_unified_timeline(sample)
    assert [i.tid for i in items] == ["fedcba9876543210fedcba98"]


def test_parse_malformed_middle_entry_does_not_borrow_from_neighbor():
    """审查修复#1:中间位置畸形条目(缺 abstime)不得越过下一 key 锚点向邻条目
    借用同名字段误组装——窗口以下一 `key:'` 锚点为上界。"""
    sample = '''{"code":0,"data":{
{main:{key:'fedcba9876543210fedcba98', appid:311, abstime:1783000300, opuin:'8888888888', nickname:'首个正常'}},
{main:{key:'abc123def4560abc123def456', appid:311, opuin:'9999999999', nickname:'缺abstime'}},
{main:{key:'0123456789abcdef01234567', appid:311, abstime:1783000400, opuin:'7777777777', nickname:'末个正常'}}
}}'''
    items = parse_unified_timeline(sample)
    # 中间畸形条目被跳过(不借邻条目 abstime/appid),前后条目均正常解析
    assert [i.tid for i in items] == ["fedcba9876543210fedcba98", "0123456789abcdef01234567"]
    assert items[1].abstime == "1783000400" and items[1].nickname == "末个正常"


def test_parse_tolerates_uppercase_hex_tid():
    """审查顺手①:key 十六进制容忍大写(实证为小写,防御性兼容)。"""
    sample = '''{"code":0,"data":{main:{
  key:'ABCDEF0123456789ABCDEF01',
  appid:311,
  abstime:1783000500,
  opuin:'3333333333',
  nickname:'大写TID',
}}}'''
    items = parse_unified_timeline(sample)
    assert [i.tid for i in items] == ["ABCDEF0123456789ABCDEF01"]


def test_parse_empty_and_unrelated_text():
    assert parse_unified_timeline("") == []
    assert parse_unified_timeline("<html>gateway error</html>") == []
    assert parse_unified_timeline('{"code":0,"data":{}}') == []


def test_feed_discovery_is_lightweight_index():
    """FeedDiscovery 是发现层轻量索引:仅 5 字段,与充实层 FeedItem(完整实体)分层。"""
    d = FeedDiscovery(tid="aa", uin="1", nickname="n", abstime="100", appid=311)
    assert (d.tid, d.uin, d.nickname, d.abstime, d.appid) == ("aa", "1", "n", "100", 311)
