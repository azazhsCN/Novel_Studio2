from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
import json
import logging
from pathlib import Path
from app.core.config import get_novel_dir, get_novel_subdirs, DATA_DIR
from app.core.storage import backup_file, move_to_trash, quarantine_corrupt_file

logger = logging.getLogger(__name__)


class CharacterCard(BaseModel):
    name: str
    role_level: str = "main"  # main / secondary / marginal
    age: str = ""
    appearance: str = ""
    personality: str = ""
    background: str = ""
    relationships: str = ""
    current_status: str = ""
    arc: str = ""
    first_chapter: int = 0   # 首次出场章节
    plot_importance: str = "" # 剧情重要性描述


class PlotSegment(BaseModel):
    """剧情概述的缓存分段"""
    start_chapter: int
    end_chapter: int
    summary: str


class CorePromptModules(BaseModel):
    """结构化核心提示词，拆分为独立模块"""
    basic_setting: str = ""          # 基础设定：世界观、背景、核心规则
    character_cards: list[CharacterCard] = []  # 角色卡片列表
    plot_overview: str = ""          # 剧情概述（自动颗粒度压缩）
    plot_segments: list[PlotSegment] = []  # 分段缓存，用于增量更新
    writing_style: str = ""          # 文风设定：视角、语言、描写特点
    continuation_direction: str = "" # 续写方向：当前走向、冲突线索


class NovelProject(BaseModel):
    id: str
    title: str
    description: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())

    # 核心提示词各模块
    core_prompt: CorePromptModules = Field(default_factory=CorePromptModules)

    # 写作配置
    base_word_count: int = 3000       # 普通章节基准字数
    key_chapter_ratio: float = 1.3    # 重点章节倍率
    turning_chapter_ratio: float = 1.5  # 转折章节倍率

    # 导出配置
    export_keep_chapter_number: bool = True   # 导出时保留章节号
    export_keep_chapter_title: bool = True    # 导出时保留章节标题

    # 统计
    total_planned: int = 0
    total_finalized: int = 0

    def save(self):
        self.updated_at = datetime.now().isoformat()
        path = get_novel_dir(self.id) / "project.json"
        tmp = path.with_suffix('.json.tmp')
        tmp.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        if path.exists():
            backup_file(path)  # 覆盖前保留历史版本
        tmp.replace(path)

    @classmethod
    def load(cls, novel_id: str) -> Optional["NovelProject"]:
        path = get_novel_dir(novel_id) / "project.json"
        if not path.exists():
            return None
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    @classmethod
    def list_all(cls) -> list["NovelProject"]:
        projects = []
        if not DATA_DIR.exists():
            return projects
        for d in DATA_DIR.iterdir():
            if d.is_dir() and d.name.startswith("novel_"):
                pid = d.name[6:]  # remove "novel_" prefix
                try:
                    p = cls.load(pid)
                except Exception as e:
                    # 单个项目损坏不拖垮整个列表：隔离坏文件并记录日志
                    logger.error(f"项目 {pid} 的 project.json 损坏，已隔离: {e}")
                    quarantine_corrupt_file(d / "project.json")
                    continue
                if p:
                    projects.append(p)
        return sorted(projects, key=lambda x: x.updated_at, reverse=True)

    def delete(self):
        """删除项目：移入回收站而非直接删除，可手动恢复"""
        path = get_novel_dir(self.id)
        if path.exists():
            move_to_trash(path, DATA_DIR / ".trash")

    def get_word_count(self, chapter_type: str) -> int:
        """根据章节类型返回目标字数"""
        if chapter_type == "key":
            return int(self.base_word_count * self.key_chapter_ratio)
        elif chapter_type == "turning":
            return int(self.base_word_count * self.turning_chapter_ratio)
        return self.base_word_count

    def update_stats(self):
        """从文件系统重新统计：已规划(未写)、已定稿章节数"""
        dirs = get_novel_subdirs(self.id)
        plans_dir = dirs["plans"]
        chapters_dir = dirs["chapters"]

        # 收集所有已写章节号
        written_numbers = set()
        finalized = 0
        for f in chapters_dir.glob("*_meta.json"):
            try:
                meta = json.loads(f.read_text(encoding="utf-8"))
                written_numbers.add(meta.get("chapter_number", 0))
            except (json.JSONDecodeError, OSError, KeyError) as e:
                import logging
                logging.getLogger(__name__).warning(f"读取元数据失败 {f.name}: {e}")
        for f in chapters_dir.glob("*.txt"):
            if not f.name.endswith('.tmp'):
                finalized += 1

        # 统计已规划但未写作的章节数
        planned_unwritten = 0
        for f in plans_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                for ch in data.get("chapters", []):
                    ch_num = ch.get("chapter_number", 0)
                    if ch_num not in written_numbers:
                        planned_unwritten += 1
            except (json.JSONDecodeError, OSError) as e:
                import logging
                logging.getLogger(__name__).warning(f"读取规划失败 {f.name}: {e}")

        self.total_planned = planned_unwritten
        self.total_finalized = finalized
