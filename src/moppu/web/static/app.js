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

function formatDateTimeTwoLine(isoStr) {
  if (!isoStr) return '-';
  const s = isoStr.endsWith('Z') || isoStr.includes('+') ? isoStr : isoStr + 'Z';
  try {
    const d = new Date(s);
    const date = d.toLocaleDateString('ko-KR', { timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit' });
    const time = d.toLocaleTimeString('ko-KR', { timeZone: 'Asia/Seoul', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    return `<span style="display:block;white-space:nowrap;">${escHtml(date)}</span><span style="display:block;color:var(--text-muted);font-size:.7rem;white-space:nowrap;">${escHtml(time)}</span>`;
  } catch (_) { return escHtml(isoStr); }
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
    if (btn.dataset.tab === 'overview')  loadOverview();
    if (btn.dataset.tab === 'agent')     { loadSuggestedQuestions(); loadStrategyConfig(); loadStrategyHistory(1); }
    if (btn.dataset.tab === 'pipeline')  { loadPipeline(); loadIngestionHistory(1); }
    if (btn.dataset.tab === 'settings')  { loadSettings(); loadCost(); }
  });
});

// ================================================================== //
// Overview Tab
// ================================================================== //

let _positionRows = [];  // 클릭 핸들러용 — 매 렌더 시 초기화

async function loadOverview() {
  try {
    const data = await API.get('/api/overview');
    const s = data.summary || {};
    document.getElementById('ov-cash').textContent       = krw(s.cash);
    document.getElementById('ov-stock-eval').textContent = krw(s.stock_eval);
    document.getElementById('ov-total-eval').textContent = krw(s.total_eval);
    document.getElementById('ov-purchase').textContent   = krw(s.total_purchase);

    const plEl  = document.getElementById('ov-pl');
    const rateEl = document.getElementById('ov-pl-rate');
    const pl = s.eval_pl || 0;
    const rate = s.eval_pl_rate || 0;
    const cls = pl >= 0 ? 'pl-positive' : 'pl-negative';
    const sign = pl >= 0 ? '+' : '';
    plEl.textContent  = `${sign}${krw(pl)}`;
    plEl.className = 'stat-value ' + cls;
    rateEl.textContent = `${sign}${rate.toFixed(2)}%`;
    rateEl.className = 'stat-value ' + cls;

    document.getElementById('ov-kis-mode').textContent = data.kis_mode === 'real' ? '실전' : '모의';
    document.getElementById('ov-dry-run').textContent  = data.dry_run ? 'ON' : 'OFF';
    updateGlobalStatus(data.emergency_stopped, data.kis_mode);

    const errEl = document.getElementById('broker-error');
    if (data.broker_error) { errEl.textContent = data.broker_error; errEl.style.display = 'block'; }
    else errEl.style.display = 'none';

    const tbody = document.getElementById('positions-body');
    _positionRows = [];
    if (!data.positions || !data.positions.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="text-muted">보유 종목 없음</td></tr>';
    } else {
      tbody.innerHTML = data.positions.map(p => {
        const idx = _positionRows.length;
        _positionRows.push(p);
        const plCls = p.unrealized_pl >= 0 ? 'pl-positive' : 'pl-negative';
        const plSign = p.unrealized_pl >= 0 ? '+' : '';
        const label = p.name
          ? `${escHtml(p.name)} <span style="color:var(--text-muted);font-size:.72rem;">(${escHtml(p.ticker)})</span>`
          : escHtml(p.ticker);
        return `<tr class="clickable-row" data-pos-idx="${idx}">
          <td>${label}</td>
          <td style="text-align:right;">${p.quantity.toLocaleString('ko-KR')}</td>
          <td class="${plCls}" style="text-align:right;">${plSign}${krw(p.unrealized_pl)}</td>
          <td class="${plCls}" style="text-align:right;">${plSign}${p.pl_rate}%</td>
        </tr>`;
      }).join('');
    }
  } catch (e) { if (e.message !== '인증 필요') console.error('loadOverview', e); }

  loadSummaryList(1);
}

// 보유 종목 클릭 → 매매 이력 팝업
document.getElementById('positions-body').addEventListener('click', (e) => {
  const tr = e.target.closest('tr[data-pos-idx]');
  if (!tr) return;
  const p = _positionRows[+tr.dataset.posIdx];
  if (p) openPositionDetail(p);
});

async function openPositionDetail(pos) {
  const modal = document.getElementById('position-modal');
  const body  = document.getElementById('position-modal-body');
  const tickerEl = document.getElementById('position-modal-ticker');
  const titleEl  = document.getElementById('position-modal-title');

  tickerEl.textContent = pos.ticker || '';
  titleEl.textContent = pos.name ? `${pos.name} 매매 이력` : `${pos.ticker} 매매 이력`;
  body.innerHTML = '<div class="modal-loading">로딩 중</div>';
  modal.style.display = 'flex';

  try {
    const d = await API.get(`/api/positions/${encodeURIComponent(pos.ticker)}/trades?days=90`);
    _renderPositionDetail(body, d);
  } catch (e) {
    body.innerHTML = `<p class="text-warn">${escHtml(e.message || '조회 실패')}</p>`;
  }
}

function _renderPositionDetail(body, d) {
  const p = d.position || {};
  const st = d.stats || {};
  const trades = d.trades || [];

  const plCls = (p.unrealized_pl || 0) >= 0 ? 'pl-positive' : 'pl-negative';
  const plSign = (p.unrealized_pl || 0) >= 0 ? '+' : '';
  const rpCls = (st.realized_pl || 0) >= 0 ? 'pl-positive' : 'pl-negative';
  const rpSign = (st.realized_pl || 0) >= 0 ? '+' : '';

  const statsHtml = `
    <div class="position-stat-grid">
      <div class="stat">
        <span class="stat-label">보유 수량</span>
        <span class="stat-value">${(p.quantity || 0).toLocaleString('ko-KR')}</span>
      </div>
      <div class="stat">
        <span class="stat-label">평균 매입가</span>
        <span class="stat-value">${krw(p.avg_price)}</span>
      </div>
      <div class="stat">
        <span class="stat-label">현재 평가금액</span>
        <span class="stat-value">${krw(p.eval_amount)}</span>
      </div>
      <div class="stat">
        <span class="stat-label">미실현 손익</span>
        <span class="stat-value ${plCls}">${plSign}${krw(p.unrealized_pl)} (${plSign}${p.pl_rate || 0}%)</span>
      </div>
      <div class="stat">
        <span class="stat-label">실현 손익(근사)</span>
        <span class="stat-value ${rpCls}">${rpSign}${krw(st.realized_pl)}</span>
      </div>
      <div class="stat">
        <span class="stat-label">승/패</span>
        <span class="stat-value">${st.win_count || 0}승 ${st.loss_count || 0}패</span>
      </div>
    </div>
  `;

  let tradesHtml = '';
  if (!trades.length) {
    tradesHtml = '<p class="text-muted" style="font-size:.85rem;text-align:center;padding:30px 0;">최근 90일 매매 이력이 없습니다.</p>';
  } else {
    tradesHtml = `
      <h4 style="margin-bottom:8px;">최근 매매 이력 (${trades.length}건)</h4>
      <div class="trade-list">
        <div class="trade-row" style="font-weight:600;color:var(--text-muted);font-size:.72rem;border-bottom:1px solid var(--border-2);">
          <span>일시</span><span>구분</span><span style="text-align:right;">수량</span><span style="text-align:right;">체결단가</span><span style="text-align:right;">체결금액</span><span style="text-align:right;">손익</span>
        </div>
        ${trades.map(t => {
          const dateStr = t.date ? `${t.date.slice(0,4)}-${t.date.slice(4,6)}-${t.date.slice(6,8)}` : '';
          const timeStr = t.time && t.time.length >= 6 ? `${t.time.slice(0,2)}:${t.time.slice(2,4)}` : '';
          const sideCls = t.side === 'BUY' ? 't-side-buy' : 't-side-sell';
          const sideLabel = t.side === 'BUY' ? '매수' : '매도';
          const statusSuffix = t.status === 'cancelled' ? ' (취소)' : t.status === 'partial' ? ' (부분)' : '';
          const plCell = t.side === 'SELL' && t.status !== 'cancelled'
            ? `<span class="${t.is_win ? 't-win' : 't-loss'}">${t.pl >= 0 ? '+' : ''}${krw(t.pl)}<br><span style="font-size:.7rem;">${t.pl_rate >= 0 ? '+' : ''}${t.pl_rate}%</span></span>`
            : '<span style="color:var(--text-muted);">-</span>';
          return `<div class="trade-row ${t.status === 'cancelled' ? 't-status-cancelled' : ''}">
            <span style="font-size:.75rem;">${dateStr}<br><span style="color:var(--text-muted);font-size:.7rem;">${timeStr}</span></span>
            <span class="t-side ${sideCls}">${sideLabel}${statusSuffix}</span>
            <span style="text-align:right;">${(t.filled_qty || 0).toLocaleString('ko-KR')}</span>
            <span style="text-align:right;">${krw(t.avg_fill_price)}</span>
            <span style="text-align:right;">${krw(t.total_amount)}</span>
            <span style="text-align:right;">${plCell}</span>
          </div>`;
        }).join('')}
      </div>
      <p style="font-size:.7rem;color:var(--text-muted);margin-top:8px;">
        * 실현 손익은 평균 매입가 기준으로 계산된 근사치입니다.
      </p>
    `;
  }

  body.innerHTML = statsHtml + tradesHtml;
}

document.getElementById('position-modal').addEventListener('click', (e) => {
  if (e.target === document.getElementById('position-modal')) closeModal('position-modal');
});

// ---- Summary list ----

let _summaryPage = 1;
const _summaryCache = {};

function openSummaryDetail(item) {
  document.getElementById('summary-modal').style.display = 'flex';
  renderModalSummary(item);
}

function openSummaryByDate(date) {
  const item = _summaryCache[date];
  if (item) openSummaryDetail(item);
}

async function loadSummaryList(page) {
  _summaryPage = page;
  const body = document.getElementById('summary-list-body');
  const pagEl = document.getElementById('summary-pagination-btns');
  if (!body) return;
  body.innerHTML = '<p class="text-muted" style="font-size:.8rem;">로딩 중...</p>';
  try {
    const data = await API.get(`/api/agent/summary-list?page=${page}&per_page=5`);
    if (!data.items || !data.items.length) {
      body.innerHTML = '<p class="text-muted" style="font-size:.8rem;">수집 요약이 없습니다.</p>';
      if (pagEl) pagEl.innerHTML = '';
      return;
    }
    data.items.forEach(item => { _summaryCache[item.date] = item; });
    body.innerHTML = data.items.map(item => {
      const cnt = (item.videos || []).length;
      return `<div class="clickable-row" data-summary-date="${escHtml(item.date)}"
        style="display:flex;align-items:center;gap:8px;padding:7px 2px;border-bottom:1px solid var(--border);cursor:pointer;">
        <span style="font-size:.82rem;font-weight:600;color:#e2e8f0;">${escHtml(item.date)}</span>
        <span style="font-size:.75rem;color:var(--text-muted);">영상 ${cnt}건</span>
        ${item.summary ? '<span style="font-size:.72rem;color:var(--success);">📋 요약</span>' : ''}
        <span style="margin-left:auto;color:var(--text-muted);font-size:.85rem;">›</span>
      </div>`;
    }).join('');
    if (pagEl) {
      if (data.total_pages > 1) {
        let h = '';
        if (page > 1) h += `<button class="btn btn-secondary btn-sm" style="padding:2px 7px;" onclick="loadSummaryList(${page - 1})">‹</button>`;
        h += `<span style="font-size:.72rem;color:var(--text-muted);margin:0 5px;">${page}/${data.total_pages}</span>`;
        if (page < data.total_pages) h += `<button class="btn btn-secondary btn-sm" style="padding:2px 7px;" onclick="loadSummaryList(${page + 1})">›</button>`;
        pagEl.innerHTML = h;
      } else {
        pagEl.innerHTML = '';
      }
    }
  } catch (e) {
    if (e.message !== '인증 필요' && body) body.innerHTML = `<p class="text-warn" style="font-size:.8rem;">${escHtml(e.message)}</p>`;
  }
}

document.getElementById('summary-list-body').addEventListener('click', (e) => {
  const row = e.target.closest('[data-summary-date]');
  if (row) openSummaryByDate(row.dataset.summaryDate);
});

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

    // Local Machine 연결 상태 — 헤더 바로 아래에 표시
    const localEl = document.getElementById('local-collector-status');
    if (localEl) {
      const lc = data.local_collector || {};
      if (lc.connected) {
        const s = lc.stale_sec;
        localEl.className = 'local-status connected';
        localEl.innerHTML = `<span class="ls-dot"></span> 로컬 수집기 연결됨 <span style="color:var(--text-muted);font-size:.72rem;">(${s}초 전 응답)</span>`;
      } else {
        localEl.className = 'local-status disconnected';
        const last = lc.last_seen ? formatKoreanDateTime(lc.last_seen) : '없음';
        localEl.innerHTML = `<span class="ls-dot"></span> <strong>Local Machine Error</strong> — 로컬 수집기 연결 끊김 <span style="color:var(--text-muted);font-size:.72rem;">(최근: ${escHtml(last)})</span>`;
      }
    }

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
      const isErr     = data.pipeline_run_msg.startsWith('오류') || data.pipeline_run_msg.includes('Error');
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
document.getElementById('btn-ingest-history-refresh').addEventListener('click', () => loadIngestionHistory(_ingestPage));

document.getElementById('ingest-history-body').addEventListener('click', (e) => {
  const tr = e.target.closest('tr[data-vid]');
  if (tr) openIngestDetail(tr.dataset.vid);
});

// ---- Ingestion history ----

let _ingestPage = 1;

async function loadIngestionHistory(page) {
  _ingestPage = page;
  const tbody = document.getElementById('ingest-history-body');
  const pagEl  = document.getElementById('ingest-pagination');
  tbody.innerHTML = '<tr><td colspan="5" class="text-muted">로딩 중...</td></tr>';
  try {
    const data = await API.get(`/api/pipeline/ingestion-history?page=${page}&per_page=10`);
    if (!data.items || !data.items.length) {
      tbody.innerHTML = '<tr><td colspan="3" class="text-muted" style="text-align:center;padding:20px;">수집 이력이 없습니다.</td></tr>';
      pagEl.innerHTML = '';
      return;
    }

    tbody.innerHTML = data.items.map(v => {
      const dt = v.created_at ? formatDateTimeTwoLine(v.created_at) : '-';
      const title = trunc(v.title || v.video_id, 48);
      const badge = v.status === 'embedded'
        ? '<span class="ingest-badge ingest-badge-embedded">완료</span>'
        : v.status === 'failed'
          ? '<span class="ingest-badge ingest-badge-failed">실패</span>'
          : '<span class="ingest-badge ingest-badge-pending">대기</span>';
      return `<tr class="clickable-row" data-vid="${escHtml(v.video_id)}">
        <td style="font-size:.76rem;width:140px;">${dt}</td>
        <td style="font-size:.82rem;">${escHtml(title)}</td>
        <td style="text-align:center;">${badge}</td>
      </tr>`;
    }).join('');

    // 페이지네이션
    if (data.total_pages > 1) {
      let pagHtml = '';
      if (page > 1) pagHtml += `<button onclick="loadIngestionHistory(${page - 1})">‹ 이전</button>`;
      const start = Math.max(1, page - 2), end = Math.min(data.total_pages, page + 2);
      for (let p = start; p <= end; p++) {
        pagHtml += `<button class="${p === page ? 'active' : ''}" onclick="loadIngestionHistory(${p})">${p}</button>`;
      }
      if (page < data.total_pages) pagHtml += `<button onclick="loadIngestionHistory(${page + 1})">다음 ›</button>`;
      pagEl.innerHTML = pagHtml;
    } else {
      pagEl.innerHTML = '';
    }
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="3" class="text-warn">${escHtml(e.message)}</td></tr>`;
    pagEl.innerHTML = '';
  }
}

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
          const t = new Date(e.ts).toLocaleString('ko-KR', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
          return `<tr><td>${t}</td><td>${e.provider}/${trunc(e.model, 15)}</td><td>${e.input_tokens.toLocaleString()}</td><td>${e.output_tokens.toLocaleString()}</td><td>${usd(e.cost_usd)}</td></tr>`;
        }).join('')
      : '<tr><td colspan="5" class="text-muted">아직 LLM 호출 내역이 없습니다. Agent 대화 또는 요약 생성 후 표시됩니다.</td></tr>';
  } catch (e) { if (e.message !== '인증 필요') console.error('loadCost', e); }
}

// ================================================================== //
// Strategy Planner
// ================================================================== //

let _strategyPage = 1;
let _strategyPolling = null;
let _strategyLiveItem = null;   // 실행 중인 항목 (optimistic)
let _strategyRows = [];         // 클릭 핸들러용 — 매 렌더 시 초기화

async function loadStrategyConfig() {
  try {
    const data = await API.get('/api/strategy/config');
    document.getElementById('inp-strategy-cron').value = data.cron || '30 9 * * 1-5';
    document.getElementById('chk-strategy-dry-run').checked = data.dry_run;
    document.getElementById('chk-strategy-enabled').checked = data.enabled;

    const dryBadge = document.getElementById('strategy-dry-badge');
    dryBadge.style.display = 'inline-flex';
    dryBadge.textContent = data.dry_run ? 'DRY RUN' : '실거래';
    dryBadge.className = 'strategy-badge ' + (data.dry_run ? 'strategy-badge-dry' : 'strategy-badge-sell');

    // 서버가 실행 중이면 live item 복원 후 폴링 시작
    if (data.running) {
      if (!_strategyLiveItem) {
        _strategyLiveItem = { run_at: new Date().toISOString(), status: 'running' };
      }
      _renderStrategyHistory(null);   // live item만 보여줌 (API data는 뒤에서 로드)
      _showStrategyStatus(data.last_msg || '전략 수립 진행 중...', 'running');
      if (!_strategyPolling) _strategyPolling = setInterval(_pollStrategyStatus, 3000);
    } else {
      if (_strategyLiveItem && _strategyLiveItem.status === 'running') {
        // 서버는 완료됐는데 live item이 남아있으면 정리
        _strategyLiveItem = null;
        loadStrategyHistory(1);
      }
      if (data.last_msg) {
        const isErr = data.last_msg.startsWith('오류');
        _showStrategyStatus(data.last_msg, isErr ? 'error' : 'done');
      }
    }
  } catch (e) { if (e.message !== '인증 필요') console.error('loadStrategyConfig', e); }
}

async function _pollStrategyStatus() {
  try {
    // 라이브 로그 + 상태 한 번에 조회
    const liveData = await API.get('/api/strategy/live-log');
    _renderStrategyLivePanel(liveData);
    if (liveData.running) {
      if (_strategyLiveItem) _strategyLiveItem.status = 'running';
      _showStrategyStatus(liveData.msg || '전략 수립 진행 중...', 'running');
      _renderStrategyHistory(null);
    } else {
      if (_strategyPolling) { clearInterval(_strategyPolling); _strategyPolling = null; }
      const isErr = (liveData.msg || '').startsWith('오류');
      if (_strategyLiveItem) {
        _strategyLiveItem.status = isErr ? 'error' : 'completed';
        _strategyLiveItem.error  = isErr ? liveData.msg : null;
        _renderStrategyHistory(null);
      }
      _showStrategyStatus(liveData.msg || '', isErr ? 'error' : 'done');
      // 실행 종료되면 실제 이력 다시 로드 (파일이 저장됐을 것)
      setTimeout(() => {
        _strategyLiveItem = null;
        loadStrategyHistory(1);
        // 완료 후 로그 패널은 5초 후 자동 숨김
        setTimeout(() => {
          const panel = document.getElementById('strategy-log-panel');
          if (panel && !_strategyRunning()) panel.style.display = 'none';
        }, 5000);
      }, 1500);
    }
  } catch (_) {}
}

function _strategyRunning() {
  const el = document.getElementById('strategy-run-status');
  return el && el.classList.contains('run-status') && !el.classList.contains('done');
}

function _renderStrategyLivePanel(data) {
  const panel = document.getElementById('strategy-log-panel');
  const content = document.getElementById('strategy-log-content');
  if (!panel || !content) return;
  const lines = data.lines || [];
  panel.style.display = (lines.length || data.running) ? 'block' : panel.style.display;
  const text = lines.join('\n') || (data.running ? '(로그 수집 중...)' : '(로그 없음)');
  if (content.textContent !== text) {
    content.textContent = text;
    // 자동 스크롤
    content.scrollTop = content.scrollHeight;
  }
}

function _showStrategyStatus(msg, state) {
  const el    = document.getElementById('strategy-run-status');
  const msgEl = document.getElementById('strategy-run-msg');
  if (!msg) { el.style.display = 'none'; return; }
  el.style.display = 'flex';
  el.className = 'run-status' + (state === 'error' ? ' error' : state === 'done' ? ' done' : '');
  msgEl.innerHTML = state === 'running'
    ? `<div class="spinner" style="display:inline-block;margin-right:6px;width:14px;height:14px;"></div>${escHtml(msg)}`
    : escHtml(msg);
}

function _renderStrategyHistoryRow(item) {
  const isRunning   = item.status === 'running';
  const isError     = item.status === 'error';
  const isCompleted = item.status === 'completed';

  const dt = formatDateTimeTwoLine(item.run_at || new Date().toISOString());

  const statusBadge = isRunning
    ? '<span class="strategy-badge strategy-badge-running"><span class="spinner" style="width:8px;height:8px;border-width:1.5px;margin-right:2px;"></span>실행 중</span>'
    : isError
    ? '<span class="strategy-badge strategy-badge-error">실패</span>'
    : '<span class="strategy-badge strategy-badge-ok">완료</span>';

  const modeBadge = item.dry_run
    ? '<span class="strategy-badge strategy-badge-dry">DRY</span>'
    : '<span class="strategy-badge strategy-badge-sell">실거래</span>';

  // 완료·실패 모두 클릭 가능 (running 만 제외)
  const clickable = isCompleted || isError;
  let trAttrs = '';
  if (clickable) {
    const idx = _strategyRows.length;
    _strategyRows.push(item);
    trAttrs = `data-strategy-idx="${idx}" class="clickable-row"`;
  }

  return `<tr ${trAttrs}>
    <td style="font-size:.78rem;">${dt}</td>
    <td>${statusBadge}</td>
    <td>${modeBadge}${clickable ? '<span style="float:right;color:var(--text-muted);font-size:.7rem;">›</span>' : ''}</td>
  </tr>`;
}

function _renderStrategyHistory(apiItems) {
  const tbody = document.getElementById('strategy-history-body');
  if (!tbody) return;
  _strategyRows = [];
  const items = apiItems || [];

  let rows = '';
  if (_strategyLiveItem) {
    rows += _renderStrategyHistoryRow(_strategyLiveItem);
  }
  if (!rows && !items.length) {
    tbody.innerHTML = '<tr><td colspan="3" class="text-muted" style="text-align:center;padding:20px;">실행 이력이 없습니다.</td></tr>';
    return;
  }
  rows += items.map(item => _renderStrategyHistoryRow({ ...item, status: item.status || 'completed' })).join('');
  tbody.innerHTML = rows;
}

document.getElementById('btn-strategy-run').addEventListener('click', async () => {
  const btn = document.getElementById('btn-strategy-run');
  btn.disabled = true;

  // API 응답 전에 즉시 "실행 중" 항목을 이력에 표시
  const dryChk = document.getElementById('chk-strategy-dry-run');
  _strategyLiveItem = {
    run_at: new Date().toISOString(),
    status: 'running',
    dry_run: dryChk ? dryChk.checked : true,
    sells: [], buys: [], summary: '',
  };
  _renderStrategyHistory([]);
  _showStrategyStatus('전략 수립 요청 중...', 'running');

  // 즉시 로그 패널 표시
  const panel = document.getElementById('strategy-log-panel');
  const content = document.getElementById('strategy-log-content');
  if (panel) panel.style.display = 'block';
  if (content) content.textContent = '대기 중...';

  try {
    await API.post('/api/strategy/run', {});
    _showStrategyStatus('전략 수립 진행 중...', 'running');
    if (!_strategyPolling) _strategyPolling = setInterval(_pollStrategyStatus, 1500);
    // 즉시 한 번 폴링해서 빠르게 첫 로그 표시
    _pollStrategyStatus();
  } catch (e) {
    // API 실패 시 live item을 오류 상태로 전환
    _strategyLiveItem.status = 'error';
    _strategyLiveItem.error = e.message || '실행 요청 실패';
    _renderStrategyHistory([]);
    _showStrategyStatus(e.message || '실행 요청 실패', 'error');
  }
  btn.disabled = false;
});

// 로그 패널: 새로고침
document.getElementById('btn-strategy-log-refresh').addEventListener('click', _pollStrategyStatus);

// 로그 패널: 중단
document.getElementById('btn-strategy-stop').addEventListener('click', async () => {
  if (!confirm('전략 수립 실행을 중단하시겠습니까?\n(진행 중 단계 완료 후 종료됨)')) return;
  const btn = document.getElementById('btn-strategy-stop');
  btn.disabled = true;
  try {
    await API.post('/api/strategy/stop', {});
    _showStrategyStatus('중단 요청됨 — 진행 중 단계 완료 후 종료', 'running');
  } catch (e) {
    alert(e.message || '중단 요청 실패');
  }
  btn.disabled = false;
});

document.getElementById('btn-strategy-schedule').addEventListener('click', () => {
  document.getElementById('strategy-schedule-modal').style.display = 'flex';
});

document.getElementById('strategy-schedule-modal').addEventListener('click', (e) => {
  if (e.target === document.getElementById('strategy-schedule-modal')) closeModal('strategy-schedule-modal');
});

document.getElementById('btn-save-strategy-cfg').addEventListener('click', async () => {
  const cron    = document.getElementById('inp-strategy-cron').value.trim();
  const dryRun  = document.getElementById('chk-strategy-dry-run').checked;
  const enabled = document.getElementById('chk-strategy-enabled').checked;
  const statusEl = document.getElementById('strategy-cfg-status');
  if (!cron) { statusEl.textContent = 'Cron 표현식을 입력하세요.'; return; }
  statusEl.textContent = '저장 중...';
  try {
    await API.post('/api/strategy/config', { cron, dry_run: dryRun, enabled });
    statusEl.textContent = `✓ 저장됨`;
    statusEl.style.color = 'var(--success)';
    loadStrategyConfig();
    setTimeout(() => {
      closeModal('strategy-schedule-modal');
      statusEl.textContent = '';
      statusEl.style.color = '';
    }, 1200);
  } catch (e) {
    statusEl.textContent = '저장 실패: ' + e.message;
    statusEl.style.color = 'var(--danger)';
  }
});

document.getElementById('btn-strategy-history-refresh').addEventListener('click', () => {
  _strategyLiveItem = null;
  loadStrategyHistory(1);
});

document.getElementById('strategy-history-body').addEventListener('click', (e) => {
  const tr = e.target.closest('tr[data-strategy-idx]');
  if (!tr) return;
  const item = _strategyRows[+tr.dataset.strategyIdx];
  if (!item) return;
  if (item.status === 'error') {
    openStrategyFailedDetail(item);
  } else if (item.status === 'completed') {
    openStrategyDetail(item);
  }
});

async function openStrategyFailedDetail(item) {
  const modal = document.getElementById('strategy-log-modal');
  const body  = document.getElementById('strategy-log-modal-body');
  const dateEl = document.getElementById('strategy-log-modal-date');
  dateEl.textContent = item.run_at ? formatKoreanDateTime(item.run_at) : '';
  body.innerHTML = '<div class="modal-loading">로딩 중</div>';
  modal.style.display = 'flex';
  try {
    let detail = item;
    // filename 이 있으면 서버에서 완전한 로그까지 불러온다
    if (item.filename) {
      detail = await API.get(`/api/strategy/history/${encodeURIComponent(item.filename)}`);
    }
    const err = escHtml(detail.error || item.error || '알 수 없는 오류');
    const logText = detail.log || '(실행 로그가 저장되지 않았습니다)';
    body.innerHTML = `
      <div style="padding:10px 12px;background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.3);border-radius:6px;margin-bottom:14px;">
        <p style="font-size:.7rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:.04em;margin-bottom:4px;">오류 메시지</p>
        <p style="color:var(--danger);font-size:.88rem;font-weight:500;">${err}</p>
      </div>
      <h4 style="margin-bottom:8px;">실행 로그</h4>
      <pre class="strategy-log-box" style="height:320px;">${escHtml(logText)}</pre>
    `;
    const pre = body.querySelector('.strategy-log-box');
    if (pre) pre.scrollTop = pre.scrollHeight;
  } catch (e) {
    body.innerHTML = `<p class="text-warn">${escHtml(e.message || '조회 실패')}</p>`;
  }
}

document.getElementById('strategy-log-modal').addEventListener('click', (e) => {
  if (e.target === document.getElementById('strategy-log-modal')) closeModal('strategy-log-modal');
});

async function loadStrategyHistory(page) {
  _strategyPage = page;
  const tbody = document.getElementById('strategy-history-body');
  const pagEl = document.getElementById('strategy-pagination');
  if (!tbody) return;
  if (!_strategyLiveItem) {
    tbody.innerHTML = '<tr><td colspan="7" class="text-muted" style="text-align:center;padding:20px;">로딩 중...</td></tr>';
  }
  try {
    const data = await API.get(`/api/strategy/history?page=${page}&per_page=10`);
    _renderStrategyHistory(data.items || []);

    if (data.total_pages > 1) {
      let pagHtml = '';
      if (page > 1) pagHtml += `<button onclick="loadStrategyHistory(${page - 1})">‹ 이전</button>`;
      const start = Math.max(1, page - 2), end = Math.min(data.total_pages, page + 2);
      for (let p = start; p <= end; p++) {
        pagHtml += `<button class="${p === page ? 'active' : ''}" onclick="loadStrategyHistory(${p})">${p}</button>`;
      }
      if (page < data.total_pages) pagHtml += `<button onclick="loadStrategyHistory(${page + 1})">다음 ›</button>`;
      pagEl.innerHTML = pagHtml;
    } else {
      pagEl.innerHTML = '';
    }
  } catch (e) {
    if (tbody) tbody.innerHTML = `<tr><td colspan="7" class="text-warn">${escHtml(e.message)}</td></tr>`;
    if (pagEl) pagEl.innerHTML = '';
  }
}

function openStrategyDetail(item) {
  if (typeof item === 'string') { try { item = JSON.parse(item); } catch(_){} }
  if (!item || item.status === 'running') return;
  document.getElementById('strategy-modal-date').textContent = item.run_at ? formatKoreanDateTime(item.run_at) : '';

  const sectorsAdd    = (item.sectors_to_add    || []).map(s => `<span class="sector-tag add">${escHtml(s)}</span>`).join('');
  const sectorsReduce = (item.sectors_to_reduce || []).map(s => `<span class="sector-tag reduce">${escHtml(s)}</span>`).join('');

  // 매도/매수를 2줄 포맷으로 — 첫 줄: 종목+수량, 둘째 줄: 사유(줄글 전체)
  const renderSell = (s) => `
    <div class="strategy-trade-row-wrap">
      <span class="side-badge trade-side-sell">SELL</span>
      <div>
        <div class="ticker-line">${s.name ? escHtml(s.name) + '(' + escHtml(s.ticker) + ')' : escHtml(s.ticker)}</div>
        <div class="reason-line">${escHtml(s.reason || '')}</div>
      </div>
      <span class="qty-line">${s.quantity < 0 ? '전량' : s.quantity + '주'}</span>
    </div>`;
  const renderBuy = (b) => `
    <div class="strategy-trade-row-wrap">
      <span class="side-badge trade-side-buy">BUY</span>
      <div>
        <div class="ticker-line">${b.name ? escHtml(b.name) + '(' + escHtml(b.ticker) + ')' : escHtml(b.ticker)}</div>
        <div class="reason-line">${escHtml(b.reason || '')}</div>
      </div>
      <span class="qty-line">${b.quantity}주 × ${b.price ? Math.round(b.price).toLocaleString('ko-KR') + '원' : '-'}</span>
    </div>`;

  const sells = (item.sells || []).map(renderSell).join('') || '<p class="text-muted" style="font-size:.8rem;">매도 없음</p>';
  const buys  = (item.buys || []).map(renderBuy).join('') || '<p class="text-muted" style="font-size:.8rem;">매수 없음</p>';

  const convictionBadge = (item.conviction != null)
    ? `<span class="strategy-badge" style="background:rgba(59,130,246,.12);color:#60a5fa;border:1px solid rgba(59,130,246,.3);">LSY 강경도 ${item.conviction}/10</span>`
    : '';

  document.getElementById('strategy-modal-body').innerHTML = `
    <div class="strategy-modal-section">
      <h4>전략 요약 ${convictionBadge}</h4>
      <div class="md-content" style="font-size:.85rem;">${mdRender(item.summary || '요약 없음')}</div>
    </div>

    ${(item.sectors_to_add || []).length || (item.sectors_to_reduce || []).length ? `
    <div class="strategy-modal-section">
      <h4>섹터 조정</h4>
      ${sectorsAdd ? `<div style="margin-bottom:6px;"><span style="font-size:.72rem;color:var(--text-muted);">추가·강화</span><div class="strategy-sector-tags">${sectorsAdd}</div></div>` : ''}
      ${sectorsReduce ? `<div><span style="font-size:.72rem;color:var(--text-muted);">축소·정리</span><div class="strategy-sector-tags">${sectorsReduce}</div></div>` : ''}
    </div>` : ''}

    <div class="strategy-modal-section">
      <h4>매도 계획</h4>
      ${sells}
    </div>

    <div class="strategy-modal-section">
      <h4>매수 계획</h4>
      ${buys}
    </div>

    <div style="display:flex;gap:20px;margin-top:12px;padding-top:12px;border-top:1px solid var(--border);">
      <span style="font-size:.78rem;color:var(--text-muted);">예상 매도: <strong>${krw(item.total_sell_krw)}</strong></span>
      <span style="font-size:.78rem;color:var(--text-muted);">예상 매수: <strong>${krw(item.total_buy_krw)}</strong></span>
      ${item.dry_run ? '<span class="strategy-badge strategy-badge-dry" style="margin-left:auto;">DRY RUN</span>' : ''}
    </div>
  `;
  document.getElementById('strategy-modal').style.display = 'flex';
}

document.getElementById('strategy-modal').addEventListener('click', (e) => {
  if (e.target === document.getElementById('strategy-modal')) closeModal('strategy-modal');
});

document.getElementById('ingest-detail-modal').addEventListener('click', (e) => {
  if (e.target === document.getElementById('ingest-detail-modal')) closeModal('ingest-detail-modal');
});

async function openIngestDetail(videoId) {
  const modal = document.getElementById('ingest-detail-modal');
  const body  = document.getElementById('ingest-modal-body');
  const dateEl = document.getElementById('ingest-modal-date');

  dateEl.textContent = '';
  body.innerHTML = '<div class="modal-loading">로딩 중</div>';
  modal.style.display = 'flex';

  try {
    const d = await API.get(`/api/pipeline/video/${encodeURIComponent(videoId)}`);
    dateEl.textContent = d.created_at ? formatKoreanDateTime(d.created_at) : '';
    const url = d.url || `https://www.youtube.com/watch?v=${d.video_id}`;

    const sourceLabel = d.channel_name
      ? escHtml(d.channel_name)
      : d.source_type?.startsWith('list:')
        ? escHtml(d.source_type.replace('list:', '목록: '))
        : '채널';

    const statusHtml = d.status === 'embedded'
      ? '<span class="ingest-badge ingest-badge-embedded">임베딩 완료</span>'
      : d.status === 'failed'
        ? `<span class="ingest-badge ingest-badge-failed">실패</span>${d.error ? `<p class="text-warn" style="font-size:.78rem;margin-top:6px;">${escHtml(d.error)}</p>` : ''}`
        : '<span class="ingest-badge ingest-badge-pending">대기</span>';

    const chunksInfo = d.n_chunks > 0
      ? `<span style="font-size:.75rem;color:var(--text-muted);margin-left:8px;">(청크 ${d.n_chunks}개 임베딩됨)</span>`
      : '';

    const retryBtnHtml = d.status === 'failed'
      ? `<div style="margin-top:18px;padding-top:14px;border-top:1px solid var(--border);">
          <button class="btn btn-primary btn-sm" data-retry-vid="${escHtml(d.video_id)}">↻ 이 영상만 재시도</button>
          <p style="font-size:.72rem;color:var(--text-muted);margin-top:6px;">
            로컬 수집기에 재시도 신호를 보냅니다. 로컬 수집기가 연결되어 있어야 합니다.
          </p>
        </div>`
      : '';

    body.innerHTML = `
      <div style="margin-bottom:16px;">
        <p style="font-size:.7rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:.04em;margin-bottom:6px;">제목</p>
        <p style="font-size:.92rem;font-weight:600;line-height:1.5;">${escHtml(d.title || d.video_id)}</p>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:16px;">
        <div>
          <p style="font-size:.7rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:.04em;margin-bottom:4px;">출처</p>
          <p style="font-size:.85rem;">${sourceLabel}</p>
        </div>
        <div>
          <p style="font-size:.7rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:.04em;margin-bottom:4px;">상태</p>
          <div style="display:flex;align-items:center;flex-wrap:wrap;">${statusHtml}${chunksInfo}</div>
        </div>
      </div>
      <div style="margin-bottom:16px;">
        <p style="font-size:.7rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:.04em;margin-bottom:6px;">영상</p>
        <a href="${escHtml(url)}" target="_blank" style="color:var(--primary);font-size:.85rem;word-break:break-all;">${escHtml(url)}</a>
      </div>
      ${retryBtnHtml}`;

    const retryBtn = body.querySelector('[data-retry-vid]');
    if (retryBtn) {
      retryBtn.addEventListener('click', async () => {
        if (!confirm('이 영상만 재시도합니다. 계속하시겠습니까?')) return;
        retryBtn.disabled = true;
        retryBtn.textContent = '요청 중...';
        try {
          const r = await API.post(`/api/pipeline/retry/${encodeURIComponent(retryBtn.dataset.retryVid)}`, {});
          alert(r.message || '재시도 요청 전송됨');
          closeModal('ingest-detail-modal');
          loadIngestionHistory(_ingestPage);
        } catch (e) {
          alert(e.message || '재시도 실패');
          retryBtn.disabled = false;
          retryBtn.textContent = '↻ 이 영상만 재시도';
        }
      });
    }
  } catch (e) {
    body.innerHTML = `<p class="text-warn">${escHtml(e.message)}</p>`;
  }
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
