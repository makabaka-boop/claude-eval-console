"""Evidence requirements shared by question generation and completion review."""

VERSION = 5
LEVELS = ("简单", "中等", "困难", "地狱")
HARD_LEVELS = ("困难", "地狱")
BUG_SUBMITTABLE_LEVELS = HARD_LEVELS

GUIDANCE = """难度按熟练工程师从本轮初始代码完成题面所必需的最小正确方案判定。
困难：需要整合多个模块/系统约束，做关键设计取舍，处理复杂状态、兼容性、权限、并发、性能或异常链路。
地狱：人执行起来需要极高的决策复杂度，通常需求很开放或约束很隐蔽，需要极其深度项目理解、架构级判断、
多步复杂的调试验证，或正确性依赖大量边界场景。即使当前的 SOTA 模型也无法成功交付。
下述分析项用于收集这两条标准的证据，不另设“必须恰好一种复杂机制”的难度门槛。
先填写 baseline_capabilities（初始代码已经具备的能力）、required_delta（本轮必要增量）和
minimal_solution（最直接的正确实现与验证路径），再决定难度。routine_solution_sufficient 表示
常规局部修改、标准库的惯用法、简单状态迁移表或单进程内存中的版本检查与请求去重是否已足够满足题面；
如果是，通常应判简单或中等，不能仅因出现并发、原子性、幂等、跨层等词就判困难。
design_tradeoffs 必须说明需要在什么可行方案之间取舍、什么真实约束使其不能机械套用；
verification_obligations 必须逐条写明必须保持的不变量、能击穿错误方案的具体输入或操作交错，以及可观察的判据。
困难需要上述决策与验证负担的具体证据；文件、接口、字段、技术栈或测试数量本身不能直接定级。
对于 Bug 修复，若多个源码确认的问题分布在不同生产模块，要求协调修复并承担联合回归义务，
其组合定位、修改和验证负担可以作为困难或困难潜力的支持证据，不要求必须另有并发或跨阶段机制。
0-1 只计算本轮从零实现的核心；Feature 只计算新增能力，不能继承原项目算法的难度；
Bug 只计算有证据支持的缺陷修复包的必要定位、修改与联合验证，不得把原项目整体复杂度算入，也不得新增需求凑困难。
校准示例：已有 HTTP 服务等待 Shutdown 完成、传播超时错误并调整容器等待时间，通常为中等；
已有 Web 工程新增四状态场次、内存队列、expectedVersion 与 requestId 检查，通常为中等；
从空仓库自行实现最大流/最小割，并证明多源多汇与 64 位容量边界、满足大图精确值和性能验收，具有困难依据。
这些是按约束判断的例子，不按项目名或关键词硬编码等级；若存在额外必要约束，必须给出证据。
地狱必须符合文档的极高决策复杂度、深度项目理解与显著验证负担，不能简单按机制数量定级。
不以模型耗时、失败次数、返工、环境故障、实现者自行增加的复杂代码或最终交付失败推高难度。
评审可以如实返回简单或中等，提交门槛不能影响定级。证据不足时不得勉强判困难。"""

GENERATION_GUIDANCE = GUIDANCE + """
生成顺序：先分析初始代码和最小正确方案，在 complexity_analysis 保存上述证据，再写最终题面。
若常规方案已足够，换一个有真实设计取舍的方向或来源；允许没有合格候选，不为配比硬凑题。
不得用额外页面、字段、接口、组件或无关边界堆高难度；保持一条主流程与原有范围预算。
内部分析不得复制进用户题面，尤其 Bug 题面不得泄漏根因或解决方法。
返回前对照最终题面复查：所有难度依据必须是题面实际要求，不能只存在于内部分析。
"""

BUG_GUIDANCE = GUIDANCE + """
本次 Bug 评定从开发者拿到的故障现象与带缺陷初始代码出发，计算定位根因、最小正确修复和必要验证的完整负担。
上述 routine_solution_sufficient 对 Bug 指这条完整路径是否常规；不能只因最终补丁很短就设为 true，
也不能只因隐藏了答案就设为 false。诊断难度须由数据流、相互作用的约束及排除合理错误根因的证据支撑。
若从间歇表象到根因必须重建跨事务、跨请求或跨阶段的时序/锁/版本图，排除多个合理根因，
并设计能稳定击穿错误修法的交错验证，则这条完整路径不是“常规定位加局部修复”；多个真实问题即使各自较局部，
只要分布在不同生产模块、需要协调修改并建立覆盖相互影响的联合回归，其组合负担也可以不是常规局部修复；
在有具体代码证据时，即使最终只恢复一处条件、锁模式或映射，也应将 routine_solution_sufficient 设为 false，
并按完整诊断与验证负担判定是否达到困难。反之，若报错或代码直接指向单一根因，不需要重建上述交互，
则局部恢复仍应判为常规，不得仅凭“并发”等词汇提高难度。
能独立从带缺陷代码推导出的简单修法始终允许。不得借原项目复杂度或新增要求提高评级。
"""

BUG_GENERATION_GUIDANCE = BUG_GUIDANCE + """
本阶段记录生成者分析，不作最终难度准入判断；最终由只读冻结缺陷快照和用户题面的独立审核决定。
每道题形成一个范围有界、可一次完成的修复包；其中问题可以位于同一项目的不同现有功能或模块，
不再要求共享单一根因、核心不变量或用户旅程。每项须有自己的触发条件、错误表现和代码证据；
不得把同一现象拆分凑数，也不得拼接跨产品域、跨运行系统或明显无法一次完成的 Bug。
困难度按整组问题必要的定位、协调修改与联合验证判断；问题数量和跨文件范围可以作为支持证据，但技术名词本身不能定级。
你知道预置线索或正确基线，不能仅因能恢复原实现而放弃候选，也不能伪造令正确方案失败的反例。
    routine_assessment 与 routine_path 如实描述判断；设计阶段 counterexample 可留空。若最终盲审以
    “局部修法会遗漏边界”作为困难依据，盲审必须给出具体输入、状态序列或操作交错及可观察判据，
    不能要求已知最小正确方案也失败。
内部代码证据、根因、修法与预置过程不得泄漏进用户题面；不得添加要求提高难度。
"""


def analysis_schema():
    strings = {"type": "array", "items": {"type": "string"}}
    properties = {
        "baseline_capabilities": {"type": "string"},
        "required_delta": {"type": "string"},
        "minimal_solution": {"type": "string"},
        "routine_solution_sufficient": {"type": "boolean"},
        "design_tradeoffs": strings,
        "verification_obligations": strings,
    }
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def generation_analysis_schema():
    """The author's assessment is evidence, not an independent veto."""
    schema = analysis_schema()
    properties = dict(schema["properties"])
    properties.pop("routine_solution_sufficient")
    properties.update({
        "routine_assessment": {"type": "string", "enum": ["足够", "不足够", "有争议"]},
        "routine_path": {"type": "string"},
        "counterexample": {"type": "string"},
    })
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def generation_analysis_errors(value):
    if not isinstance(value, dict):
        return ["缺少生成者难度分析"]
    # Legacy stored drafts remain readable, but are re-reviewed under the current version.
    if "routine_assessment" not in value:
        return analysis_errors(value, require_hard=False)
    errors = []
    for key in ("baseline_capabilities", "required_delta", "minimal_solution",
                "routine_path"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            errors.append(f"缺少{key}依据")
    if value.get("routine_assessment") not in {"足够", "不足够", "有争议"}:
        errors.append("常规方案判断必须为足够、不足够或有争议")
    # The author card may leave this empty. If the independent blind verdict
    # relies on a local-looking repair missing an edge case, that reviewer must
    # provide the concrete input/state/interleaving instead.
    for key in ("design_tradeoffs", "verification_obligations"):
        items = value.get(key)
        if not isinstance(items, list) or any(not isinstance(x, str) or not x.strip() for x in items):
            errors.append(f"{key}格式无效")
    return errors


def analysis_errors(value, require_hard=True):
    if not isinstance(value, dict):
        return ["缺少初始能力、必要增量与最小正确方案分析"]
    errors = []
    for key, label in (("baseline_capabilities", "初始代码已有能力"),
                       ("required_delta", "本轮必要增量"),
                       ("minimal_solution", "最小正确方案")):
        if not isinstance(value.get(key), str) or not value[key].strip():
            errors.append(f"缺少{label}依据")
    if not isinstance(value.get("routine_solution_sufficient"), bool):
        errors.append("未判断常规方案是否足够")
    elif require_hard and value["routine_solution_sufficient"]:
        errors.append("常规方案即可满足题面，不能判为困难")
    for key, label in (("design_tradeoffs", "关键设计取舍"),
                       ("verification_obligations", "不变量、反例与验证判据")):
        items = value.get(key)
        if not isinstance(items, list) or any(not isinstance(x, str) or not x.strip() for x in items):
            errors.append(f"{label}格式无效")
        elif require_hard and not items:
            errors.append(f"困难评级缺少{label}")
    return errors


def blind_materials(value):
    """Keep requirements/code context, remove the author's difficulty verdict."""
    if isinstance(value, dict):
        return {key: blind_materials(item) for key, item in value.items() if key not in {
            "difficulty", "task_difficulty", "difficulty_review", "complexity_analysis",
            "hard_drivers", "human_work_reasons", "difficulty_preflight",
            "_concept_retry_feedback", "_concept_overlap_feedback", "_notebook_feedback",
        }}
    if isinstance(value, list):
        return [blind_materials(item) for item in value]
    return value


def rejected_direction(validation):
    """A substantively easy direction must be replaced, not decorated in a rewrite."""
    review = validation.get("difficulty_review") or {}
    if not isinstance(review, dict):
        return False
    level = review.get("level")
    return (level in {"简单", "中等"}
            or (level in HARD_LEVELS and review.get("routine_solution_sufficient") is True)
            or review.get("inflated_label_risk") is True)
