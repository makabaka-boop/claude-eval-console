import json
import math
import random
import shutil
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

import pytest

from pairwise import browser_demo as demo


class InputRecorder:
    def __init__(self):
        self.calls = []
        self.text = ""

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "Input.insertText":
            self.text += params["text"]
        if method == "Input.dispatchKeyEvent" and params.get("commands") == ["selectAll"]:
            self.selected_all = True
        if method == "Input.dispatchKeyEvent" and params.get("key") == "Backspace" and getattr(self, "selected_all", False):
            self.text = ""
            self.selected_all = False

    def evaluate(self, expression):
        return self.text


def test_text_is_entered_progressively_and_keeps_exact_content(monkeypatch):
    cdp = InputRecorder()
    pauses = []
    monkeypatch.setattr(demo.time, "sleep", pauses.append)
    value = "预约备注：明天下午检查水压。API 123"
    demo._fill(cdp, value)
    chunks = [params["text"] for method, params in cdp.calls if method == "Input.insertText"]
    assert "".join(chunks) == value
    assert max(map(len, chunks)) <= 3
    assert len(pauses) > len(chunks) and len(set(pauses)) > 1
    assert not any(params.get("key") == "Tab" for _, params in cdp.calls)


def test_input_timeout_does_not_paste_remaining_text_or_tab_away(monkeypatch):
    cdp = InputRecorder()
    now = [0.0]
    value = "ordinary text with spaces"
    monkeypatch.setattr(demo.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(demo.time, "sleep", lambda value: now.__setitem__(0, now[0] + value))
    with pytest.raises(RuntimeError, match="上限"):
        demo._fill(cdp, value, deadline=2)
    assert 0 < len(cdp.text) < len(value)
    assert not any(params.get("key") == "Tab" for _, params in cdp.calls)


def test_rejected_input_is_not_reported_as_completed(monkeypatch):
    cdp = InputRecorder()
    monkeypatch.setattr(demo.time, "sleep", lambda _: None)
    cdp.evaluate = lambda _: ""
    with pytest.raises(RuntimeError, match="未完整接受"):
        demo._fill(cdp, "test")


@pytest.mark.parametrize("seed",range(8))
def test_typing_cadence_has_fast_bursts_and_slower_sections(seed):
    value="abcdefghijklmnopqrstuvwxyz0123456789"*2
    schedule=list(demo._typing_schedule(value,random.Random(seed)))
    assert "".join(chunk for chunk,_ in schedule)==value
    delays=[delay for _,delay in schedule]
    fast=[delay<0.13 for delay in delays]
    slow=[delay>0.15 for delay in delays]
    assert any(fast[index] and fast[index+1] for index in range(len(fast)-1))
    assert sum(slow)>=5
    assert max(delays)>3*min(delays)
    assert len(set(round(delay,3) for delay in delays))>10
    assert sum(delays)<20  # variation must not turn a short demo into a long wait


def test_typing_cadence_varies_between_fields_and_preserves_cjk_chunks():
    value="录入计划序列：明天核对水压。API [11,22,33]\n第二行"
    schedules=[list(demo._typing_schedule(value,random.Random(seed))) for seed in range(4)]
    assert len({tuple(schedule) for schedule in schedules})==4
    for schedule in schedules:
        assert "".join(chunk for chunk,_ in schedule)==value
        assert all(1<=len(chunk)<=3 for chunk,_ in schedule)
        assert all(0<delay<1.2 for _,delay in schedule)
    assert list(demo._typing_schedule(""))==[]


def test_typing_cadence_pauses_at_phrase_boundaries():
    baseline=list(demo._typing_schedule("abcd",random.Random(0)))
    phrase=list(demo._typing_schedule("a.cd",random.Random(0)))
    assert phrase[0][1]==baseline[0][1]
    assert phrase[1][1]==baseline[1][1]
    assert phrase[2][1]>baseline[2][1]+0.25


def test_recording_cadence_varies_between_actions_and_videos():
    actions = [
        {"type": "fill", "target": "名称"},
        {"type": "click", "target": "生成校准结果"},
        {"type": "upload", "target": "导入 JSON"},
        {"type": "click", "target": "保存"},
    ]
    first = demo._recording_cadence(actions, random.Random(7))
    second = demo._recording_cadence(actions, random.Random(8))
    assert len({item["reading"] for item in first}) == len(first)
    assert [item["reading"] for item in first] != [item["reading"] for item in second]
    assert [item["picker_open"] for item in first] != [item["picker_open"] for item in second]
    assert all(0.16 <= item["reading"] <= 3.8 for item in first + second)
    assert first[1]["reading"] > first[0]["reading"] + 1.0
    assert any(item["reading_move"] for item in first)
    assert any(item["reading_move"] for item in second)


def test_upload_reaches_file_button_without_the_normal_opening_hold():
    first = demo._recording_cadence([{"type": "upload", "target": "导入 JSON"}], random.Random(4))[0]
    second = demo._recording_cadence([{"type": "upload", "target": "导入 JSON"}], random.Random(5))[0]
    assert first["opening"] <= 0.33
    assert first["browser_focus"] <= 0.15
    assert (first["opening"], first["browser_focus"]) != (
        second["opening"],
        second["browser_focus"],
    )


def test_upload_activates_browser_before_pointer_approaches(tmp_path, monkeypatch):
    calls = []

    class Browser:
        def close(self):
            pass

    timing = {"profile": 1.0, "reading": 0.2}
    action = {"type": "upload", "target": "选择文件", "value": "{}"}
    monkeypatch.setattr(demo, "CDP", lambda *_args, **_kwargs: Browser())
    monkeypatch.setattr(demo, "_wait_for_page", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(demo, "viewport_rect", lambda _cdp: "0,0,800,600")
    monkeypatch.setattr(demo, "_recording_cadence", lambda _actions: [timing])
    monkeypatch.setattr(demo, "_timed_pause", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(demo, "_resolved_action", lambda _cdp, current:
                        (current, {"x": 100, "y": 80, "width": 80, "height": 24,
                                   "name": "选择文件", "fileIndex": 0}))
    monkeypatch.setattr(demo, "_activate_recording_browser",
                        lambda *_args, **_kwargs: calls.append("activate"))
    monkeypatch.setattr(demo, "_approach",
                        lambda *_args, **_kwargs: calls.append("approach"))

    def upload(*_args, **kwargs):
        assert kwargs["activate_browser"] is False
        calls.append("upload")
        return str(tmp_path / "source-1.json")

    monkeypatch.setattr(demo, "_upload", upload)
    monkeypatch.setattr(demo, "_reading_pause", lambda *_args, **_kwargs: (100, 80))
    demo.run(1234, {"actions": [action]}, "A", tmp_path / "actions.json",
             upload_directory=tmp_path, browser_pid=42)
    assert calls == ["activate", "approach", "upload"]


def test_recording_cadence_config_is_hot_loaded(tmp_path, monkeypatch):
    path = tmp_path / "cadence.json"
    config = dict(demo.DEFAULT_CADENCE_CONFIG)
    config["tempo"] = [1.0, 1.0]
    config["reading_bases"] = {"fill": 0.1, "select": 0.32, "upload": 0.4, "click": 0.56}
    path.write_text(json.dumps(config))
    monkeypatch.setenv("PAIRWISE_RECORDING_CADENCE_FILE", str(path))
    first = demo._recording_cadence([{"type": "fill", "target": "名称"}], random.Random(3))[0]
    config["reading_bases"] = {**config["reading_bases"], "fill": 0.8}
    path.write_text(json.dumps(config))
    second = demo._recording_cadence([{"type": "fill", "target": "名称"}], random.Random(3))[0]
    assert first["reading"] == 0.16
    assert second["reading"] > 0.4


def test_fill_uses_varied_schedule_without_extra_keystrokes(monkeypatch):
    cdp=InputRecorder()
    now=[0.0]
    input_times=[]
    call=cdp.call
    def tracked_call(method,params):
        if method=="Input.insertText":
            input_times.append(now[0])
        call(method,params)
    cdp.call=tracked_call
    monkeypatch.setattr(demo.time,"monotonic",lambda:now[0])
    monkeypatch.setattr(demo.time,"sleep",lambda value:now.__setitem__(0,now[0]+value))
    schedule=demo._typing_schedule
    monkeypatch.setattr(demo,"_typing_schedule",lambda value:schedule(value,random.Random(3)))
    value="abcdefghijklmno"
    demo._fill(cdp,value,deadline=15)
    gaps=[b-a for a,b in zip(input_times,input_times[1:])]
    assert max(gaps)>3*min(gaps)
    assert cdp.text==value
    keydowns=[p["key"] for method,p in cdp.calls if method=="Input.dispatchKeyEvent" and p["type"]=="keyDown"]
    assert keydowns==["a","Backspace"]


@pytest.mark.parametrize("value,target",[
    ("curl -X POST http://127.0.0.1:8080/compare", ""),
    ("API_PORT=18080 docker compose up -d", ""),
    ("go test ./...", ""),
    ("npm run build", ""),
    ("[11,22,33]", "计划 cue 序列"),
    ('{"name":"测试","n":3}', ""),
    ("{invalid-json", "JSON 编辑区"),
    ("/Users/example/My Project", ""),
    (r"C:\\projects\\sample", ""),
    ("https://example.test/demo", ""),
    ("SELECT id FROM cues WHERE id > 3", ""),
    ("def main():\n    return 1", ""),
    ("const n = 3;", ""),
    ("custom-tool --verify", "命令"),
    ("0041-repair-case-0599-b4b9a2", "任务标识"),
    ("0123456789abcdef0123456789abcdef01234567", "名称"),
    ("very-long-repository-slug", "名称"),
    ("(01)09506000134352(10)INK2407(17)280930", "标签原文"),
    ("La donna è mobile", "原文行"),
    ("女人善变", "译文行"),
    ("ISO 12647-2", "专业术语"),
])
def test_technical_content_uses_paste(value,target):
    assert demo._should_paste(value,target)


def test_resolved_subtitle_context_uses_paste_for_generic_field_label():
    assert demo._should_paste("Nessun dorma", "中文", "中文唱词/字幕 中文")


def test_explicit_copy_paste_context_uses_one_paste_for_multiline_import():
    value = "[shutters]\nA\nB\n[rules]\nA OPEN OR B OPEN\nA CLOSED OR B OPEN"
    assert demo._should_paste(value, context="粘贴一组自定义规则，停顿确认完整文本")


@pytest.mark.parametrize("value,target", [
    ("E -> A t\nA -> B\nA -> t\nB -> C D\nC ->\nD ->", "产生式编辑框"),
    ("第一行\r\n第二行", "备注"),
    ("ordinary text with spaces " * 3, "description"),
    ("长段文字包含空格和标点，应该整段粘贴。" * 3, "说明"),
    ("x " * 20 + "x", "名称"),
])
def test_long_or_multiline_content_is_inserted_as_one_paste(monkeypatch, value, target):
    cdp = InputRecorder()
    pauses = []
    monkeypatch.setattr(demo.time, "sleep", pauses.append)
    assert demo._fill(cdp, value, target=target) == "paste"
    assert [p["text"] for method, p in cdp.calls if method == "Input.insertText"] == [value]
    assert cdp.text == value
    assert demo.PASTE_FOCUS_PAUSE[0] <= pauses[0] <= demo.PASTE_FOCUS_PAUSE[1]
    assert demo.PASTE_SETTLE_PAUSE[0] <= pauses[-1] <= demo.PASTE_SETTLE_PAUSE[1]


def test_short_prose_stays_paced_at_long_text_boundary():
    assert not demo._should_paste("a " * 19 + "a", "description")
    assert demo._should_paste("a " * 19 + "ab", "description")


@pytest.mark.parametrize("value,target",[
    ("晚场演出", "名称"),
    ("先检查水压，再核对阈值。", "备注"),
    ("go to the park", "description"),
    ("Sample", "名称"),
    ("2", "阈值 K"),
    ("2026-09-18", "日期"),
    ("short-repo-slug", "名称"),
    ("", "命令"),
])
def test_ordinary_content_keeps_paced_typing(value,target):
    assert not demo._should_paste(value,target)


@pytest.mark.parametrize("value,target",[
    ("  [11,22,33]\n", "计划 cue 序列"),
    ("curl http://127.0.0.1:8080/\n", "命令"),
    ("custom-tool --check", "命令"),
])
def test_paste_preserves_exact_content_in_one_input_without_enter(monkeypatch,value,target):
    cdp=InputRecorder()
    pauses=[]
    monkeypatch.setattr(demo.time,"sleep",pauses.append)
    demo._fill(cdp,value,target=target)
    inserts=[p["text"] for method,p in cdp.calls if method=="Input.insertText"]
    assert inserts==[value] and cdp.text==value
    assert demo.PASTE_FOCUS_PAUSE[0]<=pauses[0]<=demo.PASTE_FOCUS_PAUSE[1]
    assert demo.PASTE_SETTLE_PAUSE[0]<=pauses[-1]<=demo.PASTE_SETTLE_PAUSE[1]
    assert not any(p.get("key") in {"Enter","Tab"} for _,p in cdp.calls)


def test_paste_obeys_deadline_before_changing_input(monkeypatch):
    cdp=InputRecorder()
    monkeypatch.setattr(demo.time,"monotonic",lambda:0)
    with pytest.raises(RuntimeError,match="上限"):
        demo._fill(cdp,"[11,22,33]",deadline=0.5)
    assert cdp.calls==[]


def test_rejected_paste_is_not_reported_as_success(monkeypatch):
    cdp=InputRecorder()
    cdp.evaluate=lambda _:"wrong value" if cdp.text else ""
    monkeypatch.setattr(demo.time,"sleep",lambda _:None)
    with pytest.raises(RuntimeError,match="未完整接受"):
        demo._fill(cdp,"[11,22,33]")
    assert not any(p.get("key")=="Tab" for _,p in cdp.calls)


def test_fill_replaces_prefilled_json_with_native_select_all(monkeypatch):
    cdp = InputRecorder()
    cdp.text = "[101, 102, 103]"
    monkeypatch.setattr(demo.time, "sleep", lambda _: None)
    demo._fill(cdp, "[1,2,3,4]", target="计划 cue 序列")
    assert cdp.text == "[1,2,3,4]"
    commands = [params for _, params in cdp.calls if "commands" in params]
    assert len(commands) == 1 and commands[0]["type"] == "keyDown"


def test_fill_stops_before_inserting_when_old_content_cannot_be_cleared(monkeypatch):
    cdp = InputRecorder()
    cdp.evaluate = lambda _: "[101, 102, 103]"
    monkeypatch.setattr(demo.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="未能清空旧内容"):
        demo._fill(cdp, "[1,2,3,4]", target="计划 cue 序列")
    assert not any(method == "Input.insertText" for method, _ in cdp.calls)


def test_fill_accepts_controlled_number_that_normalizes_empty_to_requested_value(monkeypatch):
    class NormalizedNumber(InputRecorder):
        def call(self, method, params):
            super().call(method, params)
            if method == "Input.dispatchKeyEvent" and params.get("key") == "Backspace":
                self.text = "1"

    cdp = NormalizedNumber()
    cdp.text = "3"
    monkeypatch.setattr(demo.time, "sleep", lambda _: None)
    demo._fill(cdp, "1", target="隔离点数量（1–20）")
    assert cdp.text == "1"
    assert not any(method == "Input.insertText" for method, _ in cdp.calls)


def test_fill_replaces_controlled_number_that_normalizes_empty_to_zero(monkeypatch):
    class ZeroOnClear(InputRecorder):
        def call(self, method, params):
            if method == "Input.insertText" and getattr(self, "selected_all", False):
                self.text = ""
                self.selected_all = False
            super().call(method, params)
            if method == "Input.dispatchKeyEvent" and params.get("key") == "Backspace":
                self.text = "0"

    cdp = ZeroOnClear()
    cdp.text = "12000000"
    monkeypatch.setattr(demo.time, "sleep", lambda _: None)
    demo._fill(cdp, "-15000000", target="纬度 microdegrees")
    assert cdp.text == "-15000000"
    assert len([p for _, p in cdp.calls if p.get("commands") == ["selectAll"]]) == 2
    assert [p["text"] for method, p in cdp.calls if method == "Input.insertText"] == ["-15000000"]


def test_fill_replaces_controlled_number_that_normalizes_empty_to_minimum(monkeypatch):
    class MinimumOnClear(InputRecorder):
        def evaluate(self, expression):
            if "el?.type === 'number'" in expression:
                return True
            return self.text

        def call(self, method, params):
            if method == "Input.insertText" and getattr(self, "selected_all", False):
                self.text = ""
                self.selected_all = False
            super().call(method, params)
            if method == "Input.dispatchKeyEvent" and params.get("key") == "Backspace":
                self.text = "2"

    cdp = MinimumOnClear()
    cdp.text = "9"
    monkeypatch.setattr(demo.time, "sleep", lambda _: None)
    demo._fill(cdp, "7", target="阈值 T")
    assert cdp.text == "7"
    assert len([p for _, p in cdp.calls if p.get("commands") == ["selectAll"]]) == 1
    assert not any(p.get("key") == "Backspace" for _, p in cdp.calls)


def test_fill_enters_controlled_number_as_one_complete_value(monkeypatch):
    class ClampedNumber(InputRecorder):
        def evaluate(self, expression):
            if "el?.type === 'number'" in expression:
                return True
            return self.text

        def call(self, method, params):
            if method == "Input.insertText" and getattr(self, "selected_all", False):
                self.text = ""
                self.selected_all = False
            super().call(method, params)
            if method == "Input.insertText" and self.text == "1":
                self.text = "2"

    cdp = ClampedNumber()
    cdp.text = "9"
    monkeypatch.setattr(demo.time, "sleep", lambda _: None)
    demo._fill(cdp, "12", target="阈值 T")
    assert cdp.text == "12"
    assert [p["text"] for method, p in cdp.calls if method == "Input.insertText"] == ["12"]


def test_click_resolver_includes_interactive_table_rows():
    script = demo._resolve_script('参数区间表中显示“0 · 1 / 6”的行', 'click')
    assert "if (actionType === 'click') selector += ',tr'" in script


def test_pointer_paths_end_at_target_and_vary_without_large_detours():
    start, end = (100, 100), (600, 400)
    paths = [list(demo._pointer_path(start, end, 0.8)) for _ in range(4)]
    assert len({tuple(path) for path in paths}) > 1
    for path in paths:
        assert path[-1] == end
        assert all(70 <= x <= 630 and 70 <= y <= 430 for x,y in path)
        assert all(math.dist(a,b) < 60 for a,b in zip([start]+path,path))


def test_failed_browser_action_retains_log_and_closes_connection(tmp_path,monkeypatch):
    class FailingBrowser:
        closed = False
        def evaluate(self, _):
            return {"x": 50, "y": 50, "width": 100, "height": 30, "name": "名称"}
        def close(self):
            self.closed = True
    cdp = FailingBrowser()
    monkeypatch.setattr(demo,"CDP",lambda _:cdp)
    monkeypatch.setattr(demo,"_wait_for_page",lambda *_args,**_kwargs:None)
    monkeypatch.setattr(demo,"viewport_rect",lambda _:"40,100,1280,720")
    monkeypatch.setattr(demo,"_pause",lambda *_:None)
    monkeypatch.setattr(demo,"_move",lambda *_:None)
    monkeypatch.setattr(demo,"_click",lambda *_:None)
    def failed(*_args,**_kwargs):
        raise RuntimeError("输入失败")
    monkeypatch.setattr(demo,"_fill",failed)
    log=tmp_path/"actions.json"
    with pytest.raises(RuntimeError,match="输入失败"):
        demo.run(1234,{"actions":[{"type":"fill","target":"名称","value":"测试"}]},"A",log)
    assert cdp.closed
    assert json.loads(log.read_text())[-1] == {"status":"failed","error":"输入失败",
        "action":{"type":"fill","target":"名称","value":"测试"}}


@pytest.mark.parametrize("target", ["选择文件", "导入 JSON"])
def test_preflight_converts_old_file_fill_without_touching_page(target, monkeypatch):
    monkeypatch.setattr(demo, "_native_picker_available", lambda: True)
    class Page:
        def evaluate(self, script):
            if "const inputs = [...document.querySelectorAll" in script:
                return {"x": 40, "y": 50, "width": 100, "height": 30, "fileIndex": 0}
            return None
    old = {"actions": [{"type": "fill", "target": target, "value": '{"cues":[]}'}]}
    ready = demo.preflight(Page(), old)
    assert ready["actions"][0]["type"] == "upload"
    assert old["actions"][0]["type"] == "fill"


def test_preflight_rejects_unresolvable_first_step_before_capture():
    class Page:
        def evaluate(self, _):
            return None
    with pytest.raises(RuntimeError, match="录制未开始"):
        demo.preflight(Page(), {"actions": [{"type": "click", "target": "不存在的按钮"}]})


def test_preflight_rejects_empty_plan_before_capture():
    with pytest.raises(RuntimeError, match="没有可执行动作"):
        demo.preflight(None, {"actions": []})


def test_preflight_rejects_untrusted_picker_before_capture(monkeypatch):
    monkeypatch.setattr(demo.sys, "platform", "darwin")
    monkeypatch.setattr(demo, "_native_picker_available", lambda: False)
    class Page:
        def evaluate(self, _):
            return {"x": 40, "y": 50, "width": 100, "height": 30, "fileIndex": 0}
    with pytest.raises(RuntimeError, match="辅助功能.*录制未开始"):
        demo.preflight(Page(), {"actions": [{"type": "upload", "target": "选择文件", "value": "{}"}]})


def test_preflight_defers_file_control_until_after_navigation(monkeypatch):
    resolved = []
    def locate(_cdp, action):
        resolved.append(action["target"])
        return action, {"x": 40, "y": 50} if action["type"] == "click" else None
    monkeypatch.setattr(demo, "_resolved_action", locate)
    monkeypatch.setattr(demo, "_native_picker_available", lambda: True)
    plan = {"actions": [
        {"type": "click", "target": "扫描片段接缝"},
        {"type": "upload", "target": "选择前段 JSON", "value": '{"readings":[1],"queries":[]}'},
    ]}
    assert demo.preflight(None, plan)["actions"] == plan["actions"]
    assert resolved == ["扫描片段接缝"]


def test_native_go_to_folder_posts_physical_modifier_sequence(monkeypatch):
    events = []
    monkeypatch.setattr(demo, "_native_key_event", lambda code, down, **options:
                        events.append((code, down, options["flags"])))
    monkeypatch.setattr(demo.time, "sleep", lambda _value: None)
    demo._native_go_to_folder()
    assert events == [(56, True, 0x20000), (55, True, 0x120000),
                      (5, True, 0x120000), (5, False, 0x120000),
                      (55, False, 0x20000), (56, False, 0)]


def test_native_paste_path_copies_then_uses_command_v(tmp_path, monkeypatch):
    events = []
    copied = []
    monkeypatch.setattr(demo.subprocess, "run", lambda argv, **options:
                        copied.append((argv, options)))
    monkeypatch.setattr(demo, "_native_key_event", lambda code, down, **options:
                        events.append((code, down, options["flags"])))
    monkeypatch.setattr(demo.time, "sleep", lambda _value: None)
    path = tmp_path / "captions.json"
    demo._native_paste_path(path)
    assert copied[0][0] == ["pbcopy"]
    assert copied[0][1]["input"] == str(path.resolve())
    assert events == [(55, True, 0x100000), (9, True, 0x100000),
                      (9, False, 0x100000), (55, False, 0)]


def test_native_picker_enters_folder_then_uses_mouse_to_select_file(tmp_path, monkeypatch):
    calls = []
    pauses = []
    monkeypatch.setattr(demo, "_pause", lambda low, high, *_args: pauses.append((low, high)))
    monkeypatch.setattr(demo, "_assert_picker_foreground", lambda *_args: None)
    monkeypatch.setattr(demo, "_native_picker_available", lambda: True)
    monkeypatch.setattr(demo, "_native_go_to_folder", lambda: calls.append("go"))
    monkeypatch.setattr(demo, "_native_paste_path", lambda path: calls.append(("paste", path)))
    monkeypatch.setattr(demo, "_native_key", lambda code: calls.append(("key", code)))
    controls = iter([None, None, ((300, 140), (920, 690))])
    monkeypatch.setattr(demo.native_picker, "picker_controls", lambda _path, *_args: next(controls))
    monkeypatch.setattr(demo.native_picker, "open_button_ready", lambda *_args: True)
    monkeypatch.setattr(demo, "_native_mouse_click", lambda *point, **_kwargs: calls.append(("click", point)))
    monkeypatch.setattr(demo, "_picker_selected_name", lambda *_args: "sample.json")
    demo._choose_native_file(None, tmp_path / "sample.json", 0)
    assert calls == ["go", ("paste", tmp_path), ("key", 36),
                     ("click", (300, 140)), ("click", (920, 690))]
    # Navigation remains visible without the former pair of 1.2–1.7 s stalls.
    assert pauses[1][1] < 0.7
    assert pauses[2][1] < 0.7
    assert pauses[1] != pauses[2]


def test_native_picker_uses_visible_file_without_reopening_folder(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(demo, "_pause", lambda *_args: None)
    monkeypatch.setattr(demo, "_assert_picker_foreground", lambda *_args: None)
    monkeypatch.setattr(demo, "_native_picker_available", lambda: True)
    monkeypatch.setattr(demo, "_native_go_to_folder", lambda: calls.append("go"))
    monkeypatch.setattr(demo, "_native_paste_path", lambda _path: calls.append("paste"))
    monkeypatch.setattr(demo, "_native_key", lambda _code: calls.append("key"))
    monkeypatch.setattr(demo.native_picker, "picker_controls",
                        lambda _path, _pid: ((300, 140), (920, 690)))
    monkeypatch.setattr(demo.native_picker, "open_button_ready", lambda *_args: True)
    monkeypatch.setattr(demo, "_native_mouse_click", lambda *point, **_kwargs: calls.append(("click", point)))
    monkeypatch.setattr(demo, "_picker_selected_name", lambda *_args: "sample.json")
    demo._choose_native_file(None, tmp_path / "sample.json", 0, browser_pid=123)
    assert calls == [("click", (300, 140)), ("click", (920, 690))]


def test_password_warning_is_clicked_away_before_demo_continues(monkeypatch):
    clicks = []
    points = iter([None, (640, 415)])
    now = [0.0]
    monkeypatch.setattr(demo, "_native_picker_available", lambda: True)
    monkeypatch.setattr(demo.native_picker, "button_center", lambda *_args: next(points))
    monkeypatch.setattr(demo, "_assert_picker_foreground", lambda *_args: None)
    monkeypatch.setattr(demo, "_native_mouse_click",
                        lambda *point, **options: clicks.append((point, options["timing_prefix"])))
    monkeypatch.setattr(demo, "_pause", lambda *_args: None)
    monkeypatch.setattr(demo.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(demo.time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    assert demo._dismiss_login_browser_prompt(browser_pid=123, deadline=2) is True
    assert clicks == [((640, 415), "browser_prompt")]


def test_native_picker_refuses_wrong_file_even_after_mouse_click(tmp_path, monkeypatch):
    monkeypatch.setattr(demo, "_pause", lambda *_args: None)
    monkeypatch.setattr(demo, "_assert_picker_foreground", lambda *_args: None)
    monkeypatch.setattr(demo, "_native_picker_available", lambda: True)
    monkeypatch.setattr(demo, "_native_go_to_folder", lambda: None)
    monkeypatch.setattr(demo, "_native_paste_path", lambda _path: None)
    monkeypatch.setattr(demo, "_native_key", lambda _code: None)
    monkeypatch.setattr(demo.native_picker, "picker_controls", lambda _path, *_args: ((100, 100), (200, 200)))
    monkeypatch.setattr(demo.native_picker, "open_button_ready", lambda *_args: True)
    monkeypatch.setattr(demo, "_native_mouse_click", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(demo, "_picker_selected_name", lambda *_args: "other.json")
    now = [0.0]
    monkeypatch.setattr(demo.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(demo.time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    with pytest.raises(RuntimeError, match="没有打开 sample.json"):
        demo._choose_native_file(None, tmp_path / "sample.json", 0)


def test_upload_filename_is_safe_and_derived_from_json():
    assert demo._upload_name({"value": '{"cues":[]}'}, 0) == "cues-1.json"
    assert demo._upload_name({"value": "plain text"}, 1) == "source-2.txt"
    with pytest.raises(RuntimeError, match="文件名无效"):
        demo._upload_name({"value": "{}", "file_name": "../outside.json"}, 0)


@pytest.mark.parametrize("hidden,target", [(False, "选择文件"), (True, "导入 JSON")])
def test_chrome_upload_selects_real_local_file_and_fires_change(tmp_path, monkeypatch, hidden, target):
    monkeypatch.setattr(demo, "_native_picker_available", lambda: True)
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not chrome.is_file():
        pytest.skip("Chrome is needed for the CDP file chooser test")
    html = ("<input id='file' type='file' accept='.json' "
            + ("style='display:none'" if hidden else "") + ">"
            + ("<button onclick=\"document.getElementById('file').click()\">导入 JSON</button>" if hidden else "")
            + "<output id='result'></output>"
            + "<script>document.getElementById('file').onchange=async e=>{"
            + "const file=e.target.files[0]; e.target.value='';"
            + "document.getElementById('result').textContent=await file.text()"
            + "}</script>")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen([
        str(chrome), "--headless=new", "--no-first-run", "--no-default-browser-check",
        f"--remote-debugging-port={port}", f"--user-data-dir={tmp_path / 'profile'}",
        "--window-size=1000,700", "data:text/html," + quote(html),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cdp = None
    try:
        cdp = demo.CDP(port)
        demo._wait_for_page(cdp, settle=False)
        action = {"type": "upload", "target": target, "value": '{"cues":[]}',
                  "file_name": "cues.json"}
        ready = demo.preflight(cdp, {"actions": [action]})
        assert ready["actions"][0]["type"] == "upload"
        resolved = cdp.evaluate(demo._upload_target_script(target))
        assert resolved and resolved["fileIndex"] == 0
        monkeypatch.setattr(demo, "_click", lambda *_args: pytest.fail("must not leave native chooser open"))
        uploaded = demo._upload(cdp, action, 0, tmp_path / "cues.json", resolved["x"], resolved["y"],
                                native_picker=False)
        assert uploaded == str(tmp_path / "cues.json")
        until = time.monotonic() + 3
        while time.monotonic() < until:
            if cdp.evaluate("document.getElementById('result').textContent") == '{"cues":[]}':
                break
            time.sleep(0.05)
        else:
            pytest.fail("The page did not receive the selected JSON file")
    finally:
        if cdp:
            cdp.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_upload_fixture_decodes_png_data_url(tmp_path):
    import base64

    payload = b"\x89PNG\r\n\x1a\nfixture"
    path = tmp_path / "fixture.png"
    demo._write_upload_fixture(
        path, "data:image/png;base64," + base64.b64encode(payload).decode("ascii")
    )
    assert path.read_bytes() == payload


def test_upload_fixture_decodes_wav_data_url(tmp_path):
    import base64

    payload = b"RIFF\x04\x00\x00\x00WAVE"
    path = tmp_path / "fixture.wav"
    demo._write_upload_fixture(
        path, "data:audio/wav;base64," + base64.b64encode(payload).decode("ascii")
    )
    assert path.read_bytes() == payload


def test_upload_fixture_preserves_pcap_bytes_and_rejects_invalid_header(tmp_path):
    import base64
    payload = b"\xd4\xc3\xb2\xa1" + bytes(range(20)) + b"\x80\xff"
    path = tmp_path / "capture.pcap"
    demo._write_upload_fixture(
        path, "data:application/vnd.tcpdump.pcap;base64," + base64.b64encode(payload).decode("ascii")
    )
    assert path.read_bytes() == payload
    with pytest.raises(RuntimeError, match="PCAP 演示文件头无效"):
        demo._write_upload_fixture(
            path, "data:application/vnd.tcpdump.pcap;base64," + base64.b64encode(b"invalid").decode("ascii")
        )


def test_drag_moves_pressed_pointer_across_canvas(monkeypatch):
    events = []

    class CanvasRecorder:
        def call(self, method, params):
            events.append((method, params))

    monkeypatch.setattr(demo.time, "sleep", lambda _: None)
    demo._drag(
        CanvasRecorder(),
        {"x": 60, "y": 70, "width": 100, "height": 100},
        "0.1,0.2,0.8,0.9",
    )
    assert events[0][1]["x"] == 20
    assert events[0][1]["y"] == 40
    assert events[1][1]["type"] == "mousePressed"
    assert sum(item[1].get("buttons") == 1 for item in events) == 8
    assert events[-1][1]["type"] == "mouseReleased"
    assert events[-1][1]["x"] == 90
    assert events[-1][1]["y"] == 110


def test_upload_locator_prefers_visible_label_over_offscreen_file_input(tmp_path):
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not chrome.is_file():
        pytest.skip("Chrome is needed for the file-control locator test")
    html = ("<meta charset='utf-8'>"
            "<label class='file' style='display:inline-block;position:relative;padding:8px'>导入 JSON 文件"
            "<input type='file' style='position:absolute;left:-9999px'></label>")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen([
        str(chrome), "--headless=new", "--no-first-run", "--no-default-browser-check",
        f"--remote-debugging-port={port}", f"--user-data-dir={tmp_path / 'profile'}",
        "--window-size=1000,700", "data:text/html," + quote(html),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cdp = None
    try:
        cdp = demo.CDP(port)
        demo._wait_for_page(cdp, settle=False)
        resolved = cdp.evaluate(demo._upload_target_script("导入 JSON 文件"))
        assert resolved and resolved["fileIndex"] == 0
        assert resolved["tag"] == "LABEL"
        assert resolved["x"] > 0
    finally:
        if cdp:
            cdp.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_upload_resolution_uses_left_right_card_for_identical_buttons(tmp_path):
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not chrome.is_file():
        pytest.skip("Chrome is needed for the file-control locator test")
    html = ("<meta charset='utf-8'>"
            "<section class='slot'><h3>前段（left · 提供末尾后缀）</h3>"
            "<input type='file' style='display:none'><button>选择 JSON 文件</button></section>"
            "<section class='slot'><h3>后段（right · 提供开头前缀）</h3>"
            "<input type='file' style='display:none'><button>选择 JSON 文件</button></section>")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen([
        str(chrome), "--headless=new", "--no-first-run", "--no-default-browser-check",
        f"--remote-debugging-port={port}", f"--user-data-dir={tmp_path / 'profile'}",
        "--window-size=1000,700", "data:text/html," + quote(html),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cdp = None
    try:
        cdp = demo.CDP(port)
        demo._wait_for_page(cdp, settle=False)
        right = cdp.evaluate(demo._upload_target_script(
            "后段（right · 提供开头前缀）区域的“选择 JSON 文件”"))
        left = cdp.evaluate(demo._upload_target_script(
            "前段（left · 提供末尾后缀）区域的“选择 JSON 文件”"))
        assert right and right["fileIndex"] == 1
        assert left and left["fileIndex"] == 0
    finally:
        if cdp:
            cdp.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_upload_resolution_pairs_sibling_buttons_with_file_inputs(tmp_path):
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not chrome.is_file():
        pytest.skip("Chrome is needed for the file-control locator test")
    html = ("<meta charset='utf-8'><div>"
            "<button>选择图片</button><button>选择目录</button>"
            "<input type='file' style='display:none'>"
            "<input type='file' style='display:none' webkitdirectory></div>")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen([
        str(chrome), "--headless=new", "--no-first-run", "--no-default-browser-check",
        f"--remote-debugging-port={port}", f"--user-data-dir={tmp_path / 'profile'}",
        "--window-size=1000,700", "data:text/html," + quote(html),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cdp = None
    try:
        cdp = demo.CDP(port)
        demo._wait_for_page(cdp, settle=False)
        image = cdp.evaluate(demo._upload_target_script("选择图片"))
        directory = cdp.evaluate(demo._upload_target_script("选择目录"))
        assert image and image["fileIndex"] == 0
        assert directory and directory["fileIndex"] == 1
    finally:
        if cdp:
            cdp.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_rapid_followup_clicks_during_short_enabled_window(tmp_path):
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not chrome.is_file():
        pytest.skip("Chrome is needed for the rapid-followup test")
    html = ("<meta charset='utf-8'><button id='start'>切换</button>"
            "<button id='undo' disabled>撤销</button>"
            "<script>window.undoClicks=0;"
            "document.querySelector('#undo').onclick=()=>window.undoClicks++;"
            "document.querySelector('#start').onclick=()=>{"
            "const undo=document.querySelector('#undo');undo.disabled=false;"
            "setTimeout(()=>undo.disabled=true,80)};</script>")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen([
        str(chrome), "--headless=new", "--no-first-run", "--no-default-browser-check",
        f"--remote-debugging-port={port}", f"--user-data-dir={tmp_path / 'profile'}",
        "--window-size=1000,700", "data:text/html," + quote(html),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cdp = None
    try:
        cdp = demo.CDP(port)
        demo._wait_for_page(cdp, settle=False)
        demo._arm_rapid_followup(cdp, "撤销")
        cdp.evaluate("document.querySelector('#start').click()")
        demo._wait_rapid_followup(cdp, "撤销")
        assert cdp.evaluate("window.undoClicks") == 1
    finally:
        if cdp:
            cdp.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_drag_locator_accepts_image_crop_stage(tmp_path):
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not chrome.is_file():
        pytest.skip("Chrome is needed for the drag locator test")
    html = ("<meta charset='utf-8'><div class='stage' "
            "style='width:500px;height:350px;background:#ddd'>"
            "<img alt='original' style='width:500px;height:350px' "
            "src='data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs='></div>")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen([
        str(chrome), "--headless=new", "--no-first-run", "--no-default-browser-check",
        f"--remote-debugging-port={port}", f"--user-data-dir={tmp_path / 'profile'}",
        "--window-size=1000,700", "data:text/html," + quote(html),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cdp = None
    try:
        cdp = demo.CDP(port)
        demo._wait_for_page(cdp, settle=False)
        resolved = cdp.evaluate(demo._resolve_script("画布", "drag"))
        assert resolved and resolved["width"] >= 500
    finally:
        if cdp:
            cdp.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_native_motion_does_not_wait_for_browser_on_each_frame(monkeypatch):
    cdp = InputRecorder()
    warps=[]
    monkeypatch.setattr(demo,"_warp_pointer",lambda *args,**kwargs:warps.append((args,kwargs)))
    assert demo._move(cdp,100,200,(40,100,1280,720),0.6)==(100,200)
    assert warps==[((140,300,0.6),{"bounds":(44,104,1316,816)})]
    assert cdp.calls==[("Input.dispatchMouseEvent",{"type":"mouseMoved","x":100,"y":200})]
    assert len(list(demo._pointer_path((0,0),(100,200),0.6)))==36


@pytest.mark.parametrize("resolved,expected",[({"x":320,"y":260},(320,260)),(None,(8,300))])
def test_initial_pointer_is_positioned_inside_page_without_clicking(monkeypatch,resolved,expected):
    cdp=InputRecorder()
    cdp.evaluate=lambda _:resolved
    warps=[]
    monkeypatch.setattr(demo,"_warp_pointer",lambda *args,**kwargs:warps.append((args,kwargs)))
    monkeypatch.setattr(demo,"_pause",lambda *_:None)
    result=demo.place_initial_pointer(cdp,"40,100,800,600")
    assert result==expected
    assert warps[0][0]==(40+expected[0],100+expected[1],0)
    assert cdp.calls==[("Input.dispatchMouseEvent",{"type":"mouseMoved","x":expected[0],"y":expected[1]})]


@pytest.mark.parametrize("obstacle",["control","text","cursor"])
def test_initial_pointer_hit_testing_skips_controls_and_visible_text(obstacle):
    if not shutil.which("node"):
        pytest.skip("Node is required for the isolated DOM-script test")
    setup="""
      const kind=KIND;
      globalThis.getComputedStyle=el=>({cursor:el.blocked && kind==='cursor'?'pointer':'auto'});
      globalThis.document={elementFromPoint:(x,y)=>({blocked:x<200,
        closest:()=>x<200 && kind==='control'?{}:null,
        childNodes:x<200 && kind==='text'?[{nodeType:3,textContent:'Visible text'}]:[]}),
        createRange:()=>({selectNodeContents(){},getClientRects:()=>[{left:0,right:200,top:0,bottom:200}]})};
    """.replace("KIND",json.dumps(obstacle))
    script=setup+"\nconsole.log(JSON.stringify("+demo._initial_pointer_script([{"x":100,"y":100},{"x":300,"y":300}])+"));"
    result=json.loads(subprocess.check_output(["node","--input-type=module","-e",script],text=True))
    assert result=={"x":300,"y":300}


def test_pointer_movement_keeps_bounds_and_respects_time_limit(monkeypatch):
    cdp=InputRecorder()
    warps=[]
    monkeypatch.setattr(demo,"_warp_pointer",lambda *args,**kwargs:warps.append(args))
    monkeypatch.setattr(demo.time,"monotonic",lambda:10)
    assert demo._move(cdp,-20,900,(0,0,800,600),0.3,deadline=11)==(4,596)
    with pytest.raises(RuntimeError,match="上限"):
        demo._move(cdp,100,100,(0,0,800,600),0.3,deadline=10.1)
    assert len(warps)==1


def test_extra_pointer_adjustments_never_add_clicks(monkeypatch):
    cdp=InputRecorder()
    warps=[]
    monkeypatch.setattr(demo,"_warp_pointer",lambda *args,**kwargs:warps.append(args))
    monkeypatch.setattr(demo.time,"sleep",lambda _:None)
    monkeypatch.setattr(demo.random,"random",lambda:0.1)
    monkeypatch.setattr(demo.random,"uniform",lambda low,high:(low+high)/2)
    demo._approach(cdp,600,400,(0,0,800,600),(20,20),None)
    demo._reading_pause(cdp,600,400,(0,0,800,600),None)
    assert len(warps)==4  # an approach adjustment plus two small reading movements
    assert all(params["type"]=="mouseMoved" for _,params in cdp.calls)


def test_final_reading_hold_does_not_call_unresponsive_browser(monkeypatch):
    class UnresponsiveBrowser:
        def call(self, *_args, **_kwargs):
            raise TimeoutError("timed out")

    monkeypatch.setattr(demo.time, "sleep", lambda _: None)
    timing = {"reading": 0.6, "reading_move": True,
              "reading_move_seconds": 0.3, "reading_second_move": True}
    assert demo._reading_pause(
        UnresponsiveBrowser(), 100, 80, (0, 0, 800, 600), None,
        last=True, timing=timing) == (100, 80)


@pytest.mark.parametrize("offscreen,covered",[(False,False),(True,False),(False,True)])
def test_resolver_scrolls_only_when_needed_and_rejects_obscured_targets(offscreen,covered):
    if not shutil.which("node"):
        pytest.skip("Node is required for the isolated DOM-script test")
    # A tiny DOM mock, not a browser: verify the actual generated resolver.
    setup="""
      const scrolls=[]; let top=TOP;
      globalThis.innerWidth=800; globalThis.innerHeight=600;
      globalThis.getComputedStyle=()=>({display:'block',visibility:'visible'});
      globalThis.requestAnimationFrame=callback=>callback(0);
      const el={id:'',innerText:'保存',textContent:'保存',tagName:'BUTTON',
        getAttribute:()=>null,closest:()=>null,matches:()=>true,contains:()=>false,
        getBoundingClientRect:()=>({left:100,right:200,top,bottom:top+30,width:100,height:30}),
        scrollIntoView:options=>{scrolls.push(options);top=400;}};
      globalThis.document={querySelectorAll:()=>[el],elementFromPoint:()=>HIT};
    """.replace("TOP","800" if offscreen else "200").replace("HIT","{}" if covered else "el")
    script=setup+"\nconst result=await "+demo._resolve_script("保存","click")+"; console.log(JSON.stringify({result,scrolls}));"
    result=json.loads(subprocess.check_output(["node","--input-type=module","-e",script],text=True))
    assert len(result["scrolls"])==int(offscreen)
    if offscreen:
        assert result["scrolls"][0]["block"]=="nearest"
    assert (result["result"] is None)==covered


def test_resolver_finds_numbered_repeated_input_by_quoted_label():
    if not shutil.which("node"):
        pytest.skip("Node is required for the isolated DOM-script test")
    setup="""
      globalThis.innerWidth=800; globalThis.innerHeight=600;
      globalThis.getComputedStyle=()=>({display:'block',visibility:'visible'});
      const inputs=[0,1,2].map(index=>({
        id:'',innerText:'',textContent:'',tagName:'INPUT',
        getAttribute:()=>null,closest:()=>({innerText:'固定起点'}),
        matches:selector=>selector.startsWith('input'),contains:()=>false,
        getBoundingClientRect:()=>({left:100,top:100+index*50,right:220,bottom:130+index*50,width:120,height:30})
      }));
      globalThis.document={querySelectorAll:()=>inputs,
        elementFromPoint:(_x,y)=>inputs[Math.round((y-115)/50)]};
    """
    script=(setup+"\nconst result=await "+demo._resolve_script("字幕 #1 行的“固定起点”","fill")
        +"; console.log(JSON.stringify(result));")
    result=json.loads(subprocess.check_output(["node","--input-type=module","-e",script],text=True))
    assert result["y"]==165
    assert result["name"]=="固定起点"


def test_resolver_finds_unlabelled_textarea_by_section_heading():
    if not shutil.which("node"):
        pytest.skip("Node is required for the isolated DOM-script test")
    setup="""
      globalThis.innerWidth=800; globalThis.innerHeight=600;
      globalThis.getComputedStyle=()=>({display:'block',visibility:'visible'});
      const section={querySelector:()=>({textContent:'输入'})};
      const el={id:'',innerText:'',textContent:'',tagName:'TEXTAREA',disabled:false,
        previousElementSibling:null,querySelector:()=>null,
        getAttribute:()=>null,closest:selector=>selector==='section'?section:null,
        matches:selector=>selector.includes('textarea'),contains:()=>false,
        getBoundingClientRect:()=>({left:100,top:100,right:300,bottom:220,width:200,height:120})};
      globalThis.document={querySelectorAll:()=>[el],querySelector:()=>null,elementFromPoint:()=>el};
    """
    script=setup+"\nconst result=await "+demo._resolve_script("输入","fill")+"; console.log(JSON.stringify(result));"
    result=json.loads(subprocess.check_output(["node","--input-type=module","-e",script],text=True))
    assert result["tag"]=="TEXTAREA"
    assert "输入" in result["name"]


def test_resolver_finds_svg_vertex_by_nested_title():
    if not shutil.which("node"):
        pytest.skip("Node is required for the isolated DOM-script test")
    setup="""
      globalThis.innerWidth=800; globalThis.innerHeight=600;
      globalThis.getComputedStyle=()=>({display:'inline',visibility:'visible'});
      const el={id:'',innerText:'',textContent:'',tagName:'circle',disabled:false,
        previousElementSibling:null,querySelector:selector=>selector==='title'?{textContent:'吸附顶点 (0, 1)'}:null,
        getAttribute:()=>null,closest:()=>null,matches:()=>true,contains:()=>false,
        getBoundingClientRect:()=>({left:100,top:100,right:120,bottom:120,width:20,height:20})};
      globalThis.document={querySelectorAll:()=>[el],querySelector:()=>null,elementFromPoint:()=>el};
    """
    script=setup+"\nconst result=await "+demo._resolve_script("吸附顶点 (0, 1)","click")+"; console.log(JSON.stringify(result));"
    result=json.loads(subprocess.check_output(["node","--input-type=module","-e",script],text=True))
    assert result["tag"]=="circle"
    assert result["name"]=="吸附顶点 (0, 1)"


def test_resolver_can_click_native_details_summary():
    if not shutil.which("node"):
        pytest.skip("Node is required for the isolated DOM-script test")
    setup="""
      globalThis.innerWidth=800; globalThis.innerHeight=600;
      globalThis.getComputedStyle=()=>({display:'block',visibility:'visible'});
      const el={id:'',innerText:'入选集合（2 项，与下载 JSON 一致）',textContent:'入选集合（2 项，与下载 JSON 一致）',
        tagName:'SUMMARY',disabled:false,previousElementSibling:null,
        getAttribute:()=>null,closest:()=>null,matches:selector=>selector.includes('summary'),contains:()=>false,
        getBoundingClientRect:()=>({left:100,top:200,right:420,bottom:232,width:320,height:32})};
      globalThis.document={querySelectorAll:selector=>selector.includes('summary')?[el]:[],
        querySelector:()=>null,elementFromPoint:()=>el};
    """
    script=setup+"\nconst result=await "+demo._resolve_script("入选集合（2 项，与下载 JSON 一致）","click")+"; console.log(JSON.stringify(result));"
    result=json.loads(subprocess.check_output(["node","--input-type=module","-e",script],text=True))
    assert result["tag"]=="SUMMARY"


@pytest.mark.parametrize("target,label_mode", [
    ("选择避难点（需求点）", "sibling"),
    ("二、基线核算中的“选择需求点：”", "parent"),
])
def test_select_resolver_uses_adjacent_label_and_quoted_alias(target, label_mode):
    if not shutil.which("node"):
        pytest.skip("Node is required for the isolated DOM-script test")
    setup="""
      globalThis.innerWidth=800; globalThis.innerHeight=600;
      globalThis.getComputedStyle=()=>({display:'block',visibility:'visible'});
      const label={innerText:LABEL,matches:selector=>selector==='label'};
      const el={id:'',innerText:'避难点-甲 避难点-乙',textContent:'避难点-甲 避难点-乙',
        tagName:'SELECT',disabled:false,previousElementSibling:MODE==='sibling'?label:null,
        getAttribute:()=>null,matches:selector=>selector.includes('select'),contains:()=>false,
        closest:selector=>MODE==='parent'&&selector==='label'?label:null,
        getBoundingClientRect:()=>({left:100,top:200,right:300,bottom:232,width:200,height:32})};
      globalThis.document={querySelectorAll:()=>[el],querySelector:()=>null,elementFromPoint:()=>el};
    """.replace("MODE",json.dumps(label_mode)).replace(
        "LABEL",json.dumps("选择避难点（需求点）" if label_mode=="sibling" else "选择需求点："))
    script=setup+"\nconst result=await "+demo._resolve_script(target,"select")+"; console.log(JSON.stringify(result));"
    result=json.loads(subprocess.check_output(["node","--input-type=module","-e",script],text=True))
    assert result["tag"]=="SELECT"


@pytest.mark.parametrize("target,expected_y", [
    ('current cut "shared" row "Disable"', 115),
    ('pipe list "external" row "Disable" checkbox', 215),
])
def test_resolver_uses_quoted_row_identity_for_table_controls(target, expected_y):
    if not shutil.which("node"):
        pytest.skip("Node is required for the isolated DOM-script test")
    setup="""
      globalThis.innerWidth=800; globalThis.innerHeight=600;
      globalThis.getComputedStyle=()=>({display:'block',visibility:'visible'});
      const row=(text,top)=>({innerText:text});
      const control=(tag,text,top,type='')=>({id:'',innerText:text,textContent:text,tagName:tag,
        disabled:false,previousElementSibling:null,parentElement:null,
        getAttribute:name=>name==='type'?type:null,
        matches:selector=>selector.includes(tag.toLowerCase()) || (type==='checkbox'&&selector.includes('checkbox')),
        contains:()=>false,closest:selector=>selector==='tr'?row(text.includes('Disable')?'shared Disable':'external',top):null,
        getBoundingClientRect:()=>({left:100,top,right:220,bottom:top+30,width:120,height:30})});
      const button=control('BUTTON','Disable',100);
      const checkbox=control('INPUT','',200,'checkbox');
      globalThis.document={querySelectorAll:()=>[button,checkbox],querySelector:()=>null,
        elementFromPoint:(_x,y)=>y<180?button:checkbox};
    """
    script=setup+"\nconst result=await "+demo._resolve_script(target,"click")+"; console.log(JSON.stringify(result));"
    result=json.loads(subprocess.check_output(["node","--input-type=module","-e",script],text=True))
    assert result["y"]==expected_y


@pytest.mark.parametrize("target,expected_y", [
    ('主输入区域的“试听”', 115),
    ('备输入区域的“试听”', 215),
])
def test_resolver_uses_channel_side_for_repeated_button_names(target, expected_y):
    if not shutil.which("node"):
        pytest.skip("Node is required for the isolated DOM-script test")
    setup="""
      globalThis.innerWidth=800; globalThis.innerHeight=600;
      globalThis.getComputedStyle=()=>({display:'block',visibility:'visible'});
      const card=(side,text)=>({innerText:text,getAttribute:name=>name==='data-testid'?`card-${side}`:null});
      const control=(side,top)=>({id:'',innerText:'试听',textContent:'试听',tagName:'BUTTON',disabled:false,
        previousElementSibling:null,parentElement:null,
        getAttribute:name=>name==='data-testid'?`audition-${side}`:null,
        matches:selector=>selector.includes('button'),contains:()=>false,
        closest:selector=>selector.includes('card-')||selector.includes('channel-card')?card(side,side==='primary'?'主输入':'备输入'):null,
        getBoundingClientRect:()=>({left:100,top,right:220,bottom:top+30,width:120,height:30})});
      const primary=control('primary',100), backup=control('backup',200);
      globalThis.document={querySelectorAll:()=>[primary,backup],querySelector:()=>null,
        elementFromPoint:(_x,y)=>y<180?primary:backup};
    """
    script=setup+"\nconst result=await "+demo._resolve_script(target,"click")+"; console.log(JSON.stringify(result));"
    result=json.loads(subprocess.check_output(["node","--input-type=module","-e",script],text=True))
    assert result["y"]==expected_y


def test_resolver_uses_ancestor_aria_label_to_disambiguate_group_buttons():
    if not shutil.which("node"):
        pytest.skip("Node is required for the isolated DOM-script test")
    setup="""
      globalThis.innerWidth=800; globalThis.innerHeight=600;
      globalThis.getComputedStyle=()=>({display:'block',visibility:'visible'});
      const group=id=>({innerText:'普通 必选 排除',getAttribute:name=>name==='aria-label'?`作业 ${id} 的状态`:null});
      const control=(id,top)=>({id:'',innerText:'必选',textContent:'必选',tagName:'BUTTON',disabled:false,
        previousElementSibling:null,parentElement:null,getAttribute:()=>null,
        matches:selector=>selector.includes('button'),contains:()=>false,
        closest:selector=>selector==='[aria-label]'?group(id):null,
        getBoundingClientRect:()=>({left:100,top,right:220,bottom:top+30,width:120,height:30})});
      const a=control('a',100), b=control('b',200);
      globalThis.document={querySelectorAll:()=>[a,b],querySelector:()=>null,
        elementFromPoint:(_x,y)=>y<180?a:b};
    """
    target='aria-label“作业 b 的状态”组内的“必选”'
    script=setup+"\nconst result=await "+demo._resolve_script(target,"click")+"; console.log(JSON.stringify(result));"
    result=json.loads(subprocess.check_output(["node","--input-type=module","-e",script],text=True))
    assert result["y"]==215


def test_resolver_can_choose_any_visible_character_option():
    if not shutil.which("node"):
        pytest.skip("Node is required for the isolated DOM-script test")
    setup="""
      globalThis.innerWidth=800; globalThis.innerHeight=600;
      globalThis.getComputedStyle=()=>({display:'block',visibility:'visible'});
      const group={getAttribute:name=>name==='aria-label'?'字符选项':null,
        querySelectorAll:()=>[first,second]};
      const option=(text,top)=>({id:'',innerText:text,textContent:text,tagName:'BUTTON',disabled:false,
        previousElementSibling:null,getAttribute:()=>null,matches:selector=>selector.includes('button'),
        contains:()=>false,closest:selector=>selector==='[aria-label]'?group:null,
        getBoundingClientRect:()=>({left:100,top,right:180,bottom:top+30,width:80,height:30})});
      const first=option('三',100),second=option('九',160);
      globalThis.document={querySelectorAll:selector=>selector==='[aria-label]'?[group]:[first,second],
        querySelector:()=>null,elementFromPoint:(_x,y)=>y<150?first:second};
    """
    script=setup+"\nconst result=await "+demo._resolve_script(
        "字符选项中任一当前可见的字符按钮","click")+"; console.log(JSON.stringify(result));"
    result=json.loads(subprocess.check_output(["node","--input-type=module","-e",script],text=True))
    assert result["name"].startswith("三")


@pytest.mark.parametrize("reset_after_change", [False, True])
def test_chrome_select_dispatches_change_event_after_native_menu_loses_focus(
        tmp_path, monkeypatch, reset_after_change):
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not chrome.is_file():
        pytest.skip("Chrome is needed for the select interaction test")
    on_change = ("result.textContent=choice.value;choice.selectedIndex=0"
                 if reset_after_change else "result.textContent=choice.value")
    html = ("<label for='choice'>Choose sink:</label>"
            "<select id='choice'><option value=''>Choose</option>"
            "<option value='a'>Alpha</option><option value='b'>Beta</option></select>"
            f"<output id='result'></output><script>choice.onchange=()=>{{{on_change}}}</script>")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen([
        str(chrome), "--headless=new", "--no-first-run", "--no-default-browser-check",
        f"--remote-debugging-port={port}", f"--user-data-dir={tmp_path / 'profile'}",
        "--window-size=1000,700", "data:text/html," + quote(html),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cdp = None
    try:
        cdp = demo.CDP(port)
        demo._wait_for_page(cdp, settle=False)
        resolved = cdp.evaluate("""(() => { const r=document.getElementById('choice').getBoundingClientRect();
          return {x:r.left+r.width/2,y:r.top+r.height/2}; })()""")
        demo._click(cdp, resolved["x"], resolved["y"])
        cdp.evaluate("document.getElementById('choice').focus()")
        monkeypatch.setattr(demo, "_key", lambda *_args, **_kwargs:
                            cdp.evaluate("document.activeElement.blur()"))
        demo._select(cdp, "Choose sink:", "Beta")
        assert cdp.evaluate("document.getElementById('choice').value") == ("" if reset_after_change else "b")
        assert cdp.evaluate("document.getElementById('result').textContent") == "b"
    finally:
        if cdp:
            cdp.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize("value", [
    "第一个可用音频输入设备",
    "首个可用输出设备",
    "First available microphone",
])
def test_select_recognizes_first_available_plan_wording(value):
    assert demo._select_first_available(value)


def test_select_does_not_treat_a_literal_option_as_first_available():
    assert not demo._select_first_available("Built-in Microphone")


def test_consecutive_input_and_click_are_continuous_but_preserve_typed_text(tmp_path,monkeypatch):
    class Browser(InputRecorder):
        closed=False
        def evaluate(self,expression):
            if expression.startswith("document.activeElement"):
                return self.text
            return {"x":150,"y":150,"width":120,"height":32,"name":"名称"}
        def close(self):
            self.closed=True
    cdp=Browser()
    now=[0.0]
    monkeypatch.setattr(demo,"CDP",lambda _:cdp)
    monkeypatch.setattr(demo,"_wait_for_page",lambda *_args,**_kwargs:None)
    monkeypatch.setattr(demo,"viewport_rect",lambda _:"0,0,800,600")
    monkeypatch.setattr(demo.time,"monotonic",lambda:now[0])
    monkeypatch.setattr(demo.time,"sleep",lambda value:now.__setitem__(0,now[0]+value))
    monkeypatch.setattr(demo,"_warp_pointer",lambda x,y,duration,**kwargs:now.__setitem__(0,now[0]+duration))
    monkeypatch.setattr(demo.random,"random",lambda:0.1)
    monkeypatch.setattr(demo.random,"uniform",lambda low,high:(low+high)/2)
    actions=[{"type":"fill","target":"名称","value":"Sample"},{"type":"click","target":"保存"}]
    log=tmp_path/"actions.json"
    demo.run(1234,{"actions":actions},"A",log,deadline=20)
    assert cdp.text=="Sample" and cdp.closed
    assert len([p for _,p in cdp.calls if p.get("type")=="mousePressed"])==2
    assert len([p for _,p in cdp.calls if p.get("type")=="mouseReleased"])==2
    assert len([p for method,p in cdp.calls if method=="Input.insertText"])==6
    assert [e["status"] for e in json.loads(log.read_text())]==["complete","complete"]
    assert 3 < now[0] < 8  # no stack of multiple long pauses at every control


def test_focus_only_changes_after_pointer_arrives_and_clicks_next_control(tmp_path,monkeypatch):
    pointer=[(100,100)]
    events=[]
    class Browser(InputRecorder):
        focused=None
        def evaluate(self,expression):
            if expression.startswith("document.activeElement"):
                return self.text
            x=200 if '"名称"' in expression else 600
            return {"x":x,"y":200,"width":100,"height":30,"name":"控件"}
        def call(self,method,params):
            if params.get("type")=="mousePressed":
                assert pointer[0]==(params["x"],params["y"])
                self.focused=pointer[0]
                events.append("click")
            if method=="Input.insertText":
                assert self.focused==pointer[0]
                events.append("input")
            assert params.get("key")!="Tab"
            super().call(method,params)
        def close(self):
            pass
    cdp=Browser()
    def warp(x,y,*_args,**_kwargs):
        if events:
            assert cdp.focused==(200,200)  # focus stays on input during travel
        pointer[0]=(x,y)
        events.append("move")
    monkeypatch.setattr(demo,"CDP",lambda _:cdp)
    monkeypatch.setattr(demo,"_wait_for_page",lambda *_args,**_kwargs:None)
    monkeypatch.setattr(demo,"viewport_rect",lambda _:"0,0,800,600")
    monkeypatch.setattr(demo,"_warp_pointer",warp)
    monkeypatch.setattr(demo.time,"sleep",lambda _:None)
    monkeypatch.setattr(demo.random,"random",lambda:1)
    monkeypatch.setattr(demo.random,"uniform",lambda low,high:(low+high)/2)
    monkeypatch.setattr(demo,"_recording_cadence",lambda actions:[
        {"profile":1.0,"reading":0.2,"reading_move":False} for _ in actions])
    origins=[]
    approach=demo._approach
    def tracked_approach(cdp,x,y,rect,origin,deadline):
        origins.append(origin)
        return approach(cdp,x,y,rect,origin,deadline)
    monkeypatch.setattr(demo,"_approach",tracked_approach)
    demo.run(1234,{"actions":[{"type":"fill","target":"名称","value":"X"},
        {"type":"click","target":"保存"}]},"A",tmp_path/"actions.json",initial_pointer=(100,100))
    assert events==["move","click","input","move","click"]
    assert cdp.focused==(600,200)
    assert origins==[(100,100),(200,200)]


def test_page_readiness_timeout_is_not_ignored(monkeypatch):
    now=[0.0]
    cdp=InputRecorder()
    monkeypatch.setattr(demo.time,"monotonic",lambda:now[0])
    monkeypatch.setattr(demo.time,"sleep",lambda value:now.__setitem__(0,now[0]+value))
    with pytest.raises(RuntimeError,match="就绪超时"):
        demo._wait_for_page(cdp,deadline=0.5,settle=False)
    assert now[0]==0.5


def test_page_readiness_waits_for_real_app_instead_of_complete_blank(monkeypatch):
    now=[0.0]
    url="http://127.0.0.1:18081/"
    class Page:
        current="about:blank"
        calls=[]
        def evaluate(self, expression):
            return self.current if expression=="location.href" else "complete"
        def call(self, method, params=None):
            self.calls.append(method)
            if method=="Page.navigate":
                self.current=params["url"]
    cdp=Page()
    monkeypatch.setattr(demo.time,"monotonic",lambda:now[0])
    monkeypatch.setattr(demo.time,"sleep",lambda seconds:now.__setitem__(0,now[0]+seconds))
    demo._wait_for_page(cdp,expected_url=url,settle=False)
    assert cdp.calls==["Page.navigate","Page.bringToFront"]
    assert cdp.current==url
    assert now[0]>=1


def test_missing_control_never_clicks_old_fallback_coordinates(tmp_path,monkeypatch):
    class MissingBrowser(InputRecorder):
        def evaluate(self,_):
            return None
        def close(self):
            pass
    cdp=MissingBrowser()
    now=[0.0]
    monkeypatch.setattr(demo,"CDP",lambda _:cdp)
    monkeypatch.setattr(demo,"_wait_for_page",lambda *_args,**_kwargs:None)
    monkeypatch.setattr(demo,"viewport_rect",lambda _:"0,0,800,600")
    monkeypatch.setattr(demo.time,"monotonic",lambda:now[0])
    monkeypatch.setattr(demo.time,"sleep",lambda value:now.__setitem__(0,now[0]+value))
    monkeypatch.setattr(demo,"_click",lambda *_:pytest.fail("must not click stale coordinates"))
    log=tmp_path/"missing.json"
    with pytest.raises(RuntimeError,match="没有找到"):
        demo.run(1234,{"actions":[{"type":"click","target":"保存","x":100,"y":100}]},"A",log,deadline=20)
    assert json.loads(log.read_text())[0]["status"]=="not-found"


def test_missing_second_action_cannot_mark_partial_demo_complete(tmp_path,monkeypatch):
    class Browser(InputRecorder):
        def close(self):
            pass
    cdp=Browser()
    now=[0.0]
    monkeypatch.setattr(demo,"CDP",lambda _:cdp)
    monkeypatch.setattr(demo,"_wait_for_page",lambda *_args,**_kwargs:None)
    monkeypatch.setattr(demo,"viewport_rect",lambda _:"0,0,800,600")
    monkeypatch.setattr(demo,"_resolved_action",lambda _cdp,action:(action,
        {"x":100,"y":100,"width":100,"height":30,"name":"导入"} if action["target"]=="导入" else None))
    monkeypatch.setattr(demo,"_approach",lambda *_args,**_kwargs:None)
    monkeypatch.setattr(demo,"_reading_pause",lambda *_args,**_kwargs:(100,100))
    monkeypatch.setattr(demo.time,"monotonic",lambda:now[0])
    monkeypatch.setattr(demo.time,"sleep",lambda value:now.__setitem__(0,now[0]+value))
    log=tmp_path/"partial.json"
    with pytest.raises(RuntimeError,match="第 2 步没有找到"):
        demo.run(1234,{"actions":[{"type":"click","target":"导入"},
            {"type":"fill","target":"固定起点","value":"1500"}]} ,"A",log,deadline=20)
    assert [event["status"] for event in json.loads(log.read_text())[:2]]==["complete","not-found"]
