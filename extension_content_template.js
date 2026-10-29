// Job Bot: adds a "Fill with Job Bot" button to job application forms. It fills, you submit.
(() => {
  if (window.__jobBotLoaded) return;
  window.__jobBotLoaded = true;

  const collectFields = /*__COLLECT__*/;
  const collectButtons = /*__BUTTONS__*/;
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const norm = s => (s || '').toLowerCase().replace(/[^a-z0-9%]+/g, ' ').trim();
  const ask = (path, payload) => new Promise(res => chrome.runtime.sendMessage({path, payload}, res));
  const COLORS = {fill: '#2e7d32', need: '#f9a825'};

  function looksLikeForm() {
    const visible = el => el.offsetParent !== null;
    const inputs = [...document.querySelectorAll('input:not([type=hidden]):not([type=search]), textarea, select')].filter(visible);
    return !!document.querySelector('input[type=file]') || inputs.length >= 4
        || inputs.some(e => e.tagName === 'TEXTAREA' && e.offsetHeight > 40)          // a note / cover letter box
        || !!document.querySelector('[role=dialog] textarea, [role=dialog] input:not([type=hidden])');
  }

  function mark(el, color) {
    if (el) { el.style.outline = `3px solid ${color}`; el.style.outlineOffset = '1px'; }
  }

  function setNative(el, value) {  // works with React/Angular forms, which ignore a plain .value = ...
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  }

  function pick(texts, values) {
    const opts = texts.map(norm);
    for (const v of values.map(norm)) {
      if (!v) continue;
      let i = opts.findIndex(o => o === v);
      if (i < 0) i = opts.findIndex(o => o.startsWith(v + ' ') || (v.startsWith(o) && o.length > 2));
      if (i < 0) i = opts.findIndex(o => new RegExp(`\\b${v.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`).test(o));
      if (i >= 0) return i;
    }
    return -1;
  }

  async function chooseCombo(el, values) {
    el.scrollIntoView({block: 'center'});
    const terms = [...new Set([...values, ...values.map(v => (v.split(/\s+/)[0] || '').replace(/,$/, ''))])];
    for (const term of terms) {
      el.focus(); el.click();
      setNative(el, term);
      await sleep(900);
      const opts = [...document.querySelectorAll('[role=option]')].filter(o => o.offsetParent !== null);
      const i = pick(opts.map(o => o.innerText), values);
      if (i >= 0) {
        opts[i].dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
        opts[i].click();
        await sleep(300);
        return true;
      }
    }
    setNative(el, '');
    el.blur();
    return false;
  }

  async function attach(el, key) {
    const r = await ask('/file', {key, host: location.hostname});
    if (!r || !r.ok) return false;
    const bytes = Uint8Array.from(atob(r.data.b64), c => c.charCodeAt(0));
    const dt = new DataTransfer();
    dt.items.add(new File([bytes], r.data.name, {type: r.data.type}));
    el.files = dt.files;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    await sleep(2500);  // let the site upload it
    return true;
  }

  async function apply(actions) {
    let done = 0;
    for (const a of actions) {
      const el = a.sel ? document.querySelector(a.sel) : document.querySelector(`[data-af='${a.idx}']`);
      if (!el) continue;
      try {
        if (a.kind === 'fill') setNative(el, a.value);
        else if (a.kind === 'date') { setNative(el, a.value); el.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true})); }
        else if (a.kind === 'select') { el.selectedIndex = a.index; el.dispatchEvent(new Event('change', {bubbles: true})); }
        else if (a.kind === 'check') { if (!el.checked) el.click(); if (!el.checked && el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) l.click(); } }
        else if (a.kind === 'click') el.click();
        else if (a.kind === 'combo') { if (!(await chooseCombo(el, a.values))) { mark(el, COLORS.need); continue; } }
        else if (a.kind === 'file') { if (!(await attach(el, a.key))) { mark(el, COLORS.need); continue; } }
        mark(el, COLORS.fill);
        done++;
        await sleep(120);
      } catch (e) { mark(el, COLORS.need); }
    }
    return done;
  }

  // ---------- the button and the result panel ----------
  const bar = document.createElement('div');
  bar.style.cssText = 'position:fixed;right:18px;bottom:18px;z-index:2147483647;display:flex;flex-direction:column;' +
                      'gap:6px;align-items:flex-end;font:13px/1.4 system-ui,Segoe UI,Arial,sans-serif';
  const panel = document.createElement('div');
  panel.style.cssText = 'display:none;max-width:360px;max-height:50vh;overflow:auto;background:#fff;color:#222;' +
                        'border:1px solid #ccc;border-radius:10px;padding:10px 12px;box-shadow:0 4px 18px rgba(0,0,0,.18)';
  const row = document.createElement('div');
  row.style.cssText = 'display:flex;gap:6px';
  const btn = (text, bg) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = text;
    b.style.cssText = `cursor:pointer;border:0;border-radius:18px;padding:9px 14px;color:#fff;background:${bg};` +
                      'font:600 13px system-ui,Segoe UI,Arial,sans-serif;box-shadow:0 2px 8px rgba(0,0,0,.2)';
    return b;
  };
  const fillBtn = btn('⚡ Fill with Job Bot', '#1565c0');
  const doneBtn = btn('✓ I submitted', '#2e7d32');
  row.append(doneBtn, fillBtn);
  bar.append(panel, row);

  const esc = s => (s || '').replace(/[&<>]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;'}[c]));
  function show(html) { panel.innerHTML = html; panel.style.display = 'block'; }

  // Clicking the Job Bot icon in Chrome's toolbar fills the page too (even where the button isn't shown).
  chrome.runtime.onMessage.addListener(msg => {
    if (msg && msg.cmd === 'fill' && (window === window.top || looksLikeForm())) {
      if (!bar.isConnected) document.documentElement.appendChild(bar);
      fillBtn.click();
    }
  });

  fillBtn.addEventListener('click', async () => {
    fillBtn.disabled = true;
    fillBtn.textContent = 'Filling…';
    try {
      const fields = collectFields();
      const buttons = collectButtons();
      const r = await ask('/plan', {url: location.href, title: document.title,
                                    text: (document.body.innerText || '').slice(0, 6000), fields, buttons});
      if (!r || !r.ok) { show(`<b>Job Bot</b><br>${esc((r && (r.error || (r.data && r.data.error))) || 'No answer from the helper.')}`); return; }
      const done = await apply(r.data.actions);
      let html = `<b>Job Bot filled ${done} field(s).</b> Green = filled. Check them, then submit yourself.`;
      if (r.data.note) html += `<br><i>${esc(r.data.note)}</i>`;
      if (r.data.needs.length) html += `<br><br><b>Needs you:</b><ul style="margin:4px 0 0 18px;padding:0">` +
                                       r.data.needs.map(n => `<li>${esc(n)}</li>`).join('') + '</ul>';
      if (r.data.gemini.length) html += `<br><b>Gemini wrote:</b><ul style="margin:4px 0 0 18px;padding:0">` +
                                        r.data.gemini.map(g => `<li><i>${esc(g.q)}</i><br>${esc(g.a)}</li>`).join('') + '</ul>';
      html += '<br><small>Multi-page form? Click Fill again on each page.</small>';
      show(html);
    } finally {
      fillBtn.disabled = false;
      fillBtn.textContent = '⚡ Fill with Job Bot';
    }
  });

  doneBtn.addEventListener('click', async () => {
    const r = await ask('/applied', {url: location.href, title: document.title});
    show(r && r.ok ? '<b>Saved</b> to applied.csv. The auto-apply bot will skip this job.' : '<b>Couldn\'t save:</b> is the helper running?');
  });

  // Show the button only on pages that look like an application form (checked again as pages change).
  const place = () => { if (looksLikeForm() && !bar.isConnected) document.documentElement.appendChild(bar); };
  place();
  new MutationObserver(() => place()).observe(document.documentElement, {childList: true, subtree: true});
})();
