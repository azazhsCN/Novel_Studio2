"""审计系统API路由"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
from app.models.novel import NovelProject
from app.models.resource import ResourceTracker


router = APIRouter(prefix="/api/novels/{novel_id}/audit", tags=["audit"])


class ResolveRequest(BaseModel):
    conflict_index: int = Field(ge=0)
    resolution: str  # ignore / update_resource / modify_chapter / patched
    notes: str = ""


class ResourceUpdateRequest(BaseModel):
    name: Optional[str] = None
    value: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None
    importance: Optional[str] = None
    state: Optional[str] = None


class PatchProposeRequest(BaseModel):
    conflict_index: int = Field(ge=0)


class PatchItem(BaseModel):
    paragraph_index: int
    original: str = ""
    revised: str


class PatchApplyRequest(BaseModel):
    conflict_index: int = Field(default=-1)
    patches: list[PatchItem]


class MergeRequest(BaseModel):
    keep_index: int = Field(ge=0)
    drop_indices: list[int] = []


class CandidateRequest(BaseModel):
    index: int = Field(ge=0)
    action: str  # accept / ignore


class BulkCleanupRequest(BaseModel):
    """一键整理：合并所有识别出的重名组 / 忽略低信息量条目"""
    merge_duplicates: bool = True
    ignore_low_information: bool = False


@router.get("/report")
async def get_audit_report(novel_id: str):
    """获取完整审计报告"""
    project = NovelProject.load(novel_id)
    if not project:
        raise HTTPException(404, "项目不存在")

    from app.core.auditor import get_audit_report
    report = await get_audit_report(novel_id)
    return report


@router.get("/resources")
async def get_resources(novel_id: str):
    """获取资源追踪表"""
    tracker = ResourceTracker.load(novel_id)
    return tracker.model_dump()


@router.get("/resources/health")
async def resource_health(novel_id: str):
    """资源表体检：近似重名 / 低信息量 / 长期未跟进 / 候选区积压"""
    tracker = ResourceTracker.load(novel_id)
    return tracker.health_report()


@router.put("/resources/{category}/{index}")
async def update_resource(novel_id: str, category: str, index: int,
                          req: ResourceUpdateRequest):
    """编辑资源项"""
    tracker = ResourceTracker.load(novel_id)
    items = [i for i, r in enumerate(tracker.resources) if r.category == category]
    if index < 0 or index >= len(items):
        raise HTTPException(404, "资源项不存在")

    real_index = items[index]
    res = tracker.resources[real_index]
    if req.name is not None:
        res.name = req.name
    if req.value is not None:
        res.value = req.value
    if req.status is not None:
        res.status = req.status
    if req.notes is not None:
        res.notes = req.notes
    if req.importance is not None:
        if req.importance not in ("core", "normal", "minor"):
            raise HTTPException(400, "importance 只能是 core/normal/minor")
        res.importance = req.importance
    if req.state is not None:
        if req.state not in ("tracked", "candidate", "ignored"):
            raise HTTPException(400, "state 只能是 tracked/candidate/ignored")
        res.state = req.state
    tracker.save()
    return {"message": "资源已更新"}


@router.delete("/resources/{category}/{index}")
async def delete_resource(novel_id: str, category: str, index: int):
    """删除资源项"""
    tracker = ResourceTracker.load(novel_id)
    items = [i for i, r in enumerate(tracker.resources) if r.category == category]
    if index < 0 or index >= len(items):
        raise HTTPException(404, "资源项不存在")

    real_index = items[index]
    tracker.resources.pop(real_index)
    tracker.save()
    return {"message": "资源已删除"}


@router.post("/resources/merge")
async def merge_resources(novel_id: str, req: MergeRequest):
    """合并近似重复的资源条目（保留 keep_index，其余并入其 aliases）"""
    tracker = ResourceTracker.load(novel_id)
    if req.keep_index >= len(tracker.resources):
        raise HTTPException(404, "保留项不存在")
    if not req.drop_indices:
        raise HTTPException(400, "没有要合并的条目")
    ok = tracker.merge_resources(req.keep_index, req.drop_indices)
    if not ok:
        raise HTTPException(400, "合并失败")
    tracker.save()
    return {"message": "已合并", "remaining": len(tracker.resources)}


@router.post("/resources/candidate")
async def handle_candidate(novel_id: str, req: CandidateRequest):
    """候选区处理：采纳（进入提示词）/ 忽略"""
    tracker = ResourceTracker.load(novel_id)
    if req.index < 0 or req.index >= len(tracker.resources):
        raise HTTPException(404, "资源不存在")
    item = tracker.resources[req.index]
    if item.state != "candidate":
        raise HTTPException(400, "该条目不在候选区")
    if req.action == "accept":
        item.state = "tracked"
        item.importance = "normal" if item.importance == "minor" else item.importance
    elif req.action == "ignore":
        item.state = "ignored"
    else:
        raise HTTPException(400, "action 只能是 accept/ignore")
    tracker.save()
    return {"message": "已处理", "state": item.state}


@router.post("/resources/cleanup")
async def bulk_cleanup(novel_id: str, req: BulkCleanupRequest):
    """一键整理资源表：合并重名组、可选忽略低信息量条目"""
    tracker = ResourceTracker.load(novel_id)
    before = len(tracker.resources)
    merged_groups = 0
    ignored = 0

    if req.merge_duplicates:
        # 从后往前处理，避免索引位移
        groups = tracker.find_duplicates()
        for g in sorted(groups, key=lambda x: max(x["indices"]), reverse=True):
            keep = g["suggested_keep"]
            drops = [i for i in g["indices"] if i != keep]
            if drops and tracker.merge_resources(keep, drops):
                merged_groups += 1

    if req.ignore_low_information:
        for r in tracker.resources:
            if r.state != "ignored" and tracker._is_low_information(r):
                r.state = "ignored"
                ignored += 1

    tracker.save()
    return {
        "message": "整理完成",
        "before": before,
        "after": len(tracker.resources),
        "merged_groups": merged_groups,
        "ignored": ignored,
    }


@router.post("/resolve")
async def resolve_conflict(novel_id: str, req: ResolveRequest):
    """解决冲突"""
    from app.core.auditor import resolve_conflict
    success = await resolve_conflict(novel_id, req.conflict_index, req.resolution, req.notes)
    if not success:
        raise HTTPException(400, "解决冲突失败")
    return {"message": "冲突已解决"}


# ---------- 段落级补丁（P0：审计建议落地，不再整章重生成）----------

@router.post("/patch/propose")
async def propose_patch(novel_id: str, req: PatchProposeRequest):
    """为某条冲突生成段落级补丁（仅预览，不落盘）"""
    project = NovelProject.load(novel_id)
    if not project:
        raise HTTPException(404, "项目不存在")

    tracker = ResourceTracker.load(novel_id)
    if req.conflict_index >= len(tracker.conflicts):
        raise HTTPException(404, "冲突不存在")
    conflict = tracker.conflicts[req.conflict_index]

    from app.models.chapter import Chapter
    chapter = Chapter.load(novel_id, conflict.chapter_number)
    if not chapter:
        raise HTTPException(404, "冲突对应章节不存在")

    from app.core.auditor import propose_patch as do_propose
    return await do_propose(project, chapter, req.conflict_index)


@router.post("/patch/apply")
async def apply_patch(novel_id: str, req: PatchApplyRequest):
    """应用段落级补丁：只替换目标段落，其余一字不动"""
    if not req.patches:
        raise HTTPException(400, "没有要应用的补丁")

    tracker = ResourceTracker.load(novel_id)
    if req.conflict_index >= len(tracker.conflicts) or req.conflict_index < 0:
        raise HTTPException(404, "冲突不存在")
    conflict = tracker.conflicts[req.conflict_index]

    from app.models.chapter import Chapter
    chapter = Chapter.load(novel_id, conflict.chapter_number)
    if not chapter:
        raise HTTPException(404, "章节不存在")

    from app.core.auditor import apply_patches
    result = apply_patches(chapter, [p.model_dump() for p in req.patches])

    # 应用成功后把冲突标记为已解决（resolution=patched）
    tracker.resolve_conflict(req.conflict_index, "patched", "已应用段落补丁")
    tracker.save()

    result["chapter_number"] = chapter.chapter_number
    result["message"] = f"已应用 {len(result['applied'])} 处修改"
    return result
