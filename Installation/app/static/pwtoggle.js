'use strict';

// Eye button on every password field: show / hide the password.
// Observes the page so fields created later (account, users) get it as well.
(function () {
  const EYE = '<svg viewBox="0 0 24 24"><path d="M12 5C6.5 5 2.7 9.1 1.5 12c1.2 2.9 5 7 10.5 7s9.3-4.1 10.5-7C21.3 9.1 17.5 5 12 5zm0 11.5a4.5 4.5 0 1 1 0-9 4.5 4.5 0 0 1 0 9zm0-7a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5z"/></svg>';
  const EYE_OFF = '<svg viewBox="0 0 24 24"><path d="M2.3 3.7 3.7 2.3l18 18-1.4 1.4-3.3-3.3A11.6 11.6 0 0 1 12 19C6.5 19 2.7 14.9 1.5 12a13 13 0 0 1 4.2-5.3L2.3 3.7zm5 5A4.5 4.5 0 0 0 15.3 15l-1.6-1.6a2.5 2.5 0 0 1-3.1-3.1L7.3 8.7zM12 5c5.5 0 9.3 4.1 10.5 7a13.2 13.2 0 0 1-3 4.2l-2.9-2.9A4.5 4.5 0 0 0 10.7 7.4L8.9 5.6A11 11 0 0 1 12 5z"/></svg>';
  const label = (shown) => (typeof t === 'function' ? t(shown ? 'pw.hide' : 'pw.show') : '');

  function enhance(input) {
    if (input.dataset.pwEye) return;
    input.dataset.pwEye = '1';
    const wrap = document.createElement('span');
    wrap.className = 'pw-wrap';
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'pw-eye';
    btn.tabIndex = -1;
    btn.innerHTML = EYE;
    btn.title = label(false);
    btn.setAttribute('aria-label', label(false));
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      const shown = input.type === 'password';
      input.type = shown ? 'text' : 'password';
      btn.innerHTML = shown ? EYE_OFF : EYE;
      btn.title = label(shown);
      btn.setAttribute('aria-label', label(shown));
      input.focus();
    });
    wrap.appendChild(btn);
  }

  const scan = (root) => root.querySelectorAll?.('input[type=password]').forEach(enhance);
  scan(document);
  new MutationObserver((records) => {
    for (const r of records) r.addedNodes.forEach((n) => n.nodeType === 1 && scan(n.matches?.('input[type=password]') ? n.parentNode : n));
  }).observe(document.body, { childList: true, subtree: true });
})();
