const $ = s => document.querySelector(s);
let subscriptions = [];
let tokens = [];
let appConfig = {};

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

async function loadAll(){
  [subscriptions, tokens, appConfig] = await Promise.all([
    api('/api/subscriptions'),
    api('/api/tokens'),
    api('/api/config')
  ]);
  const adminPort = location.port || (location.protocol === 'https:' ? '443' : '80');
  const status = $('#listenerStatus');
  if (status) status.innerHTML = '<span class="dot"></span> ADMIN :' + adminPort + ' · PUBLIC :' + appConfig.public_port;
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
    return `
    <div class="item">
      <div>
        <div class="item-title">${escapeHtml(s.name)} <span class="badge ${s.enabled?'ok':''}">${s.enabled?'active':'disabled'}</span><span class="badge">${escapeHtml(s.format)}</span>${service}${stale}</div>
        <div class="item-meta">${escapeHtml(s.url)}</div>
        <div class="item-meta"><strong>${escapeHtml(nodes)}</strong>${s.cache_age == null ? '' : ` · кэш ${s.cache_age} сек.`}</div>
      </div>
      <div class="item-actions">
        <button class="ghost" onclick="refreshSub(${s.id})">Обновить</button>
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
    const subsUrl=`${publicBase}/${t.token}/subs`;
    const namedButtons=allowed.map(s=>{
      const u=`${publicBase}/${t.token}/sub/${encodeURIComponent(s.name)}`;
      return `<button class="ghost tiny" onclick="copyText('${escapeHtml(u)}')">${escapeHtml(s.name)}</button>`;
    }).join('');
    return `<div class="item token-card">
      <div class="item-body">
        <div class="item-title">${escapeHtml(t.label)} <span class="badge ${t.enabled?'ok':''}">${t.enabled?'active':'disabled'}</span></div>
        <div class="item-meta"><strong>Состав:</strong> ${escapeHtml(scope)}</div>
        <div class="token-value" title="Токен доступа">${escapeHtml(t.token)}</div>
        <div class="item-meta token-url">${escapeHtml(shortUrl)}</div>
      </div>
      <div class="item-actions">
        <button class="ghost" onclick="copyText('${escapeHtml(t.token)}')">Токен</button>
        <button class="ghost" onclick="copyText('${escapeHtml(shortUrl)}')">Короткий URL</button>
        <button class="ghost" onclick="copyText('${escapeHtml(subsUrl)}')">/subs URL</button>
        ${namedButtons}
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
  try{
    flash('Обновляю источник и связанные сборки...');
    const r=await api(`/api/subscriptions/${id}/refresh`,{method:'POST'});
    await loadAll();
    const service=r.service_count? `, служебных отброшено: ${r.service_count}` : '';
    const stale=r.stale? ' (использован старый кэш)' : '';
    flash(`Обновлено: ${r.proxy_count ?? '?'} узлов${service}; сборок: ${r.rebuilt_bundles}${stale}`,r.stale?'error':'success');
  }catch(e){flash(e.message,'error')}
};

$('#refreshAllBtn')?.addEventListener('click',async()=>{
  try{
    flash('Обновляю все источники...');
    const r=await api('/api/subscriptions/refresh-all',{method:'POST'});
    await loadAll();
    const failed=r.failed?.length ? `; ошибки: ${r.failed.join(', ')}` : '';
    const service=r.service_count ? `; служебных отброшено: ${r.service_count}` : '';
    flash(`Обновлено источников: ${r.refreshed}/${r.sources}; узлов: ${r.proxy_count}${service}; сборок: ${r.rebuilt_bundles}${failed}`,r.failed?.length?'error':'success');
  }catch(e){flash(e.message,'error')}
});

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
