"""正文写作模块

除了拼提示词与落盘，这里还负责**篇幅兜底**：模型单次输出常常明显短于目标字数
（实测某章目标 19500 字只写了 7063 字，且是自然收尾而非被截断），因此当生成结果
低于目标的一定比例（或上游因输出上限截断）时，自动携带已写结尾继续写，直到达标
或达到续写轮数上限。开关与阈值见 config.yaml 的 `write` 段（config.get_write_options）。
"""
import logging
from app.core.api_client import api_client
from app.core.config import get_write_options
from app.core.prompt_builder import (
    build_writing_prompt, build_revision_prompt, build_write_continuation_prompt,
)
from app.models.novel import NovelProject
from app.models.chapter import Chapter
from app.models.resource import ResourceTracker

logger = logging.getLogger(__name__)


def _target_words(project: NovelProject, chapter_plan: dict) -> int:
    """本章目标字数：优先规划里的 word_count_target，否则用项目基准字数"""
    try:
        target = int(chapter_plan.get("word_count_target") or 0)
    except (TypeError, ValueError):
        target = 0
    return target if target > 0 else int(project.base_word_count)


def _revision_target(chapter_plan: dict, original_content: str) -> int:
    """改稿的目标字数：不低于原文长度；有规划目标时同时满足规划目标"""
    try:
        plan_target = int(chapter_plan.get("word_count_target") or 0)
    except (TypeError, ValueError):
        plan_target = 0
    return max(len(original_content), plan_target)


def _should_continue(content: str, target: int, passes: int, opts: dict,
                     finish_reason: str = "") -> bool:
    """是否需要续写：篇幅不足（或上游因长度截断），且未超过轮数上限"""
    if not opts["auto_continue"] or target < opts["min_target_to_enforce"]:
        return False
    if passes > opts["max_continuations"]:
        return False
    if finish_reason == "length":
        return True
    return len(content) < target * opts["min_ratio"]


def _split_overlap(content: str, chunk: str, max_overlap: int = 200,
                   min_overlap: int = 10) -> str:
    """去掉续写片段开头与已写结尾重复的部分，返回真正应新增的文本

    min_overlap 取 10：模型续写时常原样复述上一句（十几到几十字），低于 10 字的重合
    多为巧合（如标点），不做去重。
    """
    chunk = (chunk or "").lstrip()
    if not chunk:
        return ""
    limit = min(max_overlap, len(content), len(chunk))
    for size in range(limit, min_overlap - 1, -1):
        if content.endswith(chunk[:size]):
            return chunk[size:].lstrip()
    return chunk


def _merge_continuation(content: str, chunk: str, max_overlap: int = 200) -> str:
    """把一轮续写合并进已有正文（非流式路径用）"""
    addition = _split_overlap(content, chunk, max_overlap)
    if not addition:
        return content
    separator = "" if content.endswith("\n") else "\n\n"
    return f"{content}{separator}{addition}"


async def _stream_continuation(content: str, prompt: str, stats: dict, head_limit: int = 200):
    """流式取一轮续写，按"去掉与结尾重复部分"后的文本逐段 yield

    前 head_limit 个字符先缓冲用于去重，之后边生成边下发——这样界面上看到的文本
    与最终落盘内容严格一致（不会出现"界面重复、文件已去重"的不一致）。
    """
    buffer: list[str] = []
    buffered_len = 0
    flushed = False
    separator = "" if content.endswith("\n") else "\n\n"
    separator_sent = False

    def _with_separator(text: str) -> str:
        nonlocal separator_sent
        if text and separator and not separator_sent:
            separator_sent = True
            return separator + text
        return text

    async for token in api_client.chat_stream(prompt, stats=stats):
        if flushed:
            yield token
            continue
        buffer.append(token)
        buffered_len += len(token)
        if buffered_len >= head_limit:
            flushed = True
            text = _split_overlap(content, "".join(buffer), head_limit)
            if text:
                yield _with_separator(text)

    if not flushed:
        text = _split_overlap(content, "".join(buffer), head_limit)
        if text:
            yield _with_separator(text)


def _log_result(label: str, target: int, content: str, passes: int, finish: str) -> None:
    ratio = (len(content) / target * 100) if target else 0
    logger.info(
        f"{label}：{len(content)}/{target} 字（{ratio:.0f}%），生成{passes}轮，"
        f"finish_reason={finish or '未返回'}"
    )


async def write_chapter(project: NovelProject, chapter_plan: dict,
                        last_chapter_content: str,
                        style_sample: str = "") -> Chapter:
    """调用AI生成章节正文（不足目标字数时自动续写补齐）"""
    tracker = ResourceTracker.load(project.id)
    resource_summary = tracker.get_summary_for_prompt()
    target = _target_words(project, chapter_plan)
    opts = get_write_options()

    prompt = build_writing_prompt(
        project, chapter_plan, last_chapter_content, style_sample, resource_summary
    )
    stats: dict = {}
    content = (await api_client.chat(prompt, stats=stats)).strip()
    passes = 1
    finish = stats.get("finish_reason", "")

    while _should_continue(content, target, passes, opts, finish):
        remaining = max(target - len(content), 0)
        cont_prompt = build_write_continuation_prompt(
            project, chapter_plan, content, remaining, opts["continuation_tail_chars"]
        )
        stats = {}
        content = _merge_continuation(content, await api_client.chat(cont_prompt, stats=stats))
        finish = stats.get("finish_reason", "")
        passes += 1

    _log_result(f"第{chapter_plan.get('chapter_number', '?')}章生成", target, content, passes, finish)

    chapter = Chapter(
        novel_id=project.id,
        chapter_number=chapter_plan.get("chapter_number", 0),
        title=chapter_plan.get("title", ""),
        chapter_type=chapter_plan.get("chapter_type", "normal"),
        content=content,
        is_finalized=False,
    )

    chapter.save()

    # 更新项目统计
    project.update_stats()
    project.save()

    return chapter


async def revise_chapter(project: NovelProject, chapter: Chapter,
                         revision意见: str, chapter_plan: dict,
                         style_sample: str = "") -> Chapter:
    """根据修改意见重写章节（不足目标篇幅时自动续写补齐）"""
    tracker = ResourceTracker.load(project.id)
    resource_summary = tracker.get_summary_for_prompt()
    original = chapter.content
    target = _revision_target(chapter_plan, original)
    opts = get_write_options()

    prompt = build_revision_prompt(
        project, original, revision意见, chapter_plan, style_sample, resource_summary
    )
    stats: dict = {}
    content = (await api_client.chat(prompt, stats=stats)).strip()
    passes = 1
    finish = stats.get("finish_reason", "")

    while _should_continue(content, target, passes, opts, finish):
        remaining = max(target - len(content), 0)
        cont_prompt = build_write_continuation_prompt(
            project, chapter_plan, content, remaining, opts["continuation_tail_chars"]
        )
        stats = {}
        content = _merge_continuation(content, await api_client.chat(cont_prompt, stats=stats))
        finish = stats.get("finish_reason", "")
        passes += 1

    _log_result(f"第{chapter.chapter_number}章改稿", target, content, passes, finish)

    chapter.content = content
    chapter.is_finalized = False
    chapter.audit_passed = False
    chapter.save()

    return chapter


async def get_last_chapter_content(project: NovelProject) -> str:
    """获取上一章的内容，用于衔接"""
    chapters = Chapter.list_for_novel(project.id, load_content=True)
    if not chapters:
        return "（暂无前文，这是小说的开始）"
    last = chapters[-1]
    return f"【第{last.chapter_number}章 {last.title}】\n{last.content}"


def load_style_sample(novel_id: str, sample_name: str = None) -> str:
    """加载文风样本"""
    from app.core.config import get_novel_subdirs
    dirs = get_novel_subdirs(novel_id)
    samples_dir = dirs["style_samples"]

    if sample_name:
        path = samples_dir / sample_name
        if path.exists():
            return path.read_text(encoding="utf-8")

    # 如果没指定，加载第一个样本
    samples = list(samples_dir.glob("*.txt"))
    if samples:
        return samples[0].read_text(encoding="utf-8")

    return ""


async def _generate_stream(project: NovelProject, prompt: str, chapter_plan: dict,
                           original_content: str, target: int, label: str):
    """共享的流式生成 + 自动续写流程

    yield 的事件：`token`（正文增量）与 `done`（含 target_words / final_words / passes）
    """
    opts = get_write_options()
    full_content: list[str] = []
    stats: dict = {}
    async for token in api_client.chat_stream(prompt, stats=stats):
        full_content.append(token)
        yield {"type": "token", "content": token}

    content = "".join(full_content).strip()
    passes = 1
    finish = stats.get("finish_reason", "")

    while _should_continue(content, target, passes, opts, finish):
        remaining = max(target - len(content), 0)
        cont_prompt = build_write_continuation_prompt(
            project, chapter_plan, content, remaining, opts["continuation_tail_chars"]
        )
        stats = {}
        addition: list[str] = []
        async for piece in _stream_continuation(content, cont_prompt, stats):
            addition.append(piece)
            yield {"type": "token", "content": piece}
        if addition:
            content = f"{content}{''.join(addition)}"
        finish = stats.get("finish_reason", "")
        passes += 1

    _log_result(label, target, content, passes, finish)
    yield {
        "type": "_final",
        "content": content,
        "target_words": target,
        "passes": passes,
    }


async def write_chapter_stream(project: NovelProject, chapter_plan: dict,
                               last_chapter_content: str,
                               style_sample: str = ""):
    """流式生成章节正文；不足目标字数时自动续写，token 事件连续下发"""
    tracker = ResourceTracker.load(project.id)
    resource_summary = tracker.get_summary_for_prompt()
    target = _target_words(project, chapter_plan)
    prompt = build_writing_prompt(
        project, chapter_plan, last_chapter_content, style_sample, resource_summary
    )

    final = {}
    async for event in _generate_stream(
        project, prompt, chapter_plan, "", target,
        f"第{chapter_plan.get('chapter_number', '?')}章生成(流式)",
    ):
        if event["type"] == "_final":
            final = event
            continue
        yield event

    chapter = Chapter(
        novel_id=project.id,
        chapter_number=chapter_plan.get("chapter_number", 0),
        title=chapter_plan.get("title", ""),
        chapter_type=chapter_plan.get("chapter_type", "normal"),
        content=final["content"],
        is_finalized=False,
    )
    chapter.save()
    project.update_stats()
    project.save()

    yield {
        "type": "done",
        "chapter": chapter.model_dump(),
        "target_words": final["target_words"],
        "final_words": len(final["content"]),
        "passes": final["passes"],
    }


async def revise_chapter_stream(project: NovelProject, chapter: Chapter,
                                revision意见: str, chapter_plan: dict,
                                style_sample: str = ""):
    """流式修改章节；不足目标篇幅时自动续写"""
    tracker = ResourceTracker.load(project.id)
    resource_summary = tracker.get_summary_for_prompt()
    original = chapter.content
    target = _revision_target(chapter_plan, original)
    prompt = build_revision_prompt(
        project, original, revision意见, chapter_plan, style_sample, resource_summary
    )

    final = {}
    async for event in _generate_stream(
        project, prompt, chapter_plan, original, target,
        f"第{chapter.chapter_number}章改稿(流式)",
    ):
        if event["type"] == "_final":
            final = event
            continue
        yield event

    chapter.content = final["content"]
    chapter.is_finalized = False
    chapter.audit_passed = False
    chapter.save()

    yield {
        "type": "done",
        "chapter": chapter.model_dump(),
        "target_words": final["target_words"],
        "final_words": len(final["content"]),
        "passes": final["passes"],
    }
