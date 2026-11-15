import {Timeline} from './sync.js';
const $ = id => document.getElementById(id);
let token = null, expiresAt = 0, user = null, epoch = 0, socket = null, generation = 0;
let retryTimer, tokenTimer, retry = 0, refreshTask = null, registerMode = false, active = null;
let dialogs = new Map(), timelines = new Map(), pending = new Map();
const channel = typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel('messenger-session') : null;
const status = text => { $('connection-status').textContent=text; $('connection-status').classList.toggle('online',text==='Подключено'); };
const fail = (id,error) => { $(id).textContent=error.message || 'Ошибка соединения'; };
class ApiError extends Error { constructor(message,status) { super(message); this.status=status; } }
function stopSession() {
  epoch++; generation++; clearTimeout(retryTimer); clearTimeout(tokenTimer);
  if (socket) socket.close(); socket=null; token=null; user=null; active=null; refreshTask=null;
  dialogs=new Map(); timelines=new Map(); pending=new Map();
  $('workspace').hidden=true; $('auth-form').hidden=false;
  $('password').value=''; $('messages').replaceChildren(); $('dialogs').replaceChildren();
  $('message-text').value=''; $('message-text').disabled=true; $('send-button').disabled=true;
  status('Нужен вход');
}
if (channel) channel.onmessage = e => { if(e.data==='logout' || e.data==='login') stopSession(); };
function acceptToken(result) {
  token=result.access_token; expiresAt=Date.now()+result.expires_in*1000;
  clearTimeout(tokenTimer);
  tokenTimer=setTimeout(() => { if(user) connect(); }, Math.max(1000,(result.expires_in-30)*1000));
}
async function raw(path, options={}) {
  const response=await fetch(path,{...options,credentials:'same-origin',signal:AbortSignal.timeout(10000)});
  const body=response.status===204 ? null : await response.json().catch(()=>null);
  if(!response.ok) throw new ApiError(body?.message || body?.detail || `Ошибка ${response.status}`,response.status);
  return body;
}
const authHeaders={'Content-Type':'application/json','X-CSRF-Protection':'1'};
async function refresh() {
  if(refreshTask) return refreshTask;
  const e=epoch;
  const perform=async()=> {
    if(e!==epoch) throw new Error('Сессия изменилась');
    const data=await raw('/auth/refresh',{method:'POST',headers:authHeaders,body:'{}'});
    if(e!==epoch) throw new Error('Сессия изменилась');
    acceptToken(data);
  };
  const operation=(async()=> {
    try {
      if(navigator.locks) await navigator.locks.request('messenger-refresh',perform);
      else await perform();
    } catch(error) {
      if(e===epoch && error.status===401) stopSession();
      throw error;
    }
  })();
  refreshTask=operation;
  try { await operation; } finally { if(refreshTask===operation) refreshTask=null; }
}
async function api(path, options={}, retried=false) {
  const e=epoch;
  try {
    const result=await raw(path,{...options,headers:{'Content-Type':'application/json',...options.headers,Authorization:`Bearer ${token}`}});
    if(e!==epoch) throw new Error('Сессия изменилась');
    return result;
  } catch(error) {
    if(e===epoch && error.status===401 && !retried) {
      await refresh(); return api(path,options,true);
    }
    throw error;
  }
}
function timeline(id) {
  if(!timelines.has(id)) timelines.set(id,new Timeline());
  return timelines.get(id);
}
function renderDialogs() {
  $('dialogs').replaceChildren();
  for(const dialog of dialogs.values()) {
    const button=document.createElement('button'); button.type='button';
    button.className='dialog'+(active===dialog.dialog_id?' active':'');
    button.textContent=dialog.peer_username || dialog.peer_user_id.slice(0,8);
    button.dataset.dialogId=dialog.dialog_id;
    button.onclick=()=>selectDialog(dialog.dialog_id).catch(e=>fail('chat-feedback',e));
    $('dialogs').append(button);
  }
}
function render(scroll=false) {
  if(!active) return;
  const t=timeline(active), dialog=dialogs.get(active);
  $('conversation-title').textContent=dialog?.peer_username || 'Переписка';
  $('conversation-info').textContent='Личный диалог';
  const area=$('messages'), distance=area.scrollHeight-area.scrollTop-area.clientHeight;
  area.replaceChildren();
  for(const m of t.ordered()) {
    const item=document.createElement('article'); item.className='message'+(m.sender_id===user?.user_id?' mine':'');
    item.dataset.messageId=m.message_id; item.dataset.seq=String(m.seq);
    const text=document.createElement('div');text.className='text';text.textContent=m.text;
    const meta=document.createElement('small');meta.className='meta';
    meta.textContent=new Date(m.created_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
    item.append(text,meta);area.append(item);
  }
  if(!t.messages.size) { const empty=document.createElement('p');empty.className='empty';empty.textContent='Начните разговор';area.append(empty); }
  $('load-older').hidden=!t.hasOlder;
  if(scroll || distance<80) area.scrollTop=area.scrollHeight;
}
async function loadDialogs() {
  let cursor=null;
  do {
    const page=await api('/dialogs?limit=100'+(cursor?'&cursor='+encodeURIComponent(cursor):''));
    for(const dialog of page.items) dialogs.set(dialog.dialog_id,dialog);
    cursor=page.has_more?page.next_cursor:null;
  } while(cursor);
  renderDialogs();
}
async function syncDialog(id, target) {
  const e=epoch, t=timeline(id);
  if(!t.initialized) {
    const page=await api(`/dialogs/${id}/messages?limit=20`);
    if(e!==epoch) return;
    t.initialize(page);
  }
  await t.syncTo(Math.max(target,t.latest),after=>api(`/dialogs/${id}/messages?after_seq=${after}&limit=100`));
  if(e===epoch && id===active) render();
}
async function selectDialog(id) {
  active=id; $('chat-feedback').textContent='';$('message-text').value=pending.get(id)?.text || '';
  $('message-text').disabled=false;$('send-button').disabled=false;renderDialogs();render();
  await syncDialog(id,dialogs.get(id)?.last_seq || 0);
  if(active===id) render(true);
}
async function readySync(g) {
  status('Синхронизация');
  await loadDialogs();
  const initialized=[...timelines.entries()].filter(([,t])=>t.initialized).map(([id])=>id);
  // Bound concurrent HTTP synchronization to three dialogues.
  for(let i=0;i<initialized.length;i+=3) {
    await Promise.all(initialized.slice(i,i+3).map(id=>syncDialog(id,dialogs.get(id)?.last_seq || 0)));
  }
  if(g===generation) { retry=0;status('Подключено');$('global-feedback').textContent=''; }
}
function schedule() {
  clearTimeout(retryTimer);
  if(!user) return;
  status('Переподключение');
  retryTimer=setTimeout(connect,Math.min(30000,1000*2**Math.min(retry++,5))+Math.random()*400);
}
async function onEvent(data,g) {
  if(g!==generation || !user) return;
  if(data.type==='ping') { socket?.send(JSON.stringify({type:'pong'}));return; }
  if(data.type==='pong') return;
  if(data.type==='ready') { await readySync(g);return; }
  if(data.type!=='message.created') throw new Error('Неизвестное событие');
  const m=data.message, t=timeline(m.dialog_id);
  t.ingest(m);
  if(!dialogs.has(m.dialog_id)) {
    const dialog=await api(`/dialogs/${m.dialog_id}`);
    if(g!==generation) return;
    dialogs.set(m.dialog_id,dialog);renderDialogs();
  }
  dialogs.get(m.dialog_id).last_seq=Math.max(dialogs.get(m.dialog_id).last_seq,m.seq);
  if(t.initialized && t.latest>t.cursor) await syncDialog(m.dialog_id,t.latest);
  if(active===m.dialog_id) render();
}
async function connect() {
  clearTimeout(retryTimer);
  const e=epoch, g=++generation;
  if(socket) socket.close();
  socket=null;
  try {
    if(expiresAt-Date.now()<35000) await refresh();
    if(e!==epoch || g!==generation || !user) return;
    status('Подключение');
    const ws=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws`);socket=ws;
    let deadline=setTimeout(()=>ws.close(),10000);
    ws.onopen=()=>ws.send(JSON.stringify({type:'auth',access_token:token}));
    ws.onmessage=event=> {
      if(g!==generation) return;
      try {
        const data=JSON.parse(event.data);
        if(data.type==='ready') clearTimeout(deadline);
        onEvent(data,g).catch(error=>{ if(g===generation){fail('global-feedback',error);ws.close();} });
      } catch(error){ ws.close(); }
    };
    ws.onclose=event=> {
      clearTimeout(deadline);
      if(g!==generation || e!==epoch) return;
      generation++; // Invalidate in-flight readySync and event work.
      if(event.code===4001) expiresAt=0;
      schedule();
    };
    ws.onerror=()=>ws.close();
  } catch(error) { if(e===epoch && user){fail('global-feedback',error);schedule();} }
}
async function enter() {
  user=await api('/auth/me');
  $('auth-form').hidden=true;$('workspace').hidden=false;$('current-user').textContent=user.username;
  await connect();
}
$('auth-toggle').onclick=()=> {
  registerMode=!registerMode;
  $('auth-title').textContent=registerMode?'Создать аккаунт':'Войти в Messenger';
  $('auth-submit').textContent=registerMode?'Зарегистрироваться':'Войти';
  $('auth-toggle').textContent=registerMode?'Уже есть аккаунт':'Создать аккаунт';
  $('password').autocomplete=registerMode?'new-password':'current-password';$('auth-feedback').textContent='';
};
$('auth-form').onsubmit=async event=> {
  event.preventDefault();$('auth-submit').disabled=true;$('auth-feedback').textContent='';
  const e=epoch, body=JSON.stringify({username:$('username').value,password:$('password').value});
  try {
    if(registerMode) await raw('/auth/register',{method:'POST',headers:authHeaders,body});
    const result=await raw('/auth/login',{method:'POST',headers:authHeaders,body});
    if(e!==epoch) return;
    channel?.postMessage('login'); acceptToken(result);$('password').value='';await enter();
  } catch(error){ if(e===epoch) fail('auth-feedback',error); }
  finally{$('auth-submit').disabled=false;}
};
$('logout').onclick=async()=> {
  try {
    await raw('/auth/logout',{method:'POST',headers:authHeaders,body:'{}'});
    channel?.postMessage('logout');stopSession();
  } catch(error) {fail('global-feedback',new Error('Не удалось завершить серверную сессию. Повторите выход.'));}
};
$('refresh-dialogs').onclick=()=>loadDialogs().catch(e=>fail('dialog-feedback',e));
$('new-dialog-form').onsubmit=async event=> {
  event.preventDefault();$('dialog-feedback').textContent='';
  try {
    const peer=await api('/users/'+encodeURIComponent($('peer-name').value));
    const dialog=await api('/dialogs',{method:'POST',body:JSON.stringify({peer_user_id:peer.user_id})});
    dialogs.set(dialog.dialog_id,dialog);await selectDialog(dialog.dialog_id);$('peer-name').value='';
  } catch(error){fail('dialog-feedback',error);}
};
$('load-older').onclick=async()=> {
  const id=active, e=epoch, t=timeline(id), area=$('messages'), height=area.scrollHeight;
  $('load-older').disabled=true;
  try {
    const page=await api(`/dialogs/${id}/messages?before_seq=${t.oldest}&limit=20`);
    if(e!==epoch) return;
    t.older(page);if(id===active){render();area.scrollTop=area.scrollHeight-height;}
  }catch(error){fail('chat-feedback',error);}
  finally{$('load-older').disabled=false;}
};
$('send-form').onsubmit=async event=> {
  event.preventDefault();const id=active, e=epoch,text=$('message-text').value; let saved=false;
  if(!id || !text.trim()) return;
  const request=pending.get(id)?.text===text?pending.get(id):{text,client_message_id:crypto.randomUUID()};
  pending.set(id,request);$('send-button').disabled=true;$('chat-feedback').textContent='Сохранение…';
  try {
    const m=await api(`/dialogs/${id}/messages`,{method:'POST',body:JSON.stringify(request)});
    if(e!==epoch) return;
    timeline(id).ingest(m);pending.delete(id);saved=true;
    if(id===active){$('message-text').value='';$('chat-feedback').textContent='Сохранено';render(true);}
    await syncDialog(id,m.seq);
  } catch(error){ if(e===epoch)fail('chat-feedback',new Error((saved?'Сообщение сохранено; обновление истории задерживается. ':'Отправка не подтверждена. Повторите с тем же текстом. ')+error.message)); }
  finally {if(e===epoch)$('send-button').disabled=!active;}
};
$('message-text').onkeydown=event=> {
  if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();if(!$('send-button').disabled)$('send-form').requestSubmit();}
};
(async()=>{try{await refresh();await enter();}catch(error){if(error.status!==401)fail('auth-feedback',error);}})();
