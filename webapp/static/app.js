'use strict';
const $ = id => document.getElementById(id);
const seatNames = {GENERAL_ONLY:'일반실만',GENERAL_FIRST:'일반실 우선',SPECIAL_ONLY:'특실만',SPECIAL_FIRST:'특실 우선'};
const stateNames = {idle:'검색 전',login:'로그인 중',searching:'조회 중',reserving:'예약 결과 확인 중',stopping:'중지 처리 중',success:'예약 완료 · 결제 필요',error:'확인 필요',uncertain:'예약내역 확인 필요',stopped:'검색 종료'};
let alternatives = [], pendingReview = null, currentState = null, busy = false, pendingShown = false, historyVersion = '';
const time = value => value ? new Intl.DateTimeFormat('ko-KR',{timeZone:'Asia/Seoul',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(value) : '—';
const deadline = value => new Intl.DateTimeFormat('ko-KR',{timeZone:'Asia/Seoul',month:'long',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false}).format(value);

async function api(path, value) {
  const options = value === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name=csrf-token]').content},body:JSON.stringify(value)};
  const response = await fetch(path,{...options,cache:'no-store'});
  let result;
  try { result = await response.json(); } catch { throw new Error('서버 응답을 읽지 못했습니다. 페이지를 새로고침하세요.'); }
  if (!response.ok) throw new Error(result.error || '요청을 처리하지 못했습니다.');
  return result;
}
function showError(error) { $('form-error').textContent = error.message; $('form-error').focus(); }
function describe(value) {
  const date=`${value.date.slice(0,4)}-${value.date.slice(4, 6)}-${value.date.slice(6)}`;
  const start=`${value.start.slice(0,2)}:${value.start.slice(2)}`;
  const end =`${value.end.slice(0, 2)}:${value.end.slice(2)}`;

  return `${value.departure} → ${value.arrival} · ${date}\n`+`${start} ~ ${end} 이전 · `+`어른 ${value.passengers}명 · `+`경로 ${value.seniors ?? 0}명 · `+`어린이 ${value.children ?? 0}명 · `+`${seatNames[value.option]}`;
}
function condition() {
  return {departure:$('departure').value.trim(),arrival:$('arrival').value.trim(),date:$('date').value.replaceAll('-',''),start:$('start-time').value.replace(':',''),end:$('end-time').value.replace(':',''),passengers:Number($('passengers').value),seniors: Number($('seniors').value),children: Number($('children').value),option: $('seat').value};
}
function restoreCondition(value) {
  $('departure').value=value.departure; $('arrival').value=value.arrival;
  $('date').value=`${value.date.slice(0,4)}-${value.date.slice(4,6)}-${value.date.slice(6)}`;
  $('start-time').value=`${value.start.slice(0,2)}:${value.start.slice(2)}`;
  $('end-time').value =`${value.end.slice(0,2)}:${value.end.slice(2)}`;
  $('passengers').value=value.passengers;$('seniors').value = value.seniors ?? 0;
  $('children').value = value.children ?? 0;$('seat').value = value.option;
}
function renderAlternatives() {
  $('alternatives').replaceChildren();
  alternatives.forEach((value,index) => {
    const item=document.createElement('li'), label=document.createElement('p'), remove=document.createElement('button');
    label.textContent=describe(value); remove.textContent='삭제'; remove.type='button'; remove.className='secondary';
    remove.setAttribute('aria-label',`${value.departure} ${value.arrival} 추가 조건 삭제`);
    remove.disabled=Boolean(currentState?.running);
    remove.addEventListener('click',()=>{alternatives.splice(index,1); renderAlternatives(); resetReview();});
    item.append(label,remove); $('alternatives').append(item);
  });
  $('add-alternative').disabled=alternatives.length>=2 || Boolean(currentState?.running);
}
function collect() {
  const value={username:$('username').value.trim(),password:$('password').value,use_saved_password:$('use-saved-password').checked,
    remember_id:$('remember-id').checked,remember_password:$('remember-password').checked,conditions:[condition(),...alternatives],station_access:null};
  if ($('use-access').checked) {
    const stations=[];
    for (let i=0;i<3;i++) {
      const name=$(`access-station-${i}`).value.trim(), minutes=$(`access-minutes-${i}`).value;
      if (!name && !minutes) continue;
      if (!name || minutes==='') throw new Error('역 이름과 이동 시간을 함께 입력하세요.');
      stations.push({name,minutes:Number(minutes)});
    }
    value.station_access={baseline:value.conditions[0].departure,stations,extra_minutes:Number($('extra-minutes').value),boarding_minutes:Number($('boarding-minutes').value)};
  }
  return value;
}
function resetReview() {
  pendingReview=null; $('review').hidden=true; $('start-bar').hidden=false; $('confirm-check').checked=false; $('confirm-start').disabled=true;
}
function controls() {
  const running=Boolean(currentState?.running), pending=Boolean(currentState?.payment_pending || currentState?.attention_pending);
  $('condition-fields').disabled=running || busy;
  $('start-button').disabled=running || pending || busy;
  $('start-button').textContent=running?'조회 중':pending?'예약내역 확인 필요':'검색 시작';
  $('stop-button').hidden=!running; $('stop-button').disabled=busy || currentState?.kind==='stopping';
  $('confirm-start').disabled=busy || running || !$('confirm-check').checked;
  $('edit-conditions').disabled=busy;
  $('ack-button').disabled=busy || !$('ack-check').checked;
}
function renderState(value) {
  const runningChanged=Boolean(currentState?.running)!==Boolean(value.running);
  const justStarted=!currentState?.running && value.running;
  currentState=value;
  $('state-label').textContent=stateNames[value.kind] || '확인 필요';
  if ($('state-message').textContent!==value.message) $('state-message').textContent=value.message;
  $('attempts').textContent=`${value.attempts || 0}회`; $('updated').textContent=time(value.updated);
  const pending=Boolean(value.payment_pending || value.attention_pending);
  $('payment-panel').hidden=!pending;
  if (pending!==pendingShown) $('ack-check').checked=false;
  pendingShown=pending;
  $('payment-deadline').textContent=value.payment_deadline ? `결제 기한 ${deadline(value.payment_deadline)}${Date.now()>value.payment_deadline?' · 기한이 지났을 수 있습니다.':''}` : '서버에서 확인한 결제 기한이 없습니다. 코레일톡에서 확인하세요.';
  const version=JSON.stringify(value.history || []);
  if (historyVersion!==version) {
    historyVersion=version; $('history').replaceChildren();
    [...(value.history || [])].reverse().forEach(entry=>{const item=document.createElement('li'), date=document.createElement('time'), message=document.createElement('span');date.textContent=time(entry.at); message.textContent=entry.message; item.append(date,message); $('history').append(item);});
  }
  if (justStarted) { resetReview(); $('password').value=''; }
  controls(); if(runningChanged)renderAlternatives();
}
async function poll() {
  try { renderState(await api('/api/state')); $('connection-error').textContent=''; }
  catch { $('connection-error').textContent='상태 연결이 끊겼습니다. 다시 연결 중입니다. 실행 중인 예약은 코레일톡에서 확인하세요.'; }
  setTimeout(poll,2000);
}
async function action(callback) {
  if (busy) return;
  busy=true; controls(); $('form-error').textContent='';
  try { await callback(); } catch(error) { showError(error); }
  finally { busy=false; controls(); }
}
$('search-form').addEventListener('submit',event=>{
  event.preventDefault();
  action(async()=>{
    const value=collect(), result=await api('/api/preview',value);
    pendingReview={...value,confirmed:true,confirmation:result.confirmation};
    $('review-list').replaceChildren();
    result.conditions.forEach(value=>{const item=document.createElement('li'),label=document.createElement('p');label.textContent=describe(value);item.append(label);$('review-list').append(item);});
    $('review').hidden=false; $('start-bar').hidden=true; $('confirm-check').checked=false; $('review-heading').focus(); $('review').scrollIntoView({block:'center'});
  });
});
$('confirm-start').addEventListener('click',()=>action(async()=>{
  if (!pendingReview || !$('confirm-check').checked) return;
  renderState(await api('/api/start',pendingReview)); $('password').value=''; resetReview();
  const saved=await api('/api/settings'); $('saved-password-row').hidden=!saved.has_password; $('use-saved-password').checked=Boolean(saved.has_password);
}));
$('edit-conditions').addEventListener('click',()=>{resetReview();$('departure').focus();});
$('confirm-check').addEventListener('change',controls);
$('stop-button').addEventListener('click',()=>action(async()=>renderState(await api('/api/stop',{}))));
$('ack-check').addEventListener('change',controls);
$('ack-button').addEventListener('click',()=>action(async()=>{if($('ack-check').checked)renderState(await api('/api/acknowledge',{confirmed:true}));}));
$('swap').addEventListener('click',()=>{[$('departure').value,$('arrival').value]=[$('arrival').value,$('departure').value]; resetReview();});
$('use-access').addEventListener('change',()=>{$('access-fields').hidden=!$('use-access').checked;resetReview();});
$('remember-password').addEventListener('change',()=>{if($('remember-password').checked)$('remember-id').checked=true;});
$('remember-id').addEventListener('change',()=>{if(!$('remember-id').checked)$('remember-password').checked=false;});
$('condition-fields').addEventListener('input',resetReview);
$('add-alternative').addEventListener('click',()=>{
  if (alternatives.length>=2) return;
  if (!$('departure').value.trim() || !$('arrival').value.trim() || !$('date').value) {showError(new Error('역 이름과 날짜를 입력하세요.'));return;}
  const value=condition();
  if (alternatives.some(other=>JSON.stringify(other)===JSON.stringify(value))) {showError(new Error('이미 추가한 조건입니다.'));return;}
  alternatives.push(value); renderAlternatives(); resetReview();
});
$('clear-credentials').addEventListener('click',()=>action(async()=>{
  await api('/api/credentials/clear',{}); $('password').value=''; $('username').value=''; $('saved-password-row').hidden=true;
  $('use-saved-password').checked=false; $('remember-id').checked=false; $('remember-password').checked=false;
  $('credential-error').textContent='저장된 로그인 정보를 삭제했습니다.'; resetReview();
}));
async function initialize() {
  $('date').value=new Date(Date.now()+9*3600000+86400000).toISOString().slice(0,10);
  busy=true;controls();
  try {
    const saved=await api('/api/settings');
    if(saved.conditions?.length) {restoreCondition(saved.conditions[0]);alternatives=saved.conditions.slice(1);renderAlternatives();}
    $('username').value=saved.username || ''; $('remember-id').checked=Boolean(saved.username); $('remember-password').checked=Boolean(saved.has_password);
    $('saved-password-row').hidden=!saved.has_password; $('use-saved-password').checked=Boolean(saved.has_password);
    $('credential-error').textContent=saved.credential_error || '';
    if(saved.has_password)$('login-section').open=false;
    if(saved.station_access) {
      $('use-access').checked=true; $('access-fields').hidden=false;
      saved.station_access.stations.forEach((value,i)=>{if(i<3){$(`access-station-${i}`).value=value.name;$(`access-minutes-${i}`).value=value.minutes;}});
      $('extra-minutes').value=saved.station_access.extra_minutes; $('boarding-minutes').value=saved.station_access.boarding_minutes;
    }
    renderState(await api('/api/state'));
  } catch(error) {showError(error);}
  finally {busy=false;controls();poll();}
}
initialize();
