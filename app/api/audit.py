"""审计系统API路由"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from app.models.novel import NovelProject
from app.models.resource import ResourceTracker


router = APIRouter(prefix="/api/novels/{novel_id}/audit", tags=["audit"])


class ResolveRequest(BaseModel):
    conflict_index: int
    resolution: str  # ignore / update_resource / modify_chapter
    notes: str = ""


class ResourceUpdateRequest(BaseModel):
    name: Optional[str] = None
    value: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None


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


@router.post("/resolve")
async def resolve_conflict(novel_id: str, req: ResolveRequest):
    """解决冲突"""
    from app.core.auditor import resolve_conflict
    success = await resolve_conflict(novel_id, req.conflict_index, req.resolution, req.notes)
    if not success:
        raise HTTPException(400, "解决冲突失败")
    return {"message": "冲突已解决"}
