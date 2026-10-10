(() => {
'use strict';
const $ = (q, root=document) => root.querySelector(q);
const $$ = (q, root=document) => [...root.querySelectorAll(q)];
const launchToken = new URLSearchParams(location.hash.slice(1)).get('client_token');
let token = launchToken || '';
try {if(launchToken)localStorage.setItem('boss-client-token',launchToken);else token=localStorage.getItem('boss-client-token')||'';}catch{}
if(launchToken)history.replaceState(null,'',location.pathname+location.search);
const labels = {draft:'待生成',pending:'待审批',approved:'已批准',rejected:'已拒绝',sent:'已发送',sending:'待核实',not_sent:'未提交',uncertain:'待核实',failed:'失败',skipped:'已跳过'};
const kinds = {login:'旧版独立登录',connect:'连接现有Chrome',check:'检查Chrome连接',prepare:'生成岗位批次',draft:'生成专属招呼',send:'限量发送',review:'岗位审批',edit:'文案修改',settings:'筛选条件'};
let state, batch, selected = new Set(), focused = null, statusFilter='all', detailTab='summary', editing=false, settingsDirty=false, lastOperation='', currentView='jobs', polling=false;
let sendSnapshot=[], sendBatchId=null, sendRequestPending=false;
let settingsVersion=null,recheckRevision=null,recheckSending=false;
const extraCities=new Set();
kinds.resume='继续岗位批次';
kinds.profile='个人资料';kinds.check_now='手动复查一次';
const approvedForSending=item=>['approved','not_sent','uncertain'].includes(item.status);
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon = name => `<i data-lucide="${name}"></i>`;
function icons(){window.lucide?.createIcons({attrs:{'aria-hidden':'true'}});}
function date(time, short=false){return time?new Date(time*1000).toLocaleString('zh-CN',short?{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}:{hour12:false}):'暂无';}
function toast(message){const t=$('#toast');t.textContent=message;t.hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>t.hidden=true,5000);}
async function api(path, body){
  const options=body===undefined?{headers:{'X-Client-Token':token}}:{method:'POST',headers:{'Content-Type':'application/json','X-Client-Token':token},body:JSON.stringify(body)};
  const controller=new AbortController();
  const timer=body===undefined||path==='/api/check_now'?setTimeout(()=>controller.abort(),10000):null;
  try{
    const response=await fetch(path,{...options,signal:controller.signal});
    const data=await response.json();
    if(!response.ok)throw new Error(data.error||'请求未完成');
    return data;
  }catch(error){
    if(controller.signal.aborted){const timeout=new Error(body===undefined?'本机状态读取超时，正在重新连接':'本机未及时确认复查请求；将核对任务状态，不重复提交');timeout.code='CLIENT_TIMEOUT';throw timeout;}
    throw error;
  }finally{if(timer!==null)clearTimeout(timer);}
}
const busy=()=>state?.operation.status==='running';
const profileEditor=window.createProfileEditor({api,toast,icons,escape,busy});
const current=()=>batch?.items.find(item=>item.number===focused);
const selectable=item=>['draft','pending','approved','not_sent','uncertain'].includes(item.status);
function filtered(){const term=$('#search').value.toLowerCase().trim();return (batch?.items||[]).filter(item=>(statusFilter==='all'||item.status===statusFilter||(statusFilter==='approved'&&item.status==='not_sent')||(statusFilter==='uncertain'&&item.status==='sending'))&&(!term||`${item.title} ${item.company} ${item.city}`.toLowerCase().includes(term)));}
async function loadBatch(id){if(!id){batch=null;focused=null;selected.clear();renderJobs();return;}batch=await api(`/api/batch?id=${encodeURIComponent(id)}`);selected=new Set([...selected].filter(n=>batch.items.some(j=>j.number===n&&selectable(j))));if(!batch.items.some(j=>j.number===focused))focused=batch.items[0]?.number??null;renderJobs();}
async function refresh(){if(polling)return;polling=true;try{const old=state;state=await api('/api/state');$('#connection-text').textContent='本机已连接';renderShell();const available=state.batches.filter(b=>!b.source);const key=`${state.operation.started_at||''}:${state.operation.batch_id||''}`;let id=batch?.batch_id;if(state.operation.kind==='prepare'&&state.operation.batch_id&&key!==lastOperation){id=state.operation.batch_id;lastOperation=key;selected.clear();}if(!id||!available.some(b=>b.id===id))id=available[0]?.id;const signature=b=>JSON.stringify(b?.batches||[])+JSON.stringify(b?.operation||{});if(!batch||id!==batch.batch_id||(!editing&&signature(old)!==signature(state)))await loadBatch(id);else renderControls();if(currentView==='events')await renderEvents();}catch(error){$('#connection-text').textContent='本机连接中断';toast(error.message);}finally{polling=false;}}
function renderShell(){const b=state.browser;const browserLabels={ready:'Chrome已连接',needs_connection:'未连接',needs_login:'BOSS待登录',blocked:'自动化已暂停'};$('#session-state').textContent=browserLabels[b.status]||'连接待检查';$('#session-state').className=`session-state ${b.status==='ready'?'ready':''}`;$('#connect span').textContent=b.status==='blocked'?'恢复连接':'连接 Chrome';$('#model').textContent=state.model;const op=state.operation;const notice=$('#notice');const error=op.status==='error'?op.error?.message:'';notice.hidden=!error&&b.status==='ready';$('#notice-text').textContent=error||b.message||'请连接已登录BOSS的Chrome';$('#taskbar').hidden=!busy();$('#task-label').textContent=kinds[op.kind]||'任务';$('#task-message').textContent=op.logs?.at(-1)?.message||'准备中';$('#batch-select').innerHTML=state.batches.filter(b=>!b.source).map(b=>`<option value="${escape(b.id)}" ${batch?.batch_id===b.id?'selected':''}>${escape(date(b.created_at,true))} · ${b.total} 个岗位</option>`).join('')||'<option value="">暂无批次</option>';$('#delivery-notice').hidden=state.delivery?.enabled===true;$('#delivery-notice-text').textContent=state.delivery?.message||'正文发送尚未验证，自动发送已暂停';if(!settingsDirty)renderSettings();renderControls();icons();}
function renderCollectionState(){
  const browser=state.browser, riskWait=Math.max(0,Math.ceil((browser.blocked_until||0)-Date.now()/1000));
  $('#connect').title='检查现有Chrome与BOSS登录状态';
  const diagnostic=browser.pause_diagnostic||{}, sourceNames={api_response:'接口返回',page_verification:'本地可见验证组件检查',local_guard:'本地检测（缺少组件证据）',unknown:'来源未记录（旧记录或旧通道）'};
  $('#pause-details').hidden=browser.status!=='blocked';
  const manual=state.manual_recheck;
  $('#skip-wait').hidden=!manual?.supported||!riskWait;
  $('#skip-wait').disabled=busy()||!manual?.available;
  $('#skip-wait').title=manual?.used?'本轮已提前复查，仍需保留暂停':'跳过本机剩余等待，手动复查一次，不自动继续采集或发送';
  const collectionWait=state.collection?.remaining_seconds||0;
  if(state.collection){$('#collection-budget').textContent=`采集读取：每 ${state.collection.window_seconds/60} 分钟最多 ${state.collection.request_limit} 次`;
    $('#collection-interval').textContent=`读取间隔：至少 ${state.collection.interval_seconds} 秒`;}
  if(browser.status==='blocked'){
    if(browser.search_recovery_required||diagnostic.endpoint==='/wapi/zpgeek/search/joblist.json'){
      $('#connect span').textContent='复查搜索';
      $('#connect').title='检查登录后，仅复查上次失败的搜索页一次；成功后仍需手动继续本批';
    }
    $('#notice').hidden=false;
    $('#session-state').textContent=diagnostic.source==='api_response'?'接口访问暂停':diagnostic.source==='page_verification'?'页面待处理':'自动化已暂停';
    $('#notice-text').textContent=browser.message;
    if(busy()&&(state.operation.kind==='draft'||state.operation.phase==='drafting_saved'))$('#notice-text').textContent+=' 当前仅用已保存JD生成文案，不访问BOSS。';
    if(riskWait)$('#notice-text').textContent+=` 本机保护性等待剩余 ${Math.ceil(riskWait/60)} 分钟，非BOSS官方处罚倒计时。`;
    const fields=[['来源',sourceNames[diagnostic.source]||sourceNames.unknown],['触发时接口请求',diagnostic.request_sent===true?'已发起，不代表招呼已送达':diagnostic.request_sent===false?'未发起（本地阻止）':'未记录'],['暂停前页面检查',diagnostic.page_verification===true?'检测到可见验证组件':diagnostic.page_verification===false?'当时未检测到可见验证组件':'未记录'],['接口返回码',diagnostic.request_sent===false?'不适用（未请求）':diagnostic.response_code??'未记录'],['HTTP状态',diagnostic.request_sent===false?'不适用（未请求）':diagnostic.http_status??'未记录'],['接口路径',diagnostic.endpoint||'未记录'],['返回信息',diagnostic.response_message||'未记录']];
    if(diagnostic.page_evidence?.length)fields.push(['组件依据',diagnostic.page_evidence.map(item=>item.cue).join('；')]);
    if(diagnostic.page_path)fields.push(['请求所在页',diagnostic.page_path]);
    if(diagnostic.transport==='native-page')fields.push(['采集方式','BOSS网页搜索与岗位详情']);
    $('#pause-fields').innerHTML=fields.map(([name,value])=>`<dt>${escape(name)}</dt><dd>${escape(value)}</dd>`).join('');
  }else if(collectionWait&&state.operation.status!=='error'){
    $('#notice').hidden=false;
    $('#notice-text').textContent=`本机读取预算等待中，剩余 ${Math.ceil(collectionWait/60)} 分钟。`;
  }
  if(busy()&&(state.operation.kind==='draft'||state.operation.phase==='drafting_saved'))$('#task-label').textContent='DeepSeek文案（已保存JD）';
  if(busy()&&state.operation.phase==='checking_search')$('#task-label').textContent='复查搜索接口';
  if(batch?.stop_reason==='target_reached_local'&&!busy()&&browser.status!=='blocked'&&!['error','partial'].includes(state.operation.status)){$('#notice').hidden=false;$('#notice-text').textContent='本批已用近期完整资料补齐，未新增BOSS请求。';}
  const waitUntil=state.operation.phase==='waiting_budget'?state.operation.wait_until:(state.operation.batch_id===batch?.batch_id&&batch?.stats.phase==='waiting_budget'?batch.stats.wait_until:0);
  if(busy()&&waitUntil&&browser.status!=='blocked'){
    const remaining=Math.max(0,Math.ceil(waitUntil-Date.now()/1000));
    $('#notice').hidden=false;$('#notice-text').textContent=`本机读取预算等待中，剩余 ${Math.floor(remaining/60)} 分 ${remaining%60} 秒；之后继续同一任务。`;
    $('#task-label').textContent='等待后继续采集';
  }
  if(busy()&&['prepare','resume'].includes(state.operation.kind)&&state.operation.completion_first){const elapsed=Math.max(0,Math.floor(Date.now()/1000-state.operation.started_at));$('#task-label').textContent+=` · 已运行 ${Math.floor(elapsed/60)}:${String(elapsed%60).padStart(2,'0')}`;}
  if(state.operation.status==='partial'&&browser.status!=='blocked'){$('#notice').hidden=false;$('#notice-text').textContent=state.operation.shortfall_message;}
  renderSearchCheck(riskWait);
  if(batch){
    const parts=[], shortfall=batch.stats.quota_shortfall;
    if(shortfall&&(shortfall.campus||shortfall.internship))parts.push(`尚缺校招/应届 ${shortfall.campus} 个、实习 ${shortfall.internship} 个`);
    if(batch.stop_reason?.includes('risk'))parts.push('采集曾因访问检查暂停');
    if(batch.stop_reason==='request_limit')parts.push('达到本机读取上限');
    if(batch.stop_reason==='time_limit')parts.push('达到本次筛选时限');
    if(Number.isFinite(batch.stats.network_requests))parts.push(`累计读取尝试 ${batch.stats.network_requests} 次`, `缓存命中 ${(batch.stats.search_cache_hits||0)+(batch.stats.detail_cache_hits||0)} 次`);
    if(Number.isFinite(batch.stats.last_run_requests))parts.push(`上次采集读取 ${batch.stats.last_run_requests} 次`);
    if(batch.stats.search_recovery_checks)parts.push(`搜索复查 ${batch.stats.search_recovery_checks} 次`);
    if(Number.isFinite(batch.stats.effective_interval_seconds))parts.push(`读取间隔至少 ${batch.stats.effective_interval_seconds} 秒`);
    if(batch.stats.local_seeded)parts.push(`本地复核 ${batch.stats.local_seeded} 个`);
    if(batch.stats.resume_count)parts.push(`断点续采 ${batch.stats.resume_count} 次`);
    if(batch.stats.read_retries)parts.push(`连接重试 ${batch.stats.read_retries} 次`);
    if(Number.isFinite(batch.stats.screening_seconds))parts.push(`筛选 ${Math.ceil(batch.stats.screening_seconds)} 秒`);
    if(Number.isFinite(batch.stats.preview_seconds))parts.push(`总用时 ${Math.ceil(batch.stats.preview_seconds)} 秒`);
    $('#batch-meta').textContent=parts.join(' · ')||`批次 ${batch.batch_id.slice(0,8)}`;
  }
  const resume=$('#resume');
  resume.hidden=!batch?.collection_resume?.available;
  resume.disabled=busy()||browser.status!=='ready'||riskWait>0;
  resume.title=browser.status!=='ready'?'先手动恢复Chrome连接，再继续本批':batch?.collection_resume?.mode==='legacy_rescan'?'旧批次没有断点，按原条件重新扫描、去重并补齐，不发送':'从已保存搜索页继续，保留原条件和审批，不发送';
  return {riskWait,collectionWait};
}
function renderSearchCheck(riskWait){
  const op=state.operation,browser=state.browser;
  const checking=op.search_check||op.kind==='check_now';
  const result=checking&&op.finished_at?op:(browser.status==='blocked'?browser.last_search_check:null);
  const legacyUsed=browser.status==='blocked'&&state.manual_recheck?.used;
  $('#recheck-result').hidden=busy()||(!result&&!legacyUsed);
  if(busy()&&checking){
    const elapsed=Math.max(0,Math.floor(Date.now()/1000-op.started_at));
    const waiting=op.phase==='waiting_budget';
    $('#task-label').textContent=(waiting?'复查等待读取间隔':op.phase==='checking_search'?'正在复查搜索':'正在检查登录')+` · 已用 ${elapsed} 秒`;
    if(waiting){
      const remaining=Math.max(0,Math.ceil((op.wait_until||0)-Date.now()/1000));
      $('#task-message').textContent=`剩余 ${Math.floor(remaining/60)} 分 ${remaining%60} 秒，之后只复查一次；当前未请求搜索接口。`;
    }else $('#task-message').textContent=op.phase==='checking_search'?'等待上次失败搜索页的返回结果；不会续采或发送。':'等待 Chrome 连接与登录检查结果。';
    return;
  }
  if(!result&&!legacyUsed)return;
  let title='本轮提前复查已使用',message='当前仍未确认搜索恢复，没有自动续采或发送。';
  if(result){
    const error=result.error||{},diagnostic=error.diagnostic||{};
    if(error.code==='BROWSER_SEARCH_PAGE_REQUIRED'){title='复查未发起：需要职位搜索页';message=error.message;}
    else if(result.status==='complete'){title='复查完成：搜索已恢复';message='上次失败的搜索页已返回有效列表，尚未继续采集或发送。';}
    else if(result.status==='stopped'){title='复查已停止';message='本次检查已结束；搜索仍未确认恢复，没有自动重试。';}
    else if(diagnostic.source==='api_response'&&diagnostic.endpoint==='/wapi/zpgeek/search/joblist.json'){
      title='复查结束：搜索仍被拒绝';message=`BOSS 搜索接口再次返回${diagnostic.response_code==null?'错误':` code ${diagnostic.response_code}`}。本次复查已经结束，未继续采集或发送。`;
    }else{title='复查结束：未确认恢复';message=error.message||'未获得有效的搜索检查结果，没有自动重试。';}
  }
  $('#recheck-title').textContent=title;
  $('#recheck-message').textContent=message;
  const elapsed=result&&Number.isFinite(result.started_at)&&Number.isFinite(result.finished_at)?` · 用时 ${Math.max(1,Math.ceil(result.finished_at-result.started_at))} 秒`:'';
  $('#recheck-time').textContent=result?.finished_at?date(result.finished_at)+elapsed:'';
  $('#recheck-next').textContent=browser.status==='blocked'?(riskWait?`本机等待还剩 ${Math.floor(riskWait/60)} 分 ${riskWait%60} 秒；结束后可手动复查搜索。已保存岗位仍可查看和审批。`:'本机等待已结束，可点击右上角“复查搜索”；不会自动执行。'):result?.status==='complete'?'可手动继续本批；未自动启动任何任务。':'';
  if(result?.error?.code==='BROWSER_SEARCH_PAGE_REQUIRED')$('#recheck-next').textContent='此前的接口暂停仍保留，尚未再次请求BOSS搜索接口。';
}
function renderControls(){profileEditor.controls();const running=busy();const pause=renderCollectionState();for(const id of ['prepare','connect','check-browser','save-settings','draft','open-send'])$('#'+id).disabled=running;$('#prepare').disabled=running;$('#connect').disabled=running||pause.riskWait>0;$('#check-browser').disabled=running||pause.riskWait>0;$('#open-send').title=state.delivery?.enabled===true?'':(state.delivery?.message||'发送已暂停');$('#open-send').disabled=running||state.browser.status!=='ready'||pause.riskWait>0||state.delivery?.enabled!==true||!batch?.items.some(approvedForSending);$('#draft').hidden=!batch?.items.some(i=>i.status==='draft');const picked=(batch?.items||[]).filter(i=>selected.has(i.number));$('#bulk-approve').disabled=running||!picked.length||picked.some(i=>!['pending','approved','not_sent'].includes(i.status));$('#bulk-reject').disabled=running||!picked.length;$('#selected-count').textContent=picked.length?`已选 ${picked.length} 个`:'未选择';const allowed=filtered().filter(selectable);$('#select-all').checked=allowed.length>0&&allowed.every(i=>selected.has(i.number));$('#select-all').indeterminate=allowed.some(i=>selected.has(i.number))&&!$('#select-all').checked;$('#select-all').disabled=running||!allowed.length;}
function renderJobs(){const items=batch?.items||[];const counts=batch?.counts||{};const campus=items.filter(i=>i.recruitment_kind==='campus').length,intern=items.filter(i=>i.recruitment_kind==='internship').length;const quota=state?.settings.rules.recruitment_quotas||{campus:5,internship:5};$('#metrics').innerHTML=[['本批岗位',items.length,batch && batch.params.count !== state?.settings.count ? '历史批次' : `目标 ${state?.settings.count||10}`],['校招/应届',campus,`新批次 ${quota.campus}`],['实习岗位',intern,`新批次 ${quota.internship}`],['待审 / 已批',`${counts.pending||0} / ${(counts.approved||0)+(counts.not_sent||0)}`,'']].map(([label,value,sub],i)=>`<div class="metric"><div class="metric-label">${label}</div><div class="metric-value ${i===3?'green':''}">${value}<span>${sub}</span></div></div>`).join('');if(batch){$('#batch-select').value=batch.batch_id;const missing=batch.stats.quota_shortfall;$('#batch-meta').textContent=missing&&(missing.campus||missing.internship)?`尚缺校招/应届 ${missing.campus} 个、实习 ${missing.internship} 个`:(batch.stop_reason?.includes('risk')?'采集曾因访问检查暂停':'')||`批次 ${batch.batch_id.slice(0,8)}`;}else $('#batch-meta').textContent='暂无岗位';$('#status-tabs').innerHTML=[['all','全部',items.length],['pending','待审批',counts.pending||0],['approved','已批准',(counts.approved||0)+(counts.not_sent||0)],['rejected','已拒绝',counts.rejected||0],['uncertain','待核实',(counts.uncertain||0)+(counts.sending||0)],['sent','已发送',counts.sent||0]].map(([s,l,n])=>`<button class="status-tab ${s===statusFilter?'active':''}" data-status="${s}">${l} ${n}</button>`).join('');renderRows();renderDetail();renderControls();icons();}
function renderRows(){$('#job-rows').innerHTML=filtered().map(item=>`<article class="job-row ${item.number===focused?'active':''}" data-number="${item.number}" tabindex="0" aria-label="${escape(item.title)}"><input type="checkbox" data-select="${item.number}" aria-label="选择岗位 ${item.number}" ${selected.has(item.number)?'checked':''} ${!selectable(item)||busy()?'disabled':''}><div><div class="row-heading"><div class="job-title">${escape(item.title)}</div><span class="salary">${escape(item.salary)}</span></div><div class="row-company">${escape(item.company)}</div><div class="row-meta"><span>#${item.number}</span><span>${escape(item.city)}</span><span class="tag">${item.recruitment_kind==='internship'?'实习':'校招/应届'}</span><span class="tag status-tag status-${item.status}">${labels[item.status]||item.status}</span></div></div></article>`).join('')||`<div class="empty">${icon('inbox')}<h3>${batch?'没有匹配的岗位':'暂无岗位批次'}</h3></div>`;icons();}
function renderDetail(){const item=current();const target=$('#detail');if(!item){target.innerHTML=`<div class="empty">${icon('file-text')}<h3>暂无岗位详情</h3></div>`;icons();return;}const warnings=[...new Set([...(item.screening_notes||[]),...(item.warnings||[])])];target.innerHTML=`<button class="icon-button mobile-back" id="back-list" title="返回列表" aria-label="返回列表">${icon('arrow-left')}</button><div class="detail-heading"><div><h2>${escape(item.title)}</h2><div class="detail-company">${escape(item.company)}</div></div><span class="detail-salary">${escape(item.salary)}</span></div><div class="detail-meta"><span class="tag">${escape(item.city)}</span><span class="tag">${escape(item.education||'学历待确认')}</span><span class="tag">${escape(item.recruitment_label||(item.recruitment_kind==='internship'?'实习':(item.cohort_confirmed===false?'校招（届别待核实）':(batch?.params.rules.graduation_year?String(batch.params.rules.graduation_year)+' 届校招':'校招（届别待配置）'))))}</span><span class="tag status-${item.status}">${labels[item.status]}</span><a href="https://www.zhipin.com/job_detail/${encodeURIComponent(item.job_id)}.html" target="_blank" rel="noopener noreferrer">原始岗位 ${icon('external-link')}</a></div><div class="detail-tabs">${[['summary','摘要与招呼'],['jd','完整 JD'],['record','审批记录']].map(([key,label])=>`<button class="detail-tab ${detailTab===key?'active':''}" data-detail-tab="${key}">${label}</button>`).join('')}</div><div id="detail-body"></div><div class="detail-bottom"><button id="reject-one" class="button danger-text" ${!selectable(item)||busy()?'disabled':''}>${icon('x')}拒绝此岗位</button><button id="approve-one" class="button primary" ${!['pending','approved','not_sent'].includes(item.status)||busy()?'disabled':''}>${icon('check')}批准此岗位</button></div>`;const body=$('#detail-body');if(detailTab==='jd'){body.innerHTML=`<div class="detail-section"><div class="section-head"><h3>职位描述与任职要求</h3></div><pre class="jd">${escape(item.description)}</pre></div><div class="detail-section"><h3>筛选依据</h3><ul class="evidence">${(item.rule_matches||[]).map(x=>`<li>${escape(x)}</li>`).join('')}</ul></div>`;}else if(detailTab==='record'){body.innerHTML=`<dl class="record-grid"><dt>当前状态</dt><dd>${labels[item.status]}</dd><dt>更新时间</dt><dd>${escape(date(item.updated_at))}</dd><dt>发送记录</dt><dd>${escape(item.error||(['sent'].includes(item.status)?'已核对消息记录中的岗位及招呼原文':'暂无确认的发送结果'))}</dd><dt>本机修改</dt><dd>${(item.draft_edits||[]).length} 次</dd></dl>`;}else{body.innerHTML=`${item.error?`<p class="notice">${escape(item.error)}</p>`:''}<section class="detail-section"><div class="section-head"><h3>岗位摘要</h3></div><p class="summary">${escape(item.summary||'待生成摘要')}</p></section><section class="detail-section"><div class="section-head"><h3>专属招呼</h3><span><button id="copy-greeting" class="icon-button" title="复制招呼，不发送" aria-label="复制招呼" ${!item.greeting?'disabled':''}>${icon('copy')}</button><button id="edit-greeting" class="icon-button" title="编辑招呼，保存后重新审批" aria-label="编辑招呼" ${!['pending','approved','not_sent'].includes(item.status)||busy()?'disabled':''}>${icon('pencil')}</button></span></div><div class="greeting-box"><textarea id="greeting" aria-label="专属招呼" maxlength="300" readonly>${escape(item.greeting||'')}</textarea><div class="greeting-footer"><span id="greeting-count">${(item.greeting||'').length} / 300</span><div id="edit-actions" hidden><button id="cancel-edit" class="button small">取消</button><button id="save-greeting" class="button small">${icon('save')}保存并重新审批</button></div></div></div></section><section class="detail-section"><div class="section-head"><h3>简历依据</h3></div><ul class="evidence">${(item.resume_evidence||[]).map(x=>`<li>${escape(x)}</li>`).join('')||'<li>待生成</li>'}</ul></section>${warnings.length?`<section class="detail-section"><div class="section-head"><h3>需核实事项</h3></div><ul class="warnings">${warnings.map(x=>`<li>${escape(x)}</li>`).join('')}</ul></section>`:''}`;}icons();}
async function review(approve,reject){if(!batch)return;const uncertain=batch.items.filter(i=>reject.includes(i.number)&&i.status==='uncertain');if(uncertain.length&&!window.confirm(`拒绝这 ${uncertain.length} 个待核实岗位并停止后续发送？\n这不会撤回可能已发送的消息，原发送记录会保留。`))return;const wanted=new Set([...approve,...reject]);const expected_updates=Object.fromEntries(batch.items.filter(i=>wanted.has(i.number)).map(i=>[i.number,i.updated_at]));await api('/api/review',{batch_id:batch.batch_id,approve,reject,expected_updates});selected.clear();toast(`已批准 ${approve.length} 个，拒绝 ${reject.length} 个`);await loadBatch(batch.batch_id);await refresh();}
function renderSettings(){
  const p=state.settings,r=p.rules,form=$('#settings-form');
  settingsVersion=state.settings_revision;
  for(const [key,value] of Object.entries({campus_count:r.recruitment_quotas?.campus??5,internship_count:r.recruitment_quotas?.internship??5,min_salary:r.min_salary,min_daily_salary:r.min_daily_salary,graduation_year:r.graduation_year??''})){form.elements[key].value=value??'';}
  const cities=[...new Set([...r.city,...extraCities])];
  $('#city-options').innerHTML=cities.map(city=>'<label><input type="checkbox" name="cities" value="'+escape(city)+'" '+(r.city.includes(city)?'checked':'')+'>'+escape(city)+'</label>').join('');
  $('#new-city').innerHTML='<option value="">选择城市</option>'+(state.cities||[]).filter(city=>!cities.includes(city)).map(city=>'<option value="'+escape(city)+'">'+escape(city)+'</option>').join('');
  $('#role-options').innerHTML=Object.entries(state.roles).map(([key,label])=>'<label><input type="checkbox" name="roles" value="'+escape(key)+'" '+(r.target_roles.includes(key)?'checked':'')+'>'+escape(label)+'</label>').join('');
  for(const key of ['custom_role_keywords','require','exclude','exclude_company'])form.elements[key].value=(r[key]||[]).join('\n');
  form.elements.history_dedupe.value=r.history_dedupe||'job';
  for(const key of ['allow_unspecified_cohort','allow_explicit_graduate_roles','bachelor_eligible'])form.elements[key].checked=r[key]===true;
  updateQuota();
}
function updateQuota(){const f=$('#settings-form');$('#quota-total').textContent=`合计 ${Number(f.elements.campus_count.value)+Number(f.elements.internship_count.value)} 个 / 批`;}
async function renderEvents(){const events=await api('/api/events');$('#operation-log').innerHTML=(state.operation.logs||[]).map(log=>`<div class="log-line"><span class="log-time">${new Date(log.at*1000).toLocaleTimeString('zh-CN',{hour12:false})}</span><span>${escape(log.message)}</span></div>`).join('');$('#operation-log').hidden=!(state.operation.logs||[]).length;$('#event-rows').innerHTML=events.reverse().map(event=>`<div class="event-row"><span class="muted">${escape(date(event.at,true))}</span><span>${escape(kinds[event.kind]||event.kind)}</span><div>${escape(({complete:'已完成',partial:'未凑齐',error:'未完成',stopped:'已停止'})[event.message]||event.message)}${event.error?`<div class="event-error">${escape(event.error.message)}</div>`:''}${event.batch_id?`<div class="muted">${escape(event.batch_id)}</div>`:''}</div></div>`).join('')||'<div class="empty"><h3>暂无本机运行记录</h3></div>';}
async function action(path,body={}){try{await api(path,body);await refresh();}catch(error){toast(error.message);}}
function sendTargets(){const targets=sendSnapshot;$('#send-count').textContent=`已批准 ${targets.length} 个岗位`;const n=targets.filter(item=>item.status==='uncertain').length;$('#send-recheck').hidden=!n;$('#send-recheck').textContent=n?`${n} 条结果待核实：先查聊天记录，已有正文则跳过；确认缺失后才补发。`:'';$('#send-targets').innerHTML=targets.map(item=>`<div class="send-target"><strong>#${item.number} ${escape(item.company)} · ${escape(item.title)}</strong><p>${escape(item.greeting)}</p></div>`).join('');$('#execute-send').disabled=!targets.length||sendRequestPending;$('#execute-send-label').textContent=`确认发送这 ${targets.length} 个`;return targets;}
document.addEventListener('click',async event=>{const nav=event.target.closest('[data-view]');if(nav){currentView=nav.dataset.view;$$('.nav-button').forEach(b=>b.classList.toggle('active',b===nav));$$('.view').forEach(v=>v.hidden=v.id!==`view-${currentView}`);if(currentView==='events')await renderEvents();if(currentView==='profile')await profileEditor.open();return;}const tab=event.target.closest('[data-status]');if(tab){statusFilter=tab.dataset.status;renderJobs();return;}const dt=event.target.closest('[data-detail-tab]');if(dt){editing=false;detailTab=dt.dataset.detailTab;renderDetail();return;}const row=event.target.closest('.job-row');if(row&&!event.target.matches('[data-select]')){focused=Number(row.dataset.number);editing=false;$('#workspace').classList.add('mobile-detail');renderRows();renderDetail();return;}const id=event.target.closest('button')?.id;try{if(id==='back-list')$('#workspace').classList.remove('mobile-detail');if(id==='approve-one')await review([focused],[]);if(id==='reject-one')await review([],[focused]);if(id==='bulk-approve')await review([...selected],[]);if(id==='bulk-reject')await review([],[...selected]);if(id==='copy-greeting'){await navigator.clipboard.writeText(current().greeting);toast('招呼已复制，尚未发送');}if(id==='edit-greeting'){editing=true;$('#greeting').readOnly=false;$('#greeting').focus();$('#edit-actions').hidden=false;}if(id==='cancel-edit'){editing=false;renderDetail();}if(id==='save-greeting'){await api('/api/edit',{batch_id:batch.batch_id,number:focused,greeting:$('#greeting').value,expected_update:current().updated_at});editing=false;toast('招呼已保存，状态已恢复为待审批');await loadBatch(batch.batch_id);await refresh();}if(id==='prepare')await action('/api/prepare');if(id==='draft')await action('/api/draft',{batch_id:batch.batch_id});if(id==='connect')await action('/api/connect');if(id==='check-browser')await action('/api/check');if(id==='stop-task')await action('/api/stop');if(id==='refresh-events')await renderEvents();if(id==='open-send'){if(!batch)return;sendSnapshot=batch.items.filter(approvedForSending).map(item=>({...item}));sendBatchId=batch.batch_id;sendTargets();$('#send-dialog').showModal();icons();}if(id==='close-send'||id==='cancel-send')$('#send-dialog').close();if(id==='execute-send'&&!sendRequestPending){const targets=sendTargets();if(!targets.length)return;sendRequestPending=true;$('#execute-send').disabled=true;try{await api('/api/send',{batch_id:sendBatchId,approved_numbers:targets.map(i=>i.number),verify_before_retry:targets.some(i=>i.status==='uncertain'),expected_updates:Object.fromEntries(targets.map(i=>[String(i.number),i.updated_at]))});$('#send-dialog').close();await refresh();}finally{sendRequestPending=false;$('#execute-send').disabled=false;}}}catch(error){toast(error.message);}});
$('#job-rows').addEventListener('keydown',event=>{if(event.key==='Enter'&&event.target.matches('.job-row'))event.target.click();});
$('#job-rows').addEventListener('change',event=>{const n=Number(event.target.dataset.select);if(!n)return;event.target.checked?selected.add(n):selected.delete(n);renderControls();});
$('#select-all').addEventListener('change',event=>{for(const item of filtered().filter(selectable)){event.target.checked?selected.add(item.number):selected.delete(item.number);}renderRows();renderControls();});
$('#search').addEventListener('input',()=>{renderRows();renderControls();});
$('#batch-select').addEventListener('change',async event=>{selected.clear();editing=false;await loadBatch(event.target.value);});
$('#resume').addEventListener('click',async()=>{if(batch)await action('/api/resume',{batch_id:batch.batch_id});});
$('#add-city').addEventListener('click',()=>{
  const city=$('#new-city').value;
  if(!city||!(state.cities||[]).includes(city))return;
  extraCities.add(city);settingsDirty=true;
  $('#city-options').insertAdjacentHTML('beforeend','<label><input type="checkbox" name="cities" value="'+escape(city)+'" checked>'+escape(city)+'</label>');
  $('#new-city option:checked').remove();$('#new-city').value='';
});
$('#reload-settings').addEventListener('click',async()=>{if(settingsDirty&&!window.confirm('放弃尚未保存的筛选修改，重新加载？'))return;settingsDirty=false;extraCities.clear();await refresh();if(state)renderSettings();});
$('#skip-wait').addEventListener('click',()=>{if(!state.manual_recheck?.available)return;recheckRevision=state.manual_recheck.revision;$('#recheck-dialog').showModal();});
for(const id of ['close-recheck','cancel-recheck'])$('#'+id).addEventListener('click',()=>$('#recheck-dialog').close());
$('#confirm-recheck').addEventListener('click',async()=>{
  if(recheckSending||!recheckRevision)return;
  recheckSending=true;$('#confirm-recheck').disabled=true;
  try{await api('/api/check_now',{confirm_once:true,expected_pause:recheckRevision});$('#recheck-dialog').close();await refresh();}
  catch(error){toast(error.message);if(error.code==='CLIENT_TIMEOUT'){$('#recheck-dialog').close();await refresh();}}
  finally{recheckSending=false;$('#confirm-recheck').disabled=false;}
});
window.addEventListener('beforeunload',event=>{if(settingsDirty||profileEditor.isDirty()){event.preventDefault();event.returnValue='';}});
$('#detail').addEventListener('input',event=>{if(event.target.id==='greeting')$('#greeting-count').textContent=`${event.target.value.length} / 300`;});
$('#settings-form').addEventListener('input',()=>{settingsDirty=true;updateQuota();});
$('#settings-form').addEventListener('submit',async event=>{
  event.preventDefault();
  const form=new FormData(event.target);
  const body=Object.fromEntries(['campus_count','internship_count','min_salary','min_daily_salary','graduation_year'].map(k=>[k,Number(form.get(k))]));
  for(const key of ['allow_unspecified_cohort','allow_explicit_graduate_roles','bachelor_eligible'])body[key]=form.has(key);
  for(const key of ['custom_role_keywords','require','exclude','exclude_company'])body[key]=[...new Set(String(form.get(key)||'').split(/\r?\n/).map(v=>v.trim()).filter(Boolean))];
  body.cities=form.getAll('cities');body.roles=form.getAll('roles');body.history_dedupe=form.get('history_dedupe');body.expected_revision=settingsVersion;
  try{await api('/api/settings',body);settingsDirty=false;toast('筛选条件已保存，仅对新批次生效');await refresh();}
  catch(error){toast(error.message);}
});
$('#shutdown').addEventListener('click',async()=>{try{await api('/api/shutdown',{});clearInterval(refreshTimer);$$('button').forEach(button=>button.disabled=true);$('#connection-text').textContent='客户端已退出';toast('客户端已退出');}catch(error){toast(error.message);}});
icons();refresh();const refreshTimer=setInterval(refresh,2500);
})();
