"""Shared generation/review policy; independent difficulty gates remain mandatory."""

import re


VERSION = "generation-v51"
LARGE_INPUT_SCALE_MINIMUM = 100_000
BUG_DESIGN_TARGET_MINUTES = 45
BUG_DESIGN_HARD_LIMIT_MINUTES = 55
BUG_FEASIBILITY_GUIDANCE = (
    "Pair-wise Bug 修复只接受独立盲审恰好评为困难的候选；简单、中等或地狱都须换方向，不能虚高或压低评级。"
    "Claude 首轮会在 70 分钟被停止；70 分钟不是出题预算。"
    f"按熟练开发者在 {BUG_DESIGN_TARGET_MINUTES} 分钟左右完成定位、最小正确修复、针对性回归和现有构建来设计，"
    f"完整工作预计超过 {BUG_DESIGN_HARD_LIMIT_MINUTES} 分钟的候选必须收窄或换方向，给首次阅读、环境准备和验证返工留余量。"
    "估算须覆盖最终题面要求的全部用户结果，不得把可独立删除的操作、第二条故障链、额外测试环境或文档整理藏在估算外。"
    "优先选择一个有深度的故障链；多个现象只有共同破坏同一业务不变量、可在一次修复和一组针对性回归中闭环时才合题。"
    "跨生产文件定位可以成立，但不得为了凑困难、问题数或改动行数扩成完整界面回归套件、第二套状态机制或多个独立流程。"
    f"one_hour_feasible 仅在完整工作预计不超过 {BUG_DESIGN_HARD_LIMIT_MINUTES} 分钟时填 true；"
    "时间与行数仅供内部范围控制，不参与难度评分，也不得写进最终用户题面。"
)
TASK_INPUT_SCALE_GUIDANCE = (
    "0-1、Feature 和 Bug 三种题面都不得用十万级或更高的条目、记录、字符、查询、节点、边、事件、"
    "样本、输入或结果数量来堆砌难度，也不得把大整数上限配秒级时限作为困难成立的主要依据。"
    "输入规模应缩到仍能验证同一正确性、状态或复杂度边界的最小可验证规模；难度必须来自真实工程取舍、"
    "不变量、失败恢复、并发或算法正确性。纯性能压力题一律拒绝：即使使用复杂算法，只要困难成立主要"
    "依赖吞吐量、延迟、限时、基准测试或压测规模，独立审核也必须令 inflated_label_risk=true、"
    "approved=false，不能把它作为困难题放行。"
)

_LARGE_ARABIC_NUMBER_RE = re.compile(
    r"(?<![\w.])(?:\d{1,3}(?:[,，_]\d{3})+|\d{6,})(?![\w.])"
)
_LARGE_WAN_NUMBER_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*万")
_LARGE_K_NUMBER_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*[kK](?![\w])")
_LARGE_SCIENTIFIC_NUMBER_RE = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*[eE]\s*([5-9]|\d{2,})(?![\w.])"
)
_LARGE_POWER_NUMBER_RE = re.compile(
    r"(?<![\w.])10\s*(?:\^|\*\*)\s*([5-9]|\d{2,})(?![\w.])"
)
_LARGE_CHINESE_NUMBER_RE = re.compile(
    r"(?:[一二两三四五六七八九]?十万|[一二两三四五六七八九]?百万|"
    r"[一二两三四五六七八九]?千万|[一二两三四五六七八九]?亿)"
)
_INPUT_SCALE_UNIT_RE = re.compile(
    r"^\s*(?:项|条|个|组|份|批|字符|字节|记录|节点|元素|读数|查询|请求|事件|样本|"
    r"数据|边|任务|对象|行|列|结果|输入|文档|文件|消息|日志|用户|设备|站点|点|区间|操作|"
    r"items?|entries?|records?|characters?|chars?|bytes?|nodes?|edges?|queries?|requests?|"
    r"events?|samples?|rows?|columns?|results?|inputs?|documents?|files?|messages?|logs?|"
    r"users?|devices?|points?|intervals?|operations?|tasks?)",
    re.I,
)
_INPUT_SCALE_PREFIX_RE = re.compile(
    r"(?:长度|数量|规模|总数|条目数|字符数|字节数|记录数|节点数|元素数|读数|查询数|请求数|"
    r"事件数|样本数|数据量|边数|任务数|对象数|行数|列数|结果数|输入数|文档数|文件数|消息数|"
    r"日志数|用户数|设备数|站点数|点数|区间数|操作数|数组长度|文本长度|正文长度|输入长度|"
    r"数据规模|样本量|批量大小|\b(?:items?|entries?|records?|characters?|chars?|bytes?|nodes?|"
    r"edges?|queries?|requests?|events?|samples?|rows?|columns?|results?|inputs?|documents?|files?|"
    r"messages?|logs?|users?|devices?|points?|intervals?|operations?|tasks?|size|length|count)\b|"
    r"\b(?:n|m|q)\s*(?:=|<=|>=|<|>|≤|≥))",
    re.I,
)
_PERFORMANCE_PRESSURE_RE = re.compile(
    r"(?:性能|纯性能|性能压力|性能测试|压力测试|负载测试|压测|基准测试|benchmark|load\s*test|"
    r"stress\s*test|吞吐(?:量)?|每秒(?:处理|完成|响应|写入|读取)?|\b(?:qps|tps|rps)\b|"
    r"\bp(?:90|95|99)\b|延迟|响应时间|处理时间|执行时间|耗时|时限|限时)",
    re.I,
)
_PERFORMANCE_ACCEPTANCE_RE = re.compile(
    r"(?:(?:性能|吞吐|延迟|响应时间|处理时间|执行时间|耗时|时限|限时|压测|基准测试|"
    r"benchmark|\b(?:qps|tps|rps|p90|p95|p99)\b).{0,32}(?:达标|通过|合格|验收|门槛)|"
    r"(?:达标|通过|合格|验收|门槛).{0,32}(?:性能|吞吐|延迟|响应时间|处理时间|执行时间|"
    r"耗时|时限|限时|压测|基准测试|benchmark|\b(?:qps|tps|rps|p90|p95|p99)\b))",
    re.I,
)
_PERFORMANCE_ONLY_RE = re.compile(
    r"(?:(?:唯一|仅有|单一|主要|核心)(?:的)?(?:难点|门槛|依据|要求|验收(?:标准|条件)?)"
    r".{0,24}(?:性能|吞吐|延迟|时限|限时|压测|基准)|"
    r"(?:性能|吞吐|延迟|时限|限时|压测|基准).{0,24}"
    r"(?:唯一|仅有|单一|主要|核心)(?:的)?(?:难点|门槛|依据|要求|验收(?:标准|条件)?))",
    re.I,
)
_PERFORMANCE_METRIC_RE = re.compile(
    r"(?:\b(?:qps|tps|rps|p90|p95|p99)\b\s*(?:[<=>≤≥:]|不超过|低于|达到)?\s*\d|"
    r"\d+(?:\.\d+)?\s*(?:毫秒|ms|秒|s|分钟|min)\s*(?:内|以内|以下|完成|响应|处理)|"
    r"每秒\s*(?:至少|至多|不低于|不超过)?\s*\d|"
    r"\d+(?:\.\d+)?\s*(?:万|千|[kK])?\s*(?:次|个|条|项|请求|记录|任务|消息)\s*/\s*秒)",
    re.I,
)
_BOUNDED_INPUT_WITH_TIME_RE = re.compile(
    r"(?:\d+(?:\.\d+)?\s*(?:万|千|[kK])?\s*(?:项|条|个|字符|记录|查询|节点|边|事件|样本|请求)"
    r".{0,48}\d+(?:\.\d+)?\s*(?:毫秒|ms|秒|s|分钟|min)\s*(?:内|以内|以下)|"
    r"\d+(?:\.\d+)?\s*(?:毫秒|ms|秒|s|分钟|min)\s*(?:内|以内|以下).{0,48}"
    r"\d+(?:\.\d+)?\s*(?:万|千|[kK])?\s*(?:项|条|个|字符|记录|查询|节点|边|事件|样本|请求))",
    re.I,
)
_NON_PERFORMANCE_DIFFICULTY_RE = re.compile(
    r"(?:不变量|幂等|去重|崩溃恢复|断点续作|补偿|回滚|兼容迁移|向后兼容|权限|鉴权|隔离|"
    r"原子(?:性|裁决)|竞态|互斥|版本冲突|迟到消息|乱序|重放|状态机|唯一胜者|唯一方案|"
    r"字典序|稳定排序|最大流|最小割|最短路|最优解|规则裁决|精确几何|确定性结果)",
    re.I,
)


def oversized_input_scale_issue(value):
    """Return a rejection reason when a prompt uses six-figure input scale."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""

    matches = []
    for match in _LARGE_ARABIC_NUMBER_RE.finditer(text):
        amount = int(re.sub(r"[,，_]", "", match.group(0)))
        if amount >= LARGE_INPUT_SCALE_MINIMUM:
            matches.append((match.start(), match.end()))
    for match in _LARGE_WAN_NUMBER_RE.finditer(text):
        if float(match.group(1)) * 10_000 >= LARGE_INPUT_SCALE_MINIMUM:
            matches.append((match.start(), match.end()))
    for match in _LARGE_K_NUMBER_RE.finditer(text):
        if float(match.group(1)) * 1_000 >= LARGE_INPUT_SCALE_MINIMUM:
            matches.append((match.start(), match.end()))
    for match in _LARGE_SCIENTIFIC_NUMBER_RE.finditer(text):
        exponent_text = match.group(2)
        exponent = int(exponent_text) if len(exponent_text) <= 3 else 309
        if exponent > 308 or float(match.group(1)) * (10.0 ** exponent) >= LARGE_INPUT_SCALE_MINIMUM:
            matches.append((match.start(), match.end()))
    for match in _LARGE_POWER_NUMBER_RE.finditer(text):
        matches.append((match.start(), match.end()))
    matches.extend(
        (match.start(), match.end()) for match in _LARGE_CHINESE_NUMBER_RE.finditer(text)
    )

    for start, end in sorted(set(matches)):
        clause_start = max(
            text.rfind("。", 0, start),
            text.rfind("！", 0, start),
            text.rfind("？", 0, start),
            text.rfind("；", 0, start),
            text.rfind(";", 0, start),
        ) + 1
        prefix = text[max(clause_start, start - 48):start]
        suffix = text[end:end + 24]
        if _INPUT_SCALE_UNIT_RE.search(suffix) or _INPUT_SCALE_PREFIX_RE.search(prefix):
            return (
                "三种题面均不得用十万级或更高的数据条目、字符、查询或结果规模制造难度；"
                "请改用能够验证同一正确性边界的最小规模"
            )
    return ""


def pure_performance_pressure_issue(value):
    """Return a hard rejection for prompts whose difficulty is only performance pressure."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text or not _PERFORMANCE_PRESSURE_RE.search(text):
        return ""

    explicit_only = bool(_PERFORMANCE_ONLY_RE.search(text))
    performance_acceptance = bool(_PERFORMANCE_ACCEPTANCE_RE.search(text))
    bounded_input_with_time = bool(_BOUNDED_INPUT_WITH_TIME_RE.search(text))
    metric_count = len(_PERFORMANCE_METRIC_RE.findall(text))
    has_independent_correctness = bool(_NON_PERFORMANCE_DIFFICULTY_RE.search(text))
    if explicit_only or (
        not has_independent_correctness
        and (performance_acceptance or bounded_input_with_time or metric_count >= 2)
    ):
        return (
            "三种题面均不得以吞吐量、延迟、限时、基准测试或压测规模作为唯一困难来源；"
            "纯性能压力题即使使用复杂算法也必须拒绝"
        )
    return ""


def task_prompt_hard_policy_issue(value):
    """Return the first deterministic hard-policy rejection for a final task prompt."""
    return oversized_input_scale_issue(value) or pure_performance_pressure_issue(value)
ZERO_ONE_HANDWRITTEN_LINE_LIMIT = 2500
BUG_V2_MIN_PROBLEMS = 1
BUG_V2_MAX_PROBLEMS = 3
BUG_STRICT_ATTEMPTS = 2
BUG_SUBSTANTIAL_PATCH_MIN_CHANGED_LINES = 30
BUG_STATIC_GUIDANCE = (
    "本次 Bug 出题采用 static_first：模型生成与审核默认只做静态源码核验，不要求实际运行。"
    "模型调用只读取源代码、需求和已有测试文本，允许只读 Git 检查；不要启动项目、安装依赖、运行测试、"
    "执行复现脚本或启动容器。编排器仅在当前环境无需安装、无需容器和外部服务即可直接执行时，"
    "可在一次性副本中选择性运行已有检查，总计不超过 90 秒；无法运行或检查失败不能作为拒绝理由。"
    "必须给出真实代码位置、触发输入或状态序列、控制流或数据流推导、预期错误与正确结果；"
    "独立审核须重新读取代码核对，不能照抄生成者结论，也不能把推测风险当已确认缺陷。"
    "reproduction 表示触发场景，actual/observable_failure 表示源码推导的错误，evidence 表示代码依据；"
    "这些字段均不表示已经运行。不得编造日志、退出码、测试通过或已复现结论。"
    "用户题面只描述待修复行为，不要求启动项目、实际运行或执行测试；可选运行记录不替代静态证据。"
    f"每道题允许包含 {BUG_V2_MIN_PROBLEMS} 至 {BUG_V2_MAX_PROBLEMS} 个可区分、可由源码确认的真实问题；"
    "一个足够复杂的真实故障链可以单独成题，不得为了数量拼接局部小 Bug。"
    "多个现象可以有不同根因或涉及不同生产文件，但必须围绕同一用户目标和被破坏的业务不变量；"
    "若其中一个问题需要独立定位、独立修复或另建一套验收流程，就应拆题或舍弃。"
    "只保留证明修复正确所必需的操作序列与边界，优先沿用项目现有测试工具做针对性回归；"
    "不得把新测试环境、完整交互回归套件或额外依赖当作题目默认交付义务。"
    "不能把同一个现象拆句凑数，也不能跨产品域、运行系统或大型子系统拼题。"
    "跨生产文件修改与必要的联合回归可作为难度证据；问题数量本身不提级，"
    "最终只有独立审核恰好确认困难才可进入 A/B。"
) + TASK_INPUT_SCALE_GUIDANCE
BUG_NOVELTY_GUIDANCE = (
    "Bug 新意按修复任务的具体故障链判断：比较有证据的根因、触发条件和用户可观察失败结果。"
    "原 0-1/Feature 题要求某功能正确工作，只说明功能契约；"
    "共享正确结果、业务对象、算法、主流程或正常验收，不等于已经出过同一缺陷修复题。"
    "不能仅因原题要求过正确行为，就把新发现或预置的具体故障直接判重复。"
    "历史无论标什么题型，只有已经明确覆盖同一具体触发或状态序列、同一错误分支或被破坏不变量、"
    "以及实质相同的用户可见失败结果时，才作为同一具体故障拒绝。只共享根因类别、校验器、模块、"
    "机制家族或部分失败表现不再自动判重复；新触发链和新业务后果有独立源码证据时应允许。"
    "历史只有一般正确性要求时，不得编造其缺陷根因或修复经历；"
    "共享锁、缓存、排序、状态机或异常类型等机制类别，仅作相似线索，不能代替同一具体故障的证据。"
    "还要检查同类题目骨架风险：若只是替换项目名、变量、输入值或措辞，仍沿用历史题的触发链、"
    "问题组合与可观察后果，应视为实质重复；真实不同的业务后果须有独立源码证据。"
    "先核对历史已提出的具体 Bug；不能把某算法或并发机制的全部潜在故障都当成一个已出题家族。"
    "证据不足的相似交给完整题面独立复核，不等于批准。旧审核意见也须按此口径核对。"
    "historical_direction_warnings 是未按当前口径确认的历史意见，不是禁止方向；"
    "不能因其中写着已排除就直接放弃，必须重新比较真实历史题的具体故障链。"
    "这只区分功能契约与修复任务，不豁免缺陷证据、最终难度和一次可完成的有界范围审核。"
)
FEATURE_GUIDANCE = (
    "Feature 默认在同一产品和现有运行架构内增加一条边界清晰、可独立验收的窄增量，不继续扩展、"
    "重构或替换已有核心功能、核心状态机和主要数据流。优先复用已有业务对象、数据、身份权限、导航、"
    "设计风格和构建部署，只增加完成新用户结果必需的轻量状态或派生结果；不得另建完整子系统、独立服务、"
    "第二套复杂状态机、跨模块事务或大规模迁移。"
    "需求只能有一个用户目标、一个工程核心、一条主流程和一个主要用户操作，修改两个至三个真实模块，"
    "与原系统只保留一个必要接入点；纯前端来源不追加业务后端，纯后端来源不强造界面。"
    "只保留一个深而窄、完成用户结果不可删除的工程取舍，不能退化为普通 CRUD，也不能叠加第二个困难轴。"
    "以熟练工程师二十五至四十分钟完成实现和针对性验收为目标，预计超过四十五分钟必须收窄或换方向；"
    "只取困难下沿，不接受地狱级候选。只保留一个针对性测试层，交互题至多增加一条主流程冒烟。"
    "确认、批量、应用、导出、配置等操作类别本身不是禁题；若只是重做历史已验收的同一行为、"
    "换措辞或换输入值，则必须拒绝并指出历史证据。"
    "难度只计算初始代码尚未实现的必要增量，常规参数化、局部状态或标准配置不能继承原算法的难度。"
    "只保留同一用户目标必需的改动，仍须独立审核真实困难、范围可完成和历史实质差异。"
) + TASK_INPUT_SCALE_GUIDANCE
ZERO_ONE_SCOPE_GUIDANCE = (
    "0-1 必须形成一个可持续使用、可直接验收的完整用户闭环。一个足够深的工作流即可成题，"
    "也可以包含多个为同一目标服务的关联结果，但不强制拆成两个功能模块。"
    "本期只取困难下沿，不接受地狱级候选；实现模块最多三个、应用运行组件最多两个、核心外辅助机制最多一项；"
    "复杂状态机制与自定义算法只能二选一，不能把两个困难轴包装成同一核心。"
    "全栈算法题只保留轻量持久化和一条浏览器主流程冒烟，不再叠加版本谱系、跨请求状态机或完整 E2E 套件。"
    "主体实现与针对性验收以三十至四十五分钟为目标，预计超过五十分钟必须收窄或换方向，"
    "实际九十分钟运行上限不能作为题目设计预算。"
    "内部行数预算包含生产源码、样式、数据库脚本与测试且不得超过 2500 行，"
    "不包含 package-lock.json、pnpm-lock.yaml、yarn.lock、go.sum、Cargo.lock 等依赖锁文件及构建产物。"
    "该预算只用于出题和后台质检，不得写入用户题面。"
    "只保留一个针对性测试层；全栈或交互题至多再保留一条端到端主流程冒烟，不要求完整浏览器回归套件。"
    "困难度必须来自最小正确实现所需的真实约束、取舍和验证负担，不能靠模块数量、技术栈或 Docker 配置凑难度。"
) + TASK_INPUT_SCALE_GUIDANCE
FEATURE_STYLE_GUIDANCE = (
    "题面以 400 至 440 字为目标，本地接受 300 至 580 字；不要为凑字数补背景或重复约束。"
    "句数、分号数量、句长和结尾交付项的位置仅为写作建议，不能单独否决。"
    "题面必须从该项目特有的用户、对象、操作冲突或可观察缺口自然切入，按该模块真实职责组织内容；"
    "不要固定以‘在现有项目中新增一个’开头，不要把技术栈、外壳、身份、导航、设计风格和部署写成复用清单，"
    "也不要用固定交付清单收尾。Compose 运行方式应贴近对应页面或服务，测试框架应贴近它验证的规则，"
    "生产构建应贴近前端或发布边界，禁止把三者合并成末句；最后一句必须落到模块特有的用户结果或失败状态。"
    "只在相关职责处说明必要接入，不能照抄内部政策用语或沿用近期题面的开头、"
    "句序和结尾骨架。照抄范围政策、字段拼装或固定骨架属于模板化，必须拒绝；"
    "其余纯表达建议写入 ai_style_issues，不写入硬性 reasons，真正改变验收结果的歧义仍须拒绝。"
)


def feature_guidance(context, default_iterations):
    """Return the same Feature definition for every follow-up iteration."""
    return FEATURE_GUIDANCE
