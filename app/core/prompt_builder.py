"""结构化提示词构建器
根据不同的任务类型，将核心提示词各模块组合成完整的提示词
"""
from app.models.novel import NovelProject
from app.models.resource import ResourceTracker


def build_system_prompt(project: NovelProject) -> str:
    """构建系统级提示词（基础设定+文风）"""
    parts = []
    parts.append("你是一个专业的小说写作AI，擅长续写各类风格的小说。\n")

    if project.core_prompt.basic_setting:
        parts.append(f"# 基础设定\n{project.core_prompt.basic_setting}\n")

    if project.core_prompt.writing_style:
        parts.append(f"# 文风设定\n{project.core_prompt.writing_style}\n")

    return "\n".join(parts)


def build_character_prompt(project: NovelProject) -> str:
    """构建角色卡片提示词"""
    if not project.core_prompt.character_cards:
        return ""
    parts = ["# 人物设定\n"]
    for c in project.core_prompt.character_cards:
        card = f"## {c.name}\n"
        if c.age:
            card += f"- 年龄：{c.age}\n"
        if c.appearance:
            card += f"- 外貌：{c.appearance}\n"
        if c.personality:
            card += f"- 性格：{c.personality}\n"
        if c.background:
            card += f"- 背景：{c.background}\n"
        if c.relationships:
            card += f"- 关系：{c.relationships}\n"
        if c.current_status:
            card += f"- 当前状态：{c.current_status}\n"
        if c.arc:
            card += f"- 角色弧线：{c.arc}\n"
        parts.append(card)
    return "\n".join(parts)


def build_plot_overview_prompt(project: NovelProject) -> str:
    """构建剧情概述提示词"""
    if not project.core_prompt.plot_overview:
        return ""
    return f"# 剧情概述\n{project.core_prompt.plot_overview}\n"


def build_continuation_prompt(project: NovelProject) -> str:
    """构建续写方向提示词"""
    if not project.core_prompt.continuation_direction:
        return ""
    return f"# 续写方向\n{project.core_prompt.continuation_direction}\n"


def build_planning_prompt(project: NovelProject, last_chapter_content: str,
                          start_chapter: int, num_chapters: int,
                          direction_hint: str = "") -> str:
    """构建章节规划的完整提示词"""
    parts = []

    # 核心设定
    parts.append(build_system_prompt(project))
    parts.append(build_character_prompt(project))
    parts.append(build_plot_overview_prompt(project))
    parts.append(build_continuation_prompt(project))

    # 章节规划指令
    word_counts = []
    for i in range(num_chapters):
        ch_num = start_chapter + i
        word_counts.append(f"第{ch_num}章：基准{project.base_word_count}字")

    parts.append(f"""
# 章节规划任务

请规划未来{num_chapters}个章节（第{start_chapter}-{start_chapter + num_chapters - 1}章）的故事发展方向。

## 字数要求
- 普通章节：{project.base_word_count}字
- 重点章节：{int(project.base_word_count * project.key_chapter_ratio)}字（基准的130%）
- 转折章节：{int(project.base_word_count * project.turning_chapter_ratio)}字（基准的150%）

请为每章标注类型（普通/重点/转折）。

## 输出格式要求
请严格按照以下JSON格式输出，不要输出其他内容：

```json
{{
  "chapters": [
    {{
      "chapter_number": {start_chapter},
      "title": "章节标题",
      "chapter_type": "normal",
      "word_count_target": {project.base_word_count},
      "time": "时间描述",
      "scene": "场景描述",
      "core_plot": ["情节要点1", "情节要点2"],
      "prompt": "写作指导：只写具体的情节走向、人物互动、情感变化、对话要点、描写重点等创作指导，不要重复章节标题、时间、场景等已有的元信息"
    }}
  ]
}}
```
""")

    # 方向提示
    if direction_hint:
        parts.append(f"\n## 写作方向要求\n{direction_hint}\n")

    # 上一章内容
    parts.append(f"\n## 上一章内容（供衔接参考）\n{last_chapter_content}\n")

    return "\n".join(parts)


def build_writing_prompt(project: NovelProject, chapter_plan: dict,
                         last_chapter_content: str,
                         style_sample: str = "",
                         resource_summary: str = "") -> str:
    """构建正文续写的完整提示词"""
    parts = []

    parts.append(build_system_prompt(project))
    parts.append(build_character_prompt(project))
    parts.append(build_plot_overview_prompt(project))

    # 资源追踪状态（审计用）
    if resource_summary:
        parts.append(resource_summary)

    # 文风样本
    if style_sample:
        parts.append(f"# 文风参考样本\n{style_sample}\n")

    # 章节元信息（紧凑结构化，避免与写作提示重复）
    ch_num = chapter_plan.get('chapter_number', '?')
    title = chapter_plan.get('title', '')
    ch_type = chapter_plan.get('chapter_type', 'normal')
    word_target = chapter_plan.get('word_count_target', project.base_word_count)
    time_info = chapter_plan.get('time', '')
    scene_info = chapter_plan.get('scene', '')

    type_map = {'normal': '普通', 'key': '重点', 'turning': '转折'}
    type_label = type_map.get(ch_type, ch_type)

    meta_line = f"第{ch_num}章「{title}」| {type_label} | 目标{word_target}字"
    if time_info:
        meta_line += f" | 时间：{time_info}"
    if scene_info:
        meta_line += f" | 场景：{scene_info}"

    parts.append(f"# 写作任务\n{meta_line}")

    # 核心情节（精简列表）
    core_plot = chapter_plan.get("core_plot", [])
    if core_plot:
        parts.append("\n## 核心情节")
        for plot in core_plot:
            parts.append(f"- {plot}")

    # 写作提示（只保留纯写作指导，去除可能的元信息重复）
    prompt_text = chapter_plan.get('prompt', '')
    if prompt_text:
        parts.append(f"\n## 写作指导\n{prompt_text}")

    # 上一章内容
    parts.append(f"\n## 上一章内容（供衔接参考）\n{last_chapter_content}\n")

    parts.append(f"""
## 要求
- 直接输出正文，不要输出标题、章节号等元信息
- 目标字数{word_target}字左右，误差不超过10%
- 保持人物性格一致，注意与上一章衔接
- 第一人称/第三人称按文风设定
""")

    return "\n".join(parts)


def build_revision_prompt(project: NovelProject, original_content: str,
                          revision意见: str, chapter_plan: dict) -> str:
    """构建修改重写的提示词"""
    parts = []

    parts.append(build_system_prompt(project))
    parts.append(build_character_prompt(project))

    parts.append(f"""
# 章节修改任务

## 原始内容
{original_content}

## 修改意见
{revision意见}

## 章节规划参考
- 标题：{chapter_plan.get('title', '')}
- 目标字数：{chapter_plan.get('word_count_target', project.base_word_count)}字

## 要求
1. 根据修改意见重新生成本章内容
2. 保持与原章节的整体框架一致
3. 只修改需要调整的部分，不要大幅改变剧情走向
4. 直接输出修改后的完整章节内容
""")

    return "\n".join(parts)


def build_audit_prompt(project: NovelProject, chapter_content: str,
                       chapter_number: int, resource_summary: str) -> str:
    """构建审计提示词"""
    return f"""你是一个小说审计专家，负责检查小说章节的连贯性和一致性。

# 小说信息
{project.title}

# 当前资源追踪状态
{resource_summary}

# 待审计章节（第{chapter_number}章）
{chapter_content}

# 审计任务

请仔细检查本章内容，完成以下任务：

## 1. 冲突检测
检查是否存在以下类型的冲突：
- 时间线冲突：角色在同一时间出现在两个地方？
- 人物状态冲突：已死/已离开/已转变的角色出现矛盾？
- 物品冲突：已销毁/已转让的物品再次出现？
- 设定冲突：与基础设定矛盾的描述？
- 数值冲突：财富/能力等数值异常变化？
- 伏笔冲突：已回收的伏笔再次埋设？

## 2. 资源变动提取
提取本章中发生的资源变动：
- 新增/变更的财富或资产
- 新增/变更/销毁的重要物品
- 系统数值变化（如有）
- 人物状态变化
- 新埋设的伏笔
- 已回收的伏笔

## 3. 输出格式
请严格按照以下JSON格式输出：

```json
{{
  "conflicts": [
    {{
      "conflict_type": "timeline/character/item/setting/value/foreshadow",
      "description": "冲突描述",
      "suggestion": "建议处理方式"
    }}
  ],
  "resource_changes": [
    {{
      "category": "wealth/item/system/character_status/foreshadow",
      "name": "资源名称",
      "value": "当前值/状态",
      "action": "add/update/destroy/resolve"
    }}
  ],
  "summary": "本章审计总结"
}}
```
"""


def build_import_analysis_prompt(novel_text: str) -> str:
    """构建导入分析的提示词"""
    return f"""你是一个小说分析专家，请分析以下小说内容，提取结构化信息。

# 小说内容
{novel_text}

# 分析任务
请从以上内容中提取以下信息：

## 1. 基础设定
提取世界观、时代背景、核心规则、特殊设定等。

## 2. 人物设定
识别所有出场人物，为每个人物创建角色卡片，包含：姓名、年龄、外貌、性格、背景、人物关系、当前状态、角色弧线。

重要：必须为每个角色标注等级（role_level）和剧情重要性（plot_importance）：
- role_level分类标准：
  - "main"：主要角色（贯穿全文，对主线剧情有重大影响，如主角、核心反派）
  - "secondary"：次要角色（有独立剧情线，多次出场，对故事发展有明显推动作用）
  - "marginal"：边缘角色（偶尔出场，功能性角色，如路人、配角、工具人）
- plot_importance：用一句话描述该角色在剧情中的具体作用和重要程度（如"女主角，故事核心驱动力"、"第三章出场的配角，提供关键线索"）

## 3. 章节梗概
为每个章节写一个200-300字的梗概，包含主要事件、人物互动、关键转折。

## 4. 文风分析
分析写作特点：叙述视角、语言风格、描写特点、结构特点、情色描写特点、人物塑造特点。

## 5. 续写方向
基于已有内容，分析可能的故事发展方向、未解决的冲突、可探索的线索。

## 输出格式
请严格按照以下JSON格式输出：

```json
{{
  "basic_setting": "基础设定的详细文本",
  "character_cards": [
    {{
      "name": "人物名",
      "role_level": "main/secondary/marginal",
      "age": "年龄",
      "appearance": "外貌描述",
      "personality": "性格描述",
      "background": "背景介绍",
      "relationships": "人物关系",
      "current_status": "当前状态",
      "arc": "角色弧线",
      "first_chapter": 0,
      "plot_importance": "该角色在剧情中的具体作用和重要程度"
    }}
  ],
  "plot_overview": "按章节顺序的剧情梗概",
  "writing_style": "文风分析的详细文本",
  "continuation_direction": "续写方向建议"
}}
```
"""
