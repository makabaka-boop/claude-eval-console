"""Regression cases for the 09/18 prompt/scheduler failure investigation."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from pairwise import auto_refill as refill, seeded_bug, generation_policy, natural_bug
from test_pairwise import host, sample_complexity, sample_difficulty
from test_seeded_bug import source_work, fake_seed_model


class PromptCaptured(Exception):
    pass


def captured_prompt(monkeypatch, host, call):
    prompts = []
    def capture(prompt, *args, **kwargs):
        prompts.append(prompt)
        raise PromptCaptured()
    monkeypatch.setattr(host, 'run_codex_structured', capture)
    with pytest.raises(PromptCaptured):
        call()
    return prompts[0]


@pytest.mark.parametrize('kind', ['Feature 迭代', 'Bug 修复'])
def test_first_concept_does_not_invent_rule_c_history(host, monkeypatch, kind):
    context = {'repo_path': '.', '_pairwise_mode': True,
               '_concept_retry_feedback': '只读检查临时 main；常规方案足够时更换方向。'}
    prompt = captured_prompt(monkeypatch, host,
                            lambda: host.run_codex_iteration_concept(context, kind))
    assert '只读检查临时 main' in prompt
    assert '上一张概念卡已被规则 C 拒绝' not in prompt
    assert '上一方向未通过规则 C' not in prompt
    assert '已确认的规则 C 证据' not in prompt


def test_confirmed_rejection_does_not_redirect_a_fixed_bug(host, monkeypatch):
    context = {'repo_path': '.', '_pairwise_mode': True,
               '_seeded_defect': {'affected_flow': '并发回执'},
               '_concept_overlap_feedback': '已确认重复的回执修复'}
    prompt = captured_prompt(monkeypatch, host,
                            lambda: host.run_codex_iteration_concept(context, 'Bug 修复'))
    assert '已确认重复的回执修复' in prompt
    assert '不能擅自改选其他缺陷' in prompt
    assert '请检查不同的真实缺陷家族' not in prompt


@pytest.mark.parametrize('kind,frozen,expected_calls', [
    ('Bug 修复', '_seeded_defect', 1),
    ('Bug 修复', '_existing_defect', 1),
    ('Bug 修复', None, 2),
    ('Feature 迭代', None, 2),
])
def test_fixed_bug_does_not_repeat_rejected_difficulty_direction(host, monkeypatch, kind, frozen, expected_calls):
    context = {'repo_path': '.', '_pairwise_mode': True,
               'same_repository_history': [{'prompt': '历史题'}]}
    if frozen:
        context[frozen] = {'affected_flow': '固定且已核验的缺陷'}
    calls = []
    def reject(ctx, task_type):
        calls.append(ctx)
        raise host.WorkflowError('概念难度预检：常规方案足够或缺少可验证的反例')
    monkeypatch.setattr(host, 'run_codex_iteration_concept', reject)
    with pytest.raises(host.WorkflowError, match='概念难度预检'):
        host._generate_iteration_candidate_for_context(context, kind)
    assert len(calls) == expected_calls


@pytest.mark.parametrize('stage', ['concept', 'generation', 'validation'])
def test_feature_policy_is_removed_only_from_final_prompt_generation(host, monkeypatch, stage):
    context = {'repo_path': '.', '_pairwise_mode': True, '_feature_shape_mode': 'complete_module'}
    call = {
        'concept': lambda: host.run_codex_iteration_concept(context, 'Feature 迭代'),
        'generation': lambda: host.run_codex_iteration_generation(context, '', 'Feature 迭代'),
        'validation': lambda: host.run_codex_iteration_validation(context, {'prompt': '样例'}, 'Feature 迭代'),
    }[stage]
    prompt = captured_prompt(monkeypatch, host, call)
    assert generation_policy.FEATURE_GUIDANCE in prompt
    assert '第一次 Feature，必须优先设计完整新模块' not in prompt
    assert '本次 Feature 的形态已经固定为完整新模块' not in prompt
    assert '两种形态都不能把同仓库历史题目的主结果' not in prompt
    assert '即使技术内容完整也必须 approved=false' not in prompt
    if stage != 'generation':
        assert '困难' in prompt and '核心行为' in prompt


def test_root_depth_does_not_override_the_generator_scope_budget(host, monkeypatch):
    prompt = captured_prompt(monkeypatch, host, lambda: host.run_codex_task_generation(
        19, '纯前端', [], refill.ROOT_DEPTH_GUIDANCE))
    assert '允许多个必要技术难点' not in prompt
    assert '复杂机制与自定义算法合计最多一项' in prompt


@pytest.mark.parametrize('count', [0, 1, 2])
@pytest.mark.parametrize('stage', ['concept', 'generation', 'validation'])
def test_third_feature_novelty_gate_is_known_before_review(host, monkeypatch, count, stage):
    context = {'repo_path': '.', '_pairwise_mode': True,
               'iteration_policy': {'iteration_count': count}}
    call = {
        'concept': lambda: host.run_codex_iteration_concept(context, 'Feature 迭代'),
        'generation': lambda: host.run_codex_iteration_generation(context, '', 'Feature 迭代'),
        'validation': lambda: host.run_codex_iteration_validation(context, {'prompt': '样例'}, 'Feature 迭代'),
    }[stage]
    prompt = captured_prompt(monkeypatch, host, call)
    assert generation_policy.FEATURE_GUIDANCE in prompt
    assert ('必须同时具有新的业务对象、新的状态生命周期和新的用户主流程' in prompt) == (count >= 2)
    assert ('本轮已到根项目的第三条后续任务' in prompt) == (count >= 2)


def test_feature_overlap_distinguishes_new_core_from_preserved_baseline(host, monkeypatch):
    context = {'repo_path': '.', '_pairwise_mode': True,
               'same_repository_history': [{'reference_id': 'pair:root',
                                            'prompt': '非法导入保留上次有效拓扑'}]}
    prompt = captured_prompt(monkeypatch, host, lambda:
        host.run_codex_same_repository_overlap_validation(
            context, {'prompt': '新增可抢占 Worker 会话；非法导入保留旧拓扑'}, 'Feature 迭代'))
    assert '不是任意一项辅助要求' in prompt
    assert '不能单独把这个字段设为 true' in prompt
    assert '把旧核心换名、换输入或附加次要新内容仍属重复' in prompt
    medium = {'overlap_level': 'medium', 'closest_history_reference': 'pair:root',
              'same_core_behavior_result': False, 'same_defect_chain': False,
              'reason': '仅共有基线回归要求，新增核心尚待完整题面确认'}
    assert not host.same_repository_overlap_feedback(medium, defer_unconfirmed_medium=True)
    medium['same_core_behavior_result'] = True
    assert host.same_repository_overlap_feedback(medium, defer_unconfirmed_medium=True)
    medium.update(same_core_behavior_result=False, overlap_level='high')
    assert host.same_repository_overlap_feedback(medium, defer_unconfirmed_medium=True)


@pytest.mark.parametrize('stage', ['discovery', 'concept', 'generation', 'overlap', 'validation'])
def test_bug_novelty_policy_is_shared_across_discovery_and_review(host, monkeypatch, stage):
    context = {'repo_path': '.', '_pairwise_mode': True,
               'same_repository_history': [{'reference_id': 'pair:root',
                                            'task_type': 'Feature 迭代',
                                            'prompt': '导入后结果应保持正确'}]}
    call = {
        'discovery': lambda: natural_bug.discover(host, {'pair': {'prompt': '功能要求'}}, Path('.'), '[]'),
        'concept': lambda: host.run_codex_iteration_concept(context, 'Bug 修复'),
        'generation': lambda: host.run_codex_bugfix_generation(context),
        'overlap': lambda: host.run_codex_same_repository_overlap_validation(
            context, {'prompt': '修复新发现的跨代回写故障'}, 'Bug 修复'),
        'validation': lambda: host.run_codex_bugfix_validation(context, {'prompt': '故障题面'}),
    }[stage]
    prompt = captured_prompt(monkeypatch, host, call)
    if stage == 'generation':
        assert generation_policy.BUG_NOVELTY_GUIDANCE not in prompt
        assert generation_policy.BUG_STATIC_GUIDANCE not in prompt
        assert '基于本项目出一个 Bug 修复需求' in prompt
        assert '多找几个 Bug，稍微难一点' in prompt
    else:
        assert generation_policy.BUG_NOVELTY_GUIDANCE in prompt
    assert '同一已验收行为或同一根因' not in prompt
    assert '相同已验收行为或同一根因' not in prompt
    assert '必须换到尚未出题的用户流程' not in prompt
    assert '同一请求解析或字段校验缺陷家族' not in prompt


def test_pairwise_bug_final_generator_leaves_limits_to_validation(host, monkeypatch):
    captured = {}

    class Stop(Exception):
        pass

    def capture(prompt, schema, *args, **kwargs):
        captured['prompt'] = prompt
        captured['schema'] = schema
        captured['kwargs'] = kwargs
        raise Stop()

    monkeypatch.setattr(host, 'run_codex_structured', capture)
    with pytest.raises(Stop):
        host.run_codex_bugfix_generation({
            'repo_path': '.', '_pairwise_mode': True,
            '_existing_defect': {'affected_flow': '提交回执'},
        })

    schema = captured['schema']
    bug_item = schema['properties']['confirmed_bugs']['items']
    assert 'minItems' not in schema['properties']['confirmed_bugs']
    assert 'maxItems' not in schema['properties']['modules']
    assert 'minLength' not in schema['properties']['scope_summary']
    assert 'enum' not in bug_item['properties']['estimated_fix_scope']
    assert generation_policy.BUG_STATIC_GUIDANCE not in captured['prompt']
    assert generation_policy.BUG_NOVELTY_GUIDANCE not in captured['prompt']
    assert '只总结存在的问题，不要说解决方法' in captured['prompt']
    assert captured['kwargs']['model'] == 'gpt-5.6-sol'
    assert captured['kwargs']['reasoning_effort'] == 'high'


def test_bug_medium_function_overlap_goes_to_full_review_but_defect_duplicate_does_not(host):
    review = {'overlap_level': 'medium', 'closest_history_reference': 'pair:feature',
              'same_core_behavior_result': True, 'same_defect_chain': False,
              'reason': '仅历史功能契约相同，无法确认同一故障链'}
    assert not host.same_repository_overlap_feedback(
        review, defer_unconfirmed_medium=True, target_task_type='Bug 修复')
    assert host.same_repository_overlap_feedback(
        review, defer_unconfirmed_medium=True, target_task_type='Feature 迭代')
    assert host.same_repository_overlap_feedback(
        {**review, 'same_defect_chain': True},
        defer_unconfirmed_medium=True, target_task_type='Bug 修复')
    assert not host.same_repository_overlap_feedback(
        {**review, 'overlap_level': 'high'},
        defer_unconfirmed_medium=True, target_task_type='Bug 修复')
    assert host.overlap_requires_full_review({**review, 'overlap_level': 'high'}, 'Bug 修复')
    assert host.same_repository_overlap_feedback(
        {**review, 'overlap_level': 'high', 'same_defect_chain': True},
        defer_unconfirmed_medium=True, target_task_type='Bug 修复')
    assert host.same_repository_overlap_feedback(
        {**review, 'overlap_level': 'high'},
        defer_unconfirmed_medium=True, target_task_type='Feature 迭代')


@pytest.mark.parametrize('missing', ['closest_history_reference', 'reason'])
def test_bug_high_overlap_without_evidence_goes_to_full_review(host, missing):
    review = {'overlap_level': 'high', 'same_defect_chain': True,
              'closest_history_reference': 'pair:old', 'reason': '同一故障的具体依据'}
    review[missing] = ''
    assert not host.same_repository_overlap_feedback(
        review, defer_unconfirmed_medium=True, target_task_type='Bug 修复')
    assert host.overlap_requires_full_review(review, 'Bug 修复')


@pytest.mark.parametrize('confirmed', [None, True])
def test_unconfirmed_high_bug_reaches_independent_review_without_automatic_approval(
        host, monkeypatch, confirmed):
    warning = {'overlap_level': 'high', 'same_defect_chain': False,
               'closest_history_reference': 'pair:feature', 'reason': '只是算法类别相同'}
    monkeypatch.setattr(host, 'ITERATION_GENERATION_ATTEMPTS', 1)
    monkeypatch.setattr(host, 'run_codex_iteration_generation', lambda *a: {'prompt': '具体故障'})
    monkeypatch.setattr(host, 'validate_generated_iteration', lambda *a, **kw: '具体故障')
    monkeypatch.setattr(host, 'run_codex_same_repository_overlap_validation', lambda *a: warning)
    reviews = []
    def review(context, *args):
        reviews.append(context)
        result = {'approved': True}
        if confirmed is not None:
            result['overlap_adjudication'] = {'confirmed_duplicate': confirmed,
                'history_reference': 'pair:actual-bug', 'reason': '独立核实相同触发链与故障'}
        return result
    monkeypatch.setattr(host, 'run_codex_iteration_validation', review)
    expected = '缺少完整题面独立查重结论' if confirmed is None else '完整题面确认与'
    with pytest.raises(host.WorkflowError, match=expected):
        host._generate_iteration_candidate_for_context(
            {'repo_path': '.', '_pairwise_mode': True}, 'Bug 修复')
    assert reviews and reviews[0]['_overlap_warning'] == warning


@pytest.mark.parametrize('kind,pairwise,requires_new_flow', [
    ('Bug 修复', True, False), ('Feature 迭代', True, True), ('Bug 修复', False, True),
])
def test_third_followup_new_product_gate_does_not_apply_to_pairwise_bug(
        host, monkeypatch, kind, pairwise, requires_new_flow):
    context = {'repo_path': '.', '_pairwise_mode': pairwise,
               'same_repository_history': [{'reference_id': 'pair:root', 'prompt': '已有流程'}],
               'iteration_policy': {'iteration_count': 2}}
    concept = {'concept_summary': '修复现有流程', 'business_object': '既有会话',
               'primary_operation': '替换输入', 'state_lifecycle': '运行到结束',
               'failure_boundary': '旧响应覆盖新状态', 'acceptance_outcomes': ['旧响应不再覆盖'],
               'modules': ['会话状态']}
    monkeypatch.setattr(host, 'run_codex_iteration_concept', lambda *a: concept)
    monkeypatch.setattr(host, 'run_codex_same_repository_overlap_validation',
                        lambda *a: {'overlap_level': 'none'})
    gate_calls = []
    def gate(*args):
        gate_calls.append(args)
        return {'approved': False, 'reason': '没有全新业务对象'}
    monkeypatch.setattr(host, 'run_codex_third_iteration_gate', gate)
    def generate(*args):
        raise PromptCaptured()
    monkeypatch.setattr(host, 'run_codex_iteration_generation', generate)
    error = host.SameRepositoryOverlapError if requires_new_flow else PromptCaptured
    with pytest.raises(error):
        host._generate_iteration_candidate_for_context(context, kind)
    assert bool(gate_calls) == requires_new_flow


def test_content_failures_yield_to_roots_without_bypassing_ready_drafts(host, monkeypatch):
    refill.initialize(host)
    counts = {'0-1 代码生成': 4, 'Feature 迭代': 3, 'Bug 修复': 0}
    monkeypatch.setattr(refill, 'continuation_source', lambda h, kind, *a, **kw: {'kind': kind})
    for kind in ('Bug 修复', 'Feature 迭代'):
        for reason in ('难度不足', '预置前后未证明同一用户场景由通过变为失败'):
            refill._record_generation_outcome(host, kind, host.WorkflowError(reason))
    kind, source, reasons = refill.next_available_task(host, counts, {})
    assert kind == '0-1 代码生成' and source is None
    assert len(reasons) == 2 and all('连续出题未产出' in reason for reason in reasons)
    refill.initialize(host)  # Reinitialization/restart does not clear the backoff.
    assert refill._type_backoff(host, 'Bug 修复')
    monkeypatch.setattr(refill.candidate_bank, 'ready_task_counts', lambda h: {'Bug 修复': 1})
    assert refill.next_available_task(host, counts, {}, ready_mode='only')[0] == 'Bug 修复'
    refill._record_generation_outcome(host, 'Bug 修复')
    assert not refill._type_backoff(host, 'Bug 修复')
    assert refill.next_available_task(host, counts, {})[0] == 'Bug 修复'


def test_health_updates_are_atomic_and_capacity_is_not_a_content_failure(host):
    refill.initialize(host)
    for error in (host.JobCancelled('停止'), host.UpstreamCapacityError('容量不足')):
        refill._record_generation_outcome(host, 'Bug 修复', error)
    assert refill._type_backoff(host, 'Bug 修复') is None
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: refill._record_generation_outcome(
            host, 'Bug 修复', host.WorkflowError('不合格')), range(2)))
    until = refill._type_backoff(host, 'Bug 修复')
    assert until
    refill._record_generation_outcome(host, 'Bug 修复', host.WorkflowError('迟到失败'))
    assert refill._type_backoff(host, 'Bug 修复') >= until
    with host.db_connection() as db:
        db.execute('UPDATE pairwise_generation_health SET retry_at=0')
    assert refill._type_backoff(host, 'Bug 修复') is None


def feature_candidate(prompt):
    return dict(task_type='Feature 迭代', prompt=prompt, complexity_analysis=sample_complexity(),
                estimated_minutes=35,
                modules=['domain', 'http'], expansion_axis='并发回执裁决',
                engineering_core='跨请求恢复', main_user_flow='恢复并验证回执',
                new_runtime_components=[], complex_mechanisms=['恢复'], api_or_actions=['恢复'],
                new_state_sets=['投递'], acceptance_scenarios=['中断恢复'])


def test_minor_style_does_not_discard_a_feature_but_difficulty_still_blocks(host):
    prompt = '恢复过程必须保持投递版本一致。' * 34
    assert 480 < len(prompt) <= 600
    assert host.validate_generated_iteration(feature_candidate(prompt), 'Feature 迭代',
                                             flexible_scope=True) == prompt
    with pytest.raises(host.WorkflowError, match='题面应为'):
        host.validate_generated_iteration(feature_candidate(prompt), 'Feature 迭代')
    review = {'difficulty_review': sample_difficulty(), 'scope_review': {
        'engineering_core_count': 1, 'estimated_minutes': 35,
        'modules': ['domain', 'http'], 'complex_mechanisms': ['恢复'],
        'api_or_actions': ['恢复'], 'new_state_sets': ['投递'], 'new_runtime_components': [],
        'acceptance_scenarios': ['中断恢复'], 'history_overlap': False,
        'overlapping_sequences': [], 'ai_style_issues': ['结尾像验收清单'],
    }}
    assert not host.iteration_review_scope_errors(review, 'Feature 迭代', flexible_scope=True)
    review['difficulty_review'] = sample_difficulty('中等')
    assert host.iteration_review_scope_errors(review, 'Feature 迭代', flexible_scope=True)
    review['difficulty_review'] = sample_difficulty()
    review['scope_review']['history_overlap'] = True
    assert host.iteration_review_scope_errors(review, 'Feature 迭代', flexible_scope=True)


@pytest.mark.parametrize('command,reason', [
    ('true', '预置后仍通过'),
    ("python3 -c 'import absent_regression_dependency'", '环境故障'),
    ('python3 -c \'raise RuntimeError("probe-loader failed")\'', '原来源已经失败'),
])
def test_failed_seed_keeps_both_real_outputs(host, monkeypatch, tmp_path, command, reason):
    work = source_work(tmp_path)
    monkeypatch.setattr(host, 'run_codex_structured', lambda *a, **kw:
                        {**fake_seed_model(), 'regression_command': command})
    with pytest.raises(host.WorkflowError, match=reason) as caught:
        seeded_bug.seed(host, {'pair': {'prompt': '规则', 'verification_commands': ['true']}},
                        work, lambda *_: None)
    records = list((Path(host.DATA_DIR) / 'seed-verification').glob('*.json'))
    assert len(records) == 1 and str(records[0]) in str(caught.value)
    checks = json.loads(records[0].read_text())['checks']
    assert set(checks) == {'original', 'seeded'}
    for check in checks.values():
        assert isinstance(check['regression']['exit_code'], int)
    if reason == '环境故障':
        assert 'absent_regression_dependency' in checks['original']['regression']['stderr']
        assert refill._source_failure_policy(host, caught.value) == (False, False)
    if reason == '原来源已经失败':
        assert 'probe-loader failed' in str(caught.value)


def test_same_setup_is_run_in_each_clean_regression_snapshot(host, monkeypatch, tmp_path):
    work = source_work(tmp_path)
    setup = "python3 -c 'from pathlib import Path; Path(\"ready.marker\").write_text(\"ready\")'"
    command = ("python3 -c 'from pathlib import Path; from app import decide; "
               "assert Path(\"ready.marker\").read_text() == \"ready\"; assert not decide(0)'")
    monkeypatch.setattr(host, 'run_codex_structured', lambda *a, **kw:
                        {**fake_seed_model(), 'regression_command': command,
                         'regression_setup_command': setup})
    result = seeded_bug.seed(host, {'pair': {'prompt': '规则', 'verification_commands': ['true']}},
                             work, lambda *_: None)
    checks = json.loads(Path(result['verification_evidence_path']).read_text())['checks']
    assert checks['original']['setup']['exit_code'] == checks['seeded']['setup']['exit_code'] == 0
    assert checks['original']['regression']['exit_code'] == 0
    assert checks['seeded']['regression']['exit_code'] != 0
    assert not (work / 'ready.marker').exists()
