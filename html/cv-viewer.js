/* Hiring-manager viewer logic (ADR-009, D-013, P-010). External file, not an
   inline <script>, so the public response's CSP can hold `script-src 'self'`
   with no `'unsafe-inline'` exception (backend/cv_publish/routes.py).

   The capability token lives only in the URL *fragment*
   (`/cv/view#<token>`), which a browser never sends to any server — not in
   the request line, not in a `Referer` header. It is read once from
   `location.hash` and sent to the four capability routes in the
   `X-CV-Publication-Token` header, never in a path or query string. */

/* Finished document, not an editing surface: no dashed page-break markers. */
SHOW_PAGE_MARKERS = false;

var TOKEN_HEADER = 'X-CV-Publication-Token';

var TOKEN = (function(){
  // '#abc' or '#pub=abc' — accept a bare fragment or a key=value one so a
  // host page embedding this link some other way still has a stable format.
  var raw = location.hash.replace(/^#/, '');
  if(!raw) return '';
  var eq = raw.indexOf('=');
  return decodeURIComponent(eq === -1 ? raw : raw.slice(eq + 1));
})();

function showMessage(title, body){
  var msg = document.getElementById('viewerMsg');
  document.getElementById('viewerSpinner').classList.add('hidden');
  msg.innerHTML = '<h1></h1><p></p>';
  msg.querySelector('h1').textContent = title;
  msg.querySelector('p').textContent = body;
  msg.classList.remove('hidden');
}

/* Push the snapshot into the hidden inputs + shared globals, then render(). The
   keys mirror the editor's captureFormState() output exactly. */
function applyState(s){
  s = s || {};
  var fields = ['iName','iRole','iEmail','iPhone','iAvail','iSummary',
    'iFtText','iFtEmail','iFtPhone',
    'pfFont','pfSize','pfColor','pfWeight','skFont','skSize','skColor','skWeight',
    'exFont','exSize','exColor','exWeight','edFont','edSize','edColor','edWeight'];
  fields.forEach(function(id){
    var el = document.getElementById(id);
    if(el && s[id] != null) el.value = String(s[id]);
  });
  // Shared globals from cv-render.js (skills is const → mutate in place).
  skills.main  = (s.skills && s.skills.main)  ? s.skills.main.slice()  : [];
  skills.other = (s.skills && s.skills.other) ? s.skills.other.slice() : [];
  skills.tool  = (s.skills && s.skills.tool)  ? s.skills.tool.slice()  : [];
  expList    = (s.expList    || []).map(function(e){ return {
      company:e.company||'', location:e.location||'', from:e.from||'', to:e.to||'',
      assignments:(e.assignments||[]).map(function(a){ return {
        role:a.role||'', period:a.period||'', descHtml:a.descHtml||'', tools:a.tools||'' }; })
    }; });
  langList   = (s.langList   || []).map(function(l){ return { name:l.name||'', level:l.level||'' }; });
  courseList = (s.courseList || []).map(function(c){ return { name:c.name||'' }; });
  eduList    = (s.eduList    || []).map(function(e){ return {
      degree:e.degree||'', institution:e.institution||'', from:e.from||'', to:e.to||'', description:e.description||'' }; });
  photoSrc   = s.photoSrc || null;
  render();
}

/* Scale the 720px design down to fit narrow viewports (phones open link from
   email). Screen only — print resets the transform via @media print. */
function fitSheet(){
  var sheet  = document.getElementById('cvSheet');
  var scaler = document.getElementById('scaler');
  var stage  = document.querySelector('.viewer-stage');
  if(scaler.classList.contains('hidden')) return;
  var avail = stage.clientWidth - 32;               // stage horizontal padding
  var scale = Math.min(1, avail / 720);
  sheet.style.transform = scale < 1 ? ('scale(' + scale + ')') : 'none';
  scaler.style.width  = (720 * scale) + 'px';       // collapse the layout box…
  scaler.style.height = (sheet.offsetHeight * scale) + 'px';  // …to scaled dims
}
window.addEventListener('resize', fitSheet);

/* Download PDF — bound here rather than through an `onclick="window.print()"`
   attribute in the shell. The public response's CSP holds `script-src 'self'`
   with no `'unsafe-inline'` exception, so a browser refuses an inline handler:
   the button rendered and did nothing (`S5-P2-R1`). This file is already
   `'self'`, so the same action works under the strict policy unchanged. The
   shell must carry no `on*=` attribute at all — `tests/test_static_pages.py`
   pins that class, not just this one instance. */
function armDownload(){
  var btn = document.getElementById('downloadBtn');
  if(btn) btn.addEventListener('click', function(){ window.print(); });
}

function reveal(){
  document.getElementById('viewerMsg').classList.add('hidden');
  document.getElementById('viewerHeader').classList.remove('hidden');
  document.getElementById('scaler').classList.remove('hidden');
  fitSheet();
  if(document.fonts && document.fonts.ready){ document.fonts.ready.then(fitSheet); }
  setTimeout(fitSheet, 300);   // late webfont metrics
}

function _authHeaders(){
  var h = {}; h[TOKEN_HEADER] = TOKEN; return h;
}

/* Human-interaction beacon — fired ONCE, not on load (prefetch-bot defence).
   The server no-ops it for an authorized owner session; anonymous opens count. */
var beaconSent = false;
function fireBeacon(){
  if(beaconSent) return; beaconSent = true;
  fetch('/api/publications/opened', {
    method:'POST', credentials:'same-origin', headers:_authHeaders()
  }).catch(function(){});
}
function armBeacon(){
  ['pointerdown','keydown','scroll','click','touchstart'].forEach(function(ev){
    window.addEventListener(ev, fireBeacon, {once:true, passive:true});
  });
}

async function boot(){
  if(!TOKEN){ showMessage('Link not found', 'This CV link is missing or malformed.'); return; }
  var meta;
  try{
    var mr = await fetch('/api/publications/meta', {credentials:'same-origin', headers:_authHeaders()});
    if(mr.status === 404){ showMessage('Link not found', 'This link is not valid. Please check it and try again.'); return; }
    if(mr.status === 410){ showMessage('This CV is no longer available', 'Please contact the person who sent you the link.'); return; }
    if(!mr.ok){ showMessage('Something went wrong', 'This CV could not be loaded right now. Please try again later.'); return; }
    meta = await mr.json();
  }catch(e){ showMessage('Something went wrong', 'This CV could not be loaded right now. Please try again later.'); return; }

  var content;
  try{
    var cr = await fetch('/api/publications/content', {credentials:'same-origin', headers:_authHeaders()});
    if(cr.status === 410){ showMessage('This CV is no longer available', 'Please contact the person who sent you the link.'); return; }
    if(!cr.ok){ showMessage('Something went wrong', 'This CV could not be loaded right now. Please try again later.'); return; }
    content = await cr.json();
  }catch(e){ showMessage('Something went wrong', 'This CV could not be loaded right now. Please try again later.'); return; }

  var state = (content && content.cv_state) || {};
  // A publication's photo is served from the same header-authorized route as
  // meta/content — never a path/query token — so rewrite whatever the server
  // stored (a bare capability path with no identifier of its own) into a URL
  // this page can actually fetch with the auth header attached.
  if(state.photoSrc === '/api/publications/photo'){
    try{
      var pr = await fetch('/api/publications/photo', {credentials:'same-origin', headers:_authHeaders()});
      state = Object.assign({}, state, { photoSrc: pr.ok ? URL.createObjectURL(await pr.blob()) : null });
    }catch(e){ state = Object.assign({}, state, { photoSrc: null }); }
  }
  applyState(state);

  // Header: the CV's own name + role; publication title feeds the browser tab.
  document.getElementById('vhName').textContent = state.iName || (meta && meta.consultant_username) || '';
  document.getElementById('vhRole').textContent = state.iRole || '';
  document.title = (state.iName ? state.iName + ' — ' : '') + 'CV';

  if(meta && meta.can_edit){
    var edit = document.getElementById('editBtn');
    // The editor now opens straight into that consultant's Application/base
    // CV via consultant_id; there is no separate publication-edit mode to
    // route into (a publication is a read-only, server-derived snapshot of
    // the tailored CV — edit the tailored CV and Publish again to update it).
    edit.href = '/?consultant_id=' + encodeURIComponent(meta.consultant_username || '');
    edit.classList.remove('hidden');
  }

  reveal();
  armBeacon();
}

// This file is loaded at the end of the shell's <body>, so the header's
// controls already exist; bind before the first await in boot().
armDownload();
boot();
