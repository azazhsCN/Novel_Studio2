"""章节规划模块"""
import json
import uuid
from fastapi import HTTPException
from app.core.api_client import api_client
from app.core.prompt_builder import build_planning_prompt
from app.models.novel import NovelProject
from app.models.chapter import ChapterBatchPlan, ChapterPlanItem


async def generate_plan(project: NovelProject, last_chapter_content: str,
                        start_chapter: int, num_chapters: int,
                        direction_hint: str = "") -> ChapterBatchPlan:
    """调用AI生成章节规划"""
    prompt = build_planning_prompt(
        project, last_chapter_content, start_chapter, num_chapters, direction_hint
    )

    response = await api_client.chat(prompt)
    data = _extract_json(response)

    # 解析失败或章节列表为空时绝不保存空规划，直接报错
    if data.get("error"):
        raise HTTPException(502, f"规划生成失败：AI响应无法解析，请重试。原因: {data.get('error')}")
    if not data.get("chapters"):
        raise HTTPException(502, "规划生成失败：AI响应中缺少章节列表，请重试")

    # 构建规划对象
    chapters = []
    for ch in data.get("chapters", []):
        chapters.append(ChapterPlanItem(
            chapter_number=ch.get("chapter_number", 0),
            title=ch.get("title", ""),
            chapter_type=ch.get("chapter_type", "normal"),
            word_count_target=ch.get("word_count_target", project.base_word_count),
            time=ch.get("time", ""),
            scene=ch.get("scene", ""),
            core_plot=ch.get("core_plot", []),
            prompt=ch.get("prompt", ""),
        ))

    plan = ChapterBatchPlan(
        id=str(uuid.uuid4())[:8],
        novel_id=project.id,
        start_chapter=start_chapter,
        end_chapter=start_chapter + num_chapters - 1,
        direction_hint=direction_hint,
        chapters=chapters,
    )

    plan.save()

    # 更新项目统计
    project.update_stats()
    project.save()

    return plan


def _extract_json(text: str) -> dict:
    """从AI响应中提取JSON块"""
    import re
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    pattern = r'```(?:json)?\s*\n?(.*?)\n?```'
    matches = re.findall(pattern, text, re.DOTALL)
    for match in matches:
        try:
            return json.loads(match)
        except json.JSONDecodeError:
            continue

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    return {"error": "无法解析AI响应", "raw": text}
