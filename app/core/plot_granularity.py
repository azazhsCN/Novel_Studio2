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

# 匹配 “### 第12章：标题” / “## 第12章 xxx”
_DETAIL_HEADING_RE = re.compile(r'^#{1,4}\s*第\s*(\d+)\s*章', re.MULTILINE)


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
    """把“### 第12章：xxx”形式的响应拆成 {章节号: 该章概述}"""
    if not text:
        return {}
    wanted = {int(n) for n in chapter_numbers}
    hits = list(_DETAIL_HEADING_RE.finditer(text))
    result: dict[int, str] = {}
    for i, match in enumerate(hits):
        num = int(match.group(1))
        if num not in wanted or num in result:
            continue
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        body = text[match.end():end].strip()
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
