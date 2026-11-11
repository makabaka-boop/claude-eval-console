"""Check fault feasibility before planting; assess difficulty on the frozen fault."""
import json
import re
import uuid
from pathlib import Path

from .generation_policy import (
    BUG_FEASIBILITY_GUIDANCE,
    BUG_NOVELTY_GUIDANCE,
    BUG_STATIC_GUIDANCE,
    BUG_V2_MAX_PROBLEMS,
    BUG_V2_MIN_PROBLEMS,
    VERSION,
)

DESIGN_SECONDS = 5 * 60
REVIEW_SECONDS = 3 * 60
V2_DESIGN_CANDIDATE_LIMIT = 4
V2_PLANT_CANDIDATE_LIMIT = 2

V2_SOURCE_SUFFIXES = {
    '.c', '.cc', '.cpp', '.cs', '.go', '.java', '.js', '.jsx', '.kt', '.php',
    '.py', '.rb', '.rs', '.swift', '.ts', '.tsx', '.vue',
}
V2_SOURCE_IGNORED_PARTS = {
    '.git', '.github', 'coverage', 'dist', 'docs', 'fixtures', 'generated',
    'mocks', 'node_modules', 'test', 'tests', '__tests__', 'vendor',
}
V2_COMPLEXITY_PATTERNS = {
    'state_lifecycle': re.compile(
        r'\b(state|status|phase|transition|workflow|session|checkpoint|resume|restore|draft)\w*\b', re.I),
    'persistence': re.compile(
        r'\b(database|sqlite|postgres|mysql|redis|storage|repository|persist|migration|cache)\w*\b', re.I),
    'async_ordering': re.compile(
        r'\b(async|await|queue|worker|event|stream|retry|backoff|timeout|sequence|ordering|late|stale)\w*\b', re.I),
    'consistency': re.compile(
        r'\b(transaction|atomic|lock|mutex|version|revision|dedup|idempot|conflict|rollback|reconcile)\w*\b', re.I),
    'algorithmic': re.compile(
        r'\b(parser|token|graph|route|schedule|planner|geometry|codec|checksum|index|rank|sort|merge|aggregate)\w*\b', re.I),
}


V2_DIFFICULTY_BASES = (
    '跨阶段状态重建', '多个合理根因排除', '耦合约束取舍', '非局部验证义务', '局部修法边界',
)


def design_schema(min_problems=2, max_problems=4, *, structural_preflight=False):
    properties = {key: {"type": "string"} for key in (
        'affected_flow', 'original_invariants', 'proposed_change', 'trigger',
        'observable_failure', 'expected_behavior', 'repair_constraints',
        'diagnosis_challenges', 'regression_scenarios')}
    properties['code_locations'] = {'type': 'array', 'items': {'type': 'string'}}
    problem_properties = {key: {'type': 'string'} for key in (
        'trigger', 'observable_failure', 'expected_behavior', 'repair_constraints')}
    problem_properties['code_locations'] = {'type': 'array', 'items': {'type': 'string'}}
    properties['problems'] = {
        'type': 'array', 'minItems': min_problems, 'maxItems': max_problems,
        'items': {'type': 'object', 'properties': problem_properties,
                  'required': list(problem_properties), 'additionalProperties': False},
    }
    if structural_preflight:
        properties.update({
            'routine_path': {'type': 'string'},
            'routine_assessment': {
                'type': 'string', 'enum': ['足够', '不足够', '有争议'],
            },
            'root_cause_visibility': {
                'type': 'string', 'enum': ['直接暴露', '需跨函数追踪', '需跨阶段重建'],
            },
            'plausible_alternative_causes': {
                'type': 'array', 'items': {'type': 'string'},
            },
            'difficulty_bases': {
                'type': 'array', 'items': {'type': 'string', 'enum': list(V2_DIFFICULTY_BASES)},
            },
            'existing_direct_verification': {'type': 'boolean'},
            'boundary_counterexample': {'type': 'string'},
        })
    candidate = {'type': 'object', 'properties': properties, 'required': list(properties),
                 'additionalProperties': False}
    candidate_limit = V2_DESIGN_CANDIDATE_LIMIT if structural_preflight else 2
    return {'type': 'object', 'properties': {
        'candidates': {'type': 'array', 'items': candidate, 'maxItems': candidate_limit},
        'unavailable_reason': {'type': 'string'},
    }, 'required': ['candidates', 'unavailable_reason'], 'additionalProperties': False}


def v2_structural_risk(candidate):
    """Return a cheap pre-plant risk reason, never a final difficulty verdict."""
    problems = candidate.get('problems') or []
    bundle = len(problems) > 1
    bases = set(candidate.get('difficulty_bases') or [])
    if not bases:
        return '没有指出可核对的跨阶段、取舍或非局部验证依据'
    if (bases == {'局部修法边界'}
            and not str(candidate.get('boundary_counterexample') or '').strip()):
        return '仅声称局部修法会遗漏边界，但没有给出具体反例'
    alternatives = [str(item).strip() for item in
                    (candidate.get('plausible_alternative_causes') or []) if str(item).strip()]
    strong_bases = bases.intersection({
        '跨阶段状态重建', '多个合理根因排除', '耦合约束取舍', '非局部验证义务',
    })
    # This pass does not assign the final level.  It only prevents clearly
    # local/routine bundles from consuming patching and blind-review calls.
    # A disputed card may still be worth freezing when it names concrete
    # non-local evidence. The frozen-code blind review remains the final gate.
    assessment = candidate.get('routine_assessment')
    if assessment == '足够':
        return '设计卡已确认常规定位、局部修复与验证路径足够'
    if assessment == '有争议' and not (
            bundle and candidate.get('root_cause_visibility') != '直接暴露'
            and len(strong_bases) >= 2 and len(alternatives) >= 2
            and candidate.get('existing_direct_verification') is False):
        return '有争议的设计卡缺少足够具体的跨模块诊断或非局部验证依据'
    if assessment not in ('不足够', '有争议'):
        return '设计卡缺少常规修法风险判断'
    if (candidate.get('root_cause_visibility') == '直接暴露'
            and candidate.get('existing_direct_verification') is True
            and len(strong_bases) < 2
            and len(alternatives) < 2
            and not str(candidate.get('boundary_counterexample') or '').strip()):
        return '根因在局部代码直接暴露，且已有验证可直接覆盖标准修法'
    if (candidate.get('root_cause_visibility') == '直接暴露'
            and len(strong_bases) < 2
            and len(alternatives) < 2):
        return ('多个局部问题仍只指向直接根因，没有可核验的共同困难驱动'
                if bundle else '单一现象只指向局部根因，没有可核验的困难依据')
    return ''


def _v2_risk_score(candidate):
    """Rank surviving cards without pretending this is the blind verdict."""
    visibility = {'直接暴露': 0, '需跨函数追踪': 2, '需跨阶段重建': 4}
    bases = set(candidate.get('difficulty_bases') or [])
    alternatives = [item for item in candidate.get('plausible_alternative_causes') or []
                    if str(item).strip()]
    return (
        2 if candidate.get('routine_assessment') == '不足够' else 1,
        visibility.get(candidate.get('root_cause_visibility'), 0),
        len(bases - {'局部修法边界'}),
        min(len(alternatives), 3),
        0 if candidate.get('existing_direct_verification') is True else 1,
        min(len(candidate.get('problems') or []), 3),
        1 if str(candidate.get('boundary_counterexample') or '').strip() else 0,
    )


def code_files(h, work, locations):
    if not isinstance(locations, list) or not locations:
        raise h.WorkflowError('预置设计缺少可核对的代码位置')
    files = set()
    for location in locations:
        path = (work / str(location).split(':', 1)[0].strip()).resolve()
        if not path.is_relative_to(work.resolve()) or not path.is_file():
            raise h.WorkflowError('预置设计引用了不存在或越界的代码位置')
        files.add(path.relative_to(work.resolve()).as_posix())
    return files


def select(h, source, work, history_context, feedback, progress):
    """No project execution, patch application or freeze before admission."""
    v2 = source.get('bug_generation_flow') == 'bug_generation_v2'
    min_problems, max_problems = BUG_V2_MIN_PROBLEMS, BUG_V2_MAX_PROBLEMS
    def snapshot():
        return tuple(h.run_command(command, cwd=work).stdout.strip() for command in (
            ['git', 'rev-parse', 'HEAD'], ['git', 'rev-parse', 'HEAD^{tree}'],
            ['git', '--no-optional-locks', 'status', '--porcelain', '--untracked-files=all']))

    original = snapshot()
    if original[2]:
        raise h.WorkflowError('预置设计必须从干净源码快照开始')
    root = Path(h.DATA_DIR) / 'seed-design'
    root.mkdir(parents=True, exist_ok=True)
    audit_path = root / f'{uuid.uuid4().hex}.json'
    audit = {'source_pair_id': source['pair'].get('id', ''), 'source_sha': original[0],
             'source_tree': original[1], 'policy_version': VERSION,
             'verification_mode': source.get('bug_verification_mode', 'static_first'),
             'runtime_verified': False, 'created_at': h.now_text()}

    def save():
        audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding='utf-8')

    def unchanged():
        if snapshot() != original:
            raise h.WorkflowError('预置设计或独立预审修改了源码，拒绝继续埋入')

    save()
    progress('正在静态设计候选缺陷，核对故障链与现有约束')
    relationship_rule = (
        f'每张设计卡包含 {min_problems} 至 {max_problems} 个可区分问题；不同根因或生产文件必须共同破坏'
        '同一用户目标与业务不变量，并能由一次最小修复和一组针对性回归闭环。'
        '可独立删除而不影响主故障的附加问题应舍弃；不能把同一现象拆句凑数。'
        if v2 else
        f'每张设计卡必须包含 {min_problems} 至 {max_problems} 个可分别观察的问题，集中在同一用户流程或紧密相关功能；'
    )
    file_rule = (
        '植入只可修改已有业务源码；有界修复包确有必要时，也可修改已有生产运行配置、协议或数据格式定义、'
        '数据库模式或迁移。不得修改测试、README、依赖清单或锁文件、CI、Docker 或构建配置，不得新增文件。'
        if v2 else
        '只改已有业务源码，不改测试、配置、依赖或交付文件，保持正常路径；'
    )
    design_prompt = (
        '直接埋 Bug 的设计阶段，不先搜索自然缺陷。只读源码，暂不写补丁、题面或修改文件。'
        '一次调用最多提出两个简短缺陷设计卡，均须由现有业务代码支撑并具有可核验的困难依据；'
        '按证据强弱排序，第一项作为候选 A，第二项仅作备用 B；确实无候选可返回空列表。'
        '先从需求涉及的入口与核心源码选定一条用户流程，只追踪支撑因果关联所必需的依赖；'
        '找到两个可信方向或已经穷尽当前主流程后停止，不逐一论证所有其他模块。先完成结构化输出。'
        '优先检查现有多约束相互作用的正确性机制；没有这些机制就换方向，不新增架构或需求制造难度。'
        + relationship_rule +
        'problems 中每项写自己的触发条件、错误结果、正确状态、修复约束与代码位置。'
        '这些问题可以有不同根因并位于不同生产文件，但必须属于同一故障链，不能把一个现象换措辞拆成多条。'
        '卡片顶层写这组问题的有界产品范围、各自原有不变量、整体植入变化与联合回归约束。'
        'diagnosis_challenges 写开发者仅凭故障现象与带缺陷源码定位问题时，需追踪的数据流、'
        '区分的合理根因与相互作用的真实约束；regression_scenarios 写必须保留的不变量和具体边界。'
        '这些是待核验线索，不是困难结论。你知道正确基线，恢复原实现当然可能修好；'
        '不能因此直接淘汰，也不要求为已知正确修法编造失败反例。'
        '补丁局部、恢复一处条件或删除多余逻辑都不能在设计阶段自动判简单；这些信息只作为候选选择线索。'
        '若从用户现象定位到局部根因必须重建算法不变量、跨阶段状态或 rank/版本映射，并区分多个合理根因，'
        '应完整记录该诊断线索。只有冻结源码盲审恰好达到困难时才可进入 A/B；简单、中等和地狱都必须拒绝。'
        '设计者和真实性审核均不得代替难度审核提前定级；'
        '只要冻结后的故障源码可能要求跨函数/跨阶段诊断、排除多个合理根因、处理真实约束取舍或承担非局部验证义务，'
        '就应输出候选交给后续盲审，routine_assessment=有争议也可以进入，不得在这里要求困难结论。'
        '不能凭改动行数或原项目难度判难，也不能为阻止简单修法虚构性能约束。'
        + ('' if not v2 else
           'V2 还要在同一张设计卡完成植入前的常规修法风险记录：routine_path 写开发者最直接的完整定位、修复和验证路径；'
           'routine_assessment 如实判断该路径是否已经足够。root_cause_visibility 判断用户现象是否会直接指向局部根因；'
           'plausible_alternative_causes 只列从用户现象和当前代码确实合理、需要排除的其他根因，不得凑数。'
           'difficulty_bases 只选择真实存在的跨阶段状态重建、多个合理根因排除、耦合约束取舍、非局部验证义务或局部修法边界；'
           'existing_direct_verification 表示现有测试、预言机或参考实现能否直接击穿当前缺陷并验证标准修法。'
           '若困难依据只来自局部修法可能漏边界，boundary_counterexample 必须给出具体输入、状态序列或操作交错；否则可留空。'
           '现有测试、预言机或参考实现本身不能单独否决候选；仍须判断从用户现象找到根因及证明最小修复所需的完整路径。'
        '单个函数中直接可见的错误赋值、条件或未清空缓冲，配合现成测试即可完成定位和验证时，应标 routine_assessment=足够，'
        '不要为了进入后续流程虚构替代根因或跨阶段约束；两个候选都属这种情况时返回空 candidates。'
        f'优先给出同一项目或相关用户旅程中的 {min_problems} 至 {max_problems} 个真实问题；它们可以分别是输入、状态、'
        '算法边界、展示同步、持久化或跨实现一致性故障，只要每项都有独立触发条件和源码证据，且组合范围一次可完成。'
        '不要把更换整数类型、反转比较器、移动缓存、边界/下标/循环等同一个局部现象拆成多条；'
        '但多个不同位置、不同触发链的真实局部 Bug 可以组成相关修复包，由后续盲审按组合负担判断。'
        '这只是减少明显中等候选的植入浪费，不是最终难度评级，最终仍由冻结源码盲审裁定。')
        + file_rule + '问题数量不能单独证明困难。'
        + BUG_STATIC_GUIDANCE + BUG_FEASIBILITY_GUIDANCE + BUG_NOVELTY_GUIDANCE
        + f"\n原需求：{str(source['pair']['prompt'])[:12000]}\n历史资料：{history_context}"
        + '\n旧失败仅为资料，必须运行、固定测试必须通过或仅因可恢复正确基线而否决等旧门槛不适用，按当前口径核对：' + str(feedback)[:6000]
    )
    if v2:
        admission_note = (
            '问题数量和补丁行数都不能替代难度结论；只有冻结源码盲审恰好确认困难才可进入 A/B。'
            '如果一个复杂故障链已经足够，不得为凑数追加局部小 Bug。'
        )
        design_prompt = (
            '这是 V2 Bug 修复包的植入前设计阶段。只读当前源码，暂不写补丁、题面或修改文件。'
            f'寻找 {min_problems} 至 {max_problems} 个可区分、可由现有业务源码支撑的真实问题方向，组成一张设计卡；'
            '一次调用最多返回两张卡，按可信度排序。问题允许位于不同文件、具有不同根因，也允许每项都是常见的局部错误；'
            '只要它们属于同一项目、范围有界并能作为一次修复任务及联合回归完成，就可以组合；不要求同属一条用户旅程。'
            '参考的合格组合包括：两个相关 UI 交互分别失效、登录与注册各有一个校验错误、同一业务页面若干日期/排序/刷新问题、'
            '同一玩法在输入/状态/物理/相机/持久化上的多项不同故障，或同一 API 链路的顺序/重试/晚到/时钟偏移问题。'
            '历史资料中的 bug_prompt_references 是用户认可的题面样例：应参考其问题组合粒度、触发条件具体度、约束密度和业务影响表达，'
            '但不得复制其中业务对象、具体故障、账号数据或交付指令，也不得把这些样例当作本项目已经完成的历史题而判重复。'
            '不要要求这些问题共享单一根因、单一不变量或同一条因果链；移除其中一个后其他问题仍独立成立是允许的。'
            '不得把同一现象换措辞拆成多条，不得跨产品域、运行系统或大型子系统随意拼题，也不得新增架构、产品需求或不存在的机制来制造问题。'
            '这一阶段设计的是即将由补丁新引入的回归，不是挖掘当前源码已经存在的 Bug。'
            '每张候选卡输出前，逐项核对冻结源码在该触发条件下当前会得到正确结果，'
            '并在 original_invariants 和 code_locations 中给出现有正确实现的正向代码依据。'
            '如果当前源码已经会产生候选所述的同一可观察错误，该项无效，必须换方向，'
            '不得把“加深现有错误”、注释或无行为差异的补丁写成新植入故障。'
            '优先横向查看当前产品已有的多个用户可见功能，再选择源码证据最清楚、触发与预期最具体的一组；'
            '找到足够问题后停止，不需要穷尽仓库。problems 中每项分别写触发条件、错误结果、正确行为、修复约束与代码位置；'
            '顶层字段概括共同产品范围、现有不变量、植入变化和联合回归场景。'
            'routine_path、routine_assessment、root_cause_visibility、plausible_alternative_causes、difficulty_bases、'
            'existing_direct_verification 与 boundary_counterexample 必须如实填写，但只用于后续独立难度审核，不是本阶段淘汰条件。'
            + admission_note +
            f'只有当前源码确实找不到至少 {min_problems} 个可安全植入的问题，或只能靠修改测试、README、依赖/锁文件、CI、Docker、构建配置、'
            '新增文件、新增需求或拼接无关产品域才能凑数时，才返回空 candidates，并具体说明缺少什么。'
            + file_rule + BUG_STATIC_GUIDANCE + BUG_FEASIBILITY_GUIDANCE + BUG_NOVELTY_GUIDANCE
            + f"\n原需求：{str(source['pair']['prompt'])[:12000]}\n历史资料：{history_context}"
            + '\n旧失败只作查重和避坑资料，不沿用其中“必须跨阶段、必须非局部、局部修法足够就判空”的旧门槛：'
            + str(feedback)[:6000]
        )
    proposal = h.run_codex_structured(
        design_prompt,
        design_schema(min_problems, max_problems, structural_preflight=v2),
        work, 'seeded-bug-design', DESIGN_SECONDS,
        model=h.ITERATION_GENERATION_MODEL, sandbox='read-only')
    if v2 and not proposal.get('candidates'):
        audit['initial_proposal'] = proposal
        save()
        progress('首轮未找到方向，正在横向复查同一项目的相关真实问题')
        proposal = h.run_codex_structured(
            design_prompt
            + '\n首轮设计返回空候选：'
            + str(proposal.get('unavailable_reason') or '未说明')[:2500]
            + '\n请横向复查当前产品的现有用户入口，不再专门寻找跨阶段或非局部机制。'
              '可分别检查输入交互、选择/编辑、校验、排序过滤、日期时间、异步刷新、展示同步、持久化、错误恢复和边界处理。'
              f'从中选择 {min_problems} 至 {max_problems} 个源码证据清楚、触发和预期可分别描述的问题组成一题；'
              '一个复杂故障链可以单独成题，不得为了数量拼接局部小 Bug。'
              f'只有确实无法找到至少 {min_problems} 个合格问题时才返回空列表。',
            design_schema(min_problems, max_problems, structural_preflight=True),
            work, 'seeded-bug-design', DESIGN_SECONDS,
            model=h.ITERATION_GENERATION_MODEL, sandbox='read-only')
    audit['proposal'] = proposal
    save()
    unchanged()
    candidates = proposal.get('candidates')
    if not isinstance(candidates, list) or len(candidates) > 2:
        raise h.WorkflowError('预置设计返回的候选列表无效')
    if not candidates:
        prefix = '本来源未找到可合规预置的缺陷方向：'
        raise h.WorkflowError(
            prefix + str(proposal.get('unavailable_reason') or '设计阶段未找到合格方向')[:2000])
    candidate_schema = design_schema(
        min_problems, max_problems, structural_preflight=v2)['properties']['candidates']['items']
    for candidate in candidates:
        allowed_empty = {'code_locations', 'problems', 'plausible_alternative_causes',
                         'boundary_counterexample'}
        missing = [] if isinstance(candidate, dict) else list(candidate_schema['required'])
        if isinstance(candidate, dict):
            for key in candidate_schema['required']:
                if key in allowed_empty:
                    continue
                if key == 'existing_direct_verification':
                    if not isinstance(candidate.get(key), bool):
                        missing.append(key)
                elif not candidate.get(key):
                    missing.append(key)
        if missing:
            raise h.WorkflowError('预置设计缺少故障链或最小修复证据')
        code_files(h, work, candidate.get('code_locations'))
        problems = candidate.get('problems')
        if not isinstance(problems, list) or not min_problems <= len(problems) <= max_problems:
            raise h.WorkflowError(
                f'每张预置设计必须包含 {min_problems} 至 {max_problems} 个真实问题')
        identities = set()
        for problem in problems:
            if (not isinstance(problem, dict)
                    or any(not problem.get(k) for k in candidate_schema['properties']['problems']['items']['required'])):
                raise h.WorkflowError('预置设计的问题缺少触发条件、结果、约束或代码位置')
            code_files(h, work, problem.get('code_locations'))
            identity = (str(problem['trigger']).strip().casefold(),
                        str(problem['observable_failure']).strip().casefold())
            if identity in identities:
                raise h.WorkflowError('预置设计把同一问题重复拆分，不能凑数量')
            identities.add(identity)

    if v2:
        # V2 deliberately postpones authenticity and duplicate adjudication
        # until after the patch has been applied to a frozen faulty snapshot.
        # Doing it here would make the correct baseline and proposed change an
        # accidental difficulty signal and would add a redundant model call.
        screened = []
        rejected = []
        for source_index, candidate in enumerate(candidates):
            reason = v2_structural_risk(candidate)
            if reason:
                rejected.append({'source_candidate_index': source_index, 'reason': reason})
            else:
                screened.append((source_index, candidate))
        screened.sort(key=lambda item: _v2_risk_score(item[1]), reverse=True)
        audit['structural_risk_rejections'] = rejected
        audit['ranked_source_indices'] = [index for index, _ in screened]
        save()
        if not screened:
            raise h.WorkflowError(
                '本来源 V2 设计卡均命中常规修法高风险，植入前已换源码：'
                + '；'.join(f"候选 {item['source_candidate_index'] + 1}：{item['reason']}"
                           for item in rejected)[:2000])
        approved = [
            {'candidate_id': chr(ord('A') + rank), 'candidate_index': rank,
             'source_candidate_index': source_index,
             'candidate': candidate, 'review': {'status': 'deferred_until_frozen',
                                                 'structural_risk': 'passed'},
             'source_sha': original[0], 'source_tree': original[1],
             'policy_version': VERSION, 'audit_path': str(audit_path),
             'generation_flow': 'bug_generation_v2'}
            for rank, (source_index, candidate) in enumerate(screened)
        ]
        audit['approved_indices'] = list(range(len(approved)))
        audit['selected_index'] = 0
        audit['preplant_review'] = 'deferred_until_frozen'
        save()
        return {**approved[0], 'alternates': approved[1:], 'approved_designs': approved}

    properties = {
        'candidate_index': {'type': 'integer'}, 'approved': {'type': 'boolean'},
        'fault_supported': {'type': 'boolean'}, 'single_chain': {'type': 'boolean'},
        'reason': {'type': 'string'}, 'code_evidence': {'type': 'array', 'items': {'type': 'string'}},
        'scope_preserved': {'type': 'boolean'}, 'not_duplicate': {'type': 'boolean'},
    }
    schema = {'type': 'object', 'properties': {'reviews': {'type': 'array', 'items': {
        'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}}},
        'required': ['reviews'], 'additionalProperties': False}
    # This reviewer sees the correct baseline, so it only verifies authenticity,
    # causal focus and scope. Difficulty belongs exclusively to the later blind gate.
    blind = [{k: v for k, v in c.items() if k not in {'diagnosis_challenges', 'regression_scenarios'}}
             for c in candidates]
    progress('正在独立预审缺陷真实性、因果关联与范围')
    result = h.run_codex_structured(
        '独立静态评审拟植入缺陷，尚未应用补丁，不运行项目或测试。每个候选返回且仅返回一项 reviews，'
        'candidate_index 从 0 开始。先重新阅读现有代码，推导植入后的触发条件与可观察行为差异。'
        '本阶段只审核缺陷真实性、历史重复风险、同一用户流程下的因果关联和范围；不评定也不预筛难度。'
        '冻结后必须由只看故障源码和题面的独立盲审给出唯一出题难度结论。'
        '你可见正确基线与拟变更，不能把恢复原条件、撤销植入或删除新增逻辑当成开发者已知答案。'
        'fault_supported 只有 problems 中每个问题都能从源码及拟变更推导明确错误才为 true；'
        f'single_chain 是兼容字段，表示 {min_problems} 至 {max_problems} 个问题属于同一项目内一次可完成的有界修复包；'
        '允许不同根因和生产文件，但多个现象须共同破坏同一业务不变量，并能在一次最小修复与针对性回归中闭环；'
        '需要独立用户流程或独立验收的附加问题应拒绝；'
        'scope_preserved 要求保留已有业务、正常路径和契约，不新增需求、架构或无关故障。'
        'not_duplicate 只有在比较所给历史后，没有确认同一具体触发或状态序列、同一错误分支或不变量、'
        '以及实质相同的失败结果时才为 true；仅共享根因类别、校验器、模块、业务对象、正常功能契约或机制家族不能判重复。'
        '不能因为拟补丁短、可恢复某个条件或设计者知道正确基线就拒绝，也不能因看起来复杂就批准难度。'
        '真实性、因果关联和范围成立才批准；证据引用真实代码位置。通过只表示允许生成补丁和题面。'
        + BUG_STATIC_GUIDANCE + BUG_FEASIBILITY_GUIDANCE + BUG_NOVELTY_GUIDANCE
        + f"\n原需求：{str(source['pair']['prompt'])[:12000]}\n候选：{json.dumps(blind, ensure_ascii=False)}",
        schema, work, 'seeded-bug-design-review', REVIEW_SECONDS,
        model=h.REVIEW_MODEL, sandbox='read-only')
    audit['review'] = result
    save()
    unchanged()
    reviews = result.get('reviews')
    if (not isinstance(reviews, list) or any(not isinstance(r, dict) for r in reviews)
            or any(type(r.get('candidate_index')) is not int for r in reviews)
            or sorted(r['candidate_index'] for r in reviews) != list(range(len(candidates)))):
        raise h.WorkflowError('预置设计独立预审缺项或重复，不能生成补丁')
    failures = []
    approved = []
    for review in sorted(reviews, key=lambda item: item['candidate_index']):
        index = review['candidate_index']
        if (review.get('approved') is True and review.get('fault_supported') is True
                and review.get('single_chain') is True and review.get('scope_preserved') is True
                and review.get('not_duplicate') is True):
            cited = code_files(h, work, review.get('code_evidence'))
            if not cited.intersection(code_files(h, work, candidates[index]['code_locations'])):
                raise h.WorkflowError('预置设计独立预审的代码依据偏离候选')
            for problem in candidates[index]['problems']:
                if not cited.intersection(code_files(h, work, problem['code_locations'])):
                    raise h.WorkflowError('预置设计独立预审没有逐项覆盖全部问题的代码依据')
            approved.append({'candidate_id': chr(ord('A') + index), 'candidate_index': index,
                             'candidate': candidates[index], 'review': review,
                             'source_sha': original[0], 'source_tree': original[1],
                             'policy_version': VERSION, 'audit_path': str(audit_path)})
        else:
            failures.append(f"方向 {index + 1}：{review.get('reason', '')}")
    if approved:
        audit['approved_indices'] = [item['candidate_index'] for item in approved]
        audit['selected_index'] = approved[0]['candidate_index']
        save()
        return {**approved[0], 'alternates': approved[1:], 'approved_designs': approved}
    raise h.WorkflowError('本来源未找到可合规预置的缺陷方向：预置设计独立预审未通过；'
                          + '；'.join(failures)[:2000] + f'；设计记录：{audit_path}')


def v2_source_complexity_profile(h, work):
    """Conservatively reject only sources with too little production surface.

    This is deliberately a cheap static screen.  It prioritizes repositories
    with state, persistence, ordering, consistency or algorithmic mechanisms,
    while allowing a sufficiently substantial codebase even when its domain
    vocabulary does not match our signal list.
    """
    tracked = h.run_command(['git', 'ls-files', '-z'], cwd=work).stdout.split('\0')
    production = []
    for relative in tracked:
        if not relative:
            continue
        path = Path(relative)
        parts = {part.casefold() for part in path.parts}
        if path.suffix.casefold() not in V2_SOURCE_SUFFIXES:
            continue
        if parts & V2_SOURCE_IGNORED_PARTS:
            continue
        if re.search(r'(^test_|[._](?:test|spec)\.)', path.name, re.I):
            continue
        production.append(path)

    families = {name: [] for name in V2_COMPLEXITY_PATTERNS}
    line_count = 0
    scanned_bytes = 0
    for relative in production[:160]:
        path = work / relative
        try:
            if path.stat().st_size > 300_000 or scanned_bytes >= 2_000_000:
                continue
            text = path.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue
        scanned_bytes += len(text.encode('utf-8', errors='ignore'))
        line_count += sum(1 for line in text.splitlines() if line.strip())
        material = relative.as_posix() + '\n' + text
        for name, pattern in V2_COMPLEXITY_PATTERNS.items():
            if pattern.search(material):
                families[name].append(relative.as_posix())

    matched = [name for name, paths in families.items() if paths]
    directories = {path.parent.as_posix() for path in production}
    eligible = (
        len(production) >= 2
        and (len(matched) >= 2 or line_count >= 500
             or (len(production) >= 6 and len(directories) >= 2))
    )
    reasons = []
    if len(production) < 2:
        reasons.append(f'仅发现 {len(production)} 个生产源码文件')
    if len(matched) < 2 and line_count < 500:
        reasons.append(f'只识别到 {len(matched)} 类复杂机制且有效源码约 {line_count} 行')
    if len(production) < 6 and len(directories) < 2:
        reasons.append('生产代码面较窄，缺少可供跨函数或跨阶段诊断的结构')
    return {
        'eligible': eligible,
        'production_file_count': len(production),
        'production_line_count': line_count,
        'directory_count': len(directories),
        'mechanism_families': matched,
        'mechanism_files': {name: paths[:5] for name, paths in families.items() if paths},
        'reason': '；'.join(reasons),
    }
