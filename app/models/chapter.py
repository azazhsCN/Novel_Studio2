from pydantic import BaseModel, Field, field_validator
from typing import Optional
from datetime import datetime
import json
import logging
from app.core.config import get_novel_subdirs, validate_plan_id
from app.core.storage import sanitize_filename, backup_file, quarantine_corrupt_file

logger = logging.getLogger(__name__)


class ChapterPlanItem(BaseModel):
    """单章规划"""
    chapter_number: int
    title: str
    chapter_type: str = "normal"  # normal / key / turning
    word_count_target: int = 3000
    time: str = ""
    scene: str = ""
    core_plot: list[str] = []
    prompt: str = ""


class ChapterBatchPlan(BaseModel):
    """一批章节的规划"""
    id: str
    novel_id: str
    start_chapter: int
    end_chapter: int
    direction_hint: str = ""  # 用户输入的方向提示词
    chapters: list[ChapterPlanItem] = []
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())

    def save(self):
        self.updated_at = datetime.now().isoformat()
        dirs = get_novel_subdirs(self.novel_id)
        path = dirs["plans"] / f"{self.id}.json"
        tmp = path.with_suffix('.json.tmp')
        tmp.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, novel_id: str, plan_id: str) -> Optional["ChapterBatchPlan"]:
        # 白名单校验防止路径遍历；非法ID与不存在同样返回None（路由层404）
        try:
            validate_plan_id(plan_id)
        except ValueError:
            return None
        dirs = get_novel_subdirs(novel_id)
        path = dirs["plans"] / f"{plan_id}.json"
        if not path.exists():
            return None
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    @classmethod
    def list_for_novel(cls, novel_id: str) -> list["ChapterBatchPlan"]:
        dirs = get_novel_subdirs(novel_id)
        plans = []
        for f in dirs["plans"].glob("*.json"):
            try:
                p = cls.model_validate_json(f.read_text(encoding="utf-8"))
                plans.append(p)
            except Exception as e:
                # 不静默吞掉：隔离坏文件并记日志，避免规划"凭空消失"无感知
                logger.error(f"规划文件损坏，已隔离 {f.name}: {e}")
                quarantine_corrupt_file(f)
        return sorted(plans, key=lambda x: x.start_chapter)

    def delete(self):
        dirs = get_novel_subdirs(self.novel_id)
        path = dirs["plans"] / f"{self.id}.json"
        if path.exists():
            path.unlink()


class Chapter(BaseModel):
    """已写章节"""
    novel_id: str
    chapter_number: int
    title: str
    chapter_type: str = "normal"
    content: str = ""
    word_count: int = 0
    is_finalized: bool = False  # 是否已定稿
    audit_passed: bool = False  # 审计是否通过
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())

    @field_validator("title")
    @classmethod
    def _sanitize_title(cls, v: str) -> str:
        """标题会拼入文件名，必须先消毒（Windows非法字符会直接导致写入失败）"""
        return sanitize_filename(v)

    def save(self):
        self.updated_at = datetime.now().isoformat()
        self.word_count = len(self.content)
        dirs = get_novel_subdirs(self.novel_id)
        chapters_dir = dirs["chapters"]

        # 先写新文件（成功后才清理旧标题文件，避免写入失败时旧稿已删）
        filename = f"第{self.chapter_number:04d}章_{self.title}.txt"
        path = chapters_dir / filename
        tmp = path.with_suffix('.txt.tmp')
        tmp.write_text(self.content, encoding="utf-8")
        if path.exists():
            backup_file(path)  # 覆盖同名文件前保留历史版本
        tmp.replace(path)

        meta_path = chapters_dir / f"第{self.chapter_number:04d}章_meta.json"
        meta = self.model_dump()
        meta.pop("content", None)
        tmp_meta = meta_path.with_suffix('.json.tmp')
        tmp_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        if meta_path.exists():
            backup_file(meta_path)
        tmp_meta.replace(meta_path)

        # 清理旧标题的txt文件（跳过备份文件）
        for old in chapters_dir.glob(f"第{self.chapter_number:04d}章_*.txt"):
            if old.name != filename:
                old.unlink(missing_ok=True)

    @classmethod
    def load(cls, novel_id: str, chapter_number: int) -> Optional["Chapter"]:
        dirs = get_novel_subdirs(novel_id)
        meta_path = dirs["chapters"] / f"第{chapter_number:04d}章_meta.json"
        if not meta_path.exists():
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        # 加载txt内容
        txt_files = list(dirs["chapters"].glob(f"第{chapter_number:04d}章_*.txt"))
        if txt_files:
            meta["content"] = txt_files[0].read_text(encoding="utf-8")
        return cls(**meta)

    @classmethod
    def list_for_novel(cls, novel_id: str, load_content: bool = False) -> list["Chapter"]:
        """列出已写章节。load_content=True 时才读取正文（列表页不需要）"""
        dirs = get_novel_subdirs(novel_id)
        chapters = []
        for f in dirs["chapters"].glob("*_meta.json"):
            try:
                meta = json.loads(f.read_text(encoding="utf-8"))
                if load_content:
                    ch_num = meta.get("chapter_number", 0)
                    txt_files = list(dirs["chapters"].glob(f"第{ch_num:04d}章_*.txt"))
                    if txt_files:
                        meta["content"] = txt_files[0].read_text(encoding="utf-8")
                chapters.append(cls(**meta))
            except Exception as e:
                # 不静默吞掉：隔离坏文件并记日志，避免章节"凭空消失"无感知
                logger.error(f"章节元数据损坏，已隔离 {f.name}: {e}")
                quarantine_corrupt_file(f)
        return sorted(chapters, key=lambda x: x.chapter_number)
