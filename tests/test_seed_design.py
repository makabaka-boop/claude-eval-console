import json
from pathlib import Path

import pytest

from pairwise import seed_design, seeded_bug
from test_pairwise import host, git
from test_seeded_bug import source_work
from test_static_bug import design_candidate, design_review, static_model


def v2_card(*, routine='不足够', visibility='需跨阶段重建', direct=False,
            bases=None, alternatives=None, counterexample=''):
    return {**design_candidate(),
            'routine_path': '从用户现象追踪状态提交、完成最小修复并验证交错',
            'routine_assessment': routine,
            'root_cause_visibility': visibility,
            'plausible_alternative_causes': alternatives if alternatives is not None else ['事件乱序', '旧状态回写'],
            'difficulty_bases': bases if bases is not None else ['跨阶段状态重建', '多个合理根因排除'],
            'existing_direct_verification': direct,
            'boundary_counterexample': counterexample}


def test_unsupported_fault_is_rejected_before_patch_and_source_is_unchanged(host, monkeypatch, tmp_path):
    work = source_work(tmp_path)
    before = git(work, 'rev-parse', 'HEAD')
    calls = []
    def model(prompt, schema, cwd, stage, *args, **kwargs):
        calls.append(stage)
        if stage == 'seeded-bug-design':
            return {'candidates': [design_candidate()], 'unavailable_reason': ''}
        assert stage == 'seeded-bug-design-review'
        review = design_review()
        # Even an inconsistent approved=True cannot bypass fault authenticity.
        review['fault_supported'] = False
        return {'reviews': [review]}
    monkeypatch.setattr(host, 'run_codex_structured', model)
    with pytest.raises(host.WorkflowError, match='设计独立预审未通过'):
        seeded_bug.seed(host, {'pair': {'prompt': '原需求'}, 'bug_verification_mode': 'static'},
                        work, lambda *a: None)
    assert calls == ['seeded-bug-design', 'seeded-bug-design-review']
    assert git(work, 'rev-parse', 'HEAD') == before
    assert git(work, 'status', '--porcelain', '--untracked-files=all') == ''
    audit = json.loads(next((host.DATA_DIR / 'seed-design').glob('*.json')).read_text())
    assert audit['review']['reviews'][0]['fault_supported'] is False


def test_design_stage_reviews_two_cards_in_one_call_and_keeps_backup(host, monkeypatch, tmp_path):
    work = source_work(tmp_path)
    second = {**design_candidate(), 'proposed_change': '第二个变化'}
    def model(prompt, schema, cwd, stage, *args, **kwargs):
        if stage == 'seeded-bug-design':
            assert schema['properties']['candidates']['maxItems'] == 2
            return {'candidates': [design_candidate(), second],
                    'unavailable_reason': ''}
        return {'reviews': [design_review(0), design_review(1)]}
    monkeypatch.setattr(host, 'run_codex_structured', model)
    result = seed_design.select(host, {'pair': {'prompt': '原需求'}}, work, '{}', '', lambda *a: None)
    assert result['candidate_id'] == 'A'
    assert [item['candidate_id'] for item in result['approved_designs']] == ['A', 'B']
    assert result['alternates'][0]['candidate'] == second


def test_v2_keeps_routine_bundle_but_reranks_harder_backup_without_extra_review(host, monkeypatch, tmp_path):
    work = source_work(tmp_path)
    routine = v2_card(routine='足够', visibility='直接暴露', direct=True,
                      bases=['局部修法边界'], alternatives=[])
    hard = v2_card()
    calls = []
    def model(prompt, schema, cwd, stage, *args, **kwargs):
        calls.append(stage)
        assert schema['properties']['candidates']['items']['properties']['routine_assessment']
        return {'candidates': [routine, hard], 'unavailable_reason': ''}
    monkeypatch.setattr(host, 'run_codex_structured', model)
    result = seed_design.select(host, {
        'pair': {'id': 'source', 'prompt': '原需求'},
        'bug_generation_flow': 'bug_generation_v2',
    }, work, '{}', '', lambda *a: None)
    assert calls == ['seeded-bug-design']
    assert result['candidate_id'] == 'A'
    assert result['source_candidate_index'] == 1
    assert result['candidate'] == hard
    audit = json.loads(Path(result['audit_path']).read_text())
    assert audit['structural_risk_rejections'] == []


def test_v2_keeps_routine_multi_bug_bundle_for_blind_review(host, monkeypatch, tmp_path):
    work = source_work(tmp_path)
    monkeypatch.setattr(host, 'run_codex_structured', lambda *a, **kw: {
        'candidates': [v2_card(routine='足够', visibility='直接暴露', direct=True,
                               bases=['局部修法边界'], alternatives=[])],
        'unavailable_reason': '',
    })
    result = seed_design.select(host, {
        'pair': {'id': 'source', 'prompt': '原需求'},
        'bug_generation_flow': 'bug_generation_v2',
    }, work, '{}', '', lambda *a: None)
    assert result['candidate']['routine_assessment'] == '足够'


def test_v2_rejects_disputed_single_fault_in_multi_problem_mode(
        host, monkeypatch, tmp_path):
    work = source_work(tmp_path)
    weak_single = v2_card(
        routine='有争议', visibility='需跨阶段重建', direct=False,
        bases=['跨阶段状态重建', '多个合理根因排除', '非局部验证义务'])
    weak_single['problems'] = weak_single['problems'][:1]
    assert len(weak_single['problems']) == 1
    monkeypatch.setattr(host, 'run_codex_structured', lambda *a, **kw: {
        'candidates': [weak_single], 'unavailable_reason': '',
    })
    with pytest.raises(host.WorkflowError, match='2 至 7'):
        seed_design.select(host, {
            'pair': {'id': 'source', 'prompt': '原需求'},
            'bug_generation_flow': 'bug_generation_v2',
        }, work, '{}', '', lambda *a: None)


def test_v2_rejects_strong_single_fault_in_multi_problem_mode(
        host, monkeypatch, tmp_path):
    work = source_work(tmp_path)
    strong_single = v2_card(
        routine='不足够', visibility='需跨阶段重建', direct=False,
        bases=['跨阶段状态重建', '多个合理根因排除', '非局部验证义务'])
    strong_single['problems'] = strong_single['problems'][:1]
    monkeypatch.setattr(host, 'run_codex_structured', lambda *a, **kw: {
        'candidates': [strong_single], 'unavailable_reason': '',
    })
    with pytest.raises(host.WorkflowError, match='2 至 7'):
        seed_design.select(host, {
            'pair': {'id': 'source', 'prompt': '原需求'},
            'bug_generation_flow': 'bug_generation_v2',
        }, work, '{}', '', lambda *a: None)


def test_authentic_design_review_does_not_grade_a_local_patch(host, monkeypatch, tmp_path):
    work = source_work(tmp_path)
    def model(prompt, schema, cwd, stage, *args, **kwargs):
        if stage == 'seeded-bug-design':
            return {'candidates': [design_candidate()], 'unavailable_reason': ''}
        return {'reviews': [design_review()]}
    monkeypatch.setattr(host, 'run_codex_structured', model)
    result = seed_design.select(host, {'pair': {'prompt': '原需求'}}, work, '{}', '', lambda *a: None)
    assert result['review']['fault_supported'] is True
    assert json.loads(Path(result['audit_path']).read_text())['selected_index'] == 0


def test_design_preflight_rejects_authenticity_not_difficulty(host, monkeypatch, tmp_path):
    work = source_work(tmp_path)
    def model(prompt, schema, cwd, stage, *args, **kwargs):
        if stage == 'seeded-bug-design':
            return {'candidates': [design_candidate()], 'unavailable_reason': ''}
        return {'reviews': [design_review(approved=False)]}
    monkeypatch.setattr(host, 'run_codex_structured', model)
    with pytest.raises(host.WorkflowError, match='设计独立预审未通过'):
        seed_design.select(host, {'pair': {'prompt': '原需求'}}, work, '{}', '', lambda *a: None)


@pytest.mark.parametrize('reviews', [[], [design_review(), design_review()], [design_review(1)]])
def test_missing_or_duplicate_review_does_not_admit_design(host, monkeypatch, tmp_path, reviews):
    work = source_work(tmp_path)
    def model(prompt, schema, cwd, stage, *args, **kwargs):
        if stage == 'seeded-bug-design':
            return {'candidates': [design_candidate()], 'unavailable_reason': ''}
        return {'reviews': reviews}
    monkeypatch.setattr(host, 'run_codex_structured', model)
    with pytest.raises(host.WorkflowError, match='缺项或重复'):
        seed_design.select(host, {'pair': {'prompt': '原需求'}}, work, '{}', '', lambda *a: None)


def test_no_qualifying_design_stops_without_review_or_patch(host, monkeypatch, tmp_path):
    work = source_work(tmp_path)
    def model(prompt, schema, cwd, stage, *args, **kwargs):
        assert stage == 'seeded-bug-design'
        return {'candidates': [], 'unavailable_reason': '现有源码只有常规局部变化'}
    monkeypatch.setattr(host, 'run_codex_structured', model)
    with pytest.raises(host.WorkflowError, match='未找到可合规预置'):
        seeded_bug.seed(host, {'pair': {'prompt': '原需求'}, 'bug_verification_mode': 'static'},
                        work, lambda *a: None)


def test_v2_empty_first_pass_gets_related_multi_problem_retry(
        host, monkeypatch, tmp_path):
    work = source_work(tmp_path)
    calls = []
    hard = v2_card(routine='不足够')

    def model(prompt, schema, cwd, stage, *args, **kwargs):
        calls.append(prompt)
        if len(calls) == 1:
            assert '当前源码已经会产生候选所述的同一可观察错误' in prompt
            assert 'original_invariants' in prompt
            return {'candidates': [], 'unavailable_reason': '只检查了局部条件'}
        assert '横向复查当前产品的现有用户入口' in prompt
        assert '不同根因和局部修法都允许' in prompt
        return {'candidates': [hard], 'unavailable_reason': ''}

    monkeypatch.setattr(host, 'run_codex_structured', model)
    result = seed_design.select(host, {
        'pair': {'id': 'source', 'prompt': '原需求'},
        'bug_generation_flow': 'bug_generation_v2',
    }, work, '{}', '', lambda *a: None)
    assert len(calls) == 2
    assert result['candidate'] == hard
    audit = json.loads(Path(result['audit_path']).read_text())
    assert audit['initial_proposal']['unavailable_reason'] == '只检查了局部条件'


def test_design_reviewer_cannot_modify_source(host, monkeypatch, tmp_path):
    work = source_work(tmp_path)
    def model(prompt, schema, cwd, stage, *args, **kwargs):
        if stage == 'seeded-bug-design-review':
            (cwd / 'app.py').write_text('changed\n')
        return static_model(prompt, schema, cwd, stage, *args, **kwargs)
    monkeypatch.setattr(host, 'run_codex_structured', model)
    with pytest.raises(host.WorkflowError, match='修改了源码'):
        seed_design.select(host, {'pair': {'prompt': '原需求'}}, work, '{}', '', lambda *a: None)


def test_patch_cannot_switch_away_from_admitted_files(host, monkeypatch, tmp_path):
    work = source_work(tmp_path)
    (work / 'other.py').write_text('value = 1\n')
    git(work, 'add', '.')
    git(work, 'commit', '-m', 'other source')
    def model(prompt, schema, cwd, stage, *args, **kwargs):
        result = static_model(prompt, schema, cwd, stage, *args, **kwargs)
        if stage == 'seeded-bug-design':
            result['candidates'][0]['code_locations'] = ['other.py:value']
            for problem in result['candidates'][0]['problems']:
                problem['code_locations'] = ['other.py:value']
        elif stage == 'seeded-bug-design-review':
            result['reviews'][0]['code_evidence'] = ['other.py:value']
        return result
    monkeypatch.setattr(host, 'run_codex_structured', model)
    with pytest.raises(host.WorkflowError, match='偏离已通过独立预审'):
        seeded_bug.seed(host, {'pair': {'prompt': '原需求'}, 'bug_verification_mode': 'static'},
                        work, lambda *a: None)
