"""QQ 空间统一时间线解析器(feeds3_html_more 响应 → FeedDiscovery 轻量索引)。

实证结构(2026-08-31 生产验证):响应外层是 JSON(`{"code":0,"data":{...}}`),
内层 data.main 为 JS 对象字面量(单引号字符串、无引号键名,非严格 JSON,
不能走 json.loads)。每个动态条目按序出现一组连续字段:

    key:'{十六进制tid}' appid:{int} abstime:{int} opuin:'{uin}' nickname:'{name}'

解析策略(鲁棒正则,不依赖 bs4/json5):以 `key:'非空十六进制'` 定位条目起点,
向后至下一 key 锚点(至多 WINDOW_CHARS 字符)的窗口内提取其余字段;缺任一必需
字段的条目跳过(容错,不阻断后续条目)。appid 不过滤——说说(appid=311)筛选
由调用方决定(源B搭便车等场景需保留全量条目)。

分层定位:FeedDiscovery 是发现层轻量索引(本模块产物);完整实体(正文/图片/
评论)由充实层 get_user_feeds 的 FeedItem 承载,两者以 tid 对齐。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from catsitate_core.qzone.wire import LikeEvent

logger = logging.getLogger(__name__)

# 条目字段在 key 之后出现的最大跨度兜底(实证单条目约 150 字符);有下一
# key 锚点时窗口以锚点为上界,优先于本值(防跨条目借用,见 parse_unified_timeline)
WINDOW_CHARS = 500

_KEY_RE = re.compile(r"key:'([0-9a-fA-F]+)'")  # 非空十六进制 tid(实证小写,容忍大写)
# 联调实证:JS 对象的数值字段(abstime/appid)带单引号——所有数值正则统一容忍 '?..'? 形态
_ABSTIME_RE = re.compile(r"abstime:'?(\d+)'?")
_APPID_RE = re.compile(r"appid:'?(\d+)'?")
# 窗口边界条目(实机实证 2026-09-03)缺 appid 但带同值 appiconid——回退解析,
# 否则该条目被整条丢弃(系统性丢窗口最旧一条)
_APPICONID_RE = re.compile(r"appiconid:'?(\d+)'?")
# opuin 生产实证为单引号字符串,简化样本为裸数字——两种形态都收,统一转 str
_OPUIN_RE = re.compile(r"opuin:'?(\d+)'?")
# 昵称内可含 JS 转义(\' \"):先按「转义对或非引号非反斜杠」原始捕获,解码在使用处
_NICKNAME_RE = re.compile(r"nickname:'((?:\\.|[^'\\])*)'")


@dataclass
class FeedDiscovery:
    """发现层条目(统一时间线轻量索引,非完整实体)。

    tid=动态十六进制标识(与充实层 FeedItem.tid 对齐);uin/nickname=作者;
    abstime=发布时间戳(秒,字符串形态与 FeedItem 一致);appid=动态类型
    (311=说说,其余类型去留由调用方决定)。
    """

    tid: str
    uin: str
    nickname: str
    abstime: str
    appid: int


def _decode_js_escapes(text: str) -> str:
    """解码 JS 单引号字符串转义:\\' → '、\\" → "、\\\\ → \\。

    其余转义(\n 等)保持原样——昵称中的控制字符罕见,保守不扩散解码面。
    """

    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n and text[i + 1] in ("'", '"', "\\"):
            out.append(text[i + 1])
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def parse_unified_timeline(text: str) -> list[FeedDiscovery]:
    """从 feeds3_html_more 响应文本提取全部动态条目。

    容错:key 不构成十六进制定位点的条目不进入解析;定位后缺任一必需
    字段(abstime/opuin/nickname/appid)的条目跳过并记 debug 日志。
    响应无任何条目时返回空列表——「无动态」与「响应畸形」由调用方结合
    外层 code(见 client._fetch_unified)区分,解析层不做兜底决策。
    """

    text = str(text or "")
    out: list[FeedDiscovery] = []
    for match in _KEY_RE.finditer(text):
        # 窗口上界=下一 key 锚点位置(审查修复:畸形中间条目不得越过锚点向
        # 邻条目借用 abstime/appid 等同名字段误组装);无后续锚点回退固定跨度
        next_key = text.find("key:'", match.end())
        window_end = next_key if next_key != -1 else match.end() + WINDOW_CHARS
        window = text[match.end() : window_end]
        abstime = _ABSTIME_RE.search(window)
        opuin = _OPUIN_RE.search(window)
        nickname = _NICKNAME_RE.search(window)
        appid = _APPID_RE.search(window) or _APPICONID_RE.search(window)
        if not (abstime and opuin and nickname and appid):
            logger.debug("统一时间线条目缺必需字段,跳过(tid=%s)", match.group(1))
            continue
        out.append(
            FeedDiscovery(
                tid=match.group(1),
                uin=opuin.group(1),
                nickname=_decode_js_escapes(nickname.group(1)),
                abstime=abstime.group(1),
                appid=int(appid.group(1)),
            )
        )
    return out


# 「与我相关」流(scope=1)赞事件锚点(源C,Task 10):事件块起点 data-key=
# "{liker}_{owner}_{hash}"(同一标签内紧邻 data-tid=目标说说),块体至下一
# data-key 锚点/文本末尾;块内 data-uin=点赞者昵称锚(与统一时间线的 JS 对象
# 字面量不同,该流是 HTML 片段,锚为属性形态)
# 「与我相关」赞事件锚点(实机响应校准):外层 JSON、内层 JS 对象,HTML 片段
# 以 \xHH/字面 \t 转义存储,锚点匹配前先归一。条目内真实顺序为:
#   user-info(昵称锚 f-name q_namecard + link="nameCard_{liker}"、动作 span
#   「赞了我的说说」、相对时间 span 今天/昨天/前天/N月N日 HH:MM)
#   → 外层 li 的 data-key="{liker}_{owner}_{hash}" → 内容区 data-fkey 同键 + data-tid。
# 故条目锚=data-key(仅真实三元组形态匹配,推广位 LikeTipsFeeds 天然排除):
# 头部信息在 data-key 之前的窗口内取,说说 tid 在其后最近的同键 fkey 锚取。
LIKE_ENTRY_KEY_RE = re.compile(
    r'data-key="(?P<liker>\d+)_(?P<owner>\d+)_(?P<hash>[0-9a-fA-F]+)"'
)
LIKE_FKEY_RE = re.compile(
    r'data-fkey="(?P<liker>\d+)_(?P<owner>\d+)_(?P<hash>[0-9a-fA-F]+)"[^>]*?'
    r'data-tid="(?P<tid>[0-9a-zA-Z]+)"'
)
LIKE_NICK_RE = re.compile(
    r'class="f-name q_namecard\s*"\s+link="nameCard_(?P<uin>\d+)"[^>]*>(?P<nick>[^<]{1,24})<'
)
LIKE_TIME_RE = re.compile(
    r'state\s*"\s*>[\t ]*(?P<day>今天|昨天|前天|\d{1,2}月\d{1,2}日)(?:[\t ]+(?P<hm>\d{1,2}:\d{2}))?'
)
LIKE_HEAD_WINDOW = 6000  # 头部(昵称/动作/时间)距 data-key 锚的最大前向跨度
_JS_ESCAPES = (("\\x22", '"'), ("\\x27", "'"), ("\\x3C", "<"), ("\\x3E", ">"), ("\\x26", "&"), ("\\/", "/"), ("\\t", "\t"), ("\\n", "\n"))


def _normalize_js_escapes(text: str) -> str:
    """归一内层 JS 对象字符串中的 \\xHH 转义(HTML 片段的引号/尖括号以该形态存储)。"""
    for src, dst in _JS_ESCAPES:
        text = text.replace(src, dst)
    return text


def _relative_time_to_epoch(day: str, hm: str, now: datetime) -> int:
    """相对时间(今天/昨天/前天/N月N日 + HH:MM)折算 epoch 秒,天级精度。

    跨年边界按「折算结果晚于当前则回退一年」处理;HH:MM 缺失按 00:00。
    审查修复(2026-09-03):非闰年「2月29日」使 N月N日 折算构造出历史上不
    存在的日期,replace 抛 ValueError 且无捕获——异常沿 get_like_events
    上抛会中止通知扫描整轮。终审 M2 补全防护半套:时分构造的 replace 原在
    try 之外,LIKE_TIME_RE 容忍「99:99」时同样抛 ValueError 无人捕获。
    现整个折算(时分构造+日期构造+跨年回退)统一纳入一处 try:告警后
    返回 0(与「create_time 缺失不编造时间」口径一致;调用侧 like_epoch=0
    与 comment_time_prefix 的 <=0 分支均容忍,时间前缀省略、新鲜度不误截断)。
    """
    try:
        hour, minute = (int(x) for x in hm.split(":")) if hm else (0, 0)
        base = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if day == "昨天":
            base -= timedelta(days=1)
        elif day == "前天":
            base -= timedelta(days=2)
        elif day != "今天":
            month, mday = (int(x) for x in re.match(r"(\d{1,2})月(\d{1,2})日", day).groups())
            base = base.replace(month=month, day=mday)
            if base > now:
                base = base.replace(year=base.year - 1)
    except ValueError:
        # 折算构造出非法时间(非闰年 2月29日 / 越界时分 99:99):不编造时间,告警后置 0
        logger.warning("赞事件相对时间折算遇非法时间(day=%s, hm=%s),create_time 置 0", day, hm)
        return 0
    return int(base.timestamp())


def parse_like_events(text: str) -> list[LikeEvent]:
    """解析「与我相关」流(scope=1)中的赞事件。

    条目锚=data-key(真实三元组):头部(昵称/动作/时间)取 data-key 之前至
    上一条目锚的窗口,说说 tid 取其后最近的同键 data-fkey 锚。头部窗口不含
    「赞了我的说说」的条目(评论类)不产出;无昵称锚的异常形态回退键前段,
    不静默丢赞事件。
    """

    norm = _normalize_js_escapes(text)
    events: list[LikeEvent] = []
    seen: set[tuple[str, str]] = set()
    keys = list(LIKE_ENTRY_KEY_RE.finditer(norm))
    fkeys = list(LIKE_FKEY_RE.finditer(norm))
    for idx, m in enumerate(keys):
        head_start = keys[idx - 1].end() if idx > 0 else max(0, m.start() - LIKE_HEAD_WINDOW)
        head = norm[head_start:m.start()]
        if "赞了我的说说" not in head:
            continue  # 该条目非赞事件(评论/回复等)
        # 说说 tid:其后首个同键 fkey 锚
        tid = ""
        for f in fkeys:
            if f.start() > m.end() and f.group("hash") == m.group("hash") and f.group("owner") == m.group("owner"):
                tid = f.group("tid")
                break
        if not tid:
            continue  # 无目标说说锚,无法定位(不产出,观测线由调用方统计)
        create_time = ""
        t_matches = list(LIKE_TIME_RE.finditer(head))
        if t_matches:
            t = t_matches[-1]
            create_time = str(_relative_time_to_epoch(t.group("day"), t.group("hm") or "", datetime.now()))
        nicks = list(LIKE_NICK_RE.finditer(head))
        if not nicks:
            # 无昵称锚的异常形态:点赞人回退键前段,昵称回退号码本身
            events.append(LikeEvent(
                like_key=f"{m.group('liker')}_{m.group('owner')}_{m.group('hash')}",
                liker_uin=m.group("liker"), liker_nickname=m.group("liker"),
                owner_uin=m.group("owner"), target_tid=tid,
                create_time=create_time,
            ))
            continue
        for nick in nicks:
            liker_uin = nick.group("uin")
            if (liker_uin, tid) in seen:
                continue
            seen.add((liker_uin, tid))
            events.append(LikeEvent(
                like_key=f"{liker_uin}_{m.group('owner')}_{m.group('hash')}",
                liker_uin=liker_uin, liker_nickname=nick.group("nick").strip() or liker_uin,
                owner_uin=m.group("owner"), target_tid=tid,
                create_time=create_time,
            ))
    return events
