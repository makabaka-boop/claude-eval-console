import pytest

from pairwise import auto_refill, candidate_bank, models, notebook, offline_prefill, workflow
from test_pairwise import host, payload, complete, sample_difficulty


def prepare_source(host):
    parent = models.create(host, payload(task_type='0-1 代码生成', snapshot_url=''))
    complete(host, parent['id'], 'A')
    auto_refill.initialize(host)
    return parent


def test_offline_prefill_only_stages_reviewed_candidate_and_releases_source(host, monkeypatch):
    parent = prepare_source(host)
    def no_execution(*args, **kwargs):
        raise AssertionError('Offline generation must never start Claude')
    monkeypatch.setattr(workflow, 'start', no_execution)
    monkeypatch.setattr(models, 'create', no_execution)
    def generate(h, source, kind, progress, feedback, lanes):
        assert lanes == 1 and source['pair']['id'] == parent['id']
        return {'task_type': kind, 'prompt': '一个经独立审核的不同新增行为。',
                'task_difficulty': '困难', 'difficulty_review': sample_difficulty()}
    monkeypatch.setattr(auto_refill, '_generate_continuation_race', generate)
    result = offline_prefill.stage_once(host, 'Feature 迭代', lambda *_: None)
    assert result['status'] == 'ready'
    with host.db_connection() as db:
        assert db.execute('SELECT COUNT(*) FROM pairwise_tasks').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM pairwise_attempts').fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM pairwise_source_locks WHERE state='generating'").fetchone()[0] == 0
    assert candidate_bank.ready_count(host) == 1


def test_offline_prefill_keeps_the_difficulty_gate(host, monkeypatch):
    prepare_source(host)
    monkeypatch.setattr(auto_refill, '_generate_continuation_race', lambda *a, **k: {
        'task_type': 'Feature 迭代', 'prompt': '不能放行中等题',
        'task_difficulty': '中等', 'difficulty_review': sample_difficulty('中等')})
    with pytest.raises(ValueError, match='困难度复核'):
        offline_prefill.stage_once(host, 'Feature 迭代', lambda *_: None)
    assert candidate_bank.ready_count(host) == 0
    with host.db_connection() as db:
        assert db.execute("SELECT COUNT(*) FROM pairwise_source_locks WHERE state='generating'").fetchone()[0] == 0


def test_offline_prefill_refuses_to_compete_with_enabled_coordinator(host):
    prepare_source(host)
    host.write_settings({'pairwise_auto_enabled': '1'})
    with pytest.raises(host.WorkflowError, match='先暂停服务内自动出题'):
        offline_prefill.stage_once(host, 'Bug 修复', lambda *_: None)


@pytest.mark.parametrize('reason', [
    '概念难度预检：缓存键碰撞只需常规局部修复，不足困难',
    '本来源未找到可合规预置的困难缺陷：缓存键碰撞仅属常规局部修复',
])
def test_offline_failure_is_available_as_next_source_feedback(host, monkeypatch, reason):
    parent = prepare_source(host)
    def reject(*args, **kwargs):
        raise host.WorkflowError(reason)
    monkeypatch.setattr(auto_refill, '_generate_continuation_race', reject)
    with pytest.raises(host.WorkflowError, match='缓存键碰撞'):
        offline_prefill.stage_once(host, 'Bug 修复', lambda *_: None)
    with host.db_connection() as db:
        row = db.execute('SELECT * FROM pairwise_local_notebook').fetchone()
        assert row['source_pair_id'] == parent['id'] and row['task_type'] == 'Bug 修复'
        assert row['stage'] == '生成汇总' and '缓存键碰撞' in row['reason']
        assert db.execute('SELECT COUNT(*) FROM pairwise_source_directions').fetchone()[0] == 0
    context = {'task_type': 'Bug 修复', 'source_repo': row['source_repo'],
               'project_category': row['project_category']}
    with notebook.generation(host, context, 'next-offline-retry') as lessons:
        assert '缓存键碰撞' in lessons['feedback']
        assert '不是全局禁题规则' in lessons['feedback']
