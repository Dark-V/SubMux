const $ = s => document.querySelector(s);
let subscriptions = [];
let tokens = [];

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
  [subscriptions, tokens] = await Promise.all([api('/api/subscriptions'), api('/api/tokens')]);
  renderSubs(); renderScopes(); renderTokens();
}

function renderSubs(){
  $('#subCount').textContent = `${subscriptions.length} подписок`;
  const box = $('#subsList');
  if (!subscriptions.length){ box.innerHTML='<div class="empty">Источников пока нет.</div>'; return; }
  box.innerHTML = subscriptions.map(s=>`
    <div class="item">
      <div>
        <div class="item-title">${escapeHtml(s.name)} <span class="badge ${s.enabled?'ok':''}">${s.enabled?'active':'disabled'}</span><span class="badge">${escapeHtml(s.format)}</span></div>
        <div class="item-meta">${escapeHtml(s.url)}</div>
      </div>
      <div class="item-actions">
        <button class="ghost" onclick="testSub(${s.id})">Проверить</button>
        <button class="ghost" onclick="editSub(${s.id})">Изменить</button>
        <button class="danger" onclick="deleteSub(${s.id})">Удалить</button>
      </div>
    </div>`).join('');
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
    const scope=t.scope_all?'все подписки':(t.subscriptions?.map(s=>s.name).join(', ')||'нет доступа');
    const path=`${location.origin}/${t.token}/subs`;
    return `<div class="item">
      <div>
        <div class="item-title">${escapeHtml(t.label)} <span class="badge ${t.enabled?'ok':''}">${t.enabled?'active':'disabled'}</span></div>
        <div class="item-meta">Доступ: ${escapeHtml(scope)}</div>
        <div class="token-value" title="${escapeHtml(t.token)}">${escapeHtml(shortToken(t.token))}</div>
      </div>
      <div class="item-actions">
        <button class="ghost" onclick="copyText('${escapeHtml(t.token)}')">Токен</button>
        <button class="ghost" onclick="copyText('${escapeHtml(path)}')">/subs URL</button>
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
window.testSub=async id=>{try{const r=await api(`/api/subscriptions/${id}/test`,{method:'POST'});flash(r.ok?`Upstream OK: HTTP ${r.status}, ${r.bytes} bytes`:`Ошибка upstream: ${r.error||'HTTP '+r.status}`,r.ok?'success':'error')}catch(e){flash(e.message,'error')}};

$('#scopeAll').addEventListener('change',()=>$('#scopeBox').classList.toggle('hidden',$('#scopeAll').checked));
$('#tokenForm').addEventListener('submit',async e=>{
  e.preventDefault(); const all=$('#scopeAll').checked; const ids=[...document.querySelectorAll('#scopeSubs input:checked')].map(x=>Number(x.value));
  try{const r=await api('/api/tokens',{method:'POST',body:JSON.stringify({label:$('#tokenLabel').value.trim(),scope_all:all,subscription_ids:ids})});await loadAll();flash(`Токен создан: ${r.token}`);}
  catch(err){flash(err.message,'error')}
});
window.toggleToken=async(id,enabled)=>{try{await api(`/api/tokens/${id}`,{method:'PATCH',body:JSON.stringify({enabled})});await loadAll();flash(enabled?'Токен включён.':'Токен выключен.')}catch(e){flash(e.message,'error')}};
window.deleteToken=async id=>{if(!confirm('Удалить токен? Ссылки с ним сразу перестанут работать.'))return;try{await api(`/api/tokens/${id}`,{method:'DELETE'});await loadAll();flash('Токен удалён.')}catch(e){flash(e.message,'error')}};
window.copyText=async v=>{try{await navigator.clipboard.writeText(v);flash('Скопировано.')}catch{prompt('Скопируйте:',v)}};

$('#themeBtn').addEventListener('click',()=>{const c=localStorage.getItem('submux-theme')||'auto';const n=c==='dark'?'light':c==='light'?'auto':'dark';localStorage.setItem('submux-theme',n);document.documentElement.dataset.theme=n;});
loadAll().catch(e=>flash(e.message,'error'));
