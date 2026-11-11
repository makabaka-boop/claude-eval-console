from pathlib import Path
import subprocess

import pytest

from pairwise import auto_refill, models, seeded_bug, sources, workflow
from test_pairwise import git, host, payload, complete, sample_difficulty


@pytest.mark.parametrize('caller,expected', [
    ('file:///tmp/seed-check/[eval1]:11:8', True),
    ('[eval]:2:8', True),
    ('file:///tmp/seed-check/src/app.js:11:8', False),
])
def test_dom_bootstrap_error_requires_a_direct_harness_frame(caller, expected):
    output = ('TypeError: Cannot redefine property: window\n'
              '    at Function.defineProperty (<anonymous>)\n'
              f'    at {caller}\n')
    result = subprocess.CompletedProcess([], 1, '', output)
    assert seeded_bug._regression_environment_failure(result) is expected


def test_dom_bootstrap_failure_preserves_both_outputs_without_penalizing_source(host, monkeypatch, tmp_path):
    import json
    import shutil
    if not shutil.which('node'):
        pytest.skip('Node is required for the real inline DOM bootstrap regression')
    command = ('node --input-type=module -e \'Object.defineProperty(globalThis,"window",{value:1});'
               'Object.defineProperty(globalThis,"window",{value:2});\'')
    work = source_work(tmp_path)
    monkeypatch.setattr(host, 'run_codex_structured', lambda *a, **kw:
                        {**fake_seed_model(), 'regression_command': command})
    with pytest.raises(host.WorkflowError, match='环境故障') as caught:
        seeded_bug.seed(host, {'pair': {'prompt': '规则', 'verification_commands': ['true']}},
                        work, lambda *_: None)
    assert caught.value.source_environment_failure is True
    assert auto_refill._source_failure_policy(host, caught.value) == (False, False)
    evidence = json.loads(next((Path(host.DATA_DIR) / 'seed-verification').glob('*.json')).read_text())
    for side in ('original', 'seeded'):
        result = evidence['checks'][side]['regression']
        assert result['exit_code'] != 0
        assert 'Cannot redefine property: window' in result['stderr']


def source_work(tmp_path):
    work = tmp_path / "source"
    work.mkdir()
    git(work, "init", "-b", "main")
    git(work, "config", "user.name", "Test")
    git(work, "config", "user.email", "test@example.invalid")
    (work / "app.py").write_text("def decide(value):\n    return value > 0\n")
    git(work, "add", ".")
    git(work, "commit", "-m", "original")
    return work


def fake_seed_model(*args, **kwargs):
    assert kwargs.get("sandbox", "read-only") == "read-only"
    return {
        "found": True,
        "unavailable_reason": "",
        "patch": "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def decide(value):\n-    return value > 0\n+    return value >= 0\n",
        "affected_flow": "阈值裁决",
        "observable_failure": "零值被错误接受",
        "expected_behavior": "零值应拒绝",
        "design_tradeoff": "保持其他边界不变",
        "minimal_fix": "恢复严格阈值并验证零值与正数边界",
        "routine_assessment": "不足够",
        "counterexample": "只更改 UI 提示时零值仍被接受",
        "regression_command": "python3 -c 'from app import decide; import sys; sys.exit(0 if not decide(0) else 1)'",
    }


def test_seed_can_decline_before_fabricating_a_patch_or_regression(host, monkeypatch, tmp_path):
    work = source_work(tmp_path)
    original = git(work, 'rev-parse', 'HEAD')
    calls = []
    def no_direction(prompt, schema, cwd, prefix, *args, **kwargs):
        calls.append(prefix)
        assert 'found=false' in prompt
        assert 'found' in schema['required']
        assert 'minLength' not in schema['properties']['patch']
        return {'found': False, 'unavailable_reason': '可见候选均只需换缓存键的常规局部修复'}
    monkeypatch.setattr(host, 'run_codex_structured', no_direction)
    with pytest.raises(host.WorkflowError, match='未找到可合规预置的困难缺陷'):
        seeded_bug.seed(host, {'pair': {'prompt': '规则', 'verification_commands': ['false']}},
                        work, lambda *_: None)
    assert calls == ['seeded-bug-patch']
    assert git(work, 'rev-parse', 'HEAD') == original
    assert not git(work, 'status', '--porcelain', '--untracked-files=all')
    assert not (Path(host.DATA_DIR) / 'seed-verification').exists()


def test_seed_is_parentless_and_bundle_excludes_old_git_history(host, monkeypatch, tmp_path):
    work = source_work(tmp_path)
    original = git(work, "rev-parse", "HEAD")
    prompts = []
    def model(prompt, *args, **kwargs):
        prompts.append(prompt)
        return fake_seed_model(*args, **kwargs)
    monkeypatch.setattr(host, "run_codex_structured", model)
    source = {
        "pair": {"prompt": "阈值必须严格大于零", "verification_commands": ["true"]},
        "seed_history": [{"reference_id": "pair:old", "task_type": "Bug 修复",
                          "prompt": "历史已修复零值边界"}],
        "gsb_history": [{"reference_id": "gsb:other", "prompt": "其他项目的阈值处理"}],
        "rejected_directions": [{"reason": "同一缺陷家族已出题"}],
        "generation_feedback": "前次缓存键修复属于常规小修；浏览器初始化须先核对环境。",
    }
    seed = seeded_bug.seed(host, source, work, lambda *_: None)
    from pairwise.generation_policy import BUG_NOVELTY_GUIDANCE
    assert BUG_NOVELTY_GUIDANCE in prompts[0]
    assert '历史题已要求修复、已明确验收' not in prompts[0]
    assert "历史已修复零值边界" in prompts[0]
    assert "其他项目的阈值处理" in prompts[0]
    assert "同一缺陷家族已出题" in prompts[0]
    assert "前次缓存键修复属于常规小修" in prompts[0]
    assert "不代表当前候选已命中规则 C" in prompts[0]
    assert seed["changed_files"] == ["app.py"]
    assert seed["source_sha"] == original
    assert seed["sha"] != original
    assert git(work, "show", "-s", "--format=%P", seed["sha"]) == ""
    assert (work / "app.py").read_text().endswith("return value >= 0\n")
    bundle = seeded_bug.bundle(host, work, seed)
    isolated = tmp_path / "isolated"
    git(tmp_path, "clone", bundle["bundle_path"], str(isolated))
    git(isolated, "checkout", "--detach", seed["sha"])
    assert git(isolated, "rev-list", "--count", "HEAD") == "1"
    assert git(isolated, "rev-parse", "HEAD^{tree}") == seed["tree"]


def test_seed_found_true_still_requires_complete_defect_evidence(host, monkeypatch, tmp_path):
    work = source_work(tmp_path)
    original = git(work, 'rev-parse', 'HEAD')
    monkeypatch.setattr(host, 'run_codex_structured', lambda *a, **kw: {'found': True})
    with pytest.raises(host.WorkflowError, match='缺少代码改动或可观察的故障依据'):
        seeded_bug.seed(host, {'pair': {'prompt': '规则', 'verification_commands': []}},
                        work, lambda *_: None)
    assert git(work, 'rev-parse', 'HEAD') == original
    assert not git(work, 'status', '--porcelain', '--untracked-files=all')


def test_seed_rejects_test_or_delivery_changes(host, monkeypatch, tmp_path):
    work = source_work(tmp_path)
    patch = fake_seed_model()["patch"].replace("app.py", "README.md")
    (work / "README.md").write_text("def decide(value):\n    return value > 0\n")
    git(work, "add", "README.md")
    git(work, "commit", "-m", "readme")
    monkeypatch.setattr(host, "run_codex_structured", lambda *a, **k: {**fake_seed_model(), "patch": patch})
    with pytest.raises(host.WorkflowError, match="业务源代码"):
        seeded_bug.seed(host, {"pair": {"prompt": "规则", "verification_commands": ["true"]}},
                        work, lambda *_: None)


def test_seed_requires_same_scenario_to_regress_after_patch(host, monkeypatch, tmp_path):
    work = source_work(tmp_path)
    misleading = {**fake_seed_model(), "regression_command": "true"}
    monkeypatch.setattr(host, "run_codex_structured", lambda *a, **k: misleading)
    with pytest.raises(host.WorkflowError, match="由通过变为失败"):
        seeded_bug.seed(host, {"pair": {"prompt": "规则", "verification_commands": ["true"]}},
                        work, lambda *_: None)


def test_seed_preserves_complete_regression_over_old_thousand_character_limit(host, monkeypatch, tmp_path):
    work = source_work(tmp_path)
    command = "python3 -c 'from app import decide; label = \"" + "x" * 1100 + "\"; assert not decide(0)'"

    def model(prompt, schema, *args, **kwargs):
        assert 'maxLength' not in schema['properties']['regression_command']
        return {**fake_seed_model(), 'regression_command': command}

    monkeypatch.setattr(host, 'run_codex_structured', model)
    result = seeded_bug.seed(host, {'pair': {'prompt': '规则', 'verification_commands': ['true']}},
                             work, lambda *_: None)
    import json
    evidence = json.loads(Path(result['verification_evidence_path']).read_text())
    assert evidence['checks']['original']['regression']['exit_code'] == 0
    assert evidence['checks']['seeded']['regression']['exit_code'] != 0
    assert evidence['checks']['original']['regression']['command'] == command
    assert evidence['checks']['seeded']['regression']['command'] == command


def test_seed_repairs_broken_shell_before_touching_the_snapshot(host, monkeypatch, tmp_path):
    work = source_work(tmp_path)
    original = git(work, 'rev-parse', 'HEAD')
    marker = tmp_path / 'must-not-execute'
    calls = []
    stages = []

    def model(prompt, schema, cwd, prefix, *args, **kwargs):
        calls.append(prefix)
        if prefix == 'seeded-bug-patch':
            return {**fake_seed_model(), 'regression_command': f"touch '{marker}'; python3 -c 'unterminated"}
        assert prefix == 'seeded-bug-command-repair'
        assert git(work, 'rev-parse', 'HEAD') == original
        assert git(work, 'status', '--porcelain') == ''
        assert not marker.exists()
        assert '断言强度' in prompt
        return {'regression_command': fake_seed_model()['regression_command'],
                'regression_setup_command': ''}

    monkeypatch.setattr(host, 'run_codex_structured', model)
    result = seeded_bug.seed(host, {'pair': {'prompt': '规则', 'verification_commands': ['true']}},
                             work, stages.append)
    assert result['source_sha'] == original
    assert calls == ['seeded-bug-patch', 'seeded-bug-command-repair']
    assert any('语法无效' in stage for stage in stages)
    assert not marker.exists()


def test_unrepairable_command_is_not_a_source_behavior_failure(host, monkeypatch, tmp_path):
    work = source_work(tmp_path)
    original = git(work, 'rev-parse', 'HEAD')
    monkeypatch.setattr(host, 'run_codex_structured',
                        lambda *a, **k: {**fake_seed_model(), 'regression_command': "python3 -c 'broken"})
    with pytest.raises(host.WorkflowError, match='尚未进行缺陷对照') as raised:
        seeded_bug.seed(host, {'pair': {'prompt': '规则', 'verification_commands': ['true']}},
                        work, lambda *_: None)
    assert raised.value.source_environment_failure is True
    assert git(work, 'rev-parse', 'HEAD') == original
    assert git(work, 'status', '--porcelain') == ''


def test_unclosed_heredoc_cannot_silently_skip_the_regression(host, monkeypatch, tmp_path):
    work = source_work(tmp_path)
    broken = "cat <<'PROBE'\nprint('the closing marker and probe invocation were cut off')"
    # zsh considers this syntactically valid but would only write the heredoc.
    assert host.run_command(['/bin/zsh', '-n', '-c', broken], check=False).returncode == 0
    calls = []

    def repair(prompt, *args, **kwargs):
        calls.append(prompt)
        return {'regression_command': fake_seed_model()['regression_command'],
                'regression_setup_command': ''}

    monkeypatch.setattr(host, 'run_codex_structured', repair)
    checked = seeded_bug._checked_regression_commands(
        host, work, {'regression_command': broken}, lambda *_: None)
    assert len(calls) == 1 and 'heredoc 缺少结束标记' in calls[0]
    assert checked['regression_command'] == fake_seed_model()['regression_command']


@pytest.mark.parametrize('command', [
    "python3 <<'PROBE'\nassert True\nPROBE",
    "echo 'a literal << marker'",
])
def test_complete_heredoc_or_quoted_marker_does_not_trigger_repair(host, monkeypatch, tmp_path, command):
    def unexpected(*args, **kwargs):
        raise AssertionError('Valid commands must not be rewritten')
    monkeypatch.setattr(host, 'run_codex_structured', unexpected)
    checked = seeded_bug._checked_regression_commands(
        host, tmp_path, {'regression_command': command}, lambda *_: None)
    assert checked['regression_command'] == command


def test_seed_repairs_one_malformed_patch_without_changing_baseline(host, monkeypatch, tmp_path):
    work = source_work(tmp_path)
    calls = []
    stages = []

    def model(prompt, *args, **kwargs):
        calls.append(prompt)
        result = fake_seed_model()
        if len(calls) == 1:
            result["patch"] = "not a unified diff"
        return result

    monkeypatch.setattr(host, "run_codex_structured", model)
    result = seeded_bug.seed(host, {"pair": {"prompt": "规则", "verification_commands": ["true"]}},
                             work, stages.append)
    assert len(calls) == 2
    assert "not a unified diff" in calls[1]
    assert any("定向修正" in stage for stage in stages)
    assert git(work, "show", "-s", "--format=%P", result["sha"]) == ""


def test_compose_verification_uses_unique_project_random_ports_and_cleans_up(tmp_path):
    calls = []

    class FakeHost:
        def run_command(self, command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, "ok", "")

    with seeded_bug.isolated_verification(FakeHost(), tmp_path) as run:
        assert run("docker compose run --rm verify").returncode == 0
    first, cleanup = (command for command, _ in calls)
    assert first[0] == "env"
    assert first[1].startswith("COMPOSE_PROJECT_NAME=gsb-seed-")
    assert first[1] == cleanup[1]
    names = [item.partition("=")[0] for item in first[2:5]]
    ports = [int(item.partition("=")[2]) for item in first[2:5]]
    assert names == ["APP_PORT", "API_PORT", "WEB_PORT"]
    assert len(set(ports)) == 3 and all(port > 0 for port in ports)
    assert first[-3:] == ["/bin/zsh", "-lc", "docker compose run --rm verify"]
    assert cleanup[-3:] == ["compose", "down", "--remove-orphans"]

    calls.clear()
    with seeded_bug.isolated_verification(FakeHost(), tmp_path) as run:
        run("true")
    assert len(calls) == 1


def test_seeded_bug_pair_uses_private_source_lock_without_source_git_parent(host, monkeypatch, tmp_path):
    pair = models.create(host, payload(task_type="0-1 代码生成", snapshot_url=""))
    attempt = complete(host, pair["id"], "A")
    source = sources.claim(host, auto_refill.continuation_source(host, "Bug 修复"), "Bug 修复")
    work = source_work(tmp_path)
    monkeypatch.setattr(host, "run_codex_structured", fake_seed_model)
    seed = seeded_bug.seed(host, {"pair": {"prompt": "规则", "verification_commands": ["true"]}},
                           work, lambda *_: None)
    snapshot = seeded_bug.bundle(host, work, seed)
    created = models.create(host, payload(
        repo_name="repair-case-0002", task_type="Bug 修复", snapshot_url="", origin="auto",
        difficulty_preflight={"review": sample_difficulty(), "seed_snapshot": snapshot},
        source_pair_id=pair["id"], source_attempt_id=attempt["id"],
        source_reservation_token=source["reservation_token"],
    ))
    assert created["source_pair_id"] == pair["id"]
    assert created["source_base_sha"] == ""
    assert created["repo_url"] == ""
    assert [item["id"] for item in auto_refill._lineage(host, created)] == [pair["id"], created["id"]]
    assert sources.get_lock(host, pair["id"], "Bug 修复")["state"] == "created"
    assert auto_refill.continuation_source(host, "Bug 修复") is None
    workflow.start(host, created["id"])
    child_attempt = models.latest(models.attempts(host, created["id"]), "A")
    remote = tmp_path / "seeded-remote.git"
    remote_url = "https://github.com/fixture-owner/repair-case-0002"
    monkeypatch.setattr(host, "GITHUB_OWNER", "fixture-owner")
    real_run = host.run_command

    def local_github(command, **kwargs):
        command = list(command)
        if command[:3] == ["gh", "repo", "view"]:
            return subprocess.CompletedProcess(command, 1, "", "")
        if command[:3] == ["gh", "repo", "create"]:
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            real_run(["git", "push", str(remote), "main:refs/heads/main"], cwd=kwargs["cwd"])
            return subprocess.CompletedProcess(command, 0, "", "")
        return real_run([str(remote) if value == remote_url else value for value in command], **kwargs)

    monkeypatch.setattr(host, "run_command", local_github)
    published = workflow.ensure_pair_repository(host, child_attempt["id"],
                                                models.task(host, created["id"]))
    initial = Path(child_attempt["run_directory"]).parent / "main"
    assert git(initial, "rev-list", "--count", published["base_sha"]) == "1"
    assert git(initial, "rev-parse", f"{published['base_sha']}^{{tree}}") == snapshot["tree"]
    assert "Source-Snapshot" not in git(initial, "show", "-s", "--format=%B", published["base_sha"])
    assert workflow.remote_branches(host, remote_url) == {
        "main": published["base_sha"], "A": published["base_sha"], "B": published["base_sha"]
    }


def test_reference_fix_requires_same_regression_to_fail_before_and_pass_after(host, monkeypatch, tmp_path):
    work = source_work(tmp_path)
    monkeypatch.setattr(host, "run_codex_structured", fake_seed_model)
    seed = seeded_bug.seed(host, {"pair": {"prompt": "规则", "verification_commands": ["true"]}},
                           work, lambda *_: None)

    def repair(*args, **kwargs):
        fixed_work = args[2]
        (fixed_work / "app.py").write_text("def decide(value):\n    return value > 0\n")
        return {
            "fixed": True,
            "regression_command": "python3 -c 'import app; assert app.decide(0) is False'",
            "before_evidence": "零值错误通过",
            "after_evidence": "零值正确拒绝",
            "verification_reason": "相同边界输入已经恢复",
        }

    monkeypatch.setattr(host, "run_codex_structured", repair)
    seeded_bug.reference_fix(host, work, seed, {"prompt": "零值必须拒绝"}, ["true"], lambda *_: None)

    def fake_success(*args, **kwargs):
        result = repair(*args, **kwargs)
        result["regression_command"] = "true"
        return result

    monkeypatch.setattr(host, "run_codex_structured", fake_success)
    with pytest.raises(host.WorkflowError, match="修复前失败"):
        seeded_bug.reference_fix(host, work, seed, {"prompt": "零值必须拒绝"}, ["true"], lambda *_: None)

    def fix_preexisting_defect(*args, **kwargs):
        result = repair(*args, **kwargs)
        (args[2] / "app.py").write_text("def decide(value):\n    return value >= -1\n")
        result["regression_command"] = "python3 -c 'import app; assert app.decide(-1) is True'"
        return result

    monkeypatch.setattr(host, "run_codex_structured", fix_preexisting_defect)
    with pytest.raises(host.WorkflowError, match="预置前原产物"):
        seeded_bug.reference_fix(host, work, seed, {"prompt": "零值必须拒绝"}, ["true"], lambda *_: None)
