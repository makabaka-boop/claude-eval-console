import pytest

from pairwise import auto_refill as refill, candidate_bank
from test_pairwise import host


def test_bug_only_selection_survives_pause_and_service_initialization(host):
    config = refill.configure(host, {"enabled": False, "task_type_mode": "bug_only"})
    assert config["task_types"] == ["Bug 修复"]
    assert config["next_task_type"] == "Bug 修复"
    assert config["task_mix_policy"] == "bug_only"
    for enabled in (True, False, True):
        refill.initialize(host)
        config = refill.configure(host, {"enabled": enabled})
        assert config["task_type_mode"] == "bug_only"
    assert "仅生成 Bug 修复" in config["detail"]
    restored = refill.configure(host, {"enabled": False, "task_type_mode": "balanced"})
    assert restored["task_types"] == list(refill.BALANCED_TASK_TYPES)


@pytest.mark.parametrize("mode", [None, "bug", True, [], {}])
def test_invalid_mode_does_not_enable_generation(host, mode):
    with pytest.raises(host.WorkflowError, match="补题类型"):
        refill.configure(host, {"enabled": True, "task_type_mode": mode})
    assert not refill.configuration(host)["enabled"]


@pytest.mark.parametrize("ready_mode", ["any", "only", "exclude"])
def test_bug_only_ignores_other_type_deficits_and_ready_candidates(host, monkeypatch, ready_mode):
    refill.configure(host, {"enabled": True, "task_type_mode": "bug_only"})
    seen = []
    def source(h, kind, blocked, **kwargs):
        seen.append(kind)
        return {"kind": kind}
    monkeypatch.setattr(refill, "continuation_source", source)
    monkeypatch.setattr(candidate_bank, "ready_task_counts",
                        lambda h: {"0-1 代码生成": 4, "Feature 迭代": 4, "Bug 修复": 1})
    kind, chosen, _ = refill.next_available_task(
        host, {"0-1 代码生成": 0, "Feature 迭代": 0, "Bug 修复": 99}, {}, ready_mode=ready_mode)
    assert kind == "Bug 修复" and chosen == {"kind": "Bug 修复"}
    assert seen == ["Bug 修复"]


@pytest.mark.parametrize("cooling", [False, True])
def test_bug_only_dispatch_and_prefill_wait_instead_of_falling_back(host, monkeypatch, cooling):
    refill.configure(host, {"enabled": True, "task_type_mode": "bug_only"})
    for key, value in [("ACTIVE", False), ("PREFILL_ACTIVE", False),
                       ("PREFILL_NEXT_AT", 0), ("RETRY_NEXT_AT", 0)]:
        monkeypatch.setattr(refill, key, value)
    monkeypatch.setattr(refill, "_active_attempt_count", lambda h: 0)
    monkeypatch.setattr(candidate_bank, "ready_task_counts", lambda h: {"0-1 代码生成": 4})
    checked = []
    def no_source(h, kind, blocked, **kwargs):
        checked.append(kind)
        return None
    monkeypatch.setattr(refill, "continuation_source", no_source)
    monkeypatch.setattr(host, "generate_task_draft", lambda **kw: pytest.fail("Must not generate 0-1"))
    monkeypatch.setattr(refill.m, "create", lambda *a, **kw: pytest.fail("Must not create a fallback Pair"))
    if cooling:
        for _ in range(refill.TYPE_FAILURE_LIMIT):
            refill._record_generation_outcome(host, "Bug 修复", host.WorkflowError("无合格方向"))
    assert refill.run_once(host) is False
    assert refill._prefill_once(host) is False
    assert set(checked) <= {"Bug 修复"}
    config = refill.configuration(host)
    assert config["enabled"]
    assert config["status"] == "waiting_baseline"
    assert "仅补 Bug 修复" in config["detail"]
    assert config["candidate_ready"] == 0


def test_other_ready_types_do_not_fill_bug_candidate_capacity(host, monkeypatch):
    monkeypatch.setattr(candidate_bank, "overlap", lambda *a: "")
    for number, prompt in enumerate(["苹果" * 70, "河流" * 70, "星空" * 70, "沙漠" * 70], 1):
        assert candidate_bank.stage(host, "0-1 代码生成", {"prompt": prompt}, number, max_ready=4)
    assert candidate_bank.stage(host, "Bug 修复", {"prompt": "时钟" * 70}, 5,
                                max_ready=1, capacity_task_type="Bug 修复")
    assert candidate_bank.stage(host, "Bug 修复", {"prompt": "书籍" * 70}, 6,
                                max_ready=1, capacity_task_type="Bug 修复") is None
    assert candidate_bank.ready_task_counts(host) == {"0-1 代码生成": 4, "Bug 修复": 1}
    with pytest.raises(host.WorkflowError, match="不属于当前补题范围"):
        candidate_bank.stage(host, "Feature 迭代", {"prompt": "音乐" * 70}, 7,
                             max_ready=1, capacity_task_type="Bug 修复")

