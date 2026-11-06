(() => {
'use strict';
window.createGreetingAssistant=({api,toast})=>{
  const $=selector=>document.querySelector(selector);
  const form=$('#solo-form');
  let timer=null,pending=false,loading=false,loaded=false,inputDirty=false,revision=0,requestId='',rendered='',latest={status:'idle'},closed=false,recognizing=false;
  $('#greeting-nav').hidden=false;
  function controls(){
    const running=pending||latest.status==='running';
    $('#solo-inputs').disabled=running||recognizing;
    $('#solo-upload span').textContent=recognizing?'正在识别截图…':'上传岗位截图';
    $('#solo-generate span').textContent=running?'正在生成…':'生成打招呼文案';
  }
  function count(){
    $('#solo-count').textContent=`${$('#solo-text').value.length} / 300`;
    $('#solo-copy').disabled=!$('#solo-text').value.trim();
  }
  function list(id,items){
    $(id).replaceChildren(...items.map(text=>{const li=document.createElement('li');li.textContent=text;return li;}));
  }
  function render(value){
    latest=value;
    controls();
    if(value.id&&requestId!==value.id){
      requestId=value.id;
      if(!inputDirty)for(const key of ['url','title','company','description'])form.elements[key].value=value.input[key]||'';
    }
    const messages={idle:'等待提供岗位信息',running:'正在独立生成，主任务继续运行。',complete:'文案已生成，可以编辑并复制。',error:value.error};
    $('#solo-status').textContent=messages[value.status]||'';
    $('#solo-status').classList.toggle('danger-text',value.status==='error');
    $('#solo-result').hidden=value.status!=='complete';
    $('#solo-empty').hidden=value.status==='complete';
    if(value.status==='complete'&&rendered!==value.id){
      rendered=value.id;
      $('#solo-job-title').textContent=(value.job.title||'提供的岗位')+(value.job.company?` · ${value.job.company}`:'');
      $('#solo-source').textContent=value.input.description?'来源：你粘贴的岗位描述':`来源：${value.job.url}`;
      $('#solo-profile').textContent=`使用资料：${value.profile.title} · 版本 ${value.profile.version.slice(0,12)}`;
      $('#solo-summary').textContent=value.result.summary;
      $('#solo-text').value=value.result.greeting;
      list('#solo-evidence',value.result.resume_evidence);
      list('#solo-warnings',value.result.warnings);
      $('#solo-warning-section').hidden=!value.result.warnings.length;
      count();
    }
  }
  function schedule(){
    clearTimeout(timer);
    if(!closed&&latest.status==='running')timer=setTimeout(load,1200);
  }
  async function load(){
    if(pending||loading||closed)return;
    loading=true;
    const expected=revision;
    try{const value=await api('/api/greeting-assistant');if(expected===revision){render(value);loaded=true;}}
    catch(error){$('#solo-status').textContent=error.message;}
    finally{loading=false;schedule();}
  }
  async function recognize(file){
    if(recognizing||pending||latest.status==='running')return;
    const status=$('#solo-image-status'),preview=$('#solo-image-preview');
    status.classList.remove('danger-text');
    preview.hidden=true;preview.removeAttribute('src');
    recognizing=true;inputDirty=true;controls();
    status.textContent='正在读取并识别截图…';
    try{
      if(!['image/png','image/jpeg','image/webp','image/bmp'].includes(file.type))throw new Error('请选择PNG、JPG、WebP或BMP截图');
      if(file.size>8*1024*1024)throw new Error('截图不能超过8MB，请裁剪后重试');
      const source=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(new Error('截图读取失败，请重试'));reader.readAsDataURL(file);});
      const img=new Image();
      await new Promise((resolve,reject)=>{img.onload=resolve;img.onerror=()=>reject(new Error('截图格式无效，请重新上传或粘贴'));img.src=source;});
      if(Math.max(img.naturalWidth,img.naturalHeight)>10000||img.naturalWidth*img.naturalHeight>16000000)throw new Error('截图分辨率过大，请裁剪或分成多张截图');
      const canvas=document.createElement('canvas');canvas.width=img.naturalWidth;canvas.height=img.naturalHeight;
      const context=canvas.getContext('2d');context.fillStyle='#fff';context.fillRect(0,0,canvas.width,canvas.height);context.drawImage(img,0,0);
      const image=canvas.toDataURL('image/png');
      if(image.length>4*Math.ceil(8*1024*1024/3)+22)throw new Error('截图转换后超过8MB，请裁剪后重试');
      preview.src=image;preview.hidden=false;
      const value=await api('/api/greeting-assistant/recognize',{image});
      const description=form.elements.description;
      const combined=[description.value.trim(),value.text].filter(Boolean).join('\n\n');
      if(combined.length>12000)throw new Error('识别文字与现有描述合计超过12000字，请精简描述后重新上传');
      description.value=combined;
      status.textContent=`已识别 ${value.text.length} 字并追加到岗位描述，请核对后生成文案。`;
    }catch(error){
      status.textContent=error.message==='操作不存在'?'当前后台尚未加载截图识别，请等任务结束后退出并重新打开工作台。':error.message;
      status.classList.add('danger-text');
    }finally{recognizing=false;controls();$('#solo-image-file').value='';}
  }
  $('#solo-upload').addEventListener('click',()=>$('#solo-image-file').click());
  $('#solo-image-file').addEventListener('change',event=>{const file=event.target.files[0];if(file)recognize(file);});
  document.addEventListener('paste',event=>{
    if($('#view-greeting').hidden)return;
    const items=[...(event.clipboardData?.items||[])];
    const item=items.find(item=>item.kind==='file'&&item.type.startsWith('image/'));
    const file=item?.getAsFile()||[...(event.clipboardData?.files||[])].find(file=>file.type.startsWith('image/'));
    if(file){event.preventDefault();if(recognizing||pending||latest.status==='running'){toast('请等待当前识别或生成完成后再粘贴截图');return;}recognize(file);}
  });
  form.addEventListener('submit',async event=>{
    event.preventDefault();
    if(pending||recognizing||latest.status==='running')return;
    clearTimeout(timer);
    const body=Object.fromEntries(new FormData(form));
    revision++;inputDirty=false;
    pending=true;controls();
    $('#solo-result').hidden=true;$('#solo-empty').hidden=false;
    $('#solo-status').textContent='正在提交生成请求…';
    try{render(await api('/api/greeting-assistant',body));}
    catch(error){$('#solo-status').textContent=error.message;$('#solo-status').classList.add('danger-text');}
    finally{pending=false;controls();schedule();}
  });
  form.addEventListener('input',()=>{inputDirty=true;});
  $('#solo-text').addEventListener('input',count);
  $('#solo-copy').addEventListener('click',async()=>{
    try{await navigator.clipboard.writeText($('#solo-text').value);toast('文案已复制');}
    catch{toast('复制失败，请选中文案后手动复制');}
  });
  $('#greeting-nav').addEventListener('click',()=>{if(!loaded||latest.status==='running')load();});
  return {close(){closed=true;clearTimeout(timer);}};
};
})();
