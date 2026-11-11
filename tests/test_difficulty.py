import json
from pathlib import Path
from unittest import mock

import pytest

import app
import difficulty_policy as policy
from pairwise import difficulty, models as m, solo, validation, export, auto_refill
from test_pairwise import host, ready_pair, sample_difficulty, sample_complexity, approve_difficulty

# Keep the real entry point while the shared host fixture suppresses automatic jobs.
enqueue = difficulty.enqueue_if_ready


def set_review(host, pair_id, **fields):
    with host.db_connection() as db:
        for key, value in fields.items():
            db.execute(f"UPDATE pairwise_tasks SET {key}=? WHERE id=?", (value, pair_id))


@pytest.mark.parametrize("status", ["", "queued", "running", "failed"])
def test_no_completed_review_blocks_all_submission_paths(host, status):
    pair_id, _, _ = ready_pair(host)
    set_review(host, pair_id, difficulty_status=status)
    assert not solo.quick_readiness(host, pair_id)[0]
    assert not validation.preflight(host, pair_id)["eligible"]
    assert not solo.exports_page(host, {})["items"][0]["export_ready"]
    with pytest.raises(host.WorkflowError, match="难度复评"):
        solo.bundle(host, pair_id)
    with pytest.raises(host.WorkflowError, match="难度复评"):
        export.row(host, pair_id)


@pytest.mark.parametrize("level", ["简单", "中等"])
def test_lower_review_cannot_be_overridden_by_label_or_gsb(host, level):
    pair_id, _, _ = ready_pair(host)
    approve_difficulty(host, pair_id, level)
    set_review(host, pair_id, difficulty="地狱", human_confirmed=1)
    assert not solo.quick_readiness(host, pair_id)[0]
    assert f"实际难度为{level}" in "；".join(validation.preflight(host, pair_id)["issues"])
    assert solo.values(host, pair_id)["difficulty"] == level
    assert auto_refill.continuation_source(host, "Feature 迭代") is None


def confirm_manual_difficulty(host, pair_id, **overrides):
    pair = m.task(host, pair_id)
    preflight = pair["difficulty_preflight"]
    preflight["manual_annotation"] = {
        "level": "困难", "origin": "user_request", "use_for_submission": True,
        "source": pair["difficulty_source"], "basis_reviewed_at": pair["difficulty_reviewed_at"],
        **overrides,
    }
    set_review(host, pair_id, difficulty_preflight=json.dumps(preflight))


def test_confirmed_manual_difficulty_applies_to_all_delivery_paths(host):
    pair_id, _, _ = ready_pair(host)
    approve_difficulty(host, pair_id, "中等")
    original = m.task(host, pair_id)
    confirm_manual_difficulty(host, pair_id)
    pair = m.task(host, pair_id)
    result = difficulty.assessment(host, pair)
    assert result["level"] == "困难" and result["label"] == "困难 · 人工标注"
    assert result["automated_level"] == "中等" and result["decision_source"] == "manual"
    assert pair["difficulty_review"] == original["difficulty_review"]
    assert pair["notes"] == original["notes"]
    assert solo.quick_readiness(host, pair_id)[0]
    assert validation.preflight(host, pair_id)["eligible"]
    assert solo.exports_page(host, {})["items"][0]["export_ready"]
    assert solo.bundle(host, pair_id)["values"]["difficulty"] == "困难"
    assert export.row(host, pair_id)[2] == "困难"


@pytest.mark.parametrize("overrides", [
    {"use_for_submission": False}, {"source": "old"}, {"basis_reviewed_at": "old"},
    {"level": "中等"}, {"origin": "generated"},
])
def test_unconfirmed_or_stale_manual_label_does_not_remove_gate(host, overrides):
    pair_id, _, _ = ready_pair(host)
    approve_difficulty(host, pair_id, "中等")
    confirm_manual_difficulty(host, pair_id, **overrides)
    result = difficulty.assessment(host, m.task(host, pair_id))
    assert result["level"] == "中等" and result["eligible"]


@pytest.mark.parametrize("change", ["prompt", "artifact_sha", "difficulty_status"])
def test_manual_decision_cannot_authorize_changed_or_unreviewed_evidence(host, change):
    pair_id, a, _ = ready_pair(host)
    approve_difficulty(host, pair_id, "中等")
    confirm_manual_difficulty(host, pair_id)
    if change == "artifact_sha":
        m.update_attempt(host, a["id"], artifact_sha="new material")
    else:
        set_review(host, pair_id, **{change: "running" if change == "difficulty_status" else "new material"})
    result = difficulty.assessment(host, m.task(host, pair_id))
    assert result["decision_source"] == "automatic" and not result["eligible"]


@pytest.mark.parametrize("change", ["prompt", "base_sha", "artifact_sha", "trace_sha256", "verification", "retry"])
def test_review_is_bound_to_current_requirements_and_latest_snapshots(host, change):
    pair_id, a, _ = ready_pair(host)
    if change in {"prompt", "base_sha"}:
        set_review(host, pair_id, **{change: "new material"})
    elif change == "retry":
        m.update_attempt(host, a["id"], status="running")
    else:
        m.update_attempt(host, a["id"], **{change: [{"exit_code": 1}] if change == "verification" else "new material"})
    result = difficulty.assessment(host, m.task(host, pair_id))
    assert result["status"] == "pending" and not result["eligible"]


def test_recording_and_gsb_edits_do_not_change_task_difficulty(host):
    pair_id, a, _ = ready_pair(host)
    m.update_attempt(host, a["id"], recording_confirmed=0, video_seconds=25)
    set_review(host, pair_id, reason="新的 GSB 理由")
    assert difficulty.assessment(host, m.task(host, pair_id))["eligible"]


def test_exports_use_reviewed_level_and_ignore_generation_time_budget(host):
    pair_id, _, _ = ready_pair(host)
    review = sample_difficulty("地狱")
    review["one_hour_feasible"] = False
    set_review(host, pair_id, difficulty_review=json.dumps(review))
    assert export.row(host, pair_id)[2] == "地狱"
    assert solo.bundle(host, pair_id)["values"]["difficulty"] == "地狱"


def test_medium_bug_review_can_be_submitted_without_changing_review(host):
    pair_id, _, _ = ready_pair(host, task_type="Bug 修复")
    approve_difficulty(host, pair_id, "中等")
    pair = m.task(host, pair_id)
    assert difficulty.assessment(host, pair)["eligible"]
    assert validation.preflight(host, pair_id)["eligible"]
    assert solo.quick_readiness(host, pair_id)[0]
    assert solo.exports_page(host, {"number": [pair["display_number"]]})["items"][0]["export_ready"]
    assert export.row(host, pair_id)[2] == "中等"
    assert solo.bundle(host, pair_id)["values"]["difficulty"] == "中等"


def test_medium_zero_one_review_remains_below_submission_floor(host):
    pair_id, _, _ = ready_pair(host, task_type="0-1 代码生成", snapshot_url="")
    approve_difficulty(host, pair_id, "中等")
    result = difficulty.assessment(host, m.task(host, pair_id))
    assert not result["eligible"]
    assert "低于困难" in "；".join(result["issues"])


@pytest.mark.parametrize("level", ["困难", "中等"])
def test_completion_review_reads_initial_diff_and_trace_without_author_label(host, monkeypatch, level):
    pair_id, a, b = ready_pair(host)
    pair = m.task(host, pair_id)
    source = difficulty.source_key(pair, {"A": a, "B": b})
    set_review(host, pair_id, difficulty_status="queued", difficulty_source=source)
    original = (pair["gsb"], pair["reason"], pair["human_confirmed"])

    def evaluate(prompt, schema, cwd, prefix, timeout, **kwargs):
        root = Path(cwd)
        task = json.loads((root / "evidence/task.json").read_text())
        assert "difficulty" not in task and "difficulty_preflight" not in task
        assert (root / "initial/README.md").read_text() == "base\n"
        assert "+A" in (root / "evidence/A.diff").read_text()
        assert (root / "evidence/B-trace.jsonl").is_file()
        assert "minimal_solution" in schema["required"] and "code_evidence" in schema["required"]
        result = sample_difficulty(level)
        if level == "中等":
            result.update(routine_solution_sufficient=True, hard_drivers=[], design_tradeoffs=[])
        return result

    monkeypatch.setattr(host, "run_codex_structured", evaluate)
    difficulty.generate(host, pair_id, source)
    pair = m.task(host, pair_id)
    assert pair["difficulty_status"] == "complete", pair["difficulty_error"]
    assert pair["difficulty_review"]["level"] == level
    assert pair["difficulty_review"]["policy_version"] == policy.VERSION
    assert (pair["gsb"], pair["reason"], pair["human_confirmed"]) == original
    assert difficulty.assessment(host, pair)["eligible"] == (level == "困难")


def test_changed_snapshot_during_review_discards_result(host, monkeypatch):
    pair_id, _, _ = ready_pair(host)
    pair = m.task(host, pair_id)
    source = pair["difficulty_source"]
    set_review(host, pair_id, difficulty_status="queued")
    def evaluate(*args, **kwargs):
        set_review(host, pair_id, prompt="changed during review")
        return sample_difficulty()
    monkeypatch.setattr(host, "run_codex_structured", evaluate)
    difficulty.generate(host, pair_id, source)
    assert not difficulty.assessment(host, m.task(host, pair_id))["eligible"]


@pytest.mark.parametrize("path,line", [("../secret", 1), ("/etc/passwd", 1), ("A/product.txt", 50), ("A/missing", 1)])
def test_fabricated_code_references_cannot_pass(host, tmp_path, path, line):
    (tmp_path / "initial").mkdir()
    (tmp_path / "initial/README.md").write_text("base")
    (tmp_path / "A").mkdir()
    (tmp_path / "A/product.txt").write_text("product")
    result = sample_difficulty()
    result["code_evidence"][1].update(path=path, line=line)
    with pytest.raises(host.WorkflowError, match="引用|核对"):
        difficulty.validate_result(host, result, tmp_path)


def test_duplicate_enqueue_is_idempotent_and_recovery_does_not_review_all_history(host, monkeypatch):
    pair_id, _, _ = ready_pair(host)
    set_review(host, pair_id, difficulty_status="")
    calls = []
    class Immediate:
        def __init__(self, target, daemon): self.target = target
        def start(self): self.target()
    monkeypatch.setattr(difficulty.threading, "Thread", Immediate)
    monkeypatch.setattr(difficulty, "generate", lambda *args: calls.append(args))
    difficulty.recover(host)
    assert not calls
    assert enqueue(host, pair_id)
    assert not enqueue(host, pair_id)
    assert len(calls) == 1


def test_generator_and_reviewer_must_explain_minimal_solution_and_counterexample():
    review = sample_difficulty()
    assert not app.difficulty_review_errors({"difficulty_review": review})
    for field in policy.analysis_schema()["required"]:
        missing = {k: v for k, v in review.items() if k != field}
        assert app.difficulty_review_errors({"difficulty_review": missing})
    review["routine_solution_sufficient"] = True
    assert "常规方案" in "；".join(app.difficulty_review_errors({"difficulty_review": review}))


def test_blind_review_strips_self_assessment_recursively():
    data = {"difficulty":"地狱", "complexity_analysis":sample_complexity(),
            "project":{"task_difficulty":"困难"}, "prompt":"真实需求", "history":[{"difficulty":"困难"}]}
    assert policy.blind_materials(data) == {"project":{}, "prompt":"真实需求", "history":[{}]}
    candidate = {"first_prompt":"真实需求", "complexity_analysis":{"minimal_solution":"AUTHOR_ONLY_ANCHOR"},
                 "difficulty_review":{"hard_drivers":["AUTHOR_ONLY_ANCHOR"]}}
    with mock.patch.object(app, "run_codex_structured", return_value={}) as run:
        app.run_codex_task_validation(candidate, "纯后端", [])
    assert "AUTHOR_ONLY_ANCHOR" not in run.call_args.args[0]


def test_auto_pair_keeps_preflight_evidence_and_rejects_mismatched_label(host):
    draft = {"repo_name":"evidence-fixture", "first_prompt":"需求", "category":"纯后端",
             "language_framework":"Go", "task_difficulty":"困难",
             "difficulty_review":sample_difficulty(), "complexity_analysis":sample_complexity()}
    value = auto_refill._payload(host, draft, "test")
    pair = m.create(host, value)
    assert pair["difficulty_preflight"]["review"]["minimal_solution"]
    draft["task_difficulty"] = "地狱"
    with pytest.raises(ValueError, match="不接受地狱"):
        auto_refill._payload(host, draft, "test")


def test_easy_candidate_is_replaced_without_difficulty_padding(host, monkeypatch):
    import test_app as fixtures
    examples = fixtures.DraftTests()
    first, second = examples.candidate(), examples.candidate()
    second.update(repo_slug="replacement-candidate", title="Replacement Candidate")
    rejected, accepted = examples.scope_review(), examples.scope_review()
    rejected["approved"] = False
    rejected["difficulty_review"].update(level="中等", routine_solution_sufficient=True)
    monkeypatch.setattr(host, "run_codex_task_generation", lambda *a, **k: {"candidates":[first, second]})
    monkeypatch.setattr(host, "unique_repo_name", lambda name: name)
    monkeypatch.setattr(host, "generated_task_quality_key", lambda *a: 0)
    with mock.patch.object(host, "run_codex_task_validation", side_effect=[rejected, accepted]), \
         mock.patch.object(host, "run_codex_task_rewrite") as rewrite:
        draft = host.generate_task_draft(1, history_override=[])
    assert draft["repo_name"] == "replacement-candidate"
    rewrite.assert_not_called()
