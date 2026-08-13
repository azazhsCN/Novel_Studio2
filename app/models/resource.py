from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
import json
from app.core.config import get_novel_subdirs
from app.core.storage import backup_file


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
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def add_resource(self, item: ResourceItem):
        # 检查是否已存在同名同类资源
        for i, r in enumerate(self.resources):
            if r.category == item.category and r.name == item.name:
                self.resources[i] = item
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
        """生成资源摘要，用于嵌入审计提示词"""
        lines = ["## 当前资源追踪状态\n"]

        categories = {
            "wealth": "财富/资产",
            "item": "重要物品",
            "system": "系统/数值",
            "character_status": "人物状态",
            "foreshadow": "伏笔/悬念",
        }

        for cat_key, cat_name in categories.items():
            items = self.get_resources_by_category(cat_key)
            if items:
                lines.append(f"### {cat_name}")
                for item in items:
                    status_mark = "" if item.status == "active" else f" [{item.status}]"
                    lines.append(f"- {item.name}：{item.value}{status_mark}")
                lines.append("")

        unresolved = self.get_unresolved_conflicts()
        if unresolved:
            lines.append("### 未解决冲突")
            for c in unresolved:
                lines.append(f"- 第{c.chapter_number}章：{c.description}")
            lines.append("")

        return "\n".join(lines)
