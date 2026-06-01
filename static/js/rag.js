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
  const progressEl = document.getElementById('rag-progress');
  const progressText = progressEl.querySelector('.rag-progress-text');
  const progressBar = progressEl.querySelector('.rag-progress-bar');

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
      const s = await api('GET', '/api/rag/status');
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
      const data = await api('POST', '/api/rag/ask', { question: q });
      let html = esc(data.answer).replace(/\n/g, '<br>');
      if (data.citations && data.citations.length) {
        html += '<div class="rag-cites">来源:';
        data.citations.forEach((c, i) => {
          html += ` <a href="${esc(c.notion_url)}" target="_blank" rel="noopener">[${i + 1}] ${esc(c.title_path)}</a>`;
        });
        html += '</div>';
      }
      pending.innerHTML = html;
    } catch (e) {
      // api() throws with a readable message (incl. FastAPI validation arrays).
      pending.innerHTML = '<em>' + esc(e.message || '出错了') + '</em>';
    }
    refreshStatus();
  });

  function renderProgress(p) {
    if (!p || p.phase === 'idle' || (!p.running && p.phase !== 'done')) {
      progressEl.hidden = true;
      return;
    }
    progressEl.hidden = false;
    let text, pct;
    if (p.phase === 'crawl') {
      text = `抓取 Notion 页面…(已 ${p.pages} 页)`;
      pct = null;  // unknown total during crawl → indeterminate
    } else if (p.phase === 'embed') {
      // Embedding is one batch call with no mid-progress, so show the片段 count
      // with an indeterminate bar rather than a stuck 0%.
      text = `本地向量化 ${p.embed_total} 个片段…`;
      pct = null;
    } else if (p.phase === 'store') {
      text = '写入知识库…'; pct = 100;
    } else if (p.phase === 'done') {
      text = `同步完成,共 ${p.chunk_count} 个片段`; pct = 100;
    } else {
      text = '同步中…'; pct = null;
    }
    progressText.textContent = text;
    progressBar.classList.toggle('indeterminate', pct === null);
    progressBar.style.width = pct === null ? '100%' : pct + '%';
  }

  async function pollProgress(render = true) {
    try {
      const p = await api('GET', '/api/rag/sync/progress');
      if (render) renderProgress(p);
      return p;
    } catch (e) { return null; }
  }

  syncBtn.addEventListener('click', async () => {
    syncBtn.disabled = true; syncBtn.textContent = '同步中…';
    // Show progress immediately (don't wait for the first poll, which can race
    // ahead of the server actually starting the sync).
    renderProgress({ phase: 'crawl', running: true, pages: 0 });
    // Poll until the (blocking) /sync request returns; `polling` is the sole
    // stop signal — don't exit early on a stale idle/done snapshot.
    let polling = true;
    const loop = (async () => {
      while (polling) {
        const p = await pollProgress(false);  // don't render here
        // Only show snapshots from the in-flight sync; ignore a stale
        // done/idle from a previous run before this one flips running=true.
        if (p && p.running) renderProgress(p);
        if (!polling) break;
        await new Promise(res => setTimeout(res, 500));
      }
    })();
    try {
      // /sync returns 200 with an `error` field on partial/total failure
      // (so individual source failures don't 500); check it explicitly.
      const data = await api('POST', '/api/rag/sync');
      polling = false; await loop;
      progressEl.hidden = true;
      if (data.error) {
        add('bot', `<em>同步未完成:${esc(data.error)}(现有 ${data.chunk_count} 个片段保留)</em>`);
      } else {
        renderProgress({ phase: 'done', chunk_count: data.chunk_count });
        add('bot', `<em>同步完成,共 ${data.chunk_count} 个片段。</em>`);
        setTimeout(() => { progressEl.hidden = true; }, 3000);
      }
    } catch (e) {
      polling = false; progressEl.hidden = true;
      add('bot', `<em>${esc(e.message || '同步失败')}</em>`);
    }
    syncBtn.disabled = false; syncBtn.textContent = '同步';
    refreshStatus();
  });

  refreshStatus();
})();
