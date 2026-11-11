from pairwise import bug_generation_v2 as v2
from test_pairwise import git, host, sample_difficulty
from test_seeded_bug import source_work


def test_wording_only_review_keeps_the_frozen_fault():
    review = {
        'approved': False,
        'reasons': ['结尾过于模板化'],
        'task_type': 'Bug 修复',
        'bug_review': {
            'verified_bug_count': 2,
            'unverified_bugs': [],
            'overlapping_sequences': [],
            'solution_leaks': [],
            'style_issues': ['使用了通用收尾'],
            'scope_too_large': False,
            'single_focus': True,
        },
    }
    state, reason = v2._review_state(review)
    assert state == 'rewrite'
    assert '通用收尾' in reason


def test_one_real_fault_is_valid_but_an_unrelated_bundle_is_not():
    review = {
        'approved': True,
        'reasons': [],
        'task_type': 'Bug 修复',
        'bug_review': {
            'verified_bug_count': 2,
            'unverified_bugs': [],
            'overlapping_sequences': [],
            'solution_leaks': [],
            'style_issues': [],
            'scope_too_large': False,
            'single_focus': True,
        },
    }
    assert v2._review_state(review) == ('approved', '')
    review['bug_review']['single_focus'] = False
    assert v2._review_state(review)[0] == 'scope'


def test_v2_final_prompt_generator_has_no_question_policy(host, monkeypatch):
    captured = {}

    class Stop(Exception):
        pass

    def capture(prompt, schema, *args, **kwargs):
        captured['prompt'] = prompt
        captured['schema'] = schema
        captured['kwargs'] = kwargs
        raise Stop()

    monkeypatch.setattr(host, 'run_codex_structured', capture)
    context = {
        'repo_path': '.', 'repo_name': 'demo',
        'original_or_current_prompt': '已有产品需求',
        'bug_prompt_references': [{'prompt': '历史写法不应进入题面生成'}],
    }
    seed_info = {
        'description': {'affected_flow': '提交回执', 'problems': []},
        'code_evidence': ['app.py:submit'], 'changed_files': ['app.py'],
    }
    with pytest.raises(Stop):
        v2._render(host, context, seed_info)

    assert generation_policy.BUG_STATIC_GUIDANCE not in captured['prompt']
    assert generation_policy.BUG_NOVELTY_GUIDANCE not in captured['prompt']
    assert '历史写法不应进入题面生成' not in captured['prompt']
    assert '只总结存在的问题，不要说解决方法' in captured['prompt']
    assert 'minLength' not in captured['schema']['properties']['prompt']
    assert 'minItems' not in captured['schema']['properties']['confirmed_bugs']
    assert captured['kwargs']['model'] == 'gpt-5.6-sol'
    assert captured['kwargs']['reasoning_effort'] == 'high'


def test_second_blind_review_is_limited_to_structural_contradiction():
    medium = sample_difficulty('中等')
    medium['routine_solution_sufficient'] = False
    assert v2._contradictory_medium({'difficulty_review': medium})
    medium['routine_solution_sufficient'] = True
    assert not v2._contradictory_medium({'difficulty_review': medium})


def potential_design(**overrides):
    candidate = {
        'routine_assessment': '不足够',
        'root_cause_visibility': '需跨阶段重建',
        'plausible_alternative_causes': ['版本错配', '迟到回执', '恢复重放'],
        'difficulty_bases': ['跨阶段状态重建', '多个合理根因排除', '非局部验证义务'],
        'boundary_counterexample': '',
    }
    candidate.update(overrides)
    return {'candidate': candidate}


def test_hard_blind_review_is_admitted(host):
    review = sample_difficulty('困难')
    blind = {'difficulty_review': review, 'diagnosis_path': ['重建提交、回执与恢复顺序'],
             'code_evidence': ['app.py:decide']}
    admission, errors = v2._difficulty_admission(host, blind, potential_design())
    assert not errors
    assert admission['status'] == 'confirmed_hard'
    assert admission['preflight_level'] == '困难'
    assert admission['target_level'] == '困难'


def test_hell_blind_review_and_queued_draft_are_rejected(host):
    blind = {'difficulty_review': sample_difficulty('地狱'),
             'diagnosis_path': ['重建提交、回执与恢复顺序'],
             'code_evidence': ['app.py:decide']}
    admission, errors = v2._difficulty_admission(host, blind, potential_design())
    assert admission is None
    assert any('超过目标困难难度' in error for error in errors)
    with pytest.raises(ValueError, match='只接受恰好困难'):
        auto_refill._validate_continuation_draft(
            host, {'task_difficulty': '地狱'}, 'Bug 修复')


def test_medium_with_auditable_hard_potential_is_rejected_before_ab(host):
    review = sample_difficulty('中等')
    blind = {'difficulty_review': review, 'diagnosis_path': ['重建提交、回执与恢复顺序'],
             'code_evidence': ['app.py:decide']}
    admission, errors = v2._difficulty_admission(host, blind, potential_design())
    assert admission is None
    assert any('前置盲审为中等' in error and '禁止启动 Claude A/B' in error for error in errors)


def test_medium_is_rejected_regardless_of_author_routine_assessment(host):
    review = sample_difficulty('中等')
    blind = {'difficulty_review': review, 'diagnosis_path': ['重建跨阶段状态'],
             'code_evidence': ['app.py:decide']}
    admission, errors = v2._difficulty_admission(
        host, blind, potential_design(routine_assessment='足够'))
    assert admission is None
    assert errors


def test_medium_is_rejected_regardless_of_structural_signals(host):
    review = sample_difficulty('中等')
    review['design_tradeoffs'] = []
    review['verification_obligations'] = []
    design = potential_design(
        root_cause_visibility='需跨函数追踪',
        plausible_alternative_causes=[],
        difficulty_bases=['跨阶段状态重建', '非局部验证义务'],
    )
    blind = {'difficulty_review': review, 'diagnosis_path': ['跨阶段追踪'],
             'code_evidence': ['app.py:decide']}
    admission, errors = v2._difficulty_admission(host, blind, design)
    assert admission is None
    assert errors


def test_simple_or_routine_medium_never_enters_the_potential_lane(host):
    for level, routine, card_routine in (
        ('简单', False, '不足够'),
        ('中等', True, '足够'),
    ):
        review = sample_difficulty(level)
        review['routine_solution_sufficient'] = routine
        blind = {'difficulty_review': review, 'diagnosis_path': ['局部定位'],
                 'code_evidence': ['app.py:decide']}
        admission, errors = v2._difficulty_admission(
            host, blind, potential_design(routine_assessment=card_routine))
        assert admission is None
        assert errors


def test_routine_medium_multi_bug_bundle_is_rejected(host):
    review = sample_difficulty('中等')
    review['routine_solution_sufficient'] = True
    design = potential_design(routine_assessment='足够', problems=[
        {'trigger': '选择第二项'}, {'trigger': '调整透明度'},
    ])
    blind = {'difficulty_review': review, 'diagnosis_path': ['分别追踪两个用户故障'],
             'code_evidence': ['ui.py:select', 'ui.py:opacity']}
    admission, errors = v2._difficulty_admission(host, blind, design)
    assert not errors
    assert admission['status'] == v2.PROVISIONAL_STATUS
    assert admission['admission_mode'] == 'potential_fallback'


def test_first_two_attempts_reject_small_medium_patch(host):
    review = sample_difficulty('中等')
    blind = {'difficulty_review': review, 'diagnosis_path': ['追踪相关问题'],
             'code_evidence': ['ui.py:select', 'ui.py:opacity']}
    design = potential_design(problems=[
        {'trigger': '选择第二项'}, {'trigger': '调整透明度'},
    ])

    admission, errors = v2._difficulty_admission(
        host, blind, design,
        seed_info={'patch_scope': {'changed_lines': 12, 'file_count': 2}},
        attempt_number=1,
    )

    assert admission is None
    assert any('前置盲审为中等' in error for error in errors)


def test_first_two_attempts_reject_substantial_medium_patch(host):
    review = sample_difficulty('中等')
    blind = {'difficulty_review': review, 'diagnosis_path': ['追踪相关问题'],
             'code_evidence': ['api.py:save', 'ui.py:refresh']}
    design = potential_design(problems=[
        {'trigger': '保存记录'}, {'trigger': '刷新统计'},
    ])

    admission, errors = v2._difficulty_admission(
        host, blind, design,
        seed_info={'patch_scope': {'changed_lines': 30, 'file_count': 2}},
        attempt_number=2,
    )

    assert admission is None
    assert errors


def test_third_attempt_still_rejects_small_medium_patch(host):
    review = sample_difficulty('中等')
    blind = {'difficulty_review': review, 'diagnosis_path': ['追踪相关问题'],
             'code_evidence': ['ui.py:select', 'ui.py:opacity']}
    design = potential_design(problems=[
        {'trigger': '选择第二项'}, {'trigger': '调整透明度'},
    ])

    admission, errors = v2._difficulty_admission(
        host, blind, design,
        seed_info={'patch_scope': {'changed_lines': 6, 'file_count': 2}},
        attempt_number=3,
    )

    assert admission is None
    assert errors


def test_quality_failure_uses_backup_card_from_clean_source(host, monkeypatch, tmp_path):
    work = source_work(tmp_path)
    source_sha = git(work, 'rev-parse', 'HEAD')
    source_tree = git(work, 'rev-parse', 'HEAD^{tree}')
    designs = [
        {'candidate_id': key, 'candidate': {
            'proposed_change': f'变化 {key}', 'original_invariants': f'不变量 {key}',
            'code_locations': ['app.py:decide'],
        }}
        for key in ('A', 'B')
    ]
    planted = []
    monkeypatch.setattr(v2.seeded_bug, 'design_candidates', lambda *a: designs)

    def seed(h, source, repo, progress, design=None):
        assert git(repo, 'rev-parse', 'HEAD') == source_sha
        planted.append(design['candidate_id'])
        return {
            'sha': source_sha, 'tree': source_tree, 'changed_files': ['app.py'],
            'code_evidence': ['app.py:decide'],
            'description': {'affected_flow': design['candidate_id'], 'problems': [{}, {}]},
        }

    monkeypatch.setattr(v2.seeded_bug, 'seed', seed)
    monkeypatch.setattr(v2.seeded_bug, 'bundle', lambda *a: {
        'bundle_path': str(tmp_path / 'seed.bundle'), 'sha': source_sha, 'tree': source_tree,
    })
    monkeypatch.setattr(v2, '_render', lambda *a, **kw: {
        'task_type': 'Bug 修复', 'prompt': '用户提交后看到旧状态覆盖新状态，后续读取继续返回错误结果。',
        'confirmed_bugs': [{}, {}], 'modules': ['状态'], 'focus_area': '状态',
        'main_user_flow': '提交并读取', 'generation_flow': v2.FLOW,
    })

    def authenticity(h, context, seed_info, candidate):
        approved = seed_info['description']['affected_flow'] == 'B'
        return {'approved': approved, 'reasons': [] if approved else ['故障链不成立'],
                'task_type': 'Bug 修复', 'generation_flow': v2.FLOW, 'bug_review': {
                    'verified_bug_count': 2 if approved else 0,
                    'unverified_bugs': [] if approved else ['A 无法由源码确认'],
                    'overlapping_sequences': [], 'solution_leaks': [], 'style_issues': [],
                    'scope_too_large': False, 'single_focus': True}}

    monkeypatch.setattr(v2, '_authenticity', authenticity)
    monkeypatch.setattr(v2, '_difficulty', lambda *a: {
        'difficulty_review': sample_difficulty('困难'), 'diagnosis_path': ['追踪状态'],
        'code_evidence': ['app.py:decide'], 'snapshot': {},
    })
    rejected = []
    monkeypatch.setattr(v2.sources, 'reject_direction', lambda *a: rejected.append(a[-1]))
    result = v2.generate(
        host, {'pair': {'id': 'source'}, 'attempt': {'artifact_sha': source_sha,
        'artifact_tree': source_tree}}, work,
        {'repo_path': str(work)}, {'pair': {'prompt': '原题'}},
        source_sha, source_tree, lambda *a, **kw: None)
    assert planted == ['A', 'B']
    assert rejected and result['generation_flow'] == v2.FLOW
    assert result['task_difficulty'] == '困难'
