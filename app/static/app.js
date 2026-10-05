const $ = s => document.querySelector(s);
let subscriptions = [];
let tokens = [];
let appConfig = {};
const refreshStates = new Map();

function flash(message, type='success') {
  const el = $('#flash'); el.textContent = message; el.className = `alert ${type}`;
  clearTimeout(window.__flash); window.__flash = setTimeout(()=>el.classList.add('hidden'), 4500);
}

async function api(url, options={}) {
  const opts = {...options, headers:{'Content-Type':'application/json', ...(options.headers||{})}};
  const res = await fetch(url, opts);
  if (res.status === 401) { location.href='/login'; throw new Error('Unauthorized'); }
  const data = await res.json().catch(()=>({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

function escapeHtml(v='') { return String(v).replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c])); }
function shortToken(v){ return v.length > 18 ? `${v.slice(0,10)}…${v.slice(-6)}` : v; }
function formatDuration(totalSeconds){
  const s=Math.max(0,Math.floor(Number(totalSeconds)||0));
  if(s<60)return `${s} сек.`;
  const m=Math.floor(s/60), sec=s%60;
  if(m<60)return sec ? `${m} мин. ${sec} сек.` : `${m} мин.`;
  const h=Math.floor(m/60), min=m%60;
  return min ? `${h} ч. ${min} мин.` : `${h} ч.`;
}
function humanBytes(bytes){
  const n=Number(bytes)||0;
  if(n<1024)return `${n} B`;
  if(n<1024*1024)return `${(n/1024).toFixed(1)} KiB`;
  return `${(n/1024/1024).toFixed(1)} MiB`;
}

async function loadAll(){
  [subscriptions, tokens, appConfig] = await Promise.all([
    api('/api/subscriptions'),
    api('/api/tokens'),
    api('/api/config')
  ]);
  const adminPort = location.port || (location.protocol === 'https:' ? '443' : '80');
  const status = $('#listenerStatus');
  if (status) status.innerHTML = '<span class="dot"></span> ADMIN :' + adminPort + ' · PUBLIC :' + appConfig.public_port;
  const ttlInput=$('#cacheTtlMinutes');
  if(ttlInput && document.activeElement!==ttlInput) ttlInput.value=Math.max(1,Math.round((appConfig.cache_ttl_seconds||1800)/60));
  const burstHint=$('#burstRefreshHint');
  if(burstHint) burstHint.textContent=`${appConfig.burst_refresh_requests||3} быстрых запроса от одного клиента за ${appConfig.burst_refresh_window_seconds||60} сек. автоматически принудительно обновят кэш.`;
  renderSubs(); renderScopes(); renderTokens(); syncScopeVisibility();
}

function renderSubs(){
  $('#subCount').textContent = `${subscriptions.length} подписок`;
  const box = $('#subsList');
  if (!subscriptions.length){ box.innerHTML='<div class="empty">Источников пока нет.</div>'; return; }
  box.innerHTML = subscriptions.map(s=>{
    const nodes = s.proxy_count == null ? 'кэш ещё не создан' : `${s.proxy_count} узлов`;
    const service = s.service_count > 0
      ? ` <span class="badge warn">служебных: ${s.service_count}</span>`
      : '';
    const stale = s.cache_stale ? ' <span class="badge warn">cache stale</span>' : '';
    const refresh=refreshStates.get(s.id);
    const refreshStatus=refresh ? `<div class="item-meta refresh-status ${refresh.phase==='error'?'error':''}" id="refresh-status-${s.id}">${escapeHtml(refresh.message||'')}</div>` : '';
    const countdown = s.cache_expires_in == null ? '' : ` · автообновление через <span class="cache-countdown" data-expires-at="${Date.now()+s.cache_expires_in*1000}">${escapeHtml(formatDuration(s.cache_expires_in))}</span>`;
    return `
    <div class="item">
      <div>
        <div class="item-title">${escapeHtml(s.name)} <span class="badge ${s.enabled?'ok':''}">${s.enabled?'active':'disabled'}</span><span class="badge">${escapeHtml(s.format)}</span>${service}${stale}</div>
        <div class="item-meta">${escapeHtml(s.url)}</div>
        <div class="item-meta"><strong>${escapeHtml(nodes)}</strong>${s.cache_age == null ? '' : ` · кэш ${escapeHtml(formatDuration(s.cache_age))}${countdown}`}</div>
        ${refreshStatus}
      </div>
      <div class="item-actions">
        <button class="ghost" data-refresh-id="${s.id}" onclick="refreshSub(${s.id})" ${refresh?.phase==='running'?'disabled':''}>${refresh?.phase==='running'?'Обновление…':'Обновить'}</button>
        <button class="ghost" onclick="window.open('/api/subscriptions/${s.id}/raw','_blank','noopener')">RAW</button>
        <button class="ghost" onclick="editSub(${s.id})">Изменить</button>
        <button class="danger" onclick="deleteSub(${s.id})">Удалить</button>
      </div>
    </div>`;
  }).join('');
}

function renderScopes(){
  const box=$('#scopeSubs');
  box.innerHTML = subscriptions.length ? subscriptions.map(s=>`<label><input type="checkbox" value="${s.id}"> ${escapeHtml(s.name)}</label>`).join('') : '<span class="muted">Сначала добавьте подписку.</span>';
}

function renderTokens(){
  $('#tokenCount').textContent = `${tokens.length} токенов`;
  const box=$('#tokensList');
  if (!tokens.length){box.innerHTML='<div class="empty">Токенов пока нет.</div>';return;}
  box.innerHTML=tokens.map(t=>{
    const allowed=t.scope_all ? subscriptions.filter(s=>s.enabled) : (t.subscriptions||[]);
    const scope=t.scope_all?'все текущие и будущие источники':(allowed.map(s=>s.name).join(' + ')||'нет источников');
    const publicBase=(appConfig.public_base_url||location.origin).replace(/\/$/,'');
    const shortUrl=`${publicBase}/${t.token}/`;
    return `<div class="item token-card">
      <div class="item-body">
        <div class="item-title">${escapeHtml(t.label)} <span class="badge ${t.enabled?'ok':''}">${t.enabled?'active':'disabled'}</span></div>
        <div class="item-meta"><strong>Состав:</strong> ${escapeHtml(scope)}</div>
        <div class="token-value copyable" title="Нажмите, чтобы скопировать токен" onclick="copyText('${escapeHtml(t.token)}')">${escapeHtml(t.token)}</div>
        <div class="item-meta token-url copyable" title="Нажмите, чтобы скопировать URL" onclick="copyText('${escapeHtml(shortUrl)}')">${escapeHtml(shortUrl)}</div>
      </div>
      <div class="item-actions">
        <button class="ghost" onclick="copyText('${escapeHtml(shortUrl)}')">URL</button>
        <button class="ghost" onclick="editToken(${t.id})">Состав</button>
        <button class="ghost" onclick="toggleToken(${t.id},${!t.enabled})">${t.enabled?'Выключить':'Включить'}</button>
        <button class="danger" onclick="deleteToken(${t.id})">Удалить</button>
      </div>
    </div>`;
  }).join('');
}

function subPayload(){
  return {
    name:$('#subName').value.trim(), url:$('#subUrl').value.trim(), user_agent:$('#subUa').value.trim(), hwid:$('#subHwid').value.trim(),
    device_os:$('#subOs').value.trim(), ver_os:$('#subVerOs').value.trim(), device_model:$('#subModel').value.trim(), app_version:$('#subAppVersion').value.trim(),
    format:$('#subFormat').value, enabled:$('#subEnabled').checked
  };
}

$('#subForm').addEventListener('submit', async e=>{
  e.preventDefault(); const id=$('#subId').value;
  try { await api(id?`/api/subscriptions/${id}`:'/api/subscriptions',{method:id?'PUT':'POST',body:JSON.stringify(subPayload())}); resetSubForm(); await loadAll(); flash(id?'Подписка обновлена.':'Подписка добавлена.'); }
  catch(err){flash(err.message,'error');}
});

function resetSubForm(){
  $('#subForm').reset(); $('#subId').value=''; $('#subUa').value='v2raytun/android'; $('#subOs').value='Android'; $('#subEnabled').checked=true; $('#subFormat').value='auto';
  $('#subFormTitle').textContent='Добавить подписку'; $('#subCancel').classList.add('hidden');
}
$('#subCancel').addEventListener('click',resetSubForm);

window.editSub=id=>{
  const s=subscriptions.find(x=>x.id===id); if(!s)return;
  $('#subId').value=s.id; $('#subName').value=s.name; $('#subUrl').value=s.url; $('#subUa').value=s.user_agent; $('#subHwid').value=s.hwid;
  $('#subOs').value=s.device_os; $('#subVerOs').value=s.ver_os; $('#subModel').value=s.device_model; $('#subAppVersion').value=s.app_version; $('#subFormat').value=s.format; $('#subEnabled').checked=s.enabled;
  $('#subFormTitle').textContent='Изменить подписку'; $('#subCancel').classList.remove('hidden'); scrollTo({top:0,behavior:'smooth'});
};
window.deleteSub=async id=>{if(!confirm('Удалить подписку?'))return;try{await api(`/api/subscriptions/${id}`,{method:'DELETE'});await loadAll();flash('Подписка удалена.')}catch(e){flash(e.message,'error')}};
window.refreshSub=async id=>{
  const started=performance.now();
  refreshStates.set(id,{phase:'running',message:'Запрос к upstream…'});
  renderSubs();
  const timer=setInterval(()=>{
    const elapsed=((performance.now()-started)/1000).toFixed(1);
    const button=document.querySelector(`[data-refresh-id="${id}"]`);
    const status=document.querySelector(`#refresh-status-${id}`);
    if(button)button.textContent=`Обновление… ${elapsed}с`;
    if(status)status.textContent=`Запрос к upstream / пересборка кэша… ${elapsed}с`;
  },200);
  try{
    const r=await api(`/api/subscriptions/${id}/refresh`,{method:'POST'});
    const service=r.service_count? ` · служебных отброшено: ${r.service_count}` : '';
    const stale=r.stale? ' · использован старый кэш' : '';
    const elapsed=((r.elapsed_ms||Math.round(performance.now()-started))/1000).toFixed(2);
    refreshStates.set(id,{
      phase:r.stale?'error':'done',
      message:`HTTP ${r.status} · ${r.proxy_count ?? '?'} узлов · ${humanBytes(r.bytes)} · ${elapsed}с · кэш обновлён${service}${stale}`
    });
    await loadAll();
    flash(`Источник обновлён за ${elapsed}с`,r.stale?'error':'success');
    setTimeout(()=>{refreshStates.delete(id);renderSubs();},7000);
  }catch(e){
    refreshStates.set(id,{phase:'error',message:`Ошибка обновления: ${e.message}`});
    renderSubs();
    flash(e.message,'error');
    setTimeout(()=>{refreshStates.delete(id);renderSubs();},10000);
  }finally{
    clearInterval(timer);
  }
};

$('#refreshAllBtn')?.addEventListener('click',async()=>{
  const btn=$('#refreshAllBtn');
  const original=btn?.textContent||'Обновить всё';
  const started=performance.now();
  const timer=setInterval(()=>{if(btn)btn.textContent=`Обновление… ${((performance.now()-started)/1000).toFixed(1)}с`;},200);
  if(btn)btn.disabled=true;
  try{
    flash('Обновляю все источники...');
    const r=await api('/api/subscriptions/refresh-all',{method:'POST'});
    await loadAll();
    const failed=r.failed?.length ? `; ошибки: ${r.failed.join(', ')}` : '';
    const service=r.service_count ? `; служебных отброшено: ${r.service_count}` : '';
    flash(`Обновлено источников: ${r.refreshed}/${r.sources}; узлов: ${r.proxy_count}${service}; сборок: ${r.rebuilt_bundles}${failed}`,r.failed?.length?'error':'success');
  }catch(e){flash(e.message,'error')}
  finally{
    clearInterval(timer);
    if(btn){btn.disabled=false;btn.textContent=original;}
  }
});

$('#cacheTtlForm')?.addEventListener('submit',async e=>{
  e.preventDefault();
  const minutes=Math.max(1,Number($('#cacheTtlMinutes').value)||30);
  try{
    const r=await api('/api/config',{method:'PATCH',body:JSON.stringify({cache_ttl_seconds:Math.round(minutes*60)})});
    appConfig.cache_ttl_seconds=r.cache_ttl_seconds;
    await loadAll();
    flash(`TTL кэша: ${formatDuration(r.cache_ttl_seconds)}`);
  }catch(err){flash(err.message,'error')}
});

setInterval(()=>{
  document.querySelectorAll('.cache-countdown').forEach(el=>{
    const left=Math.max(0,Math.ceil((Number(el.dataset.expiresAt)-Date.now())/1000));
    el.textContent=left>0?formatDuration(left):'сейчас';
  });
},1000);

function syncScopeVisibility(){
  $('#scopeBox').classList.toggle('hidden',$('#scopeAll').checked);
}

function resetTokenForm(){
  $('#tokenForm').reset();
  $('#tokenId').value='';
  $('#tokenLabel').value='access';
  $('#scopeAll').checked=false;
  syncScopeVisibility();
  document.querySelectorAll('#scopeSubs input').forEach(x=>x.checked=false);
  $('#tokenFormTitle').textContent='Новая сборка';
  $('#tokenSave').textContent='Создать сборку';
  $('#tokenCancel').classList.add('hidden');
}

$('#scopeAll').addEventListener('change',syncScopeVisibility);
$('#tokenCancel').addEventListener('click',resetTokenForm);

window.editToken=id=>{
  const t=tokens.find(x=>x.id===id); if(!t)return;
  $('#tokenId').value=t.id;
  $('#tokenLabel').value=t.label;
  $('#scopeAll').checked=t.scope_all;
  const selected=new Set((t.subscriptions||[]).map(s=>s.id));
  document.querySelectorAll('#scopeSubs input').forEach(x=>x.checked=selected.has(Number(x.value)));
  syncScopeVisibility();
  $('#tokenFormTitle').textContent='Изменить сборку';
  $('#tokenSave').textContent='Сохранить состав';
  $('#tokenCancel').classList.remove('hidden');
  $('#tokenForm').scrollIntoView({behavior:'smooth',block:'center'});
};

$('#tokenForm').addEventListener('submit',async e=>{
  e.preventDefault();
  const id=$('#tokenId').value;
  const all=$('#scopeAll').checked;
  const ids=[...document.querySelectorAll('#scopeSubs input:checked')].map(x=>Number(x.value));
  const payload={label:$('#tokenLabel').value.trim(),scope_all:all,subscription_ids:ids};
  if(!all && ids.length===0){flash('Выбери хотя бы один источник.','error');return;}
  try{
    const r=await api(id?`/api/tokens/${id}`:'/api/tokens',{method:id?'PATCH':'POST',body:JSON.stringify(payload)});
    const createdToken=!id?r.token:null;
    resetTokenForm();
    await loadAll();
    flash(id?'Состав сборки обновлён.':`Сборка создана. Токен: ${createdToken}`);
  } catch(err){flash(err.message,'error')}
});
window.toggleToken=async(id,enabled)=>{try{await api(`/api/tokens/${id}`,{method:'PATCH',body:JSON.stringify({enabled})});await loadAll();flash(enabled?'Токен включён.':'Токен выключен.')}catch(e){flash(e.message,'error')}};
window.deleteToken=async id=>{if(!confirm('Удалить токен? Ссылки с ним сразу перестанут работать.'))return;try{await api(`/api/tokens/${id}`,{method:'DELETE'});await loadAll();flash('Токен удалён.')}catch(e){flash(e.message,'error')}};
window.copyText=async v=>{try{await navigator.clipboard.writeText(v);flash('Скопировано.')}catch{prompt('Скопируйте:',v)}};

$('#themeBtn').addEventListener('click',()=>{const c=localStorage.getItem('submux-theme')||'auto';const n=c==='dark'?'light':c==='light'?'auto':'dark';localStorage.setItem('submux-theme',n);document.documentElement.dataset.theme=n;});
loadAll().catch(e=>flash(e.message,'error'));
