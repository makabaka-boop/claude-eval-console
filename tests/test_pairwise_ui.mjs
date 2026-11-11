import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

// Exercise the actual renderer without loading the app's DOM/event handlers.
const source = readFileSync(new URL("../static/pairwise.js", import.meta.url), "utf8");
const page = readFileSync(new URL("../static/pairwise.html", import.meta.url), "utf8");

test("header separates Claude terminals from Codex background work",()=>{
  assert.ok(page.includes('id="parallel-count">Claude 0/6'));
  assert.ok(page.includes('id="codex-count">Codex 后台 0/2'));
  assert.ok(page.includes("Claude A/B 并行上限"));
  assert.ok(source.includes("queued_parallel"));
  assert.ok(source.includes("codex_active"));
});

test("task and export lists expose server-backed filters",()=>{
  for(const id of ["task-filters","task-number","task-status","task-type","task-difficulty",
    "export-filters","export-number","export-task-type","export-difficulty","export-readiness",
    "export-recording","export-solo"]){
    assert.ok(page.includes(`id="${id}"`),id);
  }
  for(const key of ["number","status","task_type","difficulty","readiness","recording","solo"]){
    assert.ok(source.includes(`${key}:`),key);
  }
  assert.ok(page.includes("0023-1")&&page.includes("清空"));
});
const difficultyContext = vm.createContext({
  latest: (p, side) => p.attempts.find(a => a.side === side),
});
vm.runInContext([
  source.match(/^const escape = .*$/m)[0],
  source.slice(source.indexOf("function manualDifficulty("), source.indexOf("function projectCell(")),
  source.slice(source.indexOf("function difficultyReviewHtml("), source.indexOf("function journeyHtml(")),
].join("\n"), difficultyContext);

test("difficulty card separates pending review from the proposed hard label", () => {
  const html=difficultyContext.difficultyReviewHtml({id:"fixture",difficulty:"困难",attempts:[{side:"A",status:"complete"},{side:"B",status:"complete"}],difficulty_assessment:{status:"pending",label:"难度待复评",issues:["尚未完成实际难度复评"]}});
  assert.ok(html.includes("难度待复评")&&html.includes("出题标签：困难"));
  assert.ok(html.includes('data-difficulty-review="fixture"'));
  assert.ok(!html.includes("disabled"));
});

test("difficulty review renders evidence safely and disables duplicate jobs", () => {
  const html=difficultyContext.difficultyReviewHtml({id:"fixture",difficulty:"困难",attempts:[],difficulty_assessment:{status:"running",review:{level:"中等",minimal_solution:"<script>bad</script>",code_evidence:[{path:"A/a.ts",line:1,claim:"<img>"}]}}});
  assert.ok(html.includes("disabled"));
  assert.ok(html.includes("&lt;script&gt;bad&lt;/script&gt;")&&!html.includes("<script>"));
  assert.ok(html.includes("A/a.ts:1")&&html.includes("&lt;img&gt;"));
});

test("review does not require a new Claude attempt and preserves the GSB draft", () => {
  const handler=source.slice(source.indexOf("if(b.dataset.difficultyReview)"),source.indexOf('if(b.id==="back")'));
  assert.ok(handler.includes("saveDraft()")&&handler.includes("/difficulty-review"));
  assert.ok(!handler.includes("/start"));
});

const reviewContext=vm.createContext({
  comparisonHtml:()=>'<section class="comparison-workbench">A/B 对比</section>',
  deliveryOptions:()=>'<option value="5">5 分</option>',
});
vm.runInContext([
  source.match(/^const escape = .*$/m)[0],
  source.slice(source.indexOf("function reviewHtml("),source.indexOf("function difficultyReviewHtml(")),
].join("\n"),reviewContext);

for(const reference_status of ["", "queued", "running", "failed", "complete"]){
  test(`human review ignores archived reference draft: ${reference_status||"empty"}`,()=>{
    const html=reviewContext.reviewHtml({reference_status,reference_gsb:"B 更好",
      reference_reason:"旧参考理由",gsb:"A 更好",reason:"人工核对理由",
      a_delivery_description:"A 已核对",b_delivery_description:"B 已核对"},true);
    assert.ok(html.includes('class="comparison-workbench"'));
    assert.ok(html.includes('id="review-form"'));
    assert.ok(html.includes('<option selected>A 更好</option>'));
    for(const value of ["人工核对理由","A 已核对","B 已核对"]){
      assert.ok(html.includes(value),value);
    }
    for(const value of ["REFERENCE DRAFT","参考 GSB","旧参考理由","retry-reference","use-reference"]){
      assert.ok(!html.includes(value),value);
    }
  });
}

test("human review remains locked before both sides finish",()=>{
  const html=reviewContext.reviewHtml({},false);
  assert.ok(html.includes("等待 A、B 都完成归档后开放"));
  assert.ok(!html.includes('id="review-form"'));
});

const gitChangeContext=vm.createContext({state:{detailGitChanges:new Map()}});
vm.runInContext([
  source.match(/^const escape = .*$/m)[0],
  source.slice(source.indexOf("function gitChangeHtml("),source.indexOf("function evidenceReady(")),
].join("\n"),gitChangeContext);

test("current A/B result renders only git totals",()=>{
  const html=gitChangeContext.gitChangeHtml({artifact_sha:"a".repeat(40),git_changes:{
    available:true,file_count:6,additions:171,deletions:27,binary_files:1,
    files:[{path:"src/<unsafe>.ts",additions:171,deletions:27,binary:false},
      {path:"public/image.png",additions:0,deletions:0,binary:true}],
  }});
  assert.ok(html.includes("6 files changed"));
  assert.ok(html.includes("171 additions")&&html.includes("27 deletions"));
  assert.ok(!html.includes("src/&lt;unsafe&gt;.ts")&&!html.includes("src/<unsafe>.ts"));
  assert.ok(!html.includes("git-change-files")&&!html.includes("binary"));
});

test("git summary has a clear fallback when an archived workspace is unavailable",()=>{
  const html=gitChangeContext.gitChangeHtml({artifact_sha:"a".repeat(40),git_changes:{available:false}});
  assert.ok(html.includes("Git 变更统计暂不可用"));
  assert.equal(gitChangeContext.gitChangeHtml({artifact_sha:""}),"");
});
test("detail git totals are optional and expire when the artifact or base changes",()=>{
  const p={id:"pair",base_sha:"base"},a={id:"attempt",artifact_sha:"artifact"};
  assert.ok(gitChangeContext.detailGitChangeHtml(p,a).includes('data-detail-git-load="pair"'));
  assert.ok(!gitChangeContext.detailGitChangeHtml(p,a).includes("暂不可用"));
  gitChangeContext.state.detailGitChanges.set(gitChangeContext.detailGitChangeKey(p,a),{
    changes:{available:true,file_count:3700,additions:610492,deletions:0}});
  assert.ok(gitChangeContext.detailGitChangeHtml(p,a).includes("3700 files changed"));
  assert.ok(gitChangeContext.detailGitChangeHtml(p,{...a,artifact_sha:"new"}).includes("按需加载"));
  assert.ok(gitChangeContext.detailGitChangeHtml({...p,base_sha:"new"},a).includes("按需加载"));
});
function detailGitContext(api){
  const pair={id:"pair",base_sha:"base",attempts:[{id:"a",side:"A",artifact_sha:"artifact-a"},{id:"b",side:"B",artifact_sha:"artifact-b"}]};
  const ctx=vm.createContext({state:{pair,dirty:true,detailGitChanges:new Map()},api,$:()=>null});
  vm.runInContext([
    source.match(/^const escape = .*$/m)[0],source.match(/^const latest = .*$/m)[0],
    source.slice(source.indexOf("function gitChangeHtml("),source.indexOf("function evidenceReady(")),
  ].join("\n"),ctx);
  return ctx;
}
test("explicit detail git loading caches both sides without replacing the detail or draft",async()=>{
  const calls=[];
  const ctx=detailGitContext(async path=>{
    calls.push(path);
    return {...ctx.state.pair,attempts:ctx.state.pair.attempts.map(a=>({...a,git_changes:{available:true,file_count:7,additions:10,deletions:0}}))};
  });
  const pair=ctx.state.pair;
  assert.ok(ctx.detailGitChangeHtml(pair,pair.attempts[0]).includes("按需加载"));
  assert.equal(calls.length,0);
  await ctx.loadDetailGitChanges("pair");
  assert.deepEqual(calls,["/pair?git_changes=1"]);
  assert.equal(ctx.state.pair,pair);
  assert.equal(ctx.state.dirty,true);
  for(const a of pair.attempts)assert.ok(ctx.detailGitChangeHtml(pair,a).includes("7 files changed"));
});
test("failed detail git loading leaves the page readable and allows retry",async()=>{
  const ctx=detailGitContext(async()=>{throw Error("读取失败 <timeout>");});
  await assert.rejects(ctx.loadDetailGitChanges("pair"),/读取失败/);
  const html=ctx.detailGitChangeHtml(ctx.state.pair,ctx.state.pair.attempts[0]);
  assert.ok(html.includes("重试读取 Git 改动"));
  assert.ok(html.includes("&lt;timeout&gt;"));
  assert.equal(ctx.state.dirty,true);
});
const start = source.indexOf("function recordingSideText(");
const end = source.indexOf("function recordingChoiceNote(", start);
assert.ok(start >= 0 && end > start);
const context = vm.createContext({ escape: value => String(value ?? "") });
vm.runInContext(source.slice(start, end), context);

const done = { status: "complete", has_file: true, duration: 33.82 };
for (const [name, A, B, label, busy] of [
  ["single-side preparation with preserved B", { status: "preparing" }, done, "准备录屏", true],
  ["preparation takes precedence over queue", { status: "preparing" }, { status: "queued" }, "准备录屏", true],
  ["B preparation is also active", done, { status: "preparing" }, "准备录屏", true],
  ["accepted batch waiting for its turn", { status: "queued" }, done, "即将录屏", true],
  ["recording takes precedence over preparation", { status: "preparing" }, { status: "recording" }, "录制中", true],
  ["both complete", done, done, "已完成", false],
  ["failure", { status: "failed" }, done, "录屏失败", false],
  ["partly complete", {}, done, "部分完成", false],
  ["not recorded", {}, {}, "未录屏", false],
]) {
  test(name, () => {
    const html = context.recordingCell({ id: "fixture", recording: { A, B } });
    assert.ok(html.includes(`>${label}</span>`), html);
    assert.equal(/<button[^>]*\bdisabled\b/.test(html), busy);
    if (B === done) assert.ok(html.includes("B 33.82s"));
  });
}

test("shows current step and time without a made-up percentage", () => {
  const html = context.recordingCell({ id: "fixture", recording: {
    A: { status: "preparing", progress: { stage: "planning", label: "生成操作路线", elapsed_seconds: 130, description: "最多等待 8 分钟" } }, B: done,
  } });
  assert.ok(html.includes("A · 生成操作路线"));
  assert.ok(html.includes("本步骤已用 2 分 10 秒"));
  assert.ok(html.includes("最多等待 8 分钟"));
  assert.ok(!html.includes("%"));
});

test("video processing is not labelled as live capture", () => {
  const html = context.recordingCell({ id: "fixture", recording: {
    A: { status: "recording", progress: { stage: "encoding", label: "处理视频", elapsed_seconds: 7 } }, B: done,
  } });
  assert.ok(html.includes(">处理视频</span>"));
  assert.ok(html.includes("本步骤已用 7 秒"));
});

test("unknown legacy timing stays unknown", () => {
  const html = context.recordingProgressHtml({ label: "准备录屏", elapsed_seconds: null });
  assert.ok(!html.includes("本步骤已用"));
  assert.equal(context.recordingElapsed(0), "0 秒");
  assert.equal(context.recordingElapsed(-5), "0 秒");
});

const statusContext = vm.createContext({});
const statusStart = source.indexOf("function taskStatusBadge(");
const statusEnd = source.indexOf("async function loadList(", statusStart);
assert.ok(statusStart >= 0 && statusEnd > statusStart);
vm.runInContext([
  source.match(/^const escape = .*$/m)[0],
  source.match(/^const labels = .*$/m)[0],
  source.slice(statusStart, statusEnd),
].join("\n"), statusContext);

for (const [value, label, tone] of [
  [undefined, "未运行", "neutral"],
  ["queued", "排队中", "warning"],
  ["starting", "启动中", "busy"],
  ["running", "首轮执行中", "busy"],
  ["recovering", "检查并恢复归档", "busy"],
  ["archiving", "归档中", "busy"],
  ["complete", "已归档", "success"],
  ["failed", "执行失败", "danger"],
  ["archive_failed", "归档失败", "danger"],
  ["stopped", "已停止", "neutral"],
]) {
  test(`task attempt badge: ${label}`, () => {
    assert.equal(statusContext.taskStatusBadge(value), `<span class="task-status-tag ${tone}">${label}</span>`);
  });
}

for (const [value, tone] of [
  ["正在生成", "busy"], ["待同时执行", "neutral"], ["A/B 并行执行中", "busy"], ["检查并恢复归档", "busy"],
  ["等待另一侧完成", "busy"], ["参考 GSB 生成中", "busy"],
  ["待人工 GSB", "warning"], ["已填写 GSB", "success"], ["需要处理", "danger"],
]) {
  test(`overall task badge: ${value}`, () => {
    assert.equal(statusContext.taskStatusBadge(value, "pair"), `<span class="task-status-tag ${tone}">${value}</span>`);
  });
}

test("unknown statuses use neutral styling and escape text", () => {
  assert.equal(statusContext.taskStatusBadge('<img src=x>', "pair"), '<span class="task-status-tag neutral">&lt;img src=x&gt;</span>');
  assert.equal(statusContext.taskStatusBadge('__proto__'), '<span class="task-status-tag neutral">__proto__</span>');
});

test("task table renders A, B and overall status with badges", () => {
  const list = source.slice(statusStart, source.indexOf("const soloStateInfo", statusEnd));
  assert.ok(list.includes('attemptStatus(p,"A")'));
  assert.ok(list.includes('attemptStatus(p,"B")'));
  assert.ok(list.includes('taskStatusBadge(p.state,"pair")'));
});

const listContext=vm.createContext({state:{expandedExports:new Set(),exportGitChanges:new Map(),selected:new Set(),selectedDetails:new Map()}});
vm.runInContext([
  source.match(/^const escape = .*$/m)[0], source.match(/^const labels = .*$/m)[0],
  source.match(/^const deliveryOptions = .*$/m)[0],
  source.match(/^const modelNameDisplay = .*$/m)[0],
  source.match(/^const latest = .*$/m)[0],
  source.slice(source.indexOf("function formatTime("),source.indexOf("function evidenceTime(")),
  source.slice(statusStart,statusEnd),
  source.slice(source.indexOf("const soloStateInfo"),source.indexOf("function recordingChoiceNote(")),
  source.slice(source.indexOf("function gitChangeHtml("),source.indexOf("function evidenceReady(")),
  source.slice(source.indexOf("function exportGitChangesHtml("),source.indexOf("function exportReviewPanel(")),
  source.slice(source.indexOf("function exportReviewPanel("),source.indexOf("async function loadExports(")),
].join("\n"),listContext);
const listItem={id:"abcdef012345",display_number:"0002-1",title:'long-project <img> "title"',
  repo_name:"long-project-feature-0013",task_type:"Feature 迭代",difficulty:"困难",state:"已填写 GSB",
  created_at:"2026-09-16 12:00:00 +0800",finished_at:"2026-09-17 10:00:00 +0800",
  attempts:[{side:"A",status:"complete"},{side:"B",status:"complete"}],
  gsb:"B 更好",human_confirmed:true,recording:{A:done,B:done},export_ready:false,
  a_delivery_score:5,a_delivery_description:"A 功能完整并通过验收。",
  a_model_name:"auto_model/urm",
  b_delivery_score:4,b_delivery_description:"B 主流程完成，边界提示有缺口。",
  b_model_name:"ark/urm-03",
  export_issues:["A 本地录屏尚未确认","B 本地录屏尚未确认"],
  solo_qa:{state:"failed",error:"完整错误 <script>"},prompt:"完整题面 <script>",reason:"可编辑的理由"};
vm.runInContext(source.slice(source.indexOf("function notebookEntryHtml("),source.indexOf("async function loadNotebook(")),listContext);
test("local notebook keeps evidence and correction fields escaped",()=>{
  const html=listContext.notebookEntryHtml({id:"entry",title:"<img src=x>",task_type:"Feature 迭代",
    project_category:"纯后端",status:"open",category:"难度不足",prompt:"<script>bad</script>",reason:"<b>原因</b>",
    material:{review:{reason:"<img>"}},events:[{kind:"preflight_passed",data:{detail:"后续已通过"}}],note:"</textarea><script>bad</script>",revision:2});
  for(const value of ["&lt;script&gt;bad&lt;/script&gt;","&lt;/textarea&gt;","后续候选通过预审",'data-revision="2"',"判定有误"]){
    assert.ok(html.includes(value),value);
  }
  assert.ok(!html.includes("<script>")&&!html.includes("<img src=x>"));
});
test("compact task row keeps metadata, both side badges, timestamps and soft delete",()=>{
  const html=listContext.taskRow(listItem);
  assert.equal((html.match(/<td\b/g)||[]).length,7);
  for(const text of ['data-label="A / B 执行"','>A</b>','>B</b>','data-delete="abcdef012345"',
    'data-label="创建时间"','data-label="结束时间"',"困难","Feature 迭代",'title="long-project &lt;img&gt; &quot;title&quot;"'])assert.ok(html.includes(text),text);
});
test("compact export row retains editable expanded content and full feedback",()=>{
  const html=listContext.exportRows(listItem),main=html.split('<tr class="export-detail-row"')[0];
  assert.equal((main.match(/<td\b/g)||[]).length,11);
  assert.ok(html.includes('colspan="11"'));
  const project=main.match(/<td class="task-cell"[^>]*>(.*?)<\/td>/s)[1];
  assert.ok(!project.includes('class="difficulty'));
  assert.ok(main.includes('class="difficulty-cell" data-label="难度"><span class="difficulty">困难</span>'));
  assert.ok(main.includes('data-delete="abcdef012345"'));
  for(const text of ["A Session","B Session","完整题面 &lt;script&gt;","完整错误 &lt;script&gt;",
    'name="reason"','name="gsb"','name="a_delivery_score"','name="a_delivery_description"',
    'name="b_delivery_score"','name="b_delivery_description"','data-pair-recording="abcdef012345"','data-export-expand="abcdef012345"',
    "待检查（2）","查看详情","A 本地录屏尚未确认"])assert.ok(html.includes(text),text);
  assert.match(html, /A 模型名称<select disabled[^>]*><option selected>auto_model\/urm<\/option>/);
  assert.match(html, /B 模型名称<select disabled[^>]*><option selected>ark\/urm-03<\/option>/);
  assert.ok(!html.includes('name="a_model_name"')&&!html.includes('name="b_model_name"'));
  assert.ok(/data-export-select[^>]+disabled/.test(main));
});
test("expanded export row shows current A and B git change summaries",()=>{
  listContext.state.exportGitChanges.set(listItem.id,{attempts:{
    A:{artifact_sha:"a".repeat(40),git_changes:{available:true,file_count:6,additions:171,deletions:27,binary_files:0,files:[{path:"src/a.ts",additions:171,deletions:27}]}},
    B:{artifact_sha:"b".repeat(40),git_changes:{available:true,file_count:2,additions:8,deletions:3,binary_files:0,files:[{path:"src/b.ts",additions:8,deletions:3}]}},
  }});
  const html=listContext.exportReviewPanel(listItem);
  for(const text of ["A / B Git 改动情况","A 当前结果","B 当前结果","6 files changed","171 additions","27 deletions","2 files changed"])assert.ok(html.includes(text),text);
  for(const text of ["src/a.ts","src/b.ts","git-change-files"])assert.ok(!html.includes(text),text);
  listContext.state.exportGitChanges.delete(listItem.id);
});
test("export git changes are fetched only when a row is expanded",()=>{
  const loader=source.slice(source.indexOf("async function loadExports("),source.indexOf("async function deleteListPair("));
  const expand=source.slice(source.indexOf("if(b.dataset.exportExpand)"),source.indexOf('if(b.id==="go-recording-export")'));
  assert.ok(loader.includes("state.expandedExports.has(item.id)")&&loader.includes("loadExportGitChanges(item.id)"));
  assert.ok(expand.includes("await loadExportGitChanges(id)"));
  assert.ok(!source.slice(source.indexOf("async function loadExports("),source.indexOf('$("#export-pairs").innerHTML')).includes("loadExportGitChanges"));
});
test("completion time is primary and missing completion is not called ongoing",()=>{
  const html=listContext.exportTimes(listItem.created_at,listItem.finished_at);
  assert.ok(html.indexOf("2026-09-17")<html.indexOf("2026-09-16"));
  assert.ok(html.includes("创建 09-16 12:00"));
  assert.ok(listContext.exportTimes(listItem.created_at,"").includes("未记录"));
  assert.ok(!listContext.exportTimes(listItem.created_at,"").includes("进行中"));
});
test("ready export remains selectable and uses the same selection bookkeeping",()=>{
  const item={...listItem,export_ready:true,export_issues:[]};
  listContext.state.selected.add(item.id);
  const html=listContext.exportRows(item);
  assert.ok(/data-export-select[^>]+checked/.test(html));
  assert.ok(!/data-export-select[^>]+disabled/.test(html));
  assert.equal(listContext.state.selectedDetails.get(item.id),item);
});

test("export headers and empty state span all eleven columns",()=>{
  const page=readFileSync(new URL("../static/pairwise.html",import.meta.url),"utf8");
  const header=page.match(/<table class="export-table list-table">(.*?)<tbody/s)[1];
  assert.equal((header.match(/<col class=/g)||[]).length,11);
  assert.equal((header.match(/<th(?:\s|>)/g)||[]).length,11);
  assert.ok(header.includes("<th>项目 / 仓库</th><th>难度</th><th>GSB</th>"));
  const loader=source.slice(source.indexOf("async function loadExports("),source.indexOf("async function deleteListPair("));
  assert.ok(loader.includes('colspan="11"'));
});

test("export difficulty uses reviewed or manually annotated labels",()=>{
  const item={...listItem,difficulty_assessment:{label:"中等 · 已复评"}};
  assert.ok(listContext.exportRows(item).includes('<span class="difficulty">中等</span><small>已复评</small>'));
  item.difficulty_assessment={status:"complete",reviewed_at:"now",preflight:{manual_annotation:{basis_reviewed_at:"now",level:"地狱"}}};
  assert.ok(listContext.exportRows(item).includes('<span class="difficulty">地狱</span><small>人工标注</small>'));
});

function deleteListContext({view="exports",confirm=true,fail=false,submitting=false,busy=false}={}){
  const calls=[],id="abcdef012345",other="fedcba543210";
  const state={view,exportItems:[{id,solo_qa:{state:submitting?"submitting":"not_submitted"}}],
    exportDirty:true,soloBusy:busy,selected:new Set([id,other]),selectedDetails:new Map([[id,{}],[other,{}]]),expandedExports:new Set([id,other])};
  const context=vm.createContext({state,window:{confirm:message=>{calls.push(["confirm",message]);return confirm;}},
    api:async path=>{calls.push(["api",path]);if(fail)throw Error("failed");},
    loadExports:async()=>calls.push(["exports"]),loadList:async()=>calls.push(["tasks"]),notice:message=>calls.push(["notice",message])});
  vm.runInContext(source.slice(source.indexOf("async function deleteListPair("),source.indexOf("function pingSoloBridge(")),context);
  return {context,state,calls,id,other};
}
test("export deletion confirms, clears selection and expansion, then reloads exports",async()=>{
  const {context,state,calls,id,other}=deleteListContext();
  await context.deleteListPair(id);
  assert.ok(calls[0][1].includes("尚未保存的 GSB")&&calls[0][1].includes("SOLO-QA 数据不会删除"));
  assert.deepEqual(calls[1],["api",`/${id}/delete`]);
  for(const set of [state.selected,state.selectedDetails,state.expandedExports]){
    assert.ok(!set.has(id)&&set.has(other));
  }
  assert.equal(state.exportDirty,false);
  assert.ok(calls.some(c=>c[0]==="exports")&&!calls.some(c=>c[0]==="tasks"));
});
test("task deletion reloads tasks, not exports",async()=>{
  const {context,calls,id}=deleteListContext({view:"tasks"});
  await context.deleteListPair(id);
  assert.ok(calls.some(c=>c[0]==="tasks")&&!calls.some(c=>c[0]==="exports"));
});
test("cancelled or failed deletion preserves unsaved GSB and selection",async()=>{
  for(const settings of [{confirm:false},{fail:true}]){
    const {context,state,calls,id}=deleteListContext(settings);
    if(settings.fail)await assert.rejects(context.deleteListPair(id),/failed/);else await context.deleteListPair(id);
    assert.ok(state.exportDirty&&state.selected.has(id)&&state.selectedDetails.has(id)&&state.expandedExports.has(id));
    assert.ok(!calls.some(c=>["exports","tasks","notice"].includes(c[0])));
    if(!settings.fail)assert.equal(calls.length,1);
  }
});
test("in-flight submissions cannot be removed from the list",async()=>{
  for(const settings of [{submitting:true},{busy:true}]){
    const {context,state,calls,id}=deleteListContext(settings);
    await assert.rejects(context.deleteListPair(id),/正在提交/);
    assert.equal(calls.length,0);assert.ok(state.selected.has(id));
  }
  const html=listContext.exportRows({...listItem,solo_qa:{state:"submitting"}});
  assert.ok(/data-delete="abcdef012345"[^>]+disabled/.test(html));
});

test("export page provides a compact floating submit action with shared state",()=>{
  const page=readFileSync(new URL("../static/pairwise.html",import.meta.url),"utf8");
  assert.ok(page.includes('id="export-submit-dock"')&&page.includes('id="solo-gsb-submit-floating"'));
  assert.equal((page.match(/data-solo-submit/g)||[]).length,2);
  const controls=source.slice(source.indexOf("function updateFloatingSubmitDock("),source.indexOf("async function syncGsb("));
  assert.ok(controls.includes('document.querySelectorAll("[data-solo-submit]")'));
  assert.ok(controls.includes('panel.getBoundingClientRect().bottom>72'));
  assert.ok(source.includes('if(b.dataset.soloSubmit!==undefined)await submitSelectedGsb()'));
});

test("same-model Pair can reach the submit helper without a mixed-model quota",async()=>{
  const id="abcdef012345",calls=[];
  const item={id,display_number:"0308",repo_name:"example",gsb:"A 更好",
    a_model_name:"auto_model/urm",b_model_name:"auto_model/urm"};
  const state={soloBusy:false,exportDirty:false,selected:new Set([id]),
    selectedDetails:new Map([[id,item]])};
  const context=vm.createContext({state,soloSubmittable:()=>true,
    window:{confirm:()=>true},renderSoloControls:()=>{},loadExports:async()=>{},notice:()=>{},
    requestSoloBridge:async(type,payload)=>{calls.push([type,payload]);return {results:[{outcome:"submitted"}]};}});
  vm.runInContext(source.slice(source.indexOf("async function submitSelectedGsb("),
    source.indexOf("function verificationSummary(")),context);
  await context.submitSelectedGsb();
  assert.equal(calls.length,1);
  assert.equal(calls[0][0],"SOLO_GSB_SUBMIT");
  assert.deepEqual(Array.from(calls[0][1].pair_ids),[id]);
});

const evidenceContext=vm.createContext({});
vm.runInContext([
  source.match(/^const escape = .*$/m)[0],
  source.slice(source.indexOf("function evidenceTime("),source.indexOf("function updateRuntime(")),
  source.slice(source.indexOf("function recordingErrorText("),source.indexOf("function attemptHtml(")),
].join("\n"),evidenceContext);
test("completed recording is shown once in the local evidence row",()=>{
  const attempt={id:"recorded",side:"A",status:"complete",artifact_sha:"a".repeat(40),recording_status:"complete",
    recording_path:"/tmp/A.m4v",recording_duration:8.97,video_seconds:8.97,recording_finished_at:"2026-09-17 12:00:00 +0800"};
  assert.equal(evidenceContext.autoRecordingHtml(attempt,"A"),"");
  const html=evidenceContext.localEvidenceHtml(attempt);
  assert.equal((html.match(/本地录屏/g)||[]).length,1);
  assert.ok(html.includes("已完成 · 8.97 秒")&&html.includes("打开视频")&&html.includes("重新生成视频"));
  assert.ok(!html.includes("A 录屏已完成"));
});

test("real frontend startup failure video is labelled as failure evidence",()=>{
  const attempt={id:"failed-ui",side:"A",status:"complete",recording_status:"complete",
    recording_path:"/tmp/A.m4v",recording_duration:22.47,
    recording_error:'已录制原始产物启动失败：nginx: invalid option: "g"'};
  assert.ok(evidenceContext.autoRecordingHtml(attempt,"A").includes("原始产物启动失败过程已录制"));
  assert.ok(evidenceContext.localEvidenceHtml(attempt).includes("启动失败实录 · 22.47 秒"));
  const cell=context.recordingCell({id:"fixture",recording:{
    A:{status:"complete",has_file:true,duration:22.47,failure_evidence:true},
    B:{status:"complete",has_file:true,duration:29.04},
  }});
  assert.ok(cell.includes("失败过程已录制"));
  assert.ok(cell.includes("A 失败实录 22.47s"));
});

test("runtime failure video is labelled as failure evidence",()=>{
  const attempt={id:"runtime-ui",side:"B",status:"complete",recording_status:"complete",
    recording_path:"/tmp/B.m4v",recording_duration:25,
    recording_error:"已录制原始产物运行失败：页面一直显示计算中"};
  assert.ok(evidenceContext.autoRecordingHtml(attempt,"B").includes("原始产物运行失败过程已录制"));
  assert.ok(evidenceContext.localEvidenceHtml(attempt).includes("运行失败实录 · 25 秒"));
});

const navContext=vm.createContext({});
vm.runInContext([
  source.match(/^const escape = .*$/m)[0],
  source.slice(source.indexOf("function detailNavigationHtml("),source.indexOf("function updateDetailNavigation(")),
].join("\n"),navContext);
test("detail navigation follows the modules present on the page",()=>{
  const base={attempts:[{},{},{}],recording_guide:{}};
  const html=navContext.detailNavigationHtml(base);
  for(const label of ["任务概览","题面与仓库","难度复评","当前流程","A / B 结果","录屏指南","GSB 对比与评价","历史尝试"])assert.ok(html.includes(label),label);
  const compact=navContext.detailNavigationHtml({attempts:[],recording_guide:null});
  assert.ok(!compact.includes("录屏指南")&&!compact.includes("历史尝试"));
  assert.equal((html.match(/aria-current="true"/g)||[]).length,1);
});

const historyContext=vm.createContext({});
vm.runInContext([
  source.match(/^const escape = .*$/m)[0],source.match(/^const labels = .*$/m)[0],
  source.slice(source.indexOf("function historyAttemptHtml("),source.indexOf("function renderDetail(")),
].join("\n"),historyContext);
test("history attempts put their start time first and label unstarted attempts by creation time",()=>{
  const started=historyContext.historyAttemptHtml({side:"A",number:3,id:"run-3",status:"stopped",
    started_at:"2026-09-25 13:57:32 +0800",created_at:"2026-09-25 13:40:00 +0800",error:"",recording_path:"/old-video.m4v"});
  assert.ok(started.indexOf("开始 2026-09-25 13:57:32")<started.indexOf("A-3"));
  assert.ok(started.includes('href="/api/pairwise/attempts/run-3/recording-file"'));
  const unstarted=historyContext.historyAttemptHtml({side:"B",number:2,id:"run-2",status:"queued",
    started_at:null,created_at:"2026-09-25 13:41:00 +0800",error:""});
  assert.ok(unstarted.indexOf("创建 2026-09-25 13:41:00")<unstarted.indexOf("B-2"));
});
test("single-side rerun opens the current pair and clears its export selection",async()=>{
  const calls=[],id="abcdef012345",state={soloBusy:false,exportDirty:false,dirty:false,
    selected:new Set([id]),selectedDetails:new Map([[id,{}]]),detailToken:0,returnView:"tasks"};
  const context=vm.createContext({state,sessionStorage:{setItem:(...args)=>calls.push(["session",...args])},
    location:{hash:""},api:async(path,payload)=>{calls.push(["api",path,payload.side]);return {id,display_number:"0001",attempts:[]};},
    renderDetail:pair=>calls.push(["detail",pair.id]),notice:message=>calls.push(["notice",message])});
  vm.runInContext(source.slice(source.indexOf("async function rerunSide("),source.indexOf('document.addEventListener("input"')),context);
  await context.rerunSide(id,"A",true);
  assert.deepEqual(calls[0],["api",`/${id}/rerun`,"A"]);
  assert.ok(!state.selected.has(id)&&!state.selectedDetails.has(id));
  assert.equal(state.returnView,"exports");
  assert.equal(context.location.hash,"0001");
  assert.ok(calls.some(call=>call[0]==="detail"&&call[1]===id));
  state.exportDirty=true;
  await assert.rejects(context.rerunSide(id,"B",true),/先保存展开区中的 GSB/);
  assert.equal(calls.filter(call=>call[0]==="api").length,1);
});

const attemptContext=vm.createContext({state:{detailGitChanges:new Map()},formatTime:v=>v||"—",verificationSummary:()=>"待验收",link:()=>"",
  autoRecordingHtml:()=>"",localEvidenceHtml:()=>""});
vm.runInContext([
  source.match(/^const escape = .*$/m)[0],source.match(/^const labels = .*$/m)[0],
  source.match(/^const latest = .*$/m)[0],source.match(/^const active = .*$/m)[0],
  source.slice(source.indexOf("function gitChangeHtml("),source.indexOf("function evidenceReady(")),
  source.slice(source.indexOf("function attemptHtml("),source.indexOf("function comparisonSide(")),
].join("\n"),attemptContext);
for(const [status,promptSent,canRecover] of [["failed",1,true],["failed",0,false],["stopped",1,false],["recovering",1,false],["complete",1,false]]){
  test(`recovery button only for original sent failed turn: ${status}/${promptSent}`,()=>{
    const html=attemptContext.attemptHtml({attempts:[{id:"original",side:"A",number:1,status,
      prompt_sent:promptSent,container_cleaned:0,verification:[],session_id:"same-session"}]},"A");
    assert.equal(html.includes('data-op="recover"'),canRecover);
    if(canRecover)assert.ok(html.includes("检查并恢复归档（不重跑）"));
    if(status==="recovering"){
      assert.ok(html.includes("检查并恢复归档"));
      assert.ok(!html.includes("data-retry="));
    }
  });
}
test("unconfirmed retry action is shown only when the server marks the attempt eligible",()=>{
  const attempt={id:"unconfirmed",side:"A",number:1,status:"failed",prompt_sent:1,
    container_cleaned:0,session_id:null,verification:[],can_retry_unconfirmed:true};
  const html=attemptContext.attemptHtml({attempts:[attempt]},"A");
  assert.ok(html.includes('data-op="retry-unconfirmed"'));
  assert.ok(html.includes("保留无轨迹现场并重跑 A"));
  attempt.can_retry_unconfirmed=false;
  assert.ok(!attemptContext.attemptHtml({attempts:[attempt]},"A").includes('data-op="retry-unconfirmed"'));
});
test("completed attempt shows its video without a confirmation form",()=>{
  const html=attemptContext.attemptHtml({id:"abcdef012345",repo_url:"https://example.test/repo",attempts:[{id:"done",side:"A",number:1,status:"complete",
    status_detail:"完成",prompt_sent:1,container_cleaned:1,verification:[],session_id:"session",artifact_sha:"a".repeat(40),video_seconds:8.97,recording_duration:8.97,recording_path:"/video.m4v",can_rerun:true}]},"A");
  assert.ok(html.includes('class="evidence"'));
  assert.ok(html.includes('data-rerun-pair="abcdef012345" data-rerun-side="A"'));
  assert.ok(!html.includes('name="recording_confirmed"'));
  assert.ok(!html.includes('data-evidence="done"'));
});
