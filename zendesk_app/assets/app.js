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
  };
});

// --- briefing ---
async function loadBriefing() {
  const loading = document.getElementById('briefing-loading');
  const content = document.getElementById('briefing-content');
  const errorEl = document.getElementById('briefing-error');
  loading.hidden = false; content.hidden = true; errorEl.hidden = true;
  try {
    const ctx = await ticketContext();
    const resp = await loraRequest('/api/integrations/zd/briefing/', ctx);
    const data = typeof resp === 'string' ? JSON.parse(resp) : resp;
    document.getElementById('summary').textContent = data.summary || '';
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

document.getElementById('briefing-retry').onclick = loadBriefing;
document.getElementById('btn-regen').onclick = loadBriefing;

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

// --- email drafts ---
function nl2brEscaped(text) {
  return escapeHtml(text).replace(/\n/g, '<br>');
}

async function draftEmail(draftType, btn) {
  const statusEl = document.getElementById('draft-status');
  btn.disabled = true;
  statusEl.className = '';
  statusEl.textContent = 'Writing draft…';
  try {
    const ctx = await ticketContext();
    const resp = await loraRequest('/api/integrations/zd/draft/',
      Object.assign({}, ctx, { draft_type: draftType }));
    const data = typeof resp === 'string' ? JSON.parse(resp) : resp;
    if (data.body) {
      // Insert into the ticket's reply composer — the agent reviews and sends.
      await client.invoke('ticket.editor.insert', nl2brEscaped(data.body));
      statusEl.className = 'ok';
      statusEl.textContent = '✓ Draft inserted in the reply box — review and edit before sending.';
    } else {
      statusEl.className = 'err';
      statusEl.textContent = 'Draft unavailable right now — try again.';
    }
  } catch (e) {
    statusEl.className = 'err';
    statusEl.textContent = diagnose(e);
  } finally {
    btn.disabled = false;
  }
}

document.getElementById('btn-draft-client').onclick = (e) => draftEmail('client_update', e.currentTarget);
document.getElementById('btn-draft-inst').onclick = (e) => draftEmail('institution_reply', e.currentTarget);

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
    const server = e && e.responseJSON && (e.responseJSON.error || e.responseJSON.error_message);
    box.innerHTML = '<span class="error">' + escapeHtml(server || diagnose(e)) + '</span>';
  } finally {
    btn.disabled = false;
  }
}

function renderFlightResult(data) {
  if (data.error) return '<span class="error">' + escapeHtml(data.error) + '</span>';
  let html = '';
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

// init
loadBriefing();
