// Down watch: while the app is open, check the server every 4 s; if it can't be reached (or the web proxy says the
// app is down) for 10 s straight, swap the screen for the down screen (the service worker keeps it), which comes
// back here by itself when the server answers again. A restart (a few seconds) doesn't trip it.
(function () {
  var me = document.currentScript, PING = me.getAttribute('data-ping'), OFF = me.getAttribute('data-offline');
  var EVERY = 4000, DOWN_MS = 10000, LIMIT = 4000, lastOk = Date.now(), fails = 0, busy = false, gone = false;
  function down() {
    if (gone || !(navigator.serviceWorker && navigator.serviceWorker.controller)) return;   // no saved down screen yet: stay
    gone = true;
    location.replace(OFF + '?down=1&back=' + encodeURIComponent(location.pathname + location.search + location.hash));
  }
  function check() {
    if (busy || gone || document.hidden) return;
    busy = true;
    var ac = window.AbortController ? new AbortController() : null, t = setTimeout(function () { if (ac) ac.abort(); }, LIMIT);
    fetch(PING + (PING.indexOf('?') < 0 ? '?' : '&') + 't=' + Date.now(), { cache: 'no-store', signal: ac ? ac.signal : undefined })
      .then(function (r) { if (r.status >= 502 && r.status <= 504) throw new Error('down'); lastOk = Date.now(); fails = 0; })
      .catch(function () { fails++; if (fails >= 2 && Date.now() - lastOk >= DOWN_MS) down(); })   // 10 s since it last answered
      .then(function () { clearTimeout(t); busy = false; });
  }
  setInterval(check, EVERY);
  setTimeout(check, 2500);
  // back from the background (phone locked, tab hidden): start counting afresh, the network may still be waking
  document.addEventListener('visibilitychange', function () { if (!document.hidden) { lastOk = Date.now(); fails = 0; setTimeout(check, 1500); } });
  window.addEventListener('offline', check);
})();
