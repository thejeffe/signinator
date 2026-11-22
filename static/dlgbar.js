// Dialog bars: each dialog's title row becomes a bar that stays at the top while the dialog scrolls, holding its
// buttons as icons (save = a disk, cancel/done = an X, restart, save as new, open in the mixer); the words stay as
// each button's tooltip and spoken label. The buttons are moved, not copied, so the page's own handlers still run.
(function () {
  var S = '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">';
  var ICON = {
    save: S + '<path d="M5 3h11l4 4v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5a2 2 0 0 1 1-2z"/><path d="M8 3v5h7V3"/><rect x="7" y="13" width="10" height="6" rx="1"/></svg>',
    close: S + '<path d="M6 6l12 12M18 6L6 18"/></svg>',
    restart: S + '<path d="M12 3v8"/><path d="M6.3 6.3a8 8 0 1 0 11.4 0"/></svg>',
    copy: S + '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/><path d="M14 11v6M11 14h6"/></svg>',
    mix: S + '<path d="M5 4v16M12 4v16M19 4v16"/><rect x="3" y="7" width="4" height="3" rx="1"/><rect x="10" y="13" width="4" height="3" rx="1"/><rect x="17" y="9" width="4" height="3" rx="1"/></svg>'
  };
  function kind(b) {
    var t = (b.textContent || '').trim().toLowerCase(), id = (b.id || '').toLowerCase();
    if (/save as new|copy/.test(t)) return 'copy';
    if (b.type === 'submit' || t === 'save') return 'save';
    if (/^(cancel|done|close)$/.test(t) || /cancel$|done$/.test(id)) return 'close';
    if (/^restart/.test(t)) return 'restart';
    if (/mix-a-lot|mixer/.test(t)) return 'mix';
    return null;
  }
  var ORDER = { mix: 0, copy: 1, restart: 2, save: 3, close: 4 };
  document.querySelectorAll('dialog').forEach(function (d) {
    var f = d.querySelector('form'), h = f && f.querySelector('h2'), acts = f && f.querySelector('.acts');
    if (!h || !acts || f.querySelector('.dlgbar')) return;
    var bar = document.createElement('div'), tools = document.createElement('div');
    bar.className = 'dlgbar'; tools.className = 'dlgtools';
    f.insertBefore(bar, f.firstChild);
    bar.appendChild(h);
    bar.appendChild(tools);
    var btns = [], msgs = [];
    Array.prototype.forEach.call(acts.querySelectorAll('button'), function (b) { btns.push(b); });
    Array.prototype.forEach.call(acts.querySelectorAll('.msg'), function (m) { msgs.push(m); });
    var prev = acts.previousElementSibling;                           // a message line just above the buttons
    if (prev && prev.classList.contains('msg')) msgs.push(prev);
    btns.sort(function (a, b) { var ka = kind(a), kb = kind(b); return (ka ? ORDER[ka] : -1) - (kb ? ORDER[kb] : -1); });
    btns.forEach(function (b) {
      var k = kind(b);
      if (k) {
        var label = (b.textContent || '').trim() || k;
        if (!b.title) b.title = label;
        b.setAttribute('aria-label', label);
        b.innerHTML = ICON[k];
        b.classList.add('ico', 'ico-' + k);
      }
      tools.appendChild(b);
    });
    msgs.forEach(function (m) { m.classList.add('dlgmsg'); bar.appendChild(m); });
    if (!acts.querySelector('button')) acts.remove();
  });
})();
