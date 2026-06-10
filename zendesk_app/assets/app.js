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
    'ticket.requester.email', 'ticket.comments',
  ]);
  return {
    ticket_id: String(data['ticket.id']),
    subject: data['ticket.subject'] || '',
    description: data['ticket.description'] || '',
    requester_email: data['ticket.requester.email'] || '',
    comments: (data['ticket.comments'] || []).map(c => c.value).slice(0, 10),
  };
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
    const steps = (data.next_steps || []).map(s => `<li>${escapeHtml(s)}</li>`).join('');
    document.getElementById('next-steps').innerHTML = steps ? `<strong>Next steps:</strong><ul>${steps}</ul>` : '';
    const f = data.facts || {};
    document.getElementById('facts').innerHTML = renderFacts(f);
    loading.hidden = true; content.hidden = false;
  } catch (e) {
    loading.hidden = true; errorEl.hidden = false;
    document.getElementById('briefing-error-detail').textContent = diagnose(e);
  }
}

function renderFacts(f) {
  if (!f || !Object.keys(f).length) return '<span class="muted">No linked LORA claim.</span>';
  const bits = [];
  if (f.status) bits.push(`<span class="pill">${escapeHtml(f.status)}</span>`);
  if (f.deadline) bits.push(`<span class="pill">Deadline ${escapeHtml(f.deadline)}</span>`);
  let html = `<div>${bits.join(' ')}</div>`;
  if (f.emails_total != null) html += `<div>✉️ ${f.emails_total} emails · <b>${f.emails_unresolved || 0} need action</b></div>`;
  if (f.disputes_total != null) html += `<div>💳 ${f.disputes_total} disputes</div>`;
  return html;
}

document.getElementById('briefing-retry').onclick = loadBriefing;

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
  try {
    const ctx = await ticketContext();
    const resp = await loraRequest('/api/integrations/zd/chat/', {
      ticket_id: ctx.ticket_id, message: msg, history: history,
    });
    const data = typeof resp === 'string' ? JSON.parse(resp) : resp;
    appendMsg('ai', data.answer || '(no answer)');
    history.push({ role: 'assistant', content: data.answer || '' });
  } catch (e) {
    appendMsg('ai', 'Sorry — something went wrong reaching LORA. ' + diagnose(e));
  }
};

function appendMsg(role, text) {
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

// init
loadBriefing();
