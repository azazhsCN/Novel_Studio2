"""章节规划与写作API路由"""
import json as json_module
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Optional
from app.models.novel import NovelProject
from app.models.chapter import ChapterBatchPlan, ChapterPlanItem, Chapter

router = APIRouter(prefix="/api/novels/{novel_id}/chapters", tags=["chapters"])


class PlanGenerateRequest(BaseModel):
    start_chapter: int = Field(ge=1, le=100000)
    num_chapters: int = Field(default=10, ge=1, le=100)
    direction_hint: str = ""


class PlanUpdateRequest(BaseModel):
    chapters: list[dict]


class ChapterPlanEditRequest(BaseModel):
    title: Optional[str] = None
    chapter_type: Optional[str] = None
    word_count_target: Optional[int] = Field(default=None, ge=100, le=100000)
    time: Optional[str] = None
    scene: Optional[str] = None
    core_plot: Optional[list[str]] = None
    prompt: Optional[str] = None


class WriteRequest(BaseModel):
    chapter_number: int
    style_sample: str = ""


class ReviseRequest(BaseModel):
    revision意见: str


class FinalizeRequest(BaseModel):
    finalized: bool = True


# ========== 章节规划 ==========

@router.get("/plans/next-chapter")
async def get_next_chapter(novel_id: str):
    """获取下一个未规划且未写作的章节号"""
    project = NovelProject.load(novel_id)
    if not project:
        raise HTTPException(404, "项目不存在")

    # 收集所有已规划的章节号
    planned_numbers = set()
    plans = ChapterBatchPlan.list_for_novel(novel_id)
    for plan in plans:
        for ch in plan.chapters:
            planned_numbers.add(ch.chapter_number)

    # 收集所有已写章节号
    written_numbers = set()
    chapters = Chapter.list_for_novel(novel_id)
    for ch in chapters:
        written_numbers.add(ch.chapter_number)

    # 找到第一个既未规划也未写作的章节号
    occupied = planned_numbers | written_numbers
    next_ch = 1
    while next_ch in occupied:
        next_ch += 1

    return {
        "next_chapter": next_ch,
        "planned_count": len(planned_numbers),
        "written_count": len(written_numbers),
    }


@router.get("/plans")
async def list_plans(novel_id: str):
    """获取所有章节规划"""
    project = NovelProject.load(novel_id)
    if not project:
        raise HTTPException(404, "项目不存在")

    plans = ChapterBatchPlan.list_for_novel(novel_id)
    return {"plans": [p.model_dump() for p in plans]}


@router.post("/plans/generate")
async def generate_plan(novel_id: str, req: PlanGenerateRequest):
    """调用AI生成章节规划"""
    project = NovelProject.load(novel_id)
    if not project:
        raise HTTPException(404, "项目不存在")

    from app.core.writer import get_last_chapter_content
    from app.core.planner import generate_plan

    last_content = await get_last_chapter_content(project)
    plan = await generate_plan(
        project, last_content, req.start_chapter, req.num_chapters, req.direction_hint
    )

    return {"message": "规划生成成功", "plan": plan.model_dump()}


@router.get("/plans/{plan_id}")
async def get_plan(novel_id: str, plan_id: str):
    """获取单个规划详情"""
    plan = ChapterBatchPlan.load(novel_id, plan_id)
    if not plan:
        raise HTTPException(404, "规划不存在")
    return plan.model_dump()


@router.put("/plans/{plan_id}")
async def update_plan(novel_id: str, plan_id: str, req: PlanUpdateRequest):
    """更新规划（增删改查）"""
    plan = ChapterBatchPlan.load(novel_id, plan_id)
    if not plan:
        raise HTTPException(404, "规划不存在")

    plan.chapters = [ChapterPlanItem(**ch) for ch in req.chapters]
    plan.save()
    return {"message": "规划更新成功", "plan": plan.model_dump()}


@router.delete("/plans/{plan_id}")
async def delete_plan(novel_id: str, plan_id: str):
    """删除规划"""
    plan = ChapterBatchPlan.load(novel_id, plan_id)
    if not plan:
        raise HTTPException(404, "规划不存在")
    plan.delete()

    # 更新统计
    project = NovelProject.load(novel_id)
    if project:
        project.update_stats()
        project.save()

    return {"message": "删除成功"}


@router.delete("/plans/{plan_id}/chapters/{chapter_number}")
async def delete_plan_chapter(novel_id: str, plan_id: str, chapter_number: int):
    """删除规划中的单个章节"""
    plan = ChapterBatchPlan.load(novel_id, plan_id)
    if not plan:
        raise HTTPException(404, "规划不存在")

    original_len = len(plan.chapters)
    plan.chapters = [ch for ch in plan.chapters if ch.chapter_number != chapter_number]
    if len(plan.chapters) == original_len:
        raise HTTPException(404, "章节不存在")

    # 更新起止章节号
    if plan.chapters:
        plan.start_chapter = min(ch.chapter_number for ch in plan.chapters)
        plan.end_chapter = max(ch.chapter_number for ch in plan.chapters)
    plan.save()

    # 更新统计
    project = NovelProject.load(novel_id)
    if project:
        project.update_stats()
        project.save()

    return {"message": f"第{chapter_number}章规划已删除", "plan": plan.model_dump()}


@router.put("/plans/{plan_id}/chapters/{chapter_number}")
async def edit_plan_chapter(novel_id: str, plan_id: str, chapter_number: int,
                            req: ChapterPlanEditRequest):
    """编辑规划中的单个章节"""
    plan = ChapterBatchPlan.load(novel_id, plan_id)
    if not plan:
        raise HTTPException(404, "规划不存在")

    for i, ch in enumerate(plan.chapters):
        if ch.chapter_number == chapter_number:
            if req.title is not None:
                plan.chapters[i].title = req.title
            if req.chapter_type is not None:
                plan.chapters[i].chapter_type = req.chapter_type
                # 自动计算字数
                project = NovelProject.load(novel_id)
                if project:
                    plan.chapters[i].word_count_target = project.get_word_count(req.chapter_type)
            if req.word_count_target is not None:
                plan.chapters[i].word_count_target = req.word_count_target
            if req.time is not None:
                plan.chapters[i].time = req.time
            if req.scene is not None:
                plan.chapters[i].scene = req.scene
            if req.core_plot is not None:
                plan.chapters[i].core_plot = req.core_plot
            if req.prompt is not None:
                plan.chapters[i].prompt = req.prompt
            plan.save()
            return {"message": "章节规划更新成功", "chapter": plan.chapters[i].model_dump()}

    raise HTTPException(404, "章节不存在")


# ========== 正文写作 ==========

@router.get("")
async def list_chapters(novel_id: str):
    """获取所有已写章节"""
    project = NovelProject.load(novel_id)
    if not project:
        raise HTTPException(404, "项目不存在")

    chapters = Chapter.list_for_novel(novel_id, load_content=False)
    return {
        "chapters": [
            {
                "chapter_number": c.chapter_number,
                "title": c.title,
                "chapter_type": c.chapter_type,
                "word_count": c.word_count,
                "is_finalized": c.is_finalized,
                "audit_passed": c.audit_passed,
                "created_at": c.created_at,
            }
            for c in chapters
        ]
    }


@router.post("/write")
async def write_chapter(novel_id: str, req: WriteRequest):
    """调用AI生成章节正文"""
    project = NovelProject.load(novel_id)
    if not project:
        raise HTTPException(404, "项目不存在")

    # 查找对应的规划
    plans = ChapterBatchPlan.list_for_novel(novel_id)
    chapter_plan = None
    for plan in plans:
        for ch in plan.chapters:
            if ch.chapter_number == req.chapter_number:
                chapter_plan = ch.model_dump()
                break
        if chapter_plan:
            break

    if not chapter_plan:
        raise HTTPException(400, f"第{req.chapter_number}章没有对应的规划，请先生成规划")

    from app.core.writer import write_chapter, get_last_chapter_content, load_style_sample

    last_content = await get_last_chapter_content(project)
    style_sample = req.style_sample or load_style_sample(novel_id)

    chapter = await write_chapter(project, chapter_plan, last_content, style_sample)

    return {
        "message": "章节生成成功",
        "chapter": {
            "chapter_number": chapter.chapter_number,
            "title": chapter.title,
            "word_count": chapter.word_count,
            "content": chapter.content,
        },
    }


@router.get("/{chapter_number}")
async def get_chapter(novel_id: str, chapter_number: int):
    """获取已写章节内容"""
    chapter = Chapter.load(novel_id, chapter_number)
    if not chapter:
        raise HTTPException(404, "章节不存在")
    return chapter.model_dump()


class ChapterContentUpdateRequest(BaseModel):
    content: str


@router.put("/{chapter_number}/content")
async def update_chapter_content(novel_id: str, chapter_number: int, req: ChapterContentUpdateRequest):
    """手动更新章节内容"""
    chapter = Chapter.load(novel_id, chapter_number)
    if not chapter:
        raise HTTPException(404, "章节不存在")

    chapter.content = req.content
    chapter.is_finalized = False
    chapter.audit_passed = False
    chapter.save()

    return {"message": "章节内容已更新", "word_count": chapter.word_count}


@router.post("/{chapter_number}/revise")
async def revise_chapter(novel_id: str, chapter_number: int, req: ReviseRequest):
    """根据修改意见重写章节"""
    chapter = Chapter.load(novel_id, chapter_number)
    if not chapter:
        raise HTTPException(404, "章节不存在")

    project = NovelProject.load(novel_id)
    if not project:
        raise HTTPException(404, "项目不存在")

    # 查找规划
    plans = ChapterBatchPlan.list_for_novel(novel_id)
    chapter_plan = {}
    for plan in plans:
        for ch in plan.chapters:
            if ch.chapter_number == chapter_number:
                chapter_plan = ch.model_dump()
                break
        if chapter_plan:
            break

    from app.core.writer import revise_chapter as do_revise
    revised = await do_revise(project, chapter, req.revision意见, chapter_plan)

    return {
        "message": "章节重写成功",
        "chapter": {
            "chapter_number": revised.chapter_number,
            "title": revised.title,
            "word_count": revised.word_count,
            "content": revised.content,
        },
    }


@router.put("/{chapter_number}/finalize")
async def finalize_chapter(novel_id: str, chapter_number: int, req: FinalizeRequest):
    """定稿/取消定稿"""
    chapter = Chapter.load(novel_id, chapter_number)
    if not chapter:
        raise HTTPException(404, "章节不存在")

    chapter.is_finalized = req.finalized
    chapter.save()

    # 更新统计
    project = NovelProject.load(novel_id)
    if project:
        project.update_stats()
        project.save()

    status = "定稿" if req.finalized else "取消定稿"
    return {"message": f"第{chapter_number}章已{status}"}


@router.post("/{chapter_number}/audit")
async def audit_chapter(novel_id: str, chapter_number: int):
    """对章节进行审计"""
    chapter = Chapter.load(novel_id, chapter_number)
    if not chapter:
        raise HTTPException(404, "章节不存在")

    project = NovelProject.load(novel_id)
    if not project:
        raise HTTPException(404, "项目不存在")

    from app.core.auditor import audit_chapter as do_audit
    result = await do_audit(project, chapter)

    return result


# ========== 流式写作 ==========

@router.post("/write/stream")
async def write_chapter_stream(novel_id: str, req: WriteRequest):
    """流式生成章节正文 (SSE)"""
    project = NovelProject.load(novel_id)
    if not project:
        raise HTTPException(404, "项目不存在")

    plans = ChapterBatchPlan.list_for_novel(novel_id)
    chapter_plan = None
    for plan in plans:
        for ch in plan.chapters:
            if ch.chapter_number == req.chapter_number:
                chapter_plan = ch.model_dump()
                break
        if chapter_plan:
            break

    if not chapter_plan:
        raise HTTPException(400, f"第{req.chapter_number}章没有对应的规划")

    from app.core.writer import write_chapter_stream as do_write_stream
    from app.core.writer import get_last_chapter_content, load_style_sample

    last_content = await get_last_chapter_content(project)
    style_sample = req.style_sample or load_style_sample(novel_id)

    async def event_generator():
        try:
            async for event in do_write_stream(project, chapter_plan, last_content, style_sample):
                yield f"data: {json_module.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:
            # 流中途异常必须显式告知前端（否则前端会把断连当作正常结束）
            import logging
            logging.getLogger(__name__).error(f"流式写作中断: {e}", exc_info=True)
            yield f"data: {json_module.dumps({'type': 'error', 'message': f'生成中断: {e}'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{chapter_number}/revise/stream")
async def revise_chapter_stream(novel_id: str, chapter_number: int, req: ReviseRequest):
    """流式修改章节 (SSE)"""
    chapter = Chapter.load(novel_id, chapter_number)
    if not chapter:
        raise HTTPException(404, "章节不存在")

    project = NovelProject.load(novel_id)
    if not project:
        raise HTTPException(404, "项目不存在")

    plans = ChapterBatchPlan.list_for_novel(novel_id)
    chapter_plan = {}
    for plan in plans:
        for ch in plan.chapters:
            if ch.chapter_number == chapter_number:
                chapter_plan = ch.model_dump()
                break
        if chapter_plan:
            break

    from app.core.writer import revise_chapter_stream as do_revise_stream

    async def event_generator():
        try:
            async for event in do_revise_stream(project, chapter, req.revision意见, chapter_plan):
                yield f"data: {json_module.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:
            # 流中途异常必须显式告知前端（否则前端会把断连当作正常结束）
            import logging
            logging.getLogger(__name__).error(f"流式修改中断: {e}", exc_info=True)
            yield f"data: {json_module.dumps({'type': 'error', 'message': f'修改中断: {e}'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
