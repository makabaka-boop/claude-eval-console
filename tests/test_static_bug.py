import json
from pathlib import Path

import pytest

from pairwise import auto_refill as refill, bug_generation_v2, generation_policy, natural_bug, seeded_bug, seed_design, sources, models
from test_pairwise import host, git, sample_difficulty, sample_bug_audit, payload, complete, clone_archived_fixture
from test_pairwise_sources import single
from test_refill_policy import captured_prompt
from test_seeded_bug import source_work, fake_seed_model


@pytest.mark.parametrize('stage', ['discovery', 'concept', 'generation', 'validation'])
def test_pairwise_bug_prompts_require_static_evidence_without_runtime_gate(host, monkeypatch, stage):
    historical = '历史资料原文：先实际复现，再修复。'
    context = {'repo_path': '.', '_pairwise_mode': True,
               'same_repository_history': [{'prompt': historical}],
               '_seeded_defect': {'affected_flow': '已有流程'}}
    call = {
        'discovery': lambda: natural_bug.discover(host, {'pair': {'prompt': historical}}, Path('.'), '[]'),
        'concept': lambda: host.run_codex_iteration_concept(context, 'Bug 修复'),
        'generation': lambda: host.run_codex_bugfix_generation(context),
        'validation': lambda: host.run_codex_bugfix_validation(context, {'prompt': '修复已有流程'}),
    }[stage]
    prompt = captured_prompt(monkeypatch, host, call)
    if stage == 'generation':
        assert generation_policy.BUG_STATIC_GUIDANCE not in prompt
        assert generation_policy.BUG_NOVELTY_GUIDANCE not in prompt
        # The renderer consumes the frozen defect only. Historical text remains
        # in the dedicated design/overlap/review calls.
        assert historical not in prompt
        assert '基于本项目出一个 Bug 修复需求' in prompt
    else:
        assert generation_policy.BUG_STATIC_GUIDANCE in prompt
        assert historical in prompt  # Never rewrite history into different evidence.
    for obsolete in ('至少一个问题必须稳定复现', '正式候选仍须实际复现',
                     '并运行可重复的检查', '只返回实际稳定复现的问题'):
        assert obsolete not in prompt


def test_legacy_bug_generation_retains_its_runtime_policy(host, monkeypatch):
    prompt = captured_prompt(monkeypatch, host,
        lambda: host.run_codex_bugfix_generation({'repo_path': '.'}))
    assert generation_policy.BUG_STATIC_GUIDANCE not in prompt
    assert '并运行可重复的检查' in prompt


def design_candidate():
    return {'affected_flow': '阈值裁决', 'original_invariants': '零值拒绝',
            'proposed_change': '零值被错误接受', 'trigger': '输入零值',
            'observable_failure': '错误接受', 'expected_behavior': '应拒绝',
            'repair_constraints': '保留正值行为', 'diagnosis_challenges': '作者修法独有标记',
            'regression_scenarios': '作者反例独有标记', 'code_locations': ['app.py:decide'],
            'problems': [
                {'trigger': '输入零值', 'observable_failure': '零值被错误接受',
                 'expected_behavior': '零值应拒绝', 'repair_constraints': '保留正值行为',
                 'code_locations': ['app.py:decide']},
                {'trigger': '零值后提交负值', 'observable_failure': '错误状态延续到后续裁决',
                 'expected_behavior': '后续裁决保持独立', 'repair_constraints': '不污染后续输入',
                 'code_locations': ['app.py:decide']},
            ]}


def design_review(index=0, *, approved=True):
    return {'candidate_index': index, 'approved': True, 'fault_supported': True,
            'single_chain': True, 'reason': '模拟独立审核结论', 'code_evidence': ['app.py:decide'],
            'scope_preserved': True, 'not_duplicate': True} | {'approved': approved}


def static_model(prompt, schema, *args, **kwargs):
    assert generation_policy.BUG_STATIC_GUIDANCE in prompt
    assert generation_policy.BUG_NOVELTY_GUIDANCE in prompt
    if args[1] == 'seeded-bug-design':
        return {'candidates': [design_candidate()], 'unavailable_reason': ''}
    if args[1] == 'seeded-bug-design-review':
        assert '作者修法独有标记' not in prompt and '作者反例独有标记' not in prompt
        return {'reviews': [design_review()]}
    assert '不先搜索自然缺陷' in prompt
    assert 'regression_command' not in schema['properties']
    result = fake_seed_model(*args, **kwargs)
    result.pop('regression_command')
    return {**result, 'code_evidence': ['app.py:decide']}


def test_static_seed_freezes_and_bundles_without_running_any_project_command(host, monkeypatch, tmp_path):
    work = source_work(tmp_path)
    real_run = host.run_command
    commands = []
    def git_only(command, *args, **kwargs):
        commands.append(command)
        assert command[0] == 'git', 'Static preparation must not execute the product or its tests'
        return real_run(command, *args, **kwargs)
    monkeypatch.setattr(host, 'run_command', git_only)
    monkeypatch.setattr(host, 'run_codex_structured', static_model)
    source = {'pair': {'prompt': '已有约束', 'verification_commands': ['false']},
              'bug_verification_mode': 'static'}
    seed = seeded_bug.seed(host, source, work, lambda *a: None)
    snapshot = seeded_bug.bundle(host, work, seed)
    assert seed['verification_mode'] == snapshot['verification_mode'] == 'static'
    assert seed['runtime_verified'] is snapshot['runtime_verified'] is False
    assert Path(snapshot['bundle_path']).is_file()
    assert snapshot['code_evidence'] == ['app.py:decide']
    assert snapshot['design_preflight']['review']['approved'] is True
    assert Path(snapshot['design_preflight']['audit_path']).is_file()
    assert 'regression_command' not in seed['description']
    assert not (host.DATA_DIR / 'seed-verification').exists()
    assert commands


def test_static_first_optionally_runs_safe_existing_check_in_disposable_copy(host, monkeypatch, tmp_path):
    work = source_work(tmp_path)
    monkeypatch.setattr(host, 'run_codex_structured', static_model)
    seed = seeded_bug.seed(host, {
        'pair': {'prompt': '已有约束', 'verification_commands': ['true', 'docker compose up']},
        'bug_verification_mode': 'static_first',
    }, work, lambda *a: None)
    check = seed['runtime_check']
    assert seed['verification_mode'] == 'static_first' and seed['runtime_verified'] is False
    assert check['performed'] is True and check['successful'] is True
    assert [item['command'] for item in check['checks']] == ['true']
    assert git(work, 'status', '--porcelain', '--untracked-files=all') == ''


@pytest.mark.parametrize('locations', [[], ['missing.py:run'], ['../outside.py:run']])
def test_static_seed_still_requires_real_code_locations(host, monkeypatch, tmp_path, locations):
    work = source_work(tmp_path)
    def model(*args, **kwargs):
        return {**static_model(*args, **kwargs), 'code_evidence': locations}
    monkeypatch.setattr(host, 'run_codex_structured', model)
    with pytest.raises(host.WorkflowError, match='代码位置'):
        seeded_bug.seed(host, {'pair': {'prompt': '规则'}, 'bug_verification_mode': 'static'},
                        work, lambda *a: None)


def test_static_seed_does_not_bypass_independent_review(host, tmp_path):
    bundle = tmp_path / 'seed.bundle'
    bundle.write_bytes(b'fixture')
    draft = {'task_difficulty': '困难', 'difficulty_review': sample_difficulty(),
             'task_type': 'Bug 修复', 'prompt': '已有故障的修复任务',
             'seed_snapshot': {'bundle_path': str(bundle), 'sha': 'a' * 40, 'tree': 'b' * 40,
                               'verification_mode': 'static', 'runtime_verified': False,
                               'code_evidence': ['app.py:run'], 'static_analysis': {'counterexample': '场景'}}}
    with pytest.raises(ValueError, match='独立审核'):
        refill._validate_continuation_draft(host, draft, 'Bug 修复')
    draft['independent_review'] = {'approved': True, 'difficulty_review': sample_difficulty(),
        'bug_review': {'verified_bug_count': 2, 'unverified_bugs': [], 'overlapping_sequences': [],
                       'solution_leaks': [], 'style_issues': [], 'scope_too_large': False, 'single_focus': True}}
    with pytest.raises(ValueError, match='开发者视角难度盲审'):
        refill._validate_continuation_draft(host, draft, 'Bug 修复')
    draft['independent_review']['developer_difficulty_audit'] = sample_bug_audit(
        draft['prompt'], draft['seed_snapshot'], draft['difficulty_review'])
    assert refill._validate_continuation_draft(host, draft, 'Bug 修复')[0] == '困难'
    draft['prompt'] += '增加额外需求'
    with pytest.raises(ValueError, match='开发者视角难度盲审'):
        refill._validate_continuation_draft(host, draft, 'Bug 修复')


def test_v2_medium_potential_cannot_start_ab_or_bypass_with_cached_admission(host, tmp_path):
    bundle = tmp_path / 'potential.bundle'
    bundle.write_bytes(b'fixture')
    prompt = '用户更新输入并发生恢复重放后，迟到结果会覆盖新版本，后续查询继续返回与当前输入不一致的状态。'
    seed = {'bundle_path': str(bundle), 'sha': 'a' * 40, 'tree': 'b' * 40,
            'verification_mode': 'static_first', 'runtime_verified': False,
            'code_evidence': ['app.py:run'], 'static_analysis': {'counterexample': '提交后中断并恢复'}}
    medium = sample_difficulty('中等')
    blind = {'difficulty_review': medium, 'diagnosis_path': ['重建提交、恢复和回执顺序'],
             'code_evidence': ['app.py:run']}
    design = {'candidate': {'routine_assessment': '不足够',
              'root_cause_visibility': '需跨阶段重建',
              'plausible_alternative_causes': ['版本错配', '恢复重放', '迟到回执'],
              'difficulty_bases': ['跨阶段状态重建', '多个合理根因排除', '非局部验证义务'],
              'boundary_counterexample': '',
              'problems': [{'trigger': '恢复重放'}, {'trigger': '迟到回执'}]}}
    admission, errors = bug_generation_v2._difficulty_admission(host, blind, design)
    assert admission is None
    assert any('禁止启动 Claude A/B' in error for error in errors)
    legacy_admission = {
        'status': 'provisional', 'admission_mode': 'potential_fallback',
        'preflight_level': '中等', 'target_level': '困难',
        'final_gate': 'post_execution_difficulty_review',
    }
    independent = {'approved': True, 'reasons': [], 'task_type': 'Bug 修复',
                   'generation_flow': bug_generation_v2.FLOW,
                   'difficulty_review': medium, 'difficulty_admission': legacy_admission,
                   'bug_review': {'verified_bug_count': 2, 'unverified_bugs': [],
                       'overlapping_sequences': [], 'solution_leaks': [], 'style_issues': [],
                       'scope_too_large': False, 'single_focus': True}}
    independent['developer_difficulty_audit'] = sample_bug_audit(prompt, seed, medium)
    draft = {'task_difficulty': '困难', 'difficulty_review': medium,
             'difficulty_admission': legacy_admission, 'task_type': 'Bug 修复', 'prompt': prompt,
             'generation_flow': bug_generation_v2.FLOW, 'seed_snapshot': seed,
             'independent_review': independent, 'design_preflight': design}
    with pytest.raises(ValueError, match='低于困难'):
        refill._validate_continuation_draft(host, draft, 'Bug 修复')


def test_automatic_bug_generation_never_calls_runtime_reference_repair(host, monkeypatch):
    _, attempt = single(host)
    source = refill.continuation_source(host, 'Bug 修复')
    old_concept = {'direction_identity': {'mechanism': '快照错配', 'invariant': '请求与输入一致',
                                         'code_locations': ['product.txt:apply']}}
    identity = sources.direction_identity(old_concept)
    old_reason = '旧规则：属于原功能，所以已排除'
    with host.db_connection() as db:
        db.execute('INSERT INTO pairwise_source_directions VALUES (?,?,?,?,?,?)',
                   (source['pair']['id'], 'Bug 修复', sources.direction_fingerprint(identity),
                    json.dumps(identity, ensure_ascii=False), old_reason, host.now_text()))
    monkeypatch.setattr(host, 'clone_repository_snapshot',
                        lambda url, path, sha: clone_archived_fixture(host, url, path, sha))
    monkeypatch.setattr(host.CODEX_JOB_CONTEXT, 'existing_bug', {
        'source_sha': attempt['artifact_sha'], 'source_tree': attempt['artifact_tree'],
    }, raising=False)
    monkeypatch.setattr(natural_bug, 'discover_verified', lambda *a, **kw: None)
    design = {'candidate_id': 'A', 'candidate': {'proposed_change': '快照错配',
        'original_invariants': '请求与输入一致', 'code_locations': ['product.txt:apply']}}
    monkeypatch.setattr(seeded_bug, 'design_candidates', lambda *a: [design])
    def seed(h, selected, path, progress, design=None):
        assert selected['bug_verification_mode'] == 'static_first'
        assert selected['rejected_directions'] == []
        assert selected['historical_direction_warnings'] == [{'identity': identity, 'reason': old_reason}]
        return {'sha': attempt['artifact_sha'], 'tree': attempt['artifact_tree'],
                'verification_mode': 'static_first', 'runtime_verified': False}
    def generate(context, kind, feedback):
        assert context['_bug_verification_mode'] == 'static_first'
        # The seed stage already receives old direction records structurally.
        # Later stages get only a bounded mode/current-stage note.
        assert feedback == '核验模式：static_first。'
        assert old_reason not in feedback
        host.CODEX_JOB_CONTEXT.direction_guard(old_concept)
        return {'prompt': '源码分析后的候选'}
    monkeypatch.setattr(seeded_bug, 'seed', seed)
    monkeypatch.setattr(host, '_generate_iteration_candidate_for_context', generate)
    monkeypatch.setattr(seeded_bug, 'bundle', lambda *a: {'verification_mode': 'static_first'})
    monkeypatch.setattr(seeded_bug, 'reference_fix',
                        lambda *a: pytest.fail('Static generation must not run a reference repair'))
    result = refill._generate_continuation(host, source, 'Bug 修复', lambda *a, **kw: None)
    assert result['seed_snapshot']['verification_mode'] == 'static_first'
    assert result['bug_generation_strategy'] == 'existing_first_fallback_seed'
    assert result['bug_verification_mode'] == 'static_first' and result['runtime_verified'] is False


def test_blind_difficulty_rejection_is_remembered_for_the_frozen_bug_direction(host, monkeypatch):
    _, attempt = single(host)
    source = refill.continuation_source(host, 'Bug 修复')
    monkeypatch.setattr(host, 'clone_repository_snapshot',
                        lambda url, path, sha: clone_archived_fixture(host, url, path, sha))
    monkeypatch.setattr(natural_bug, 'discover_verified', lambda *a, **kw: None)
    design = {'candidate_id': 'A', 'candidate': {'proposed_change': '缓存错配',
        'original_invariants': '读取结果对应当前版本', 'code_locations': ['product.txt:apply']}}
    monkeypatch.setattr(seeded_bug, 'design_candidates', lambda *a: [design])
    monkeypatch.setattr(seeded_bug, 'seed', lambda *a, **kw: {
        'sha': attempt['artifact_sha'], 'tree': attempt['artifact_tree'],
        'verification_mode': 'static', 'runtime_verified': False,
    })
    concept = {'direction_identity': {'mechanism': '缓存版本错配', 'invariant': '读取结果对应当前版本',
                                      'code_locations': ['product.txt:apply']}}
    def reject_after_blind_review(context, kind, feedback):
        host.CODEX_JOB_CONTEXT.direction_guard(concept)
        review = {'difficulty_review': sample_difficulty('简单')}
        host.CODEX_JOB_CONTEXT.notebook_observer(
            '迭代预审', {'prompt': '缓存失效现象'}, '常规局部修复足够', review, 'rejected')
        raise host.WorkflowError('难度问题：当前方向的最小正确方案未达到困难')
    monkeypatch.setattr(host, '_generate_iteration_candidate_for_context', reject_after_blind_review)
    with pytest.raises(host.WorkflowError, match='难度问题'):
        refill._generate_continuation(host, source, 'Bug 修复', lambda *a, **kw: None)
    assert sources.rejected_directions(host, source, 'Bug 修复') == [{
        'identity': sources.direction_identity(concept), 'reason': '常规局部修复足够',
    }]


def test_bug_candidate_b_restarts_from_clean_source_after_a_quality_failure(host, monkeypatch):
    _, attempt = single(host)
    source = refill.continuation_source(host, 'Bug 修复')
    monkeypatch.setattr(host, 'clone_repository_snapshot',
                        lambda url, path, sha: clone_archived_fixture(host, url, path, sha))
    monkeypatch.setattr(natural_bug, 'discover_verified', lambda *a, **kw: None)
    designs = [
        {'candidate_id': 'A', 'candidate': {'proposed_change': '错误 A',
            'original_invariants': '约束 A', 'code_locations': ['product.txt:apply']}},
        {'candidate_id': 'B', 'candidate': {'proposed_change': '错误 B',
            'original_invariants': '约束 B', 'code_locations': ['product.txt:apply']}},
    ]
    monkeypatch.setattr(seeded_bug, 'design_candidates', lambda *a: designs)
    planted = []
    def seed(h, selected, work, progress, design=None):
        assert git(work, 'rev-parse', 'HEAD') == attempt['artifact_sha']
        planted.append(design['candidate_id'])
        return {'sha': attempt['artifact_sha'], 'tree': attempt['artifact_tree'],
                'verification_mode': 'static_first', 'runtime_verified': False,
                'changed_files': ['product.txt'], 'description': {}}
    monkeypatch.setattr(seeded_bug, 'seed', seed)
    monkeypatch.setattr(seeded_bug, 'bundle', lambda *a: {'verification_mode': 'static_first'})
    calls = []
    def generate(context, kind, feedback):
        candidate_id = planted[-1]
        calls.append(candidate_id)
        concept = {'direction_identity': {'mechanism': '机制 ' + candidate_id,
            'invariant': '约束 ' + candidate_id, 'code_locations': ['product.txt:apply']}}
        host.CODEX_JOB_CONTEXT.direction_guard(concept)
        if candidate_id == 'A':
            raise host.WorkflowError('难度问题：候选 A 仅为常规局部修复')
        return {'prompt': '候选 B 的困难修复题面'}
    monkeypatch.setattr(host, '_generate_iteration_candidate_for_context', generate)
    result = refill._generate_continuation(host, source, 'Bug 修复', lambda *a, **kw: None)
    assert planted == calls == ['A', 'B']
    assert result['bug_verification_mode'] == 'static_first'


def test_verified_existing_bug_is_used_before_seed_fallback(host, monkeypatch):
    _, attempt = single(host)
    source = refill.continuation_source(host, 'Bug 修复')
    monkeypatch.setattr(host, 'clone_repository_snapshot',
                        lambda url, path, sha: clone_archived_fixture(host, url, path, sha))
    discovery = {
        'discovery_id': 'natural-1', 'source_sha': attempt['artifact_sha'],
        'source_tree': attempt['artifact_tree'], 'affected_flow': '输入版本绑定',
        'problems': [
            {'trigger': '先更新输入再返回旧结果', 'observable_failure': '旧结果覆盖新状态',
             'expected_behavior': '拒绝过期结果', 'evidence': '版本比较缺失',
             'code_locations': ['product.txt:apply']},
            {'trigger': '失败后使用旧请求重试', 'observable_failure': '新输入被旧请求替换',
             'expected_behavior': '保留当前输入', 'evidence': '重试沿用旧快照',
             'code_locations': ['product.txt:apply']},
        ],
        'minimal_fix': '统一请求和结果的版本裁决', 'routine_assessment': '不足够',
        'counterexample': '返回顺序与提交顺序相反', 'code_locations': ['product.txt:apply'],
    }
    reverification = {'actual_evidence': '两条故障链均由源码确认',
                      'source_sha': attempt['artifact_sha']}
    monkeypatch.setattr(natural_bug, 'discover_verified', lambda *a, **kw: {
        'lane': 2, 'discovery': discovery, 'reverification': reverification,
    })
    monkeypatch.setattr(seeded_bug, 'design_candidates',
                        lambda *a: pytest.fail('verified existing bugs must skip seed design'))

    def generate(context, kind, feedback):
        assert kind == 'Bug 修复'
        assert context['_existing_defect'] == discovery
        assert '_seeded_defect' not in context
        assert context['current_commit'] == attempt['artifact_sha']
        return {'task_type': kind, 'prompt': '现有代码中的两个版本错配问题。'}

    monkeypatch.setattr(host, '_generate_iteration_candidate_for_context', generate)
    result = refill._generate_continuation(
        host, source, 'Bug 修复', lambda *a, **kw: None)
    assert result['bug_generation_strategy'] == 'existing_first'
    assert result['bug_verification_mode'] == 'static'
    assert result['existing_bug_discovery'] == discovery
    assert result['existing_bug_reverification'] == reverification


def test_bug_source_rotation_remembers_source_attempt_after_cooldown_reset(host):
    old, _ = single(host)
    chosen = refill.continuation_source(host, 'Bug 修复')
    claimed = sources.claim(host, chosen, 'Bug 修复')
    sources.release(host, claimed, '无合格方向', failed=True, cooldown_after_attempt=True)
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_source_locks SET cooldown_until=0 WHERE source_pair_id=?", (old['id'],))
        db.execute("UPDATE pairwise_tasks SET created_at='2026-09-18 00:00:00 +0800' WHERE id=?", (old['id'],))
    assert sources.get_lock(host, old['id'], 'Bug 修复')['failure_count'] == 0
    fresh = models.create(host, payload(repo_name='untried-bug-root', task_type='0-1 代码生成', snapshot_url=''))
    complete(host, fresh['id'], 'A')
    assert refill.continuation_source(host, 'Bug 修复')['pair']['id'] == fresh['id']
    assert refill.continuation_source(host, 'Feature 迭代')['pair']['id'] == old['id']
