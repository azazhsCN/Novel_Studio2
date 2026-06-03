"""正文写作模块"""
import os
from pathlib import Path
from app.core.api_client import api_client
from app.core.prompt_builder import build_writing_prompt, build_revision_prompt
from app.models.novel import NovelProject
from app.models.chapter import Chapter
from app.models.resource import ResourceTracker


async def write_chapter(project: NovelProject, chapter_plan: dict,
                        last_chapter_content: str,
                        style_sample: str = "") -> Chapter:
    """调用AI生成章节正文"""
    # 获取资源追踪摘要
    tracker = ResourceTracker.load(project.id)
    resource_summary = tracker.get_summary_for_prompt()

    prompt = build_writing_prompt(
        project, chapter_plan, last_chapter_content, style_sample, resource_summary
    )

    response = await api_client.chat(prompt)

    chapter = Chapter(
        novel_id=project.id,
        chapter_number=chapter_plan.get("chapter_number", 0),
        title=chapter_plan.get("title", ""),
        chapter_type=chapter_plan.get("chapter_type", "normal"),
        content=response.strip(),
        is_finalized=False,
    )

    chapter.save()

    # 更新项目统计
    project.update_stats()
    project.save()

    return chapter


async def revise_chapter(project: NovelProject, chapter: Chapter,
                         revision意见: str, chapter_plan: dict) -> Chapter:
    """根据修改意见重写章节"""
    prompt = build_revision_prompt(project, chapter.content, revision意见, chapter_plan)
    response = await api_client.chat(prompt)

    chapter.content = response.strip()
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


async def write_chapter_stream(project: NovelProject, chapter_plan: dict,
                                last_chapter_content: str,
                                style_sample: str = ""):
    """流式生成章节正文，yield每个token事件"""
    tracker = ResourceTracker.load(project.id)
    resource_summary = tracker.get_summary_for_prompt()

    prompt = build_writing_prompt(
        project, chapter_plan, last_chapter_content, style_sample, resource_summary
    )

    full_content = []
    async for token in api_client.chat_stream(prompt):
        full_content.append(token)
        yield {"type": "token", "content": token}

    content = "".join(full_content).strip()
    chapter = Chapter(
        novel_id=project.id,
        chapter_number=chapter_plan.get("chapter_number", 0),
        title=chapter_plan.get("title", ""),
        chapter_type=chapter_plan.get("chapter_type", "normal"),
        content=content,
        is_finalized=False,
    )
    chapter.save()
    project.update_stats()
    project.save()

    yield {"type": "done", "chapter": chapter.model_dump()}


async def revise_chapter_stream(project: NovelProject, chapter: Chapter,
                                 revision意见: str, chapter_plan: dict):
    """流式修改章节，yield每个token事件"""
    prompt = build_revision_prompt(project, chapter.content, revision意见, chapter_plan)

    full_content = []
    async for token in api_client.chat_stream(prompt):
        full_content.append(token)
        yield {"type": "token", "content": token}

    content = "".join(full_content).strip()
    chapter.content = content
    chapter.is_finalized = False
    chapter.audit_passed = False
    chapter.save()

    yield {"type": "done", "chapter": chapter.model_dump()}
