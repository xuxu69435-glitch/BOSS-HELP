(() => {
'use strict';
window.createProfileEditor = ({api, toast, icons, escape, busy}) => {
  const $ = q => document.querySelector(q);
  const form = $('#profile-form');
  let data = null, version = null, dirty = false, saving = false;
  const symbol = name => `<i data-lucide="${name}"></i>`;
  const lines = value => value.replace(/\r/g, '').split('\n');
  const button = (action, label, attrs='') => `<button type="button" class="icon-button danger-text" data-profile-action="${action}" ${attrs} title="${label}" aria-label="${label}">${symbol('trash-2')}</button>`;
  function controls() {
    $('#save-profile').disabled = busy() || saving || !data || !dirty;
    $('#reload-profile').disabled = saving;
    $('#profile-status').textContent = saving ? '正在保存' : dirty ? '有未保存修改' : data ? '已保存于本机' : '未加载';
    form.querySelectorAll('input,textarea,select,button').forEach(control=>control.disabled=saving||!data);
  }
  function mark() { dirty = true; controls(); }
  function facts(section, root) {
    const items = data[section]?.items || [];
    const valueType=item=>item.label==='每周可实习天数'&&/^\d*$/.test(item.value)?'type="number" min="0" max="7" step="1"':item.label==='最早到岗时间'&&(!item.value||/^\d{4}-\d{2}-\d{2}$/.test(item.value))?'type="date"':'type="text"';
    $(root).innerHTML = items.map((item, index) => `<div class="profile-fact">
      <input data-section="${section}" data-index="${index}" data-field="label" aria-label="字段名称 ${index+1}" maxlength="80" placeholder="字段名称" value="${escape(item.label)}" required>
      <input ${valueType(item)} data-section="${section}" data-index="${index}" data-field="value" aria-label="${escape(item.label||'字段内容')}" maxlength="3000" placeholder="字段内容" value="${escape(item.value)}">
      ${button('remove-fact','删除字段',`data-section="${section}" data-index="${index}"`)}
      ${section==='personal_info'?`<input type="url" class="profile-fact-link" data-section="${section}" data-index="${index}" data-field="link" aria-label="附加链接 ${index+1}" maxlength="2048" placeholder="附加链接（可选）" value="${escape(item.link||'')}">`:''}
    </div>`).join('') || '<p class="muted profile-empty">暂无条目</p>';
  }
  function modules() {
    $('#profile-modules').innerHTML = data.modules.map((module, index) => `<section class="profile-module">
      <div class="profile-module-head"><input data-module-title="${index}" aria-label="模块名称 ${index+1}" value="${escape(module.title)}" maxlength="160" required>${button('remove-module','删除模块',`data-module="${index}"`)}</div>
      ${module.rows.map((row, r) => `<div class="profile-row">
        <div class="profile-row-head"><select data-row-type="${r}" data-module="${index}" aria-label="条目格式 ${index+1}-${r+1}"><option value="richtext" ${row.type!=='tags'?'selected':''}>文本</option><option value="tags" ${row.type==='tags'?'selected':''}>技能标签</option></select>${button('remove-row','删除条目',`data-module="${index}" data-row="${r}"`)}</div>
        <textarea data-module="${index}" data-row-content="${r}" aria-label="模块正文 ${index+1}-${r+1}" rows="5" maxlength="20000">${escape((row.type==='tags'?row.tags:row.content||[]).join('\n'))}</textarea>
      </div>`).join('')}
      <button type="button" class="button small" data-profile-action="add-row" data-module="${index}">${symbol('plus')}新增条目</button>
    </section>`).join('') || '<p class="muted profile-empty">暂无补充资料</p>';
  }
  function render(value) {
    data = structuredClone(value.data);
    version = value.version;
    dirty = false;
    $('#profile-title').value = data.title || '';
    $('#profile-name').textContent = data.name;
    $('#profile-version').textContent = value.created_at ? `保存时间 ${new Date(value.created_at*1000).toLocaleString('zh-CN')}` : '初始导入资料';
    $('#profile-preview').textContent = value.model_text || '';
    facts('personal_info','#profile-info');
    facts('job_intention','#profile-intention');
    modules(); controls(); icons();
  }
  async function reload() {
    if(dirty && !window.confirm('放弃尚未保存的资料修改，重新加载？')) return;
    try { render(await api('/api/profile')); }
    catch(error) { toast(error.message); }
  }
  form.addEventListener('input', event => {
    if(!data) return;
    const target=event.target, d=target.dataset;
    if(target.id==='profile-title') data.title=target.value;
    else if(d.section && d.field) data[d.section].items[Number(d.index)][d.field]=target.value;
    else if(d.moduleTitle!==undefined) data.modules[Number(d.moduleTitle)].title=target.value;
    else if(d.rowContent!==undefined){const row=data.modules[Number(d.module)].rows[Number(d.rowContent)];row[row.type==='tags'?'tags':'content']=lines(target.value);}
    else return;
    mark();
  });
  form.addEventListener('change', event => {
    const d=event.target.dataset;
    if(data && d.section && d.field==='label'){facts(d.section,d.section==='personal_info'?'#profile-info':'#profile-intention');icons();return;}
    if(!data || d.rowType===undefined) return;
    const row=data.modules[Number(d.module)].rows[Number(d.rowType)];
    const values=row.type==='tags'?row.tags:row.content;
    row.type=event.target.value;
    delete row.content; delete row.tags;
    row[row.type==='tags'?'tags':'content']=values || [];
    mark(); modules(); icons();
  });
  form.addEventListener('click', event => {
    const target=event.target.closest('[data-profile-action]');
    if(!data || !target || saving) return;
    const d=target.dataset, action=d.profileAction;
    if(action==='add-info'){
      if(data.personal_info.items.length>=40)return toast('个人信息最多40项');
      data.personal_info.items.push({label:'补充信息',value:'',icon:'',link:''});
      facts('personal_info','#profile-info');
    } else if(action==='add-intention'){
      data.job_intention ||= {title:'求职与实习安排',icon:'',items:[],show_background:true};
      if(data.job_intention.items.length>=30)return toast('求职安排最多30项');
      const labels=['最早到岗时间','每周可实习天数','可连续实习时长','作品链接'];
      data.job_intention.items.push({label:labels.find(label=>!data.job_intention.items.some(item=>item.label===label))||'补充安排',value:''});
      facts('job_intention','#profile-intention');
    } else if(action==='remove-fact'){
      const item=data[d.section].items[Number(d.index)];
      if(item.value && !window.confirm(`删除字段“${item.label}”？`))return;
      data[d.section].items.splice(Number(d.index),1);
      facts(d.section,d.section==='personal_info'?'#profile-info':'#profile-intention');
    } else if(action==='add-module'){
      if(data.modules.length>=40)return toast('资料模块最多40个');
      data.modules.push({id:crypto.randomUUID(),title:'补充经历',icon:'',rows:[{type:'richtext',content:[]}]}); modules();
    } else if(action==='remove-module'){
      if(!window.confirm(`删除模块“${data.modules[Number(d.module)].title}”？`))return;
      data.modules.splice(Number(d.module),1); modules();
    } else if(action==='add-row'){
      const rows=data.modules[Number(d.module)].rows;
      if(rows.length>=60)return toast('每个模块最多60条');
      rows.push({type:'richtext',content:[]}); modules();
    } else if(action==='remove-row'){
      if(!window.confirm('删除这个资料条目？'))return;
      data.modules[Number(d.module)].rows.splice(Number(d.row),1); modules();
    } else return;
    mark(); icons();
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if(!data || !dirty || saving || busy()) return;
    saving=true;controls();
    try {
      const saved=await api('/api/profile',{expected_version:version,data});
      render(saved);toast('资料已保存于本机，新批次使用新版本；尚未调用模型');
    } catch(error){toast(error.message);}
    finally{saving=false;controls();}
  });
  $('#reload-profile').addEventListener('click',reload);
  return {open:async()=>{if(!data||!dirty)await reload();}, controls, isDirty:()=>dirty};
};
})();
