from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
import difflib
import json
import logging
import re
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

# 冲突严重度（需求优化）：只有 error 才判定"审计未通过"，
# warning / info 属于写作建议，仅提示而不阻塞定稿。
SEVERITY_LABELS = {
    "error": "错误（须修正）",
    "warning": "警告（建议关注）",
    "info": "提示（写作建议）",
}
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}

# 资源重要度：core/normal 进入提示词，minor 默认只留档
IMPORTANCE_LABELS = {
    "core": "核心",
    "normal": "一般",
    "minor": "次要",
}

# 资源条目状态：tracked=已入库，candidate=待确认（不进入提示词），ignored=已忽略
ITEM_STATES = {"tracked", "candidate", "ignored"}

# 归一化名称时要去掉的装饰性后缀/括注，例如「柳冰冰（状态）」「技能（C级）」
_DECOR_RE = re.compile(r"[（(][^）)]{0,12}[）)]")
_PUNCT_RE = re.compile(r"[\s\u3000·・,，。.、;；:：!！?？\"'“”‘’`~\-_/\\|+*#]")


# 只有这些"纯状态词"才算没有信息量；不能按长度粗暴判断，
# 否则「在手上」「Lv3」这类合法短值会被误杀。
# 注意：必须定义在模块级——pydantic 会把类中以 "_" 开头的属性当作私有属性处理。
_STATUS_ONLY_VALUES = {
    "已获得", "已兑换", "已解锁", "已销毁", "已回收", "已触发",
    "已更新", "已变更", "无", "未知", "暂无",
}


def normalize_resource_name(name: str) -> str:
    """归一化资源名，用于近似重复判定

    「柳冰冰（状态）」「柳冰冰心理状态」「柳冰冰 状态」归一化后都能对齐到「柳冰冰心理状态」
    这一类装饰差异上——但不会把「初级格斗术」和「高级格斗术」判成同一个（保持原文差异）。
    """
    text = _DECOR_RE.sub("", name or "")
    text = _PUNCT_RE.sub("", text)
    return text.lower()


class ResourceItem(BaseModel):
    """单条资源追踪项"""
    category: str  # wealth / item / system / character_status / foreshadow
    name: str
    value: str
    chapter_introduced: int = 0  # 引入的章节号
    chapter_updated: int = 0     # 最后更新的章节号
    status: str = "active"       # active / resolved / destroyed
    notes: str = ""
    # === P1：准入与分层（老数据缺省即按"一般"处理，向后兼容）===
    importance: str = "normal"   # core / normal / minor
    confidence: float = 1.0      # 审计给出的置信度，低于阈值只进候选区
    aliases: list[str] = []      # 归并时保留下来的别名
    state: str = "tracked"       # tracked / candidate / ignored
    source_excerpt: str = ""     # 出处原文片段，便于人工核对
    stale: bool = False          # 长期未更新的伏笔/线索

    def display_name(self) -> str:
        return self.name


class AuditConflict(BaseModel):
    """审计发现的冲突

    P0 扩展：不再只有一句描述，而是带严重度、原文锚点与可执行改写，
    使"根据审计建议微调"成为可能（而不是整章重生成）。
    """
    chapter_number: int
    conflict_type: str  # timeline / character / item / setting / value / foreshadow
    description: str
    suggestion: str = ""
    resolved: bool = False
    resolution: str = ""  # ignore / update_resource / modify_chapter
    notes: str = ""  # 解决备注
    # === P0：可执行化 ===
    severity: str = "warning"      # error / warning / info
    anchor: str = ""               # 需要修改的原文片段（用于段落定位）
    fix_type: str = "none"         # rewrite_paragraph / replace_text / update_resource / none
    suggested_text: str = ""       # fix_type=rewrite_paragraph 时的改写文本
    paragraph_index: int = -1      # 锚点定位到的段落序号（-1 表示未定位）


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

    # ---------- 查找 ----------
    def find_similar(self, category: str, name: str, threshold: float = 0.9) -> Optional[int]:
        """在同分类里找"同一条资源"的既有条目，返回索引（P1 归并）

        判定要保守：只认归一化后完全相等，或相似度够高**且长度相当**。
        仅按子串匹配会把「柳冰冰」和「柳冰冰的纹身」误并成一条（实测会把 9 条
        不同资源压成一组），因此子串只在长度接近时才采纳。
        """
        target = normalize_resource_name(name)
        if not target:
            return None
        best_index, best_ratio = None, 0.0
        for i, r in enumerate(self.resources):
            if r.category != category:
                continue
            if r.name == name or name in r.aliases:
                return i
            other = normalize_resource_name(r.name)
            if not other:
                continue
            if other == target:
                return i
            # 长度差异大说明不是同一个东西（「X」vs「X的纹身」「X状态」）
            len_ratio = min(len(target), len(other)) / max(len(target), len(other))
            if len_ratio < 0.75:
                continue
            ratio = difflib.SequenceMatcher(None, target, other).ratio()
            if ratio > best_ratio:
                best_index, best_ratio = i, ratio
        return best_index if best_ratio >= threshold else None

    def add_resource(self, item: ResourceItem, merge_threshold: float = 0.9) -> str:
        """写入资源，返回动作：added / merged / rejected

        相比此前"只做 (category, name) 精确匹配"，现在增加了：
        - 近似重名归并（保留别名，避免「柳冰冰（状态）」和「柳冰冰心理状态」并存）
        - 低信息量拒收（值为空或仅"已获得"这类无内容描述）
        - 候选区（state=candidate 不进入提示词，等待人工确认）
        """
        if not item.name.strip():
            return "rejected"

        index = self.find_similar(item.category, item.name, merge_threshold)
        if index is not None:
            r = self.resources[index]
            if item.name != r.name and item.name not in r.aliases:
                r.aliases.append(item.name)
            if item.value:
                r.value = item.value
            if item.chapter_introduced:
                r.chapter_introduced = r.chapter_introduced or item.chapter_introduced
            if item.chapter_updated:
                r.chapter_updated = item.chapter_updated
            if item.notes:
                r.notes = item.notes
            if item.importance == "core":
                r.importance = "core"  # 升级不回退
            if item.confidence > r.confidence:
                r.confidence = item.confidence
            if item.state == "tracked" and r.state == "candidate":
                r.state = "tracked"
            if item.status == "destroyed":
                r.status = "destroyed"
            elif r.status != "destroyed":
                # 已销毁的资源再次出现属于冲突，交由审计报告提示，不在此静默复活
                r.status = item.status
            return "merged"

        # 只有新建条目才做信息量准入（更新已有条目时允许短值，如"在手上"）
        if self._is_low_information(item):
            return "rejected"

        self.resources.append(item)
        return "added"

    # 只有这些"纯状态词"才算没有信息量；不能按长度粗暴判断，
    # 否则「在手上」「Lv3」这类合法短值会被误杀。
    @staticmethod
    def _is_low_information(item: ResourceItem) -> bool:
        """低信息量判定：这类条目会把资源表撑成流水账，拦在入库前

        注意：销毁/回收记录允许没有描述（"已销毁"本身就是全部信息）。
        """
        value = (item.value or "").strip()
        if not value:
            return item.status != "destroyed"
        plain = _PUNCT_RE.sub("", value)
        if plain in _STATUS_ONLY_VALUES:
            return True
        # 单字描述基本无意义（"无""是"），两个字符以上（"Lv3""在手上"）放行
        return len(plain) < 2

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

    # ---------- 提示词摘要（P1：分层 + 体积上限）----------
    def get_summary_for_prompt(self, max_chars: int = 4000,
                               chapter_number: Optional[int] = None,
                               related_names: Optional[list[str]] = None) -> str:
        """生成资源摘要，用于嵌入提示词

        此前无条件把所有条目（实测 186 条 = 12976 字符，占审计提示词 49%）塞进去。
        现在按重要度分层，并做体积上限截断：
        - core 一定带；normal 次之；minor / candidate / ignored 不带（仅留档）
        - 给 chapter_number 时优先带最近更新过的条目
        - related_names 命中的条目优先（本章相关）
        """
        tracked = [r for r in self.resources if r.state == "tracked"]
        if not tracked:
            return ""

        related = {normalize_resource_name(n) for n in (related_names or []) if n}

        def priority(r: ResourceItem):
            imp = {"core": 0, "normal": 1, "minor": 2}.get(r.importance, 1)
            is_related = 0 if normalize_resource_name(r.name) in related else 1
            # 最近章节更新过的优先（负数越小越靠前）
            recent = -(r.chapter_updated or 0)
            return (is_related, imp, recent)

        ordered = sorted(tracked, key=priority)

        lines = ["## 当前资源追踪状态（仅列核心与一般条目）\n"]
        used = len(lines[0])

        grouped: dict[str, list[ResourceItem]] = {}
        for r in ordered:
            grouped.setdefault(r.category, []).append(r)

        # 分类顺序：5 个标准分类在前，模型外分类追加在后
        cat_order: list[tuple[str, str]] = []
        for cat_key, cat_name in RESOURCE_CATEGORIES.items():
            if grouped.get(cat_key):
                cat_order.append((cat_key, cat_name))
        for cat_key in sorted(grouped):
            if cat_key not in RESOURCE_CATEGORIES and grouped[cat_key]:
                cat_order.append((cat_key, f"{cat_key}（未归类）"))

        def format_line(item: ResourceItem) -> str:
            status_mark = "" if item.status == "active" else f" [{item.status}]"
            stale_mark = "（久未跟进）" if item.stale else ""
            alias = f"（别名：{'、'.join(item.aliases[:2])}）" if item.aliases else ""
            return f"- {item.name}{alias}：{item.value}{status_mark}{stale_mark}"

        # ---------- 第一轮：每个分类至少保留一条，避免整类被预算挤掉 ----------
        # （此前是顺序填充，排在后面的未归类分类会被整体丢弃）
        emitted: dict[str, list[str]] = {}
        cursor: dict[str, int] = {}
        truncated_categories: dict[str, int] = {}
        for cat_key, cat_name in cat_order:
            items = grouped[cat_key]
            header = f"### {cat_name}"
            first = format_line(items[0])
            if used + len(header) + len(first) + 3 > max_chars:
                # 预算实在不够，只写分类名与条数，至少让模型知道这个分类存在
                lines.append(f"{header}（共 {len(items)} 条，篇幅所限未列出）")
                used += len(lines[-1]) + 1
                emitted[cat_key] = []
                cursor[cat_key] = len(items)
                continue
            lines.append(header)
            lines.append(first)
            used += len(header) + len(first) + 2
            emitted[cat_key] = [first]
            cursor[cat_key] = 1

        lines.append("")

        # ---------- 第二轮：按优先级轮转填充剩余预算 ----------
        progressed = True
        while progressed and used < max_chars:
            progressed = False
            for cat_key, _cat_name in cat_order:
                items = grouped[cat_key]
                idx = cursor[cat_key]
                if idx >= len(items):
                    continue
                line = format_line(items[idx])
                if used + len(line) + 1 > max_chars:
                    truncated_categories[cat_key] = len(items) - idx
                    cursor[cat_key] = len(items)
                    continue
                lines.append(line)
                used += len(line) + 1
                cursor[cat_key] = idx + 1
                progressed = True

        for cat_key, _cat_name in cat_order:
            remaining = len(grouped[cat_key]) - cursor[cat_key]
            if remaining > 0:
                truncated_categories[cat_key] = remaining

        if truncated_categories:
            total_hidden = sum(truncated_categories.values())
            lines.append("")
            lines.append(
                f"（另有 {total_hidden} 条因篇幅限制未列出，完整内容见资源表）"
            )

        # 未解决冲突：在剩余预算内附加
        unresolved = self.get_unresolved_conflicts()
        if unresolved and used < max_chars:
            lines.append("")
            lines.append("### 未解决冲突")
            used += len(lines[-1]) + 1
            shown = 0
            for c in unresolved[:10]:
                line = f"- 第{c.chapter_number}章：{c.description}"
                if used + len(line) + 1 > max_chars:
                    break
                lines.append(line)
                used += len(line) + 1
                shown += 1
            if shown < len(unresolved):
                lines.append(f"- （另有 {len(unresolved) - shown} 条未解决冲突）")

        return "\n".join(lines)

    # ---------- 体检与整理（P1）----------
    def find_duplicates(self, threshold: float = 0.9) -> list[dict]:
        """列出近似重名分组，供"资源表体检"与一键合并使用

        与 find_similar 同样的保守规则：归一化完全相等，或"相似度够高且长度相当"。
        不做子串包含匹配，否则「柳冰冰」会把所有带她名字的条目都吞进去。
        """
        names = [(i, r) for i, r in enumerate(self.resources) if r.state != "ignored"]
        groups: list[list[int]] = []
        assigned: set[int] = set()

        for i, r_i in enumerate(names):
            idx_i = r_i[0]
            if idx_i in assigned:
                continue
            group = [idx_i]
            norm_i = normalize_resource_name(r_i[1].name)
            if not norm_i:
                continue
            for j in range(i + 1, len(names)):
                idx_j, r_j = names[j]
                if idx_j in assigned or r_j.category != r_i[1].category:
                    continue
                norm_j = normalize_resource_name(r_j.name)
                if not norm_j:
                    continue
                if norm_i != norm_j:
                    len_ratio = min(len(norm_i), len(norm_j)) / max(len(norm_i), len(norm_j))
                    if len_ratio < 0.75:
                        continue
                    ratio = difflib.SequenceMatcher(None, norm_i, norm_j).ratio()
                    if ratio < threshold:
                        continue
                group.append(idx_j)
                assigned.add(idx_j)
            if len(group) > 1:
                assigned.add(idx_i)
                groups.append(group)

        result = []
        for g in groups:
            items = [self.resources[i] for i in g]
            result.append({
                "indices": g,
                "category": items[0].category,
                "names": [x.name for x in items],
                "suggested_keep": max(g, key=lambda i: (len(self.resources[i].value),
                                                        self.resources[i].chapter_updated)),
            })
        return result

    def health_report(self) -> dict:
        """资源表体检：近似重名 / 低信息量 / 长期未跟进 / 候选区积压"""
        low_info = []
        stale = []
        candidates = []
        for i, r in enumerate(self.resources):
            if r.state == "candidate":
                candidates.append({"index": i, "name": r.name, "value": r.value})
            if r.state == "ignored":
                continue
            if self._is_low_information(r):
                low_info.append({"index": i, "name": r.name, "value": r.value,
                                 "category": r.category})
            if r.stale:
                stale.append({"index": i, "name": r.name, "category": r.category,
                              "chapter_updated": r.chapter_updated})
        return {
            "total": len(self.resources),
            "tracked": len([r for r in self.resources if r.state == "tracked"]),
            "candidates": candidates,
            "duplicate_groups": self.find_duplicates(),
            "low_information": low_info,
            "stale": stale,
        }

    def merge_resources(self, keep_index: int, drop_indices: list[int]) -> bool:
        """把若干条合并到 keep_index：保留别名、挑选最合适的值、保留更早的引入章

        取值规则（按分类语义区分，而不是一律取最长）：
        - character_status：状态是"快照"，**最新章节**的描述覆盖旧描述（旧状态已被剧情推翻）；
          但若最新那条明显更短（少于被合并条目的一半），说明它是精简记录，改用更全的
        - 其它分类：保留**信息量最大**（最长）的描述，避免丢细节
        """
        if keep_index < 0 or keep_index >= len(self.resources):
            return False
        keep = self.resources[keep_index]
        is_status = keep.category == "character_status"
        merged_from: list[ResourceItem] = []

        for idx in sorted(set(drop_indices), reverse=True):
            if idx == keep_index or idx < 0 or idx >= len(self.resources):
                continue
            drop = self.resources[idx]
            if drop.name != keep.name and drop.name not in keep.aliases:
                keep.aliases.append(drop.name)
            for alias in drop.aliases:
                if alias not in keep.aliases and alias != keep.name:
                    keep.aliases.append(alias)

            if drop.value:
                if is_status:
                    newer = drop.chapter_updated >= keep.chapter_updated
                    much_shorter = len(drop.value) * 2 < len(keep.value)
                    if newer and not much_shorter:
                        keep.value = drop.value
                        keep.chapter_updated = drop.chapter_updated
                    elif not newer and len(drop.value) > len(keep.value):
                        keep.value = drop.value
                elif len(drop.value) > len(keep.value):
                    keep.value = drop.value

            if drop.chapter_introduced and (
                not keep.chapter_introduced or drop.chapter_introduced < keep.chapter_introduced
            ):
                keep.chapter_introduced = drop.chapter_introduced
            keep.chapter_updated = max(keep.chapter_updated, drop.chapter_updated)
            if drop.importance == "core":
                keep.importance = "core"
            if len(drop.source_excerpt or "") > len(keep.source_excerpt or ""):
                keep.source_excerpt = drop.source_excerpt
            merged_from.append(drop)
            self.resources.pop(idx)
            if idx < keep_index:
                keep_index -= 1
        return True
