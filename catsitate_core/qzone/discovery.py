"""QQ 空间统一时间线解析器(feeds3_html_more 响应 → FeedDiscovery 轻量索引)。

实证结构(2026-08-31 生产验证):响应外层是 JSON(`{"code":0,"data":{...}}`),
内层 data.main 为 JS 对象字面量(单引号字符串、无引号键名,非严格 JSON,
不能走 json.loads)。每个动态条目按序出现一组连续字段:

    key:'{十六进制tid}' appid:{int} abstime:{int} opuin:'{uin}' nickname:'{name}'

解析策略(鲁棒正则,不依赖 bs4/json5):以 `key:'非空十六进制'` 定位条目起点,
向后 WINDOW_CHARS 字符窗口内提取其余字段;缺任一必需字段的条目跳过
(容错,不阻断后续条目)。appid 不过滤——说说(appid=311)筛选由调用方决定
(源B搭便车等场景需保留全量条目)。

分层定位:FeedDiscovery 是发现层轻量索引(本模块产物);完整实体(正文/图片/
评论)由充实层 get_user_feeds 的 FeedItem 承载,两者以 tid 对齐。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# 条目字段在 key 之后出现的最大跨度(实证单条目约 150 字符,500 足够宽松)
WINDOW_CHARS = 500

_KEY_RE = re.compile(r"key:'([0-9a-f]+)'")  # 非空小写十六进制 tid(实证形态)
_ABSTIME_RE = re.compile(r"abstime:(\d+)")
# opuin 生产实证为单引号字符串,简化样本为裸数字——两种形态都收,统一转 str
_OPUIN_RE = re.compile(r"opuin:'?(\d+)'?")
# 昵称内可含 JS 转义(\' \"):先按「转义对或非引号非反斜杠」原始捕获,解码在使用处
_NICKNAME_RE = re.compile(r"nickname:'((?:\\.|[^'\\])*)'")
_APPID_RE = re.compile(r"appid:(\d+)")


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
        window = text[match.end() : match.end() + WINDOW_CHARS]
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
