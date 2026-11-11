'use strict';
const $=s=>document.querySelector(s), API='/api/feedback';
const state={config:null,page:1,pages:1,task:null,run:null,dirty:false,editorKey:'',mediaKey:'',busy:false};
const names={queued:'排队中',starting:'正在启动',running:'首轮执行中',resuming:'原会话继续中',interrupted:'等待处理中断',failed:'执行故障',stopped:'已停止',archiving:'归档中',archive_failed:'归档失败',archived:'已归档'};
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const option=(value,label=value)=>`<option value="${esc(value)}">${esc(label)}</option>`;
const tag=(text,kind='')=>`<span class="tag ${kind}">${esc(text)}</span>`;
function notify(text){const toast=$('#toast');toast.textContent=text;toast.hidden=false;if(toast.showPopover)toast.showPopover();clearTimeout(notify.timer);notify.timer=setTimeout(()=>{if(toast.hidePopover)toast.hidePopover();toast.hidden=true;},7000);}
async function request(path,body,raw=false){
  const response=await fetch(API+path,body===undefined?{cache:'no-store'}:{method:'POST',headers:{'Content-Type':raw?'application/octet-stream':'application/json'},body:raw?body:JSON.stringify(body)});
  if(!response.ok){let data;try{data=await response.json();}catch{data={};}throw Error(data.error||`请求失败 (${response.status})`);}
  return response;
}
async function json(path,body){return (await request(path,body)).json();}
async function busy(button,work){if(state.busy)return;state.busy=true;if(button)button.disabled=true;updateExportSelection();try{await work();}catch(error){notify(error.message);}finally{state.busy=false;if(button?.isConnected)button.disabled=false;updateExportSelection();}}
function show(id){const dialog=$(id);if(!dialog.open)dialog.showModal();}
document.addEventListener('click',event=>{const close=event.target.closest('[data-close]');if(close&&!state.busy)close.closest('dialog').close();});
document.querySelectorAll('dialog').forEach(d=>d.addEventListener('cancel',event=>{if(state.busy)event.preventDefault();}));
function setOptions(select,values,blank){select.innerHTML=(blank===undefined?'':option('',blank))+values.map(v=>option(v)).join('');}
function renderProfile(){const p=state.config.profile;$('#profile-open').textContent=p.domain||'尚未选择 →';$('#profile-open').disabled=p.locked;$('#domain-notice').hidden=p.locked;}
async function configure(){state.config=await json('/config');renderProfile();setOptions($('#profile-form select'),Object.keys(state.config.domains),'请选择');setOptions($('#type-filter'),state.config.task_types,'全部任务类型');setOptions($('#export-type'),state.config.task_types,'全部任务类型');setOptions($('#task-form [name=task_type]'),state.config.task_types);setOptions($('#task-form [name=difficulty]'),state.config.difficulties);setOptions($('#task-form [name=reproducibility]'),state.config.reproducibility,'暂不填写');}
$('#profile-open').onclick=()=>show('#profile-dialog');
$('#profile-form').onsubmit=event=>{event.preventDefault();busy(event.submitter,async()=>{state.config.profile=await json('/profile',{domain:$('#profile-form select').value});renderProfile();$('#profile-dialog').close();notify('本期领域已固定，可开始建题');});};
$('#new-task').onclick=()=>{if(!state.config?.profile.domain){show('#profile-dialog');return;}$('#task-domain').value=state.config.profile.domain;setOptions($('#task-form [name=subdirection]'),state.config.domains[state.config.profile.domain]);show('#task-dialog');};
$('#task-form').onsubmit=event=>{event.preventDefault();busy(event.submitter,async()=>{
  const files=[...$('#task-files').files];if(files.length>10||files.some(f=>f.size>20*1024*1024))throw Error('每题最多 10 份附件，每份不超过 20 MB');
  const payload=Object.fromEntries(new FormData(event.target));let task=await json('/tasks',payload);
  try{for(const file of files)task=await (await request(`/tasks/${task.id}/attachments?name=${encodeURIComponent(file.name)}`,file,true)).json();}
  catch(error){notify(`题目已保存，部分附件未上传：${error.message}。可在详情补充。`);}
  event.target.reset();$('#task-dialog').close();await refreshList();await openTask(task.id);
});};
async function refreshList(){
  const token=state.listToken=(state.listToken||0)+1;
  const query=new URLSearchParams({page:state.page,query:$('#search').value,task_type:$('#type-filter').value});
  const data=await json('/tasks?'+query);if(token!==state.listToken)return;state.page=data.page;state.pages=data.pages;
  $('#total').textContent=data.total;$('#page-label').textContent=`第 ${data.page} / ${data.pages} 页 · 共 ${data.total} 题`;$('#prev').disabled=data.page<=1;$('#next').disabled=data.page>=data.pages;
  $('#tasks').innerHTML=data.items.length?data.items.map(t=>{const r=t.runs[t.runs.length-1];const status=r?names[r.status]||r.status:'待执行';return `<article class="task-row" role="button" tabindex="0" data-task="${t.id}"><div><div class="task-title">${esc(t.title)}</div><div class="task-meta">${esc(t.repo_name)} · ${esc(t.subdirection)}</div></div><div class="tags">${tag(t.task_type)}${tag(t.difficulty)}</div><div>${tag(status,r&&['failed','archive_failed'].includes(r.status)?'error':r?.status==='interrupted'?'warning':'')}<div class="task-meta">${esc(t.created_at)}</div></div><span class="muted">查看 →</span></article>`;}).join(''):'<div class="empty"><strong>从第一道专项题目开始</strong>保存原始题面，建立一份可追溯的完整执行记录。</div>';
}
$('#tasks').onclick=event=>{const row=event.target.closest('[data-task]');if(row)busy(null,()=>openTask(row.dataset.task));};
$('#tasks').onkeydown=event=>{if(event.key==='Enter'||event.key===' '){const row=event.target.closest('[data-task]');if(row){event.preventDefault();busy(null,()=>openTask(row.dataset.task));}}};
$('#refresh').onclick=event=>busy(event.target,refreshList);
$('#prev').onclick=()=>{state.page--;busy(null,refreshList);};$('#next').onclick=()=>{state.page++;busy(null,refreshList);};
$('#type-filter').onchange=()=>{state.page=1;busy(null,refreshList);};
$('#search').oninput=()=>{clearTimeout(state.searchTimer);state.searchTimer=setTimeout(()=>{state.page=1;refreshList().catch(e=>notify(e.message));},350);};
function renderTask(){const t=state.task;$('#detail-title').textContent=t.title;$('#original-prompt').textContent=t.prompt;
  $('#task-metadata').innerHTML=`${esc(t.domain)} / ${esc(t.subdirection)} · ${esc(t.task_type)} · ${esc(t.difficulty)} · ${esc(t.languages)}<br>初始快照：<a href="${esc(t.initial_snapshot)}" target="_blank" rel="noopener">${esc(t.initial_sha)}</a>`;
  $('#attachments').innerHTML=t.attachments.map(a=>`<div><a href="${API}/tasks/${t.id}/attachments/${a.id}">${esc(a.name)} ↓</a><span class="hint"> → ${esc(a.container_path)}</span></div>`).join('');
  $('#attachment-label').hidden=!!t.runs.length;$('#run-empty').hidden=!!t.runs.length;
  const select=$('#run-select');select.innerHTML=t.runs.map((r,i)=>option(r.id,`执行 ${i+1} · ${names[r.status]} · ${r.id}`)).join('');select.hidden=!t.runs.length;if(state.run)select.value=state.run.id;
}
async function openTask(id,runId){state.task=await json('/tasks/'+id);const candidate=runId||state.task.runs.at(-1)?.id;
  state.dirty=false;state.editorKey='';state.mediaKey='';state.run=candidate?await json('/runs/'+candidate):null;
  renderTask();renderRun();$('#interruption-reason').value='';show('#detail-dialog');$('#detail-dialog').scrollTop=0;
}
$('#run-select').onchange=()=>busy(null,()=>openTask(state.task.id,$('#run-select').value));
$('#attachment-file').onchange=event=>busy(event.target,async()=>{const file=event.target.files[0];if(!file)return;state.task=await(await request(`/tasks/${state.task.id}/attachments?name=${encodeURIComponent(file.name)}`,file,true)).json();renderTask();event.target.value='';notify('附件已保存');});
function actionButton(action,label,kind=''){return `<button data-action="${action}" class="${kind}">${esc(label)}</button>`;}
function renderRun(){const r=state.run;$('#review-section').hidden=!r;$('#events-panel').hidden=!r;$('#reason-label').hidden=!r||!['interrupted','running','resuming'].includes(r.status);
  if(!r){$('#run-live').innerHTML='';$('#evidence').innerHTML='';$('#run-controls').innerHTML=actionButton('start','开始单 Session 执行','primary');return;}
  $('#run-live').innerHTML=`<div>${tag(names[r.status]||r.status,['failed','archive_failed'].includes(r.status)?'error':r.status==='interrupted'?'warning':'')}</div><p class="status-detail">${esc(r.detail)}</p>${r.error?`<div class="error-message">${esc(r.error)}</div>`:''}<div class="evidence-grid"><div><b>首轮 SessionID · 提交定位</b><code>${esc(r.first_session_id)||'等待轨迹生成'}</code></div><div><b>首轮 TurnID / PromptID · 提交定位</b><code>${esc(r.first_turn_id)||'等待轨迹生成'}</code></div></div>${r.continuations.map(c=>`<div class="continuation">继续 ${c.number} · ${esc(c.status)} · 实际 ID：${esc(c.prompt_id)||'待轨迹确认'}<br>${esc(c.reason)}</div>`).join('')}`;
  let controls='';if(r.status==='interrupted')controls+=actionButton('resume','继续本任务','primary');if(['running','resuming'].includes(r.status))controls+=actionButton('interrupted','确认模型自行中断');
  if(r.status!=='archived'&&r.status!=='stopped')controls+=actionButton('stop','停止并保留现场','danger');
  if(['failed','stopped'].includes(r.status))controls+=actionButton('retry','独立新 Session 重跑');if(r.status==='archive_failed')controls+=actionButton('archive','重试归档','primary');
  if(['interrupted','failed','stopped','archive_failed'].includes(r.status))controls+=actionButton('sync','同步原会话轨迹');$('#run-controls').innerHTML=controls;
  const mediaKey=[r.id,r.status==='archived',r.artifact_sha,r.recording_sha256].join(':');
  if(state.mediaKey!==mediaKey){state.mediaKey=mediaKey;const task=state.task;
    $('#evidence').innerHTML=`<div class="evidence-grid"><div><b>原始完整轨迹</b>${r.trace_path?`<a href="${API}/runs/${r.id}/trace">下载 JSONL ↓</a>`:'待归档'}<p class="hint">SHA-256：${esc(r.trace_sha256)||'—'}</p></div><div><b>全部续写后的最终产物</b>${r.artifact_sha?`<a href="${esc(task.repo_url)}/commit/${r.artifact_sha}" target="_blank" rel="noopener">${esc(r.artifact_sha)} ↗</a>`:'待归档'}<p class="hint">保留模型中间提交，通过初始快照祖先关系核验。</p></div></div>${r.status==='archived'?`<div class="upload-box"><h3>完整运行录屏</h3><div class="actions"><button id="prepare-recording" type="button">准备最终快照的干净录屏目录</button></div><p id="recording-directory" class="hint"></p><p class="hint">实际运行当前最终产物并完整录制，包括失败过程。上传 90 秒内的视频，每份最多 500 MB。</p>${r.recording_path?`<video controls preload="metadata" src="${API}/runs/${r.id}/recording"></video><p class="hint">实际时长 ${Number(r.recording_seconds).toFixed(2)} 秒 · 绑定产物 ${esc(r.recording_artifact_sha.slice(0,12))}</p>`:''}<label>导入 ${r.recording_path?'替换':''} MP4 / MOV / M4V / WebM<input id="recording-file" type="file" accept=".mp4,.mov,.m4v,.webm"></label></div>`:''}`;
    const prepare=$('#prepare-recording');if(prepare)prepare.onclick=event=>busy(event.target,async()=>{const prepared=await json(`/runs/${r.id}/prepare-recording`,{});$('#recording-directory').textContent=`${prepared.directory} · ${prepared.note}`;});
    const upload=$('#recording-file');if(upload)upload.onchange=event=>busy(event.target,async()=>{const file=event.target.files[0];if(!file)return;notify('正在上传并核验真实视频时长…');state.run=await(await request(`/runs/${r.id}/recording?name=${encodeURIComponent(file.name)}`,file,true)).json();await refreshRun();notify('录屏已关联，请实际播放检查后再确认复核');});
  }
  $('#analysis-status').textContent=`分析状态：${r.review_confirmed&&r.analysis_status==='complete'?'已完成分析并复核':({pending:'待分析',running:'指定模型分析中',complete:'已生成草稿，待复核',stale:'证据已变化，需要重新分析',failed:'分析失败'})[r.analysis_status]||r.analysis_status}${r.analysis_error?' · '+r.analysis_error:''}`;
  $('#analyze').disabled=r.status!=='archived'||!r.recording_path||r.analysis_status==='running';$('#analyze').textContent=r.analysis_status==='running'?'正在分析全部证据…':'分析全部轨迹与产物';$('#analysis-download').href=`${API}/runs/${r.id}/analysis`;
  $('#citation-list').innerHTML=(r.analysis.evidence||[]).map(c=>`<div><b>${c.area==='result'?'结果':'过程'}</b> · ${esc(c.path)}:${esc(c.line)}<blockquote>${esc(c.quote)}</blockquote></div>`).join('')||'<p class="hint">分析完成后展示可核对的文件引用。</p>';
  const editorKey=[r.id,r.analysis_source,r.analysis_status,r.review_confirmed].join(':');
  if(!state.dirty&&state.editorKey!==editorKey){state.editorKey=editorKey;populateReview(r);}
  const checked=r.preflight;if(checked)renderPreflight(checked);
  $('#events').innerHTML=r.events.map(e=>`<div><span class="hint">${esc(e.created_at)}</span> ${esc(e.message)}</div>`).join('');
}
function populateReview(r){let draft=null;try{draft=JSON.parse(localStorage.getItem('feedback-review-'+r.id));}catch{}
  const source=draft&&draft.source===r.preflight?.evidence_source?draft.values:r;
  const form=$('#review-form');for(const key of ['delivery_score','result_feedback','process_feedback','other_issues','notes','trace_url','recording_url'])form.elements[key].value=source[key]??'';form.elements.recording_confirmed.checked=!!source.recording_confirmed;
  if(source!==r){state.dirty=true;notify('已恢复本机未保存的评价草稿');}
}
function reviewValues(){const form=$('#review-form');return {...Object.fromEntries(new FormData(form)),recording_confirmed:form.elements.recording_confirmed.checked};}
$('#review-form').oninput=()=>{state.dirty=true;try{localStorage.setItem('feedback-review-'+state.run.id,JSON.stringify({source:state.run.preflight?.evidence_source,values:reviewValues()}));}catch{}};
function renderPreflight(checked){$('#preflight').innerHTML=checked.ready_for_local_export?'<b>当前证据已复核，可导出本地文件。</b>':`<b>复核与导出前还需：</b><ul>${checked.issues.map(i=>`<li>${esc(i)}</li>`).join('')}</ul>`;$('#export-xlsx').disabled=!checked.ready_for_local_export;$('#export-json').disabled=!checked.ready_for_local_export;}
async function refreshRun(){if(!state.run)return;const id=state.run.id;const current=await json('/runs/'+id);if(state.run?.id!==id)return;state.run=current;renderRun();}
$('#run-controls').onclick=event=>{const button=event.target.closest('[data-action]');if(!button)return;busy(button,async()=>{const action=button.dataset.action;if(action==='start'||action==='retry'){const r=await json(`/tasks/${state.task.id}/${action}`,{});await openTask(state.task.id,r.id);}else{const reason=$('#interruption-reason').value;if(['resume','interrupted'].includes(action)&&!reason.trim())throw Error('请先填写实际中断情况，发送给模型的内容仅为“继续”');await json(`/runs/${state.run.id}/${action}`,{reason});await refreshRun();}await refreshList();});};
$('#analyze').onclick=event=>busy(event.target,async()=>{await json(`/runs/${state.run.id}/analyze`,{model:state.config.analysis_model,effort:state.config.analysis_effort});await refreshRun();});
async function saveReview(confirm){const payload={...reviewValues(),confirm,evidence_source:state.run.preflight?.evidence_source};await json(`/runs/${state.run.id}/review`,payload);localStorage.removeItem('feedback-review-'+state.run.id);state.dirty=false;state.editorKey='';await refreshRun();notify(confirm?'复核已确认，可以进行本地导出':'评价草稿已保存');}
$('#save-draft').onclick=event=>busy(event.target,()=>saveReview(false));$('#review-form').onsubmit=event=>{event.preventDefault();busy(event.submitter,()=>saveReview(true));};
$('#preflight-button').onclick=event=>busy(event.target,async()=>{const checked=await json(`/runs/${state.run.id}/preflight`,{});state.run.preflight=checked;renderPreflight(checked);notify(checked.ready_for_local_export?'证据核验通过':'核验完成，请补齐列出的项目');});
async function download(format,ids=[state.run.id]){const response=await request('/exports/'+format,{run_ids:ids});const url=URL.createObjectURL(await response.blob());const anchor=document.createElement('a');anchor.href=url;anchor.download='domain-feedback-local.'+format;document.body.append(anchor);anchor.click();anchor.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
$('#export-xlsx').onclick=event=>busy(event.target,()=>download('xlsx'));$('#export-json').onclick=event=>busy(event.target,()=>download('json'));
$('#environment').onclick=event=>busy(event.target,async()=>{show('#environment-dialog');$('#environment-result').textContent='正在检查本机依赖…';const result=await json('/environment');$('#environment-result').textContent=JSON.stringify(result,null,2);});
async function poll(){if(state.busy)return;try{if($('#detail-dialog').open&&state.run)await refreshRun();if(!$('#task-dialog').open&&!$('#profile-dialog').open){if(state.view==='exports')await refreshExports();else await refreshList();}}catch{/* Keep drafts and visible evidence when the service is restarting. */}}
configure().then(activateView).catch(e=>notify('初始化失败：'+e.message));setInterval(poll,5000);

// Export candidates use the same run detail, review and final evidence checks.
state.exportPage=1;state.exportItems=[];state.exportSelected=new Map();
async function activateView(){
  state.view=location.hash==='#exports'?'exports':'tasks';
  $('#task-view').hidden=state.view!=='tasks';$('#export-view').hidden=state.view!=='exports';
  for(const view of ['tasks','exports']){const link=$('#nav-'+view);if(view===state.view)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');}
  if(state.view==='exports')await refreshExports();else await refreshList();
}
window.addEventListener('hashchange',()=>{if(state.config)activateView().catch(e=>notify(e.message));});
function updateExportSelection(){
  if(!state.exportSelected)return;
  const selected=state.exportSelected,eligible=state.exportItems.filter(i=>i.preflight.ready_for_local_export);
  $('#export-selected').textContent=`已选 ${selected.size} 条`;
  $('#export-prev').disabled=state.busy||state.exportPage<=1;$('#export-next').disabled=state.busy||state.exportPage>=(state.exportPages||1);
  for(const id of ['export-clear','export-check','export-batch-json','export-batch-xlsx'])$('#'+id).disabled=state.busy||!selected.size;
  document.querySelectorAll('[data-export-select]').forEach(input=>{input.checked=selected.has(input.dataset.exportSelect);input.disabled=state.busy||!state.exportItems.find(i=>i.id===input.dataset.exportSelect)?.preflight.ready_for_local_export;});
  const page=$('#export-select-page'),count=eligible.filter(i=>selected.has(i.id)).length;
  page.disabled=state.busy||!eligible.length;page.checked=!!eligible.length&&count===eligible.length;page.indeterminate=count>0&&count<eligible.length;
}
async function refreshExports(){
  const token=state.exportToken=(state.exportToken||0)+1;
  const query=new URLSearchParams({page:state.exportPage,query:$('#export-search').value,task_type:$('#export-type').value,readiness:$('#export-readiness').value});
  const data=await json('/exports?'+query);if(token!==state.exportToken)return;
  state.exportPage=data.page;state.exportPages=data.pages;state.exportItems=data.items;
  $('#export-candidates').textContent=data.candidate_total;$('#export-ready').textContent=data.ready_count;$('#export-pending').textContent=data.pending_count;$('#export-total').textContent=data.total;
  $('#export-page').textContent=`第 ${data.page} / ${data.pages} 页 · 共 ${data.total} 条`;
  $('#export-prev').disabled=data.page<=1;$('#export-next').disabled=data.page>=data.pages;
  const open=new Set([...document.querySelectorAll('#export-rows details[open]')].map(d=>d.dataset.exportIssues));
  for(const item of data.items){if(!item.preflight.ready_for_local_export)state.exportSelected.delete(item.id);else if(state.exportSelected.has(item.id))state.exportSelected.set(item.id,item);}
  $('#export-rows').innerHTML=data.items.length?data.items.map(item=>{
    const ready=item.preflight.ready_for_local_export,issues=item.preflight.issues;
    const analysis=({complete:'分析完成',pending:'待分析',stale:'需重新分析',running:'分析中',failed:'分析失败'})[item.analysis_status]||item.analysis_status;
    return `<tr><td><input type="checkbox" data-export-select="${item.id}" aria-label="选择 ${esc(item.title)}" ${ready?'':'disabled'}></td><td><div class="task-title">${esc(item.title)}</div><div class="task-meta">${esc(item.repo_name)} · ${esc(item.domain)} / ${esc(item.subdirection)}</div><div class="tags">${tag(item.task_type)}${tag(item.difficulty)}</div></td><td><span class="hint">SessionID</span><code>${esc(item.first_session_id)||'待补齐'}</code><span class="hint">首轮 TurnID / PromptID</span><code>${esc(item.first_turn_id)||'待补齐'}</code></td><td><p>${item.has_trace?'轨迹已关联':'轨迹待补齐'} · ${item.has_recording?'录屏 '+Number(item.recording_seconds).toFixed(2)+' 秒':'录屏待补齐'}</p><p>${esc(analysis)} · ${item.review_confirmed?'已确认':'待复核'}</p><p class="hint">交付完整性：${esc(item.delivery_score??'待评价')}</p></td><td>${tag(ready?'可导出':'待补齐',ready?'':'warning')}${issues.length?`<details data-export-issues="${item.id}" ${open.has(item.id)?'open':''}><summary>${issues.length} 项待补齐</summary><ul>${issues.map(i=>`<li>${esc(i)}</li>`).join('')}</ul></details>`:''}</td><td><button data-export-open="${item.id}" data-task-id="${item.task_id}">题面与资料</button></td></tr>`;
  }).join(''):`<tr><td colspan="6" class="empty"><strong>${data.candidate_total?'没有符合筛选条件的执行':'暂无已归档执行'}</strong>${data.candidate_total?'可以调整导出状态或搜索条件。':'任务完成并归档后会出现在这里，资料尚未补齐的记录也会保留。'}</td></tr>`;
  updateExportSelection();
}
$('#export-rows').addEventListener('click',event=>{const button=event.target.closest('[data-export-open]');if(button)busy(button,()=>openTask(button.dataset.taskId,button.dataset.exportOpen));});
$('#export-rows').addEventListener('change',event=>{
  const input=event.target.closest('[data-export-select]');if(!input)return;
  const item=state.exportItems.find(i=>i.id===input.dataset.exportSelect);
  if(!state.busy&&item?.preflight.ready_for_local_export){if(input.checked){if(state.exportSelected.size>=200)notify('每批最多选择 200 条');else state.exportSelected.set(item.id,item);}else state.exportSelected.delete(item.id);}
  updateExportSelection();
});
$('#export-select-page').onchange=event=>{
  if(!state.busy)for(const item of state.exportItems.filter(i=>i.preflight.ready_for_local_export)){if(!event.target.checked)state.exportSelected.delete(item.id);else if(state.exportSelected.size<200)state.exportSelected.set(item.id,item);else{notify('每批最多选择 200 条');break;}}
  updateExportSelection();
};
$('#export-clear').onclick=()=>{state.exportSelected.clear();$('#export-check-result').hidden=true;updateExportSelection();};
$('#export-refresh').onclick=event=>busy(event.target,refreshExports);
$('#export-prev').onclick=event=>busy(event.target,async()=>{state.exportPage--;await refreshExports();});
$('#export-next').onclick=event=>busy(event.target,async()=>{state.exportPage++;await refreshExports();});
for(const id of ['export-type','export-readiness'])$('#'+id).onchange=()=>{state.exportPage=1;refreshExports().catch(e=>notify(e.message));};
$('#export-search').oninput=()=>{clearTimeout(state.exportSearchTimer);state.exportSearchTimer=setTimeout(()=>{state.exportPage=1;refreshExports().catch(e=>notify(e.message));},350);};
$('#export-check').onclick=event=>busy(event.target,async()=>{
  const data=await json('/exports/preflight',{run_ids:[...state.exportSelected.keys()]});
  const result=$('#export-check-result');result.hidden=false;
  result.innerHTML=data.ready_for_local_export?'<b>所选记录证据核验通过，可以批量导出。</b>':`<b>以下记录需补齐，已移出选择：</b><ul>${data.items.filter(i=>!i.ready_for_local_export).map(i=>{const title=state.exportSelected.get(i.run_id)?.title||i.run_id;state.exportSelected.delete(i.run_id);return `<li>${esc(title)}：${i.issues.map(esc).join('；')}</li>`;}).join('')}</ul>`;
  await refreshExports();
});
for(const format of ['json','xlsx'])$('#export-batch-'+format).onclick=event=>busy(event.target,async()=>{const ids=[...state.exportSelected.keys()];await download(format,ids);notify(`已导出 ${ids.length} 条专项记录`);});
