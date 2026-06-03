"""审计系统模块
负责资源追踪、冲突检测、审计报告生成
"""
from app.core.api_client import api_client
from app.core.prompt_builder import build_audit_prompt
from app.core.importer import _extract_json
from app.models.novel import NovelProject
from app.models.chapter import Chapter
from app.models.resource import ResourceTracker, ResourceItem, AuditConflict


async def audit_chapter(project: NovelProject, chapter: Chapter) -> dict:
    """对已写章节进行审计"""
    tracker = ResourceTracker.load(project.id)
    resource_summary = tracker.get_summary_for_prompt()

    prompt = build_audit_prompt(project, chapter.content, chapter.chapter_number, resource_summary)
    response = await api_client.chat(prompt)
    result = _extract_json(response)

    # 处理冲突
    conflicts = []
    for c in result.get("conflicts", []):
        conflict = AuditConflict(
            chapter_number=chapter.chapter_number,
            conflict_type=c.get("conflict_type", "unknown"),
            description=c.get("description", ""),
            suggestion=c.get("suggestion", ""),
        )
        conflicts.append(conflict)
        tracker.add_conflict(conflict)

    # 处理资源变动
    resource_changes = []
    for rc in result.get("resource_changes", []):
        action = rc.get("action", "add")
        item = ResourceItem(
            category=rc.get("category", ""),
            name=rc.get("name", ""),
            value=rc.get("value", ""),
            chapter_introduced=chapter.chapter_number if action == "add" else 0,
            chapter_updated=chapter.chapter_number,
            status="active" if action != "destroy" else "destroyed",
        )

        if action == "resolve":
            # 解决伏笔
            for i, r in enumerate(tracker.resources):
                if r.category == "foreshadow" and r.name == item.name:
                    tracker.resources[i].status = "resolved"
                    tracker.resources[i].chapter_updated = chapter.chapter_number
                    break
        else:
            tracker.add_resource(item)

        resource_changes.append({
            "category": item.category,
            "name": item.name,
            "value": item.value,
            "action": action,
        })

    tracker.save()

    # 标记审计状态
    has_conflicts = len(conflicts) > 0
    chapter.audit_passed = not has_conflicts
    chapter.save()

    return {
        "chapter_number": chapter.chapter_number,
        "conflicts": [c.model_dump() for c in conflicts],
        "resource_changes": resource_changes,
        "has_conflicts": has_conflicts,
        "summary": result.get("summary", ""),
    }


async def resolve_conflict(novel_id: str, conflict_index: int,
                            resolution: str, notes: str = "") -> bool:
    """解决冲突"""
    tracker = ResourceTracker.load(novel_id)
    if 0 <= conflict_index < len(tracker.conflicts):
        tracker.resolve_conflict(conflict_index, resolution, notes)
        tracker.save()
        return True
    return False


async def get_resource_tracker(novel_id: str) -> ResourceTracker:
    """获取资源追踪器"""
    return ResourceTracker.load(novel_id)


async def get_audit_report(novel_id: str) -> dict:
    """获取完整审计报告"""
    tracker = ResourceTracker.load(novel_id)

    # 带实际索引的未解决冲突
    unresolved_with_index = []
    for i, c in enumerate(tracker.conflicts):
        if not c.resolved:
            entry = c.model_dump()
            entry["actual_index"] = i
            unresolved_with_index.append(entry)

    return {
        "resources": {
            "wealth": [r.model_dump() for r in tracker.get_resources_by_category("wealth")],
            "item": [r.model_dump() for r in tracker.get_resources_by_category("item")],
            "system": [r.model_dump() for r in tracker.get_resources_by_category("system")],
            "character_status": [r.model_dump() for r in tracker.get_resources_by_category("character_status")],
            "foreshadow": [r.model_dump() for r in tracker.get_resources_by_category("foreshadow")],
        },
        "unresolved_conflicts": unresolved_with_index,
        "total_resources": len(tracker.resources),
        "total_conflicts": len(tracker.conflicts),
        "resolved_conflicts": len([c for c in tracker.conflicts if c.resolved]),
    }
