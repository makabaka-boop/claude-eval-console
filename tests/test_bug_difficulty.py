from test_static_bug import static_model


def authenticity():
    return {'approved': True, 'reasons': [], 'task_type': 'Bug 修复', 'bug_review': {
        'verified_bug_count': 2, 'unverified_bugs': [], 'overlapping_sequences': [],
        'solution_leaks': [], 'style_issues': [], 'scope_too_large': False, 'single_focus': True}}


def blind_verdict(routine=False, level=None):
    review = sample_difficulty(level or ('简单' if routine else '困难'))
    review.pop('code_evidence')
    review['routine_solution_sufficient'] = routine
    return {'difficulty_review': review, 'diagnosis_path': ['从输入流追踪到阈值裁决'],
            'code_evidence': ['app.py:decide']}


def test_developer_review_only_sees_faulty_tree_and_user_prompt(host, monkeypatch, tmp_path):
    work = source_work(tmp_path)
    original = git(work, 'rev-parse', 'HEAD')
    # Keep correct source refs and history in the source; none may reach the blind copy.
    (work / 'app.py').write_text('def decide(value):\n    return value >= 0\n')
    (work / '.gitattributes').write_text('app.py export-ignore\n')
    git(work, 'add', '.')
    git(work, 'commit', '-m', 'HIDDEN_PLANT_PATCH')
    git(work, 'branch', 'known-correct', original)
    frozen = git(work, 'rev-parse', 'HEAD')
    context = {'repo_path': str(work), 'current_commit': frozen, '_pairwise_mode': True,
               '_seeded_defect': {'changed_files': ['HIDDEN_CHANGED_FILES']},
               '_difficulty_dispute': 'HIDDEN_DISPUTE',
               '_approved_concept': {'root_cause': 'HIDDEN_CONCEPT'},
               'same_repository_history': [{'prompt': 'HIDDEN_HISTORY'}]}
    candidate = {'prompt': '零值被错误接受，应当拒绝，保留正数行为。',
                 'confirmed_bugs': [{'evidence': 'HIDDEN_ROOT_CAUSE'}],
                 'complexity_analysis': {'minimal_solution': 'HIDDEN_FIX'},
                 'seed_snapshot': {'source_sha': original, 'audit_path': 'HIDDEN_AUDIT'}}
    calls, copies = [], []
    def model(prompt, schema, cwd, prefix, *args, **kwargs):
        calls.append(prefix)
        if prefix == 'bugfix-validation':
            assert 'difficulty_review' not in schema['properties']
            assert 'HIDDEN_HISTORY' in prompt and 'HIDDEN_ROOT_CAUSE' in prompt
            return authenticity()
        assert prefix == 'bugfix-difficulty-review'
        assert str(work) not in prompt and original not in prompt and 'HIDDEN_' not in prompt
        assert candidate['prompt'] in prompt
        assert '修改位置局部' in prompt and '跨事务' in prompt
        assert kwargs['sandbox'] == 'read-only' and kwargs['model'] == host.REVIEW_MODEL
        assert not (cwd / '.git').exists()
        assert (cwd / 'app.py').read_bytes() == (work / 'app.py').read_bytes()
        assert sorted(p.name for p in cwd.iterdir()) == ['.gitattributes', 'app.py']
        copies.append(cwd)
        return blind_verdict()
    monkeypatch.setattr(host, 'run_codex_structured', model)
    result = host.run_codex_bugfix_validation(context, candidate)
    assert result['approved'] is True
    assert result['developer_difficulty_audit']['snapshot']['tree'] == git(work, 'rev-parse', 'HEAD^{tree}')
    assert calls == ['bugfix-validation', 'bugfix-difficulty-review']
    assert not copies[0].exists()
    assert git(work, 'rev-parse', 'HEAD') == frozen
    assert git(work, 'rev-parse', 'known-correct') == original
    assert git(work, 'status', '--porcelain', '--untracked-files=all') == ''


def test_known_correct_revert_reaches_blind_review_but_easy_bug_still_rejected(host, monkeypatch, tmp_path):
    work = source_work(tmp_path)
    calls = []
    real_run = host.run_command
    def git_only(command, *args, **kwargs):
        assert command[0] == 'git', 'Must not execute the product or any tests'
        return real_run(command, *args, **kwargs)
    def model(prompt, schema, cwd, stage, *args, **kwargs):
        calls.append(stage)
        if stage == 'bugfix-validation':
            return authenticity()
        if stage == 'bugfix-difficulty-review':
            assert 'return value >= 0' in (cwd / 'app.py').read_text()
            return blind_verdict(routine=True)
        result = static_model(prompt, schema, cwd, stage, *args, **kwargs)
        if stage == 'seeded-bug-patch':
            result.update(routine_assessment='足够', counterexample='')
        return result
    monkeypatch.setattr(host, 'run_command', git_only)
    monkeypatch.setattr(host, 'run_codex_structured', model)
    seed = seeded_bug.seed(host, {'pair': {'prompt': '零值应拒绝'}, 'bug_verification_mode': 'static'},
                           work, lambda *a: None)
    result = host.run_codex_bugfix_validation(
        {'repo_path': str(work), 'current_commit': seed['sha'], '_pairwise_mode': True},
        {'prompt': '零值被错误接受，应当拒绝，保留正数行为。'})
    assert calls == ['seeded-bug-design', 'seeded-bug-design-review', 'seeded-bug-patch',
                     'bugfix-validation', 'bugfix-difficulty-review']
    assert result['approved'] is False
    assert result['difficulty_review']['routine_solution_sufficient'] is True
    assert '低于困难' in '；'.join(result['reasons'])


def test_medium_bug_is_rejected_without_inflating_it_to_hard(host, monkeypatch, tmp_path):
    work = source_work(tmp_path)
    verdict = blind_verdict(routine=True, level='中等')
    monkeypatch.setattr(host, 'run_codex_structured', lambda *a, **kw:
                        authenticity() if a[3] == 'bugfix-validation' else verdict)
    result = host.run_codex_bugfix_validation(
        {'repo_path': str(work), 'current_commit': git(work, 'rev-parse', 'HEAD'),
         '_pairwise_mode': True},
        {'prompt': '修复需要追踪状态交错并验证保持现有行为。'})
    assert result['approved'] is False
    assert result['difficulty_review']['level'] == '中等'
    assert '低于困难' in '；'.join(host.bug_difficulty_review_errors(result))


def test_confirmed_bug_wording_failures_keep_the_frozen_direction(host):
    result = authenticity()
    result['approved'] = False
    result['difficulty_review_status'] = 'not_started'
    result['bug_review']['style_issues'] = ['触发条件没有写完整']
    feedback = host.bugfix_prompt_rewrite_feedback(result)
    assert '保留当前冻结源码' in feedback
    assert '不得换 Bug' in feedback

    result['bug_review']['overlapping_sequences'] = ['历史题已覆盖相同缺陷']
    assert host.bugfix_prompt_rewrite_feedback(result) == ''


@pytest.mark.parametrize('field,value', [('unverified_bugs', ['无代码依据']),
    ('overlapping_sequences', [1]), ('solution_leaks', ['答案泄漏']),
    ('scope_too_large', True), ('single_focus', False)])
def test_fault_duplicate_leak_and_scope_rejections_cannot_be_upgraded(host, monkeypatch, field, value):
    calls = []
    def model(*args, **kwargs):
        calls.append(args[3])
        result = authenticity()
        result['bug_review'][field] = value
        return result
    monkeypatch.setattr(host, 'run_codex_structured', model)
    result = host.run_codex_bugfix_validation({'repo_path': '.', '_pairwise_mode': True},
                                             {'prompt': '修复故障'})
    assert result['approved'] is False
    assert calls == ['bugfix-validation']
    assert result['difficulty_review_status'] == 'not_started'
    assert host.bugfix_review_errors(result)
    assert not any('任务难度依据' in error for error in host.bugfix_review_errors(result))
    # A corrupted approval cannot use the status marker to waive the hard gate.
    assert any('任务难度依据' in error for error in host.bugfix_review_errors({**result, 'approved': True}))
