import json
import subprocess
from pathlib import Path

import pytest

from pairwise import auto_refill, bug_runtime, generation_policy
from test_pairwise import host


def runtime(monkeypatch, h, seconds=1500):
    state = bug_runtime.Runtime(bug_runtime.time.monotonic() + seconds,
                               {'pair': {'id': 'source'}, 'attempt': {'artifact_sha': 'a' * 40}})
    monkeypatch.setattr(h.CODEX_JOB_CONTEXT, 'bug_generation_runtime', state, raising=False)
    return state


@pytest.mark.parametrize('prefix,expected', [('seeded-bug-design', 300),
    ('seeded-bug-patch', 180), ('bugfix-generation', 180),
    ('bugfix-validation', 180), ('bugfix-difficulty-review', 300)])
def test_stage_caps_replace_oversized_individual_timeouts(host, monkeypatch, prefix, expected):
    state = runtime(monkeypatch, host)
    assert state.limit(host, prefix, 900) == expected


def test_budget_includes_queue_time_and_reserves_final_blind_review(host, monkeypatch):
    state = runtime(monkeypatch, host, seconds=450)
    assert 148 <= state.limit(host, 'bugfix-validation', 720) <= 150
    state.deadline = bug_runtime.time.monotonic() + 320
    with pytest.raises(host.GenerationTimeout, match='剩余预算不足'):
        state.limit(host, 'bugfix-validation', 720)


def test_adaptive_extension_never_spends_downstream_reserve(host):
    state = bug_runtime.Runtime(
        bug_runtime.time.monotonic() + 700,
        {'pair': {'id': 'source'}, 'attempt': {'artifact_sha': 'a' * 40}},
        active_budget_seconds=700,
    )
    # Patch must leave ten minutes for the remaining render/review stages, so
    # the 180-second base call cannot receive an extension in this budget.
    assert bug_runtime.adaptive_hard_timeout(
        state, 'seeded-bug-patch', 180) is None


def test_v2_active_budget_excludes_recorded_queue_time(host):
    now = bug_runtime.time.monotonic()
    state = bug_runtime.Runtime(
        now + 500, {'pair': {'id': 'source'}, 'attempt': {'artifact_sha': 'a' * 40}},
        active_budget_seconds=200, active_soft_seconds=100)
    state.started = now - 100
    state.queue_seconds = 80
    assert 178 <= state.limit(host, 'custom-v2-stage', 500) <= 181
    budget = state.budget_state()
    assert budget['queue_seconds'] == 80
    assert 19 <= budget['active_seconds'] <= 21


def test_design_timeout_retries_once_with_partial_context_and_releases_slot(host, monkeypatch):
    state = runtime(monkeypatch, host)
    monkeypatch.setattr(bug_runtime, 'RETRY_SECONDS', 0)
    calls = []
    def invoke(prompt, schema, cwd, prefix, timeout, **kwargs):
        calls.append((prompt, timeout))
        if len(calls) == 1:
            error = host.GenerationTimeout('timeout')
            error.diagnostic_tail = '已读 src/core.ts 的 merge 分支'
            raise error
        return {'candidate': 'complete'}
    monkeypatch.setattr(host, '_run_codex_structured', invoke)
    progress = []
    monkeypatch.setattr(host.CODEX_JOB_CONTEXT, 'progress',
                        lambda detail, rejection='': progress.append((detail, rejection)), raising=False)
    # The shared fixture blocks live models; bind only the real orchestration wrapper.
    monkeypatch.setattr(host, 'run_codex_structured', _real_wrapper)
    result = host.run_codex_structured('设计一个方向', {}, Path('.'), 'seeded-bug-design', 300)
    assert result == {'candidate': 'complete'} and state.retries == 1
    assert len(calls) == 2 and 'src/core.ts' in calls[1][0]
    assert host.CODEX_SLOT_ACTIVE == 0
    assert any('技术超时，0 秒后定向重试（1/1）' in item[0] for item in progress)
    assert progress[-1][0] == 'seeded-bug-design 正在定向重试（1/1）'


def test_second_timeout_exits_without_repeating_entire_source(host, monkeypatch):
    state = runtime(monkeypatch, host)
    monkeypatch.setattr(bug_runtime, 'RETRY_SECONDS', 0)
    calls = []
    def invoke(*args, **kwargs):
        calls.append(1)
        raise host.GenerationTimeout('timeout')
    monkeypatch.setattr(host, '_run_codex_structured', invoke)
    monkeypatch.setattr(host, 'run_codex_structured', _real_wrapper)
    with pytest.raises(host.GenerationTimeout):
        host.run_codex_structured('设计', {}, Path('.'), 'seeded-bug-design', 300)
    assert len(calls) == 2 and state.retries == 1


@pytest.mark.parametrize('prefix', ['iteration-concept', 'bugfix-generation',
    'bugfix-validation', 'bugfix-difficulty-review'])
def test_later_stage_retries_same_input_without_author_retry_logs(host, monkeypatch, prefix):
    runtime(monkeypatch, host)
    monkeypatch.setattr(bug_runtime, 'RETRY_SECONDS', 0)
    calls = []
    def invoke(*args, **kwargs):
        calls.append(args)
        error = host.GenerationTimeout('timeout')
        error.diagnostic_tail = 'HIDDEN author answer or previous verdict'
        raise error
    monkeypatch.setattr(host, '_run_codex_structured', invoke)
    monkeypatch.setattr(host, 'run_codex_structured', _real_wrapper)
    with pytest.raises(host.GenerationTimeout):
        host.run_codex_structured('只有题面', {}, Path('.'), prefix, 360)
    assert len(calls) == 2 and calls[0][0] == calls[1][0]
    assert calls[0][0] == bug_runtime.EXECUTION_GUIDANCE + '只有题面'
    assert 'HIDDEN' not in calls[1][0] and calls[0][2] == calls[1][2]


@pytest.mark.parametrize('automatic_bug', [True, False])
def test_subagent_setting_is_scoped_to_automatic_bug_calls(host, monkeypatch, automatic_bug):
    if automatic_bug:
        runtime(monkeypatch, host)
    seen = {}
    class Process:
        pid = 12345
        returncode = 0
        def __init__(self, args, **kwargs):
            seen['args'] = args
            self.output = Path(args[args.index('--output-last-message') + 1])
        def communicate(self, input, timeout):
            self.output.write_text('{"ok":true}')
            return '', ''
    monkeypatch.setattr(host.subprocess, 'Popen', Process)
    assert host._run_codex_structured('当前一步', {}, Path('.'), 'iteration-concept', 90) == {'ok': True}
    assert ('agents.enabled=false' in seen['args']) is automatic_bug
    assert ('multi_agent' in seen['args']) is automatic_bug


def test_timeout_diagnostics_survive_process_and_temporary_directory_cleanup(host, monkeypatch):
    runtime(monkeypatch, host)
    seen = {}
    class Process:
        pid = 12345
        returncode = None
        def __init__(self, args, **kwargs):
            seen['output'] = Path(args[args.index('--output-last-message') + 1])
            kwargs['stderr'].write('正在核对 src/state.ts:apply\n')
            kwargs['stderr'].flush()
        def communicate(self, input, timeout):
            raise subprocess.TimeoutExpired('codex', timeout)
    monkeypatch.setattr(host.subprocess, 'Popen', Process)
    monkeypatch.setattr(host, 'terminate_process', lambda p: seen.update(terminated=True))
    with pytest.raises(host.GenerationTimeout) as caught:
        host._run_codex_structured('内部设计资料', {}, Path('.'), 'seeded-bug-design', 2)
    root = next((host.DATA_DIR / 'bug-generation-calls').iterdir())
    assert not seen['output'].parent.exists()
    metadata = json.loads((root / 'status.json').read_text())
    assert seen['terminated'] is True
    assert metadata['status'] == 'timeout' and metadata['stage'] == 'seeded-bug-design'
    assert 'src/state.ts' in metadata['last_output']
    assert (root / 'prompt.txt').read_text() == '内部设计资料'
    assert 'src/state.ts' in caught.value.diagnostic_tail
    assert (root / 'schema.json').exists()


def test_design_timeout_extends_in_one_minute_steps_while_output_is_fresh(host, monkeypatch):
    runtime(monkeypatch, host)
    progress = []
    monkeypatch.setattr(host.CODEX_JOB_CONTEXT, 'progress',
                        lambda detail, rejection='': progress.append(detail), raising=False)
    seen = {'timeouts': [], 'inputs': []}

    class Process:
        pid = 12345
        returncode = None

        def __init__(self, args, **kwargs):
            self.stderr = kwargs['stderr']
            self.output = Path(args[args.index('--output-last-message') + 1])

        def communicate(self, input=None, timeout=None):
            seen['timeouts'].append(timeout)
            seen['inputs'].append(input)
            call = len(seen['timeouts'])
            self.stderr.write(f'analysis\n正在核对新的代码分支 {call}\n')
            self.stderr.flush()
            if call < 4:
                raise subprocess.TimeoutExpired('codex', timeout)
            self.output.write_text('{"ok":true}')
            self.returncode = 0
            return None, None

    monkeypatch.setattr(host.subprocess, 'Popen', Process)
    result = host._run_codex_structured(
        '设计', {}, Path('.'), 'seeded-bug-design', 300)
    assert result == {'ok': True}
    assert seen['timeouts'] == [300, 60, 60, 60]
    assert seen['inputs'][0] == '设计' and seen['inputs'][1:] == [None, None, None]
    assert progress == [
        'seeded-bug-design 最近 60 秒仍有新的代码分析输出，续时至 6/8 分钟',
        'seeded-bug-design 最近 60 秒仍有新的代码分析输出，续时至 7/8 分钟',
        'seeded-bug-design 最近 60 秒仍有新的代码分析输出，续时至 8/8 分钟',
    ]
    status = json.loads(next((host.DATA_DIR / 'bug-generation-calls').rglob('status.json')).read_text())
    assert [event['decision'] for event in status['adaptive_timeout']['events']] == [
        'extend', 'extend', 'extend']


@pytest.mark.parametrize('mode,expected_reason,expected_calls', [
    ('inactive', '达到 8 分钟硬上限', 4),
    ('repeated', '近期只在重复执行已见过的源码遍历命令', 2),
    ('hard', '达到 8 分钟硬上限', 4),
])
def test_design_adaptive_timeout_allows_silence_but_stops_at_repeat_or_hard_cap(
        host, monkeypatch, mode, expected_reason, expected_calls):
    runtime(monkeypatch, host)
    seen = []

    class Process:
        pid = 12345
        returncode = None

        def __init__(self, args, **kwargs):
            self.stderr = kwargs['stderr']

        def communicate(self, input=None, timeout=None):
            seen.append(timeout)
            if mode != 'inactive':
                command = ('/bin/zsh -lc "rg -n core src" in /tmp/pairwise-followup-test/source'
                           if mode == 'repeated' else
                           f'/bin/zsh -lc "sed -n {len(seen)}p src/core.ts" in /tmp/pairwise-followup-test/source')
                self.stderr.write(f'exec\n{command}\n')
                self.stderr.flush()
            raise subprocess.TimeoutExpired('codex', timeout)

    monkeypatch.setattr(host.subprocess, 'Popen', Process)
    monkeypatch.setattr(host, 'terminate_process', lambda process: None)
    with pytest.raises(host.GenerationTimeout, match=expected_reason):
        host._run_codex_structured('设计', {}, Path('.'), 'seeded-bug-design', 300)
    assert len(seen) == expected_calls
    status = json.loads(next((host.DATA_DIR / 'bug-generation-calls').rglob('status.json')).read_text())
    assert status['adaptive_timeout']['events'][-1]['decision'] == 'stop'
    assert status['adaptive_timeout']['events'][-1]['reason'] == expected_reason


def test_design_silent_analysis_extends_to_hard_cap(host, monkeypatch):
    runtime(monkeypatch, host)
    progress = []
    monkeypatch.setattr(host.CODEX_JOB_CONTEXT, 'progress',
                        lambda detail, rejection='': progress.append(detail), raising=False)

    class Process:
        pid = 12345
        returncode = None

        def __init__(self, args, **kwargs):
            pass

        def communicate(self, input=None, timeout=None):
            raise subprocess.TimeoutExpired('codex', timeout)

    monkeypatch.setattr(host.subprocess, 'Popen', Process)
    monkeypatch.setattr(host, 'terminate_process', lambda process: None)
    with pytest.raises(host.GenerationTimeout, match='达到 8 分钟硬上限'):
        host._run_codex_structured('设计', {}, Path('.'), 'seeded-bug-design', 300)
    assert progress == [
        'seeded-bug-design 最近 60 秒无新输出，允许静默分析到硬上限，续时至 6/8 分钟',
        'seeded-bug-design 最近 60 秒无新输出，允许静默分析到硬上限，续时至 7/8 分钟',
        'seeded-bug-design 最近 60 秒无新输出，允许静默分析到硬上限，续时至 8/8 分钟',
    ]


def test_patch_stage_also_extends_while_analysis_is_fresh(host, monkeypatch):
    runtime(monkeypatch, host)
    seen = []

    class Process:
        pid = 12345
        returncode = None

        def __init__(self, args, **kwargs):
            self.stderr = kwargs['stderr']

        def communicate(self, input=None, timeout=None):
            seen.append(timeout)
            self.stderr.write('analysis\n仍有新的盲审输出\n')
            self.stderr.flush()
            raise subprocess.TimeoutExpired('codex', timeout)

    monkeypatch.setattr(host.subprocess, 'Popen', Process)
    monkeypatch.setattr(host, 'terminate_process', lambda process: None)
    with pytest.raises(host.GenerationTimeout, match='达到 6 分钟硬上限'):
        host._run_codex_structured(
            '生成补丁', {}, Path('.'), 'seeded-bug-patch', 180)
    assert seen == [180, 60, 60, 60]
    status = json.loads(next((host.DATA_DIR / 'bug-generation-calls').rglob('status.json')).read_text())
    assert status['adaptive_timeout']['hard_seconds'] == 360


@pytest.mark.parametrize('body,expected_status', [('{"ok": true}', 'complete'), ('[]', 'failed')])
def test_completed_output_is_preserved_and_invalid_schema_is_not_marked_complete(host, monkeypatch, body, expected_status):
    runtime(monkeypatch, host)
    class Process:
        pid = 12345
        returncode = 0
        def __init__(self, args, **kwargs):
            self.output = Path(args[args.index('--output-last-message') + 1])
        def communicate(self, input, timeout):
            self.output.write_text(body)
            return None, None
    monkeypatch.setattr(host.subprocess, 'Popen', Process)
    if expected_status == 'complete':
        assert host._run_codex_structured('设计', {}, Path('.'), 'seeded-bug-design', 2) == {'ok': True}
    else:
        with pytest.raises(host.WorkflowError, match='格式不正确'):
            host._run_codex_structured('设计', {}, Path('.'), 'seeded-bug-design', 2)
    root = next((host.DATA_DIR / 'bug-generation-calls').iterdir())
    assert (root / 'result.json').read_text() == body
    assert json.loads((root / 'status.json').read_text())['status'] == expected_status


def test_completed_stage_checkpoint_binds_source_candidate_policy_and_hashes(host, monkeypatch, tmp_path):
    state = runtime(monkeypatch, host)
    state.source_tree = 'b' * 40
    state.set_candidate('B')
    monkeypatch.setattr(host, '_run_codex_structured',
                        lambda *a, **kw: {'candidate': 'complete'})
    monkeypatch.setattr(host, 'run_codex_structured', _real_wrapper)
    result = host.run_codex_structured('题面', {'type': 'object'}, tmp_path,
                                       'bugfix-generation', 180)
    assert result == {'candidate': 'complete'}
    checkpoint = next((host.DATA_DIR / 'bug-generation-checkpoints').rglob('latest.json'))
    saved = json.loads(checkpoint.read_text())
    assert saved['candidate_id'] == 'B'
    assert saved['source_sha'] == 'a' * 40 and saved['source_tree'] == 'b' * 40
    assert saved['policy_version'] == generation_policy.VERSION
    assert saved['payload_sha256'] and saved['prompt_sha256'] and saved['schema_sha256']


def test_technical_timeouts_use_short_backoff_without_quality_or_source_penalty(host):
    error = host.GenerationTimeout('seeded-bug-design 超时')
    for _ in range(4):
        auto_refill._record_generation_outcome(host, 'Bug 修复', error)
    remaining = auto_refill._type_backoff(host, 'Bug 修复') - bug_runtime.time.time()
    assert 0 < remaining <= 46
    assert auto_refill._source_failure_policy(host, error) == (False, False)
    with host.db_connection() as db:
        row = db.execute("SELECT * FROM pairwise_generation_health WHERE task_type='Bug 修复'").fetchone()
    assert row['failures'] == 0 and row['error_kind'] == 'timeout'


def test_feature_technical_timeout_uses_short_backoff(host):
    error = host.GenerationTimeout('iteration-concept 超时，已停止')
    for _ in range(4):
        auto_refill._record_generation_outcome(host, 'Feature 迭代', error)
    remaining = auto_refill._type_backoff(host, 'Feature 迭代') - bug_runtime.time.time()
    assert 0 < remaining <= bug_runtime.RETRY_SECONDS + 1
    with host.db_connection() as db:
        row = db.execute(
            "SELECT * FROM pairwise_generation_health WHERE task_type='Feature 迭代'"
        ).fetchone()
    assert row['failures'] == 0 and row['error_kind'] == 'timeout'


def test_legacy_feature_timeout_does_not_keep_ten_minute_backoff(host):
    updated_at = host.now_text()
    with host.db_connection() as db:
        auto_refill._initialize_health(db)
        db.execute(
            '''INSERT INTO pairwise_generation_health
               (task_type,failures,retry_at,reason,updated_at,error_kind)
               VALUES (?,?,?,?,?,?)''',
            ('Feature 迭代', 0, bug_runtime.time.time() + 600,
             'iteration-concept 超时，已停止', updated_at, 'quality'),
        )
    remaining = auto_refill._type_backoff(host, 'Feature 迭代') - bug_runtime.time.time()
    assert 0 < remaining <= bug_runtime.RETRY_SECONDS + 1


def test_quality_rejection_has_no_backoff_but_later_timeout_does(host):
    for _ in range(2):
        auto_refill._record_generation_outcome(host, 'Bug 修复', host.WorkflowError('难度不足'))
    assert auto_refill._type_backoff(host, 'Bug 修复') is None
    auto_refill._record_generation_outcome(host, 'Bug 修复', host.GenerationTimeout('超时'))
    assert auto_refill._type_backoff(host, 'Bug 修复') > bug_runtime.time.time()


@pytest.mark.parametrize('reason,technical', [
    ('2 路题面竞速均未通过：路线 1：seeded-bug-design 超时，已停止；路线 2：seeded-bug-design 超时，已停止', True),
    ('2 路题面竞速均未通过：路线 1：本来源未找到困难缺陷；路线 2：seeded-bug-design 超时，已停止', False),
])
def test_legacy_timeout_recovery_never_loosens_mixed_quality_failures(host, reason, technical):
    with host.db_connection() as db:
        auto_refill._initialize_health(db)
        db.execute('INSERT INTO pairwise_generation_health(task_type,failures,retry_at,reason,updated_at) VALUES (?,?,?,?,?)',
                   ('Bug 修复', 1, bug_runtime.time.time() + 600, reason, '2020-01-01 00:00:00 +0800'))
    assert (auto_refill._type_backoff(host, 'Bug 修复') is None) == technical


def test_bug_source_has_one_lane_and_preserves_typed_timeout(host, monkeypatch):
    calls = []
    monkeypatch.setattr(auto_refill.sources, 'rejected_directions', lambda *a: [])
    def generate(h, source, kind, progress, feedback):
        calls.append(h.CODEX_JOB_CONTEXT.bug_generation_runtime)
        raise h.GenerationTimeout('test timeout')
    monkeypatch.setattr(auto_refill, '_generate_continuation', generate)
    with pytest.raises(host.WorkflowError) as caught:
        auto_refill._generate_continuation_race(host, {'pair': {'id': 'source'}}, 'Bug 修复', lambda *a: None)
    assert len(calls) == 1 and bug_runtime.is_timeout(caught.value)
    assert not hasattr(host.CODEX_JOB_CONTEXT, 'bug_generation_runtime')


# Capture before the shared host fixture replaces it with a live-model guard.
import app
_real_wrapper = app.run_codex_structured
