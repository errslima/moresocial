// Progressive enhancement only: every action also works as a plain form post.
for (const button of document.querySelectorAll('button[data-copy]')) {
  button.addEventListener('click', async () => {
    const id = button.dataset.copy;
    const area = document.getElementById(id);
    const status = document.querySelector(`[data-copy-status="${id}"]`);
    try {
      await navigator.clipboard.writeText(area.value);
      if (status) status.textContent = 'Copied the text shown above. Nothing was sent; paste it into your messaging app.';
    } catch {
      area.select();
      if (status) status.textContent = 'Select-and-copy the text above (automatic copy is blocked by the browser).';
    }
  });
}
for (const form of document.querySelectorAll('form[data-busy]')) {
  form.addEventListener('submit', () => {
    const b = form.querySelector('button[type=submit],button:not([type])');
    // An icon button keeps its icon; only its screen-reader label changes.
    if (b) { b.disabled = true; (b.querySelector('.sr') || b).textContent = form.dataset.busy; }
  });
}
const wa = document.getElementById('wa');
if (wa && wa.dataset.active === 'yes') {
  const state = wa.querySelector('[data-wa-state]'), qr = wa.querySelector('[data-wa-qr]'), hist = wa.querySelector('[data-wa-history]');
  const labels = {pending: 'Starting the connection…', starting: 'Starting the connection…', capacity: 'All WhatsApp slots are in use. Try again later.',
    pairing: 'Scan this code with WhatsApp on your phone: Settings › Linked devices › Link a device.', loading: 'Linked. Loading chats…',
    ready: 'Linked and syncing.', disconnected: 'WhatsApp disconnected. It will retry; you can also unlink and link again.',
    auth_failure: 'WhatsApp rejected the saved session. Unlink and delete the session, then link again.', error: 'Connection problem. Retrying.',
    stopped: 'Not linked.', not_connected: 'Not linked.'};
  let delay = 2000;
  const poll = async () => {
    try {
      const r = await fetch(wa.dataset.statusUrl, {headers: {Accept: 'application/json'}, cache: 'no-store', credentials: 'same-origin'});
      if (r.ok) {
        const s = await r.json();
        state.textContent = labels[s.state] || 'Checking status…';
        if (s.qr) { qr.src = s.qr; qr.hidden = false; } else { qr.hidden = true; qr.removeAttribute('src'); }
        const h = s.history || {};
        hist.textContent = h.history_chats != null ? `History: ${h.history_done ?? 0} of ${h.history_chats} recent chats loaded.` : '';
        delay = s.state === 'ready' ? 15000 : 3000;
      } else {
        // Show only the server's opaque request ID; nothing from the page is reported anywhere.
        const ref = r.headers.get('X-Request-ID') || '';
        state.textContent = 'Status is temporarily unavailable.' + (/^[0-9a-f]{32}$/.test(ref) ? ` Reference: ${ref}` : '');
        delay = Math.min(delay * 2, 30000);
      }
    } catch { delay = Math.min(delay * 2, 30000); }
    setTimeout(poll, delay);
  };
  poll();
}
// The sidebar is a <details> so the compact menu works without JavaScript. On wide screens
// it is always expanded (browsers without ::details-content need it open to show the links).
const appnav = document.querySelector('[data-appnav]');
if (appnav) {
  const narrow = matchMedia('(max-width: 900px)');
  const sync = () => { appnav.open = !narrow.matches; };
  if (!narrow.matches) sync();
  narrow.addEventListener('change', sync);
}
// The composer grows with its text up to its CSS max-height, where browsers lack field-sizing.
if (!CSS.supports('field-sizing', 'content')) {
  for (const area of document.querySelectorAll('textarea[data-autogrow]')) {
    const fit = () => { area.style.height = 'auto'; area.style.height = area.scrollHeight + 'px'; };
    area.addEventListener('input', fit);
    fit();
  }
}
// Latest WhatsApp history opens at the newest message; older pages keep their position.
const waHistory = document.querySelector('[data-wa-history="latest"]');
if (waHistory) waHistory.scrollTop = waHistory.scrollHeight;
