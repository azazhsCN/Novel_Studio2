from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
import json
import logging
from app.core.config import get_novel_subdirs
from app.core.storage import backup_file, quarantine_corrupt_file

logger = logging.getLogger(__name__)

# 首选的资源分类（键 → 中文名）。历史数据里出现过模型外分类（如 skill、system_character），
# 一律保留并展示，不做静默丢弃；顺序即前端展示顺序。
RESOURCE_CATEGORIES = {
    "wealth": "财富/资产",
    "item": "重要物品",
    "system": "系统/数值",
    "character_status": "人物状态",
    "foreshadow": "伏笔/悬念",
}

# 审计检测的 6 类冲突（键 → 中文名，顺序即报告顺序）。报告中的"通过项"= 未被命中的类型。
CONFLICT_TYPES = {
    "timeline": "时间线冲突",
    "character": "人物状态冲突",
    "item": "物品冲突",
    "setting": "设定冲突",
    "value": "数值冲突",
    "foreshadow": "伏笔冲突",
}


class ResourceItem(BaseModel):
    """单条资源追踪项"""
    category: str  # wealth / item / system / character_status / foreshadow
    name: str
    value: str
    chapter_introduced: int = 0  # 引入的章节号
    chapter_updated: int = 0     # 最后更新的章节号
    status: str = "active"       # active / resolved / destroyed
    notes: str = ""


class AuditConflict(BaseModel):
    """审计发现的冲突"""
    chapter_number: int
    conflict_type: str  # timeline / character / item / setting / value / foreshadow
    description: str
    suggestion: str = ""
    resolved: bool = False
    resolution: str = ""  # ignore / update_resource / modify_chapter
    notes: str = ""  # 解决备注


class ResourceTracker(BaseModel):
    """小说资源追踪表"""
    novel_id: str
    resources: list[ResourceItem] = []
    conflicts: list[AuditConflict] = []
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())

    def save(self):
        self.updated_at = datetime.now().isoformat()
        dirs = get_novel_subdirs(self.novel_id)
        path = dirs["base"] / "resources.json"
        tmp = path.with_suffix('.json.tmp')
        tmp.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        if path.exists():
            backup_file(path)  # 覆盖前保留历史版本
        tmp.replace(path)

    @classmethod
    def load(cls, novel_id: str) -> "ResourceTracker":
        dirs = get_novel_subdirs(novel_id)
        path = dirs["base"] / "resources.json"
        if not path.exists():
            return cls(novel_id=novel_id)
        try:
            return cls.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            # 资源表损坏：隔离留证并退化为空表，不再让每次审计/报告都 500
            logger.error(f"资源表损坏，已隔离 {path.name}: {e}")
            quarantine_corrupt_file(path)
            return cls(novel_id=novel_id)

    def add_resource(self, item: ResourceItem):
        """按 (category, name) 字段级合并

        此前是整体替换（self.resources[i] = item），会把原有的"引入章节"清零、
        把人工填写的备注清空。现在只更新审计真正给出的字段。
        """
        for r in self.resources:
            if r.category == item.category and r.name == item.name:
                if item.value:
                    r.value = item.value
                if item.chapter_introduced:
                    r.chapter_introduced = item.chapter_introduced
                if item.chapter_updated:
                    r.chapter_updated = item.chapter_updated
                if item.notes:
                    r.notes = item.notes
                if item.status == "destroyed":
                    r.status = "destroyed"
                elif r.status != "destroyed":
                    # 已销毁的资源再次出现属于冲突，交由审计报告提示，不在此静默复活
                    r.status = item.status
                return
        self.resources.append(item)

    def get_resources_by_category(self, category: str) -> list[ResourceItem]:
        return [r for r in self.resources if r.category == category]

    def add_conflict(self, conflict: AuditConflict):
        self.conflicts.append(conflict)

    def get_unresolved_conflicts(self) -> list[AuditConflict]:
        return [c for c in self.conflicts if not c.resolved]

    def resolve_conflict(self, index: int, resolution: str, notes: str = ""):
        if 0 <= index < len(self.conflicts):
            self.conflicts[index].resolved = True
            self.conflicts[index].resolution = resolution
            if notes:
                self.conflicts[index].notes = notes

    def get_summary_for_prompt(self) -> str:
        """生成资源摘要，用于嵌入审计提示词（含模型外分类，不静默丢弃）"""
        lines = ["## 当前资源追踪状态\n"]

        grouped: dict[str, list[ResourceItem]] = {}
        for r in self.resources:
            grouped.setdefault(r.category, []).append(r)

        def append_group(cat_name: str, items: list[ResourceItem]) -> None:
            if not items:
                return
            lines.append(f"### {cat_name}")
            for item in items:
                status_mark = "" if item.status == "active" else f" [{item.status}]"
                lines.append(f"- {item.name}：{item.value}{status_mark}")
            lines.append("")

        for cat_key, cat_name in RESOURCE_CATEGORIES.items():
            append_group(cat_name, grouped.pop(cat_key, []))
        # 模型外分类（历史数据里的 skill / system_character 等）也要进入提示词
        for cat_key in sorted(grouped):
            append_group(f"{cat_key}（未归类）", grouped[cat_key])

        unresolved = self.get_unresolved_conflicts()
        if unresolved:
            lines.append("### 未解决冲突")
            for c in unresolved:
                lines.append(f"- 第{c.chapter_number}章：{c.description}")
            lines.append("")

        return "\n".join(lines)
