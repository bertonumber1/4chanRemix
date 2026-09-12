"use strict";

// ── state ─────────────────────────────────────────────────────────────────────
let provs=[], es=null, jobStart=0;
let sessData=[], sessFilt='all', sessSort='status', sessSortAsc=true;
let libPage_=0, libStatus_='', libQ_='', libDebounce=null;
let toolLogES=null;

// ── tab switching ─────────────────────────────────────────────────────────────

// ── telegram control panel ───────────────────────────────────────────────────
let tgSeq_=0, tgTimer_=null, tgBusy_=false;

function tgOpts(){
  const n=id=>{const v=document.getElementById(id).value.trim();return v?Number(v):0;};
  return {dry_run:document.getElementById('tg-dry').checked,
          oldest:document.getElementById('tg-oldest').checked,
          archives:document.getElementById('tg-arch').checked,
          limit:n('tg-limit'), max_gb:n('tg-maxgb')};
}

async function tgRun(mode){
  const ch=document.getElementById('tg-channel').value.trim();
  const body={mode:mode, channel:ch, opts:tgOpts()};
  const log=document.getElementById('tg-job-log');
  log.textContent='starting…';
  try{
    const r=await fetch('/api/tg/scraper',{method:'POST',headers:{'Content-Type':'application/json'},
                                          body:JSON.stringify(body)});
    const d=await r.json();
    if(!d.ok){ log.textContent=d.reason||'could not start'; return; }
    tgSeq_=0; tgPoll(true);
  }catch(e){ log.textContent='request failed: '+e; }
}

async function tgStopJob(){
  await fetch('/api/tg/scraper/stop',{method:'POST'});
  tgPoll(true);
}

async function tgUploader(action){
  const note=document.getElementById('tg-up-note');
  try{
    const r=await fetch('/api/tg/uploader',{method:'POST',headers:{'Content-Type':'application/json'},
                                            body:JSON.stringify({action})});
    const d=await r.json();
    note.textContent=d.reason||'';
    if(!d.ok) note.style.color='#e0703f'; else note.style.color='';
  }catch(e){ note.textContent='request failed: '+e; }
  tgPoll(true);
}

function tgOpenUploadDir(){
  const p=(window.__tgUploadDir||'');
  if(!p){ alert('The uploader folder is not configured on this machine.'); return; }
  fetch('/api/open-folder?path='+encodeURIComponent(p))
    .then(r=>r.json()).then(d=>{ if(!d.ok) alert('Cannot open: '+(d.reason||'failed')); });
}

async function tgSet(key,val){
  const m=document.getElementById('tg-set-msg');
  m.textContent='saving…';
  const r=await fetch('/api/tg/settings',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({[key]:val})});
  const j=await r.json();
  m.textContent=j.ok?('saved '+key):('! '+(j.reason||'failed'));
  if(j.settings) tgPaintSettings(j.settings);
  setTimeout(()=>{m.textContent='';},2500);
}

function tgPaintSettings(s){
  if(!s) return;
  const set=(id,v,chk)=>{const e=document.getElementById(id); if(!e||document.activeElement===e)return;
    if(chk) e.checked=!!v; else e.value=(v===undefined||v===null)?'':v;};
  set('set-conns',s.conns); set('set-repack',s.repack_wav,true);
  set('set-reuse',s.reuse_duplicates,true); set('set-max',s.max_tracks);
  set('set-mark',s.mark_share); set('set-newyear',s.new_year);
  set('set-mpm',s.msg_per_min); set('set-pause',s.pause);
  set('set-root',s.root); set('set-quiet',s.quiet_hours);
  set('set-auth',s.check_authenticity,true); set('set-lossy',s.lossy_confidence);
}

async function tgTool(what){
  const out=document.getElementById('tg-tool-out'),
        st=document.getElementById('tg-tool-state');
  st.textContent='checking…'; out.textContent='';
  const r=await fetch('/api/tg/tools',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({what})});
  const j=await r.json();
  st.textContent=j.ok?'ok':'failed';
  out.textContent=j.output||j.reason||'(nothing)';
  setTimeout(()=>{st.textContent='';},4000);
}

async function tgAct(name,confirmFirst){
  if(confirmFirst && !confirm('This DELETES the posts that are in the wrong tab '
      +'and queues them to be posted again. Run the check first if unsure. Continue?')) return;
  const out=document.getElementById('tg-tool-out'),
        st=document.getElementById('tg-tool-state');
  st.textContent='running…'; out.textContent='(this can take a while)';
  const r=await fetch('/api/tg/action',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
  const j=await r.json();
  st.textContent=j.ok?'done':'failed';
  out.textContent=j.output||j.reason||'(nothing)';
}

function tgPaint(d){
  tgPaintSettings(d.settings);
  // ---- uploader ----
  const u=d.uploader||{}, dot=document.getElementById('tg-up-dot'),
        st=document.getElementById('tg-up-state'), tog=document.getElementById('tg-up-toggle');
  window.__tgUploadDir=u.dir||'';
  dot.className='tg-dot '+(u.running?'on':(u.configured?'off':''));
  if(!u.configured){ st.textContent='not on this machine'; tog.disabled=true; }
  else{ st.textContent=u.running?'posting':(u.stopped_by_latch?'stopped':'idle'); tog.disabled=false; }
  // reflect reality, but never fight the user mid-click
  if(!tgBusy_) tog.checked = u.configured && !u.stopped_by_latch;

  document.getElementById('tg-up-posted').textContent=(u.posted||0).toLocaleString();
  document.getElementById('tg-up-files').textContent=(u.files||0).toLocaleString();
  document.getElementById('tg-up-failed').textContent=(u.failed||0).toLocaleString();
  document.getElementById('tg-up-held').textContent=(u.held||0).toLocaleString();
  const hb=document.getElementById('tg-up-held-box');
  hb.hidden=!(u.held_rows&&u.held_rows.length);
  document.getElementById('tg-up-held-n').textContent=u.held?('('+u.held+')'):'';
  document.getElementById('tg-up-held-list').textContent=(u.held_rows||[]).join('\n');
  const pct=(u.total&&u.posted)?Math.min(100,u.posted/(u.posted+u.total)*100):0;
  document.getElementById('tg-up-bar').style.width=pct.toFixed(1)+'%';
  document.getElementById('tg-up-last').textContent=u.last?('last: '+u.last):'';
  const err=document.getElementById('tg-up-err');
  err.hidden=!u.last_error; err.textContent=u.last_error||'';
  const ul=document.getElementById('tg-up-log');
  ul.textContent=(u.log_tail&&u.log_tail.length)?u.log_tail.join('\n'):'';

  // ---- scraper job ----
  const j=d.job||{};
  document.getElementById('tg-job-dot').className='tg-dot '+(j.running?'on':'');
  document.getElementById('tg-job-state').textContent=
      j.running?(j.label||'running')+' · '+j.elapsed+'s'
               :(j.rc===null||j.rc===undefined?'idle':'exit '+j.rc);
  document.getElementById('tg-stop').disabled=!j.running;
  if(j.lines&&j.lines.length){
    const box=document.getElementById('tg-job-log');
    const stick=box.scrollTop+box.clientHeight>=box.scrollHeight-24;
    if(tgSeq_===0) box.textContent='';
    box.textContent+=(box.textContent?'\n':'')+j.lines.join('\n');
    if(stick) box.scrollTop=box.scrollHeight;
  }
  if(typeof j.seq==='number') tgSeq_=j.seq;

  const pa=d.paths||{};
  document.getElementById('tg-paths').textContent=
    'scraper: '+(pa.scraper||'—')+(pa.scraper_ok?'':'  (NOT FOUND)')+
    '   ·   uploader: '+(pa.upload_dir||'—')+
    '   ·   host: '+(d.platform||'?');
}

async function tgPoll(force){
  if(document.getElementById('tab-telegram')===null) return;
  const visible=document.getElementById('tab-telegram').classList.contains('active');
  if(!visible&&!force) return;
  try{
    const r=await fetch('/api/tg/status?since='+tgSeq_);
    const d=await r.json();
    if(d.ok===false){ document.getElementById('tg-paths').textContent=d.reason||''; return; }
    tgPaint(d);
  }catch(e){ /* transient; next tick retries */ }
}

function tgStartPolling(){
  if(tgTimer_) return;
  tgTimer_=setInterval(tgPoll,2000);
  tgPoll(true);
}
document.addEventListener('DOMContentLoaded',tgStartPolling);
// don't let the toggle flicker back while a click is in flight
document.addEventListener('DOMContentLoaded',()=>{
  const t=document.getElementById('tg-up-toggle');
  if(t){ t.addEventListener('mousedown',()=>{tgBusy_=true;
         setTimeout(()=>{tgBusy_=false;},1500);}); }
});

function switchTab(name){
  if(name==='tools') setTimeout(loadNaming,0);
  document.querySelectorAll('.tab-content').forEach(el=>el.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(el=>el.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');
  // Match on data-tab, never on the button's TEXT. The old version compared
  // b.textContent against a hard-coded English map, so it already missed
  // Telegram and SPEK-TRO (renamed from 'Fake-FLAC') and left no tab
  // highlighted — and under a translation it would have matched nothing at all.
  document.querySelectorAll('.tab-btn[data-tab="'+name+'"]').forEach(b=>b.classList.add('active'));
  if(name==='session') loadSession();
  if(name==='library'){ loadLibraryStats(); loadLibrary(0); }
  if(name==='tools'){ loadLibraryStats(); }
  if(name==='fakeflac'){ loadFakeflacSuspects(0); checkVampAvailable(); }
  if(name==='labels') lbInit();
}

// ── init ─────────────────────────────────────────────────────────────────────
async function init(){
  const r=await fetch('/api/config'); const c=await r.json();
  document.getElementById('src-in').value  = (c.sources||[])[0]||'';
  document.getElementById('dest-in').value = c.destination_root||'';
  document.getElementById('direct-src-in').value  = (c.sources||[])[0]||'';
  document.getElementById('direct-dest-in').value = c.destination_root||'';
  provs = c.providers||[];
  renderProviders();
  renderProvidersDirect();
}

// ── providers ─────────────────────────────────────────────────────────────────
function renderProviders(){
  document.getElementById('prov-list').innerHTML = provs.map(p=>{
    const badge = p.requires_auth
      ? (p.has_key ? `<span class="badge set" title="${p.key_hint}">key ✓</span>`
                   : `<span class="badge unset">no key</span>`) : '';
    const keyInput = p.key_field ? `
      <input class="prov-key" id="key-${p.id}" type="password"
             placeholder="${p.has_key ? '(keep existing)' : 'paste key…'}">
      <button class="key-eye" onclick="toggleKey('${p.id}')" title="Show/hide the key">👁</button>` : '';
    return `<div class="prov-row">
      <input type="checkbox" id="p-${p.id}" ${p.enabled?'checked':''}
             onchange="setProv('${p.id}',this.checked)" title="Enable/disable this metadata provider">
      <label for="p-${p.id}">${p.name||p.id}</label>
      ${badge}${keyInput}
    </div>`;
  }).join('');
}
function toggleKey(id){ const el=document.getElementById('key-'+id); if(el) el.type=el.type==='password'?'text':'password'; }
function setProv(id,on){
  const p=provs.find(x=>x.id===id); if(p) p.enabled=on;
  const dp=document.getElementById('dp-'+id); if(dp) dp.checked=on;
  const pp=document.getElementById('p-'+id); if(pp) pp.checked=on;
}
function renderProvidersDirect(){
  document.getElementById('direct-prov-list').innerHTML = provs.map(p=>`
    <div class="prov-row">
      <input type="checkbox" id="dp-${p.id}" ${p.enabled?'checked':''}
             onchange="setProv('${p.id}',this.checked)">
      <label for="dp-${p.id}">${p.name||p.id}</label>
      ${p.requires_auth ? (p.has_key ? `<span class="badge set">key ✓</span>` : `<span class="badge unset">no key</span>`) : ''}
    </div>`).join('');
}

// ── save config ───────────────────────────────────────────────────────────────
// The button says "hide until the next reload", so the 60-second re-check
// must not put the bar straight back up a moment later.
let doctorDismissed=false;
function dismissDoctor(){
  doctorDismissed=true;
  document.getElementById('doctor-bar').hidden=true;
}
async function checkDoctor(){
  if(doctorDismissed) return;
  try{
    const d=await (await fetch('/api/doctor')).json();
    const bar=document.getElementById('doctor-bar');
    if(d.ok||!d.problems||!d.problems.length){ bar.hidden=true; return; }
    document.getElementById('doctor-text').innerHTML=
      '<b>Check the configuration</b> — '+d.problems.map(p=>p.replace(/</g,'&lt;')).join(' · ');
    bar.hidden=false;
  }catch(e){}
}
checkDoctor();
setInterval(checkDoctor, 60000);

async function saveConfig(){
  const src=document.getElementById('src-in').value.trim();
  const dest=document.getElementById('dest-in').value.trim();
  const providerUpdates={};
  for(const p of provs){
    if(!p.key_field) continue;
    const val=(document.getElementById('key-'+p.id)?.value||'').trim();
    if(val) providerUpdates[p.id]={[p.key_field]:val};
  }
  const btn=document.getElementById('save-btn');
  btn.textContent='Saving…';
  const r=await fetch('/api/config/save',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({paths:{sources:src?[src]:[],destination_root:dest},providers:providerUpdates})});
  const j=await r.json();
  if(j.ok){
    btn.textContent='Saved ✓'; btn.className='hdr-btn ok';
    setTimeout(()=>{btn.textContent='Save config';btn.className='hdr-btn';},2500);
    const cr=await fetch('/api/config'); const cc=await cr.json();
    provs=cc.providers||[]; renderProviders();
    provs.forEach(p=>{ const el=document.getElementById('key-'+p.id); if(el) el.value=''; });
  } else {
    btn.textContent='Error'; btn.className='hdr-btn err';
    setTimeout(()=>{btn.textContent='Save config';btn.className='hdr-btn';},3000);
    appendLog('broken','config save: '+(j.error||'unknown'));
  }
}

async function restartService(force){
  const btn=document.getElementById('restart-btn');
  if(!force && !confirm('Restart the music-organiser service now?'+
      ' This drops your connection for a few seconds while it comes back up.')) return;
  btn.textContent='Restarting…'; btn.disabled=true;
  const r=await fetch('/api/restart',{method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({force:!!force})});
  if(r.status===409){
    btn.textContent='Restart service'; btn.disabled=false;
    if(confirm('A job is currently running — restarting will interrupt it. Restart anyway?'))
      return restartService(true);
    return;
  }
  const j=await r.json().catch(()=>({}));
  if(!r.ok || !j.ok){
    btn.textContent='Error'; btn.className='hdr-btn err';
    appendLog('broken','restart failed: '+(j.error||r.status));
    setTimeout(()=>{btn.textContent='Restart service';btn.className='hdr-btn';btn.disabled=false;},3000);
    return;
  }
  // Service is about to go down — poll /api/health until it's back, then reload the page.
  let tries=0;
  const poll=setInterval(async ()=>{
    tries++;
    try{
      const hr=await fetch('/api/health',{cache:'no-store'});
      if(hr.ok){ clearInterval(poll); location.reload(); return; }
    }catch(e){ /* still down, keep polling */ }
    btn.textContent='Restarting… ('+tries+'s)';
    if(tries>60){ clearInterval(poll); btn.textContent='Still down?'; btn.className='hdr-btn err'; }
  },1000);
}

// ── filesystem browser ────────────────────────────────────────────────────────
let browserTarget_='src';


// ── show a folder in the desktop file manager ────────────────────────────────
// Explorer on Windows, Finder on macOS, xdg-open on Linux. Same desktop caveat
// as the picker: it draws on the SERVER's desktop, so from another machine the
// button just reports that and does nothing.
async function openInFileManager(inputId,btn){
  const inp=document.getElementById(inputId);
  const path=(inp&&inp.value||'').trim();
  if(!path){ alert('Pick a folder first'); return; }
  const label=btn?btn.textContent:'';
  if(btn){ btn.disabled=true; btn.textContent='…'; }
  try{
    const r=await fetch('/api/open-folder?path='+encodeURIComponent(path));
    const d=await r.json();
    if(!d.ok) alert('Cannot open folder: '+(d.reason||'failed'));
  }catch(e){ alert('Cannot open folder'); }
  finally{ if(btn){ btn.disabled=false; btn.textContent=label; } }
}
// ── native folder picker ──────────────────────────────────────────────────────
// Opens a real GTK folder chooser on this machine's own desktop. If there's no
// display to draw on (you're on another machine), fall back to the in-page tree.
async function nativePick(target,btn){
  const inp=document.getElementById(target==='src'?'src-in':'dest-in');
  if(btn) btn.disabled=true;
  try{
    const r=await fetch('/api/pick-folder?start='+encodeURIComponent(inp.value||''));
    const d=await r.json();
    if(d.ok&&d.path){
      inp.value=d.path;
      if(target==='src') scanSource();
      if(typeof saveConfig==='function') document.getElementById('save-btn').classList.add('dirty');
    } else if(d.reason!=='cancelled'){
      // No native dialog available on this machine — fall back to the
      // in-page picker, which works from anywhere. Silent, per
      // /api/pick-folder's own docstring: this is the expected path on
      // Linux/headless, not an error worth interrupting the user over.
      openBrowser(target);
    }
  }catch(e){ openBrowser(target); }
  finally{ if(btn) btn.disabled=false; }
}
async function nativePickDirect(target,btn){
  const inp=document.getElementById(target==='src'?'direct-src-in':'direct-dest-in');
  if(btn) btn.disabled=true;
  try{
    const r=await fetch('/api/pick-folder?start='+encodeURIComponent(inp.value||''));
    const d=await r.json();
    if(d.ok&&d.path){
      inp.value=d.path;
      if(target==='src') scanSourceDirect();
    } else if(d.reason!=='cancelled'){
      openBrowser('direct-'+target);
    }
  }catch(e){ openBrowser('direct-'+target); }
  finally{ if(btn) btn.disabled=false; }
}
async function scanSource(){
  const p=document.getElementById('src-in').value.trim(); if(!p)return;
  document.getElementById('scan-result').textContent='scanning…';
  const r=await fetch('/api/scan?path='+encodeURIComponent(p));
  const d=await r.json();
  document.getElementById('scan-result').textContent=
    d.error?d.error:d.count.toLocaleString()+' files';
}
function J(s){ return JSON.stringify(s); }

// ── file browser ────────────────────────────────────────────────────────────
// Every row carries its path in a data attribute and is handled by one
// delegated listener. The old tree built onclick="browse('...')" strings, so a
// folder name containing a quote broke the row silently — and this library is
// full of names like "DJ Konik - Don't You Want Me".
const FB_TARGETS={
  'src':        {input:'src-in',        label:'SOURCE folder to read from'},
  'dest':       {input:'dest-in',       label:'OUTPUT folder to file into'},
  'direct-src': {input:'direct-src-in', label:'SOURCE folder (direct mode)'},
  'direct-dest':{input:'direct-dest-in',label:'OUTPUT folder (direct mode)'},
  'ff-folder':  {input:'ff-folder-in',  label:'folder to check for fake FLACs'},
};
let fbTarget_=null, fbPath_='', fbSel_='', fbResolve_=null;

async function openBrowser(target){
  fbTarget_=target;
  const t=FB_TARGETS[target]||{label:'folder'};
  document.getElementById('fb-title').textContent='Choose the '+t.label;
  document.getElementById('fb-overlay').classList.remove('hidden');
  try{
    const pl=await (await fetch('/api/fs/places')).json();
    document.getElementById('fb-places').innerHTML=(pl.places||[]).map(p=>
      `<button data-go="${esc(p.path)}" title="${esc(p.path)}">${esc(p.label)}</button>`).join('');
  }catch(e){}
  const cur=(document.getElementById(t.input)||{}).value||'';
  fbGo(cur||'');
}
function closeBrowser(){
  document.getElementById('fb-overlay').classList.add('hidden');
  fbTarget_=null;
  if(fbResolve_){ const r=fbResolve_; fbResolve_=null; r(''); }
}
function fbUp(){
  const el=document.getElementById('fb-list');
  const up=el.getAttribute('data-parent');
  if(up) fbGo(up);
}
async function fbGo(path){
  const list=document.getElementById('fb-list');
  list.innerHTML='<div class="fb-empty">reading…</div>';
  let d;
  try{
    d=await (await fetch('/api/fs?path='+encodeURIComponent(path||''))).json();
  }catch(e){
    list.innerHTML='<div class="fb-empty">could not read that folder</div>'; return;
  }
  fbPath_=d.path||''; fbSel_=fbPath_;
  document.getElementById('fb-path').value=fbPath_;
  document.getElementById('fb-sel').textContent=fbPath_||'—';
  list.setAttribute('data-parent',d.parent||'');
  document.getElementById('fb-note').textContent =
    d.audio ? (d.audio.toLocaleString()+' audio file(s) directly in here') : '';
  if(d.error){
    list.innerHTML='<div class="fb-empty">'+esc(d.error)+'</div>'; return;
  }
  const rows=(d.dirs||[]).map(x=>{
    const c = x.audio>0 ? `<span class="cnt has">${x.audio.toLocaleString()} audio</span>`
            : x.audio<0 ? '<span class="cnt">unreadable</span>' : '<span class="cnt"></span>';
    return `<div class="fb-row" data-go="${esc(x.path)}" data-pick="${esc(x.path)}">
      <span class="ic">▶</span><span class="nm">${esc(x.name)}</span>${c}</div>`;
  });
  list.innerHTML = rows.join('') || (
    '<div class="fb-empty">No sub-folders here.'+
    (d.audio?' The audio is in this folder itself — press <b>Use this folder</b>.':'')+
    '</div>');
}
function fbUse(){
  if(fbResolve_){
    const r=fbResolve_; fbResolve_=null;
    document.getElementById('fb-overlay').classList.add('hidden');
    fbTarget_=null; r(fbSel_); return;
  }
  if(!fbTarget_) return;
  const t=FB_TARGETS[fbTarget_];
  const inp=document.getElementById(t.input);
  if(inp){
    inp.value=fbSel_;
    inp.dispatchEvent(new Event('change'));
  }
  if(fbTarget_==='ff-folder'){ closeBrowser(); return; }
  closeBrowser();
  if(typeof saveConfig==='function' && (fbTarget_==='src'||fbTarget_==='dest'))
    document.getElementById('save-btn').classList.add('dirty');
}
document.addEventListener('click',ev=>{
  const go=ev.target.closest('[data-go]');
  if(go && document.getElementById('fb-overlay') &&
     !document.getElementById('fb-overlay').classList.contains('hidden')){
    ev.preventDefault();
    // one click selects, a second opens — so you can pick a folder you can see
    const pick=go.getAttribute('data-pick');
    if(pick && fbSel_!==pick){
      fbSel_=pick;
      document.getElementById('fb-sel').textContent=pick;
      document.querySelectorAll('.fb-row.sel').forEach(r=>r.classList.remove('sel'));
      go.classList.add('sel');
      return;
    }
    fbGo(go.getAttribute('data-go'));
  }
});
document.addEventListener('keydown',ev=>{
  const ov=document.getElementById('fb-overlay');
  if(!ov || ov.classList.contains('hidden')) return;
  if(ev.key==='Escape') closeBrowser();
  if(ev.key==='Backspace' && ev.target.id!=='fb-path'){ ev.preventDefault(); fbUp(); }
});

// ── pipeline jobs ─────────────────────────────────────────────────────────────
function getJobBody(){
  return {
    sources:[document.getElementById('src-in').value.trim()].filter(Boolean),
    dest:document.getElementById('dest-in').value.trim(),
    providers:provs.filter(p=>p.enabled).map(p=>p.id),
    dry_run:document.getElementById('dry-run').checked,
  };
}
async function run(kind){
  const body=getJobBody();
  if(body.dry_run) appendLog('warning','DRY RUN — '+kind+': nothing will be written');
  const r=await fetch('/api/job/'+kind,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const j=await r.json();
  if(j.error){ appendLog('broken',j.error); return; }
  setRunning(true,kind); jobStart=Date.now();
  if(es) es.close();
  es=new EventSource('/api/job/stream');
  es.onmessage=e=>{
    const m=JSON.parse(e.data);
    if(m.type==='log')      appendLog(m.level,m.text);
    else if(m.type==='progress') onProgress(m);
    else if(m.type==='phase')    onPhase(m);
    else if(m.type==='done'){
      setRunning(false,kind); es.close(); es=null;
      if(kind==='import'||kind==='pipeline'||kind==='fetch'||kind==='organise')
        setTimeout(loadSession,500);
    }
  };
  es.onerror=()=>{ setRunning(false,kind); if(es){es.close();es=null;} };
}
async function runDirectStep(kind){
  const body={
    sources:[document.getElementById('direct-src-in').value.trim()].filter(Boolean),
    dest:document.getElementById('direct-dest-in').value.trim(),
    providers:provs.filter(p=>p.enabled).map(p=>p.id),
    dry_run:document.getElementById('direct-dry-run').checked,
  };
  if(!body.sources.length){ alert('Pick a Source folder first.'); return; }
  if(kind!=='direct_scan' && !body.dest){ alert('Pick an Output folder first.'); return; }
  const r=await fetch('/api/job/'+kind,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const j=await r.json();
  if(j.error){ appendLogDirect('broken',j.error); return; }
  startDirectStream(kind);
}

async function stopJob(btn){
  // Say so immediately: the request lands at once, but the worker finishes the
  // file it is on, and silence for a second reads as "the button did nothing".
  const btns=[document.getElementById('btn-stop'),document.getElementById('direct-btn-stop')];
  btns.forEach(b=>{ if(b){ b.disabled=true; b.textContent='■ stopping…'; }});
  const s=document.getElementById('hdr-status');
  if(s){ s.textContent='● stopping…'; s.style.color='var(--warn)'; }
  try{ await fetch('/api/job/stop',{method:'POST'}); }catch(e){}
  setTimeout(()=>{ btns.forEach(b=>{ if(b) b.textContent='■ Stop'; }); }, 4000);
}

const ACTION_BTNS=['btn-all','btn-import','btn-fetch','btn-organise'];
function setRunning(on,kind){
  ACTION_BTNS.forEach(id=>{ document.getElementById(id).disabled=on; });
  const sb=document.getElementById('btn-stop');
  sb.disabled=!on; if(!on) sb.textContent='■ Stop';
  const s=document.getElementById('hdr-status');
  s.textContent=on?'● '+kind:'idle'; s.style.color=on?'var(--ok)':'var(--dim)';
  if(!on){
    document.getElementById('prog').style.width='0%';
    ['import','fetch','organise'].forEach(p=>{
      const el=document.getElementById('ph-'+p);
      if(el&&el.classList.contains('running')) el.className='phase';
    });
  }
}
function onProgress(m){
  const pct=m.total>0?(m.done/m.total*100).toFixed(1):0;
  document.getElementById('prog').style.width=pct+'%';
  if(m.imported!==undefined) document.getElementById('s-imp').textContent='imported: '+m.imported.toLocaleString();
  if(m.duplicate!==undefined) document.getElementById('s-dup').textContent='duplicate: '+m.duplicate.toLocaleString();
  if(m.broken!==undefined) document.getElementById('s-bad').textContent='broken: '+m.broken.toLocaleString();
  document.getElementById('s-prog').textContent=m.done.toLocaleString()+' / '+m.total.toLocaleString();
  document.getElementById('s-el').textContent='elapsed: '+Math.round((Date.now()-jobStart)/1000)+'s';
}
function onPhase(m){
  const el=document.getElementById('ph-'+m.phase); if(!el) return;
  el.className='phase '+(m.status||'');
  if(m.status==='running')
    appendLog('phase-hdr','── '+m.phase.toUpperCase()+' ──────────────────────');
}
function appendLog(level,text){
  const el=document.getElementById('log');
  const ts=new Date().toTimeString().slice(0,8);
  const div=document.createElement('div');
  div.className='ll '+(level||'info');
  div.innerHTML=`<span class="ts">${ts}</span><span class="lv">${level}</span><span class="tx">${esc(text)}</span>`;
  el.appendChild(div); el.scrollTop=el.scrollHeight;
}
function clearLog(){ document.getElementById('log').innerHTML=''; }
function esc(s){ return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

// ── direct mode (no database — folder in, folder out) ──────────────────────────
let browserTargetDirect_='src';
let esDirect=null, jobStartDirect=0;
async function scanSourceDirect(){
  const p=document.getElementById('direct-src-in').value.trim(); if(!p)return;
  document.getElementById('direct-scan-result').textContent='scanning…';
  const r=await fetch('/api/scan?path='+encodeURIComponent(p));
  const d=await r.json();
  document.getElementById('direct-scan-result').textContent=
    d.error?d.error:d.count.toLocaleString()+' files';
}
async function runDirect(){
  const body={
    sources:[document.getElementById('direct-src-in').value.trim()].filter(Boolean),
    dest:document.getElementById('direct-dest-in').value.trim(),
    providers:provs.filter(p=>p.enabled).map(p=>p.id),
    dry_run:document.getElementById('direct-dry-run').checked,
  };
  if(!body.sources.length){ appendLogDirect('broken','set a source folder first'); return; }
  if(!body.dest){ appendLogDirect('broken','set an output folder first'); return; }
  if(body.dry_run) appendLogDirect('warning','DRY RUN — nothing will be written');
  const r=await fetch('/api/job/direct',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const j=await r.json();
  if(j.error){ appendLogDirect('broken',j.error); return; }
  setRunningDirect(true); jobStartDirect=Date.now();
  if(esDirect) esDirect.close();
  esDirect=new EventSource('/api/job/stream');
  esDirect.onmessage=e=>{
    const m=JSON.parse(e.data);
    if(m.type==='log')      appendLogDirect(m.level,m.text);
    else if(m.type==='progress') onProgressDirect(m);
    else if(m.type==='done'){ setRunningDirect(false); esDirect.close(); esDirect=null; }
  };
  esDirect.onerror=()=>{ setRunningDirect(false); if(esDirect){esDirect.close();esDirect=null;} };
}
function startDirectStream(kind){
  if(document.getElementById('direct-dry-run').checked)
    appendLogDirect('warning','DRY RUN — nothing will be written');
  setRunningDirect(true); jobStartDirect=Date.now();
  if(esDirect) esDirect.close();
  esDirect=new EventSource('/api/job/stream');
  esDirect.onmessage=e=>{
    const m=JSON.parse(e.data);
    if(m.type==='log')      appendLogDirect(m.level,m.text);
    else if(m.type==='progress') onProgressDirect(m);
    else if(m.type==='done'){ setRunningDirect(false); esDirect.close(); esDirect=null; }
  };
  esDirect.onerror=()=>{ setRunningDirect(false); if(esDirect){esDirect.close();esDirect=null;} };
}

const DIRECT_BTNS=['direct-btn-all','direct-btn-scan','direct-btn-tags','direct-btn-org'];
function setRunningDirect(on){
  DIRECT_BTNS.forEach(id=>{const b=document.getElementById(id); if(b) b.disabled=on;});
  const dsb=document.getElementById('direct-btn-stop');
  dsb.disabled=!on; if(!on) dsb.textContent='■ Stop';
  if(!on) document.getElementById('direct-prog').style.width='0%';
}
function onProgressDirect(m){
  const pct=m.total>0?(m.done/m.total*100).toFixed(1):0;
  document.getElementById('direct-prog').style.width=pct+'%';
  if(m.imported!==undefined) document.getElementById('direct-s-imp').textContent='imported: '+m.imported.toLocaleString();
  if(m.duplicate!==undefined) document.getElementById('direct-s-dup').textContent='duplicate: '+m.duplicate.toLocaleString();
  if(m.broken!==undefined) document.getElementById('direct-s-bad').textContent='broken: '+m.broken.toLocaleString();
  document.getElementById('direct-s-prog').textContent=m.done.toLocaleString()+' / '+m.total.toLocaleString();
  document.getElementById('direct-s-el').textContent='elapsed: '+Math.round((Date.now()-jobStartDirect)/1000)+'s';
}
function appendLogDirect(level,text){
  const el=document.getElementById('direct-log');
  const ts=new Date().toTimeString().slice(0,8);
  const div=document.createElement('div');
  div.className='ll '+(level||'info');
  div.innerHTML=`<span class="ts">${ts}</span><span class="lv">${level}</span><span class="tx">${esc(text)}</span>`;
  el.appendChild(div); el.scrollTop=el.scrollHeight;
}
function clearLogDirect(){ document.getElementById('direct-log').innerHTML=''; }

// ── session tab ───────────────────────────────────────────────────────────────
async function loadSession(){
  const r=await fetch('/api/session/files');
  const d=await r.json();
  sessData=d.files||[];
  const st=d.stats||{};
  document.getElementById('ss-total').textContent=(st.total||0).toLocaleString();
  document.getElementById('ss-imp').textContent=(st.imported||0).toLocaleString();
  document.getElementById('ss-brk').textContent=(st.broken||0).toLocaleString();
  document.getElementById('ss-dup').textContent=(st.duplicate||0).toLocaleString();
  const nb=document.getElementById('btn-refetch');
  nb.hidden=!(st.broken>0);
  nb.textContent='↺ Re-fetch broken ('+st.broken+')';
  renderSession();
}
function setSessFilter(f,btn){
  sessFilt=f;
  document.querySelectorAll('#tab-session .chip').forEach(c=>c.classList.remove('active'));
  btn.classList.add('active');
  renderSession();
}
function sortSess(col){
  if(sessSort===col) sessSortAsc=!sessSortAsc;
  else { sessSort=col; sessSortAsc=true; }
  renderSession();
}
function filterSession(){ renderSession(); }
function renderSession(){
  const q=(document.getElementById('sess-search').value||'').toLowerCase();
  let rows=sessData.filter(r=>{
    if(sessFilt!=='all'&&r.status!==sessFilt) return false;
    if(!q) return true;
    return (r.artist||'').toLowerCase().includes(q)||
           (r.album||'').toLowerCase().includes(q)||
           (r.title||'').toLowerCase().includes(q)||
           (r.filename||'').toLowerCase().includes(q);
  });
  rows.sort((a,b)=>{
    let av=a[sessSort]||'', bv=b[sessSort]||'';
    return sessSortAsc?(av<bv?-1:av>bv?1:0):(av<bv?1:av>bv?-1:0);
  });
  const COLS=['filename','artist','album','year','label','catalog_number','dur','status'];
  document.getElementById('sess-tbody').innerHTML=rows.map((r,i)=>`
    <tr style="cursor:pointer">
      <td class="ff-tick" onclick="event.stopPropagation()"><input type="checkbox" class="sess-cb" data-path="${esc(r.path||'')}" onchange="sessSelCount()"></td>
      <td onclick="showDetail(sessData,${sessData.indexOf(r)})" title="${esc(r.path||'')}">${esc(r.filename||'')}</td>
      <td onclick="showDetail(sessData,${sessData.indexOf(r)})">${esc(r.artist||'')}</td>
      <td onclick="showDetail(sessData,${sessData.indexOf(r)})">${esc(r.album||'')}</td>
      <td onclick="showDetail(sessData,${sessData.indexOf(r)})">${esc(r.year||'')}</td>
      <td onclick="showDetail(sessData,${sessData.indexOf(r)})">${esc(r.label||'')}</td>
      <td onclick="showDetail(sessData,${sessData.indexOf(r)})">${esc(r.catalog_number||'')}</td>
      <td onclick="showDetail(sessData,${sessData.indexOf(r)})">${esc(r.dur||'')}</td>
      <td onclick="showDetail(sessData,${sessData.indexOf(r)})"><span class="status-badge ${r.status||'unknown'}">${r.status||'?'}</span></td>
    </tr>`).join('');
  sessSelCount();
}

// ── session: manual work on a selection ─────────────────────────────────────
function sessSelectedPaths(){
  return Array.from(document.querySelectorAll('.sess-cb:checked'))
              .map(cb=>cb.dataset.path).filter(Boolean);
}
function sessSelCount(){
  const n=document.querySelectorAll('.sess-cb:checked').length,
        all=document.querySelectorAll('.sess-cb').length,
        el=document.getElementById('sess-sel-count');
  if(el) el.textContent=n?`${n} selected`:'0 selected';
  const m=document.getElementById('sess-all');
  if(m){ m.checked=n>0&&n===all; m.indeterminate=n>0&&n<all; }
}
function sessSelectAll(on){
  document.querySelectorAll('.sess-cb').forEach(cb=>{cb.checked=!!on;});
  sessSelCount();
}

async function sessAct(action){
  const paths=sessSelectedPaths();
  if(!paths.length){ alert('Tick some rows first.'); return; }
  let dest='';
  if(action==='move'||action==='copy'){
    dest=await pickFolderModal(action==='move'
      ? 'Move '+paths.length+' file(s) to…' : 'Export a copy of '+paths.length+' file(s) to…');
    if(!dest) return;
  }
  const words={move:'MOVE',copy:'copy',delete:'PERMANENTLY DELETE',
               forget:'remove from the session (files untouched)',
               retag:'re-read tags from disk for'};
  if(!confirm(`${words[action]} ${paths.length} file(s)?`+
              (dest?('\n\n→ '+dest):'')+
              (action==='delete'?'\n\nThis cannot be undone.':''))) return;
  const r=await fetch('/api/session/act',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action,paths,dest,db:'session'})});
  const j=await r.json();
  if(j.error){ alert(j.error); return; }
  if(j.failed&&j.failed.length){
    alert(`${j.done} done, ${j.failed.length} failed:\n`+
      j.failed.slice(0,8).map(f=>f.path.split('/').pop()+' — '+f.error).join('\n'));
  }
  loadSession();
}

function sessExportCsv(){
  const paths=new Set(sessSelectedPaths());
  const rows=sessData.filter(r=>paths.has(r.path));
  if(!rows.length){ alert('Tick some rows first.'); return; }
  const cols=['filename','artist','album','year','label','catalog_number','dur','status','path'];
  const q=v=>'"'+String(v===undefined||v===null?'':v).replace(/"/g,'""')+'"';
  const csv=[cols.join(',')].concat(rows.map(r=>cols.map(c=>q(r[c])).join(','))).join('\n');
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));
  a.download='session-'+new Date().toISOString().slice(0,10)+'.csv';
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(()=>URL.revokeObjectURL(a.href),10000);
}

// The file browser, used as a folder PICKER that resolves to a path.
function pickFolderModal(title){
  return new Promise(resolve=>{
    fbTarget_='__promise__';
    FB_TARGETS['__promise__']={input:'',label:'folder'};
    document.getElementById('fb-title').textContent=title||'Choose a folder';
    document.getElementById('fb-overlay').classList.remove('hidden');
    fetch('/api/fs/places').then(r=>r.json()).then(pl=>{
      document.getElementById('fb-places').innerHTML=(pl.places||[]).map(p=>
        `<button data-go="${esc(p.path)}" title="${esc(p.path)}">${esc(p.label)}</button>`).join('');
    }).catch(()=>{});
    fbGo('');
    fbResolve_=resolve;
  });
}
async function refetchBroken(){
  const body={
    sources:[document.getElementById('src-in').value.trim()].filter(Boolean),
    dest:document.getElementById('dest-in').value.trim(),
    providers:provs.filter(p=>p.enabled).map(p=>p.id),
    dry_run:false,
  };
  openToolLog('Re-fetching broken files…');
  const r=await fetch('/api/job/fetch_broken',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const j=await r.json();
  if(j.error){ appendToolLog('broken',j.error); return; }
  startToolStream(()=>{ loadSession(); });
}
async function commitSession(){
  const r=await fetch('/api/commit',{method:'POST'});
  const j=await r.json();
  if(j.error){ alert('Commit failed: '+j.error); return; }
  alert(`✓ Committed — library.db: ${j.before.toLocaleString()} → ${j.after.toLocaleString()} files (+${j.delta})`);
}

// ── library tab ───────────────────────────────────────────────────────────────
async function loadLibraryStats(){
  const r=await fetch('/api/library/stats');
  const d=await r.json();
  const lib=d.library||{};
  document.getElementById('ls-total').textContent=(lib.total||0).toLocaleString();
  document.getElementById('ls-artists').textContent=(lib.artists||0).toLocaleString();
  document.getElementById('ls-labels').textContent=(lib.labels||0).toLocaleString();
  document.getElementById('ls-size').textContent=(lib.size_gb||0).toFixed(1);
}
function setLibFilter(s,btn){
  libStatus_=s; libPage_=0;
  document.querySelectorAll('#tab-library .chip').forEach(c=>c.classList.remove('active'));
  btn.classList.add('active');
  loadLibrary(0);
}
function debounceLibSearch(){
  clearTimeout(libDebounce);
  libDebounce=setTimeout(()=>{ libQ_=document.getElementById('lib-search').value.trim(); libPage_=0; loadLibrary(0); },300);
}
function libPage(dir){
  const newPage=libPage_+dir;
  if(newPage<0) return;
  loadLibrary(newPage);
}
async function loadLibrary(page){
  libPage_=page;
  const params=new URLSearchParams({db:'library',q:libQ_,status:libStatus_,page,per_page:50});
  const r=await fetch('/api/library/files?'+params);
  const d=await r.json();
  document.getElementById('lib-page-info').textContent=`page ${(d.page||0)+1} of ${d.pages||1}`;
  document.getElementById('lib-count').textContent=`${(d.total||0).toLocaleString()} total`;
  document.getElementById('lib-tbody').innerHTML=(d.files||[]).map((r,i)=>`
    <tr onclick="showDetail(null,null,${J(r)})" style="cursor:pointer">
      <td>${esc(r.artist||'')}</td>
      <td>${esc(r.album||'')}</td>
      <td>${esc(r.title||'')}</td>
      <td>${esc(r.year||'')}</td>
      <td>${esc(r.label||'')}</td>
      <td>${esc(r.catalog_number||'')}</td>
      <td><span class="status-badge ${r.status||'unknown'}">${r.status||'?'}</span></td>
    </tr>`).join('');
}

// ── tools tab ─────────────────────────────────────────────────────────────────
async function runTool(kind,opts){
  const body=Object.assign({
    sources:[document.getElementById('src-in').value.trim()].filter(Boolean),
    dest:document.getElementById('dest-in').value.trim(),
    providers:provs.filter(p=>p.enabled).map(p=>p.id),
    dry_run:false,
  }, opts||{});
  const labels={rebuild:'Rebuilding library index…',vacuum:'Compacting library DB…',
                fetch_broken:'Re-fetching broken files…',
                tags_from_names:'Reading tags off the filenames…'};
  openToolLog(labels[kind]||kind+'…');
  const r=await fetch('/api/job/'+kind,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const j=await r.json();
  if(j.error){ appendToolLog('broken',j.error); return; }
  startToolStream(()=>{
    if(kind==='rebuild') loadLibraryStats();
    if(kind==='fetch_broken') loadSession();
  });
}

async function loadNaming(){
  try{
    const d=await (await fetch('/api/naming')).json();
    const box=document.getElementById('naming-schemes');
    box.innerHTML=(d.schemes||[]).map(sc=>`
      <label class="naming-opt${sc.active?' on':''}">
        <input type="radio" name="fscheme" value="${esc(sc.key)}" ${sc.active?'checked':''}
               onchange="setSetting('folder_scheme',this.value,loadNaming)">
        <div>
          <b>${esc(sc.label)}</b>
          <p class="naming-why">${esc(sc.blurb)}</p>
          ${(sc.examples||[]).map(e=>`<code class="naming-ex" title="${esc(e.note)}">${esc(e.path)}</code>`).join('')}
        </div>
      </label>`).join('');
    const cfg=await (await fetch('/api/config')).json();
    const im=document.getElementById('set-import-mode');
    if(im && cfg && cfg.import_mode) im.value=cfg.import_mode;
    const doel=document.getElementById('set-del-orphans');
    if(doel && cfg && cfg.delete_orphaned_extras!==undefined)
      doel.checked=!!cfg.delete_orphaned_extras;
  }catch(e){}
}

async function setSetting(key,value,after){
  const m=document.getElementById('naming-msg');
  m.textContent='saving…';
  const r=await fetch('/api/settings',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({key,value})});
  const j=await r.json();
  m.textContent=j.ok?('saved — '+key):('! '+(j.reason||'failed'));
  if(j.ok&&after) after();
  checkDoctor();
  setTimeout(()=>{m.textContent='';},3000);
}

async function runAudits(){
  const resultEl=document.getElementById('audit-results');
  resultEl.innerHTML='<div style="color:var(--dim);font-size:11px;padding:4px 0">running…</div>';
  const r=await fetch('/api/library/audits');
  const d=await r.json();
  if(d.error){ resultEl.innerHTML=`<div style="color:var(--err);font-size:11px">${esc(d.error)}</div>`; return; }
  const rows=(d.audits||[]).map(a=>`
    <div class="audit-row ${a.count>0?'has-issues':''}">
      <span class="ac ${a.count===0?'ok':''}">${a.count}</span>
      <span class="al">${esc(a.label)}</span>
    </div>`).join('');
  resultEl.innerHTML=`<div style="font-size:10px;color:var(--dim);padding:4px 0">
    ${d.total_issues} total issues</div>${rows}`;
}

async function runSQL(){
  const sql=document.getElementById('sql-input').value.trim();
  const db=document.getElementById('sql-db').value;
  const stat=document.getElementById('sql-status');
  const results=document.getElementById('sql-results');
  if(!sql){ stat.textContent='empty'; return; }
  stat.textContent='running…'; results.innerHTML='';
  const r=await fetch('/api/library/sql',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({sql,db})});
  const d=await r.json();
  if(d.error){ stat.textContent='error'; results.innerHTML=`<div style="padding:12px;color:var(--err);font-size:12px">${esc(d.error)}</div>`; return; }
  stat.textContent=`${d.count} rows`;
  if(!d.rows||!d.rows.length){ results.innerHTML='<div style="padding:12px;color:var(--dim);font-size:12px">no rows</div>'; return; }
  const cols=d.cols||Object.keys(d.rows[0]);
  results.innerHTML=`<table>
    <thead><tr>${cols.map(c=>`<th>${esc(c)}</th>`).join('')}</tr></thead>
    <tbody>${d.rows.map(row=>`<tr>${cols.map(c=>`<td title="${esc(String(row[c]||''))}">${esc(String(row[c]||''))}</td>`).join('')}</tr>`).join('')}</tbody>
  </table>`;
}

// ── tool log overlay ──────────────────────────────────────────────────────────
function openToolLog(title){
  document.getElementById('tool-log-title').textContent=title;
  document.getElementById('tool-log').innerHTML='';
  document.getElementById('tool-log-overlay').classList.remove('hidden');
}
function closeToolLog(e){ if(e.target.id==='tool-log-overlay') closeToolLogBtn(); }
function closeToolLogBtn(){
  document.getElementById('tool-log-overlay').classList.add('hidden');
  if(toolLogES){ toolLogES.close(); toolLogES=null; }
}
function appendToolLog(level,text){
  const el=document.getElementById('tool-log');
  const div=document.createElement('div');
  div.className='ll '+(level||'info');
  const ts=new Date().toTimeString().slice(0,8);
  div.innerHTML=`<span class="ts">${ts}</span><span class="lv">${level}</span><span class="tx">${esc(text)}</span>`;
  el.appendChild(div); el.scrollTop=el.scrollHeight;
}
function startToolStream(onDone){
  if(toolLogES) toolLogES.close();
  toolLogES=new EventSource('/api/job/stream');
  toolLogES.onmessage=e=>{
    const m=JSON.parse(e.data);
    if(m.type==='log') appendToolLog(m.level,m.text);
    else if(m.type==='done'){
      toolLogES.close(); toolLogES=null;
      document.getElementById('tool-log-title').textContent='Done';
      if(onDone) onDone();
    }
  };
  toolLogES.onerror=()=>{ if(toolLogES){toolLogES.close();toolLogES=null;} };
}

// ── fake-flac tab ────────────────────────────────────────────────────────────
let ffPage_=0, ffRows_=[], ffCurrent_=null;

async function checkVampAvailable(){
  const btn=document.getElementById('ff-vamp-btn');
  try{
    const r=await fetch('/api/fakeflac/vamp-available');
    const d=await r.json();
    btn.disabled=!d.available;
    btn.title=d.available
      ? 'run sonic-annotator + the Vamp CNN plugin on all suspects'
      : 'sonic-annotator not found on this machine';
  }catch(e){ btn.disabled=true; }
}

async function loadFakeflacSuspects(page){
  ffPage_=page;
  const db=document.getElementById('ff-db').value;
  const params=new URLSearchParams({db,page,per_page:50});
  const r=await fetch('/api/fakeflac/suspects?'+params);
  const d=await r.json();
  ffRows_=d.files||[];
  document.getElementById('ff-page-info').textContent=`page ${(d.page||0)+1} of ${d.pages||1}`;
  document.getElementById('ff-count').textContent=`${(d.total||0).toLocaleString()} suspects`;
  document.getElementById('ff-tbody').innerHTML=ffRows_.map((row,i)=>{
    const conf=row.transcode_confidence||0;
    const cls=conf>=0.6?'hi':conf>=0.3?'mid':'lo';
    const who=[row.artist||row.albumartist,row.album].filter(Boolean).join(' — ');
    return `<tr style="cursor:pointer">
      <td class="ff-tick" onclick="event.stopPropagation()"><input type="checkbox" class="ff-cb" data-i="${i}" onchange="ffSelCount()"></td>
      <td onclick="openFakeflacDetail(${i})">${esc(row.filename||'')}</td>
      <td onclick="openFakeflacDetail(${i})">${esc(who)}</td>
      <td onclick="openFakeflacDetail(${i})"><span class="ff-verdict v-${esc(row.transcode_verdict||'unknown')}">${esc(row.transcode_verdict||'—')}</span></td>
      <td onclick="openFakeflacDetail(${i})">${row.transcode_cutoff_hz?(row.transcode_cutoff_hz/1000).toFixed(1)+' kHz':'—'}</td>
      <td onclick="openFakeflacDetail(${i})">${row.transcode_wall_db?Math.round(row.transcode_wall_db)+' dB':'—'}</td>
      <td onclick="openFakeflacDetail(${i})">${row.transcode_above_db?Math.round(row.transcode_above_db)+' dB':'—'}</td>
      <td onclick="openFakeflacDetail(${i})"><span class="ff-conf ${cls}">${Math.round(conf*100)}%</span></td>
      <td onclick="openFakeflacDetail(${i})" title="${esc(row.transcode_notes||'')}">${esc(row.transcode_notes||'')}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="9" style="padding:20px;color:var(--dim)">no suspects — run a scan above</td></tr>';
  ffSelCount();
}
function ffPage(delta){ loadFakeflacSuspects(Math.max(0,ffPage_+delta)); }

// ── SPEK-TRO selection & bulk actions ───────────────────────────────────────
function ffSelected(){
  return Array.from(document.querySelectorAll('.ff-cb:checked'))
              .map(cb=>ffRows_[+cb.dataset.i]).filter(Boolean);
}
function ffSelCount(){
  const n=document.querySelectorAll('.ff-cb:checked').length,
        all=document.querySelectorAll('.ff-cb').length;
  document.getElementById('ff-sel-count').textContent=
    n?`${n} selected`:'0 selected';
  const master=document.getElementById('ff-all');
  if(master){ master.checked=n>0&&n===all; master.indeterminate=n>0&&n<all; }
}
function ffSelectAll(on){
  document.querySelectorAll('.ff-cb').forEach(cb=>{cb.checked=!!on;});
  ffSelCount();
}

async function ffBulk(action){
  const rows=ffSelected();
  if(!rows.length){ alert('Tick some rows first.'); return; }
  const verb={delete:'PERMANENTLY DELETE',isolate:'move to Suspected Transcodes',
              dismiss:'clear the suspected flag on'}[action];
  if(!confirm(`${verb} ${rows.length} file(s)?`+(action==='delete'?'\n\nThis cannot be undone.':''))) return;
  const db=document.getElementById('ff-db').value;
  const r=await fetch('/api/fakeflac/bulk',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action,db,paths:rows.map(x=>x.path)})});
  const j=await r.json();
  if(j.error){ alert(j.error); return; }
  if(j.failed&&j.failed.length){
    alert(`${j.done} done, ${j.failed.length} failed:\n`+
          j.failed.slice(0,8).map(f=>f.path.split('/').pop()+' — '+f.error).join('\n'));
  }
  loadFakeflacSuspects(ffPage_);
}

async function ffDeleteAll(){
  const db=document.getElementById('ff-db').value;
  const total=(document.getElementById('ff-count').textContent||'').trim();
  if(!confirm(`PERMANENTLY DELETE every suspect in ${db} (${total}) — not just this page.\n\n`
              +`This cannot be undone. Are you sure?`)) return;
  if(!confirm('Last check: this deletes the audio files themselves. Continue?')) return;
  let page=0, killed=0;
  for(;;){
    const r=await fetch('/api/fakeflac/suspects?'+new URLSearchParams({db,page:0,per_page:200}));
    const d=await r.json();
    const rows=d.files||[];
    if(!rows.length) break;
    const rr=await fetch('/api/fakeflac/bulk',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'delete',db,paths:rows.map(x=>x.path)})});
    const j=await rr.json();
    killed+=(j.done||0);
    if(!(j.done)) break;                 // nothing moved: stop rather than spin
    if(++page>200) break;
  }
  alert(`Deleted ${killed} file(s).`);
  loadFakeflacSuspects(0);
}

async function ffZipBlob(rows){
  const r=await fetch('/api/spectrogram/zip',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({paths:rows.map(x=>x.path)})});
  if(!r.ok){ const j=await r.json().catch(()=>({})); throw new Error(j.error||('HTTP '+r.status)); }
  return await r.blob();
}

async function ffZipSelected(){
  const rows=ffSelected();
  if(!rows.length){ alert('Tick some rows first.'); return; }
  const info=document.getElementById('ff-sel-count');
  info.textContent=`rendering ${rows.length}…`;
  try{
    const blob=await ffZipBlob(rows);
    const a=document.createElement('a');
    a.href=URL.createObjectURL(blob);
    a.download='spektro-'+new Date().toISOString().slice(0,10)+'.zip';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(()=>URL.revokeObjectURL(a.href),10000);
  }catch(e){ alert('Could not build the zip: '+e.message); }
  ffSelCount();
}

async function ffShareSelected(){
  const rows=ffSelected();
  if(!rows.length){ alert('Tick some rows first.'); return; }
  const info=document.getElementById('ff-sel-count');
  info.textContent=`rendering ${rows.length}…`;
  try{
    // One image shares as an image, which is what other apps want. Several go
    // as a zip. Where the browser has no file sharing, fall back to a download.
    let files;
    if(rows.length===1){
      const r=await fetch('/api/spectrogram?path='+encodeURIComponent(rows[0].path));
      const b=await r.blob();
      files=[new File([b],(rows[0].filename||'spectrogram')+'.png',{type:'image/png'})];
    }else{
      const b=await ffZipBlob(rows);
      files=[new File([b],'spektro.zip',{type:'application/zip'})];
    }
    if(navigator.canShare&&navigator.canShare({files})){
      await navigator.share({files,title:'Spectrograms',
        text:rows.length===1?rows[0].filename:`${rows.length} spectrograms`});
    }else{
      const a=document.createElement('a');
      a.href=URL.createObjectURL(files[0]); a.download=files[0].name;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(()=>URL.revokeObjectURL(a.href),10000);
      info.textContent='saved (this browser cannot share directly)';
      setTimeout(ffSelCount,3000); return;
    }
  }catch(e){ if(e && e.name!=='AbortError') alert('Share failed: '+e.message); }
  ffSelCount();
}

async function runFakeflacScan(){
  const db=document.getElementById('ff-db').value;
  const force=document.getElementById('ff-force').checked;
  const folder=document.getElementById('ff-folder-in').value.trim();
  if(db==='folder' && !folder){ alert('Click Browse and pick a folder first'); return; }
  openToolLog(db==='folder' ? `Checking ${folder}…` : 'Scanning for fake FLACs…');
  const r=await fetch('/api/job/fake_flac_scan',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({db,force,dest:folder})});
  const j=await r.json();
  if(j.error){ appendToolLog('broken',j.error); return; }
  startToolStream(()=>loadFakeflacSuspects(0));
}

async function nativePickFakeflacFolder(btn){
  const inp=document.getElementById('ff-folder-in');
  if(btn) btn.disabled=true;
  try{
    const r=await fetch('/api/pick-folder?start='+encodeURIComponent(inp.value||''));
    const d=await r.json();
    if(d.ok&&d.path){
      inp.value=d.path;
      document.getElementById('ff-db').value='folder';
      loadFakeflacSuspects(0);
    } else if(d.reason!=='cancelled'){
      alert('Cannot open the folder picker: '+(d.reason||'unknown reason'));
    }
  }catch(e){ alert('Cannot open the folder picker'); }
  finally{ if(btn) btn.disabled=false; }
}

async function runFakeflacVamp(){
  const db=document.getElementById('ff-db').value;
  openToolLog('Vamp-confirming suspects (slow)…');
  const r=await fetch('/api/job/fake_flac_vamp',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({db})});
  const j=await r.json();
  if(j.error){ appendToolLog('broken',j.error); return; }
  startToolStream(()=>loadFakeflacSuspects(ffPage_));
}

function openFakeflacDetail(i){
  const row=ffRows_[i];
  if(!row) return;
  ffCurrent_=row;
  const who=[row.artist||row.albumartist,row.album].filter(Boolean).join(' — ');
  const conf=Math.round((row.transcode_confidence||0)*100);
  const verdict=conf>=90?'almost certainly from a lossy source'
              :conf>=60?'looks like a lossy source'
              :conf>=30?'borderline — look at the picture'
              :'no lossy signature found';
  document.getElementById('ff-spec-name').textContent=row.filename||row.path;
  document.getElementById('ff-spec-sub').textContent=who||row.path;
  document.getElementById('ff-spec-nums').innerHTML=
    `<b>${verdict}</b> · cutoff `+
    (row.transcode_cutoff_hz?Math.round(row.transcode_cutoff_hz)+' Hz':'—')+
    ` · confidence ${conf}% · ${esc(row.transcode_notes||'')}`;
  const url='/api/spectrogram?path='+encodeURIComponent(row.path);
  const img=document.getElementById('ff-spec-img'),
        st=document.getElementById('ff-spec-status');
  img.hidden=true; st.hidden=false; st.textContent='Rendering spectrogram…';
  img.onload=()=>{ img.hidden=false; st.hidden=true; };
  img.onerror=()=>{ st.textContent='Could not render this one — is ffmpeg installed?'; };
  img.src=url;
  const saveLink=document.getElementById('ff-spec-save');
  saveLink.href=url;
  saveLink.download=(row.filename||'spectrogram')+'.png';
  const panel=document.getElementById('ff-spec');
  panel.hidden=false;
  panel.scrollIntoView({behavior:'smooth',block:'nearest'});
}
function ffSpecClose(){ document.getElementById('ff-spec').hidden=true; }
async function ffShareOne(){
  if(!ffCurrent_) return;
  try{
    const r=await fetch('/api/spectrogram?path='+encodeURIComponent(ffCurrent_.path));
    const b=await r.blob();
    const files=[new File([b],(ffCurrent_.filename||'spectrogram')+'.png',{type:'image/png'})];
    if(navigator.canShare&&navigator.canShare({files})){
      await navigator.share({files,title:ffCurrent_.filename||'Spectrogram'});
    }else{
      const a=document.createElement('a');
      a.href=URL.createObjectURL(files[0]); a.download=files[0].name;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(()=>URL.revokeObjectURL(a.href),10000);
    }
  }catch(e){ if(e&&e.name!=='AbortError') alert('Share failed: '+e.message); }
}
function closeSpec(e){ if(e.target.id==='spec-overlay') closeSpecBtn(); }
function closeSpecBtn(){ document.getElementById('spec-overlay').classList.add('hidden'); }

function copySpecLink(){
  const url=location.origin+document.getElementById('spec-img').getAttribute('src');
  const status=document.getElementById('spec-copy-status');
  navigator.clipboard.writeText(url)
    .then(()=>{ status.textContent='copied'; })
    .catch(()=>{ status.textContent=url; });
}

async function openFakeflacFolder(btn){
  if(!ffCurrent_) return;
  const label=btn?btn.textContent:'';
  if(btn){ btn.disabled=true; btn.textContent='…'; }
  try{
    const r=await fetch('/api/open-folder?path='+encodeURIComponent(ffCurrent_.path));
    const d=await r.json();
    if(!d.ok) alert('Cannot open folder: '+(d.reason||'failed'));
  }catch(e){ alert('Cannot open folder'); }
  finally{ if(btn){ btn.disabled=false; btn.textContent=label; } }
}

async function ffAction(kind){
  if(!ffCurrent_) return;
  if(kind==='delete' && !confirm(`Permanently delete ${ffCurrent_.filename}? This cannot be undone.`)) return;
  if(kind==='isolate' && !confirm(`Move ${ffCurrent_.filename} to the Suspected Transcodes folder?`)) return;
  const db=document.getElementById('ff-db').value;
  const r=await fetch('/api/fakeflac/'+kind,{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({path:ffCurrent_.path,db})});
  const j=await r.json();
  if(j.error){ alert(j.error); return; }
  closeSpecBtn();
  loadFakeflacSuspects(ffPage_);
}

// ── detail popup ──────────────────────────────────────────────────────────────
let detailRow_=null;
function showDetail(arr,idx,rowObj){
  const r=rowObj||(arr&&arr[idx]);
  if(!r) return;
  detailRow_=r;
  document.getElementById('detail-title').textContent=r.filename||r.path||'File detail';
  // Editable — the tags go into the FILE, not just the row. A database that
  // disagrees with the file on disk is how this library got into trouble.
  const EDIT=[
    ['Artist','artist'],['Album artist','albumartist'],['Album','album'],
    ['Title','title'],['Year','year'],['Label','label'],
    ['Cat#','catalog_number'],['Genre','genre'],
    ['Track no.','track_number'],['Disc no.','disc_number'],
  ];
  const READ=[['Status','status'],['Duration','dur'],['Size MB','mb'],['Path','path']];
  const cls=f=>f==='status'?(r[f]==='imported'?'ok':r[f]==='broken'?'err':r[f]==='duplicate'?'warn':''):'';
  document.getElementById('detail-body').innerHTML=`
    <div class="meta-grid">
      <span class="mk">Filename</span>
      <span class="mv"><input class="ed" id="ed-filename" value="${esc(r.filename||'')}"
        title="Rename the file on disk. The extension is kept if you leave it off."></span>
      ${EDIT.map(([k,f])=>`
        <span class="mk">${k}</span>
        <span class="mv"><input class="ed" data-field="${f}" value="${esc(String(r[f]==null?'':r[f]))}"></span>`).join('')}
      ${READ.map(([k,f])=>`
        <span class="mk">${k}</span>
        <span class="mv ${cls(f)}">${esc(String(r[f]||'—'))}</span>`).join('')}
    </div>
    <div class="detail-actions">
      <label class="dry-label" title="Untick to change only this app's database and leave the file's own tags alone"><input type="checkbox" id="ed-write" checked> write into the file</label>
      <span id="ed-msg" class="fb-note"></span>
      <span style="flex:1"></span>
      <button class="btn-xs ghost" onclick="closeDetailBtn()">Cancel</button>
      <button class="btn-xs" onclick="saveDetail()" title="Save the tags (and the name) to the file and the database">Save changes</button>
    </div>`;
  document.getElementById('detail-overlay').classList.remove('hidden');
}

async function saveDetail(){
  if(!detailRow_) return;
  const msg=document.getElementById('ed-msg');
  const fields={};
  document.querySelectorAll('#detail-body input.ed[data-field]').forEach(inp=>{
    const f=inp.dataset.field, was=detailRow_[f]==null?'':String(detailRow_[f]);
    if(inp.value!==was) fields[f]=inp.value;
  });
  const rename=document.getElementById('ed-filename').value.trim();
  const renamed=(rename && rename!==(detailRow_.filename||''))?rename:'';
  if(!Object.keys(fields).length && !renamed){ msg.textContent='nothing changed'; return; }
  msg.textContent='saving…';
  const r=await fetch('/api/session/edit',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({path:detailRow_.path,fields,rename:renamed,
      write_tags:document.getElementById('ed-write').checked,db:'session'})});
  const j=await r.json();
  msg.textContent=j.reason||(j.ok?'saved':'failed');
  if(j.ok){ setTimeout(()=>{ closeDetailBtn(); loadSession(); },700); }
}
function closeDetail(e){ if(e.target.id==='detail-overlay') closeDetailBtn(); }
function closeDetailBtn(){ document.getElementById('detail-overlay').classList.add('hidden'); }

init();

// ═══════════════════════════════════════════════════════════════════════════
// LABELS TAB — a record label's Discogs catalogue vs. the folders you hold.
//
// Every row is rendered with data-* attributes and handled by ONE delegated
// listener. Building onclick="lbThing('...')" strings is what broke the old
// sidebar tree: a folder called "DJ Konik - Don't You Want Me" ends the JS
// string early and the row silently does nothing.
// ═══════════════════════════════════════════════════════════════════════════
let lbView_='catalogue', lbStatusF_='', lbQ_='', lbRows_=[], lbSel_=new Set();
let lbES_=null, lbDebounce_=null, lbState_=null, lbInited_=false;

async function lbInit(){
  await lbLoadStatus();
  if(!lbInited_){
    lbInited_=true;
    document.querySelectorAll('#tab-labels .lb-view').forEach(b=>
      b.addEventListener('click',()=>{ 
        document.querySelectorAll('#tab-labels .lb-view').forEach(x=>x.classList.remove('active'));
        b.classList.add('active'); lbView_=b.dataset.view; lbSel_.clear(); lbLoad(); }));
    document.querySelectorAll('#tab-labels .lb-f').forEach(b=>
      b.addEventListener('click',()=>{
        document.querySelectorAll('#tab-labels .lb-f').forEach(x=>x.classList.remove('active'));
        b.classList.add('active'); lbStatusF_=b.dataset.status; lbLoad(); }));
    const q=document.getElementById('lb-q');
    q.addEventListener('input',()=>{ clearTimeout(lbDebounce_);
      lbDebounce_=setTimeout(()=>{ lbQ_=q.value.trim(); lbLoad(); },250); });
    document.getElementById('lb-rows').addEventListener('click',lbRowClick);
    document.getElementById('lb-roots').addEventListener('click',ev=>{
      const b=ev.target.closest('[data-rm]'); if(!b) return;
      lbRemoveRoot(b.dataset.rm);
    });
    document.getElementById('lb-overview').addEventListener('click',ev=>{
      const sw=ev.target.closest('[data-switch-label]');
      if(sw){ lbSwitchLabel(sw.dataset.switchLabel); return; }
      const un=ev.target.closest('[data-untrack-label]');
      if(un){ lbUntrackLabel(un.dataset.untrackLabel); return; }
    });
  }
  lbLoad();
}

async function lbLoadStatus(){
  let d; try{ d=await (await fetch('/api/label/status')).json(); }catch(e){ return; }
  if(d.ok===false){ lbLog('broken',d.reason||'label panel unavailable'); return; }
  lbState_=d;
  document.getElementById('lb-label-name').textContent=d.label_name||'—';
  document.getElementById('lb-label-id').textContent=d.label_id?('Discogs '+d.label_id):'';
  const c=d.catalogue||{};
  document.getElementById('lb-cat-status').innerHTML = c.releases
    ? (c.releases.toLocaleString()+' <span>releases</span> · '+
       c.tracklists.toLocaleString()+' <span>with track listings</span>')
    : (d.has_token? '<span>no catalogue yet — press Get catalogue</span>'
                  : '<span>no Discogs token — add one in the Pipeline tab</span>');
  document.getElementById('lb-archive-in').value=d.archive||'';
  document.getElementById('lb-roots').innerHTML=(d.roots||[]).map(r=>
    `<div class="lb-root ${r.exists?'':'gone'}">
       <span class="lb-role ${esc(r.role)}">${r.role==='owned'?'OWNED':'INCOMING'}</span>
       <span class="lb-rpath" data-i18n-skip title="${esc(r.path)}">${esc(r.path)}</span>
       ${r.exists?'':'<span class="lb-gone" title="This folder is not there right now — an unreadable root scans nothing and would otherwise report success">missing</span>'}
       <button class="lb-x" data-rm="${esc(r.path)}" title="Stop using this folder. Nothing on disk is touched.">×</button>
     </div>`).join('') ||
    '<div class="lb-none">No folders yet. Add at least one.</div>';
  const stale = d.scan && d.scan.stale;
  const sb = document.getElementById('lb-stale');
  document.getElementById('lb-stale-text').textContent = stale || '';
  sb.hidden = !stale;
  document.getElementById('lb-when').innerHTML = d.scan
    ? ('<span>last scan</span>: <span data-i18n-skip>'+
       esc(new Date(d.scan.when*1000).toLocaleString())+'</span>')
    : '<span>never scanned</span>';
  lbLoadOverview();
}

async function lbLoadOverview(){
  let d; try{ d=await (await fetch('/api/label/overview')).json(); }catch(e){ return; }
  const labels=d.labels||[];
  const el=document.getElementById('lb-overview');
  if(labels.length<2){ el.innerHTML=''; return; }   // nothing to overview with just one
  el.innerHTML=labels.map(o=>{
    const s=o.summary||{};
    const stats = o.summary
      ? `${(s.complete||0).toLocaleString()} <span>complete</span> · `+
        `${(s.missing||0).toLocaleString()} <span>missing</span>`
      : '<span class="lb-dim">never scanned</span>';
    return `<div class="lb-ov-item ${o.active?'active':''}">
      <button class="lb-ov-name" data-switch-label="${o.label_id}"
              ${o.active?'disabled':''} title="${o.active?'This is the active label':'Switch to this label'}"
              data-i18n-skip>${esc(o.label_name||String(o.label_id))}</button>
      <span class="lb-ov-stats" data-i18n-skip>${stats}</span>
      ${o.active?'':`<button class="lb-x" data-untrack-label="${o.label_id}" title="Stop tracking this label. Its cached catalogue and scan are left on disk, untouched.">×</button>`}
    </div>`;
  }).join('');
}

async function lbSwitchLabel(id){
  const labels=(await (await fetch('/api/label/overview')).json()).labels||[];
  const found=labels.find(o=>String(o.label_id)===String(id));
  await fetch('/api/label/set',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({what:'label',id:id,name:found?found.label_name:''})});
  lbLoadStatus(); lbLoad();
}

async function lbUntrackLabel(id){
  await fetch('/api/label/untrack',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:id})});
  lbLoadOverview();
}

function lbSummary(s){
  if(!s){ document.getElementById('lb-summary').innerHTML=''; return; }
  const cell=(n,label,cls,tip)=>
    `<div class="lb-stat ${cls||''}" title="${esc(tip||'')}">
       <b>${(n||0).toLocaleString()}</b><span>${esc(label)}</span></div>`;
  document.getElementById('lb-summary').innerHTML=
    cell(s.total,'in catalogue','', 'Every release Discogs files under this label')+
    cell(s.complete,'complete','ok','Releases you hold in full')+
    cell(s.genuine_complete,'genuine complete','ok',
        'Complete AND spectrally verified — not just track-count complete. The '+
        'gap between this and "complete" is how much still needs an authenticity check.')+
    cell(s.partial,'incomplete','warn','Releases you hold only part of')+
    cell(s.missing,'missing','err','Releases you do not hold at all')+
    cell(s.held_fake,'held but fake','err',
        'Held in full, but a confidently-lossy spectral verdict — re-hunt this one')+
    cell(s.held,'unverified','','You have a folder but no track listing to check it against')+
    cell(s.tracks_missing,'tracks outstanding','err','Individual tracks still to find, across every release')+
    cell(s.needs_convert,'to convert','warn','Releases whose best folder contains WAV/AIFF rather than FLAC')+
    (s.new||s.upgrade||s.duplicate
      ? cell(s.new,'new from incoming','ok','Incoming folders for releases you do not own')+
        cell(s.upgrade,'upgrades','ok','Incoming folders more complete than the one you own')+
        cell(s.duplicate,'duplicates','','Incoming folders that add nothing')
      : '');
}

async function lbLoad(){
  const wrap=document.getElementById('lb-rows'), empty=document.getElementById('lb-empty');
  const filters=document.getElementById('lb-filters');
  filters.style.visibility = (lbView_==='catalogue') ? 'visible':'hidden';
  wrap.innerHTML=''; empty.hidden=true;

  if(lbView_==='moves'){
    const d=await (await fetch('/api/label/moves')).json();
    if(!(d.rows||[]).length){ empty.textContent='Nothing has been moved yet.'; empty.hidden=false; return; }
    wrap.innerHTML=d.rows.map(r=>`<tr class="lb-tr">
      <td class="lb-c-when" data-i18n-skip>${esc(r.when)}</td>
      <td class="lb-c-act"><span class="pill ${r.ok?'ok':'err'}">${esc(r.action)}</span></td>
      <td class="lb-c-path" data-i18n-skip><div class="lb-from" title="${esc(r.src)}">${esc(r.src)}</div>
          <div class="lb-to" title="${esc(r.dest)}">→ ${esc(r.dest)}</div>
          ${r.note?`<div class="lb-note">${esc(r.note)}</div>`:''}</td>
      <td class="lb-c-do">${r.undoable
        ? `<button class="btn-xs" data-undo="${esc(r.dest)}" title="Move this folder back where it came from">Put back</button>`
        : ''}</td></tr>`).join('');
    return;
  }

  if(lbView_==='orphans'){
    const d=await (await fetch('/api/label/orphans')).json();
    if(d.ok===false){ empty.textContent=d.reason||'nothing scanned yet'; empty.hidden=false; return; }
    if(!(d.rows||[]).length){ empty.textContent='Every folder matched a release in this catalogue.'; empty.hidden=false; return; }
    document.getElementById('lb-count').innerHTML='<span>showing</span> '+
      d.total.toLocaleString();
    wrap.innerHTML=d.rows.map(r=>`<tr class="lb-tr">
      <td class="lb-c-sel"><input type="checkbox" data-pick="${esc(r.path)}"></td>
      <td class="lb-c-cat" data-i18n-skip>${esc(r.catno||'—')}</td>
      <td class="lb-c-title" data-i18n-skip><div class="lb-t">${esc(r.name)}</div>
          <div class="lb-sub" title="${esc(r.path)}">${esc(r.root)}</div></td>
      <td class="lb-c-st"><span class="pill">${r.lossless} <span>lossless</span>${r.lossy?(' · '+r.lossy+' <span>lossy</span>'):''}</span></td>
      <td class="lb-c-do"><button class="btn-xs ghost" data-open="${esc(r.path)}" title="Open this folder in the file manager">↗</button></td>
    </tr>`).join('');
    return;
  }

  if(lbView_==='series'){
    const d=await (await fetch('/api/label/series')).json();
    if(d.ok===false){ empty.textContent=d.reason||'nothing scanned yet'; empty.hidden=false; return; }
    if(!(d.rows||[]).length){ empty.textContent='No numbered series detected yet.'; empty.hidden=false; return; }
    wrap.innerHTML=d.rows.map(g=>`<tr class="lb-tr">
      <td class="lb-c-cat" data-i18n-skip></td>
      <td class="lb-c-title" data-i18n-skip><div class="lb-t">${esc(g.series)}</div></td>
      <td class="lb-c-yr" data-i18n-skip></td>
      <td class="lb-c-st"><span class="pill ${g.missing.length?'partial':'complete'}">
        ${g.have.length} <span>of</span> ${g.total}</span></td>
      <td class="lb-c-cov" data-i18n-skip></td>
      <td class="lb-c-do">${g.missing.length
        ? `<span class="flag err" title="Volume numbers with no folder at all">missing: ${g.missing.join(', ')}</span>`
        : ''}</td>
    </tr>`).join('');
    return;
  }

  if(lbView_==='other-labels'){
    const d=await (await fetch('/api/label/cross-label?role=incoming')).json();
    if(!(d.rows||[]).length){
      empty.textContent='No incoming folder matches a different tracked label right now.';
      empty.hidden=false; return;
    }
    wrap.innerHTML=d.rows.map(r=>`<tr class="lb-tr">
      <td class="lb-c-cat" data-i18n-skip>${esc(r.catno||'—')}</td>
      <td class="lb-c-title" data-i18n-skip><div class="lb-t">${esc(r.title)}</div>
        <div class="lb-sub" title="${esc(r.path)}">${esc(r.name)}</div></td>
      <td class="lb-c-yr" data-i18n-skip></td>
      <td class="lb-c-st"><span class="pill upgrade" data-i18n-skip title="Matched by ${esc(r.matched_by)}">${esc(r.label_name)}</span></td>
      <td class="lb-c-cov" data-i18n-skip></td>
      <td class="lb-c-do"><button class="btn-xs ghost" data-open="${esc(r.path)}" title="Open this folder in the file manager">↗</button></td>
    </tr>`).join('');
    return;
  }

  const qs=new URLSearchParams();
  if(lbView_==='incoming') qs.set('verdict','new,upgrade,duplicate');
  else if(lbStatusF_) qs.set('status',lbStatusF_);
  if(lbQ_) qs.set('q',lbQ_);
  qs.set('limit','500');
  const d=await (await fetch('/api/label/rows?'+qs)).json();
  if(d.ok===false){ empty.textContent=d.reason||'nothing scanned yet'; empty.hidden=false;
                    lbSummary(null); document.getElementById('lb-count').textContent=''; return; }
  lbSummary(d.summary); lbRows_=d.rows||[];
  document.getElementById('lb-count').innerHTML =
    '<span>showing</span> '+d.rows.length.toLocaleString()+
    ' / '+d.total.toLocaleString();
  if(!lbRows_.length){ empty.textContent='Nothing matches that filter.'; empty.hidden=false; return; }

  wrap.innerHTML=lbRows_.map((r,i)=>{
    const pill = r.verdict
      ? `<span class="pill ${r.verdict}" title="${esc(lbVerdictTip(r.verdict))}">${esc(r.verdict)}</span>`
      : `<span class="pill ${esc(r.status)}" title="${esc(lbStatusTip(r.status))}">${esc(lbStatusWord(r.status))}</span>`;
    const cov = r.known_tracklist
      ? `${r.have}/${r.expected}`
      : `<span class="lb-dim" title="No Discogs track listing cached for this release, so completeness cannot be checked. Press Get tracklists.">?</span>`;
    const flags=[];
    if(r.needs_convert) flags.push(`<span class="flag warn" title="${r.needs_convert} file(s) here are WAV/AIFF. They count as lossless and close the gap, but convert them before you call the release finished.">WAV</span>`);
    if(r.lossy_only) flags.push('<span class="flag err" title="The only audio in that folder is lossy. It does not count as owning the release.">lossy</span>');
    const cc=r.crosscheck;
    let ccBadge='';
    if(cc){
      const problems=[];
      if(cc.naming && !cc.naming.matches) problems.push('naming');
      if(cc.tags && (cc.tags.missing_artist.length || cc.tags.missing_tracknumber.length ||
                    cc.tags.contradicts_artist.length || cc.tags.contradicts_catno.length))
        problems.push('tags');
      if(cc.artwork && !cc.artwork.present) problems.push('art');
      if(problems.length) ccBadge=`<button class="btn-xs ghost warn" data-cc="${i}" `+
        `title="Discogs cross-check found something to look at: ${esc(problems.join(', '))}">check ▾</button>`;
    }
    return `<tr class="lb-tr" data-i="${i}">
      <td class="lb-c-sel"><input type="checkbox" data-pick="${esc(r.incoming_folder||r.folder||'')}"
        title="${(r.incoming_folder||r.folder)?'':'No folder for this one yet — tick it to search Soulseek for it'}"></td>
      <td class="lb-c-cat" data-i18n-skip>${esc(r.catno||'—')}</td>
      <td class="lb-c-title" data-i18n-skip><div class="lb-t">${esc(r.artist? r.artist+' — '+r.title : r.title)}</div>
        ${r.folder_name?`<div class="lb-sub" title="${esc(r.folder)}">${esc(r.folder_name)}</div>`:''}</td>
      <td class="lb-c-yr" data-i18n-skip>${esc(String(r.year||''))}</td>
      <td class="lb-c-st">${pill} ${flags.join(' ')}</td>
      <td class="lb-c-cov">${cov}</td>
      <td class="lb-c-do">
        ${r.missing_tracks.length?`<button class="btn-xs ghost" data-more="${i}" title="Show the tracks still missing from this release">${r.missing_tracks.length} <span>missing</span> ▾</button>`:''}
        ${ccBadge}
        ${r.incoming_folder && r.verdict?`<button class="btn-xs ghost" data-tag-incoming="${esc(r.incoming_folder)}" `+
          `title="This folder's files have no tags at all — write the catalogue number, artist and album `+
          `this row matched it to into every file (only_missing, so anything already tagged is left alone). `+
          `Per-track title/track-number too, wherever a track listing lets it match a specific file.">Tag files</button>`:''}
        ${r.incoming_folder && r.verdict?`<button class="btn-xs ghost" data-rename-incoming="${esc(r.incoming_folder)}" `+
          `title="Rename this folder in place to this archive's own convention — (catno) Title (Year) — `+
          `so it reads like the rest of the collection. Refuses if a folder already exists under that name. `+
          `Run this before Move to archive, not instead of it.">Rename folder</button>`:''}
        ${r.folder?`<button class="btn-xs ghost" data-open="${esc(r.folder)}" title="Open the folder you hold in the file manager">↗</button>`:''}
      </td></tr>`;
  }).join('');
}

function lbStatusWord(s){
  return {complete:'complete',partial:'incomplete',missing:'missing',held:'unverified',
          held_fake:'held — fake'}[s]||s;
}
function lbStatusTip(s){
  return {complete:'Every track Discogs lists for this release is in your folder',
          partial:'Your folder is missing some of the tracks Discogs lists',
          missing:'You do not hold this release at all',
          held:'You have a folder but no track listing to check it against',
          held_fake:'A confidently-lossy spectral verdict on a release you otherwise '+
                    'hold in full — see the authenticity notes'}[s]||'';
}
function lbVerdictTip(v){
  return {new:'An incoming folder for a release you do not own — this is a gain',
          upgrade:'The incoming folder holds more of this release than the one you own',
          duplicate:'The incoming folder adds nothing you do not already have'}[v]||'';
}

function lbRowClick(ev){
  const more=ev.target.closest('[data-more]');
  if(more){
    const tr=more.closest('tr'), r=lbRows_[+more.dataset.more];
    const nx=tr.nextElementSibling;
    if(nx && nx.classList.contains('lb-more')){ nx.remove(); return; }
    const row=document.createElement('tr');
    row.className='lb-more';
    row.innerHTML=`<td colspan="7"><div class="lb-missing" data-i18n-skip>
      <b>Still missing from (${esc(r.catno)}) ${esc(r.title)}</b>
      <ol>${r.missing_tracks.map(t=>'<li>'+esc(t)+'</li>').join('')}</ol></div></td>`;
    tr.after(row); return;
  }
  const ccmore=ev.target.closest('[data-cc]');
  if(ccmore){
    const tr=ccmore.closest('tr'), r=lbRows_[+ccmore.dataset.cc];
    const nx=tr.nextElementSibling;
    if(nx && nx.classList.contains('lb-more-cc')){ nx.remove(); return; }
    const cc=r.crosscheck||{};
    const lines=[];
    if(cc.naming && !cc.naming.matches)
      lines.push(`<div><b>Naming</b> — expected <code>${esc(cc.naming.expected)}</code>, `+
                 `folder is <code>${esc(cc.naming.actual||'(no folder)')}</code></div>`);
    if(cc.tags){
      const t=cc.tags;
      if(t.missing_artist.length) lines.push('<div><b>Missing artist tag</b> on: '+
        t.missing_artist.map(esc).join(', ')+'</div>');
      if(t.missing_tracknumber.length) lines.push('<div><b>Missing track number</b> on: '+
        t.missing_tracknumber.map(esc).join(', ')+'</div>');
      if(t.contradicts_artist.length) lines.push('<div><b>Artist tag disagrees with Discogs</b> on: '+
        t.contradicts_artist.map(x=>esc(x.track)+' ('+esc(x.tag)+' vs '+esc(x.catalogue)+')').join(', ')+'</div>');
      if(t.contradicts_catno.length) lines.push('<div><b>Catalogue-number tag disagrees</b> on: '+
        t.contradicts_catno.map(x=>esc(x.track)+' ('+esc(x.tag)+' vs '+esc(x.catalogue)+')').join(', ')+'</div>');
      if((t.missing_artist.length||t.missing_tracknumber.length) && r.folder)
        lines.push('<div><button class="btn-xs" data-fix-tags="'+esc(r.folder)+'" '+
          'title="Fill in ONLY the missing artist/track-number tags shown above, from this '+
          'catalogue entry. Never touches a tag that already has a value, right or wrong.">'+
          'Fix tags</button></div>');
    }
    if(cc.artwork){
      lines.push('<div><b>Artwork</b> — '+
        (cc.artwork.present?('present, '+esc(cc.artwork.source)):'none found')+'</div>');
      if(!cc.artwork.present && r.folder)
        lines.push('<div><button class="btn-xs" data-get-artwork="'+esc(r.folder)+'" '+
          'title="Save a loose folder.jpg next to the tracks — extracted from another file '+
          'here that already has one, or fetched from Discogs if nothing here does. Never '+
          'embeds into or otherwise touches the audio files.">Get artwork</button></div>');
      if((cc.artwork.deferred||[]).length)
        lines.push('<div class="lb-dim">Not checked yet: '+
          cc.artwork.deferred.map(esc).join(' · ')+'</div>');
    }
    const row=document.createElement('tr');
    row.className='lb-more-cc';
    row.innerHTML=`<td colspan="7"><div class="lb-missing" data-i18n-skip>
      <b>Discogs cross-check — (${esc(r.catno)}) ${esc(r.title)}</b>
      ${lines.join('')}</div></td>`;
    tr.after(row); return;
  }
  const op=ev.target.closest('[data-open]');
  if(op){ fetch('/api/open-folder?path='+encodeURIComponent(op.dataset.open)); return; }
  const ft=ev.target.closest('[data-fix-tags]');
  if(ft){ lbFixTags(ft.dataset.fixTags); return; }
  const ga=ev.target.closest('[data-get-artwork]');
  if(ga){ lbGetArtwork(ga.dataset.getArtwork); return; }
  const ti=ev.target.closest('[data-tag-incoming]');
  if(ti){ lbTagIncoming(ti.dataset.tagIncoming); return; }
  const ri=ev.target.closest('[data-rename-incoming]');
  if(ri){ lbRenameIncoming(ri.dataset.renameIncoming); return; }
  const un=ev.target.closest('[data-undo]');
  if(un){ lbUndo(un.dataset.undo); return; }
  const cb=ev.target.closest('input[data-pick]');
  if(cb){ if(cb.checked) lbSel_.add(cb.dataset.pick); else lbSel_.delete(cb.dataset.pick); }
}

function lbSelectAll(on){
  document.querySelectorAll('#lb-rows input[data-pick]:not([disabled])').forEach(cb=>{
    cb.checked=on; if(on) lbSel_.add(cb.dataset.pick); else lbSel_.delete(cb.dataset.pick);
  });
}

async function lbAddRoot(role){
  const path=await pickFolderModal(role==='owned'
    ? 'Choose a folder you ALREADY OWN of this label'
    : 'Choose an INCOMING folder to judge against what you own');
  if(!path) return;
  // Adding a folder is a full onboarding pass now (index -> match ->
  // completeness -> authenticity -> cross-check for what's new under this
  // root), not just recording a path — so it runs as a job like Scan does,
  // with the same log/progress stream. A refusal (not a folder, etc.) comes
  // back as a warning line in the job log instead of a synchronous alert.
  lbJob('label_onboard',{dest:path,role});
}
async function lbRemoveRoot(path){
  if(!confirm('Stop using this folder?\n\n'+path+'\n\nNothing on disk is touched.')) return;
  await fetch('/api/label/set',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({what:'remove_root',path})});
  lbLoadStatus();
}
async function lbPickArchive(){
  const path=await pickFolderModal('Choose the ARCHIVE folder that Move files releases into');
  if(!path) return;
  const r=await (await fetch('/api/label/set',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({what:'archive',path})})).json();
  if(!r.ok) alert(r.reason||'could not set that folder');
  lbLoadStatus();
}

async function lbChangeLabel(){
  const q=prompt('Search Discogs for a record label by name:','');
  if(!q) return;
  const d=await (await fetch('/api/label/search?q='+encodeURIComponent(q))).json();
  if(!d.ok){ alert(d.reason||'search failed'); return; }
  if(!(d.results||[]).length){ alert('No label called that on Discogs.'); return; }
  const pick=prompt('Which one?\n\n'+d.results.map((r,i)=>
    (i+1)+'. '+r.title+'  (id '+r.id+')').join('\n')+'\n\nType a number:','1');
  const n=parseInt(pick,10);
  if(!n || n<1 || n>d.results.length) return;
  const chosen=d.results[n-1];
  await fetch('/api/label/set',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({what:'label',id:chosen.id,name:chosen.title})});
  lbLoadStatus();
  lbLog('info','label is now '+chosen.title+' — press Get catalogue');
}

function lbLog(level,text){
  const el=document.getElementById('lb-log'); if(!el) return;
  const d=document.createElement('div');
  d.className='l '+(level||'info'); d.textContent=text;
  el.appendChild(d); el.scrollTop=el.scrollHeight;
}

async function lbJob(kind,body){
  const btn=document.getElementById('lb-scan-btn');
  const r=await fetch('/api/job/'+kind,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{dest:''})});
  const j=await r.json();
  if(j.error){ lbLog('broken',j.error); return; }
  if(btn) btn.disabled=true;
  lbLog('info','started: '+kind);
  if(lbES_) lbES_.close();
  lbES_=new EventSource('/api/job/stream');
  lbES_.onmessage=e=>{
    const m=JSON.parse(e.data);
    if(m.type==='log') lbLog(m.level,m.text);
    else if(m.type==='done'){
      if(btn) btn.disabled=false;
      lbES_.close(); lbES_=null;
      lbLoadStatus(); lbLoad();
    }
  };
  lbES_.onerror=()=>{ if(btn) btn.disabled=false;
                      if(lbES_){lbES_.close();lbES_=null;} };
}

function lbExport(kind){
  // A plain navigation, so the browser saves the file wherever it saves things.
  window.location='/api/label/export?kind='+encodeURIComponent(kind);
}

async function lbMove(dry){
  const paths=[...lbSel_].filter(Boolean);
  if(!paths.length){ alert('Tick the rows you want to move first.'); return; }
  if(!lbState_ || !lbState_.archive){
    alert('Choose the Archive folder on the left first — that is where Move puts things.');
    return;
  }
  // Say which folders these are before moving them. In the Catalogue view the
  // tick box selects the folder you ALREADY OWN, so a careless "select all"
  // here would relocate the archive itself rather than import anything. The
  // counts make that impossible to do without seeing it.
  const owned=[], incoming=[];
  lbRows_.forEach(r=>{
    if(r.incoming_folder && paths.includes(r.incoming_folder)) incoming.push(r.incoming_folder);
    else if(r.folder && paths.includes(r.folder)) owned.push(r.folder);
  });
  if(!dry){
    let msg='MOVE '+paths.length+' folder(s) into:\n\n'+lbState_.archive+'\n\n';
    if(owned.length) msg+='\u26a0 '+owned.length+' of these are folders you ALREADY OWN — '+
                          'moving them relocates your library, it does not import anything.\n';
    if(incoming.length) msg+=incoming.length+' are incoming folders.\n';
    msg+='\nFirst few:\n'+paths.slice(0,5).map(p=>'  '+p).join('\n')+
         (paths.length>5?('\n  … and '+(paths.length-5)+' more'):'')+
         '\n\nEvery move is logged and can be put back.';
    if(!confirm(msg)) return;
  }
  const r=await (await fetch('/api/label/move',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({paths,dry_run:!!dry})})).json();
  if(r.ok===false && r.reason){ alert(r.reason); return; }
  // Per folder, never one ok/failed: on a move of forty you need to know which
  // three failed and why.
  (r.results||[]).forEach(x=>lbLog(x.ok?'info':'broken',
    (dry?'would move: ':'')+x.name+' — '+x.reason));
  lbLog('info',(dry?'preview: ':'moved: ')+
    (r.results||[]).filter(x=>x.ok).length+' of '+paths.length);
  if(!dry){ lbSel_.clear(); lbLoad(); }
}

// Nicotine+'s own local REST API — the "api-nicotine-plus" plugin, already
// installed and pointed at this label (see label2lossless). Host/port are
// this plugin's own preferences (127.0.0.1:12339 on this box); it sets
// Access-Control-Allow-Origin: * specifically so pages like this one can
// call it directly. Ticked rows are read from the checkboxes' own <tr
// data-i>, not lbSel_/data-pick — Move needs a real folder path to tick a
// row, a Soulseek search does not, so a MISSING release (no folder at all)
// must still be tickable here even though it never can be for Move.
const SLSK_API = 'http://127.0.0.1:12339';
const _SLSK_VARIOUS = new Set(['', 'various', 'various artists', 'va', 'v/a',
                               'verschiedene', 'compilation']);

async function lbSearchSlsk(){
  const trs=[...document.querySelectorAll('#lb-rows input[data-pick]:checked')]
    .map(cb=>cb.closest('tr[data-i]')).filter(Boolean);
  if(!trs.length){ alert('Tick the rows you want to search for first.'); return; }
  const rows=trs.map(tr=>lbRows_[+tr.dataset.i]).filter(Boolean);
  for(const r of rows){
    const a=(r.artist||'').trim();
    const query=(_SLSK_VARIOUS.has(a.toLowerCase().replace(/\.$/,''))
      ? r.title : `${a} ${r.title}`).trim();
    if(!query) continue;
    try{
      const resp=await fetch(SLSK_API+'/search',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({query, mode:'global', switch_page:true})});
      const d=await resp.json().catch(()=>({}));
      if(resp.ok && d.ok!==false) lbLog('info','SLSK search: '+query);
      else lbLog('warning','SLSK search failed: '+query+' — '+(d.error||resp.status));
    }catch(e){
      lbLog('broken','Could not reach Nicotine+ on '+SLSK_API+
        ' — is it running, with the api-nicotine-plus plugin enabled?');
      return;
    }
  }
}

async function lbUndo(dest){
  if(!confirm('Put this folder back where it came from?\n\n'+dest)) return;
  const r=await (await fetch('/api/label/undo',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({dest})})).json();
  lbLog(r.ok?'info':'broken', r.reason||'');
  lbLoad();
}

// Opt-in, one folder at a time — same as every other write this tab does.
// No dry-run prompt here: write_tags_to_file's only_missing=True already
// makes this a strictly additive fill-in-the-gaps action, never an overwrite.
async function lbFixTags(folder){
  const r=await (await fetch('/api/label/fix-tags',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({paths:[folder]})})).json();
  const row=(r.results||[])[0];
  lbLog(row && row.ok ? 'info' : 'broken', (row && row.reason) || r.reason || '');
  lbLoad();
}

// Same opt-in, one-folder-at-a-time shape as lbFixTags. Only ever ADDS a
// folder.jpg — never touches an audio file, never overwrites artwork that
// is already there (get_artwork refuses before it gets this far).
async function lbGetArtwork(folder){
  const r=await (await fetch('/api/label/get-artwork',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({paths:[folder]})})).json();
  const row=(r.results||[])[0];
  lbLog(row && row.ok ? 'info' : 'broken', (row && row.reason) || r.reason || '');
  lbLoad();
}

// Incoming-only: this folder's files carry no tags at all yet, and the row
// it is showing under IS the catalogue match — write that match's own
// catalog_number/artist/album into every file, only_missing so a file that
// somehow already has a value is left alone.
async function lbTagIncoming(folder){
  const r=await (await fetch('/api/label/tag-incoming',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({paths:[folder]})})).json();
  const row=(r.results||[])[0];
  lbLog(row && row.ok ? 'info' : 'broken', (row && row.reason) || r.reason || '');
  lbLoad();
}

// Renames the folder in place to the archive's own "(catno) Title (Year)"
// convention. A rename only — Move to archive stays a separate step.
async function lbRenameIncoming(folder){
  const r=await (await fetch('/api/label/rename-incoming',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({paths:[folder]})})).json();
  const row=(r.results||[])[0];
  lbLog(row && row.ok ? 'info' : 'broken', (row && row.reason) || r.reason || '');
  lbLoad();
}
