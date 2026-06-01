// Floating RAG Q&A widget. Self-contained; no-op when the feature is disabled.
(function () {
  const fab = document.getElementById('rag-fab');
  const panel = document.getElementById('rag-panel');
  if (!fab || !panel) return;

  const log = document.getElementById('rag-log');
  const statusEl = document.getElementById('rag-status');
  const form = document.getElementById('rag-form');
  const input = document.getElementById('rag-input');
  const syncBtn = document.getElementById('rag-sync');

  function add(role, html) {
    const div = document.createElement('div');
    div.className = 'rag-msg rag-' + role;
    div.innerHTML = html;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  function esc(s) {
    return (s || '').replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
  }

  async function refreshStatus() {
    try {
      const r = await fetch('/api/rag/status');
      const s = await r.json();
      if (!s.enabled) { fab.hidden = true; panel.hidden = true; return; }
      fab.hidden = false;
      statusEl.textContent =
        `模型 ${s.model} · 片段 ${s.chunk_count} · 今日剩余 ${s.remaining_today}/${s.daily_limit}`;
    } catch (e) { fab.hidden = true; }
  }

  fab.addEventListener('click', () => { panel.hidden = false; fab.hidden = true; input.focus(); });
  document.getElementById('rag-close').addEventListener('click', () => {
    panel.hidden = true; fab.hidden = false;
  });

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    add('user', esc(q));
    input.value = '';
    add('bot', '<em>思考中…</em>');
    const pending = log.lastChild;
    try {
      const r = await fetch('/api/rag/ask', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: q }),
      });
      const data = await r.json();
      if (!r.ok) { pending.innerHTML = '<em>' + esc(data.detail || '出错了') + '</em>'; return; }
      let html = esc(data.answer).replace(/\n/g, '<br>');
      if (data.citations && data.citations.length) {
        html += '<div class="rag-cites">来源:';
        data.citations.forEach((c, i) => {
          html += ` <a href="${esc(c.notion_url)}" target="_blank" rel="noopener">[${i + 1}] ${esc(c.title_path)}</a>`;
        });
        html += '</div>';
      }
      pending.innerHTML = html;
    } catch (e) { pending.innerHTML = '<em>网络错误</em>'; }
    refreshStatus();
  });

  syncBtn.addEventListener('click', async () => {
    syncBtn.disabled = true; syncBtn.textContent = '同步中…';
    try {
      const r = await fetch('/api/rag/sync', { method: 'POST' });
      const data = await r.json();
      add('bot', r.ok ? `<em>同步完成,共 ${data.chunk_count} 个片段。</em>`
                      : `<em>${esc(data.detail || '同步失败')}</em>`);
    } catch (e) { add('bot', '<em>同步失败</em>'); }
    syncBtn.disabled = false; syncBtn.textContent = '同步';
    refreshStatus();
  });

  refreshStatus();
})();
