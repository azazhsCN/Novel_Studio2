from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
import json
from app.core.config import get_novel_subdirs


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
            except Exception:
                pass
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

    def save(self):
        self.updated_at = datetime.now().isoformat()
        self.word_count = len(self.content)
        dirs = get_novel_subdirs(self.novel_id)
        chapters_dir = dirs["chapters"]

        # 清理旧标题的txt文件
        for old in chapters_dir.glob(f"第{self.chapter_number:04d}章_*.txt"):
            if old.name != f"第{self.chapter_number:04d}章_{self.title}.txt":
                old.unlink(missing_ok=True)

        # 非原子写入：先写临时文件再重命名
        filename = f"第{self.chapter_number:04d}章_{self.title}.txt"
        path = chapters_dir / filename
        tmp = path.with_suffix('.txt.tmp')
        tmp.write_text(self.content, encoding="utf-8")
        tmp.replace(path)

        meta_path = chapters_dir / f"第{self.chapter_number:04d}章_meta.json"
        meta = self.model_dump()
        meta.pop("content", None)
        tmp_meta = meta_path.with_suffix('.json.tmp')
        tmp_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_meta.replace(meta_path)

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
            except Exception:
                pass
        return sorted(chapters, key=lambda x: x.chapter_number)
