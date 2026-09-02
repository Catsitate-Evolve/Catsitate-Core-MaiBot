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
        appid = _APPID_RE.search(window)
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
# 以 \xHH 转义存储,锚点匹配前先归一。条目锚=内层 data-fkey="{liker}_{owner}_{hash}"
# 与 data-tid 相邻(外层 data-key 为其镜像但不带 tid;推广位 LikeTipsFeeds 无
# 三元组形态,天然不匹配)。点赞者昵称锚=f-name q_namecard + link="nameCard_{liker}";
# 动作与时间在 state span 文本中(相对时间 今天/昨天/前天/N月N日 HH:MM,折算
# epoch,天级精度)。
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
LIKE_ENTRY_WINDOW = 8000  # 单条目内昵称/动作/时间距 fkey 锚的最大跨度
_JS_ESCAPES = (("\\x22", '"'), ("\\x27", "'"), ("\\x3C", "<"), ("\\x3E", ">"), ("\\x26", "&"), ("\\/", "/"), ("\\t", "\t"), ("\\n", "\n"))


def _normalize_js_escapes(text: str) -> str:
    """归一内层 JS 对象字符串中的 \\xHH 转义(HTML 片段的引号/尖括号以该形态存储)。"""
    for src, dst in _JS_ESCAPES:
        text = text.replace(src, dst)
    return text


def _relative_time_to_epoch(day: str, hm: str, now: datetime) -> int:
    """相对时间(今天/昨天/前天/N月N日 + HH:MM)折算 epoch 秒,天级精度。

    跨年边界按「折算结果晚于当前则回退一年」处理;HH:MM 缺失按 00:00。
    """
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
    return int(base.timestamp())


def parse_like_events(text: str) -> list[LikeEvent]:
    """解析「与我相关」流(scope=1)中的赞事件。

    条目窗口=本 fkey 锚至下一 fkey 锚:窗口内须含「赞了我的说说」动作
    (评论类条目不产出),时间取窗口内首个 state span 的相对时间,昵称按
    nameCard 锚逐个提取(同条目多点赞人逐人产出,按 点赞人+说说 去重)。
    无昵称锚的异常形态回退 fkey 前段,不静默丢赞事件。
    """

    norm = _normalize_js_escapes(text)
    events: list[LikeEvent] = []
    seen: set[tuple[str, str]] = set()
    fkeys = list(LIKE_FKEY_RE.finditer(norm))
    for idx, m in enumerate(fkeys):
        end = fkeys[idx + 1].start() if idx + 1 < len(fkeys) else min(m.end() + LIKE_ENTRY_WINDOW, len(norm))
        body = norm[m.end():end]
        if "赞了我的说说" not in body:
            continue  # 该条目非赞事件(评论/回复等)
        create_time = ""
        t_match = LIKE_TIME_RE.search(body)
        if t_match:
            create_time = str(_relative_time_to_epoch(t_match.group("day"), t_match.group("hm") or "", datetime.now()))
        nicks = list(LIKE_NICK_RE.finditer(body))
        if not nicks:
            # 无昵称锚的异常形态:点赞人回退 fkey 前段,昵称回退号码本身
            events.append(LikeEvent(
                like_key=f"{m.group('liker')}_{m.group('owner')}_{m.group('hash')}",
                liker_uin=m.group("liker"), liker_nickname=m.group("liker"),
                owner_uin=m.group("owner"), target_tid=m.group("tid"),
                create_time=create_time,
            ))
            continue
        for nick in nicks:
            liker_uin = nick.group("uin")
            if (liker_uin, m.group("tid")) in seen:
                continue
            seen.add((liker_uin, m.group("tid")))
            events.append(LikeEvent(
                like_key=f"{liker_uin}_{m.group('owner')}_{m.group('hash')}",
                liker_uin=liker_uin, liker_nickname=nick.group("nick").strip() or liker_uin,
                owner_uin=m.group("owner"), target_tid=m.group("tid"),
                create_time=create_time,
            ))
    return events
