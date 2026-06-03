"""小说导入与AI分析模块"""
import json
import re
import logging
from app.core.api_client import api_client
from app.core.prompt_builder import build_import_analysis_prompt
from app.models.novel import NovelProject, CorePromptModules, CharacterCard

logger = logging.getLogger(__name__)


async def analyze_novel(novel_text: str) -> dict:
    """调用AI分析小说内容，返回结构化信息"""
    logger.info(f"开始分析小说，原文长度: {len(novel_text)} 字符")

    prompt = build_import_analysis_prompt(novel_text)
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


def split_chapters_regex(novel_text: str) -> list[dict]:
    """用正则表达式拆分小说文本为章节列表，返回 [{chapter_number, title, start_position}]"""
    # 中文数字映射
    cn_nums = {'一':1,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10,
               '十一':11,'十二':12,'十三':13,'十四':14,'十五':15,'十六':16,'十七':17,'十八':18,'十九':19,'二十':20,
               '二十一':21,'二十二':22,'二十三':23,'二十四':24,'二十五':25,'二十六':26,'二十七':27,'二十八':28,'二十九':29,'三十':30,
               '三十一':31,'三十二':32,'三十三':33,'三十四':34,'三十五':35,'三十六':36,'三十七':37,'三十八':38,'三十九':39,'四十':40,
               '四十一':41,'四十二':42,'四十三':43,'四十四':44,'四十五':45,'四十六':46,'四十七':47,'四十八':48,'四十九':49,'五十':50}

    # 匹配 "第X章" 或 "第X节" 模式，X可以是中文数字或阿拉伯数字
    # 也匹配纯数字开头如 "1、" "1." 以及 "Chapter X"
    pattern = r'^(第[零一二三四五六七八九十百千万\d]+[章节回幕卷]|Chapter\s*\d+|\d+[、.．]\s*)'
    lines = novel_text.split('\n')

    chapters = []
    pos = 0  # 跟踪当前行在原文中的位置
    for line in lines:
        stripped = line.strip()
        line_start = pos
        pos += len(line) + 1  # +1 for the \n

        if not stripped:
            continue
        m = re.match(pattern, stripped, re.IGNORECASE)
        if m:
            matched = m.group(1)
            title = stripped
            ch_num = len(chapters) + 1
            num_match = re.search(r'第([零一二三四五六七八九十百千万\d]+)[章节回幕卷]', matched)
            if num_match:
                num_str = num_match.group(1)
                if num_str.isdigit():
                    ch_num = int(num_str)
                elif num_str in cn_nums:
                    ch_num = cn_nums[num_str]
            elif re.match(r'Chapter\s*(\d+)', matched, re.IGNORECASE):
                ch_num = int(re.match(r'Chapter\s*(\d+)', matched, re.IGNORECASE).group(1))
            elif re.match(r'(\d+)[、.．]', matched):
                ch_num = int(re.match(r'(\d+)[、.．]', matched).group(1))

            chapters.append({"chapter_number": ch_num, "title": title, "start_position": line_start})

    # 如果没有找到任何章节标记，将整篇文本作为单章
    if not chapters:
        chapters.append({"chapter_number": 1, "title": "", "start_position": 0})

    # 修正：确保start_position是唯一的，按位置排序后重新编号
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
