"""剧情概述颗粒度规划（需求 3.2）

规则：
- 0-50 章：每章详细摘要（约 200-300 字/章）
- 50-150 章：早期章节压缩为一段，近期章节保留详细摘要
- 150+ 章：早期压缩为一句话，中期压缩为一段，近期保留详细
- 用户锁定的“关键章节”始终单独成段，永不被压缩

本模块只做纯计算（分段计划、响应拆章），不调用 AI、不读写文件，便于离线验证。
"""
import re
from dataclasses import dataclass, field

_HEADING_NUM = r'[0-9０-９零〇一二三四五六七八九十百千万两]+'
_HEADING_SEP = r'[：:、.．—-]'
# 标准写法：带 # 的 markdown 标题（提示词要求 AI 输出这种）
_DETAIL_HEADING_RE = re.compile(
    rf'^[ \t]{{0,3}}#{{1,6}}[ \t]*\*{{0,2}}[ \t]*第[ \t]*({_HEADING_NUM})[ \t]*章',
    re.MULTILINE)
# 兜底写法：不带 #，但「章」后必须紧跟分隔符或加粗符号。
# 不能放宽到"章 + 任意字符"，否则正文里的「第1章的详细摘要…」会被当成标题（曾踩过）。
_BARE_HEADING_RE = re.compile(
    rf'^[ \t]{{0,3}}\*{{0,2}}[ \t]*第[ \t]*({_HEADING_NUM})[ \t]*章[ \t]*(?:{_HEADING_SEP}|\*)',
    re.MULTILINE)
# 用于剥掉文本开头的一行标题
_LEADING_MD_HEADING_RE = re.compile(
    rf'^[ \t]{{0,3}}#{{1,6}}[ \t]*\*{{0,2}}[ \t]*第[ \t]*({_HEADING_NUM})[ \t]*章[ \t]*{_HEADING_SEP}?')
_LEADING_BARE_HEADING_RE = re.compile(
    rf'^[ \t]{{0,3}}\*{{0,2}}[ \t]*第[ \t]*({_HEADING_NUM})[ \t]*章[ \t]*(?:{_HEADING_SEP}|\*)')

_CN_DIGITS = {'零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
              '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}


def cn_to_int(text: str) -> int | None:
    """把章节号转成 int：支持「12」「１２」「十二」「二十三」「一百零五」，无法解析返回 None"""
    s = (text or "").strip()
    if not s:
        return None
    normalized = s.translate(str.maketrans('０１２３４５６７８９', '0123456789'))
    if normalized.isdigit():
        return int(normalized)
    if any(ch not in _CN_DIGITS and ch not in '十百千' for ch in s):
        return None
    total = 0
    number = 0
    for ch in s:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch == '十':
            total += (number or 1) * 10
            number = 0
        elif ch == '百':
            total += (number or 1) * 100
            number = 0
        elif ch == '千':
            total += (number or 1) * 1000
            number = 0
        else:
            return None
    return total + number


def _heading_spans(text: str) -> list[tuple[int, int, int]]:
    """返回 [(标题起点, 标题终点, 章节号)]，按位置排序并去除重叠"""
    spans: list[tuple[int, int, int]] = []
    for match in _DETAIL_HEADING_RE.finditer(text):
        num = cn_to_int(match.group(1))
        if num is not None:
            spans.append((match.start(), match.end(), num))
    for match in _BARE_HEADING_RE.finditer(text):
        num = cn_to_int(match.group(1))
        if num is None:
            continue
        line_end = text.find('\n', match.start())
        line = text[match.start():line_end if line_end != -1 else len(text)]
        if '。' in line or len(line.strip()) > 40:
            continue  # 长句/叙述句不像标题
        spans.append((match.start(), match.end(), num))
    spans.sort()
    merged: list[tuple[int, int, int]] = []
    for span in spans:
        if merged and span[0] < merged[-1][1]:
            continue  # 与上一个标题重叠（同一标题的两种写法）
        merged.append(span)
    return merged


def strip_leading_heading(text: str) -> str:
    """去掉开头的一行章节标题（保留标题行里分隔符之后的正文）

    解析成功时标题由 `format_overview_segment` 统一补回，避免出现
    「标题被剥掉后概述里看不到章节号」或「中文/阿拉伯数字混排」的问题。
    """
    s = (text or "").lstrip()
    if not s:
        return ""
    match = _LEADING_MD_HEADING_RE.match(s) or _LEADING_BARE_HEADING_RE.match(s)
    if not match:
        return text.strip()
    return s[match.end():].lstrip('：:、.．—-* \t').strip()


def format_overview_segment(start_chapter: int, end_chapter: int, summary: str) -> str:
    """给一个分段加上规范的章节号标题，保证剧情概述里始终能看到章节号"""
    label = (f"第{start_chapter}章" if start_chapter == end_chapter
             else f"第{start_chapter}-{end_chapter}章")
    body = (summary or "").strip()
    if not body:
        return f"### {label}"
    lines = body.split("\n")
    first = lines[0].strip().lstrip('：:、.．—- ').strip()
    rest = "\n".join(lines[1:]).strip()
    # 首行很短且还有正文时，视作本章标题，放到标题行上
    if rest and first and len(first) <= 30:
        return f"### {label}：{first}\n\n{rest}"
    return f"### {label}\n{body}"


@dataclass
class SegmentPlan:
    """一个待生成的剧情概述分段"""
    kind: str                                  # detail（逐章详细）/ compress（压缩为一段）/ one_liner（压缩为一句话）
    start_chapter: int
    end_chapter: int
    chapters: list[int] = field(default_factory=list)


def _split_ranges(numbers: list[int], batch_size: int) -> list[tuple[int, int]]:
    """把升序章节号切成连续区间，每段最多 batch_size 章"""
    ranges: list[tuple[int, int]] = []
    cur: list[int] = []
    for n in numbers:
        if cur and (n != cur[-1] + 1 or len(cur) >= batch_size):
            ranges.append((cur[0], cur[-1]))
            cur = []
        cur.append(n)
    if cur:
        ranges.append((cur[0], cur[-1]))
    return ranges


def build_segment_plan(chapter_numbers, locked_chapters, granularity: dict) -> list[SegmentPlan]:
    """按需求 3.2 计算分段计划（按章节号升序返回）"""
    numbers = sorted({int(n) for n in chapter_numbers})
    if not numbers:
        return []
    locked = {int(n) for n in (locked_chapters or [])}
    total = len(numbers)

    detail_max = int(granularity["detail_max_chapters"])
    mid_max = int(granularity["mid_max_chapters"])
    recent_count = min(int(granularity["recent_detail_count"]), total)
    detail_batch = max(1, int(granularity["detail_batch_size"]))
    compress_block = max(1, int(granularity["compress_block_chapters"]))

    recent = set(numbers[-recent_count:]) if recent_count else set()

    if total <= detail_max:
        # 全部章节都是详细档
        detail_chapters = list(numbers)
        compress_tiers: list[tuple[str, list[int]]] = []
    elif total <= mid_max:
        # 早期压缩为一段，近期（+锁定）保留详细
        detail_chapters = sorted(recent | locked)
        compress_tiers = [("compress", [n for n in numbers if n not in recent])]
    else:
        # 早期压缩为一句话，中期压缩为一段，近期（+锁定）保留详细
        detail_chapters = sorted(recent | locked)
        early = [n for n in numbers if n <= detail_max]
        middle = [n for n in numbers if detail_max < n and n not in recent]
        compress_tiers = [("one_liner", early), ("compress", middle)]

    plan: list[SegmentPlan] = []
    # 锁定章节：永远单独成段，绝不被合并/压缩
    for n in sorted(detail_chapters):
        if n in locked:
            plan.append(SegmentPlan("detail", n, n, [n]))
    rest = [n for n in detail_chapters if n not in locked]
    for start, end in _split_ranges(rest, detail_batch):
        plan.append(SegmentPlan("detail", start, end, [n for n in rest if start <= n <= end]))

    # 压缩档：绕开锁定章节（它们已单独成段）
    for kind, block in compress_tiers:
        remaining = [n for n in block if n not in locked]
        for start, end in _split_ranges(remaining, compress_block):
            plan.append(SegmentPlan(kind, start, end, [n for n in remaining if start <= n <= end]))

    plan.sort(key=lambda p: (p.start_chapter, p.end_chapter))
    return plan


def split_detail_response(text: str, chapter_numbers) -> dict[int, str]:
    """把「### 第12章：xxx」「## 第十二章 xxx」等响应拆成 {章节号: 该章概述}

    章节号支持阿拉伯/全角/中文数字；返回的概述已去掉标题行（标题由
    `format_overview_segment` 统一补回），并清理标题后遗留的分隔符。
    """
    if not text:
        return {}
    wanted = {int(n) for n in chapter_numbers}
    spans = _heading_spans(text)
    result: dict[int, str] = {}
    for i, (_start, end, num) in enumerate(spans):
        if num not in wanted or num in result:
            continue
        stop = spans[i + 1][0] if i + 1 < len(spans) else len(text)
        body = text[end:stop].strip().lstrip('：:、.．—- \t').strip()
        if body:
            result[num] = body
    return result


def describe_plan(plan: list[SegmentPlan]) -> str:
    """分段计划的可读摘要（写日志用）"""
    parts = []
    for seg in plan:
        label = {"detail": "逐章详细", "compress": "压缩成段", "one_liner": "压缩成句"}.get(seg.kind, seg.kind)
        parts.append(f"{label} 第{seg.start_chapter}-{seg.end_chapter}章")
    return "；".join(parts)
