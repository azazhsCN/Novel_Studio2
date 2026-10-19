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
    parts.append(build_continuation_prompt(project))

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

    # 核心情节（长章给出每个要点的字数预算，模型更容易写足篇幅）
    core_plot = chapter_plan.get("core_plot", [])
    if core_plot:
        parts.append("\n## 核心情节")
        per_item = max(500, word_target // max(len(core_plot), 1))
        for plot in core_plot:
            if word_target >= 6000:
                parts.append(f"- {plot}（此要点请写足约{per_item}字）")
            else:
                parts.append(f"- {plot}")

    # 写作提示（只保留纯写作指导，去除可能的元信息重复）
    prompt_text = chapter_plan.get('prompt', '')
    if prompt_text:
        parts.append(f"\n## 写作指导\n{prompt_text}")

    # 上一章内容
    parts.append(f"\n## 上一章内容（供衔接参考）\n{last_chapter_content}\n")

    lower = int(word_target * 0.9)
    length_block = f"""
## 要求
- 直接输出正文，不要输出标题、章节号等元信息
- **篇幅：约{word_target}字（不得少于{lower}字）**；宁可把细节写足，也不要提前收尾
- 保持人物性格一致，注意与上一章衔接
- 第一人称/第三人称按文风设定"""
    if word_target >= 6000:
        length_block += f"""
- 按上面的"核心情节"逐条展开，每条都要写到，不要合并或跳过
- 写完后自查字数：不足{lower}字时，继续扩写对话、动作、心理与环境细节，不要用一段总结草草收尾"""
    parts.append(length_block + "\n")

    return "\n".join(parts)


def build_revision_prompt(project: NovelProject, original_content: str,
                          revision意见: str, chapter_plan: dict,
                          style_sample: str = "",
                          resource_summary: str = "") -> str:
    """构建修改重写的提示词

    与首写提示词保持同等上下文：剧情概述、续写方向、文风样本、资源状态都要带上，
    否则多轮改写后剧情与文风会逐步漂移。
    """
    parts = []

    parts.append(build_system_prompt(project))
    parts.append(build_character_prompt(project))
    parts.append(build_plot_overview_prompt(project))
    parts.append(build_continuation_prompt(project))

    if resource_summary:
        parts.append(resource_summary)

    if style_sample:
        parts.append(f"# 文风参考样本\n{style_sample}\n")

    parts.append(f"""
# 章节修改任务

## 原始内容
{original_content}

## 修改意见
{revision意见}

## 章节规划参考
- 标题：{chapter_plan.get('title', '')}
- 目标字数：{chapter_plan.get('word_count_target', project.base_word_count)}字（请写足，不要压缩篇幅）

## 要求
1. 根据修改意见重新生成本章内容
2. 保持与原章节的整体框架一致
3. 只修改需要调整的部分，不要大幅改变剧情走向
4. 直接输出修改后的完整章节内容，**不要遗漏后半部分、不要提前收尾**
""")

    return "\n".join(parts)


def build_write_continuation_prompt(project: NovelProject, chapter_plan: dict,
                                    written: str, remaining: int,
                                    tail_chars: int = 800) -> str:
    """续写提示词：单次生成不足目标字数时，接着已写内容继续写（不重复）"""
    ch_num = chapter_plan.get("chapter_number", "?")
    title = chapter_plan.get("title", "")
    target = chapter_plan.get("word_count_target", project.base_word_count)
    tail = written[-tail_chars:] if tail_chars and len(written) > tail_chars else written
    return f"""你在续写同一章小说，请**紧接着下面的内容继续往下写**，不要重写、不要重复已有情节。

# 本章信息
第{ch_num}章《{title}》| 目标{target}字 | 已写约{len(written)}字 | 还需约{max(remaining, 0)}字

# 已写内容的结尾（仅用于衔接，不要重复输出）
...{tail}

# 要求
- 只输出续写的正文，不要标题、章节号、总结或说明文字
- 直接从下一句开始，不要复述上面的内容
- 保持人称、文风、时间线与情节连贯
- 还需约{max(remaining, 0)}字；若情节确实已到本章结局，用一两句话自然收束即可"""


def build_audit_prompt(project: NovelProject, chapter_content: str,
                       chapter_number: int, resource_summary: str,
                       nearby_characters: str = "", recent_overview: str = "") -> str:
    """构建审计提示词

    P0 关键修复：此前只给 AI「标题 + 资源摘要 + 本章正文」，
    基础设定 / 人物卡 / 剧情概述一律没带——所以"设定冲突""人物状态冲突"
    根本无从对照，模型只能凭正文猜，于是产出大量"时间模糊""未提及"这类空话。
    现在补齐判定依据，并要求给出可执行的段落级改写。
    """
    parts = [f"""你是一个小说审稿编辑，负责找出**真正的连贯性错误**，并给出**可直接应用的最小修改**。

# 项目
{project.title}
"""]

    # 判定依据一：基础设定
    if project.core_prompt.basic_setting:
        parts.append(
            "# 基础设定（判定「设定冲突」的依据）\n"
            f"{project.core_prompt.basic_setting}\n"
        )

    # 判定依据二：人物卡（优先放本章出场者）
    if nearby_characters:
        parts.append(
            "# 相关人物卡（判定「人物状态冲突」的依据）\n"
            f"{nearby_characters}\n"
        )

    # 判定依据三：前情概述
    if recent_overview:
        parts.append(
            "# 前情概述（判定「时间线/伏笔」的依据）\n"
            f"{recent_overview}\n"
        )

    # 判定依据四：资源表
    if resource_summary:
        parts.append(resource_summary)

    parts.append(f"""# 待审计章节（第{chapter_number}章）
{chapter_content}
""")

    parts.append("""# 审计任务

## 一、只报"能被证实"的错误
逐条核对下列 6 类冲突。**必须有正文原句作为证据**，说不出原句的就不要报：

- 时间线冲突：同一角色同时出现在两处；时间倒流；与"前情概述"矛盾的时序
- 人物状态冲突：已死/已离场/已转变的角色状态与"人物卡"或资源表不符
- 物品冲突：已销毁/已转让的物品再次出现且未说明来路
- 设定冲突：违反"基础设定"中的明确规则
- 数值冲突：作品内可量化的数值（资产、等级、属性、时间跨度等）与资源表记录矛盾
- 伏笔冲突：已回收的伏笔再次被当作未解决

**不要报这些**（它们不是错误，是写作建议，最多作为 info）：
- "某角色未出场/缺席"——除非前情明确要求他必须在本章出现
- "时间描写不够具体""建议加强描写""可以补充细节"这类泛泛评价
- 文笔、节奏、篇幅、情感强度方面的主观意见

## 二、给出可执行修改
每条冲突必须给出 `anchor`：**原封不动复制本章中需要修改的那一句（或连续几句）**，
长度不超过 80 字，必须能在正文中精确找到。
`fix_type` 取值：
- `replace_text`：只需替换 anchor 这一处文字（最常见）
- `rewrite_paragraph`：anchor 所在段落需要整体重写，此时必须给出 `suggested_text`
- `update_resource`：正文没问题，是资源表记错了，无需改文
- `none`：确实不好定位（尽量避免）

## 三、资源变动提取（重要性把关）
**只提取会影响后续剧情走向的资源**，具体判定标准见下方各类定义。下列内容一律不要提取：
- 一次性的、用后即弃的物品（一杯水、一张纸巾、普通衣物）
- 状态没有变化的日常重复（吃饭、睡觉、通勤）
- 没有专名、没有后续作用的泛称
- 只有"获得/使用/失去"这类动作词、没有具体内容的状态

每条资源必须标注：
- `importance`：`core`（贯穿主线的关键要素）/ `normal`（有后续作用）/
  `minor`（仅本章提及，通常会被放入待确认区）
- `confidence`：0-1，你对"它真的重要且真的发生了"的把握

**命名规则（很重要，直接决定资源表会不会变成流水账）**：
- `character_status` 的 `name` **只写人物姓名本身**（例如只写"角色名"三个字），
  不要附加任何括注或后缀——「角色名（状态）」「角色名心理状态」「角色名（新人物状态）」
  这类变体都会被判为重复条目；
  同一人物的身心/身份变化**合并成一条**，在 `value` 里用一段话写清当前综合状态
- `item` / `foreshadow` 的 `name` 写稳定的专名，不要在名字里塞描述或括注
- 同一个东西在不同章节**必须复用同一个名字**，否则会被当成新资源

**分类选择**（按小说的实际类型判断，不要硬套某一种题材）：
- `wealth`：金钱、产业、地位等可量化或可转移的资产
- `item`：有专名、且后续还会用到的具体物件
- `system`：作品内存在的规则化体系数值（如等级、属性、声望、修为、职位等；
  若本作品没有这类体系，此项留空即可）
- `character_status`：人物当前的身心、身份、立场状态
- `foreshadow`：已埋设但尚未回收的线索、承诺、悬念

## 四、输出格式
严格输出以下 JSON，不要输出任何额外文字：

```json
{
  "conflicts": [
    {
      "conflict_type": "timeline/character/item/setting/value/foreshadow",
      "severity": "error/warning/info",
      "description": "冲突描述（一句话说清哪里不对）",
      "evidence": "支撑判断的前情依据（来自设定/人物卡/资源表的原文）",
      "anchor": "本章中需要修改的原句（不超过80字，必须能精确匹配）",
      "fix_type": "replace_text/rewrite_paragraph/update_resource/none",
      "suggested_text": "fix_type=rewrite_paragraph 时的改写文本；其它情况留空",
      "suggestion": "给作者的处理建议"
    }
  ],
  "resource_changes": [
    {
      "category": "wealth/item/system/character_status/foreshadow",
      "name": "资源名称（写专名，不要写"某物品"）",
      "value": "当前具体值/状态（写清内容，不要只写"已获得"）",
      "action": "add/update/destroy/resolve",
      "importance": "core/normal/minor",
      "confidence": 0.9,
      "source_excerpt": "本章中首次提到它的原句（不超过40字）"
    }
  ],
  "summary": "本章审计总结（1-2 句）"
}
```

判定规则：
- `severity=error` 仅用于"读者会明显察觉的自相矛盾"
- `severity=warning` 用于"可能有问题，需要作者确认"
- `severity=info` 用于"写作建议"
- 没有冲突就返回空数组，不要为了凑数编造
""")

    return "\n".join(parts)


def build_patch_prompt(chapter_content: str, paragraphs: list[str],
                       conflict: dict) -> str:
    """段落级补丁提示词（P0 核心：只改有问题的段落，不重写整章）

    这是"审计建议可直接落地"的关键——此前唯一途径是整章重新生成，
    改一个词也要付 2 万字的代价，且原稿被覆盖。
    """
    para_list = "\n".join(f"[{i}] {p}" for i, p in enumerate(paragraphs))
    anchor = conflict.get("anchor", "")
    return f"""你在做小说**局部修订**：只改必要的地方，其余文字一个字都不要动。

# 需要修正的问题
- 类型：{conflict.get('conflict_type', '')}
- 严重度：{conflict.get('severity', 'warning')}
- 问题：{conflict.get('description', '')}
- 依据：{conflict.get('evidence', '') or '（无）'}
- 需要修改的原句：{anchor or '（未指定，请自行定位）'}
- 作者建议：{conflict.get('suggestion', '') or '（无）'}

# 本章段落（编号仅用于定位，不要输出编号）
{para_list}

# 输出要求
1. 只输出**需要修改的段落**，用如下 JSON 返回，不要输出其它内容：
```json
{{
  "patches": [
    {{
      "paragraph_index": 3,
      "original": "该段落的原文（用于校验定位是否准确）",
      "revised": "修改后的整段文字"
    }}
  ]
}}
```
2. `paragraph_index` 必须来自上面的编号
3. `original` 必须是该段落原文的**完整内容**（用于精确匹配校验）
4. 未涉及的段落不要出现在 patches 里；若无需修改则返回空数组
5. 修改后要自然衔接上下文，不要改变剧情走向，不要增删关键情节
6. 篇幅与原段落相当（±30% 以内）"""



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
分析写作特点：叙述视角、语言风格、句式与节奏、描写密度、对话风格、结构特点、人物塑造手法。
（只描述该作品**实际存在**的特征，不要套用任何特定题材或流派的模板。）

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


def build_plot_detail_prompt(chapters_text: str, chapter_numbers: list[int],
                             old_overview: str = "", target_chars: int = 300) -> str:
    """逐章详细摘要提示词（需求 3.2：0-50 章每章 200-300 字）"""
    nums_label = "、".join(f"第{n}章" for n in chapter_numbers)
    low = max(50, int(target_chars * 0.7))
    return f"""你是一个小说剧情概述专家。请为以下章节**逐章**撰写详细摘要。

# 章节内容（{nums_label}）
{chapters_text}

# 输出要求
- 每章单独一节，标题格式严格为：### 第X章：本章主题（X 必须是真实章节号）
- 每章摘要 {low}-{int(target_chars)} 字，密集叙事体，连贯叙事而非逐条罗列
- 保留所有关键细节：人名、具体行为、关键数值、专有名词、人物状态变化、伏笔
- 不要合并章节、不要改写章节号、不要输出前导说明或解释性文字
- 给定的每一章都要有输出，一个都不能漏"""


def build_plot_compress_prompt(range_label: str, source_text: str,
                              old_overview: str = "") -> str:
    """早期章节压缩为一段的提示词（50-150 章档）"""
    return f"""你是一个小说剧情概述专家。请把以下内容压缩成**一段**连贯的剧情概述。

# 当前剧情概述（参考）
{old_overview}

# 待压缩内容（{range_label}）
{source_text}

# 输出要求
- 只输出一段正文（若干句，必须连贯），不要分小节、不要加任何标题
- 保留贯穿性关键信息：主要人物、关键事件与转折、重要伏笔及其回收
- 直接输出正文，不要前导说明、问候语"""


def build_plot_one_liner_prompt(range_label: str, source_text: str,
                                old_overview: str = "") -> str:
    """早期章节压缩为一句话的提示词（150+ 章档）"""
    return f"""你是一个小说剧情概述专家。请用**一句话**概括以下内容。

# 当前剧情概述（参考）
{old_overview}

# 待压缩内容（{range_label}）
{source_text}

# 输出要求
- 只输出一句话，不超过 80 字，不要标题、不要分点、不要换行
- 抓住这段剧情的核心走向与结果
- 直接输出正文，不要前导说明"""
