'use strict';

// ── State ─────────────────────────────────────────────────────────────────
let ws = null;
let muted = false;
let _ttsQueue = [];
let _ttsPlaying = false;
let _currentTTSSrc = null;
let briefingBuffer = '';
let userHasInteracted = false;
let pendingGreeting = null;
const serviceDataStore = {};
const serviceHealthStore = {};
let accountNames = { gmail1: 'Gmail 1', gmail2: 'Gmail 2', outlook: 'Outlook' };
let actionItemsStore = [];
let currentModalItem = null;
let _orbTick, _orbMid, _orbSphere;
let _reconnectDelay = 2000;
let _reconnectTimer = null;

// ── DOM refs ───────────────────────────────────────────────────────────────
const clock        = document.getElementById('clock');
const dateDisplay  = document.getElementById('date-display');
const statusText   = document.getElementById('status-text');
const briefingOutput = document.getElementById('briefing-output');
const btnBrief     = document.getElementById('btn-brief');
const btnMute      = document.getElementById('btn-mute');
const connIndicator = document.getElementById('conn-indicator');
const panelMain    = document.getElementById('panel-main');

// ── Clock ──────────────────────────────────────────────────────────────────
function updateClock() {
  const now = new Date();
  const hh = String(now.getHours()).padStart(2,'0');
  const mm = String(now.getMinutes()).padStart(2,'0');
  const ss = String(now.getSeconds()).padStart(2,'0');
  clock.textContent = `${hh}:${mm}:${ss}`;
  const days   = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  dateDisplay.textContent = `${days[now.getDay()]} · ${months[now.getMonth()]} ${now.getDate()} ${now.getFullYear()}`;
}
updateClock();
setInterval(updateClock, 1000);

// ── Account groups (rebuilt after config received) ─────────────────────────
let accountLabels = { gmail1: 'Account 1', gmail2: 'Account 2', outlook: 'Outlook' };
let ACCOUNT_GROUPS = [];

function domainLabel(email) {
  if (!email || email.startsWith('Account') || email.startsWith('Gmail')) return 'Gmail';
  const domain = email.split('@')[1] || email;
  if (domain === 'gmail.com') return 'Personal Gmail';
  return domain.charAt(0).toUpperCase() + domain.slice(1);
}

function buildAccountGroups() {
  const g1 = accountLabels.gmail1;
  const g2 = accountLabels.gmail2;
  const ol = accountLabels.outlook;

  ACCOUNT_GROUPS = [
    {
      key: 'gmail1', label: g1,
      services: [
        { id: 'calendar_gmail1', label: 'Calendar', speech: `${g1} calendar` },
        { id: 'gmail_1_mail',    label: 'Gmail',    speech: `${g1} inbox` },
        { id: 'gmail_1_chat',    label: 'Chat',     speech: `${g1} chat` },
      ]
    },
    {
      key: 'gmail2', label: g2,
      services: [
        { id: 'calendar_gmail2', label: 'Calendar', speech: `${g2} calendar` },
        { id: 'gmail_2_mail',    label: 'Gmail',    speech: `${g2} inbox` },
        { id: 'slack',           label: 'Slack',    speech: 'Slack' },
      ]
    },
    {
      key: 'outlook', label: ol,
      services: [
        { id: 'outlook',          label: 'Outlook',  speech: `${ol} email` },
        { id: 'calendar_outlook', label: 'Calendar', speech: `${ol} calendar` },
        { id: 'teams',            label: 'Teams',    speech: 'Teams' },
      ]
    },
  ];
}

function flatServices() {
  const out = [];
  for (const g of ACCOUNT_GROUPS) {
    for (const s of g.services) out.push({ id: s.id, speech: s.speech, label: `${g.label} · ${s.label}` });
  }
  return out;
}

function applyAccountNames() {
  buildAccountGroups();
  buildServiceChips();
}

// ── Hex canvas ─────────────────────────────────────────────────────────────
(function initHexCanvas() {
  const canvas = document.getElementById('hex-canvas');
  const ctx = canvas.getContext('2d');
  let W, H, hexes;
  const R = 28;
  function hexPoints(cx,cy,r){const p=[];for(let i=0;i<6;i++){const a=Math.PI/180*(60*i-30);p.push([cx+r*Math.cos(a),cy+r*Math.sin(a)]);}return p;}
  function drawHex(cx,cy,alpha){const p=hexPoints(cx,cy,R-2);ctx.beginPath();ctx.moveTo(p[0][0],p[0][1]);for(let i=1;i<6;i++)ctx.lineTo(p[i][0],p[i][1]);ctx.closePath();ctx.strokeStyle=`rgba(139,92,246,${alpha})`;ctx.lineWidth=0.7;ctx.stroke();}
  function build(){W=canvas.width=window.innerWidth;H=canvas.height=window.innerHeight;hexes=[];const hw=Math.sqrt(3)*R,hh=R*2;for(let r=0;r*hh*0.75<H+hh;r++)for(let c=0;c*hw<W+hw;c++)hexes.push({x:c*hw+(r%2?hw/2:0),y:r*hh*0.75,phase:Math.random()*Math.PI*2});}
  let t=0;function animate(){ctx.clearRect(0,0,W,H);t+=0.007;for(const h of hexes){const a=(Math.sin(t+h.phase)*0.5+0.5)*0.55+0.1;drawHex(h.x,h.y,a);}requestAnimationFrame(animate);}
  build();window.addEventListener('resize',build);animate();
})();

// ── Arc reactor — CSS-driven multi-ring orb, HTML structure in index.html ──

// ── Audio ──────────────────────────────────────────────────────────────────
let audioCtx = null;
function startAmbientHum(){if(audioCtx)return;audioCtx=new(window.AudioContext||window.webkitAudioContext)();const o=audioCtx.createOscillator(),g=audioCtx.createGain();o.type='sine';o.frequency.value=60;g.gain.value=0.025;o.connect(g);g.connect(audioCtx.destination);o.start();}
function playPing(){if(!audioCtx)return;const o=audioCtx.createOscillator(),g=audioCtx.createGain();o.type='sine';o.frequency.setValueAtTime(880,audioCtx.currentTime);o.frequency.exponentialRampToValueAtTime(440,audioCtx.currentTime+0.3);g.gain.setValueAtTime(0.18,audioCtx.currentTime);g.gain.exponentialRampToValueAtTime(0.001,audioCtx.currentTime+0.4);o.connect(g);g.connect(audioCtx.destination);o.start();o.stop(audioCtx.currentTime+0.4);}

// ── TTS ────────────────────────────────────────────────────────────────────
let _cachedVoice = null;

// Ranked voice preferences — Daniel (macOS) is the closest to a natural British male
const _VOICE_PREFS = [
  ['Daniel',  'en-GB'],
  ['Arthur',  'en-GB'],
  ['Oliver',  'en-GB'],
  ['George',  'en-GB'],
  ['James',   'en-GB'],
  [null,      'en-GB'],  // any British English
  [null,      'en-AU'],  // Australian as fallback
  [null,      'en'],     // any English
];

function _preferredVoice() {
  if (_cachedVoice) return _cachedVoice;
  const voices = speechSynthesis.getVoices();
  for (const [name, lang] of _VOICE_PREFS) {
    const v = voices.find(v => v.lang.startsWith(lang) && (!name || v.name.includes(name)));
    if (v) { _cachedVoice = v; break; }
  }
  return _cachedVoice;
}
speechSynthesis.onvoiceschanged = () => { _cachedVoice = null; };

// Contextual prosody — Jarvis is calm, precise, and carries weight
const _PROSODY = {
  greeting: { rate: 0.82, pitch: 0.90 },  // warm, unhurried
  status:   { rate: 0.95, pitch: 0.88 },  // crisp, efficient
  briefing: { rate: 0.86, pitch: 0.84 },  // measured, authoritative
  critical: { rate: 0.80, pitch: 0.78 },  // solemn, grave
  detail:   { rate: 0.84, pitch: 0.85 },  // conversational
};

function _prepareText(text) {
  return text
    .replace(/\b([A-Z]{3,})\b/g, w => w[0] + w.slice(1).toLowerCase()) // SCANNING → Scanning
    .replace(/[—–]/g, ', ')          // em/en dash → natural pause
    .replace(/·/g, ',')              // interpunct → pause
    .replace(/\s*\.\.\.\s*/g, '… ') // normalize ellipsis
    .replace(/\s+/g, ' ')
    .trim();
}

async function _playTTSChunk(text, context) {
  if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  try {
    const res = await fetch(`/tts?text=${encodeURIComponent(text)}&context=${encodeURIComponent(context)}`);
    if (!res.ok) throw new Error('tts_fail');
    const buf = await res.arrayBuffer();
    if (!buf.byteLength) throw new Error('empty');
    const decoded = await audioCtx.decodeAudioData(buf);
    await new Promise((resolve) => {
      const src = audioCtx.createBufferSource();
      _currentTTSSrc = src;
      src.buffer = decoded;
      src.connect(audioCtx.destination);
      src.onended = resolve;
      src.start();
    });
    _currentTTSSrc = null;
  } catch {
    await new Promise(resolve => {
      const utt = new SpeechSynthesisUtterance(text);
      const v = _preferredVoice();
      if (v) utt.voice = v;
      utt.rate = 0.86; utt.pitch = 0.84;
      utt.onend = resolve;
      speechSynthesis.speak(utt);
    });
  }
}

async function _drainTTS() {
  if (_ttsPlaying) return;
  _ttsPlaying = true;
  while (_ttsQueue.length && !muted) {
    const { text, context } = _ttsQueue.shift();
    await _playTTSChunk(text, context);
    await new Promise(r => setTimeout(r, 130));
  }
  _ttsPlaying = false;
}

// Queue a briefing sentence: text reveals via its own paced queue, TTS plays independently.
function speakWithReveal(sentence, context = 'briefing') {
  if (!sentence.trim()) return;
  if (muted) {
    _flushWords(sentence);
    return;
  }
  _briefRevealBuf.push(sentence);
  _drainReveal();
  if (userHasInteracted) {
    _ttsQueue.push({ text: sentence, context });
    _drainTTS();
  }
}

function speak(text, context = 'briefing') {
  if (muted || !text.trim()) return;
  if (!userHasInteracted) { pendingGreeting = text; return; }
  const parts = _prepareText(text).match(/[^.!?…]+[.!?…]+\s*/g) || [_prepareText(text)];
  for (const part of parts) { if (part.trim()) _ttsQueue.push({ text: part.trim(), context }); }
  _drainTTS();
}

function unlockAudio() {
  if (userHasInteracted) return;
  userHasInteracted = true;
  startAmbientHum();
  if (pendingGreeting) { speak(pendingGreeting, 'greeting'); pendingGreeting = null; }
}

// ── Service chips (grouped by account) ────────────────────────────────────
function buildServiceChips() {
  const container = document.getElementById('service-list');
  container.innerHTML = '';
  for (const group of ACCOUNT_GROUPS) {
    const groupEl = document.createElement('div');
    groupEl.className = 'account-group';

    const header = document.createElement('div');
    header.className = 'account-group-header';
    header.textContent = group.label.toUpperCase();
    groupEl.appendChild(header);

    for (const svc of group.services) {
      const row = document.createElement('div');
      row.className = 'account-service-row';
      row.id = `chip-${svc.id}`;
      row.innerHTML = `<div class="dot"></div>
        <span class="service-row-num"></span>
        <span class="service-row-label">${svc.label}</span>
        <span class="chip-stale">STALE</span>
        <span class="service-row-state">IDLE</span>
        <button class="chip-retry" onclick="retryService(event,'${svc.id}')">RETRY</button>
        <button class="chip-relogin" onclick="reloginService(event,'${svc.id}')">RE-LOGIN</button>`;
      row.addEventListener('click', e => {
        if (!e.target.classList.contains('chip-retry') && !e.target.classList.contains('chip-relogin'))
          openDetailDrawer(svc.id, `${group.label} · ${svc.label}`);
      });
      groupEl.appendChild(row);
    }

    // number the rows within each group
    const rows = groupEl.querySelectorAll('.account-service-row');
    rows.forEach((r, i) => { r.querySelector('.service-row-num').textContent = `${i+1}.`; });

    container.appendChild(groupEl);
  }
}

function setChipState(serviceId, state) {
  const chip = document.getElementById(`chip-${serviceId}`);
  if (!chip) return;
  chip.className = `account-service-row ${state}`;
  chip.querySelector('.service-row-state').textContent = state.toUpperCase();
}

function formatTs(ts) {
  if (!ts) return 'never';
  return new Date(ts * 1000).toLocaleString('en-GB', { hour: '2-digit', minute: '2-digit', day: '2-digit', month: 'short' });
}

function mergeHealth(services) {
  for (const [key, health] of Object.entries(services || {})) {
    serviceHealthStore[key] = { ...(serviceHealthStore[key] || {}), ...health };
    applyServiceHealth(key, serviceHealthStore[key]);
  }
}

function applyServiceHealth(serviceKey, health) {
  const ids = _SERVICE_CHIP_MAP[serviceKey] || [serviceKey];
  for (const id of ids) {
    const row = document.getElementById(`chip-${id}`);
    if (!row) continue;
    const stale = !!health.stale;
    const state = stale ? 'stale' : (health.status === 'ok' ? 'done' : health.status === 'error' ? 'error' : health.status || 'idle');
    row.classList.remove('idle', 'scanning', 'done', 'error', 'stale');
    row.classList.add(state);
    row.title = `${health.service || serviceKey}: ${health.status || 'unknown'}${health.error ? ` — ${health.error}` : ''}`;
    row.querySelector('.service-row-state').textContent = stale ? 'STALE' : String(state).toUpperCase();
  }
}

function retryService(event, serviceId) {
  event.stopPropagation();
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  setChipState(serviceId, 'scanning');
  ws.send(`retry:${serviceId}`);
}

function reloginService(event, serviceId) {
  event.stopPropagation();
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  setChipState(serviceId, 'scanning');
  ws.send(`relogin:${serviceId}`);
}

// ── Calendar ───────────────────────────────────────────────────────────────
function eventTimeLabel(ev) {
  const rawTime = String(ev?.time || '').trim();
  if (rawTime) return rawTime;
  if (ev?.all_day === true || ev?.is_all_day === true || String(ev?.time_status || '').toLowerCase() === 'all_day') {
    return 'ALL DAY';
  }
  return 'TIME UNKNOWN';
}

function renderCalendar(events) {
  const list = document.getElementById('calendar-list');
  if (!events || events.length === 0) { list.innerHTML='<div class="no-events">NO EVENTS TODAY</div>'; return; }
  list.innerHTML = '';
  list.innerHTML = events.map(ev => {
    const timeLabel = eventTimeLabel(ev);
    const timeKind = timeLabel === 'TIME UNKNOWN' ? 'time-unknown' : timeLabel === 'ALL DAY' ? 'time-allday' : 'time-known';
    const statusClass = ev.status ? `status-${ev.status}` : '';
    const badge = ev.status === 'ongoing' ? ' <span class="event-status-badge">LIVE</span>'
                : ev.status === 'concluded' ? ' <span class="event-status-badge">DONE</span>'
                : '';
    const src = ev.source ? `<div class="event-source">${ev.source}</div>` : '';
    const detail = `${timeLabel}. ${ev.title}. ${ev.source ? 'From ' + ev.source + '.' : ''}`;
    return `<div class="event-card ${statusClass} ${timeKind}" onclick="speakDetail('${detail.replace(/'/g,"\\'")}')">
      <div class="event-time">${timeLabel}${badge}</div>
      <div class="event-title">${ev.title}</div>
      ${src}
    </div>`;
  }).join('');
  gsap.from('#calendar-list .event-card', { opacity: 0, x: 10, duration: 0.32, stagger: 0.06, ease: 'power2.out', clearProps: 'all' });
}

// ── Action items ───────────────────────────────────────────────────────────
function renderActionItems(items) {
  actionItemsStore = items || [];
  const list = document.getElementById('action-list');
  const count = document.getElementById('action-count');
  if (!items || items.length === 0) { list.innerHTML='<div class="no-events">NO ACTION ITEMS</div>'; count.textContent=''; return; }

  const critical = items.filter(i => i.priority === 'critical').length;
  count.textContent = critical ? `· ${critical} CRITICAL` : `· ${items.length}`;

  list.innerHTML = items.map((item, idx) => {
    const cls = item.priority === 'critical' ? 'action-critical'
               : item.priority === 'important' ? 'action-important' : 'action-info';
    const priorityLabel = item.priority?.toUpperCase() || 'INFO';
    const fromLine = item.from ? `<div class="action-from">↳ ${item.from}</div>` : '';
    return `<div class="action-card ${cls}" onclick="openActionModal(${idx})">
      <div class="action-meta">
        <div class="action-priority-dot"></div>
        <span class="action-priority-label">${priorityLabel}</span>
        <span class="action-source-label">${(item.source || '').toUpperCase()}</span>
      </div>
      <div class="action-text">${item.item}</div>
      ${fromLine}
      <div class="action-explain-hint">VIEW DETAILS</div>
    </div>`;
  }).join('');

  gsap.from('#action-list .action-card', { opacity: 0, x: 10, duration: 0.32, stagger: 0.06, ease: 'power2.out', clearProps: 'all' });

  // Flash header for critical items
  if (critical > 0) {
    gsap.fromTo('#header',
      { backgroundColor: 'rgba(217,119,6,0)' },
      { backgroundColor: 'rgba(217,119,6,0.12)', duration: 0.3, yoyo: true, repeat: 5, ease: 'power1.inOut',
        onComplete: () => gsap.set('#header', { clearProps: 'backgroundColor' }) }
    );
  }

  // Auto-read critical items + native notification
  const criticalItems = items.filter(i => i.priority === 'critical');
  if (criticalItems.length) {
    speak(`Attention, sir. You have ${criticalItems.length} critical item${criticalItems.length>1?'s':''} requiring immediate attention. `
      + criticalItems.map(i => i.item).join('. ') + '.', 'critical');
    if ('Notification' in window && Notification.permission === 'granted') {
      new Notification('J.A.R.V.I.S. — Critical Alert', {
        body: criticalItems[0].item,
        icon: '/favicon.svg',
        tag: 'jarvis-critical'
      });
    }
  }
}

function openActionModal(idx) {
  unlockAudio();
  const item = actionItemsStore[idx];
  if (!item) return;
  currentModalItem = item;

  const prio = item.priority || 'info';
  const badge = document.getElementById('modal-priority-badge');
  badge.textContent = prio.toUpperCase();
  badge.className = `modal-badge badge-${prio}`;

  const srcBadge = document.getElementById('modal-source-badge');
  srcBadge.textContent = (item.source || '').toUpperCase();

  document.getElementById('modal-item-text').textContent = item.item || '';

  const fromRow = document.getElementById('modal-from-row');
  if (item.from) {
    document.getElementById('modal-from').textContent = item.from;
    fromRow.style.display = '';
  } else {
    fromRow.style.display = 'none';
  }

  const subjRow = document.getElementById('modal-subject-row');
  if (item.subject) {
    document.getElementById('modal-subject').textContent = item.subject;
    subjRow.style.display = '';
  } else {
    subjRow.style.display = 'none';
  }

  document.getElementById('modal-detail').textContent = item.detail || 'No additional context available.';

  document.getElementById('action-modal').classList.add('open');
}

function closeActionModal(e) {
  if (e && e.target !== document.getElementById('action-modal')) return;
  document.getElementById('action-modal').classList.remove('open');
  currentModalItem = null;
}

function speakModalContent() {
  if (!currentModalItem) return;
  const item = currentModalItem;
  // Read only the prose detail — it already contains the full context naturally
  speakDetail(item.detail || item.item);
}

function speakDetail(text) {
  unlockAudio();
  speechSynthesis.cancel();
  _ttsQueue = [];
  _ttsPlaying = false;
  if (_currentTTSSrc) { try { _currentTTSSrc.stop(); } catch {} _currentTTSSrc = null; }
  speak(text, 'detail');
}

// ── Live notifications ─────────────────────────────────────────────────────
let _notifTimer = null;
let _lastNotifService = null;
const _notifHistory = []; // [{service, items, count, time}]

// Map scraper key → chip id(s) to highlight
const _SERVICE_CHIP_MAP = {
  gmail_1: ['gmail_1_mail', 'gmail_1_chat'],
  gmail_2: ['gmail_2_mail'],
  outlook: ['outlook'],
  slack:   ['slack'],
  teams:   ['teams'],
};

function showLiveNotification(msg) {
  const { service, service_label, items, count } = msg;
  _lastNotifService = service;
  _notifHistory.unshift({ service, service_label, items, count, time: new Date() });

  // Badge + glow on relevant service chip(s)
  const chipIds = _SERVICE_CHIP_MAP[service] || [service];
  for (const chipId of chipIds) {
    const row = document.getElementById(`chip-${chipId}`);
    if (!row) continue;
    row.classList.add('has-notification');
    let badge = row.querySelector('.chip-badge');
    if (!badge) {
      badge = document.createElement('span');
      badge.className = 'chip-badge';
      row.querySelector('.service-row-state').before(badge);
    }
    badge.textContent = (parseInt(badge.textContent) || 0) + count;
  }

  // Populate toast
  document.getElementById('notif-toast-label').textContent = (service_label || service).toUpperCase();
  document.getElementById('notif-toast-count').textContent = `+${count} NEW`;

  const bodyEl = document.getElementById('notif-toast-body');
  bodyEl.innerHTML = items.slice(0, 3).map(item => {
    const from   = item.from || item.channel || '';
    const detail = item.subject || item.preview || (item.count ? `${item.count} new messages` : '');
    return `<div class="notif-item">
      <span class="notif-item-from">${_esc(from)}</span>
      <span class="notif-item-detail">${_esc(detail)}</span>
    </div>`;
  }).join('') + (count > 3 ? `<div style="font-size:10px;color:rgba(255,170,0,0.4);padding-top:4px">+${count - 3} more</div>` : '');

  const toast = document.getElementById('notif-toast');
  toast.classList.add('show');
  clearTimeout(_notifTimer);
  _notifTimer = setTimeout(() => toast.classList.remove('show'), 10000);

  // TTS alert
  const first = items[0];
  const who = first.from || first.channel || 'someone';
  const kindWord = first.kind === 'email' ? 'email' : 'message';
  speak(`Sir, new ${kindWord} from ${who}${count > 1 ? ` and ${count - 1} others` : ''}.`, 'status');
}

function _esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function openNotifDrawer() {
  if (!_lastNotifService) return;
  const svc = flatServices().find(s => s.id === _lastNotifService
    || (_SERVICE_CHIP_MAP[_lastNotifService] || []).includes(s.id));
  const label = svc ? svc.label : _lastNotifService;
  document.getElementById('notif-toast').classList.remove('show');
  openDetailDrawer(_lastNotifService === 'gmail_1' ? 'gmail_1_mail'
                 : _lastNotifService === 'gmail_2' ? 'gmail_2_mail'
                 : _lastNotifService, label);
}

// ── Detail drawer ──────────────────────────────────────────────────────────
function openDetailDrawer(serviceId, label) {
  const data = serviceDataStore[serviceId];
  const meta = data?.meta || serviceHealthStore[serviceId] || {};
  const drawer = document.getElementById('detail-drawer');
  document.getElementById('drawer-title').textContent = label.toUpperCase();
  const body = document.getElementById('drawer-body');
  if (!data) { body.innerHTML = '<div class="drawer-empty">NO DATA — RUN A BRIEFING FIRST</div>'; }
  else {
    let html = '';
    if (meta.stale || data.error) {
      html += `<div class="drawer-health-banner ${meta.stale ? 'stale' : 'error'}">
        <div>${meta.stale ? 'STALE DATA' : 'SERVICE ERROR'}</div>
        <span>${_esc(data.error || meta.error || 'Last scan could not verify fresh data')}</span>
        ${meta.last_success_ts ? `<span>Last success: ${formatTs(meta.last_success_ts)}</span>` : ''}
        ${meta.diagnostic_path ? `<span>Diagnostics: ${_esc(meta.diagnostic_path)}</span>` : ''}
      </div>`;
    }
    const emails = data.emails || [], chats = data.chats || [], messages = data.messages || [], events = data.events || [];
    if (emails.length) {
      html += `<div class="drawer-section-title">EMAILS (${emails.length})</div>`;
      html += emails.map(e=>`<div class="drawer-item"><div class="drawer-item-meta">${e.sender||'Unknown'}</div><div class="drawer-item-subject">${e.subject||'(no subject)'}</div>${e.snippet?`<div class="drawer-item-preview">${e.snippet}</div>`:''}</div>`).join('');
    }
    if (chats.length) {
      const dms    = chats.filter(c => c.type === 'dm');
      const spaces = chats.filter(c => c.type !== 'dm');
      if (dms.length) {
        html += `<div class="drawer-section-title">DIRECT MESSAGES (${dms.length})</div>`;
        html += dms.map(c=>`<div class="drawer-item drawer-item-highlight"><div class="drawer-item-meta"><span class="drawer-tag drawer-tag-dm">DM</span>${c.from||''}</div><div class="drawer-item-preview">${c.preview||''}</div></div>`).join('');
      }
      if (spaces.length) {
        html += `<div class="drawer-section-title">SPACES &amp; GROUPS (${spaces.length})</div>`;
        html += spaces.map(c=>`<div class="drawer-item"><div class="drawer-item-meta">${c.from||''}</div><div class="drawer-item-preview">${c.preview||''}</div></div>`).join('');
      }
    }
    if (messages.length) {
      const mentioned = messages.filter(m => m.is_mention || m.is_dm);
      const general   = messages.filter(m => !m.is_mention && !m.is_dm);
      const renderMsg = (m, highlight) => {
        const name = m.channel || m.chat || '';
        const tag  = m.is_mention ? `<span class="drawer-tag drawer-tag-mention">@</span>` : m.is_dm ? `<span class="drawer-tag drawer-tag-dm">DM</span>` : '';
        const cls  = highlight ? 'drawer-item drawer-item-highlight' : 'drawer-item';
        let inner  = `<div class="${cls}"><div class="drawer-item-meta">${tag}${name}</div>`;
        const recent = m.recent_messages || [];
        if (recent.length) {
          inner += recent.map(r=>`<div class="drawer-msg-row"><span class="drawer-msg-sender">${r.sender||''}</span><span class="drawer-msg-text">${r.text||''}</span></div>`).join('');
        } else if (m.preview) {
          inner += `<div class="drawer-item-preview">${m.preview}</div>`;
        } else {
          inner += `<div class="drawer-item-preview">${m.unread} unread</div>`;
        }
        inner += `</div>`;
        return inner;
      };
      if (mentioned.length) {
        html += `<div class="drawer-section-title">MENTIONS &amp; DIRECT (${mentioned.length})</div>`;
        html += mentioned.map(m=>renderMsg(m, true)).join('');
      }
      if (general.length) {
        html += `<div class="drawer-section-title">CHANNEL ACTIVITY (${general.length})</div>`;
        html += general.map(m=>renderMsg(m, false)).join('');
      }
      if (!mentioned.length && !general.length) {
        html += `<div class="drawer-section-title">MESSAGES (${messages.length})</div>`;
        html += messages.map(m=>renderMsg(m, false)).join('');
      }
    }
    if (events.length) {
      html += `<div class="drawer-section-title">EVENTS (${events.length})</div>`;
      html += events.map(ev=>`<div class="drawer-item"><div class="drawer-item-meta">${eventTimeLabel(ev)}</div><div class="drawer-item-subject">${ev.title}</div></div>`).join('');
    }
    body.innerHTML = html || '<div class="drawer-empty">NO ITEMS FOUND</div>';
  }
  drawer.classList.add('open');
}

function healthSummaryText() {
  const rows = Object.entries(serviceHealthStore).map(([key, h]) =>
    `${key}: ${h.status || 'unknown'} | stale=${!!h.stale} | items=${h.item_count ?? '?'} | duration=${h.duration_ms ?? '?'}ms | last_success=${formatTs(h.last_success_ts)} | error=${h.error || ''} | diag=${h.diagnostic_path || ''}`
  );
  return rows.length ? rows.join('\n') : 'No health data yet. Run a briefing first.';
}

function copyHealthSummary() {
  const text = healthSummaryText();
  navigator.clipboard?.writeText(text).then(() => {
    statusText.textContent = 'DIAGNOSTIC SUMMARY COPIED';
  }).catch(() => {
    statusText.textContent = 'COPY FAILED — SEE HEALTH DRAWER';
  });
}

function openHealthDrawer() {
  document.getElementById('drawer-title').textContent = 'SERVICE HEALTH';
  const entries = Object.entries(serviceHealthStore);
  const body = document.getElementById('drawer-body');
  if (!entries.length) {
    body.innerHTML = '<div class="drawer-empty">NO HEALTH DATA YET — RUN A BRIEFING FIRST</div>';
  } else {
    body.innerHTML = `<button class="drawer-copy-btn" onclick="copyHealthSummary()">COPY DIAGNOSTIC SUMMARY</button>` +
      entries.map(([key, h]) => {
        const cls = h.stale ? 'stale' : h.status === 'ok' ? 'ok' : 'error';
        return `<div class="health-card ${cls}">
          <div class="health-card-top"><span>${_esc(h.service || key)}</span><strong>${_esc(h.stale ? 'STALE' : h.status || 'UNKNOWN')}</strong></div>
          <div class="health-line">Items: ${h.item_count ?? 0} · Duration: ${h.duration_ms ?? '?'}ms · Confidence: ${_esc(h.confidence || '?')}</div>
          <div class="health-line">Last success: ${formatTs(h.last_success_ts)}</div>
          ${h.error ? `<div class="health-error">${_esc(h.error)}</div>` : ''}
          ${h.diagnostic_path ? `<div class="health-line">Diagnostics: ${_esc(h.diagnostic_path)}</div>` : ''}
        </div>`;
      }).join('');
  }
  document.getElementById('detail-drawer').classList.add('open');
}

// ── Idle visualization ─────────────────────────────────────────────────────
const _HEX = '0123456789ABCDEF';
function _rh2() { return _HEX[Math.random()*16|0] + _HEX[Math.random()*16|0]; }

function initIdleViz() {
  const grid = document.getElementById('idle-hex-grid');
  if (!grid) return;
  const COLS = 22, ROWS = 6;
  for (let i = 0; i < COLS * ROWS; i++) {
    const c = document.createElement('span');
    c.className = 'hex-cell';
    c.textContent = _rh2();
    c.style.opacity = (Math.random() * 0.28 + 0.06).toFixed(2);
    grid.appendChild(c);
  }
  setInterval(() => {
    const cells = grid.children;
    const n = (Math.random() * 10 | 0) + 3;
    for (let i = 0; i < n; i++) {
      const el = cells[Math.random() * cells.length | 0];
      el.textContent = _rh2();
      el.style.opacity = (Math.random() * 0.32 + 0.06).toFixed(2);
    }
  }, 120);
}

// Ticker messages that rotate in the readout panel
const _TICKERS = [
  'MEMORY SYSTEMS NOMINAL',
  'NETWORK LINK ESTABLISHED',
  'SESSION TOKENS VALID',
  'PLAYWRIGHT CONTEXTS READY',
  'GROQ API RESPONSIVE',
  'ALL SUBSYSTEMS OPTIMAL',
  'STANDING BY FOR COMMAND',
];
let _tickerIdx = 0;
function initTicker() {
  const el = document.getElementById('brief-ticker');
  if (!el) return;
  el.textContent = _TICKERS[0];
  setInterval(() => {
    _tickerIdx = (_tickerIdx + 1) % _TICKERS.length;
    gsap.to(el, {
      opacity: 0, duration: 0.22, ease: 'power1.in',
      onComplete() {
        el.textContent = _TICKERS[_tickerIdx];
        gsap.to(el, { opacity: 1, duration: 0.35, ease: 'power1.out' });
      }
    });
  }, 4000);
}

// Stats computation after briefing — numbers count up via GSAP
function _gsapCount(id, target) {
  const el = document.getElementById(id);
  if (!el) return;
  const proxy = { n: 0 };
  gsap.to(proxy, {
    n: target, duration: 1.1, ease: 'power2.out',
    onUpdate() { el.textContent = Math.round(proxy.n); }
  });
}

function updateBriefStats() {
  let unread = 0;
  for (const key of ['gmail_1', 'gmail_2', 'outlook', 'slack', 'teams']) {
    const d = serviceDataStore[key];
    if (!d) continue;
    unread += (d.emails || []).length + (d.chats || []).length + (d.messages || []).length;
  }
  const critical = actionItemsStore.filter(i => i.priority === 'critical').length;
  const events = document.querySelectorAll('.event-card:not(.status-concluded)').length;
  const now = new Date();
  const hh = String(now.getHours()).padStart(2,'0');
  const mm = String(now.getMinutes()).padStart(2,'0');

  _gsapCount('bstat-unread',   unread   || 0);
  _gsapCount('bstat-critical', critical || 0);
  _gsapCount('bstat-events',   events   || 0);
  document.getElementById('bstat-brieftime').textContent = `${hh}:${mm}`;

  // Stagger-reveal the stat boxes
  gsap.from('.bstat', { opacity: 0, y: 8, duration: 0.45, stagger: 0.09, ease: 'power2.out', clearProps: 'all' });
}

// ── Word-reveal briefing ────────────────────────────────────────────────────
let _sentBuf = '';
let _briefRevealBuf = [];
let _briefRevealActive = false;

function _flushWords(sentence) {
  const p = document.createElement('p');
  p.className = 'brief-para';
  const words = sentence.split(/\s+/).filter(Boolean);
  words.forEach(w => {
    const s = document.createElement('span');
    s.className = 'word';
    s.textContent = w + ' ';
    p.appendChild(s);
  });
  briefingOutput.appendChild(p);
  briefingOutput.scrollTop = briefingOutput.scrollHeight;
  gsap.from(p.querySelectorAll('.word'), {
    opacity: 0, y: 6, duration: 0.28, stagger: 0.38, ease: 'power2.out', clearProps: 'all'
  });
}

// Reveals sentences one at a time, paced to approximate speech speed.
// Completely decoupled from TTS so it works regardless of audio state.
async function _drainReveal() {
  if (_briefRevealActive) return;
  _briefRevealActive = true;
  while (_briefRevealBuf.length) {
    const sentence = _briefRevealBuf.shift();
    _flushWords(sentence);
    // Pace next sentence after current one has fully revealed (stagger*words) + brief gap
    const words = sentence.split(/\s+/).filter(Boolean).length;
    await new Promise(r => setTimeout(r, words * 380 + 500));
  }
  _briefRevealActive = false;
}

function finishBriefing() {
  if (briefingBuffer.trim()) {
    speakWithReveal(briefingBuffer.trim(), 'briefing');
    briefingBuffer = '';
  }
}

// ── WebSocket ──────────────────────────────────────────────────────────────
function connect() {
  if (_reconnectTimer) {
    clearTimeout(_reconnectTimer);
    _reconnectTimer = null;
  }
  ws = new WebSocket(`ws://${location.host}/ws`);
  window._jarvis_ws = ws;

  ws.onopen = () => {
    _reconnectDelay = 2000;
    connIndicator.className = 'connected';
    statusText.textContent = 'SYSTEMS ONLINE';
    btnBrief.disabled = false;
    document.getElementById('btn-setup').disabled = false;
  };
  ws.onerror = () => { statusText.textContent = 'CONNECTION ERROR — IS SERVER RUNNING?'; };
  ws.onclose = () => {
    connIndicator.className = '';
    const delay = _reconnectDelay + Math.floor(Math.random() * 750);
    statusText.textContent = `RECONNECTING IN ${Math.round(delay / 1000)}S...`;
    btnBrief.disabled = true;
    document.getElementById('btn-setup').disabled = true;
    _reconnectTimer = setTimeout(connect, delay);
    _reconnectDelay = Math.min(_reconnectDelay * 1.7, 30000);
  };

  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);

    if (msg.type === 'config') {
      accountNames = { ...accountNames, ...msg.accounts };
      if (msg.labels) accountLabels = { ...accountLabels, ...msg.labels };
      applyAccountNames();
    }

    if (msg.type === 'greeting') {
      statusText.textContent = msg.text.toUpperCase();
      speak(msg.text, 'greeting');
    }

    if (msg.type === 'status') {
      setChipState(msg.service, msg.state);
      const svc = flatServices().find(s => s.id === msg.service);
      const speech = svc?.speech || svc?.label || msg.service;
      if (msg.state === 'scanning') speak(`Scanning ${speech}.`, 'status');
      if (msg.state === 'done')     { playPing(); speak(`${speech} complete.`, 'status'); }
      if (msg.state === 'error')    speak(`${speech} unavailable.`, 'status');
    }

    if (msg.type === 'service_data') {
      serviceDataStore[msg.service] = msg.data;
      if (msg.data?.meta) mergeHealth({ [msg.service]: { ...(msg.data.meta || {}), service: msg.data.service, error: msg.data.error } });
    }

    if (msg.type === 'phase') {
      if (msg.phase === 'relogin') {
        // Relogin for a specific service — show the LOGGED IN button
        document.getElementById('btn-next').style.display = '';
        statusText.textContent = 'LOG INTO THE SERVICE IN THE BROWSER WINDOW, THEN CLICK NEXT';
      } else if (msg.phase === 'scanning') {
        gsapScanStart();
        panelMain.className = 'panel scanning';
        statusText.textContent = 'SCANNING SERVICES...';
        document.getElementById('brief-readout-state').textContent = 'SCANNING ALL SERVICES…';
        speak('Initiating briefing sequence, sir.', 'briefing');
      } else if (msg.phase === 'briefing') {
        panelMain.className = 'panel briefing';
        statusText.textContent = 'COMPILING BRIEFING...';
        document.getElementById('brief-readout-state').textContent = 'COMPILING INTELLIGENCE BRIEFING…';
        speak('All services scanned. Compiling your intelligence briefing, sir.', 'briefing');
        briefingOutput.innerHTML = '';
        briefingBuffer = '';
        _sentBuf = '';
        _briefRevealBuf = [];
        _briefRevealActive = false;
      }
    }

    if (msg.type === 'calendar_events') renderCalendar(msg.events);
    if (msg.type === 'action_items')    renderActionItems(msg.items);
    if (msg.type === 'notification')    showLiveNotification(msg);
    if (msg.type === 'health_update')   mergeHealth(msg.services);

    if (msg.type === 'briefing_chunk') {
      briefingBuffer += msg.text;
      // Reveal each complete sentence as soon as it's formed; keep trailing partial text
      const sentences = briefingBuffer.match(/[^.!?]+[.!?]+/g);
      if (sentences) {
        for (const s of sentences) { if (s.trim()) speakWithReveal(s.trim(), 'briefing'); }
        briefingBuffer = briefingBuffer.replace(/[^.!?]+[.!?]+/g, '').trimStart();
      }
    }

    if (msg.type === 'done') {
      finishBriefing();
      gsapScanEnd();
      panelMain.className = 'panel briefed';
      statusText.textContent = 'BRIEFING COMPLETE';
      document.getElementById('brief-readout-state').textContent = 'BRIEFING COMPLETE · STANDING BY';
      updateBriefStats();
      btnBrief.disabled = false;
      btnBrief.textContent = 'NEW BRIEFING';
      document.getElementById('qa-output').innerHTML = '';
      setTimeout(() => document.getElementById('qa-input').focus(), 400);
    }

    if (msg.type === 'qa_chunk') {
      const qaOut = document.getElementById('qa-output');
      const lastAnswer = qaOut.querySelector('.qa-block:last-child .qa-answer');
      if (lastAnswer) lastAnswer.textContent += msg.text;
      qaOut.scrollTop = qaOut.scrollHeight;
    }

    if (msg.type === 'qa_done') {
      _qaAsking = false;
      const qaOut = document.getElementById('qa-output');
      const lastAnswer = qaOut.querySelector('.qa-block:last-child .qa-answer');
      if (lastAnswer) {
        const text = lastAnswer.textContent;
        lastAnswer.innerHTML = '';
        text.split(/\s+/).filter(Boolean).forEach(w => {
          const s = document.createElement('span'); s.className = 'word'; s.textContent = w + ' '; lastAnswer.appendChild(s);
        });
        gsap.from(lastAnswer.querySelectorAll('.word'), { opacity: 0, y: 4, duration: 0.25, stagger: 0.03, ease: 'power2.out', clearProps: 'all' });
        speak(text, 'briefing');
      }
      document.getElementById('qa-input').focus();
    }

    if (msg.type === 'setup_status') {
      const labels = { gmail1: accountNames.gmail1, gmail2: accountNames.gmail2, outlook: accountNames.outlook, slack: 'Slack', teams: 'Teams' };
      const name = labels[msg.profile] || msg.profile;
      if (msg.state === 'awaiting_login') {
        statusText.textContent = `LOG INTO ${name.toUpperCase()} IN BROWSER, THEN CLICK NEXT`;
        document.getElementById('btn-next').style.display = '';
      } else if (msg.state === 'authed') {
        statusText.textContent = `${name.toUpperCase()} — SESSION SAVED`;
        document.getElementById('btn-next').style.display = 'none';
      }
    }

    if (msg.type === 'relogin_done') {
      document.getElementById('btn-next').style.display = 'none';
      statusText.textContent = 'RE-LOGIN COMPLETE — RETRYING SERVICE...';
    }

    if (msg.type === 'setup_done') {
      statusText.textContent = 'ALL LOGINS COMPLETE';
      document.getElementById('btn-next').style.display = 'none';
      const btn = document.getElementById('btn-setup');
      btn.disabled = false; btn.textContent = 'RE-LOGIN';
    }

    if (msg.type === 'error') {
      statusText.textContent = `ERROR: ${msg.message.slice(0,60)}`;
      btnBrief.disabled = false;
    }
  };
}

// ── Controls ───────────────────────────────────────────────────────────────
btnBrief.addEventListener('click', () => {
  unlockAudio();
  if (!ws || ws.readyState !== WebSocket.OPEN) { statusText.textContent='NOT CONNECTED'; return; }
  btnBrief.disabled = true;
  btnBrief.textContent = 'SCANNING...';
  briefingOutput.innerHTML = '';
  _sentBuf = '';
  panelMain.className = 'panel';
  document.getElementById('brief-readout-state').textContent = 'STANDBY · AWAITING COMMAND, SIR';
  document.getElementById('calendar-list').innerHTML = '<div class="no-events">SCANNING...</div>';
  document.getElementById('action-list').innerHTML = '<div class="no-events">SCANNING...</div>';
  document.getElementById('action-count').textContent = '';
  flatServices().forEach(s => {
    setChipState(s.id, 'idle');
    const row = document.getElementById(`chip-${s.id}`);
    if (row) {
      row.classList.remove('has-notification');
      const badge = row.querySelector('.chip-badge');
      if (badge) badge.remove();
    }
  });
  ws.send('brief');
});

document.getElementById('btn-setup').addEventListener('click', () => {
  unlockAudio();
  if (!ws || ws.readyState !== WebSocket.OPEN) { statusText.textContent='NOT CONNECTED'; return; }
  const btn = document.getElementById('btn-setup');
  btn.disabled = true; btn.textContent = 'LOGGING IN...';
  statusText.textContent = 'OPENING BROWSERS FOR LOGIN...';
  ws.send('setup');
});

document.getElementById('btn-next').addEventListener('click', () => {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  document.getElementById('btn-next').style.display = 'none';
  ws.send('setup_next');
});

btnMute.addEventListener('click', () => {
  muted = !muted;
  if (muted) {
    speechSynthesis.cancel();
    _ttsQueue = []; _ttsPlaying = false;
    if (_currentTTSSrc) { try { _currentTTSSrc.stop(); } catch {} _currentTTSSrc = null; }
    // Flush pending reveal queue so muting doesn't hide briefing content
    while (_briefRevealBuf.length) _flushWords(_briefRevealBuf.shift());
    _briefRevealActive = false;
    btnMute.textContent = 'UNMUTE'; btnMute.classList.add('active');
  } else {
    btnMute.textContent = 'MUTE'; btnMute.classList.remove('active');
  }
});

// ── Ambient mode ───────────────────────────────────────────────────────────
if (new URLSearchParams(window.location.search).get('ambient') === '1') {
  document.getElementById('app').classList.add('ambient');
}

// ── Keyboard-first action nav ──────────────────────────────────────────────
let _focusedActionIdx = -1;

function _updateActionFocus(idx) {
  const cards = document.querySelectorAll('#action-list .action-card');
  cards.forEach((c, i) => c.classList.toggle('focused', i === idx));
  _focusedActionIdx = idx;
  if (cards[idx]) cards[idx].scrollIntoView({ block: 'nearest' });
}

// ── Conversational Q&A ─────────────────────────────────────────────────────
let _qaAsking = false;

function submitQA() {
  if (_qaAsking || !ws || ws.readyState !== WebSocket.OPEN) return;
  const inp = document.getElementById('qa-input');
  const q = inp.value.trim();
  if (!q) return;
  inp.value = '';
  _qaAsking = true;

  const qaOut = document.getElementById('qa-output');
  const block = document.createElement('div');
  block.className = 'qa-block';
  block.innerHTML = `<div class="qa-q-label">↳ ${q}</div><div class="qa-answer"></div>`;
  qaOut.appendChild(block);
  qaOut.scrollTop = qaOut.scrollHeight;

  ws.send(`ask:${q}`);
}

document.getElementById('qa-submit').addEventListener('click', submitQA);
document.getElementById('qa-input').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); submitQA(); } });

// ── Command Palette ────────────────────────────────────────────────────────
const _COMMANDS = [
  { icon: 'ti-radar-2',      label: 'New Briefing',         desc: 'Run a full intelligence scan',          action: () => btnBrief.click() },
  { icon: 'ti-volume',       label: 'Mute / Unmute',        desc: 'Toggle voice output',                   action: () => btnMute.click() },
  { icon: 'ti-login',        label: 'Setup Logins',         desc: 'Open browser login flow for services',  action: () => document.getElementById('btn-setup').click() },
  { icon: 'ti-calendar-event', label: 'View Schedule',      desc: "Jump to today's schedule panel",        action: () => document.getElementById('panel-calendar').scrollIntoView({ behavior: 'smooth' }) },
  { icon: 'ti-list-check',   label: 'View Action Items',    desc: 'Jump to action items panel',            action: () => document.getElementById('panel-actions').scrollIntoView({ behavior: 'smooth' }) },
  { icon: 'ti-bell',         label: 'Enable Notifications', desc: 'Request system notification permission', action: () => Notification.requestPermission() },
  { icon: 'ti-heartbeat',    label: 'Service Health',       desc: 'Inspect scraper status and diagnostics', action: openHealthDrawer },
  { icon: 'ti-history',      label: 'Briefing History',     desc: 'View past intelligence briefings',
    action: () => {
      fetch('/history').then(r => r.json()).then(entries => {
        document.getElementById('drawer-title').textContent = 'BRIEFING HISTORY';
        const body = document.getElementById('drawer-body');
        if (!entries.length) { body.innerHTML = '<div class="drawer-empty">NO HISTORY YET — RUN A BRIEFING FIRST</div>'; }
        else {
          body.innerHTML = entries.map(b => {
            const d = new Date(b.ts * 1000);
            const label = d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
            return `<div class="drawer-item"><div class="drawer-item-meta">${label}</div><div class="drawer-item-preview">${b.text.slice(0, 140)}…</div></div>`;
          }).join('');
        }
        document.getElementById('detail-drawer').classList.add('open');
      }).catch(() => {});
    }
  },
];
let _palOpen = false, _palIdx = 0, _palFiltered = [..._COMMANDS];

function openPalette() {
  _palOpen = true; _palIdx = 0; _palFiltered = [..._COMMANDS];
  document.getElementById('cmd-overlay').classList.add('open');
  const inp = document.getElementById('cmd-input');
  inp.value = '';
  _renderPalette();
  setTimeout(() => inp.focus(), 10);
}
function closePalette() {
  _palOpen = false;
  document.getElementById('cmd-overlay').classList.remove('open');
}
function _renderPalette() {
  const list = document.getElementById('cmd-list');
  if (!_palFiltered.length) { list.innerHTML = '<div class="cmd-empty">No commands found</div>'; return; }
  list.innerHTML = _palFiltered.map((cmd, i) => `
    <div class="cmd-item${i === _palIdx ? ' selected' : ''}" data-idx="${i}">
      <i class="ti ${cmd.icon} cmd-item-icon" aria-hidden="true"></i>
      <div><div class="cmd-item-label">${cmd.label}</div><div class="cmd-item-desc">${cmd.desc}</div></div>
    </div>`).join('');
  list.querySelectorAll('.cmd-item').forEach(el =>
    el.addEventListener('click', () => { const c = _palFiltered[+el.dataset.idx]; closePalette(); c?.action(); })
  );
  const sel = list.querySelector('.selected');
  if (sel) sel.scrollIntoView({ block: 'nearest' });
}
document.getElementById('cmd-input').addEventListener('input', e => {
  const q = e.target.value.toLowerCase();
  _palIdx = 0;
  _palFiltered = q ? _COMMANDS.filter(c => c.label.toLowerCase().includes(q) || c.desc.toLowerCase().includes(q)) : [..._COMMANDS];
  _renderPalette();
});

// ── Keyboard shortcuts ─────────────────────────────────────────────────────
document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'k') { e.preventDefault(); _palOpen ? closePalette() : openPalette(); return; }
  if (_palOpen) {
    if (e.key === 'ArrowDown') { e.preventDefault(); _palIdx = Math.min(_palIdx + 1, _palFiltered.length - 1); _renderPalette(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); _palIdx = Math.max(_palIdx - 1, 0); _renderPalette(); }
    else if (e.key === 'Enter') { const c = _palFiltered[_palIdx]; closePalette(); c?.action(); }
    else if (e.key === 'Escape') { closePalette(); }
    return;
  }
  // Action item keyboard nav (J/K/Enter/S/X) — skip if typing in an input
  if (document.activeElement.tagName === 'INPUT') return;
  const cards = document.querySelectorAll('#action-list .action-card');
  if (e.key === 'j' || e.key === 'J') { if (cards.length) _updateActionFocus(Math.min(_focusedActionIdx + 1, cards.length - 1)); return; }
  if (e.key === 'k' || e.key === 'K') { if (_focusedActionIdx > 0) _updateActionFocus(_focusedActionIdx - 1); return; }
  if (e.key === 'Enter' && _focusedActionIdx >= 0 && cards.length) { openActionModal(_focusedActionIdx); return; }
  if ((e.key === 's' || e.key === 'S') && _focusedActionIdx >= 0) {
    const item = actionItemsStore[_focusedActionIdx]; if (item) speakDetail(item.detail || item.item); return;
  }
  if ((e.key === 'x' || e.key === 'X') && _focusedActionIdx >= 0) {
    actionItemsStore.splice(_focusedActionIdx, 1);
    renderActionItems(actionItemsStore);
    _focusedActionIdx = Math.min(_focusedActionIdx, actionItemsStore.length - 1);
    if (_focusedActionIdx >= 0) _updateActionFocus(_focusedActionIdx);
    return;
  }
  if (e.key === 'Escape') {
    document.getElementById('action-modal').classList.remove('open');
    document.getElementById('detail-drawer').classList.remove('open');
    currentModalItem = null;
  }
});

// ── GSAP ───────────────────────────────────────────────────────────────────
function initGSAP() {
  // Entrance: header slides down, panels stagger up, footer slides up
  gsap.from('#header', { opacity: 0, y: -12, duration: 0.5, ease: 'power2.out' });
  gsap.from('#footer', { opacity: 0, y: 12, duration: 0.45, ease: 'power2.out', delay: 0.3 });
  gsap.from('.panel', {
    opacity: 0, y: 16, duration: 0.55,
    stagger: { amount: 0.28, from: 'start' },
    ease: 'power2.out', delay: 0.1, clearProps: 'all'
  });

  // Orb — replaces all CSS keyframe animations on orb child divs
  const tickEl   = document.querySelector('.orb-tick-ring');
  const midEl    = document.querySelector('.orb-mid-ring');
  const sphereEl = document.querySelector('.orb-sphere');

  if (tickEl)   _orbTick   = gsap.to(tickEl,   { rotation: 360,  duration: 20, ease: 'none', repeat: -1, transformOrigin: '50% 50%' });
  if (midEl)    _orbMid    = gsap.to(midEl,     { rotation: -360, duration: 9,  ease: 'none', repeat: -1, transformOrigin: '50% 50%' });
  if (sphereEl) _orbSphere = gsap.to(sphereEl,  { scale: 1.06, opacity: 1, duration: 4, ease: 'sine.inOut', repeat: -1, yoyo: true, transformOrigin: '50% 50%' });
  if (sphereEl) gsap.set(sphereEl, { opacity: 0.85, scale: 1 });

  // Idle radar — soft opacity breathe replacing CSS keyframe
  const radar = document.getElementById('idle-hex-grid');
  if (radar) gsap.to(radar, { opacity: 1, duration: 5, ease: 'sine.inOut', repeat: -1, yoyo: true, startAt: { opacity: 0.55 } });
}

function gsapScanStart() {
  if (!_orbTick) return;
  // Spin 11× faster (20s → ~1.8s effective) — smooth ramp via timeScale tween
  gsap.to(_orbTick,   { timeScale: 11,  duration: 0.7, ease: 'power2.in' });
  gsap.to(_orbMid,    { timeScale: 7.5, duration: 0.7, ease: 'power2.in' });
  gsap.to(_orbSphere, { timeScale: 6.5, duration: 0.5, ease: 'power2.in' });
}

function gsapScanEnd() {
  if (!_orbTick) return;
  // Decelerate back to idle speed
  gsap.to(_orbTick,   { timeScale: 1, duration: 1.4, ease: 'power2.out' });
  gsap.to(_orbMid,    { timeScale: 1, duration: 1.4, ease: 'power2.out' });
  gsap.to(_orbSphere, { timeScale: 1, duration: 1.0, ease: 'power2.out' });
}

function initOrbParallax() {
  const orbEl = document.getElementById('arc-ring');
  if (!orbEl) return;
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  document.addEventListener('mousemove', (e) => {
    const rect = orbEl.getBoundingClientRect();
    const dx = e.clientX - (rect.left + rect.width / 2);
    const dy = e.clientY - (rect.top + rect.height / 2);
    gsap.to(orbEl, {
      x: clamp(dx * 0.05, -14, 14),
      y: clamp(dy * 0.05, -10, 10),
      duration: 0.8, ease: 'power2.out'
    });
  });
}

// ── Init ───────────────────────────────────────────────────────────────────
buildAccountGroups();
buildServiceChips();
initIdleViz();
initTicker();
initGSAP();
initOrbParallax();
connect();

// ── PWA ────────────────────────────────────────────────────────────────────
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/service-worker.js').catch(() => {});
if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission();
