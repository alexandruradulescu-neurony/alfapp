const client = ZAFClient.init();
let history = [];
let devTokenUsed = null; // true = zcli local token sent; false = secure-setting path

function diagnose(e) {
  const s = e && typeof e.status === 'number' ? e.status : null;
  const mode = devTokenUsed === null ? '' : devTokenUsed ? ' [dev token sent]' : ' [secure mode]';
  if (s === 401 || s === 403) {
    return 'LORA refused the secret (HTTP ' + s + ').' + mode +
      ' Check that sidebar_secret_token in LORA Admin → System Settings matches the app setting exactly.';
  }
  if (s === 429) return 'Too many failed attempts — LORA blocked this caller for ~5 minutes. Fix the secret, wait, then retry.';
  if (s === 404) return 'LORA endpoint not found (HTTP 404) — check lora_base_url.';
  if (s !== null && s >= 500) return 'LORA had an internal error (HTTP ' + s + ').';
  if (s !== null) return 'Unexpected response from LORA (HTTP ' + s + ').' + mode;
  return 'Could not reach LORA at all — network or proxy issue.' + mode;
}

client.invoke('resize', { width: '100%', height: '520px' });

// --- helpers ---
async function loraRequest(path, body) {
  const settings = await client.metadata().then(m => m.settings);
  const opts = {
    url: settings.lora_base_url.replace(/\/$/, '') + path,
    type: 'POST',
    contentType: 'application/json',
    data: JSON.stringify(body),
  };
  if (settings.sidebar_secret_token) {
    // zcli local server: no secure-settings support, so the value typed at the
    // zcli prompt is exposed here — send it directly. Installed apps never
    // expose secure settings to the browser, so this branch is dev-only.
    devTokenUsed = true;
    opts.headers = { Authorization: 'Bearer ' + settings.sidebar_secret_token };
  } else {
    // Installed app: Zendesk's proxy substitutes the secure setting server-side.
    // Requires secure:true and the domain in manifest.json domainWhitelist.
    devTokenUsed = false;
    opts.headers = { Authorization: 'Bearer {{setting.sidebar_secret_token}}' };
    opts.secure = true;
  }
  return client.request(opts);
}

async function ticketContext() {
  const data = await client.get([
    'ticket.id', 'ticket.subject', 'ticket.description',
    'ticket.requester.email', 'ticket.requester.name', 'ticket.createdAt',
  ]);
  const ctx = {
    ticket_id: String(data['ticket.id']),
    subject: data['ticket.subject'] || '',
    description: data['ticket.description'] || '',
    requester_email: data['ticket.requester.email'] || '',
    requester_name: data['ticket.requester.name'] || '',
    ticket_created_at: data['ticket.createdAt'] || '',
    comments: [],
  };
  try {
    // Zendesk REST API (agent session): comments WITH timestamps, authors and
    // the public/internal flag — ZAF's ticket.comments has none of those.
    const resp = await client.request({
      url: '/api/v2/tickets/' + ctx.ticket_id
        + '/comments.json?include=users&sort_order=desc&per_page=30',
      type: 'GET',
    });
    const users = {};
    (resp.users || []).forEach(u => { users[u.id] = u.name; });
    ctx.comments = (resp.comments || []).reverse().map(c => ({
      author: users[c.author_id] || '',
      created_at: c.created_at || '',
      public: c.public !== false,
      text: (c.plain_body || c.body || '').slice(0, 1500),
    }));
  } catch (e) {
    // REST unavailable — fall back to bare comment text.
    const fallback = await client.get(['ticket.comments']);
    ctx.comments = (fallback['ticket.comments'] || []).map(c => c.value).slice(0, 30);
  }
  return ctx;
}

// --- tabs ---
document.querySelectorAll('.tab').forEach(tab => {
  tab.onclick = () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    const which = tab.dataset.tab;
    document.getElementById('panel-briefing').hidden = which !== 'briefing';
    document.getElementById('panel-chat').hidden = which !== 'chat';
    document.getElementById('panel-email').hidden = which !== 'email';
    document.getElementById('panel-updates').hidden = which !== 'updates';
    document.getElementById('panel-formfill').hidden = which !== 'formfill';
    if (which === 'email') loadEmails();
    if (which === 'updates') loadUpdates();
    if (which === 'formfill') ffLoadImageOptions();
  };
});

function timeAgo(iso) {
  const then = new Date(iso).getTime();
  if (!then) return '';
  const mins = Math.round((Date.now() - then) / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return mins + ' min ago';
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return hrs + 'h ago';
  return Math.round(hrs / 24) + 'd ago';
}

// --- briefing ---
// regen=false: read the ONE stored summary (no AI cost, instant, same text the
// LORA app shows). regen=true (Regenerate button): regenerate AND persist, so
// the refresh shows up in the app too.
async function loadBriefing(regen) {
  const loading = document.getElementById('briefing-loading');
  const content = document.getElementById('briefing-content');
  const errorEl = document.getElementById('briefing-error');
  loading.hidden = false; content.hidden = true; errorEl.hidden = true;
  try {
    const ctx = await ticketContext();
    const body = regen ? Object.assign({}, ctx, { refresh: true }) : ctx;
    const resp = await loraRequest('/api/integrations/zd/briefing/', body);
    const data = typeof resp === 'string' ? JSON.parse(resp) : resp;
    const sum = document.getElementById('summary');
    sum.textContent = data.summary || '';
    const note = document.createElement('div');
    note.className = 'muted';
    note.style.marginTop = '4px';
    if (data.summary_updated_at) {
      note.textContent = 'Updated ' + timeAgo(data.summary_updated_at);
      sum.appendChild(note);
    } else if (data.stored === false) {
      note.textContent = 'Live briefing — no linked LORA claim to save against.';
      sum.appendChild(note);
    }
    document.getElementById('next-steps').innerHTML = ''; // generated on demand
    const f = data.facts || {};
    document.getElementById('facts').innerHTML = renderFacts(f);
    document.getElementById('attention').innerHTML = renderAttention(data.attention || []);
    loading.hidden = true; content.hidden = false;
  } catch (e) {
    loading.hidden = true; errorEl.hidden = false;
    document.getElementById('briefing-error-detail').textContent = diagnose(e);
  }
}

function renderFacts(f) {
  if (!f || !Object.keys(f).length) {
    return '<div class="info-line">No linked LORA claim — briefing is based on the ticket only.</div>';
  }
  const bits = [];
  if (f.status) bits.push(`<span class="pill">${escapeHtml(f.status)}</span>`);
  if (f.deadline) {
    const days = Math.ceil((new Date(f.deadline) - new Date()) / 86400000);
    let cls = 'pill';
    let label = 'Deadline ' + f.deadline;
    if (days < 0) { cls += ' overdue'; label += ' (passed)'; }
    else if (days <= 7) { cls += ' due-soon'; label += ` (${days}d left)`; }
    bits.push(`<span class="${cls}">${escapeHtml(label)}</span>`);
  }
  let html = `<div class="row">${bits.join(' ')}</div>`;
  if (f.emails_total != null) html += `<div class="row">✉️ ${f.emails_total} emails · <b>${f.emails_unresolved || 0} need action</b></div>`;
  if (f.disputes_total != null) html += `<div class="row">💳 ${f.disputes_total} disputes</div>`;
  if (f.next_update_due) html += `<div class="row">🗓 Day-${f.next_update_due.day} client update due <b>${escapeHtml(f.next_update_due.date)}</b></div>`;
  return html;
}

function renderAttention(items) {
  if (!items.length) return '';
  const rows = items.map(a =>
    `<li><span class="muted">${escapeHtml(a.date)}</span> — ${escapeHtml(a.subject)}</li>`).join('');
  return `<div class="attention"><strong>⚠️ Needs attention</strong><ul>${rows}</ul></div>`;
}

document.getElementById('briefing-retry').onclick = () => loadBriefing(false);
document.getElementById('btn-regen').onclick = () => loadBriefing(true);

document.getElementById('btn-next-steps').onclick = async () => {
  const btn = document.getElementById('btn-next-steps');
  const target = document.getElementById('next-steps');
  btn.disabled = true;
  target.innerHTML = '<div class="skel" style="width: 70%"></div><div class="skel" style="width: 58%"></div>';
  try {
    const ctx = await ticketContext();
    const resp = await loraRequest('/api/integrations/zd/briefing/',
      Object.assign({}, ctx, { mode: 'next_steps' }));
    const data = typeof resp === 'string' ? JSON.parse(resp) : resp;
    const steps = (data.next_steps || []).map(s => `<li>${escapeHtml(s)}</li>`).join('');
    target.innerHTML = steps
      ? `<strong>Next steps:</strong><ul>${steps}</ul>`
      : '<span class="muted">No pending actions found.</span>';
  } catch (e) {
    target.innerHTML = '<span class="error">' + escapeHtml(diagnose(e)) + '</span>';
  } finally {
    btn.disabled = false;
  }
};

// --- chat suggestion chips ---
document.querySelectorAll('.chip').forEach(ch => {
  ch.onclick = () => {
    const input = document.getElementById('chat-input');
    input.value = ch.dataset.q;
    document.getElementById('chat-form').requestSubmit();
  };
});

// --- chat ---
const chatLog = document.getElementById('chat-log');
document.getElementById('chat-form').onsubmit = async (ev) => {
  ev.preventDefault();
  const input = document.getElementById('chat-input');
  const msg = input.value.trim();
  if (!msg) return;
  appendMsg('user', msg);
  input.value = '';
  history.push({ role: 'user', content: msg });
  const typing = document.getElementById('typing');
  const sendBtn = ev.target.querySelector('button');
  typing.hidden = false;
  sendBtn.disabled = true;
  try {
    const ctx = await ticketContext();
    const resp = await loraRequest('/api/integrations/zd/chat/',
      Object.assign({}, ctx, { message: msg, history: history }));
    const data = typeof resp === 'string' ? JSON.parse(resp) : resp;
    appendMsg('ai', data.answer || '(no answer)');
    history.push({ role: 'assistant', content: data.answer || '' });
  } catch (e) {
    appendMsg('ai', 'Sorry — something went wrong reaching LORA. ' + diagnose(e));
  } finally {
    typing.hidden = true;
    sendBtn.disabled = false;
    input.focus();
  }
};

function appendMsg(role, text) {
  const empty = document.getElementById('chat-empty');
  if (empty) empty.hidden = true;
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  div.textContent = text;
  chatLog.appendChild(div);
  chatLog.scrollTop = chatLog.scrollHeight;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// --- flight lookup ---
async function flightLookup(refresh) {
  const btn = document.getElementById('btn-flight');
  const box = document.getElementById('flight-result');
  btn.disabled = true;
  box.hidden = false;
  box.innerHTML = '<div class="skel" style="width: 60%"></div><div class="skel" style="width: 45%"></div>';
  try {
    const data0 = await client.get(['ticket.id']);
    const body = { ticket_id: String(data0['ticket.id']) };
    if (refresh) body.refresh = true;
    const resp = await loraRequest('/api/integrations/zd/flight-lookup/', body);
    const data = typeof resp === 'string' ? JSON.parse(resp) : resp;
    box.innerHTML = renderFlightResult(data);
    const link = document.getElementById('flight-refresh');
    if (link) link.onclick = () => flightLookup(true);
  } catch (e) {
    const s = e && typeof e.status === 'number' ? e.status : null;
    const server = e && e.responseJSON && (e.responseJSON.error || e.responseJSON.error_message);
    // Auth/lockout statuses: diagnose() explains the secret mismatch far
    // better than the server's bare "Unauthorized".
    const msg = (s === 401 || s === 403 || s === 429) ? diagnose(e) : (server || diagnose(e));
    box.innerHTML = '<span class="error">' + escapeHtml(msg) + '</span>';
  } finally {
    btn.disabled = false;
  }
}

function renderFlightResult(data) {
  if (data.error) return '<span class="error">' + escapeHtml(data.error) + '</span>';
  let html = '';
  const verdict = (data.flight && data.flight.verdict) || data.verdict;
  if (verdict && verdict.label) {
    html += `<div class="fr-chip v-${escapeHtml(verdict.level || '')}">${escapeHtml(verdict.label)}</div>`;
  }
  if (data.flight) {
    const f = data.flight;
    const head = ['✈ ' + (f.number || ''), f.airline ? '— ' + f.airline : '', f.status ? '· ' + f.status : '']
      .filter(Boolean).join(' ');
    html += `<div class="fr-head">${escapeHtml(head)}</div>`;
    (f.legs || []).forEach(l => {
      html += `<div class="fr-route">${escapeHtml(l.from_iata)} (${escapeHtml(l.from_city)}) → ${escapeHtml(l.to_iata)} (${escapeHtml(l.to_city)})</div>`;
      const times = [
        l.scheduled_departure_local ? 'dep ' + l.scheduled_departure_local : '',
        l.scheduled_arrival_local ? 'arr ' + l.scheduled_arrival_local : '',
      ].filter(Boolean).join(' · ');
      if (times) html += `<div class="fr-route muted">${escapeHtml(times)}</div>`;
      const depBits = [l.from_terminal ? 'Terminal ' + l.from_terminal : '', l.from_gate ? 'Gate ' + l.from_gate : ''].filter(Boolean).join(', ');
      const arrBits = [l.to_terminal ? 'Terminal ' + l.to_terminal : '', l.to_gate ? 'Gate ' + l.to_gate : '', l.to_baggage_belt ? 'Belt ' + l.to_baggage_belt : ''].filter(Boolean).join(', ');
      const fac = [depBits ? 'dep ' + depBits : '', arrBits ? 'arr ' + arrBits : ''].filter(Boolean).join(' · ');
      if (fac) html += `<div class="fr-route muted">${escapeHtml(fac)}</div>`;
    });
  } else if (data.error_message) {
    html += `<div class="fr-head">${escapeHtml(data.error_message)}</div>`;
    if (data.candidates && data.candidates.length) {
      html += '<div>Likely candidates:</div><ul>' + data.candidates.map(c =>
        `<li>${escapeHtml(c.number)} → ${escapeHtml(c.destination)}${c.scheduled_local ? ' · dep ' + escapeHtml(c.scheduled_local) : ''}</li>`).join('') + '</ul>';
    }
  }
  if (data.analysis && data.analysis.summary) {
    html += `<div class="fr-ai"><strong>AI check:</strong> ${escapeHtml(data.analysis.summary)}`;
    if (data.analysis.mismatches && data.analysis.mismatches.length) {
      html += '<ul>' + data.analysis.mismatches.map(m => `<li>${escapeHtml(m)}</li>`).join('') + '</ul>';
    }
    html += '</div>';
  }
  if (data.note_posted) html += '<div class="muted" style="margin-top:6px">✓ Posted as internal note on the ticket.</div>';
  if (data.cached) html += '<div style="margin-top:6px"><span class="muted">Saved result.</span> <button id="flight-refresh" class="muted-link" type="button">Refresh from provider</button></div>';
  return html || '<span class="muted">No result.</span>';
}

document.getElementById('btn-flight').onclick = () => flightLookup(false);

// --- email tab: a window onto the SAME stored emails the LORA app shows ---
async function loadEmails() {
  const box = document.getElementById('email-result');
  box.hidden = false;
  box.innerHTML = '<div class="skel" style="width: 60%"></div><div class="skel" style="width: 45%"></div>';
  try {
    const data0 = await client.get(['ticket.id']);
    const resp = await loraRequest('/api/integrations/zd/emails/',
      { ticket_id: String(data0['ticket.id']) });
    const data = typeof resp === 'string' ? JSON.parse(resp) : resp;
    box.innerHTML = renderEmailList(data.emails || []);
  } catch (e) {
    const s = e && typeof e.status === 'number' ? e.status : null;
    box.innerHTML = '<span class="error">' + escapeHtml(diagnose(e)) + '</span>';
  }
}

function renderEmailList(emails) {
  if (!emails.length) {
    return '<div class="muted">No emails recorded for this ticket yet. '
      + 'Use “Check email now” to pull any waiting in the mailbox.</div>';
  }
  return emails.map(em => {
    let html = '<div class="em-card">';
    html += `<div class="em-subject">${escapeHtml(em.subject || '(No Subject)')}</div>`;
    const when = em.received_at ? ' · ' + timeAgo(em.received_at) : '';
    html += `<div class="muted">from ${escapeHtml(em.from_email || 'unknown')}${when}</div>`;
    const cat = (em.category || '').toString();
    html += `<div class="em-chips"><span class="em-chip">${escapeHtml(cat)}</span>`;
    if (em.action_required) html += '<span class="em-chip c-attention">needs action</span>';
    else if (em.auto_resolved) html += '<span class="em-chip c-OBJECT_NOT_FOUND">auto-resolved</span>';
    html += '</div>';
    if (em.summary) html += `<div class="em-summary">${escapeHtml(em.summary)}</div>`;
    return html + '</div>';
  }).join('');
}

async function checkEmail() {
  const btn = document.getElementById('btn-check-email');
  const box = document.getElementById('email-result');
  btn.disabled = true;
  box.hidden = false;
  box.innerHTML = '<div class="skel" style="width: 60%"></div><div class="skel" style="width: 45%"></div>';
  try {
    const data0 = await client.get(['ticket.id']);
    const resp = await loraRequest('/api/integrations/zd/email-check/',
      { ticket_id: String(data0['ticket.id']) });
    const data = typeof resp === 'string' ? JSON.parse(resp) : resp;
    if (data.error || data.error_message) {
      box.innerHTML = '<div class="em-head">' + escapeHtml(data.error || data.error_message) + '</div>';
      return;
    }
    // After checking, show the up-to-date stored list (single source).
    await loadEmails();
    const n = (data.processed || []).length;
    if (n) {
      const head = document.createElement('div');
      head.className = 'em-head'; head.style.marginBottom = '6px';
      head.textContent = `${n} new email(s) pulled.`;
      box.insertBefore(head, box.firstChild);
    }
  } catch (e) {
    const s = e && typeof e.status === 'number' ? e.status : null;
    const server = e && e.responseJSON && (e.responseJSON.error || e.responseJSON.error_message);
    const msg = (s === 401 || s === 403 || s === 429) ? diagnose(e) : (server || diagnose(e));
    box.innerHTML = '<span class="error">' + escapeHtml(msg) + '</span>';
  } finally {
    btn.disabled = false;
  }
}

document.getElementById('btn-check-email').onclick = checkEmail;

// --- updates (client progress updates: initial + day-2/5/11/21 follow-ups) ---
const upList = document.getElementById('updates-list');
const upLoading = document.getElementById('updates-loading');
const upEmpty = document.getElementById('updates-empty');
const upError = document.getElementById('updates-error');
document.getElementById('updates-retry').onclick = loadUpdates;

function upBadge(it) {
  if (it.state === 'sent') return '✓ Sent' + (it.sent_at ? ' · ' + timeAgo(it.sent_at) : '');
  if (it.state === 'drafted') return 'Draft ready' + (it.has_news === false ? ' · no news yet' : '');
  if (it.state === 'skipped') return 'Skipped';
  if (it.is_due) return 'Due now';
  return '';
}

function upFmtDate(iso) { const d = new Date(iso); return isNaN(d) ? '' : d.toLocaleDateString(); }

function renderUpdates(data) {
  upLoading.hidden = true;
  if (!data || !data.claim) {
    upList.hidden = true; upEmpty.hidden = false; upError.hidden = true; return;
  }
  upEmpty.hidden = true; upError.hidden = true; upList.hidden = false;
  if (!(data.items || []).length) {
    // Linked claim but nothing scheduled yet (e.g. an older claim) — offer to start.
    upList.innerHTML =
      (data.message ? `<div class="info-line" style="margin-bottom:8px">${escapeHtml(data.message)}</div>` : '')
      + '<div class="muted" style="margin-bottom:8px">No client updates have been started for this claim.</div>'
      + '<div class="actions"><button type="button" data-action="start">Start client updates</button></div>';
    return;
  }
  let html = '';
  if (data.message) html += `<div class="info-line" style="margin-bottom:8px">${escapeHtml(data.message)}</div>`;
  (data.items || []).forEach(it => {
    const id = it.kind === 'followup' ? it.id : 'initial';
    html += '<div class="up-card" style="border:1px solid #e5e7eb;border-radius:10px;padding:10px;margin-bottom:10px">';
    html += `<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:6px">`
      + `<b>${escapeHtml(it.label)}</b><span class="muted" style="font-size:11px">${escapeHtml(upBadge(it))}</span></div>`;
    if (it.state === 'sent') {
      html += `<div class="muted" style="white-space:pre-wrap;font-size:12px">${escapeHtml(it.body || '')}</div>`;
    } else if (it.state === 'skipped') {
      html += '<div class="muted" style="font-size:12px">This update was skipped.</div>';
    } else if (it.state === 'drafted') {
      html += `<textarea class="up-body" data-id="${id}" rows="7" style="width:100%;box-sizing:border-box;font-size:12px">${escapeHtml(it.body || '')}</textarea>`;
      html += '<div class="actions" style="margin-top:6px">';
      html += `<button type="button" data-action="send" data-kind="${it.kind}" data-id="${id}"${it.can_send ? '' : ' disabled'}>Send to client</button>`;
      html += `<button type="button" data-action="prepare" data-kind="${it.kind}" data-id="${id}">Regenerate</button>`;
      if (it.kind === 'followup') html += `<button type="button" data-action="skip" data-kind="followup" data-id="${id}">Skip</button>`;
      html += '</div>';
    } else { // scheduled
      html += it.is_due
        ? `<div class="actions"><button type="button" data-action="prepare" data-kind="followup" data-id="${id}">Prepare update</button></div>`
        : `<div class="muted" style="font-size:12px">Scheduled for ${escapeHtml(upFmtDate(it.due_at))}.</div>`;
    }
    html += '</div>';
  });
  upList.innerHTML = html;
}

async function loadUpdates() {
  upLoading.hidden = false; upList.hidden = true; upEmpty.hidden = true; upError.hidden = true;
  try {
    const ctx = await ticketContext();
    renderUpdates(await loraRequest('/api/integrations/zd/updates/', { ticket_id: ctx.ticket_id }));
  } catch (e) {
    upLoading.hidden = true; upError.hidden = false;
    document.getElementById('updates-error-detail').textContent = diagnose(e);
  }
}

upList.addEventListener('click', async ev => {
  const btn = ev.target.closest('button[data-action]');
  if (!btn) return;
  const action = btn.dataset.action, kind = btn.dataset.kind, id = btn.dataset.id;
  let body = '';
  if (action === 'send') {
    const ta = upList.querySelector('textarea.up-body[data-id="' + id + '"]');
    body = ta ? ta.value : '';
  }
  btn.disabled = true;
  try {
    const ctx = await ticketContext();
    const payload = { ticket_id: ctx.ticket_id, action: action, kind: kind };
    if (kind === 'followup') payload.id = Number(id);
    if (action === 'send') payload.body = body;
    renderUpdates(await loraRequest('/api/integrations/zd/updates/', payload));
  } catch (e) {
    btn.disabled = false; upError.hidden = false;
    document.getElementById('updates-error-detail').textContent = diagnose(e);
  }
});

// --- form filling (Browser Use) ---
let ffSession = null, ffLiveUrl = null, ffPoll = null;

async function ffLoadImageOptions() {
  const box = document.getElementById('ff-image');
  box.innerHTML = 'Image (optional): <input id="ff-file" type="file" accept="image/*">';
  try {
    const d0 = await client.get(['ticket.id']);
    const resp = await loraRequest('/api/integrations/zd/form-fill/attachments/',
      { ticket_id: String(d0['ticket.id']) });
    const data = typeof resp === 'string' ? JSON.parse(resp) : resp;
    const atts = (data.attachments || []);
    if (atts.length) {
      const opts = atts.map(a =>
        `<option value="${escapeHtml(a.url)}" data-name="${escapeHtml(a.filename)}">${escapeHtml(a.filename)}</option>`).join('');
      box.innerHTML =
        'Image (optional): <select id="ff-att"><option value="">— from ticket —</option>' + opts + '</select>'
        + ' or upload <input id="ff-file" type="file" accept="image/*">';
    }
  } catch (e) { /* leave the plain file input */ }
}

// Multipart upload can't go through loraRequest (JSON). Build the ZAF request directly.
async function ffUpload(formData) {
  const settings = await client.metadata().then(m => m.settings);
  const opts = {
    url: settings.lora_base_url.replace(/\/$/, '') + '/api/integrations/zd/form-fill/upload/',
    type: 'POST', data: formData, processData: false, contentType: false,
  };
  if (settings.sidebar_secret_token) {
    opts.headers = { Authorization: 'Bearer ' + settings.sidebar_secret_token };
  } else {
    opts.headers = { Authorization: 'Bearer {{setting.sidebar_secret_token}}' };
    opts.secure = true;
  }
  const resp = await client.request(opts);
  return typeof resp === 'string' ? JSON.parse(resp) : resp;
}

async function ffStartFill() {
  const url = document.getElementById('ff-url').value.trim();
  const statusEl = document.getElementById('ff-status');
  if (!url) { statusEl.textContent = 'Paste the form URL first.'; return; }
  const btn = document.getElementById('ff-fill');
  btn.disabled = true; statusEl.textContent = 'Starting…';
  document.getElementById('ff-shot').innerHTML = '';
  try {
    const d0 = await client.get(['ticket.id']);
    const ticketId = String(d0['ticket.id']);
    const body = { ticket_id: ticketId, url: url,
                   post_screenshot: document.getElementById('ff-post').checked };
    // Image source A: a picked ticket attachment.
    const att = document.getElementById('ff-att');
    if (att && att.value) {
      body.image_url = att.value;
      const opt = att.options[att.selectedIndex];
      body.image_filename = opt ? opt.getAttribute('data-name') : 'attachment';
    }
    // Image source B: an agent-uploaded file → upload first, then pass the form_fill_id.
    const file = document.getElementById('ff-file');
    if (!body.image_url && file && file.files && file.files[0]) {
      statusEl.textContent = 'Uploading image…';
      const fd = new FormData();
      fd.append('ticket_id', ticketId);
      fd.append('image', file.files[0]);
      const up = await ffUpload(fd);
      if (up && up.form_fill_id) body.form_fill_id = up.form_fill_id;
    }
    statusEl.textContent = 'Filling the form…';
    const resp = await loraRequest('/api/integrations/zd/form-fill/start/', body);
    const data = typeof resp === 'string' ? JSON.parse(resp) : resp;
    if (data.error) { statusEl.textContent = data.error; btn.disabled = false; return; }
    ffSession = data.session_id; ffLiveUrl = data.live_url;
    statusEl.textContent = 'Filling the form… open the live view to watch.';
    document.getElementById('ff-actions').hidden = false;
    if (ffPoll) clearInterval(ffPoll);
    ffPoll = setInterval(ffCheck, 4000);
  } catch (e) {
    statusEl.innerHTML = '<span class="error">' + escapeHtml(diagnose(e)) + '</span>';
    btn.disabled = false;
  }
}

async function ffCheck() {
  if (!ffSession) return;
  try {
    const resp = await loraRequest('/api/integrations/zd/form-fill/status/', { session_id: ffSession });
    const data = typeof resp === 'string' ? JSON.parse(resp) : resp;
    if (data.screenshot) {
      document.getElementById('ff-shot').innerHTML =
        '<img src="' + data.screenshot + '" alt="filled form" style="width:100%;border:1px solid #e5e7eb;border-radius:8px">';
    }
    if (data.status === 'FILLED') {
      if (ffPoll) { clearInterval(ffPoll); ffPoll = null; }
      document.getElementById('ff-status').textContent = 'Filled — review, then Approve & submit.';
    } else if (data.status === 'FAILED') {
      if (ffPoll) { clearInterval(ffPoll); ffPoll = null; }
      document.getElementById('ff-status').textContent = 'The fill did not complete — open the live view to take over.';
    }
  } catch (e) { /* keep polling */ }
}

async function ffApprove() {
  if (!ffSession) return;
  const statusEl = document.getElementById('ff-status');
  document.getElementById('ff-approve').disabled = true;
  statusEl.textContent = 'Submitting…';
  try {
    const d0 = await client.get(['ticket.id']);
    const resp = await loraRequest('/api/integrations/zd/form-fill/submit/',
      { session_id: ffSession, ticket_id: String(d0['ticket.id']),
        post_screenshot: document.getElementById('ff-post').checked });
    const data = typeof resp === 'string' ? JSON.parse(resp) : resp;
    if (data.screenshot) {
      document.getElementById('ff-shot').innerHTML =
        '<img src="' + data.screenshot + '" alt="confirmation" style="width:100%;border:1px solid #e5e7eb;border-radius:8px">';
    }
    statusEl.textContent = data.error ? data.error : '✓ Submitted.';
    document.getElementById('ff-actions').hidden = true;
    document.getElementById('ff-fill').disabled = false;
    ffSession = null;
  } catch (e) {
    statusEl.innerHTML = '<span class="error">' + escapeHtml(diagnose(e)) + '</span>';
    document.getElementById('ff-approve').disabled = false;
  }
}

async function ffCancel() {
  if (ffPoll) { clearInterval(ffPoll); ffPoll = null; }
  if (ffSession) {
    try { await loraRequest('/api/integrations/zd/form-fill/cancel/', { session_id: ffSession }); } catch (e) {}
  }
  ffSession = null;
  document.getElementById('ff-actions').hidden = true;
  document.getElementById('ff-fill').disabled = false;
  document.getElementById('ff-status').textContent = 'Cancelled.';
}

document.getElementById('ff-fill').onclick = ffStartFill;
document.getElementById('ff-approve').onclick = ffApprove;
document.getElementById('ff-cancel').onclick = ffCancel;
document.getElementById('ff-live').onclick = () => { if (ffLiveUrl) window.open(ffLiveUrl, '_blank'); };

// init
loadBriefing(false);
