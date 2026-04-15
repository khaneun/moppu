/* ============================================================
   Moppu Monitor — Frontend Logic
   ============================================================ */

// ------------------------------------------------------------------ //
// Auth
// ------------------------------------------------------------------ //

let _token = sessionStorage.getItem('moppu_token') || '';

const API = {
  get: async (url) => {
    const r = await fetch(url, { headers: _authHeader() });
    if (r.status === 401) { showLogin(); throw new Error('인증 필요'); }
    if (!r.ok) { let d = ''; try { d = (await r.json()).detail; } catch(_){} throw new Error(d || `HTTP ${r.status}`); }
    return r.json();
  },
  post: async (url, body) => {
    const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', ..._authHeader() }, body: JSON.stringify(body) });
    if (r.status === 401) { showLogin(); throw new Error('인증 필요'); }
    if (!r.ok) { let d = ''; try { d = (await r.json()).detail; } catch(_){} throw new Error(d || `HTTP ${r.status}`); }
    return r.json();
  },
  put: async (url, body) => {
    const r = await fetch(url, { method: 'PUT', headers: { 'Content-Type': 'application/json', ..._authHeader() }, body: JSON.stringify(body) });
    if (r.status === 401) { showLogin(); throw new Error('인증 필요'); }
    if (!r.ok) { let d = ''; try { d = (await r.json()).detail; } catch(_){} throw new Error(d || `HTTP ${r.status}`); }
    return r.json();
  },
  del: async (url) => {
    const r = await fetch(url, { method: 'DELETE', headers: _authHeader() });
    if (r.status === 401) { showLogin(); throw new Error('인증 필요'); }
    if (!r.ok) { let d = ''; try { d = (await r.json()).detail; } catch(_){} throw new Error(d || `HTTP ${r.status}`); }
    return r.json();
  },
};

function _authHeader() { return _token ? { 'Authorization': `Bearer ${_token}` } : {}; }

function showLogin() {
  sessionStorage.removeItem('moppu_token');
  _token = '';
  document.getElementById('login-overlay').style.display = 'flex';
}

function hideLogin() {
  document.getElementById('login-overlay').style.display = 'none';
}

document.getElementById('btn-login').addEventListener('click', doLogin);
document.getElementById('login-pw').addEventListener('keydown', (e) => { if (e.key === 'Enter') doLogin(); });

async function doLogin() {
  const id = document.getElementById('login-id').value.trim();
  const pw = document.getElementById('login-pw').value;
  const errEl = document.getElementById('login-error');
  errEl.textContent = '';
  try {
    const data = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, password: pw }),
    }).then(r => { if (!r.ok) throw new Error(); return r.json(); });
    _token = data.token;
    sessionStorage.setItem('moppu_token', _token);
    hideLogin();
    initApp();
  } catch (_) {
    errEl.textContent = '아이디 또는 비밀번호가 올바르지 않습니다.';
  }
}

// ------------------------------------------------------------------ //
// Helpers
// ------------------------------------------------------------------ //

function krw(n) { return n == null ? '-' : Math.round(n).toLocaleString('ko-KR') + '원'; }
function usd(n) { return n == null ? '-' : '$' + n.toFixed(4); }
function escHtml(s) { const d = document.createElement('div'); d.textContent = String(s || ''); return d.innerHTML; }
function trunc(s, n) { if (!s) return ''; return s.length > n ? s.slice(0, n) + '…' : s; }
function mdRender(text) { return marked.parse(text || '', { breaks: true }); }

function formatKoreanDateTime(isoStr) {
  if (!isoStr) return null;
  // DB의 published_at는 UTC(tzinfo 없음)로 저장되므로 Z를 붙여 UTC로 해석
  const s = isoStr.endsWith('Z') || isoStr.includes('+') ? isoStr : isoStr + 'Z';
  try {
    return new Date(s).toLocaleString('ko-KR', {
      timeZone: 'Asia/Seoul',
      year: 'numeric', month: 'long', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    });
  } catch (_) { return isoStr; }
}

// ------------------------------------------------------------------ //
// Modals
// ------------------------------------------------------------------ //

function closeModal(id) { document.getElementById(id).style.display = 'none'; }

document.getElementById('summary-modal').addEventListener('click', (e) => {
  if (e.target === document.getElementById('summary-modal')) closeModal('summary-modal');
});
document.getElementById('btn-modal-close').addEventListener('click', () => closeModal('summary-modal'));
document.getElementById('prompt-modal').addEventListener('click', (e) => {
  if (e.target === document.getElementById('prompt-modal')) closeModal('prompt-modal');
});
document.getElementById('btn-prompt-close').addEventListener('click', () => closeModal('prompt-modal'));

// ------------------------------------------------------------------ //
// Tabs
// ------------------------------------------------------------------ //

document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
    if (btn.dataset.tab === 'overview') loadOverview();
    if (btn.dataset.tab === 'agent')    { loadPipeline(); loadSuggestedQuestions(); }
    if (btn.dataset.tab === 'settings') { loadSettings(); loadCost(); }
  });
});

// ================================================================== //
// Overview Tab
// ================================================================== //

async function loadOverview() {
  try {
    const data = await API.get('/api/overview');
    document.getElementById('cash-balance').textContent = krw(data.cash_balance_krw);
    document.getElementById('total-asset').textContent  = krw(data.total_eval_krw);
    document.getElementById('ov-kis-mode').textContent  = data.kis_mode === 'real' ? '실전' : '모의';
    document.getElementById('ov-dry-run').textContent   = data.dry_run ? 'ON' : 'OFF';
    updateGlobalStatus(data.emergency_stopped, data.kis_mode);

    const errEl = document.getElementById('broker-error');
    if (data.broker_error) { errEl.textContent = data.broker_error; errEl.style.display = 'block'; }
    else errEl.style.display = 'none';

    const tbody = document.getElementById('positions-body');
    if (!data.positions || !data.positions.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="text-muted">보유 종목 없음</td></tr>';
    } else {
      tbody.innerHTML = data.positions.map(p => {
        const cls = p.unrealized_pl >= 0 ? 'pl-positive' : 'pl-negative';
        const sign = p.unrealized_pl >= 0 ? '+' : '';
        return `<tr>
          <td>${escHtml(p.ticker)}</td><td>${p.quantity}</td>
          <td>${krw(p.avg_price)}</td><td>${krw(p.eval_amount)}</td>
          <td class="${cls}">${sign}${krw(p.unrealized_pl)}</td>
          <td class="${cls}">${sign}${p.pl_rate}%</td>
        </tr>`;
      }).join('');
    }
  } catch (e) { if (e.message !== '인증 필요') console.error('loadOverview', e); }

  loadSummaryLabel();
}

// ---- Summary label ----

let _summaryData = null;

async function loadSummaryLabel() {
  const labelEl = document.getElementById('ingestion-summary-label');
  try {
    const data = await API.get('/api/agent/summary');
    _summaryData = data;
    const today = data.date || new Date().toISOString().slice(0, 10);

    const cnt = data.videos ? data.videos.length : 0;
    if (!cnt) {
      labelEl.innerHTML = `<span class="label-none">${escHtml(today)} 영상 없음</span>`;
      labelEl.style.cursor = 'default';
      labelEl.onclick = null;
    } else if (data.summary) {
      labelEl.innerHTML = `<span class="label-text">📋 ${escHtml(today)} 영상 요약본(${cnt}건)</span>`;
      labelEl.onclick = openSummaryModal;
    } else {
      labelEl.innerHTML = `<span class="label-none">${escHtml(today)} 수집 ${cnt}건 (요약 미생성)</span>`;
      labelEl.onclick = openSummaryModal;
    }
  } catch (e) {
    if (e.message !== '인증 필요') labelEl.innerHTML = '<span class="label-none">데이터 로드 실패</span>';
  }
}

// ---- Summary modal ----

const YT_SVG = `<svg class="yt-icon" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M23.5 6.19a3.02 3.02 0 0 0-2.12-2.14C19.54 3.5 12 3.5 12 3.5s-7.54 0-9.38.55A3.02 3.02 0 0 0 .5 6.19C0 8.04 0 12 0 12s0 3.96.5 5.81a3.02 3.02 0 0 0 2.12 2.14C4.46 20.5 12 20.5 12 20.5s7.54 0 9.38-.55a3.02 3.02 0 0 0 2.12-2.14C24 15.96 24 12 24 12s0-3.96-.5-5.81zM9.75 15.02V8.98L15.5 12l-5.75 3.02z"/></svg>`;

function renderModalSummary(data) {
  document.getElementById('modal-date-label').textContent = data.date || '';
  const textEl = document.getElementById('modal-summary-text');
  const sourcesEl = document.getElementById('modal-sources');

  // 모달 타이틀 변경
  const titleEl = textEl.closest('.modal')?.querySelector('.modal-header h3');
  if (titleEl) titleEl.textContent = '영상 요약본';

  if (!data.summary) {
    textEl.innerHTML = '<p class="text-muted">오늘 수집된 영상이 없습니다.</p>';
    sourcesEl.innerHTML = '';
    return;
  }
  textEl.className = 'modal-summary-content md-content';
  textEl.innerHTML = mdRender(data.summary);

  if (data.videos && data.videos.length) {
    sourcesEl.innerHTML = `<h4>수집 영상 목록</h4>` + data.videos.map(v => {
      const url = v.url || `https://www.youtube.com/watch?v=${v.video_id}`;
      const shortTitle = trunc(v.title || v.video_id, 20);
      const uploadDate = formatKoreanDateTime(v.published_at);
      const dateStr = uploadDate
        ? `<span style="color:var(--text-muted);font-size:.7rem;display:block;margin-top:2px;">업로드 일자 : ${escHtml(uploadDate)}</span>`
        : '';
      return `<div class="source-item" style="flex-direction:column;align-items:flex-start;">
        <div style="display:flex;align-items:center;gap:6px;">${YT_SVG}<a href="${escHtml(url)}" target="_blank" class="source-link" title="${escHtml(v.title || '')}">${escHtml(shortTitle)}</a></div>
        ${dateStr}
      </div>`;
    }).join('');
  } else { sourcesEl.innerHTML = ''; }
}

async function openSummaryModal() {
  const modal = document.getElementById('summary-modal');
  const textEl = document.getElementById('modal-summary-text');
  const sourcesEl = document.getElementById('modal-sources');
  modal.style.display = 'flex';

  if (_summaryData && _summaryData.summary) {
    renderModalSummary(_summaryData);
    return;
  }
  textEl.innerHTML = '<div class="modal-loading">요약 생성 중</div>';
  sourcesEl.innerHTML = '';
  try {
    const data = await API.post('/api/agent/generate-summary', {});
    _summaryData = { ..._summaryData, ...data };
    renderModalSummary(data);
    // 레이블 갱신
    if (data.summary) {
      const today = data.date || '';
      document.getElementById('ingestion-summary-label').innerHTML = `<span class="label-text">📋 ${escHtml(today)} 영상 요약본</span>`;
    }
  } catch (e) { textEl.textContent = e.message || '요약 생성 실패'; }
}

// ================================================================== //
// Agent Tab
// ================================================================== //

// ---- Prompt modal ----

document.getElementById('btn-view-prompt').addEventListener('click', async () => {
  const modal = document.getElementById('prompt-modal');
  const content = document.getElementById('prompt-modal-content');
  modal.style.display = 'flex';
  content.innerHTML = '<div class="modal-loading">로딩 중</div>';
  try {
    const data = await API.get('/api/agent/prompt');
    content.className = 'modal-summary-content md-content';
    content.innerHTML = mdRender(data.system_prompt);
  } catch (e) { content.textContent = e.message; }
});

// ---- Suggested questions ----

async function loadSuggestedQuestions() {
  if (chatHistory.length > 0) return;  // 대화 있으면 표시 안 함
  const el = document.getElementById('suggested-questions');
  el.innerHTML = '<span class="text-muted" style="font-size:.75rem;">질문 추천 로딩 중...</span>';
  try {
    const data = await API.get('/api/agent/suggested-questions');
    el.innerHTML = (data.questions || []).map(q =>
      `<button class="sq-btn" onclick="submitSuggestedQ(this)">${escHtml(q)}</button>`
    ).join('');
  } catch (_) { el.innerHTML = ''; }
}

function submitSuggestedQ(btn) {
  document.getElementById('chat-input').value = btn.textContent;
  document.getElementById('chat-form').dispatchEvent(new Event('submit'));
}

// ---- Chat ----

let chatHistory = [];

document.getElementById('chat-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = document.getElementById('chat-input');
  const msg = input.value.trim();
  if (!msg) return;

  // 추천 질문 숨기기
  document.getElementById('suggested-questions').innerHTML = '';

  appendChatMsg('user', msg);
  input.value = '';
  document.getElementById('btn-send').disabled = true;

  const loadingEl = document.createElement('div');
  loadingEl.className = 'msg-loading';
  loadingEl.textContent = '생각하는 중';
  document.getElementById('chat-messages').appendChild(loadingEl);
  scrollChat();

  try {
    const data = await API.post('/api/agent/chat', { message: msg, history: chatHistory });
    loadingEl.remove();
    appendAgentMsg(data.text, data.citations || []);
    chatHistory.push({ role: 'user', content: msg });
    chatHistory.push({ role: 'assistant', content: data.text });
    if (chatHistory.length > 20) chatHistory = chatHistory.slice(-20);
  } catch (err) {
    loadingEl.remove();
    appendChatMsg('agent', err.message || 'Agent 응답 실패');
  }
  document.getElementById('btn-send').disabled = false;
});

function appendChatMsg(role, text) {
  const el = document.createElement('div');
  el.className = role === 'user' ? 'msg msg-user' : 'msg msg-agent';
  // user: plain text with wrap / agent: markdown
  el.innerHTML = role === 'user'
    ? escHtml(text)
    : `<div class="md-content">${mdRender(text)}</div>`;
  document.getElementById('chat-messages').appendChild(el);
  scrollChat();
}

function appendAgentMsg(text, citations) {
  const el = document.createElement('div');
  el.className = 'msg msg-agent';
  let html = `<div class="md-content">${mdRender(text)}</div>`;
  if (citations.length) {
    html += '<div class="msg-citations">' + citations.map(c => {
      const label = trunc(c.title || c.video_id, 20);
      return `<a href="${escHtml(c.url)}" target="_blank" class="citation-link" title="${escHtml(c.title || '')}">${escHtml(label)}</a>`;
    }).join('') + '</div>';
  }
  el.innerHTML = html;
  document.getElementById('chat-messages').appendChild(el);
  scrollChat();
}

function scrollChat() {
  const el = document.getElementById('chat-messages');
  el.scrollTop = el.scrollHeight;
}

// ---- Pipeline ----

let _pipelinePolling = null;

async function loadPipeline() {
  try {
    const [data, chData] = await Promise.all([
      API.get('/api/pipeline/status'),
      API.get('/api/channels'),
    ]);

    // 실행 상태 표시
    const runStatusEl = document.getElementById('pipeline-run-status');
    const runMsgEl    = document.getElementById('pipeline-run-msg');
    if (data.pipeline_running) {
      // EC2 직접 실행 중
      runStatusEl.style.display = 'flex';
      runStatusEl.className = 'run-status';
      runMsgEl.innerHTML = `<div class="spinner" style="display:inline-block;margin-right:6px;"></div>${escHtml(data.pipeline_run_msg || '실행 중...')}`;
      if (!_pipelinePolling) _pipelinePolling = setInterval(loadPipeline, 2000);
    } else if (data.pipeline_run_msg) {
      const isWaiting = data.pipeline_run_msg.includes('대기 중');
      const isErr     = data.pipeline_run_msg.startsWith('오류');
      runStatusEl.style.display = 'flex';
      runStatusEl.className = 'run-status ' + (isErr ? 'error' : (isWaiting ? '' : 'done'));
      runMsgEl.innerHTML = isWaiting
        ? `<div class="spinner" style="display:inline-block;margin-right:6px;"></div>${escHtml(data.pipeline_run_msg)}`
        : escHtml(data.pipeline_run_msg);

      if (isWaiting) {
        // 로컬 수집기 완료 신호 대기 — 5초마다 계속 폴링
        if (!_pipelinePolling) _pipelinePolling = setInterval(loadPipeline, 5000);
      } else {
        // 완료 신호 수신됨 — 폴링 중단
        if (_pipelinePolling) { clearInterval(_pipelinePolling); _pipelinePolling = null; }
      }
    } else {
      if (_pipelinePolling) { clearInterval(_pipelinePolling); _pipelinePolling = null; }
      runStatusEl.style.display = 'none';
    }

    // 채널 서브카드
    document.getElementById('sc-channels').textContent =
      `${data.channels.enabled} / ${data.channels.total}`;
    const chanList = document.getElementById('channel-list');
    chanList.innerHTML = chData.length
      ? chData.map(c => `
          <div class="channel-item">
            <span class="ch-dot ${c.enabled ? 'enabled' : 'disabled'}"></span>
            <span style="flex:1;font-size:.78rem;">${escHtml(trunc(c.name || c.handle || c.channel_id, 18))}</span>
          </div>`).join('')
      : '<p class="text-muted" style="font-size:.75rem;">채널 없음</p>';

    // 영상 서브카드
    document.getElementById('sc-videos').textContent = `${data.videos.total}건`;

    // 임베딩 서브카드 (삭제 카운트 포함)
    const total = data.videos.total || 0;
    const embedded = data.videos.embedded || 0;
    const failed = data.videos.failed || 0;
    const delEmb = data.deleted_embeddings || 0;
    const pct = total > 0 ? Math.round(embedded / total * 100) : 0;
    document.getElementById('sc-embedding').innerHTML = `
      <div class="sub-card-stat">${embedded}<span style="font-size:.75rem;color:var(--text-muted);font-weight:400;"> / ${total}</span></div>
      <div class="embed-bar-wrap">
        <div class="embed-bar"><div class="embed-fill ${failed > 0 ? 'has-failed' : ''}" style="width:${pct}%"></div></div>
        <div class="embed-meta">${pct}% 완료${failed > 0 ? ` · 실패 ${failed}건` : ''}${delEmb > 0 ? ` · 삭제됨 ${delEmb}건` : ''}</div>
      </div>`;
  } catch (e) { if (e.message !== '인증 필요') console.error('loadPipeline', e); }
}

// ---- Pipeline refresh ----

document.getElementById('btn-pipeline-refresh').addEventListener('click', () => loadPipeline());

// 로그 파일 최신 줄로 상태 메시지 업데이트 (로컬 수집 완료 등 반영)
async function _updateStatusFromLog(msgEl, fallback) {
  try {
    const data = await API.get('/api/pipeline/log');
    const lines = (data.lines || []).filter(l => l.trim());
    const latest = lines[lines.length - 1] || fallback;
    // 타임스탬프 제거 후 표시
    msgEl.textContent = latest.replace(/^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\s*/, '');
  } catch (_) {
    msgEl.textContent = fallback;
  }
}

// ---- Pipeline run / log ----

document.getElementById('btn-pipeline-run').addEventListener('click', async () => {
  if (!confirm('파이프라인을 실행합니다.\n로컬 수집기(감시 모드 실행 중)에도 신호를 전송합니다.\n\n계속하시겠습니까?')) return;
  const btn = document.getElementById('btn-pipeline-run');
  btn.disabled = true;
  try {
    // 로컬 수집기 트리거 (watch 모드 실행 중이면 즉시 반응)
    API.post('/api/collect/request-run', {}).catch(() => {});
    // EC2 측 동기화 (video_list_entries 갱신)
    await API.post('/api/pipeline/run', {});
    loadPipeline();
  } catch (e) {
    alert(e.message || '실행 실패');
  }
  btn.disabled = false;
});

async function loadPipelineLog() {
  const body = document.getElementById('log-modal-body');
  body.innerHTML = '<p class="text-muted">로딩 중...</p>';
  try {
    const data = await API.get('/api/pipeline/log');
    if (!data.exists || !data.lines || !data.lines.length) {
      body.innerHTML = '<div class="log-empty">실행 로그가 없습니다.<br>파이프라인을 실행하면 로그가 기록됩니다.</div>';
      return;
    }
    // 최신 로그가 아래에 오도록 역순으로 표시
    const content = data.lines.join('\n');
    body.innerHTML = `<pre class="log-content">${escHtml(content)}</pre>`;
    // 스크롤을 맨 아래로
    const pre = body.querySelector('.log-content');
    if (pre) pre.scrollTop = pre.scrollHeight;
  } catch (e) { body.innerHTML = `<p class="text-warn">${e.message}</p>`; }
}

document.getElementById('btn-pipeline-log').addEventListener('click', () => {
  document.getElementById('log-modal').style.display = 'flex';
  loadPipelineLog();
});

document.getElementById('btn-log-refresh').addEventListener('click', loadPipelineLog);

// ---- App log ----

async function loadAppLog() {
  const body = document.getElementById('app-log-body');
  body.innerHTML = '<p class="text-muted">로딩 중...</p>';
  try {
    const data = await API.get('/api/logs/app');
    if (!data.lines || !data.lines.length) {
      body.innerHTML = '<div class="log-empty">로그가 없습니다.</div>';
      return;
    }
    const src = data.source === 'journald' ? 'journald' : '파일';
    body.innerHTML = `<p style="font-size:.7rem;color:var(--text-muted);margin-bottom:6px;">출처: ${src}</p><pre class="log-content">${escHtml(data.lines.join('\n'))}</pre>`;
    const pre = body.querySelector('.log-content');
    if (pre) pre.scrollTop = pre.scrollHeight;
  } catch (e) { body.innerHTML = `<p class="text-warn">${e.message}</p>`; }
}

document.getElementById('btn-app-log').addEventListener('click', () => {
  document.getElementById('app-log-modal').style.display = 'flex';
  loadAppLog();
});
document.getElementById('btn-app-log-refresh').addEventListener('click', loadAppLog);

// ---- Pipeline edit buttons ----

document.getElementById('btn-edit-channels').addEventListener('click', openChannelModal);
document.getElementById('btn-edit-lists').addEventListener('click', openListModal);

async function openChannelModal() {
  document.getElementById('channel-modal').style.display = 'flex';
  await _renderChannelModal();
}

async function _renderChannelModal() {
  const body = document.getElementById('channel-modal-body');
  body.innerHTML = '<p class="text-muted">로딩 중...</p>';
  try {
    const channels = await API.get('/api/channels');

    // 채널 목록 테이블 + 추가 폼
    body.innerHTML = `
      <table class="edit-table">
        <thead><tr><th>Channel ID</th><th>이름</th><th>핸들</th><th>활성</th><th></th></tr></thead>
        <tbody id="ch-tbody">
          ${channels.map(c => `
            <tr data-cid="${escHtml(c.channel_id)}">
              <td style="font-family:monospace;font-size:.73rem;color:var(--text-muted)">${escHtml(c.channel_id.slice(0,12))}…</td>
              <td><input type="text" class="ch-name" value="${escHtml(c.name || '')}" placeholder="이름"></td>
              <td><input type="text" class="ch-handle" value="${escHtml(c.handle || '')}" placeholder="@handle"></td>
              <td><label class="switch"><input type="checkbox" class="ch-enabled" ${c.enabled ? 'checked' : ''}><span class="slider"></span></label></td>
              <td style="display:flex;gap:4px;">
                <button class="btn-xs btn-save-ch">저장</button>
                <button class="btn-xs btn-del-ch" style="color:var(--danger);">삭제</button>
              </td>
            </tr>`).join('')}
        </tbody>
      </table>

      <div style="margin-top:16px;padding-top:14px;border-top:1px solid var(--border);">
        <h4 style="margin-bottom:10px;">+ 채널 추가</h4>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">
          <div class="form-group"><label>Channel ID (UC…)</label><input type="text" id="new-ch-id" placeholder="UCxxxxxxxxxxxx"></div>
          <div class="form-group"><label>핸들 (@handle)</label><input type="text" id="new-ch-handle" placeholder="@someinvestor"></div>
          <div class="form-group"><label>이름</label><input type="text" id="new-ch-name" placeholder="채널 이름"></div>
          <div class="form-group"><label>제목 필터 (title_contains)</label><input type="text" id="new-ch-filter" placeholder="이선엽"></div>
        </div>
        <p id="ch-add-status" class="text-muted" style="margin-top:6px;"></p>
        <button id="btn-add-ch" class="btn btn-primary" style="margin-top:8px;">추가</button>
      </div>`;

    // 저장 버튼
    body.querySelectorAll('.btn-save-ch').forEach(btn => {
      btn.addEventListener('click', async () => {
        const row = btn.closest('tr');
        const cid = row.dataset.cid;
        try {
          await API.put(`/api/channels/${encodeURIComponent(cid)}`, {
            name: row.querySelector('.ch-name').value.trim(),
            handle: row.querySelector('.ch-handle').value.trim(),
            enabled: row.querySelector('.ch-enabled').checked,
          });
          btn.textContent = '✓';
          setTimeout(() => { btn.textContent = '저장'; }, 1500);
          loadPipeline();
        } catch (e) { alert('저장 실패: ' + e.message); }
      });
    });

    // 삭제 버튼
    body.querySelectorAll('.btn-del-ch').forEach(btn => {
      btn.addEventListener('click', async () => {
        const row = btn.closest('tr');
        const cid = row.dataset.cid;
        if (!confirm(`채널 ${cid} 와 연결된 모든 영상·임베딩을 삭제합니다.\n계속하시겠습니까?`)) return;
        try {
          const r = await API.del(`/api/channels/${encodeURIComponent(cid)}`);
          row.remove();
          loadPipeline();
          alert(`삭제 완료 (임베딩 ${r.deleted_embeddings}건 제거)`);
        } catch (e) { alert('삭제 실패: ' + e.message); }
      });
    });

    // 추가 버튼
    document.getElementById('btn-add-ch').addEventListener('click', async () => {
      const chId = document.getElementById('new-ch-id').value.trim();
      const handle = document.getElementById('new-ch-handle').value.trim();
      const name = document.getElementById('new-ch-name').value.trim();
      const filter = document.getElementById('new-ch-filter').value.trim();
      const statusEl = document.getElementById('ch-add-status');
      if (!chId && !handle) { statusEl.textContent = 'Channel ID 또는 핸들을 입력하세요.'; return; }
      statusEl.textContent = '추가 중...';
      try {
        const r = await API.post('/api/channels', {
          channel_id: chId || null,
          handle: handle || null,
          name: name || null,
          title_contains: filter || null,
          enabled: true,
        });
        statusEl.textContent = `✓ 추가됨: ${r.channel_id}`;
        loadPipeline();
        await _renderChannelModal();  // 목록 새로고침
      } catch (e) {
        statusEl.textContent = '실패: ' + e.message;
      }
    });
  } catch (e) { body.innerHTML = `<p class="text-warn">${e.message}</p>`; }
}

async function openListModal() {
  document.getElementById('list-modal').style.display = 'flex';
  const body = document.getElementById('list-modal-body');
  body.innerHTML = '<p class="text-muted">로딩 중...</p>';
  try {
    const lists = await API.get('/api/video-lists');
    if (!Object.keys(lists).length) { body.innerHTML = '<p class="text-muted">등록된 영상 목록 없음</p>'; return; }

    body.innerHTML = Object.entries(lists).map(([name, entries]) => `
      <div class="list-section">
        <h4>${escHtml(name)} <span style="color:var(--text-muted);font-weight:400;">(${entries.length}건)</span></h4>
        <div id="list-entries-${escHtml(name)}">
          ${entries.map(e => {
            const url = e.source_url || `https://www.youtube.com/watch?v=${e.video_id}`;
            return `<div class="list-entry-row" id="entry-${escHtml(name)}-${escHtml(e.video_id)}">
              <a href="${escHtml(url)}" target="_blank" class="list-entry-vid">${escHtml(e.video_id)}</a>
              <span style="flex:1;color:var(--text-muted);font-size:.75rem">${escHtml(e.source_url ? trunc(e.source_url, 40) : '')}</span>
              <button class="btn-del" onclick="deleteListEntry('${escHtml(name)}','${escHtml(e.video_id)}')" title="삭제">×</button>
            </div>`;
          }).join('')}
        </div>
        <div class="add-video-row">
          <input type="text" id="new-url-${escHtml(name)}" placeholder="YouTube URL 또는 영상 ID 추가">
          <button class="btn btn-primary btn-sm" onclick="addListEntry('${escHtml(name)}')">추가</button>
        </div>
      </div>`).join('');
  } catch (e) { body.innerHTML = `<p class="text-warn">${e.message}</p>`; }
}

async function deleteListEntry(listName, videoId) {
  if (!confirm(`${videoId} 를 삭제하시겠습니까?`)) return;
  try {
    await API.del(`/api/video-lists/${encodeURIComponent(listName)}/entries/${encodeURIComponent(videoId)}`);
    const el = document.getElementById(`entry-${listName}-${videoId}`);
    if (el) el.remove();
    loadPipeline();
  } catch (e) { alert('삭제 실패: ' + e.message); }
}

async function addListEntry(listName) {
  const input = document.getElementById(`new-url-${listName}`);
  const url = input.value.trim();
  if (!url) return;
  try {
    const data = await API.post(`/api/video-lists/${encodeURIComponent(listName)}/entries`, { url });
    if (data.already_exists) { alert('이미 등록된 영상입니다.'); return; }
    input.value = '';
    openListModal();  // 새로고침
    loadPipeline();
  } catch (e) { alert('추가 실패: ' + e.message); }
}

// ================================================================== //
// Settings Tab
// ================================================================== //

async function loadSettings() {
  try {
    const data = await API.get('/api/settings');
    document.getElementById('set-kis-mode').textContent = data.kis_mode === 'real' ? '실전투자' : '모의투자';
    document.getElementById('set-kis-mode').className = 'badge badge-mode' + (data.kis_mode === 'real' ? ' real' : '');

    // Provider 옵션 레이블 초기화 후 현재 사용중 배지 삽입
    const sel = document.getElementById('sel-provider');
    Array.from(sel.options).forEach(opt => {
      opt.text = opt.text.replace(/\s*\[사용중\]$/, '');
      if (opt.value === data.llm.provider) opt.text += ' [사용중]';
    });
    sel.value = data.llm.provider;

    // Model 입력 + 사용중 배지
    document.getElementById('inp-model').value = data.llm.model;
    let noteEl = document.getElementById('model-active-note');
    if (!noteEl) {
      noteEl = document.createElement('div');
      noteEl.id = 'model-active-note';
      noteEl.style.marginTop = '5px';
      document.getElementById('inp-model').after(noteEl);
    }
    noteEl.innerHTML = `<span class="badge-active-llm">✓ 사용중</span> <span style="font-size:.78rem;color:var(--text-muted);margin-left:4px;">${escHtml(data.llm.provider)} / ${escHtml(data.llm.model)}</span>`;

    document.getElementById('chk-dry-run').checked = data.dry_run;
    updateGlobalStatus(data.emergency_stopped, data.kis_mode);
    updateEmergencyUI(data.emergency_stopped);
  } catch (e) { if (e.message !== '인증 필요') console.error('loadSettings', e); }
}

document.getElementById('btn-paper').addEventListener('click', async () => {
  try { await API.post('/api/settings/kis-mode', { mode: 'paper' }); loadSettings(); loadOverview(); } catch (e) { alert('모드 변경 실패'); }
});

document.getElementById('btn-real').addEventListener('click', async () => {
  if (!confirm('실전투자 모드로 전환합니다. 실제 주문이 실행될 수 있습니다.')) return;
  try { await API.post('/api/settings/kis-mode', { mode: 'real' }); loadSettings(); loadOverview(); } catch (e) { alert('모드 변경 실패'); }
});

document.getElementById('btn-apply-llm').addEventListener('click', async () => {
  const provider = document.getElementById('sel-provider').value;
  const model = document.getElementById('inp-model').value.trim();
  if (!model) { alert('모델명을 입력하세요.'); return; }
  const statusEl = document.getElementById('llm-status');
  statusEl.textContent = '적용 중...';
  try {
    const data = await API.post('/api/settings/llm', { provider, model });
    statusEl.textContent = `적용 완료: ${data.provider}/${data.model}`;
    statusEl.style.color = 'var(--success)';
  } catch (e) {
    statusEl.textContent = e.message || '적용 실패';
    statusEl.style.color = 'var(--danger)';
  }
});

document.getElementById('chk-dry-run').addEventListener('change', async function() {
  if (!this.checked && !confirm('Dry Run을 끄면 실제 주문이 실행될 수 있습니다.')) { this.checked = true; return; }
  try { await API.post('/api/settings/dry-run', { enabled: this.checked }); } catch (e) { alert('변경 실패'); this.checked = !this.checked; }
});

document.getElementById('btn-emergency').addEventListener('click', async () => {
  if (!confirm('긴급 중단을 활성화합니다.')) return;
  try {
    const data = await API.post('/api/settings/emergency-stop', { active: true });
    document.getElementById('emergency-status').textContent = data.message;
    updateEmergencyUI(true); updateGlobalStatus(true);
  } catch (e) { alert('긴급 중단 실패'); }
});

document.getElementById('btn-resume').addEventListener('click', async () => {
  if (!confirm('운영을 재개합니다.')) return;
  try {
    const data = await API.post('/api/settings/emergency-stop', { active: false });
    document.getElementById('emergency-status').textContent = data.message;
    updateEmergencyUI(false); updateGlobalStatus(false);
  } catch (e) { alert('재개 실패'); }
});

function updateEmergencyUI(stopped) {
  document.getElementById('btn-emergency').style.display = stopped ? 'none' : 'block';
  document.getElementById('btn-resume').style.display = stopped ? 'block' : 'none';
}

function updateGlobalStatus(stopped, kisMode) {
  const badge = document.getElementById('status-badge');
  badge.textContent = stopped ? '긴급 중단' : '정상 운영';
  badge.className = 'badge ' + (stopped ? 'badge-stopped' : 'badge-ok');
  if (kisMode) {
    const m = document.getElementById('header-mode');
    m.textContent = kisMode === 'real' ? '실전투자' : '모의투자';
    m.className = 'badge badge-mode' + (kisMode === 'real' ? ' real' : '');
  }
}

// ================================================================== //
// Cost Tab
// ================================================================== //

async function loadCost() {
  try {
    const data = await API.get('/api/cost');
    document.getElementById('cost-stats').innerHTML = `
      <div class="stat"><span class="stat-label">오늘 입력 토큰</span><span class="stat-value">${data.today.input_tokens.toLocaleString()}</span></div>
      <div class="stat"><span class="stat-label">오늘 출력 토큰</span><span class="stat-value">${data.today.output_tokens.toLocaleString()}</span></div>
      <div class="stat"><span class="stat-label">오늘 추정 비용</span><span class="stat-value">${usd(data.today.estimated_cost_usd)}</span></div>
      <div class="stat"><span class="stat-label">누적 추정 비용</span><span class="stat-value">${usd(data.total.estimated_cost_usd)}</span></div>`;

    const tbody = document.getElementById('cost-body');
    tbody.innerHTML = data.recent_entries && data.recent_entries.length
      ? data.recent_entries.slice().reverse().map(e => {
          if (!e || !e.ts) return '';
          const t = new Date(e.ts).toLocaleString('ko-KR', { hour: '2-digit', minute: '2-digit' });
          return `<tr><td>${t}</td><td>${e.provider}/${trunc(e.model, 15)}</td><td>${e.input_tokens.toLocaleString()}</td><td>${e.output_tokens.toLocaleString()}</td><td>${usd(e.cost_usd)}</td></tr>`;
        }).join('')
      : '<tr><td colspan="5" class="text-muted">아직 LLM 호출 내역이 없습니다. Agent 대화 또는 요약 생성 후 표시됩니다.</td></tr>';
  } catch (e) { if (e.message !== '인증 필요') console.error('loadCost', e); }
}

// ================================================================== //
// Init
// ================================================================== //

function initApp() {
  loadOverview();
}

// 토큰이 있으면 바로 앱 시작, 없으면 로그인 화면
if (_token) {
  initApp();
} else {
  showLogin();
}
