"use strict";
const $ = s => document.querySelector(s);
const escape = s => String(s ?? "").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const state = {page:1,pages:1,exportPage:1,exportPages:1,selected:new Set(),selectedDetails:new Map(),exportItems:[],pair:null,dirty:false,busy:false,listToken:0,exportToken:0,detailToken:0,autoDirty:false,autoInitialized:false,autoConfig:null,view:"tasks",returnView:"tasks",soloBridgeReady:false,soloBridgeVersion:"",soloBusy:false,soloRequests:new Map(),soloMessage:""};
const labels = {queued:"排队中",starting:"启动中",running:"首轮执行中",archiving:"归档中",complete:"已归档",failed:"执行失败",archive_failed:"归档失败",stopped:"已停止"};
const active = a => a && ["queued","starting","running","archiving"].includes(a.status);
let draftId = sessionStorage.getItem("pairwise-question-draft"), generatedDraft = null;
let draftPollBusy = false;
async function pollDraft(){
  if(!draftId||draftPollBusy)return;
  draftPollBusy=true;
  try{const d=await api(`/drafts/${draftId}`);$("#draft-progress").textContent=d.detail||"正在排队生成题面";
    $("#generate-draft").disabled=["queued","running"].includes(d.status);
    if(d.status==="complete"){generatedDraft=d.result;draftId=null;sessionStorage.removeItem("pairwise-question-draft");
      $("#draft-progress").innerHTML='题面已生成。<button type="button" class="secondary" id="apply-draft">载入生成题面（覆盖题面字段）</button>';}
    if(d.status==="failed"){draftId=null;sessionStorage.removeItem("pairwise-question-draft");}
  }catch(e){$("#draft-progress").textContent=e.message;}finally{draftPollBusy=false;}
}
const latest = (p,s) => p.attempts.filter(a=>a.side===s).at(-1);
const canRestartBoth = p => p.attempts.length >= 2 && ["A","B"].every(side => {
  const a=latest(p,side);
  return a && ["failed","stopped"].includes(a.status) && !a.prompt_sent && a.container_cleaned;
});
function notice(message){$("#notice").textContent=message;$("#notice").hidden=false;if($("#create-dialog").open){$("#create-notice").textContent=message;$("#create-notice").hidden=false;}}
async function api(path,payload){const r=await fetch(`/api/pairwise${path}`,payload===undefined?{}:{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});const data=await r.json();if(!r.ok)throw Error(data.error||"请求失败");return data;}
async function action(fn){if(state.busy)return;state.busy=true;try{await fn();}catch(e){notice(e.message);}finally{state.busy=false;}}
function link(url,text){return /^https:\/\//.test(url||"")?`<a href="${escape(url)}" target="_blank" rel="noopener noreferrer">${escape(text)}</a>`:escape(text||"—");}
function formatTime(value){
  if(!value)return "—";
  const text=String(value).trim();
  const match=text.match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
  return match?`${match[2]}-${match[3]} ${match[4]}:${match[5]}`:text;
}
function updateRuntime(runtime){
  if(!runtime)return;
  $("#console-version").textContent=`版本 ${runtime.app_version||"—"}`;
  $("#parallel-count").textContent=`并行 ${runtime.active_parallel??0}/${runtime.max_parallel??6}`;
  const recording=runtime.recording||{},recordingCount=$("#recording-count");
  if(recordingCount)recordingCount.textContent=recording.active?`录屏中 · 待录 ${recording.queued||0}`:recording.queued?`待录 ${recording.queued}`:recording.failed?`录屏需处理 ${recording.failed}`:"录屏空闲";
}
function recordingGuideHtml(guide,open=false){
  if(!guide)return "";
  const commandText=(guide.commands||[]).join("\n");
  const commandTitle=guide.kind==="frontend"?"后台启动与验收命令":"录屏中的终端命令";
  const commandDescription=guide.kind==="frontend"?"纯前端视频不会出现终端；这些命令只在后台负责启动真实产物和核对运行结果。":"这是点击“开始录屏”后系统将演示的命令预览；README 展示后，还会运行其中的真实请求示例。";
  const interfaceActions=(guide.ui_actions||[]).length?`<div class="guide-panel guide-ui"><h3>界面操作重点</h3><ol>${guide.ui_actions.map(item=>`<li>${escape(item)}</li>`).join("")}</ol><p>按上面的业务顺序在真实页面操作；不要只展示静态首页或测试报告。</p></div>`:"";
  return `<details class="card recording-guide detail-anchor" id="detail-recording" ${open?"open":""}>
    <summary class="guide-heading"><div><p class="eyebrow">RECORDING PLAYBOOK</p><h2>录屏指南</h2><small>需求概括、验收重点和可复制命令</small></div><span class="guide-type">${escape(guide.label)}</span></summary>
    <div class="guide-summary"><strong>这次要做什么</strong><p>${escape(guide.summary)}</p></div>
    <div class="guide-layout">
      <div class="guide-panel"><h3>重点验证哪里</h3><ol>${(guide.focus||[]).map(item=>`<li>${escape(item)}</li>`).join("")}</ol></div>
      <div class="guide-panel"><h3>怎么录、怎么验</h3><ol>${(guide.steps||[]).map(step=>`<li><strong>${escape(step.title)}</strong><span>${escape(step.description)}</span></li>`).join("")}</ol></div>
      ${interfaceActions}
    </div>
    <div class="guide-commands"><div><h3>${commandTitle}</h3><p>${commandDescription}</p></div><button type="button" class="secondary" data-copy-guide="${escape(commandText)}">复制全部命令</button><pre>${escape(commandText)}</pre></div>
    <p class="guide-note">${escape(guide.note)}</p>
  </details>`;
}
function datetimeLocal(value){if(!value)return "";const d=new Date(value);if(Number.isNaN(d.getTime()))return "";const local=new Date(d.getTime()-d.getTimezoneOffset()*60000);return local.toISOString().slice(0,16);}
function renderAuto(config,directories=[]){
  state.autoConfig=config;
  const directoryOptions=$("#project-directories");
  if(directoryOptions&&directories.length)directoryOptions.innerHTML=directories.map(x=>`<option value="${escape(x)}"></option>`).join("");
  const form=$("#auto-form");
  if(!config||!form)return;
if(!state.autoInitialized||!state.autoDirty){form.elements.project_directory.value=config.project_directory||"";form.elements.target_count.value=String(config.target_count||1);form.elements.start_at.value=config.scheduled?datetimeLocal(config.start_at):"";state.autoInitialized=true;}const counts=config.task_type_counts||{},mix=`0-1 ${counts["0-1 代码生成"]||0} · 迭代 ${counts["Feature 迭代"]||0} · Bug 修复 ${counts["Bug 修复"]||0}`;const status=$("#auto-status");status.classList.toggle("has-error",Boolean(config.error));status.innerHTML=`<strong>${escape(config.detail)}</strong><span>本批已创建 ${escape(config.created_count)} / ${escape(config.target_count)} 道${config.generating?" · 题面生成中":""}</span><span>当前三类：${escape(mix)} · 下一类 ${escape(config.next_task_type||"—")}</span>${config.error?`<span>${escape(config.error)}</span>`:""}<small>历史题库：${escape(config.history_file)}</small>`;$("#stop-auto").disabled=!config.enabled&&!config.generating;}
async function loadList(){const token=++state.listToken;const data=await api(`?page=${state.page}&query=${encodeURIComponent($("#search").value)}`);if(token!==state.listToken)return;state.page=data.page;state.pages=data.pages;updateRuntime(data.runtime);renderAuto(data.auto_refill,data.project_directories);$("#pagination").textContent=`第 ${data.page} / ${data.pages} 页 · 共 ${data.total} 对 · 每页 20 条`;$("#prev").disabled=data.page<=1;$("#next").disabled=data.page>=data.pages;$("#pairs").innerHTML=data.items.length?data.items.map(p=>`<tr><td class="task-cell"><button data-open="${p.id}">${escape(p.title)}</button><small>${escape(p.local_project_folder)} · ${escape(p.project_category)} · ${escape(p.task_type)}</small></td><td><span class="difficulty">${escape(p.difficulty)}</span></td><td><time>${escape(formatTime(p.created_at))}</time></td><td><time>${escape(formatTime(p.finished_at))}</time></td><td>${labels[latest(p,"A")?.status]||"未运行"}</td><td>${labels[latest(p,"B")?.status]||"未运行"}</td><td><span class="badge">${escape(p.state)}</span></td><td><button class="row-delete" data-delete="${p.id}" title="仅从列表隐藏，数据库记录保留">删除</button></td></tr>`).join(""):'<tr><td colspan="8" class="empty">还没有 Pair。可启动自动补题，或手动新建一道题目。</td></tr>';}
const soloStateInfo={not_submitted:["未提交","neutral"],submitting:["提交中","busy"],qc_pending:["质检中","busy"],qc_passed:["质检通过","success"],needs_fix:["需要返修","danger"],discarded:["已废弃","muted"],failed:["提交失败","danger"],remote_missing:["远端未找到","danger"],local_changed:["本地已变化","warning"]};
const soloSubmittable=item=>item.export_ready&&["not_submitted","failed","remote_missing"].includes(item.solo_qa?.state||"not_submitted");
async function showView(name,{hash=true}={}){state.view=name;state.pair=null;state.dirty=false;$("#detail-view").hidden=true;$("#notebook-view").hidden=name!=="notebook";$("#list-view").hidden=name!=="tasks";$("#export-view").hidden=name!=="exports";document.querySelectorAll("[data-view]").forEach(b=>b.classList.toggle("active",b.dataset.view===name));if(hash)location.hash=name==="tasks"?"":name;if(name==="exports")await loadExports();else if(name==="notebook")await loadNotebook();else await loadList();}
function updateExportSelection(){const ready=state.exportItems.filter(item=>item.export_ready);const selectedReady=ready.filter(item=>state.selected.has(item.id));const selectPage=$("#export-select-page");selectPage.checked=ready.length>0&&selectedReady.length===ready.length;selectPage.indeterminate=selectedReady.length>0&&selectedReady.length<ready.length;$("#export-selected-count").textContent=`已选 ${state.selected.size} 对`;renderSoloControls();}
function soloCell(item){const solo=item.solo_qa||{},[label,tone]=soloStateInfo[solo.state]||[solo.state||"未提交","neutral"];const detail=solo.error||solo.qc_summary||"";return `<span class="solo-state ${escape(tone)}">${escape(label)}</span>${solo.detail_url?`<a class="remote-detail" href="${escape(solo.detail_url)}" target="_blank" rel="noopener noreferrer">#${escape(solo.remote_id)}</a>`:""}${detail?`<small title="${escape(detail)}">${escape(detail)}</small>`:""}`;}
async function loadExports(){const token=++state.exportToken;const data=await api(`/exports?page=${state.exportPage}&query=${encodeURIComponent($("#export-search").value)}`);if(token!==state.exportToken)return;state.exportPage=data.page;state.exportPages=data.pages;state.exportItems=data.items;updateRuntime(data.runtime);if(data.runtime?.solo_helper_path)$("#solo-helper-path").textContent=data.runtime.solo_helper_path;$("#export-pagination").textContent=`第 ${data.page} / ${data.pages} 页 · 共 ${data.total} 对 · 每页 ${data.page_size}`;$("#export-prev").disabled=data.page<=1;$("#export-next").disabled=data.page>=data.pages;$("#export-pairs").innerHTML=data.items.length?data.items.map(item=>{if(!item.export_ready&&state.selected.has(item.id)){state.selected.delete(item.id);state.selectedDetails.delete(item.id);}const selected=state.selected.has(item.id);if(selected)state.selectedDetails.set(item.id,item);return `<tr><td><input type="checkbox" data-export-select="${item.id}" aria-label="选择 ${escape(item.title)}" ${selected?"checked":""} ${item.export_ready?"":"disabled"}></td><td class="task-cell"><button data-open="${item.id}">${escape(item.title)}</button><small>${String(item.project_number).padStart(4,"0")}-${escape(item.repo_name)} · ${escape(item.task_type)}</small></td><td><span class="difficulty">${escape(item.difficulty)}</span></td><td class="session-cell"><small>A ${escape(item.a_session_id||"—")}</small><small>B ${escape(item.b_session_id||"—")}</small></td><td>${item.gsb?`<strong>${escape(item.gsb)}</strong>`:"—"}</td><td>${item.export_ready?'<span class="readiness ready">可提交</span>':`<span class="readiness blocked">待补资料</span><small title="${escape(item.export_issues.join("；"))}">${escape(item.export_issues.join("；"))}</small>`}</td><td class="solo-cell">${soloCell(item)}</td><td><time>${escape(formatTime(item.finished_at))}</time></td></tr>`;}).join(""):'<tr><td colspan="8" class="empty">还没有 A/B 均完成归档的 Pair。</td></tr>';updateExportSelection();renderSoloControls();}
function pingSoloBridge(){window.postMessage({source:"claude-eval-console",type:"SOLO_QA_BRIDGE_PING"},window.location.origin);}
function requestSoloBridge(type,payload={},timeoutMs=10*60*1000){if(!state.soloBridgeReady)return Promise.reject(Error("Chrome 提交助手未连接，请重新加载扩展并刷新本页"));const requestId=window.crypto?.randomUUID?.()||`${Date.now()}-${Math.random()}`;return new Promise((resolve,reject)=>{const timer=setTimeout(()=>{state.soloRequests.delete(requestId);reject(Error("提交助手等待超时；请同步我的 GSB 确认远端是否已收到"));},timeoutMs);state.soloRequests.set(requestId,{resolve,reject,timer});window.postMessage({source:"claude-eval-console",type,requestId,payload},window.location.origin);});}
function gsbBridgeReady(){const parts=state.soloBridgeVersion.split(".").map(Number);return state.soloBridgeReady&&(parts[0]>1||(parts[0]===1&&(parts[1]||0)>=3));}
function renderSoloControls(){const status=$("#solo-bridge-status"),submit=$("#solo-gsb-submit"),sync=$("#solo-gsb-sync");if(!status)return;const compatible=gsbBridgeReady();status.className="solo-bridge "+(state.soloBusy?"busy":compatible?"connected":"disconnected");status.textContent=state.soloBusy?"正在处理":compatible?`提交助手已连接 · v${state.soloBridgeVersion}`:state.soloBridgeReady?`助手 v${state.soloBridgeVersion} 需重新加载`:"提交助手未连接";const eligible=[...state.selected].map(id=>state.selectedDetails.get(id)).filter(item=>item&&soloSubmittable(item));submit.disabled=!compatible||state.soloBusy||!eligible.length;submit.textContent=eligible.length?`提交所选 Pair（${eligible.length}）`:"提交所选 Pair";sync.disabled=!compatible||state.soloBusy;$("#solo-status-detail").textContent=state.soloMessage||(compatible?"勾选资料完整的 Pair 后提交；两份轨迹会依次上传，录屏使用详情中保存的对象存储链接。":"重新加载 v1.3.0 提交助手并刷新页面后，即可批量提交到 GSB。");}
async function syncGsb(){if(state.soloBusy)return;state.soloBusy=true;state.soloMessage="正在读取 SOLO-QA 的我的 GSB 提交…";renderSoloControls();try{const result=await requestSoloBridge("SOLO_GSB_SYNC",{},3*60*1000);state.soloMessage=`已同步远端 ${result.remote_total||0} 条；匹配本地 ${result.matched||0} 条${result.unmatched?`，${result.unmatched} 条未匹配`:""}${result.remote_missing?`，${result.remote_missing} 条远端已不存在`:""}`;await loadExports();notice(state.soloMessage);}catch(error){state.soloMessage=error.message;notice(error.message);}finally{state.soloBusy=false;renderSoloControls();}}
async function submitSelectedGsb(){if(state.soloBusy)return;const items=[...state.selected].map(id=>state.selectedDetails.get(id)).filter(item=>item&&soloSubmittable(item));if(!items.length)throw Error("所选 Pair 都已提交，或尚未通过资料检查");const quota=state.modelQuota;if(quota?.active){const mixed=items.filter(item=>item.a_model_name==="auto_model/urm"&&item.b_model_name==="ark/urm-03").length;if(mixed<items.length&&3*(quota.mixed+mixed)<quota.total+items.length)throw Error(`本批提交后异模型比例不足 1/3；请先完成至少 ${quota.additional_mixed_needed} 对异模型提交。`);items.sort((a,b)=>Number(b.a_model_name==="auto_model/urm"&&b.b_model_name==="ark/urm-03")-Number(a.a_model_name==="auto_model/urm"&&a.b_model_name==="ark/urm-03"));}const preview=items.slice(0,6).map(item=>`${String(item.project_number).padStart(4,"0")}-${item.repo_name} · ${item.gsb}`).join("\n");if(!window.confirm(`将向 SOLO-QA GSB 提交以下 ${items.length} 对数据，并上传 A/B 两份轨迹：\n\n${preview}${items.length>6?`\n另有 ${items.length-6} 对`:""}\n\n提交后会自动质检，确认继续吗？`))return;state.soloBusy=true;state.soloMessage=`正在逐条提交 ${items.length} 对…`;renderSoloControls();try{const result=await requestSoloBridge("SOLO_GSB_SUBMIT",{pair_ids:items.map(item=>item.id)});const submitted=(result.results||[]).filter(x=>x.outcome==="submitted").length,recovered=(result.results||[]).filter(x=>x.outcome==="recovered").length,skipped=(result.results||[]).filter(x=>x.outcome==="skipped").length,failed=(result.results||[]).find(x=>x.outcome==="failed");state.soloMessage=failed?`已提交 ${submitted} 对、找回 ${recovered} 对；在 ${failed.pair_id} 停止：${failed.error}${result.remaining?`，剩余 ${result.remaining} 对未处理`:""}`:`提交完成：新增 ${submitted} 对${recovered?`，找回已有 ${recovered} 对`:""}${skipped?`，跳过 ${skipped} 对`:""}`;await loadExports();notice(state.soloMessage);}catch(error){state.soloMessage=error.message;notice(error.message);}finally{state.soloBusy=false;renderSoloControls();}}
function verificationSummary(a){
  const checks=a?.verification||[];
  if(!checks.length)return "未配置固定验收命令";
  const passed=checks.filter(item=>Number(item.exit_code)===0).length;
  return `${passed}/${checks.length} 条命令通过`;
}
function evidenceReady(a){return Boolean(a&&a.recording_confirmed&&a.trace_path&&a.recording_path&&a.video_seconds);}
function localRecordingReady(a){return Boolean(a&&a.recording_status==="complete"&&a.recording_path);}
function recordingErrorText(value){const text=String(value||"");return /capture error|无法完成/i.test(text)?"macOS 未授权屏幕录制。请在系统设置 → 隐私与安全性 → 屏幕与系统音频录制中允许 Codex、终端或 Python，重启控制台后重新排队。":text||"未生成有效视频，请检查 macOS 屏幕录制权限。";}
function autoRecordingHtml(a,side){
  const status=a.recording_status||"";
  if(status==="queued")return `<div class="recording-callout is-busy"><div><strong>录屏排队中</strong><small>全局一次只录一个，轮到 ${side} 时会自动打开干净产物和真实验收。</small></div><span class="recording-state">等待</span></div>`;
  if(status==="preparing")return `<div class="recording-callout is-busy"><div><strong>正在准备 ${side} 的干净录屏目录</strong><small>将从本侧产物快照启动，不复用模型执行现场。</small></div><span class="recording-state">准备中</span></div>`;
  if(status==="recording")return `<div class="recording-callout is-live"><div><strong>正在自动录制 ${side}</strong><small>后端题只录终端窗口且隐藏鼠标、菜单栏与 Dock；验收结束后自动收尾，最迟 88 秒。</small></div><span class="recording-state">REC</span></div>`;
  if(status==="complete")return `<div class="recording-callout is-complete"><div><strong>${side} 自动录屏已完成 · ${escape(a.recording_duration||a.video_seconds||"—")} 秒</strong><small>本地保存：${escape(a.recording_path||a.recording_directory||"项目录/录屏")}</small></div><div class="recording-actions"><a href="/api/pairwise/attempts/${a.id}/recording-file">下载视频</a><button type="button" class="secondary" data-attempt="${a.id}" data-op="recording">重新录制</button></div></div>`;
  if(status==="failed")return `<div class="recording-callout is-failed"><div><strong>${side} 自动录屏失败</strong><small>${escape(recordingErrorText(a.recording_error))}</small>${a.recording_path?`<a href="/api/pairwise/attempts/${a.id}/recording-file">下载已保留的现场视频</a>`:""}</div><button type="button" data-attempt="${a.id}" data-op="recording">重新排队</button></div>`;
  return `<div class="recording-callout"><div><strong>等待自动录制 ${side}</strong><small>A/B 都完成归档后会自动加入全局录屏队列。</small></div></div>`;
}
function attemptHtml(p,side){const a=latest(p,side);if(!a)return `<article class="card attempt-card is-empty"><div class="attempt-head"><span class="attempt-letter">${side}</span><div><h2>${side} · 独立首轮</h2><p>等待同时创建 A/B</p></div><span class="badge">未运行</span></div></article>`;
const sameSideActive=p.attempts.some(x=>x.side===side&&active(x));
const canRetry=["failed","stopped"].includes(a.status)&&a.container_cleaned&&!sameSideActive;
const retry=canRetry?`<button data-retry="${side}">新建 ${side} 独立 Session</button>`:"";
const canStop=active(a)||(["failed","stopped"].includes(a.status)&&!a.container_cleaned);
return `<article class="card attempt-card"><div class="attempt-head"><span class="attempt-letter">${side}</span><div><h2>${side} · 第 ${a.number} 次独立 Session</h2><p>${escape(a.status_detail)}</p></div><span class="badge">${labels[a.status]||escape(a.status)}</span></div>${a.error?`<p class="error">${escape(a.error)}</p>`:""}<div class="attempt-facts"><div><span>SessionID</span><strong>${escape(a.session_id||"等待产生")}</strong></div><div><span>执行时间</span><strong>${escape(formatTime(a.started_at))} → ${escape(formatTime(a.finished_at))}</strong></div><div><span>产物快照</span><strong>${a.artifact_sha?link(`${p.repo_url}/commit/${a.artifact_sha}`,a.artifact_sha.slice(0,12)):"—"}</strong></div><div><span>固定验收</span><strong>${escape(verificationSummary(a))}</strong></div></div><div class="attempt-actions">${retry}${canStop?`<button class="secondary" data-attempt="${a.id}" data-op="stop">停止并保留现场</button>`:""}${a.status==="archive_failed"?`<button data-attempt="${a.id}" data-op="archive">重新归档（不调用模型）</button>`:""}${a.trace_path?`<a href="/api/pairwise/attempts/${a.id}/trace">下载原始 JSONL</a>`:""}</div>
${a.status==="complete"?`${autoRecordingHtml(a,side)}<form class="evidence" data-evidence="${a.id}"><div class="local-evidence-summary"><span><b>本地轨迹</b>${a.trace_path?`<a href="/api/pairwise/attempts/${a.id}/trace">检查 JSONL</a>`:"<em>缺失</em>"}</span><span><b>本地录屏</b>${a.recording_path?`<a href="/api/pairwise/attempts/${a.id}/recording-file">检查视频</a>`:"<em>缺失</em>"}</span><small>提交时由 Chrome 助手把这两个本地文件直接上传到 SOLO-QA，无需对象存储链接。</small></div><label>录屏时长（秒，≤ 90）<input name="video_seconds" type="number" min="0.1" step="0.1" max="90" value="${escape(a.video_seconds??"")}" readonly></label><label class="check"><input name="recording_confirmed" type="checkbox" ${a.recording_confirmed?"checked":""}>我已检查本地录屏，确认从干净状态启动，完整展示真实输出；失败过程没有被剪掉。</label><button class="secondary" type="submit">保存 ${side} 资料确认</button></form><details class="raw-result"><summary>查看模型最终回复与验收原始输出</summary><pre>${escape(a.first_result||"无文本回复")}</pre>${a.verification.map(v=>`<p>${escape(v.command)} · 退出码 ${escape(v.exit_code)}</p><pre>${escape(v.output)}</pre>`).join("")||"<p>未配置验收命令，请人工运行产物。</p>"}</details>`:""}</article>`;}
function comparisonSide(p,side){
  const a=latest(p,side),checks=a?.verification||[],passed=checks.filter(item=>Number(item.exit_code)===0).length;
  return `<article class="comparison-side"><div class="comparison-title"><span class="attempt-letter">${side}</span><strong>${side} 的客观材料</strong></div><dl><div><dt>固定验收</dt><dd>${checks.length?`${passed}/${checks.length} 通过`:"未配置"}</dd></div><div><dt>产物快照</dt><dd>${a?.artifact_sha?link(`${p.repo_url}/commit/${a.artifact_sha}`,a.artifact_sha.slice(0,12)):"—"}</dd></div><div><dt>原始轨迹</dt><dd>${a?.trace_path?"已归档":"缺失"}</dd></div><div><dt>运行录屏</dt><dd>${evidenceReady(a)?"链接与时长已确认":"待填写"}</dd></div></dl></article>`;
}
function comparisonHtml(p){
  return `<section class="card comparison-workbench"><div class="section-heading"><div><p class="eyebrow">COMPARISON</p><h2>A/B 对比工作台</h2></div><span class="guide-type">A/B 均已归档</span></div><p class="compliance-note">这里汇总两侧客观材料。填写结论前请核对轨迹、代码、验收输出和两段真实录屏，确保理由里的文件、报错和行为都能回验。</p><div class="comparison-grid">${comparisonSide(p,"A")}${comparisonSide(p,"B")}</div><div class="writing-guide"><strong>GSB 理由规则（与交付完整性描述分开）</strong><ol><li>A、B 分别写清优点和问题，同时覆盖执行过程与最终产物。</li><li>过程问题落到具体步骤、文件或检查动作，写清行为和影响；产物问题指向文件、报错或遗漏需求。</li><li>保留结论需要的事实，不堆数字和逐行代码对比；不用泛化主语、原始通用验收命令或与其他理由相似的固定句式。</li><li>结尾用本题特有的操作与实际后果完成权衡，不使用固定判优口号。</li></ol></div></section>`;
}
function reviewHtml(p,allComplete){
  if(!allComplete)return `<section class="card review-locked"><span>3</span><div><h2>人工 GSB</h2><p>等待 A、B 都完成归档后开放。先不要提前判断。</p></div></section>`;
  return `${comparisonHtml(p)}<section class="card review-card"><div class="section-heading"><div><p class="eyebrow">FINAL DECISION</p><h2>确认 GSB</h2></div></div><form id="review-form"><div class="review-grid"><label>GSB 结论<select name="gsb"><option value="">请选择</option>${["A 更好","Same","B 更好"].map(x=>`<option ${p.gsb===x?"selected":""}>${x}</option>`).join("")}</select></label><label class="review-reason">GSB 理由<textarea name="reason" rows="9" placeholder="分别说明 A、B 的优缺点、具体依据和最终权衡">${escape(p.reason)}</textarea></label><label class="review-reason">备注<textarea name="notes" rows="3">${escape(p.notes)}</textarea></label></div><label class="check"><input type="checkbox" name="human_confirmed" ${p.human_confirmed?"checked":""}>我已核对 A/B 的轨迹、产物、验收结果和两份录屏，确认结论与理由可由这些材料回验。</label><div class="review-actions"><button type="submit">保存评价</button><button class="secondary" type="button" id="preflight">检查交付资料</button><button class="secondary" type="button" id="export-one">导出此 Pair</button><small id="draft-state">输入自动保存到本机草稿，正式评价需点击保存。</small></div></form><div id="preflight-result"></div></section>`;
}
function difficultyReviewHtml(p){
  const d=p.difficulty_assessment||{},r=d.review||{},pre=d.preflight?.review||{},busy=["queued","running"].includes(d.status);
  const done=["A","B"].every(side=>latest(p,side)?.status==="complete");
  const list=values=>(values||[]).map(value=>`<li>${escape(value)}</li>`).join("");
  const evidence=(r.code_evidence||[]).map(item=>`<li><code>${escape(item.path)}:${escape(item.line)}</code> — ${escape(item.claim)}</li>`).join("");
  return `<section class="card difficulty-review-card detail-anchor" id="detail-difficulty"><div class="section-heading"><h2>实际难度复评</h2><button class="secondary" data-difficulty-review="${p.id}" ${!done||busy?"disabled":""}>${busy?"复评处理中":d.status==="complete"?"重新复评难度":"复评难度"}</button></div><p><strong>${escape(difficultyLabel(p)||"难度待复评")}</strong> · 出题标签：${escape(p.difficulty)}</p><p>${escape(d.decision_source==="manual"?`提交检查与导出采用人工标注：${d.level}。原自动复评：${d.automated_level}，依据保留如下。`:manualDifficulty(p)?`人工标注为 ${manualDifficulty(p)}；自动复评为 ${d.level}，复评依据及提交检查保留。`:d.eligible?"难度已通过，提交前仍需核对交付资料。":(d.issues||["A/B 归档后复评，完成前不可提交。"]).join("；"))}</p>${d.error?`<p class="error">${escape(d.error)}</p>`:""}${r.level?`<details open><summary>复评依据${d.reviewed_at?` · ${escape(d.reviewed_at)}`:""}</summary><p><b>初始能力：</b>${escape(r.baseline_capabilities)}</p><p><b>本轮增量：</b>${escape(r.required_delta)}</p><p><b>最小正确方案：</b>${escape(r.minimal_solution)}</p><p><b>常规方案是否足够：</b>${r.routine_solution_sufficient?"是":"否"}</p><h3>关键取舍与验证</h3><ul>${list(r.design_tradeoffs)}${list(r.verification_obligations)}</ul><ul>${list(r.human_work_reasons)}</ul><h3>代码证据</h3><ul>${evidence}</ul></details>`:""}<details><summary>出题预审依据</summary>${pre.level?`<p>${escape(pre.level)} · ${escape(pre.minimal_solution)}</p><ul>${list(pre.design_tradeoffs)}${list(pre.verification_obligations)}</ul>`:"<p>此题没有保存完整预审依据，须完成实际难度复评。</p>"}</details></section>`;
}
function journeyHtml(p){
  const a=latest(p,"A"),b=latest(p,"B"),allComplete=Boolean(a&&b&&a.status==="complete"&&b.status==="complete"),recordingFailed=allComplete&&[a,b].some(item=>item.recording_status==="failed"),allRecorded=allComplete&&localRecordingReady(a)&&localRecordingReady(b),allEvidence=allComplete&&evidenceReady(a)&&evidenceReady(b);
  const stage=p.human_confirmed?4:allEvidence?3:allComplete?2:1;
  const steps=["运行 A/B","录制证据","人工 GSB","检查并导出"];
  let title="等待 A/B 完成首轮",detail="系统只读监控两个独立 Session，不会发送追加消息。",action="";
  if(!p.attempts.length){title="先同时启动 A/B";detail="系统会从同一 main 快照建立两个目录，各发送一次相同题面。";action='<button id="start-pair">同时运行 A / B</button>';}
  else if(canRestartBoth(p)){title="A/B 均未发送题面，可同时重建";detail="复用原 main 与冻结配置，旧失败记录继续保留。";action='<button id="start-pair">同时重建 A / B</button>';}
  else if([a,b].some(item=>item&&["failed","stopped","archive_failed"].includes(item.status))){title="有一侧需要处理";detail="在对应 A/B 卡片保留现场、重新归档，或在允许时新建独立 Session。";}
  else if(allComplete&&p.difficulty_assessment&&!p.difficulty_assessment.eligible){title=p.difficulty_assessment.level?"难度未通过，当前不可提交":"下一步：完成实际难度复评";detail=(p.difficulty_assessment.issues||[]).join("；");}
  else if(recordingFailed){title="自动录屏需要处理";detail="检查对应侧的 macOS 权限或错误提示，处理后点击重新排队；其他项目不会被阻塞。";}
  else if(allComplete&&!allRecorded){title="自动录屏处理中";detail="A、B 已进入全局单队列；系统录完一个再录下一个，完成核心演示即停止。";}
  else if(allRecorded&&!allEvidence){title="下一步：检查并上传两段录屏";detail="下载 A/B 原始视频核对，上传对象存储后填写直链并确认。";}
  else if(allEvidence&&!p.human_confirmed){title="下一步：人工比较 A/B";detail="对照两份录屏、验收输出和具体实现，填写结论与理由。";}
  else if(p.human_confirmed){title="评价已保存，检查后导出";detail="运行交付资料检查；全部通过后即可导出此 Pair。";}
  return `<section class="card journey-card detail-anchor" id="detail-progress"><div class="journey-copy"><p class="eyebrow">NEXT ACTION</p><h2>${escape(title)}</h2><p>${escape(detail)}</p>${action}</div><ol class="journey-steps">${steps.map((name,index)=>`<li class="${index+1<stage?"done":index+1===stage?"current":""}"><span>${index+1}</span><small>${name}</small></li>`).join("")}</ol></section>`;
}
function taskContextHtml(p){
  const b=latest(p,"B"),locked=b&&(["starting","running","recovering","archiving","upstream_paused"].includes(b.status)||Boolean(b.prompt_sent&&b.status==="queued"));
  const selected=p.b_execution_model||"auto_model/urm",aSelected=p.a_execution_model||"auto_model/urm";
  return `<details class="card task-context detail-anchor" id="detail-task"><summary><div><p class="eyebrow">TASK CONTEXT</p><h2>题面与仓库信息</h2></div><small>需要时展开核对</small></summary><div class="meta"><span>本地项目文件夹</span><span>${escape(p.local_project_folder)}</span><span>来源项目快照</span><span>${p.source_snapshot_url?link(p.source_snapshot_url,p.source_base_sha):"0-1：空 README"}</span><span>main 初始快照</span><span>${p.snapshot_url?link(p.snapshot_url,p.base_sha):"同时启动 A/B 时创建"}</span><span>固定配置</span><span>${p.config.image?`A ${escape(aSelected)} / B ${escape(selected)} / 1,000,000 tokens / Claude Code ${escape(p.config.harness_version)} / ${escape(p.config.operating_system)}`:"同时启动 A/B 时读取实际配置并冻结"}</span></div><div class="review-actions"><label>B 下次执行模型 <select id="b-execution-model" ${locked?"disabled":""}><option value="auto_model/urm" ${selected==="auto_model/urm"?"selected":""}>auto_model/urm（原方案）</option><option value="ark/urm-03" ${selected==="ark/urm-03"?"selected":""}>ark/urm-03（异模型）</option></select></label><button type="button" class="secondary" id="save-b-execution-model" ${locked?"disabled":""}>保存模型方案</button><small>A 本题已选 ${escape(aSelected)}；切换已完成题目的 B 模型后，须单侧重跑 B，按新证据提交。</small></div><h3>完整 User Prompt（A/B 共用）</h3><pre>${escape(p.prompt)}</pre><p>每个 Session 只发送一次共同 Prompt；禁止输入“继续”“重试”或任何追加内容。</p></details>`;
}
function renderDetail(p){
  state.pair=p;updateRuntime(p.runtime);$("#list-view").hidden=true;$("#export-view").hidden=true;$("#detail-view").hidden=false;
  const a=latest(p,"A"),b=latest(p,"B"),allComplete=Boolean(a&&b&&a.status==="complete"&&b.status==="complete"),allEvidence=allComplete&&evidenceReady(a)&&evidenceReady(b);
  $("#detail-view").innerHTML=`<div class="detail-hero"><button class="secondary" id="back">← 返回${state.returnView==="exports"?"导出列表":"任务列表"}</button><div class="detail-title-row"><div><p class="eyebrow">${escape(p.local_project_folder)}</p><h1>${escape(p.title)}</h1><p>${escape(p.project_category)} · ${escape(p.task_type)} · ${escape(difficultyLabel(p))} · ${escape(p.language_framework)}</p></div><span class="badge">${escape(p.state)}</span></div></div>${journeyHtml(p)}<section class="section-heading attempts-heading"><div><p class="eyebrow">LIVE RESULTS</p><h2>A / B 当前结果</h2></div><small>两侧完全独立，只共享初始 main 和题面</small></section><section class="attempts">${attemptHtml(p,"A")}${attemptHtml(p,"B")}</section>${recordingGuideHtml(p.recording_guide,allComplete&&!allEvidence)}${reviewHtml(p,allComplete)}${taskContextHtml(p)}${p.attempts.length>2?`<details class="card prior-attempts"><summary>查看全部历史尝试</summary>${p.attempts.map(item=>`<p>${item.side}-${item.number} · ${escape(item.session_id||item.id)} · ${labels[item.status]} · ${escape(item.error)}</p>`).join("")}</details>`:""}`;
  if(canRestartBoth(p))document.querySelectorAll("[data-retry]").forEach(button=>button.hidden=true);
  const form=$("#review-form"),draft=readDraft(p.id);state.dirty=false;if(form&&draft&&draft.revision===p.revision){restoreForm(form,draft.values);state.dirty=true;$("#draft-state").textContent="已恢复本机未提交草稿";}
  document.querySelectorAll("form[data-evidence]").forEach(evidenceForm=>{try{const evidenceDraft=JSON.parse(localStorage.getItem(`pairwise-evidence-${evidenceForm.dataset.evidence}`));if(evidenceDraft&&evidenceDraft.base===JSON.stringify(formValues(evidenceForm))){restoreForm(evidenceForm,evidenceDraft.values);state.dirty=true;}}catch{}});
}
function restoreForm(form,values){for(const [k,v] of Object.entries(values)){const el=form.elements.namedItem(k);if(el){if(el.type==="checkbox")el.checked=v;else el.value=v;}}}
function readDraft(id){try{return JSON.parse(localStorage.getItem(`pairwise-review-${id}`));}catch{return null;}}
function formValues(form){const x=Object.fromEntries(new FormData(form));form.querySelectorAll('input[type="checkbox"]').forEach(e=>x[e.name]=e.checked);return x;}
function saveDraft(){const f=$("#review-form");if(!f||!state.pair)return;try{
const p=state.pair,values=formValues(f),reviewKey=`pairwise-review-${p.id}`;
const changed=Object.entries(values).some(([k,v])=>v!==(k==="human_confirmed"?Boolean(p[k]):p[k]));
if(changed)localStorage.setItem(reviewKey,JSON.stringify({revision:p.revision,values}));else localStorage.removeItem(reviewKey);
let evidenceChanged=false;
document.querySelectorAll("form[data-evidence]").forEach(form=>{
 const a=p.attempts.find(a=>a.id===form.dataset.evidence),values=formValues(form),key=`pairwise-evidence-${a.id}`;
 const base={video_seconds:String(a.video_seconds??""),recording_confirmed:Boolean(a.recording_confirmed)};
 if(JSON.stringify(values)!==JSON.stringify(base)){localStorage.setItem(key,JSON.stringify({base:JSON.stringify(base),values}));evidenceChanged=true;}else localStorage.removeItem(key);
});state.dirty=changed||evidenceChanged;
}catch{notice("本机草稿存储不可用，请及时保存修改。");}}
async function openPair(id){const token=++state.detailToken;const p=await api(`/${id}`);if(token!==state.detailToken)return;renderDetail(p);location.hash=id;}
async function exportIds(ids){if(!ids.length)throw Error("请先选择 Pair");const r=await fetch("/api/pairwise/export",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({ids})});if(!r.ok)throw Error((await r.json()).error);const url=URL.createObjectURL(await r.blob());const a=document.createElement("a");a.href=url;a.download="pairwise-gsb.xlsx";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);notice("已导出 Pair-wise GSB Excel。");}
document.addEventListener("input",e=>{if(e.target.closest("form[data-notebook-entry]")){notebookState.dirty=true;notebookState.dirtyIds.add(e.target.closest("form[data-notebook-entry]").dataset.notebookEntry);}if(e.target.closest("#detail-view")){state.dirty=true;saveDraft();}if(e.target.closest("#auto-form"))state.autoDirty=true;});
document.addEventListener("change",e=>{if(e.target.dataset.exportSelect){const id=e.target.dataset.exportSelect,item=state.exportItems.find(x=>x.id===id);if(e.target.checked){state.selected.add(id);if(item)state.selectedDetails.set(id,item);}else{state.selected.delete(id);state.selectedDetails.delete(id);}updateExportSelection();}if(e.target.id==="export-select-page"){state.exportItems.filter(item=>item.export_ready).forEach(item=>{if(e.target.checked){state.selected.add(item.id);state.selectedDetails.set(item.id,item);}else{state.selected.delete(item.id);state.selectedDetails.delete(item.id);}});document.querySelectorAll("[data-export-select]").forEach(box=>{if(!box.disabled)box.checked=e.target.checked;});updateExportSelection();}});
document.addEventListener("click",e=>{const b=e.target.closest("button");if(!b||b.type==="submit"&&b.form)return;action(async()=>{
  if(b.dataset.view){saveDraft();state.detailToken++;await showView(b.dataset.view);return;}
  if(b.id==="notebook-prev"||b.id==="notebook-next"){if(notebookState.dirty)throw Error("请先保存错题本处理记录，再翻页。");notebookState.page+=b.id==="notebook-prev"?-1:1;await loadNotebook();}
  if(b.dataset.copyGuide!==undefined){await navigator.clipboard.writeText(b.dataset.copyGuide);notice("录屏命令已复制。进入 A 或 B 的干净录屏目录后执行。");}
  if(b.dataset.delete){if(!window.confirm("仅从任务列表隐藏这道题？数据库、轨迹和产物记录都会保留。"))return;await api(`/${b.dataset.delete}/delete`,{});state.selected.delete(b.dataset.delete);state.selectedDetails.delete(b.dataset.delete);await loadList();notice("已从任务列表删除；数据库记录仍然保留。");}
  if(b.dataset.open){saveDraft();state.returnView=b.closest("#export-view")?"exports":"tasks";sessionStorage.setItem("pairwise-return-view",state.returnView);await openPair(b.dataset.open);}
  if(b.id==="new-pair"){$("#create-notice").hidden=true;const form=$("#create-form");if(!form.elements.project_directory.value){const defaults=await api("");form.elements.project_directory.value=defaults.default_directory;}$("#create-dialog").showModal();}
  if(b.id==="close-create")$("#create-dialog").close();
  if(b.id==="generate-draft"){const d=await api("/drafts",{});draftId=d.id;sessionStorage.setItem("pairwise-question-draft",draftId);await pollDraft();}
  if(b.id==="apply-draft"&&generatedDraft){const f=$("#create-form"),d=generatedDraft;for(const [k,value] of Object.entries({title:d.repo_name,repo_name:d.repo_name,task_type:"0-1 代码生成",project_category:d.category,difficulty:d.task_difficulty||"困难",language_framework:d.language_framework,prompt:d.first_prompt,verification_commands:(d.verification_commands||[]).join("\n"),snapshot_url:"",reproducibility:"已容器化，可一键起环境"})){f.elements.namedItem(k).value=value||"";}f.elements.topic_confirmed.checked=false;$("#draft-progress").textContent="已载入，请核对题面与难度后创建 Pair。";}
  if(b.dataset.difficultyReview){saveDraft();const p=await api(`/${b.dataset.difficultyReview}/difficulty-review`,{});renderDetail(p);notice("已加入难度复评队列。");}
  if(b.id==="back"){saveDraft();state.detailToken++;await showView(state.returnView);}
  if(b.id==="refresh")await loadList();
  if(b.id==="export-refresh")await loadExports();
  if(b.id==="stop-auto"){const f=$("#auto-form");const config=await api("/auto-refill",{enabled:false,target_count:Number(f.elements.target_count.value),project_directory:f.elements.project_directory.value,start_at:""});state.autoDirty=false;renderAuto(config);notice("已停止后续自动补题。已经启动的 A/B 不会被中断。");}
  if(b.id==="prev"||b.id==="next"){state.page+=b.id==="prev"?-1:1;await loadList();}
  if(b.id==="export-prev"||b.id==="export-next"){state.exportPage+=b.id==="export-prev"?-1:1;await loadExports();}
  if(b.id==="start-pair"){saveDraft();notice("正在建立共同 main，并同时准备 A、B 两个独立目录…");const p=await api(`/${state.pair.id}/start`,{});renderDetail(p);notice("A、B 已同时进入执行队列，将分别打开独立终端。");}
  if(b.dataset.retry){saveDraft();notice(`正在为 ${b.dataset.retry} 建立新的独立 Session…`);const p=await api(`/${state.pair.id}/start`,{side:b.dataset.retry});renderDetail(p);notice(`${b.dataset.retry} 已进入执行队列。`);}
  if(b.dataset.op){const data=await api(`/attempts/${b.dataset.attempt}/${b.dataset.op}`,{});renderDetail(data);if(b.dataset.op==="recording")notice("已加入录屏队列。系统会按顺序录制，完成核心演示后自动停止并保存原始视频。");}
  if(b.id==="preflight"){const r=await api(`/${state.pair.id}/preflight`,{});$("#preflight-result").innerHTML=`<p class="${r.eligible?"":"error"}">${r.eligible?"交付字段和本地证据核验通过。":r.issues.map(escape).join("<br>")}</p><small>${escape(r.notice)}</small>`;}
  if(b.id==="export-one"){if(state.dirty)throw Error("有未保存的修改，请先保存交付链接和评价");await exportIds([state.pair.id]);}
  if(b.id==="export-selected")await exportIds([...state.selected]);
  if(b.id==="solo-gsb-sync")await syncGsb();
  if(b.id==="solo-gsb-submit")await submitSelectedGsb();
});});
document.addEventListener("keydown",e=>{
  const tab=e.target.closest("#bank-view [data-bank-tab]");
  if(!tab||!["ArrowLeft","ArrowRight","Home","End"].includes(e.key))return;
  e.preventDefault();
  const tabs=[...document.querySelectorAll("#bank-view [data-bank-tab]")];
  const index=tabs.indexOf(tab);
  const next=e.key==="Home"?0:e.key==="End"?tabs.length-1:(index+(e.key==="ArrowRight"?1:-1)+tabs.length)%tabs.length;
  selectBankTab(tabs[next].dataset.bankTab);
  tabs[next].focus();
});
document.addEventListener("submit",e=>{e.preventDefault();const form=e.target;action(async()=>{
 if(form.id==="create-form"){const values=formValues(form);if(generatedDraft&&values.prompt===generatedDraft.first_prompt){values.difficulty_preflight={review:generatedDraft.difficulty_review,analysis:generatedDraft.complexity_analysis};}const p=await api("",values);$("#create-dialog").close();form.reset();renderDetail(p);location.hash=p.id;notice("题目已创建。点击“同时运行 A / B”后将冻结配置并建立两个目录。");}
 if(form.id==="auto-form"){const values=formValues(form);const config=await api("/auto-refill",{enabled:true,target_count:Number(values.target_count),project_directory:values.project_directory,start_at:values.start_at||""});state.autoDirty=false;renderAuto(config);notice(config.scheduled?`已预约自动补题：${config.detail}`:"自动补题已开始。生成合规题面后会直接创建 Pair 并同时运行 A/B。");}
 if(form.dataset.evidence){saveDraft();const p=await api(`/attempts/${form.dataset.evidence}/evidence`,formValues(form));localStorage.removeItem(`pairwise-evidence-${form.dataset.evidence}`);const draft=readDraft(p.id);if(draft){draft.revision=p.revision;draft.values.human_confirmed=false;localStorage.setItem(`pairwise-review-${p.id}`,JSON.stringify(draft));}renderDetail(p);notice("交付链接已保存，请核对后保存人工评价。");}
 if(form.id==="review-form"){const p=await api(`/${state.pair.id}/review`,{...formValues(form),revision:state.pair.revision});localStorage.removeItem(`pairwise-review-${p.id}`);renderDetail(p);notice("人工评价已保存。");}
});});
let searchTimer;$("#search").addEventListener("input",()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>action(async()=>{state.page=1;await loadList();}),250);});
let exportSearchTimer;$("#export-search").addEventListener("input",()=>{clearTimeout(exportSearchTimer);exportSearchTimer=setTimeout(()=>action(async()=>{state.exportPage=1;await loadExports();}),250);});
window.addEventListener("message",event=>{if(event.source!==window||event.origin!==window.location.origin)return;const message=event.data;if(!message||message.source!=="solo-qa-helper")return;if(message.type==="SOLO_QA_BRIDGE_READY"){state.soloBridgeReady=true;state.soloBridgeVersion=String(message.payload?.version||"");renderSoloControls();return;}if(!["SOLO_QA_BRIDGE_RESULT","SOLO_QA_BRIDGE_ERROR"].includes(message.type))return;const pending=state.soloRequests.get(message.requestId);if(!pending)return;clearTimeout(pending.timer);state.soloRequests.delete(message.requestId);if(message.type==="SOLO_QA_BRIDGE_ERROR")pending.reject(Error(message.payload?.error||"提交助手操作失败"));else pending.resolve(message.payload||{});});
window.addEventListener("beforeunload",e=>{if(state.dirty){saveDraft();e.preventDefault();e.returnValue="";}});
setInterval(()=>{if(state.busy||state.dirty||$("#create-dialog").open)return;action(async()=>{if(state.pair){const p=await api(`/${state.pair.id}`);if(!state.dirty&&JSON.stringify(p)!==JSON.stringify(state.pair))renderDetail(p);}else if(state.view==="exports")await loadExports();else await loadList();});},5000);
setInterval(pollDraft,5000);
pingSoloBridge();setInterval(()=>{if(!state.soloBridgeReady)pingSoloBridge();},3000);
action(async()=>{const hash=location.hash.slice(1);state.returnView=sessionStorage.getItem("pairwise-return-view")==="exports"?"exports":"tasks";if(hash==="exports"||hash==="notebook")await showView(hash);else{await loadList();if(/^[a-f0-9]{12}$/.test(hash))await openPair(hash);}});
