"""审计系统模块
负责资源追踪、冲突检测、审计报告生成

设计要点（P0/P1 重构）：
- 审计必须带足"判定依据"（基础设定 / 相关人物卡 / 前情概述 / 资源表），否则模型只能瞎猜；
- 冲突带严重度：只有 error 才算"未通过"，warning/info 是提示，不阻塞定稿；
- 冲突带 anchor / fix_type / suggested_text，配合段落级补丁实现"微调"而非整章重写；
- 资源入库有准入门槛与近似归并，低信息量条目不会污染资源表。
"""
import logging
import re
from datetime import datetime

from fastapi import HTTPException

from app.core.api_client import api_client
from app.core.prompt_builder import build_audit_prompt, build_patch_prompt
from app.core.importer import _extract_json
from app.models.novel import NovelProject
from app.models.chapter import Chapter
from app.models.resource import (
    ResourceTracker, ResourceItem, AuditConflict, RESOURCE_CATEGORIES, CONFLICT_TYPES,
    SEVERITY_ORDER, IMPORTANCE_LABELS, normalize_resource_name,
)

logger = logging.getLogger(__name__)

# 资源置信度低于该值 → 只进候选区，等人工确认（不进入提示词）
CONFIDENCE_THRESHOLD = 0.6

# 伏笔超过这么多章没有更新，标记为 stale（移出提示词优先队列，但不删除）
STALE_CHAPTERS = 30


def _split_paragraphs(content: str) -> list[str]:
    """按空行切段，保留原始顺序（与前端展示一致）"""
    return [p for p in re.split(r"\n", content) if p.strip() != ""]


def _locate_paragraph(content: str, anchor: str) -> int:
    """在正文中定位 anchor 所在段落序号；找不到返回 -1"""
    anchor = (anchor or "").strip()
    if not anchor:
        return -1
    needle = anchor[:40]
    for i, para in enumerate(_split_paragraphs(content)):
        if needle in para:
            return i
    return -1


def _build_character_context(project: NovelProject, chapter_content: str) -> str:
    """只挑本章出场的人物卡，避免把全部角色都塞进提示词"""
    cards = project.core_prompt.character_cards
    if not cards:
        return ""
    appeared = [c for c in cards if c.name and c.name in chapter_content]
    # 没有命中任何名字时退化为"主要角色"，仍然比"完全不提供"有价值
    if not appeared:
        appeared = [c for c in cards if getattr(c, "role_level", "") == "main"][:5]
    if not appeared:
        appeared = cards[:5]

    lines = []
    for c in appeared:
        parts = [f"## {c.name}"]
        if c.age:
            parts.append(f"- 年龄：{c.age}")
        if c.current_status:
            parts.append(f"- 当前状态：{c.current_status}")
        if c.relationships:
            parts.append(f"- 关系：{c.relationships}")
        if c.arc:
            parts.append(f"- 角色弧线：{c.arc}")
        lines.append("\n".join(parts))
    return "\n\n".join(lines)


def _build_recent_overview(project: NovelProject, chapter_number: int,
                           max_chars: int = 3000) -> str:
    """取前情概述中与本章相关的部分（概述过长时保留末尾，靠近当前进度）"""
    overview = (project.core_prompt.plot_overview or "").strip()
    if not overview:
        return ""
    if len(overview) <= max_chars:
        return overview
    tail = overview[-max_chars:]
    # 尽量从句首开始，避免半句话
    cut = tail.find("\n")
    if 0 <= cut < 200:
        tail = tail[cut + 1:]
    return "（概述过长，仅列最近部分）\n" + tail


def _normalize_severity(value: str) -> str:
    v = (value or "").strip().lower()
    return v if v in SEVERITY_ORDER else "warning"


def _normalize_fix_type(value: str) -> str:
    v = (value or "").strip().lower()
    allowed = {"rewrite_paragraph", "replace_text", "update_resource", "none"}
    return v if v in allowed else "none"


async def audit_chapter(project: NovelProject, chapter: Chapter) -> dict:
    """对已写章节进行审计"""
    tracker = ResourceTracker.load(project.id)
    # P1：摘要分层 + 体积上限，不再无脑塞入全部条目
    resource_summary = tracker.get_summary_for_prompt(
        max_chars=4000, chapter_number=chapter.chapter_number
    )

    prompt = build_audit_prompt(
        project, chapter.content, chapter.chapter_number, resource_summary,
        nearby_characters=_build_character_context(project, chapter.content),
        recent_overview=_build_recent_overview(project, chapter.chapter_number),
    )
    response = await api_client.chat(prompt)
    result = _extract_json(response)

    # 解析失败不能当作"无冲突"：审计的失败方向必须显式报错
    if result.get("error"):
        raise HTTPException(502, f"审计失败：AI响应无法解析，请重试。原因: {result.get('error')}")

    # ---------- 处理冲突（带严重度与可执行补丁）----------
    conflicts: list[AuditConflict] = []
    filtered_out = 0
    for c in result.get("conflicts", []):
        if not isinstance(c, dict):
            continue
        description = (c.get("description") or "").strip()
        if not description:
            filtered_out += 1
            continue

        anchor = (c.get("anchor") or "").strip()
        severity = _normalize_severity(c.get("severity"))
        fix_type = _normalize_fix_type(c.get("fix_type"))

        # 没有原句锚点的"错误"通常是模型臆测：降级为 info，不阻塞定稿
        if severity == "error" and not anchor:
            severity = "info"

        conflict = AuditConflict(
            chapter_number=chapter.chapter_number,
            conflict_type=c.get("conflict_type", "unknown") or "unknown",
            description=description,
            suggestion=(c.get("suggestion") or "").strip(),
            severity=severity,
            anchor=anchor,
            fix_type=fix_type,
            suggested_text=(c.get("suggested_text") or "").strip(),
            paragraph_index=_locate_paragraph(chapter.content, anchor),
        )
        # evidence 存进 notes，报告里可展示"依据"
        evidence = (c.get("evidence") or "").strip()
        if evidence:
            conflict.notes = f"依据：{evidence}"

        conflicts.append(conflict)
        tracker.add_conflict(conflict)

    # ---------- 处理资源变动（准入 + 归并 + 分层）----------
    resource_changes = []
    stats = {"added": 0, "merged": 0, "rejected": 0, "candidate": 0}
    for rc in result.get("resource_changes", []):
        if not isinstance(rc, dict):
            continue
        action = (rc.get("action") or "add").strip().lower()
        name = (rc.get("name") or "").strip()
        if not name:
            stats["rejected"] += 1
            continue

        importance = (rc.get("importance") or "normal").strip().lower()
        if importance not in IMPORTANCE_LABELS:
            importance = "normal"
        try:
            confidence = float(rc.get("confidence", 1.0))
        except (TypeError, ValueError):
            confidence = 1.0
        confidence = min(max(confidence, 0.0), 1.0)

        category = (rc.get("category") or "").strip()
        if category not in RESOURCE_CATEGORIES:
            # 模型自造分类统一归入最接近的现有分类，避免出现 skill/system_character 这类野分类
            category = _coerce_category(category, name)

        # 低置信度或次要条目 → 只进候选区，不进入提示词
        state = "tracked"
        if confidence < CONFIDENCE_THRESHOLD or importance == "minor":
            state = "candidate"

        item = ResourceItem(
            category=category,
            name=name,
            value=(rc.get("value") or "").strip(),
            chapter_introduced=chapter.chapter_number if action == "add" else 0,
            chapter_updated=chapter.chapter_number,
            status="destroyed" if action == "destroy" else "active",
            importance=importance,
            confidence=confidence,
            state=state,
            source_excerpt=(rc.get("source_excerpt") or "").strip()[:200],
        )

        if action == "resolve":
            resolved = False
            target = normalize_resource_name(name)
            for r in tracker.resources:
                if r.category == "foreshadow" and (
                    normalize_resource_name(r.name) == target or name in r.aliases
                ):
                    r.status = "resolved"
                    r.chapter_updated = chapter.chapter_number
                    resolved = True
                    break
            outcome = "resolved" if resolved else "rejected"
            if not resolved:
                stats["rejected"] += 1
        else:
            outcome = tracker.add_resource(item)
            stats[outcome] = stats.get(outcome, 0) + 1
            if outcome != "rejected" and state == "candidate":
                stats["candidate"] += 1

        resource_changes.append({
            "category": item.category,
            "name": item.name,
            "value": item.value,
            "action": action,
            "importance": item.importance,
            "outcome": outcome,
        })

    _mark_stale(tracker, chapter.chapter_number)
    tracker.save()

    # ---------- 审计结论：只有 error 才算未通过 ----------
    errors = [c for c in conflicts if c.severity == "error"]
    warnings = [c for c in conflicts if c.severity == "warning"]
    infos = [c for c in conflicts if c.severity == "info"]
    has_errors = len(errors) > 0

    triggered = {c.conflict_type for c in errors} | {c.conflict_type for c in warnings}
    passed_checks = [label for key, label in CONFLICT_TYPES.items() if key not in triggered]

    chapter.audit_passed = not has_errors
    chapter.audit_summary = result.get("summary", "")
    chapter.audit_conflict_types = sorted(triggered)
    chapter.audit_passed_checks = passed_checks
    chapter.audited_at = datetime.now().isoformat()
    chapter.save()

    logger.info(
        f"第{chapter.chapter_number}章审计完成：错误 {len(errors)} / 警告 {len(warnings)} / "
        f"提示 {len(infos)}（过滤空描述 {filtered_out}）；资源 新增{stats['added']} "
        f"归并{stats['merged']} 拒收{stats['rejected']} 候选{stats['candidate']}"
    )

    return {
        "chapter_number": chapter.chapter_number,
        "conflicts": [c.model_dump() for c in conflicts],
        "resource_changes": resource_changes,
        "has_conflicts": len(conflicts) > 0,
        "has_errors": has_errors,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "info_count": len(infos),
        "passed_checks": passed_checks,
        "summary": result.get("summary", ""),
        "resource_stats": stats,
    }


def _coerce_category(category: str, name: str) -> str:
    """把模型自造分类映射到现有 5 类（历史数据里的 skill / system_character 等）"""
    c = (category or "").lower()
    if c in RESOURCE_CATEGORIES:
        return c
    if any(k in c for k in ("skill", "ability", "技能")):
        return "system"
    if any(k in c for k in ("character", "人物", "角色")):
        return "character_status"
    if any(k in c for k in ("item", "prop", "物品", "道具")):
        return "item"
    if any(k in c for k in ("money", "wealth", "资产", "钱")):
        return "wealth"
    if "伏笔" in name or "foreshadow" in c:
        return "foreshadow"
    return "item"


def _mark_stale(tracker: ResourceTracker, current_chapter: int) -> None:
    """长期未跟进的伏笔/线索标记 stale（移出提示词优先队列，但不删除）"""
    for r in tracker.resources:
        if r.category == "foreshadow" and r.status == "active":
            if current_chapter - (r.chapter_updated or 0) >= STALE_CHAPTERS:
                r.stale = True


# ---------- 段落级补丁（P0 核心）----------

async def propose_patch(project: NovelProject, chapter: Chapter,
                        conflict_index: int) -> dict:
    """针对某条冲突生成段落级补丁（不落盘，仅预览）"""
    tracker = ResourceTracker.load(project.id)
    if conflict_index < 0 or conflict_index >= len(tracker.conflicts):
        raise HTTPException(404, "冲突不存在")
    conflict = tracker.conflicts[conflict_index]

    if conflict.resolved:
        raise HTTPException(400, "该冲突已处理")

    paragraphs = _split_paragraphs(chapter.content)
    if not paragraphs:
        raise HTTPException(400, "本章没有可修改的正文")

    conflict_dict = conflict.model_dump()

    # 已定位到段落且模型给了改写文本 → 直接可用，省一次调用
    if conflict.paragraph_index >= 0 and conflict.suggested_text:
        idx = conflict.paragraph_index
        return {
            "conflict_index": conflict_index,
            "chapter_number": chapter.chapter_number,
            "patches": [{
                "paragraph_index": idx,
                "original": paragraphs[idx],
                "revised": conflict.suggested_text,
                "source": "audit",
            }],
            "paragraph_count": len(paragraphs),
        }

    prompt = build_patch_prompt(chapter.content, paragraphs, conflict_dict)
    response = await api_client.chat(prompt)
    result = _extract_json(response)
    if result.get("error"):
        raise HTTPException(502, f"补丁生成失败：AI响应无法解析。原因: {result.get('error')}")

    patches = []
    for p in result.get("patches", []):
        if not isinstance(p, dict):
            continue
        try:
            idx = int(p.get("paragraph_index", -1))
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx >= len(paragraphs):
            continue
        original = (p.get("original") or "").strip()
        revised = (p.get("revised") or "").strip()
        if not revised:
            continue
        # 校验定位：模型给的原文必须与真实段落一致（允许首尾空白差异）
        if original and original != paragraphs[idx].strip():
            actual_idx = next(
                (i for i, para in enumerate(paragraphs)
                 if original[:40] and original[:40] in para),
                -1,
            )
            if actual_idx < 0:
                logger.warning(f"补丁定位校验失败，已丢弃该段落（索引 {idx}）")
                continue
            idx = actual_idx
        patches.append({
            "paragraph_index": idx,
            "original": paragraphs[idx],
            "revised": revised,
            "source": "ai",
        })

    if not patches:
        raise HTTPException(502, "未能生成有效补丁，请重试或改用整章重写")

    return {
        "conflict_index": conflict_index,
        "chapter_number": chapter.chapter_number,
        "patches": patches,
        "paragraph_count": len(paragraphs),
    }


def apply_patches(chapter: Chapter, patches: list[dict]) -> dict:
    """把补丁应用到章节（只替换目标段落，其余一字不动），并标记冲突已处理"""
    paragraphs = _split_paragraphs(chapter.content)
    applied, failed = [], []

    # 从后往前替换，避免索引位移
    normalized = []
    for p in patches:
        try:
            idx = int(p.get("paragraph_index", -1))
        except (TypeError, ValueError):
            failed.append({"paragraph_index": p.get("paragraph_index"), "reason": "索引非法"})
            continue
        revised = (p.get("revised") or "").strip()
        if idx < 0 or idx >= len(paragraphs):
            failed.append({"paragraph_index": idx, "reason": "索引越界"})
            continue
        if not revised:
            failed.append({"paragraph_index": idx, "reason": "改写内容为空"})
            continue
        expected = (p.get("original") or "").strip()
        if expected and expected != paragraphs[idx].strip():
            failed.append({"paragraph_index": idx, "reason": "原文与当前正文不一致，拒绝覆盖"})
            continue
        normalized.append((idx, revised))

    for idx, revised in sorted(normalized, key=lambda x: -x[0]):
        paragraphs[idx] = revised
        applied.append(idx)

    if not applied:
        raise HTTPException(400, "没有可应用的补丁（原文已变动或内容为空）")

    before_len = len(chapter.content)
    chapter.content = "\n".join(paragraphs)
    chapter.is_finalized = False
    # 局部修订后审计状态失效，需重新审计
    chapter.audit_passed = False
    chapter.save()

    return {
        "applied": sorted(applied),
        "failed": failed,
        "before_chars": before_len,
        "after_chars": len(chapter.content),
        "word_count": chapter.word_count,
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

    # 带实际索引的未解决冲突，按严重度排序（error 优先）
    unresolved_with_index = []
    for i, c in enumerate(tracker.conflicts):
        if not c.resolved:
            entry = c.model_dump()
            entry["actual_index"] = i
            entry["conflict_type_label"] = CONFLICT_TYPES.get(c.conflict_type, c.conflict_type)
            unresolved_with_index.append(entry)
    unresolved_with_index.sort(
        key=lambda e: (SEVERITY_ORDER.get(e.get("severity", "warning"), 1), e["chapter_number"])
    )

    # 按分类分组；候选区单独返回，不与已确认资源混排
    grouped: dict[str, list[dict]] = {}
    for r in tracker.resources:
        if r.state == "candidate":
            continue
        grouped.setdefault(r.category, []).append(r.model_dump())

    resources: dict[str, list[dict]] = {}
    resource_category_labels: dict[str, str] = {}
    for cat_key, cat_name in RESOURCE_CATEGORIES.items():
        resources[cat_key] = grouped.pop(cat_key, [])
        resource_category_labels[cat_key] = cat_name
    for cat_key in sorted(grouped):
        resources[cat_key] = grouped[cat_key]
        resource_category_labels[cat_key] = f"{cat_key}（未归类）"

    candidates = [
        {"index": i, **r.model_dump()}
        for i, r in enumerate(tracker.resources) if r.state == "candidate"
    ]

    # 已通过审计的章节及其通过项（需求 3.7：报告需包含"通过项"）
    passed_items = []
    for ch in Chapter.list_for_novel(novel_id):
        if ch.audited_at and ch.audit_passed:
            passed_items.append({
                "chapter_number": ch.chapter_number,
                "title": ch.title,
                "summary": ch.audit_summary,
                "passed_checks": ch.audit_passed_checks,
                "audited_at": ch.audited_at,
            })

    severity_counts = {"error": 0, "warning": 0, "info": 0}
    for e in unresolved_with_index:
        sev = e.get("severity", "warning")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1

    return {
        "resources": resources,
        "resource_category_labels": resource_category_labels,
        "conflict_type_labels": CONFLICT_TYPES,
        "severity_labels": {"error": "错误", "warning": "警告", "info": "提示"},
        "severity_counts": severity_counts,
        "passed_items": passed_items,
        "unresolved_conflicts": unresolved_with_index,
        "candidates": candidates,
        "total_resources": len([r for r in tracker.resources if r.state != "candidate"]),
        "total_candidates": len(candidates),
        "total_conflicts": len(tracker.conflicts),
        "resolved_conflicts": len([c for c in tracker.conflicts if c.resolved]),
    }
