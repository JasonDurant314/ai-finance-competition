"""quote 回读校验 —— 抑制幻觉的**硬闸门**。

规则：LLM 声称的原文片段必须能在页面文本中找到，否则拒收该字段。

这是本方案对抗多模态模型幻觉的核心手段：裁判模型被要求输出
「页面上看得见的连续字符串」，代码立刻回原文校验。编造的数字
几乎不可能同时编对位置和上下文，因此会被这一关挡掉。

三级判定：
    EXACT    完全匹配（归一化空白后）           -> 通过
    FUZZY    相似度 >= 阈值（OCR 噪声容差）      -> 通过但降置信
    MISS     相似度 < 阈值                      -> **拒收**
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum

from agent.validate.numeric import _to_halfwidth

DEFAULT_FUZZY_THRESHOLD = 0.86


class QuoteMatch(str, Enum):
    EXACT = "exact"
    FUZZY = "fuzzy"
    MISS = "miss"


@dataclass
class QuoteCheckResult:
    status: QuoteMatch
    similarity: float
    matched_text: str = ""
    offset: int = -1
    note: str = ""

    @property
    def passed(self) -> bool:
        return self.status is not QuoteMatch.MISS


# 需要归一化的字符：空白、各类引号、破折号、全角标点
_WHITESPACE = " \t\r\n\u3000\u00a0"
_PUNCT_MAP = {
    "“": '"', "”": '"', "‘": "'", "’": "'",
    "（": "(", "）": ")", "，": ",", "。": ".", "；": ";", "：": ":",
    "－": "-", "—": "-", "–": "-", "．": ".",
}


def normalize_text(s: str) -> str:
    """归一化文本用于比对：全角转半角、去空白、统一标点。"""
    s = _to_halfwidth(s)
    for src, dst in _PUNCT_MAP.items():
        s = s.replace(src, dst)
    for ws in _WHITESPACE:
        s = s.replace(ws, "")
    return s


def similarity(a: str, b: str) -> float:
    """归一化后的序列相似度 [0,1]。"""
    na, nb = normalize_text(a), normalize_text(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def check_quote(
    quote: str,
    page_text: str,
    *,
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
    min_quote_len: int = 2,
) -> QuoteCheckResult:
    """校验 quote 是否能在 page_text 中找到。

    Args:
        quote: LLM 声称的原文片段。
        page_text: 该页的**全部**可用文本（文本层 + OCR 的并集，提高召回）。
        fuzzy_threshold: OCR 噪声容忍阈值。

    Returns:
        ``QuoteCheckResult``。``status == MISS`` 时调用方必须拒收该字段。
    """
    if not quote or len(normalize_text(quote)) < min_quote_len:
        return QuoteCheckResult(
            QuoteMatch.MISS, 0.0, note=f"quote 过短（<{min_quote_len} 个有效字符），不足以定位"
        )

    nq = normalize_text(quote)
    np_ = normalize_text(page_text)

    if not np_:
        return QuoteCheckResult(QuoteMatch.MISS, 0.0, note="页面无可用文本，无法回读")

    # 一级：精确子串
    idx = np_.find(nq)
    if idx >= 0:
        return QuoteCheckResult(
            QuoteMatch.EXACT, 1.0, matched_text=quote, offset=idx, note="原文精确匹配"
        )

    # 二级：滑窗模糊匹配
    best = 0.0
    best_off = -1
    win = len(nq)
    # 窗口长度在 ±20% 内滑动，容忍 OCR 增删字符
    for delta in (0, 1, -1, 2, -2, 3, -3, 5, -5, max(1, win // 5), -max(1, win // 5)):
        w = win + delta
        if w < min_quote_len or w > len(np_):
            continue
        step = 1 if len(np_) < 4000 else 2
        for i in range(0, len(np_) - w + 1, step):
            cand = np_[i : i + w]
            # 快速预筛：首字符必须相同，否则跳过
            if cand[0] != nq[0] and cand[-1] != nq[-1]:
                continue
            r = SequenceMatcher(None, nq, cand).ratio()
            if r > best:
                best = r
                best_off = i
                if best >= 0.99:
                    break
        if best >= 0.99:
            break

    if best >= fuzzy_threshold:
        return QuoteCheckResult(
            QuoteMatch.FUZZY,
            round(best, 4),
            matched_text=page_text[max(0, best_off) : max(0, best_off) + win],
            offset=best_off,
            note=f"模糊匹配（相似度 {best:.3f}，OCR 噪声容差内）",
        )

    return QuoteCheckResult(
        QuoteMatch.MISS,
        round(best, 4),
        note=f"回读失败：最高相似度仅 {best:.3f} < 阈值 {fuzzy_threshold}，判定为幻觉或定位错误",
    )


def find_quote_span(quote: str, page_text: str) -> tuple[int, int] | None:
    """返回 quote 在页文本中的字符区间，供 Web 端高亮。"""
    res = check_quote(quote, page_text)
    if not res.passed or res.offset < 0:
        return None
    return (res.offset, res.offset + len(normalize_text(quote)))


__all__ = [
    "QuoteMatch",
    "QuoteCheckResult",
    "check_quote",
    "find_quote_span",
    "normalize_text",
    "similarity",
    "DEFAULT_FUZZY_THRESHOLD",
]
