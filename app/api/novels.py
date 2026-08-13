"""小说项目管理API路由"""
import re
import logging
from pathlib import Path
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, Field
from typing import Optional
from app.models.novel import NovelProject, CorePromptModules, CharacterCard
from app.core.config import validate_novel_id

logger = logging.getLogger(__name__)

UPLOAD_SIZE_LIMIT = 50 * 1024 * 1024  # 50MB


def _strip_ai_preamble(text: str) -> str:
    """去除AI响应中常见的前导说明文字"""
    # 匹配常见的前导模式：从开头到第一个换行或冒号后的内容
    patterns = [
        r'^(好的[，,。.]?\s*)',
        r'^(已[根据更新为您完成].{5,50}[：:。\n])',
        r'^(以下是.{3,30}[：:。\n])',
        r'^(根据.{5,50}[：:。\n])',
        r'^(以下是更新后的.{3,30}[：:。\n])',
        r'^(已为您.{5,30}[：:。\n])',
    ]
    result = text
    for p in patterns:
        result = re.sub(p, '', result, count=1, flags=re.DOTALL)
    return result.strip()

router = APIRouter(prefix="/api/novels", tags=["novels"])


class NovelCreateRequest(BaseModel):
    id: str = Field(..., pattern=r'^[a-zA-Z0-9_-]{1,50}$')
    title: str
    description: str = ""
    base_word_count: int = 3000


class NovelUpdateRequest(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = None
    base_word_count: Optional[int] = Field(default=None, gt=0, le=100000)
    key_chapter_ratio: Optional[float] = Field(default=None, gt=0, le=10)
    turning_chapter_ratio: Optional[float] = Field(default=None, gt=0, le=10)
    export_keep_chapter_number: Optional[bool] = None
    export_keep_chapter_title: Optional[bool] = None


class CorePromptUpdateRequest(BaseModel):
    basic_setting: Optional[str] = None
    character_cards: Optional[list[dict]] = None
    plot_overview: Optional[str] = None
    writing_style: Optional[str] = None
    continuation_direction: Optional[str] = None


class ImportRequest(BaseModel):
    title: str
    novel_id: str = ""


@router.get("")
async def list_novels():
    """获取所有小说项目列表"""
    projects = NovelProject.list_all()
    return {
        "novels": [
            {
                "id": p.id,
                "title": p.title,
                "description": p.description,
                "total_planned": p.total_planned,
                "total_finalized": p.total_finalized,
                "updated_at": p.updated_at,
            }
            for p in projects
        ]
    }


@router.post("")
async def create_novel(req: NovelCreateRequest):
    """创建新小说项目"""
    existing = NovelProject.load(req.id)
    if existing:
        raise HTTPException(400, f"项目ID '{req.id}' 已存在")

    project = NovelProject(
        id=req.id,
        title=req.title,
        description=req.description,
        base_word_count=req.base_word_count,
    )
    project.save()
    return {"message": "创建成功", "novel_id": project.id}


@router.get("/{novel_id}")
async def get_novel(novel_id: str):
    """获取小说项目详情"""
    project = NovelProject.load(novel_id)
    if not project:
        raise HTTPException(404, "项目不存在")
    data = project.model_dump()
    # 计算全书累计字数和已写章节数
    from app.models.chapter import Chapter
    chapters = Chapter.list_for_novel(novel_id)
    data["total_word_count"] = sum(ch.word_count for ch in chapters)
    data["total_written"] = len(chapters)
    return data


@router.put("/{novel_id}")
async def update_novel(novel_id: str, req: NovelUpdateRequest):
    """更新小说项目基本配置"""
    project = NovelProject.load(novel_id)
    if not project:
        raise HTTPException(404, "项目不存在")

    if req.title is not None:
        project.title = req.title
    if req.description is not None:
        project.description = req.description
    if req.base_word_count is not None:
        project.base_word_count = req.base_word_count
    if req.key_chapter_ratio is not None:
        project.key_chapter_ratio = req.key_chapter_ratio
    if req.turning_chapter_ratio is not None:
        project.turning_chapter_ratio = req.turning_chapter_ratio
    if req.export_keep_chapter_number is not None:
        project.export_keep_chapter_number = req.export_keep_chapter_number
    if req.export_keep_chapter_title is not None:
        project.export_keep_chapter_title = req.export_keep_chapter_title

    project.save()
    return {"message": "更新成功"}


@router.delete("/{novel_id}")
async def delete_novel(novel_id: str):
    """删除小说项目"""
    project = NovelProject.load(novel_id)
    if not project:
        raise HTTPException(404, "项目不存在")
    project.delete()
    return {"message": "删除成功"}


@router.put("/{novel_id}/core-prompt")
async def update_core_prompt(novel_id: str, req: CorePromptUpdateRequest):
    """更新核心提示词各模块"""
    try:
        project = NovelProject.load(novel_id)
        if not project:
            raise HTTPException(404, "项目不存在")

        if req.basic_setting is not None:
            project.core_prompt.basic_setting = req.basic_setting
        if req.character_cards is not None:
            cards = []
            for c in req.character_cards:
                try:
                    if 'first_chapter' in c and (c['first_chapter'] == '' or c['first_chapter'] is None):
                        c['first_chapter'] = 0
                    cards.append(CharacterCard(**c))
                except Exception as e:
                    logger.warning(f"角色卡片解析失败: {e}")
            project.core_prompt.character_cards = cards
        if req.plot_overview is not None:
            project.core_prompt.plot_overview = req.plot_overview
        if req.writing_style is not None:
            project.core_prompt.writing_style = req.writing_style
        if req.continuation_direction is not None:
            project.core_prompt.continuation_direction = req.continuation_direction

        project.save()
        return {"message": "核心提示词更新成功"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"更新核心提示词失败: {e}", exc_info=True)
        raise HTTPException(500, f"更新失败: {str(e)}")


@router.get("/{novel_id}/core-prompt")
async def get_core_prompt(novel_id: str):
    """获取核心提示词各模块"""
    project = NovelProject.load(novel_id)
    if not project:
        raise HTTPException(404, "项目不存在")
    return project.core_prompt.model_dump()


@router.post("/{novel_id}/import")
async def import_novel_file(novel_id: str, file: UploadFile = File(...)):
    """导入已有小说文件进行AI分析"""
    validate_novel_id(novel_id)
    content = await file.read()
    if len(content) > UPLOAD_SIZE_LIMIT:
        raise HTTPException(413, f"文件过大，最大允许{UPLOAD_SIZE_LIMIT // 1024 // 1024}MB")

    # 尝试多种编码解码
    text = None
    for encoding in ["utf-8", "gbk", "gb2312", "gb18030", "big5", "latin-1"]:
        try:
            text = content.decode(encoding)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        raise HTTPException(400, "无法解码文件，请确保文件为UTF-8或GBK编码")

    # 读取项目
    project = NovelProject.load(novel_id)
    if not project:
        raise HTTPException(404, "项目不存在，请先创建项目")

    # 记录原始文件信息
    original_length = len(text)

    # AI分析
    from app.core.importer import analyze_novel
    try:
        analysis = await analyze_novel(text)
    except Exception as e:
        logger.error(f"导入AI分析失败: {e}", exc_info=True)
        raise HTTPException(500, "导入AI分析失败，请重试")

    # 检查AI返回是否包含错误（原始响应只记日志，不回显给客户端，防止反射注入）
    if analysis.get("error"):
        logger.warning("AI分析返回异常: %s", analysis.get("error"))
        logger.debug("原始响应前500字: %s", str(analysis.get("raw", ""))[:500])
        raise HTTPException(500, "AI分析返回异常，无法解析分析结果，请重试或更换小说文件")

    # 更新核心提示词
    if analysis.get("basic_setting"):
        project.core_prompt.basic_setting = analysis["basic_setting"]
    if analysis.get("character_cards"):
        cards = []
        for c in analysis["character_cards"]:
            try:
                cards.append(CharacterCard(**c))
            except Exception:
                pass  # 跳过格式异常的角色卡片
        project.core_prompt.character_cards = cards
    if analysis.get("plot_overview"):
        project.core_prompt.plot_overview = analysis["plot_overview"]
    if analysis.get("writing_style"):
        project.core_prompt.writing_style = analysis["writing_style"]
    if analysis.get("continuation_direction"):
        project.core_prompt.continuation_direction = analysis["continuation_direction"]

    project.save()

    # 用正则拆分章节并创建已定稿章节
    chapters_created = 0
    from app.core.importer import create_chapters_from_regex
    try:
        created = create_chapters_from_regex(novel_id, text)
        chapters_created = len(created)
        project.update_stats()
        project.save()
    except Exception as e:
        logger.warning(f"章节拆分失败: {e}")

    return {
        "message": f"导入分析完成，已创建{chapters_created}个章节" if chapters_created else "导入分析完成，请审核AI生成的结构化信息",
        "original_length": original_length,
        "chapters_created": chapters_created,
        "analysis": analysis,
    }


@router.post("/{novel_id}/style-sample")
async def upload_style_sample(novel_id: str, file: UploadFile = File(...)):
    """上传文风样本"""
    validate_novel_id(novel_id)
    from app.core.config import get_novel_subdirs
    dirs = get_novel_subdirs(novel_id)

    content = await file.read()
    if len(content) > UPLOAD_SIZE_LIMIT:
        raise HTTPException(413, f"文件过大，最大允许{UPLOAD_SIZE_LIMIT // 1024 // 1024}MB")

    # 防止路径遍历：只取文件名部分
    safe_name = Path(file.filename).name
    if not safe_name:
        raise HTTPException(400, "无效的文件名")

    path = dirs["style_samples"] / safe_name
    path.write_bytes(content)

    return {"message": f"文风样本 '{safe_name}' 上传成功"}


@router.get("/{novel_id}/style-samples")
async def list_style_samples(novel_id: str):
    """获取文风样本列表"""
    from app.core.config import get_novel_subdirs
    dirs = get_novel_subdirs(novel_id)
    samples_dir = dirs["style_samples"]

    samples = []
    for f in sorted(samples_dir.iterdir()):
        if f.is_file():
            samples.append({
                "name": f.name,
                "size": f.stat().st_size,
            })
    return {"samples": samples}


# ========== 导出功能 ==========

def _content_disposition(filename: str) -> str:
    """生成支持中文文件名的Content-Disposition头"""
    from urllib.parse import quote
    encoded = quote(filename)
    return f"attachment; filename*=UTF-8''{encoded}"


@router.get("/{novel_id}/export/chapter/{chapter_number}")
async def export_chapter(novel_id: str, chapter_number: int):
    """导出单个章节为txt"""
    from fastapi.responses import Response
    from app.models.chapter import Chapter

    project = NovelProject.load(novel_id)
    if not project:
        raise HTTPException(404, "项目不存在")

    chapter = Chapter.load(novel_id, chapter_number)
    if not chapter:
        raise HTTPException(404, "章节不存在")

    # 构建标题行
    header_parts = []
    if project.export_keep_chapter_number:
        header_parts.append(f"第{chapter.chapter_number}章")
    if project.export_keep_chapter_title:
        header_parts.append(chapter.title)
    header = " ".join(header_parts)

    content = f"{header}\n\n{chapter.content}" if header else chapter.content
    filename = f"第{chapter.chapter_number:04d}章_{chapter.title}.txt"

    return Response(
        content=content.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": _content_disposition(filename)}
    )


@router.get("/{novel_id}/export/all")
async def export_all_chapters(novel_id: str):
    """导出所有已定稿章节为合并txt"""
    from fastapi.responses import Response
    from app.models.chapter import Chapter

    project = NovelProject.load(novel_id)
    if not project:
        raise HTTPException(404, "项目不存在")

    chapters = Chapter.list_for_novel(novel_id, load_content=True)
    finalized = [c for c in chapters if c.is_finalized]

    if not finalized:
        finalized = chapters

    parts = [f"《{project.title}》\n\n"]
    for ch in finalized:
        # 构建章节标题行
        header_parts = []
        if project.export_keep_chapter_number:
            header_parts.append(f"第{ch.chapter_number}章")
        if project.export_keep_chapter_title:
            header_parts.append(ch.title)
        header = " ".join(header_parts)

        if header:
            parts.append(f"{header}\n\n{ch.content}\n\n{'='*50}\n\n")
        else:
            parts.append(f"{ch.content}\n\n{'='*50}\n\n")

    content = "".join(parts)
    filename = f"{project.title}_全部章节.txt"

    return Response(
        content=content.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": _content_disposition(filename)}
    )


# ========== AI更新功能 ==========

class AIUpdateRequest(BaseModel):
    scope: str  # "characters" / "plot_overview" / "all"


@router.post("/{novel_id}/ai-update")
async def ai_update_core_prompt(novel_id: str, req: AIUpdateRequest):
    """AI自动分析已写章节，更新核心提示词"""
    project = NovelProject.load(novel_id)
    if not project:
        raise HTTPException(404, "项目不存在")

    from app.models.chapter import Chapter
    from app.core.api_client import api_client
    from app.core.importer import _extract_json

    chapters = Chapter.list_for_novel(novel_id, load_content=True)
    if not chapters:
        raise HTTPException(400, "暂无已写章节，无法分析更新")

    # 构建已写章节摘要（最近20章详细，更早的压缩）
    recent = chapters[-20:]
    older = chapters[:-20] if len(chapters) > 20 else []
    chapter_texts = []
    for ch in older:
        chapter_texts.append(f"第{ch.chapter_number}章 {ch.title}:\n{ch.content[:500]}")
    for ch in recent:
        chapter_texts.append(f"第{ch.chapter_number}章 {ch.title}:\n{ch.content}")
    all_text = "\n\n".join(chapter_texts)

    # 当前角色列表
    current_chars = [c.model_dump() for c in project.core_prompt.character_cards]

    if req.scope == "characters" or req.scope == "all":
        # AI分析角色
        char_prompt = f"""你是一个小说角色分析专家。请分析以下小说章节内容，提取所有出现过的角色信息。

# 已有角色卡片
{chr(10).join([f"- {c['name']}({c.get('role_level','main')})" for c in current_chars]) if current_chars else "暂无"}

# 章节内容
{all_text}

# 任务
1. 保留所有已有角色，更新其当前状态和角色弧线
2. 新增所有有一定剧情量的角色（有对话、有行动、对剧情有影响）
3. 为每个角色严格按以下标准标注等级（role_level）：
   - "main"：主要角色——贯穿故事始终，对主线剧情有重大影响（通常2-4个）
   - "secondary"：次要角色——有独立剧情线，多次出场，对故事发展有推动作用
   - "marginal"：边缘角色——偶尔出场，功能性角色（如路人、配角、工具人）
4. 根据剧情发展，可以提升或降低角色等级。注意：main角色不应超过5个，大多数角色应为secondary或marginal
5. 为每个角色填写plot_importance（一句话描述该角色在剧情中的具体作用）

请严格按JSON格式输出：
```json
{{
  "character_cards": [
    {{
      "name": "角色名",
      "role_level": "main/secondary/marginal",
      "age": "年龄",
      "appearance": "外貌",
      "personality": "性格",
      "background": "背景",
      "relationships": "人物关系",
      "current_status": "当前状态",
      "arc": "角色弧线",
      "first_chapter": 0,
      "plot_importance": "该角色在剧情中的具体作用和重要程度"
    }}
  ]
}}
```"""

        try:
            char_response = await api_client.chat(char_prompt)
            char_data = _extract_json(char_response)
            if char_data.get("character_cards"):
                new_cards = []
                for c in char_data["character_cards"]:
                    try:
                        new_cards.append(CharacterCard(**c))
                    except Exception:
                        pass
                project.core_prompt.character_cards = new_cards
        except Exception as e:
            if req.scope == "characters":
                raise HTTPException(500, f"角色分析失败: {str(e)}")

    if req.scope == "plot_overview" or req.scope == "all":
        # AI更新剧情概述（增量缓存 + 分段处理）
        from app.models.novel import PlotSegment

        old_overview = project.core_prompt.plot_overview or "暂无"
        cached_segments = project.core_prompt.plot_segments
        total_chapters = len(chapters)

        # 找出已有缓存覆盖到的最新章节号
        cached_end = max((s.end_chapter for s in cached_segments), default=0)

        # 新章节（未被缓存覆盖的）
        new_chapters = [ch for ch in chapters if ch.chapter_number > cached_end]

        async def _summarize_new_chapters(new_chs, batch_size, granularity_label):
            """对新章节全文传入，分批总结"""
            results = []
            for i in range(0, len(new_chs), batch_size):
                batch = new_chs[i:i + batch_size]
                start_num = batch[0].chapter_number
                end_num = batch[-1].chapter_number
                seg_text = "\n\n".join(
                    f"第{ch.chapter_number}章 {ch.title}:\n{ch.content}"
                    for ch in batch
                )
                prompt = f"""你是一个小说剧情概述专家。请根据以下章节的完整内容，生成剧情概述。

# 章节范围：第{start_num}-{end_num}章（共{len(batch)}章）
# 章节内容
{seg_text}

# 概述要求
{granularity_label}，按以下格式输出：

### 第X-Y章：[阶段主题标题]
[密集叙事体概述，保留所有关键细节：人名、具体行为、数值、技能名称、人物状态变化。连贯叙事，非逐章摘要。]

# 重要
- 直接输出概述正文，第一个字就是"###"
- 不要输出前导说明、问候语
- 每段之间空一行"""

                response = await api_client.chat(prompt)
                results.append(PlotSegment(
                    start_chapter=start_num,
                    end_chapter=end_num,
                    summary=_strip_ai_preamble(response),
                ))
            return results

        async def _recompress_old_segments(old_segments, batch_size, granularity_label):
            """对已有缓存摘要进行二次压缩（不重新读取原文）"""
            results = []
            for i in range(0, len(old_segments), batch_size):
                batch = old_segments[i:i + batch_size]
                start_num = batch[0].start_chapter
                end_num = batch[-1].end_chapter
                summaries_text = "\n\n".join(
                    f"[第{s.start_chapter}-{s.end_chapter}章的概述]\n{s.summary}"
                    for s in batch
                )
                prompt = f"""你是一个小说剧情概述专家。请将以下已有的分段概述合并压缩。

# 已有概述（共{len(batch)}段，覆盖第{start_num}-{end_num}章）
{summaries_text}

# 压缩要求
将上述概述合并，{granularity_label}，按以下格式输出：

### 第X-Y章：[阶段主题标题]
[密集叙事体概述，保留关键细节，不要丢失重要信息。]

# 重要
- 直接输出概述正文，第一个字就是"###"
- 不要输出前导说明、问候语"""

                response = await api_client.chat(prompt)
                results.append(PlotSegment(
                    start_chapter=start_num,
                    end_chapter=end_num,
                    summary=_strip_ai_preamble(response),
                ))
            return results

        try:
            if total_chapters <= 50:
                # ≤50章：一次性处理，全文传入
                all_text_full = "\n\n".join(
                    f"第{ch.chapter_number}章 {ch.title}:\n{ch.content}"
                    for ch in chapters
                )
                plot_prompt = f"""你是一个小说剧情概述专家。请根据已写章节的完整内容，生成结构化的剧情概述。

# 当前剧情概述
{old_overview}

# 章节内容（共{total_chapters}章）
{all_text_full}

# 分层压缩规则（严格遵守）
- ≤20章：每5章左右总结为一段
- 21-50章：每8章左右总结为一段

# 输出格式

### 第X-Y章：[阶段主题标题]
[密集叙事体概述，保留所有关键细节：人名、具体行为、数值、技能名称、人物状态变化。连贯叙事，非逐章摘要。]

# 重要要求
- 直接输出概述正文，第一个字就是"###"
- 不要输出前导说明、问候语、解释性文字
- 每段之间空一行分隔"""

                plot_response = await api_client.chat(plot_prompt)
                project.core_prompt.plot_overview = _strip_ai_preamble(plot_response)
                # ≤50章不缓存分段，直接用全文结果

            else:
                # >50章：增量缓存 + 分段处理
                recent_count = 20
                recent_chapters = chapters[-recent_count:]
                early_chapters = chapters[:-recent_count]

                # 确定压缩参数
                if total_chapters <= 100:
                    early_batch_size = 12
                    early_granularity = "每12章左右总结为一段"
                else:
                    early_batch_size = 20
                    early_granularity = "每20章左右总结为一段"

                # === 第一步：处理早期章节 ===
                # 找出早期中未被缓存覆盖的新章节
                early_new = [ch for ch in early_chapters if ch.chapter_number > cached_end]
                # 找出仍有效的旧缓存：起点落在早期范围内的段（含跨越边界的段）都归早期，
                # 保证所有缓存段都被重新处理，不会出现覆盖缺口
                early_boundary = early_chapters[-1].chapter_number
                early_cached = [
                    s for s in cached_segments
                    if s.start_chapter <= early_boundary
                ]

                early_segments = []
                if early_cached:
                    # 对旧缓存进行二次压缩
                    recompressed = await _recompress_old_segments(
                        early_cached, early_batch_size, early_granularity
                    )
                    early_segments.extend(recompressed)

                if early_new:
                    # 对新章节全文总结
                    new_summaries = await _summarize_new_chapters(
                        early_new, early_batch_size, early_granularity
                    )
                    early_segments.extend(new_summaries)

                # === 第二步：处理近期章节（最近20章，每5章一批，全文传入）===
                # 近期章节始终重新总结（因为颗粒度更细）
                recent_new = [ch for ch in recent_chapters if ch.chapter_number > cached_end]
                recent_old_cached = [
                    s for s in cached_segments
                    if s.start_chapter > early_boundary
                ]

                recent_segments = []
                if recent_old_cached:
                    # 对近期旧缓存二次压缩（5章粒度）
                    recompressed = await _recompress_old_segments(
                        recent_old_cached, 5, "每5章左右总结为一段"
                    )
                    recent_segments.extend(recompressed)

                if recent_new:
                    new_summaries = await _summarize_new_chapters(
                        recent_new, 5, "每5章左右总结为一段"
                    )
                    recent_segments.extend(new_summaries)

                # === 第三步：合并 ===
                all_new_segments = early_segments + recent_segments
                all_new_segments.sort(key=lambda s: s.start_chapter)

                project.core_prompt.plot_segments = all_new_segments
                project.core_prompt.plot_overview = "\n\n".join(
                    s.summary for s in all_new_segments
                )

        except Exception as e:
            if req.scope == "plot_overview":
                raise HTTPException(500, f"剧情概述更新失败: {str(e)}")

    project.save()

    return {
        "message": "AI更新完成",
        "updated_scope": req.scope,
        "character_count": len(project.core_prompt.character_cards),
        "plot_overview_length": len(project.core_prompt.plot_overview),
    }
