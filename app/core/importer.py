"""小说导入与AI分析模块"""
import json
import re
import logging
from app.core.api_client import api_client
from app.core.prompt_builder import build_import_analysis_prompt
from app.models.novel import NovelProject, CorePromptModules, CharacterCard

logger = logging.getLogger(__name__)

# 送入AI分析的字符上限：避免几MB的小说全文直发LLM导致失败/巨额费用
MAX_ANALYSIS_CHARS = 60_000


def _sample_long_text(text: str, max_chars: int = MAX_ANALYSIS_CHARS) -> str:
    """长文本采样：保留开头+结尾+均匀抽取10段，控制送入AI的量"""
    if len(text) <= max_chars:
        return text
    head = max_chars // 3
    tail = max_chars // 3
    middle = text[head:-tail]
    samples = []
    mid_budget = max_chars - head - tail
    if middle and mid_budget > 0:
        sample_size = mid_budget // 10
        if sample_size > 0:
            step = max(len(middle) // 10, 1)
            for i in range(10):
                start = i * step
                samples.append(middle[start:start + sample_size])
    parts = [text[:head], "\n\n...（中间内容省略）...\n\n"]
    if samples:
        parts.append("\n\n...（中间内容省略）...\n\n".join(samples))
        parts.append("\n\n...（中间内容省略）...\n\n")
    parts.append(text[-tail:])
    return "".join(parts)


async def analyze_novel(novel_text: str) -> dict:
    """调用AI分析小说内容，返回结构化信息"""
    logger.info(f"开始分析小说，原文长度: {len(novel_text)} 字符")

    sampled = _sample_long_text(novel_text)
    if len(sampled) < len(novel_text):
        logger.info(f"原文过长，已采样至 {len(sampled)} 字符后再分析")

    prompt = build_import_analysis_prompt(sampled)
    logger.info(f"提示词长度: {len(prompt)} 字符")

    try:
        response = await api_client.chat(prompt)
        logger.info(f"AI响应长度: {len(response)} 字符")
        logger.debug(f"AI响应前500字: {response[:500]}")
    except Exception as e:
        logger.error(f"AI API调用失败: {e}")
        return {"error": f"AI API调用失败: {str(e)}", "raw": ""}

    # 尝试从响应中提取JSON
    result = _extract_json(response)

    if result.get("error"):
        logger.warning(f"JSON解析失败: {result.get('error')}")
        logger.debug(f"原始响应: {response[:1000]}")
    else:
        logger.info(f"分析成功: 角色{len(result.get('character_cards', []))}个, "
                    f"剧情概述{len(result.get('plot_overview', ''))}字")

    return result


async def import_novel(title: str, novel_text: str, novel_id: str = None) -> NovelProject:
    """导入小说：分析内容 → 创建项目"""
    if not novel_id:
        novel_id = title.replace(" ", "_").replace("/", "_")[:50]

    analysis = await analyze_novel(novel_text)

    character_cards = []
    for cc in analysis.get("character_cards", []):
        try:
            character_cards.append(CharacterCard(**cc))
        except Exception as e:
            logger.warning(f"角色卡片解析失败: {cc}, 错误: {e}")

    project = NovelProject(
        id=novel_id,
        title=title,
        core_prompt=CorePromptModules(
            basic_setting=analysis.get("basic_setting", ""),
            character_cards=character_cards,
            plot_overview=analysis.get("plot_overview", ""),
            writing_style=analysis.get("writing_style", ""),
            continuation_direction=analysis.get("continuation_direction", ""),
        ),
    )

    project.save()
    return project


# 强章节标记：命中即视为标题（这些形态在正文中几乎不会出现在行首）
# 包裹符号：书名号《》、方头括号【】、六角括号〔〕、双尖括号〖〗、方括号［］[]
# 注意：以下三个片段都是"完整字符类"，不可再嵌进别的字符类（否则会多出一个字面 ]）
_WRAP_OPEN = r'[《【〔〖［\[]'          # 开括号
_WRAP_CLOSE = r'[》】〕〗］\]]'          # 闭括号
_WRAP_NOT_CLOSE = r'[^》】〕〗］\]]'     # 非闭括号字符（手写，避免嵌套字符类）
_CN_NUM = r'[0-9０-９零一二三四五六七八九十百千万两]{1,6}'
_STRONG_HEADING_PATTERNS = (
    # 【书名】（12）标题 / 《书名》(12)标题 —— 中文网文常见的"书名/卷名 + 括号编号"式分章
    re.compile(rf'^{_WRAP_OPEN}\s*{_WRAP_NOT_CLOSE}{{1,60}}\s*{_WRAP_CLOSE}\s*[（(]\s*[0-9０-９]{{1,4}}\s*[）)]'),
    # 【第12章】标题 —— 包裹式的"第X章"
    re.compile(rf'^{_WRAP_OPEN}\s*第\s*{_CN_NUM}\s*[章节回幕卷集篇]'),
    # 第X章 / 第X节 / 第X回 / 第X卷 / 第X部 / 第X集 / 第X篇
    # （"部"排除"部分"，避免把"第一部分内容"这类行内文字当成标题）
    re.compile(rf'^第\s*{_CN_NUM}\s*(?:[章节回幕卷集篇]|部(?!分))'),
    # Chapter 12
    re.compile(r'^chapter\s*[0-9]{1,4}\b', re.IGNORECASE),
)

# 弱章节标记：形如 "12、标题"，容易与正文（如"４．５左右，不算特别大…"）混淆，
# 需额外加长度与标点限制后才接受
_WEAK_HEADING_PATTERNS = (
    re.compile(r'^[0-9]{1,4}\s*[、.．]\s*\S'),
)

# 出现这些标点说明更像叙述句而非标题
_SENTENCE_MARKS = ('。', '！', '？', '!', '?', '…', '，', ',', '；', ';')


def is_chapter_heading(line: str) -> bool:
    """判断一行是否像章节标题（带正文防误判保护）"""
    text = line.strip()
    if not text or len(text) > 80:
        return False
    if any(p.match(text) for p in _STRONG_HEADING_PATTERNS):
        return True
    # 弱形态：必须很短、且不含任何句读标点，避免把整句正文当成标题
    if len(text) <= 30 and not any(mark in text for mark in _SENTENCE_MARKS):
        return any(p.match(text) for p in _WEAK_HEADING_PATTERNS)
    return False


def _first_line_title(text: str, limit: int = 40) -> str:
    """取文本的第一个非空行作为标题（用于无标记的整篇文本，或首个标记之前的前言）"""
    for line in text.split('\n'):
        candidate = line.strip()
        if candidate:
            return candidate[:limit]
    return "（前言）"


def split_chapters_regex(novel_text: str) -> list[dict]:
    """用正则拆分小说文本为章节列表，返回 [{chapter_number, title, start_position}]

    识别以下章节标记（任一行命中即视为标题）：
      - 【书名】（12）标题 / 《书名》(12)标题 —— 中文网文常见的"书名/卷名 + 括号编号"式分章
      - 第X章 / 第X节 / 第X回 / 第X卷 / 第X部 / 第X集 / 第X篇（中文、阿拉伯、全角数字均可）
      - Chapter 12
      - 12、标题 / 12. 标题

    首个标记之前的内容（书名、作者、前言）会保留为独立一章，不再被丢弃；
    完全没有标记时，整篇作为一章，且绝不用正文句子充当标题。
    """
    lines = novel_text.split('\n')

    headings: list[dict] = []
    pos = 0  # 跟踪当前行在原文中的位置
    for line in lines:
        stripped = line.strip()
        line_start = pos
        pos += len(line) + 1  # +1 for the \n
        if not stripped:
            continue
        if is_chapter_heading(stripped):
            headings.append({"title": stripped, "start_position": line_start})

    # 没有找到任何章节标记：整篇文本作为单章，标题取首行（避免出现空标题或正文句标题）
    if not headings:
        return [{"chapter_number": 1, "title": _first_line_title(novel_text), "start_position": 0}]

    chapters: list[dict] = []
    if headings[0]["start_position"] > 0:
        # 首个标题之前的内容（书名/作者/前言）单独成章，保证正文不丢
        chapters.append({
            "title": _first_line_title(novel_text[:headings[0]["start_position"]]),
            "start_position": 0,
        })
    chapters.extend(headings)

    # 按位置排序后统一重新编号，保证章节号连续（避免原文编号跳号/重号）
    chapters.sort(key=lambda x: x["start_position"])
    for i, ch in enumerate(chapters):
        ch["chapter_number"] = i + 1

    return chapters


def create_chapters_from_regex(novel_id: str, novel_text: str) -> list:
    """用正则拆分小说文本并创建已定稿章节"""
    from app.models.chapter import Chapter

    chapters_data = split_chapters_regex(novel_text)
    logger.info(f"正则拆分识别到 {len(chapters_data)} 个章节")

    created = []
    for i, ch_data in enumerate(chapters_data):
        start = ch_data["start_position"]
        # 结束位置：到下一章节起始，或到文末
        if i + 1 < len(chapters_data):
            end = chapters_data[i + 1]["start_position"]
        else:
            end = len(novel_text)

        content = novel_text[start:end].strip()
        if not content:
            continue

        chapter = Chapter(
            novel_id=novel_id,
            chapter_number=ch_data["chapter_number"],
            title=ch_data["title"],
            content=content,
            is_finalized=True,
        )
        chapter.save()
        created.append(chapter)

    logger.info(f"创建了 {len(created)} 个已定稿章节")
    return created


def _extract_json(text: str) -> dict:
    """从AI响应中提取JSON块，多重容错"""
    # 方法1: 直接解析
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # 方法2: 提取 ```json ... ``` 块
    import re
    pattern = r'```(?:json)?\s*\n?(.*?)\n?```'
    matches = re.findall(pattern, text, re.DOTALL)
    for match in matches:
        try:
            return json.loads(match.strip())
        except json.JSONDecodeError:
            continue

    # 方法3: 找第一个 { 到最后一个 }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start:end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # 方法3b: 尝试修复常见JSON格式问题
            # 移除尾部多余逗号
            fixed = re.sub(r',\s*([}\]])', r'\1', candidate)
            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                pass

    # 方法4: 尝试找多个JSON对象（有些AI会返回多个JSON块）
    json_blocks = re.findall(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
    for block in json_blocks:
        try:
            parsed = json.loads(block)
            if isinstance(parsed, dict) and len(parsed) > 2:
                return parsed
        except json.JSONDecodeError:
            continue

    return {"error": "无法解析AI响应为JSON格式", "raw": text[:2000]}
