/* PptRemote — interfaccia telefono.
   Il PC mostra un codice, il telefono lo digita e riceve un token:
   da quel momento i comandi passano senza chiedere più nulla. */
(() => {
  'use strict';

  const TOKEN_KEY = 'ppremote.token';
  const HWND_KEY = 'ppremote.hwnd';
  const POLL_MS = 1400;

  const el = {
    dot: document.getElementById('dot'),
    pcname: document.getElementById('pcname'),
    pairing: document.getElementById('pairing'),
    pairhint: document.getElementById('pairhint'),
    code: document.getElementById('code'),
    codeDisplay: document.getElementById('code-display'),
    codeError: document.getElementById('code-error'),
    codeGrid: document.getElementById('code-grid'),
    denied: document.getElementById('denied'),
    deniedTitle: document.getElementById('denied-title'),
    deniedText: document.getElementById('denied-text'),
    main: document.getElementById('main'),
    windows: document.getElementById('windows'),
    empty: document.getElementById('empty'),
    nowLabel: document.getElementById('now-label'),
    nowSub: document.getElementById('now-sub'),
    dock: document.getElementById('dock'),
    target: document.getElementById('target'),
    toast: document.getElementById('toast'),
    sheet: document.getElementById('sheet'),
    gotobox: document.getElementById('gotobox'),
    gotoDisplay: document.getElementById('goto-display'),
    stage: document.getElementById('stage'),
    slideCurrent: document.getElementById('slide-current'),
    slideTotal: document.getElementById('slide-total'),
    preview: document.getElementById('preview'),
    previewHint: document.getElementById('preview-hint'),
    previewWrap: document.getElementById('preview-wrap'),
    padHint: document.getElementById('pad-hint'),
    laserDot: document.getElementById('laser-dot'),
    btnLaser: document.getElementById('btn-laser'),
    howto: document.getElementById('howto'),
    howtoClose: document.getElementById('howto-close'),
    timerValue: document.getElementById('timer-value'),
    timerToggle: document.getElementById('timer-toggle'),
    timerReset: document.getElementById('timer-reset'),
    tabOpen: document.getElementById('tab-open'),
    tabScreen: document.getElementById('tab-screen'),
    screen: document.getElementById('screen'),
    screenPadHint: document.getElementById('screen-pad-hint'),
    screenDot: document.getElementById('screen-dot'),
    monitors: document.getElementById('monitors'),
    screenAge: document.getElementById('screen-age'),
    screenRefresh: document.getElementById('screen-refresh'),
    screenZoom: document.getElementById('screen-zoom'),
    btnCloseFile: document.getElementById('btn-close-file'),
    tabFiles: document.getElementById('tab-files'),
    search: document.getElementById('search'),
    folderHint: document.getElementById('folder-hint'),
    files: document.getElementById('files'),
    filesEmpty: document.getElementById('files-empty'),
  };

  let token = localStorage.getItem(TOKEN_KEY) || '';
  let selectedHwnd = Number(localStorage.getItem(HWND_KEY) || 0);
  let confirmCloseHwnd = 0;
  let state = null;
  let pollTimer = 0;
  let watchTimer = 0;
  let chain = Promise.resolve();
  let gotoValue = '';
  let started = false;
  let wakeLock = null;
  let pairId = '';
  let codeInput = '';
  let codeBusy = false;
  let activeTab = 'open';
  let previewTimer = 0;
  let timerStart = 0;
  let timerElapsed = 0;
  let timerRunning = false;
  let searchTimer = 0;
  let filesLoaded = false;
  let fileQuery = '';
  let fileSeq = 0;

  /* ---------------------------------------------------------------- utils */
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));

  const show = (node) => node.classList.remove('hidden');
  const hide = (node) => node.classList.add('hidden');

  function vibrate(ms) {
    try { if (navigator.vibrate) navigator.vibrate(ms); } catch { /* ignora */ }
  }

  let toastTimer = 0;
  function toast(text, isError = false) {
    if (!text) return;
    el.toast.textContent = text;
    el.toast.className = 'toast show' + (isError ? ' error' : '');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.toast.className = 'toast'; }, 2200);
  }

  /* ------------------------------------------------------------------ api */
  function apiUrl(path) {
    const sep = path.includes('?') ? '&' : '?';
    return path + sep + 'token=' + encodeURIComponent(token);
  }

  async function api(path, opts = {}) {
    const res = await fetch(apiUrl(path), {
      method: opts.method || 'GET',
      headers: opts.body ? { 'Content-Type': 'application/json' } : undefined,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      cache: 'no-store',
    });
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) {
      token = '';
      localStorage.removeItem(TOKEN_KEY);
      startPair();
      throw new Error('autorizzazione scaduta');
    }
    if (!res.ok) throw new Error(data.error || ('errore ' + res.status));
    return data;
  }

  /* una sola richiesta alla volta: l'ordine dei comandi resta quello dei tocchi */
  function enqueue(fn) {
    chain = chain.then(fn).catch((err) => {
      console.warn(err);
      if (err && err.message && !/autorizzazione/.test(err.message)) toast(err.message, true);
    });
    return chain;
  }

  /* --------------------------------------------------------- collegamento */
  function deviceName() {
    const ua = navigator.userAgent;
    let model = '';
    const m = ua.match(/Android\s[^;]*;\s*([^;)]+?)(?:\s+Build[\s/)]|\))/);
    if (m) model = m[1].replace(/\s*Build.*$/i, '').trim();
    const browser = /EdgA|Edg\//.test(ua) ? 'Edge'
      : /SamsungBrowser/.test(ua) ? 'Samsung'
      : /Firefox/.test(ua) ? 'Firefox'
      : /Chrome/.test(ua) ? 'Chrome'
      : 'Browser';
    const base = model || (/iPhone|iPad/.test(ua) ? 'iPhone' : 'Telefono');
    return base + ' · ' + browser;
  }

  function showDenied(title, text) {
    clearInterval(watchTimer);
    el.deniedTitle.textContent = title;
    el.deniedText.textContent = text;
    hide(el.code);
    hide(el.pairing);
    hide(el.main);
    hide(el.dock);
    show(el.denied);
  }

  async function startPair(retries = 2) {
    clearTimeout(watchTimer);
    clearInterval(pollTimer);
    startPolling.paused = true;
    state = null;
    codeInput = '';
    pairId = '';
    el.codeError.textContent = '';
    el.codeDisplay.classList.remove('error');
    hide(el.main);
    hide(el.dock);
    hide(el.denied);
    hide(el.code);
    show(el.pairing);
    el.pairhint.textContent = 'Un attimo…';
    el.dot.className = 'dot';
    el.pcname.textContent = 'PptRemote';

    for (let attempt = 0; attempt < retries; attempt += 1) {
      try {
        const res = await fetch('/api/pair', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ device: deviceName() }),
        });
        if (!res.ok) throw new Error('http ' + res.status);
        const data = await res.json();
        pairId = data.id;
        hide(el.pairing);
        show(el.code);
        renderCode();
        watchPair();
        return;
      } catch {
        el.pairhint.textContent = 'PC non raggiungibile: controlla di essere sulla stessa Wi-Fi.';
        await new Promise((r) => setTimeout(r, 1200));
      }
    }
  }

  function renderCode() {
    const slots = [];
    for (let i = 0; i < 6; i += 1) slots.push(codeInput[i] || '·');
    el.codeDisplay.innerHTML = esc(slots.slice(0, 3).join(' '))
      + '&nbsp;&nbsp;&nbsp;' + esc(slots.slice(3).join(' '));
  }

  function setKeypadEnabled(on) {
    el.codeGrid.querySelectorAll('button').forEach((b) => { b.disabled = !on; });
  }

  async function submitCode() {
    if (codeBusy) return;
    codeBusy = true;
    setKeypadEnabled(false);
    try {
      const res = await fetch('/api/pair/confirm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: pairId, code: codeInput }),
      });
      const data = await res.json().catch(() => ({}));

      if (res.ok && data.token) {
        clearTimeout(watchTimer);
        token = data.token;
        localStorage.setItem(TOKEN_KEY, token);
        hide(el.code);
        hide(el.denied);
        show(el.main);
        show(el.dock);
        started = true;
        startPolling();
        enqueue(loadState);
        vibrate(25);
        return;
      }
      if (res.status === 403) {
        const left = data.attemptsLeft;
        el.codeError.textContent = 'Codice errato'
          + (left != null ? ' · altri ' + left + ' tentativi' : '');
        el.codeDisplay.classList.add('error', 'shake');
        setTimeout(() => el.codeDisplay.classList.remove('shake'), 400);
        codeInput = '';
        renderCode();
        vibrate(70);
        return;
      }
      if (res.status === 429) {
        showDenied('Troppi tentativi', 'Per sicurezza il PC ha bloccato la richiesta. Genera un nuovo codice.');
        return;
      }
      if (res.status === 410) {
        showDenied('Codice non più valido', 'Il codice è scaduto o è stato annullato sul PC. Genera un nuovo codice.');
        return;
      }
      el.codeError.textContent = data.error || 'Risposta inattesa dal PC';
    } catch {
      el.codeError.textContent = 'PC non raggiungibile';
    } finally {
      codeBusy = false;
      setKeypadEnabled(true);
    }
  }

  /* se il PC annulla la richiesta, il telefono lo scopre subito */
  function watchPair() {
    clearTimeout(watchTimer);
    watchTimer = setTimeout(async () => {
      if (!pairId || el.code.classList.contains('hidden')) return;
      try {
        const res = await fetch('/api/pair?id=' + encodeURIComponent(pairId));
        const data = await res.json();
        if (data.status === 'denied') {
          showDenied('Richiesta annullata', 'Il PC ha annullato il collegamento.');
          return;
        }
        if (data.status === 'expired' || data.status === 'locked') {
          showDenied('Codice scaduto', 'Genera un nuovo codice dal telefono: il PC ne mostrerà un altro.');
          return;
        }
        if (data.status === 'approved' && data.token) {
          token = data.token;
          localStorage.setItem(TOKEN_KEY, token);
          hide(el.code);
          show(el.main);
          show(el.dock);
          started = true;
          startPolling();
          enqueue(loadState);
          return;
        }
      } catch { /* riprova al prossimo giro */ }
      watchPair();
    }, 2000);
  }

  /* ---------------------------------------------------------------- stato */
  async function loadState() {
    try {
      const data = await api('/api/state');
      state = data;
      el.dot.className = 'dot online';
      if (data.pc) el.pcname.textContent = data.pc;
      render();
    } catch (err) {
      if (/autorizzazione/.test(err.message || '')) return;
      el.dot.className = 'dot offline';
      if (!state) {
        el.nowLabel.textContent = 'PC non raggiungibile';
        el.nowSub.textContent = 'Controlla la Wi-Fi e che il server sia aperto sul PC.';
        el.empty.classList.remove('hidden');
      }
    }
  }

  function startPolling() {
    startPolling.paused = false;
    clearInterval(pollTimer);
    pollTimer = setInterval(() => {
      if (document.hidden || startPolling.paused) return;
      enqueue(loadState);
    }, POLL_MS);
  }

  /* ------------------------------------------------------------- selezione */
  function resolveSelection() {
    const wins = (state && state.windows) || [];
    if (!wins.length) { selectedHwnd = 0; return; }
    if (wins.some((w) => w.hwnd === selectedHwnd)) return;
    const pick = wins.find((w) => w.kind === 'show' && w.foreground)
      || wins.find((w) => w.kind === 'show')
      || wins.find((w) => w.foreground)
      || wins[0];
    selectedHwnd = pick.hwnd;
    localStorage.setItem(HWND_KEY, String(selectedHwnd));
  }

  function prettyTitle(title, app) {
    const t = String(title || '')
      .replace(/^(PowerPoint Slide Show|Presentazione)\s*[-–]\s*/i, '')
      .replace(/\s*[-–]\s*(PowerPoint|LibreOffice Impress|LibreOffice|WPS Presentation|WPS Office)\s*$/i, '')
      .replace(/^\[(.*)\]$/, '$1')
      .replace(/^Presentazione\d+$/i, 'Presentazione senza nome');
    return t.trim() || String(title || app || 'Finestra');
  }

  function appClass(app) {
    if (app === 'PowerPoint') return 'ppt';
    if (app === 'LibreOffice') return 'lo';
    if (app === 'WPS Office') return 'wps';
    return '';
  }

  function monitorLabel(index) {
    const mon = ((state && state.monitors) || []).find((m) => m.index === index);
    if (!mon) return '';
    return mon.primary ? 'schermo ' + mon.index : 'schermo ' + mon.index + ' (proiettore)';
  }

  /* -------------------------------------------------------------- disegno */
  function render() {
    if (!state) return;
    resolveSelection();
    const wins = state.windows || [];

    el.windows.innerHTML = wins.map((w) => {
      const cls = ['win'];
      if (w.hwnd === selectedHwnd) cls.push('selected');
      if (w.kind === 'show') cls.push('showing');
      const pills = [];
      if (w.kind === 'show') pills.push('<span class="pill">in presentazione</span>');
      if (w.foreground) pills.push('<span class="pill fg">primo piano</span>');
      const mon = monitorLabel(w.monitor);
      const meta = [w.app, mon].filter(Boolean).join(' · ');
      const tail = w.hwnd === confirmCloseHwnd
        ? '<div class="close-confirm">'
          + '<button class="yes" data-close-confirm="' + w.hwnd + '">Chiudi</button>'
          + '<button data-close-cancel="1">No</button></div>'
        : '<button class="win-close" data-close="' + w.hwnd + '" aria-label="Chiudi">✕</button>';
      return '<article class="' + cls.join(' ') + '" data-hwnd="' + w.hwnd + '">'
        + '<div class="badge ' + appClass(w.app) + '">' + esc((w.app || '??').slice(0, 2).toUpperCase()) + '</div>'
        + '<div class="win-body">'
        + '<div class="win-title">' + esc(prettyTitle(w.title, w.app)) + '</div>'
        + '<div class="win-meta">' + esc(meta) + (pills.length ? ' ' + pills.join(' ') : '') + '</div>'
        + '</div>' + tail + '</article>';
    }).join('');

    el.empty.classList.toggle('hidden', wins.length > 0);
    el.windows.classList.toggle('hidden', wins.length === 0);

    const selected = wins.find((w) => w.hwnd === selectedHwnd);
    const showWin = wins.find((w) => w.kind === 'show');

    if (showWin) {
      el.nowLabel.textContent = 'In presentazione: ' + prettyTitle(showWin.title, showWin.app);
      const where = monitorLabel(showWin.monitor);
      el.nowSub.textContent = (where ? where + ' · ' : '') + (showWin.app || '');
    } else if (selected) {
      el.nowLabel.textContent = prettyTitle(selected.title, selected.app);
      el.nowSub.textContent = 'Pronta · premi F5 per avviare la presentazione';
    } else {
      el.nowLabel.textContent = 'Nessuna presentazione';
      el.nowSub.textContent = 'Apri un file sul PC, poi tocca ⟳.';
    }

    el.target.textContent = selected
      ? 'Comandi → ' + prettyTitle(selected.title, selected.app)
      : 'Nessuna finestra selezionata';

    const noWin = wins.length === 0;
    document.querySelectorAll('#dock .key').forEach((b) => { b.disabled = noWin; });

    renderStage(wins);
  }

  /* ------------------------------------------------------- palco / stage */
  let previewHwnd = 0;

  function renderStage(wins) {
    const showWin = wins.find((w) => w.kind === 'show');
    if (!showWin) {
      el.stage.classList.add('hidden');
      stopPreview();
      setLaser(false, true);
      return;
    }
    el.stage.classList.remove('hidden');
    const slide = showWin.slide || {};
    el.slideCurrent.textContent = slide.current > 0 ? slide.current : '–';
    el.slideTotal.textContent = slide.total > 0 ? slide.total : '–';
    startPreview(showWin.hwnd);
  }

  /* ------------------------------------------------------ laser / puntatore */
  let laserOn = false;

  function setLaser(on, silent) {
    laserOn = on;
    el.btnLaser.classList.toggle('on', on);
    el.btnLaser.textContent = on ? '🔴 Laser ON' : '🔴 Laser';
    el.previewWrap.classList.toggle('laser', on);
    if (!silent) padHintShow();
  }

  function padHintShow() {
    el.padHint.classList.remove('faded');
    el.padHint.querySelector('.pad-hint-big').textContent = laserOn
      ? '👆 Tocca dove vuoi il puntatore'
      : '👆 Tocca qui';
    el.padHint.querySelector('.pad-hint-small').textContent = laserOn
      ? 'il pallino rosso segue il tuo dito'
      : 'il puntatore del PC va dove tocchi';
  }

  el.btnLaser.addEventListener('click', () => {
    vibrate(10);
    act('laser');
    setLaser(!laserOn);
  });

  /* guida rapida: si vede finché non dici "ho capito" */
  const HOWTO_KEY = 'ppremote.howto.hidden';
  if (localStorage.getItem(HOWTO_KEY) === '1') {
    el.howto.classList.add('hidden');
  } else {
    el.howto.classList.remove('hidden');
  }
  el.howtoClose.addEventListener('click', () => {
    localStorage.setItem(HOWTO_KEY, '1');
    el.howto.classList.add('hidden');
  });

  /* toccare l'anteprima = portare il puntatore lì (ottimo per il laser) */
  let absPending = null;
  let absTimer = 0;

  function absSend() {
    if (!absPending) return;
    const { x, y } = absPending;
    absPending = null;
    fetch(apiUrl('/api/pointer'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'abs', x, y }),
      cache: 'no-store',
    }).catch(() => {});
  }

  function absFromEvent(ev) {
    const rect = el.preview.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    absPending = {
      x: (ev.clientX - rect.left) / rect.width,
      y: (ev.clientY - rect.top) / rect.height,
    };
    // pallino di conferma sotto il dito
    el.laserDot.style.left = (ev.clientX - rect.left) + 'px';
    el.laserDot.style.top = (ev.clientY - rect.top) + 'px';
    el.laserDot.classList.add('on');
    el.padHint.classList.add('faded');
    if (!absTimer) absTimer = setInterval(() => {
      if (!absPending) { clearInterval(absTimer); absTimer = 0; return; }
      absSend();
    }, 70);
  }

  el.preview.addEventListener('pointerdown', (ev) => {
    ev.preventDefault();
    el.preview.setPointerCapture(ev.pointerId);
    el.preview.classList.add('active');
    absFromEvent(ev);
  });
  el.preview.addEventListener('pointermove', (ev) => {
    if (!el.preview.classList.contains('active')) return;
    ev.preventDefault();
    absFromEvent(ev);
  });
  const previewStop = () => {
    el.preview.classList.remove('active');
    absSend();
  };
  el.preview.addEventListener('pointerup', previewStop);
  el.preview.addEventListener('pointercancel', previewStop);

  function startPreview(hwnd) {
    if (previewTimer && previewHwnd === hwnd) return;
    stopPreview();
    previewHwnd = hwnd;
    const tick = () => {
      if (document.hidden || activeTab !== 'open') return;
      const url = '/api/preview?token=' + encodeURIComponent(token)
        + '&hwnd=' + hwnd + '&t=' + Date.now();
      const probe = new Image();
      probe.onload = () => {
        el.preview.src = probe.src;
        el.preview.classList.remove('hidden');
        el.previewHint.classList.add('hidden');
      };
      probe.onerror = () => {
        el.preview.classList.add('hidden');
        el.previewHint.classList.remove('hidden');
        el.previewHint.textContent = 'Anteprima non disponibile per questa finestra.';
      };
      probe.src = url;
    };
    tick();
    previewTimer = setInterval(tick, 2200);
  }

  function stopPreview() {
    clearInterval(previewTimer);
    previewTimer = 0;
    previewHwnd = 0;
  }

  function timerRender() {
    const ms = timerElapsed + (timerRunning ? Date.now() - timerStart : 0);
    const total = Math.max(0, Math.floor(ms / 1000));
    const mm = String(Math.floor(total / 60)).padStart(2, '0');
    const ss = String(total % 60).padStart(2, '0');
    el.timerValue.textContent = mm + ':' + ss;
    el.timerToggle.textContent = timerRunning ? '❚❚' : '▶';
  }

  function timerToggle() {
    if (timerRunning) {
      timerElapsed += Date.now() - timerStart;
      timerRunning = false;
    } else {
      timerStart = Date.now();
      timerRunning = true;
    }
    timerRender();
  }

  function timerReset() {
    timerElapsed = 0;
    timerStart = Date.now();
    timerRunning = false;
    timerRender();
  }

  function timerAutoStart() {
    if (!timerRunning && timerElapsed === 0) {
      timerStart = Date.now();
      timerRunning = true;
      timerRender();
    }
  }

  el.timerToggle.addEventListener('click', () => { vibrate(6); timerToggle(); });
  el.timerReset.addEventListener('click', () => { vibrate(6); timerReset(); });
  setInterval(timerRender, 500);
  timerRender();

  /* --------------------------------------------------- schede e file */
  function setTab(name) {
    activeTab = name;
    document.querySelectorAll('.tab').forEach((tab) => {
      tab.classList.toggle('active', tab.dataset.tab === name);
    });
    el.tabOpen.classList.toggle('hidden', name !== 'open');
    el.tabFiles.classList.toggle('hidden', name !== 'files');
    el.tabScreen.classList.toggle('hidden', name !== 'screen');
    if (name === 'screen') startScreen();
    else {
      stopScreen();
      if (name === 'open') renderStage((state && state.windows) || []);
      else if (name === 'files' && !filesLoaded) loadFiles('');
    }
  }

  /* ------------------------------------------------- specchio dello schermo */
  let screenTimer = 0;
  let screenMonitor = Number(localStorage.getItem('ppremote.monitor') || 0) || 1;

  function renderMonitors() {
    const mons = (state && state.monitors) || [];
    el.monitors.innerHTML = mons.map((m) => (
      '<button data-monitor="' + m.index + '" class="'
      + (m.index === screenMonitor ? 'on' : '') + '">Schermo ' + m.index + '</button>'
    )).join('');
  }

  function startScreen() {
    renderMonitors();
    clearInterval(screenTimer);
    screenTick();
    screenTimer = setInterval(screenTick, 700);
  }

  let lastScreenAt = 0;
  function screenTick() {
    if (document.hidden || activeTab !== 'screen') return;
    const probe = new Image();
    probe.onload = () => {
      el.screen.src = probe.src;
      el.screenPadHint.classList.add('faded');
      lastScreenAt = Date.now();
      el.screenAge.textContent = 'aggiornato ora';
    };
    probe.onerror = () => { el.screenAge.textContent = 'schermo non disponibile'; };
    probe.src = '/api/screen?token=' + encodeURIComponent(token)
      + '&monitor=' + screenMonitor + '&w=620&t=' + Date.now();
  }

  setInterval(() => {
    if (activeTab !== 'screen' || !lastScreenAt) return;
    const age = Math.round((Date.now() - lastScreenAt) / 1000);
    el.screenAge.textContent = age <= 1 ? 'aggiornato ora' : 'aggiornato ' + age + 's fa';
  }, 1000);

  el.screenZoom.addEventListener('click', () => {
    // apre l'immagine a piena risoluzione: lì il browser permette lo zoom con due dita
    const url = '/api/screen?token=' + encodeURIComponent(token)
      + '&monitor=' + screenMonitor + '&w=1400&t=' + Date.now();
    window.open(url, '_blank');
  });

  el.screenRefresh.addEventListener('click', () => {
    vibrate(6);
    lastScreenAt = 0;
    el.screenAge.textContent = 'aggiorno…';
    screenTick();
  });

  function stopScreen() {
    clearInterval(screenTimer);
    screenTimer = 0;
  }

  el.monitors.addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-monitor]');
    if (!btn) return;
    screenMonitor = Number(btn.dataset.monitor) || 1;
    localStorage.setItem('ppremote.monitor', String(screenMonitor));
    renderMonitors();
    vibrate(6);
    startScreen();
  });

  /* toccare lo specchio = spostare il mouse lì */
  let screenDotTimer = 0;
  function screenPoint(ev) {
    const rect = el.screen.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const x = (ev.clientX - rect.left) / rect.width;
    const y = (ev.clientY - rect.top) / rect.height;
    el.screenDot.style.left = (ev.clientX - rect.left) + 'px';
    el.screenDot.style.top = (ev.clientY - rect.top) + 'px';
    el.screenDot.classList.add('on');
    clearTimeout(screenDotTimer);
    screenDotTimer = setTimeout(() => el.screenDot.classList.remove('on'), 1500);
    fetch(apiUrl('/api/pointer'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        action: 'abs', space: 'screen', monitor: screenMonitor, x, y,
      }),
      cache: 'no-store',
    }).catch(() => {});
  }

  el.screen.addEventListener('pointerdown', (ev) => {
    ev.preventDefault();
    el.screen.setPointerCapture(ev.pointerId);
    el.screen.classList.add('active');
    screenPoint(ev);
  });
  el.screen.addEventListener('pointermove', (ev) => {
    if (!el.screen.classList.contains('active')) return;
    ev.preventDefault();
    screenPoint(ev);
  });
  const screenStop = () => el.screen.classList.remove('active');
  el.screen.addEventListener('pointerup', screenStop);
  el.screen.addEventListener('pointercancel', screenStop);

  /* --------------------------------------------------- swipe tra le schede */
  let swipeStart = null;
  el.main.addEventListener('pointerdown', (ev) => {
    if (ev.target.closest('#preview-wrap, #screen-wrap, input, textarea, button, .file-actions')) {
      swipeStart = null;
      return;
    }
    swipeStart = { x: ev.clientX, y: ev.clientY, t: Date.now() };
  });

  function endSwipe(ev) {
    if (!swipeStart) return;
    const dx = ev.clientX - swipeStart.x;
    const dy = ev.clientY - swipeStart.y;
    const elapsed = Date.now() - swipeStart.t;
    swipeStart = null;
    if (elapsed > 900 || Math.abs(dx) < 55 || Math.abs(dx) < Math.abs(dy) * 1.5) return;
    const order = ['open', 'files', 'screen'];
    const idx = order.indexOf(activeTab);
    const nextIdx = dx < 0 ? Math.min(idx + 1, order.length - 1) : Math.max(idx - 1, 0);
    if (nextIdx !== idx) {
      vibrate(8);
      setTab(order[nextIdx]);
    }
  }

  el.main.addEventListener('pointerup', endSwipe);
  el.main.addEventListener('pointercancel', () => { swipeStart = null; });

  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => setTab(tab.dataset.tab));
  });

  function formatSize(bytes) {
    if (!bytes) return '';
    const mb = bytes / (1024 * 1024);
    return mb >= 1 ? mb.toFixed(1) + ' MB' : Math.max(1, Math.round(bytes / 1024)) + ' KB';
  }

  function renderFiles(files, info) {
    el.files.innerHTML = files.map((f) => {
      const meta = [
        f.rel || 'cartella principale',
        f.slides ? f.slides + ' slide' : '',
        formatSize(f.size),
      ].filter(Boolean).join(' · ');
      const path = esc(f.path);
      return '<article class="file">'
        + '<div class="file-body">'
        + '<div class="file-name">' + esc(f.name) + '</div>'
        + '<div class="file-meta">' + esc(meta) + '</div>'
        + '</div>'
        + '<div class="file-actions">'
        + '<button data-file="' + path + '" data-present="0">Apri</button>'
        + '<button class="go" data-file="' + path + '" data-present="1">Presenta</button>'
        + '</div></article>';
    }).join('');
    el.filesEmpty.classList.toggle('hidden', files.length > 0);
    if (files.length === 0) {
      const where = esc(info.folder || '—');
      const count = info.total != null ? info.total : 0;
      if (fileQuery) {
        el.filesEmpty.innerHTML = 'Nessun file con <b>' + esc(fileQuery) + '</b> in questa cartella.'
          + '<br><span class="muted">' + where + ' · ' + count + ' presentazioni</span>'
          + '<br><span class="muted">Se i tuoi file sono altrove, sul PC premi «Scegli…» '
          + 'accanto a «Cartella file».</span>';
      } else {
        el.filesEmpty.innerHTML = 'Nessuna presentazione in questa cartella.'
          + '<br><span class="muted">' + where + '</span>'
          + '<br><span class="muted">Sul PC premi «Scegli…» per indicare dove tieni i file.</span>';
      }
    }
  }

  async function loadFiles(query) {
    fileQuery = (query || '').trim();
    const seq = ++fileSeq;
    try {
      const data = await api('/api/files?q=' + encodeURIComponent(fileQuery));
      if (seq !== fileSeq) return; // risposta superata da una ricerca più recente
      filesLoaded = true;
      const files = data.files || [];
      const total = data.total != null ? data.total : files.length;
      el.folderHint.textContent = 'Cartella: ' + (data.folder || '—') + ' · ' + total
        + ' presentazioni' + (data.truncated ? ' (scansione parziale)' : '');
      renderFiles(files, { folder: data.folder, total });
    } catch (err) {
      if (seq === fileSeq) toast(err.message, true);
    }
  }

  function openFile(path, present) {
    vibrate(10);
    enqueue(async () => {
      const res = await api('/api/files/open', {
        method: 'POST',
        body: { path, present },
      });
      if (res.state) {
        state = res.state;
        el.dot.className = 'dot online';
        render();
      }
      toast(res.note || (present ? 'Presentazione avviata' : 'File aperto'));
      if (res.warning) setTimeout(() => toast(res.warning, true), 1400);
      if (present) {
        timerAutoStart();
        setTab('open');
      }
    });
  }

  el.files.addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-file]');
    if (!btn) return;
    openFile(btn.dataset.file, btn.dataset.present === '1');
  });

  el.search.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => loadFiles(el.search.value), 320);
  });

  /* ---------------------------------------------------------------- azioni */
  function act(action, extra = {}) {
    if (!state || !(state.windows || []).length) {
      toast('Nessuna presentazione aperta sul PC', true);
      return;
    }
    vibrate(9);
    enqueue(async () => {
      const res = await api('/api/action', {
        method: 'POST',
        body: Object.assign({ action, hwnd: selectedHwnd }, extra),
      });
      if (res.state) {
        state = res.state;
        el.dot.className = 'dot online';
        render();
      }
      if (res.note) toast(res.note);
      if (action === 'present' || action === 'present_current') timerAutoStart();
    });
  }

  /* -------------------------------------------------------------- eventi */
  el.windows.addEventListener('click', (ev) => {
    const closeBtn = ev.target.closest('[data-close]');
    if (closeBtn) {
      confirmCloseHwnd = Number(closeBtn.dataset.close) || 0;
      render();
      return;
    }
    if (ev.target.closest('[data-close-cancel]')) {
      confirmCloseHwnd = 0;
      render();
      return;
    }
    const confirmBtn = ev.target.closest('[data-close-confirm]');
    if (confirmBtn) {
      const target = Number(confirmBtn.dataset.closeConfirm) || 0;
      confirmCloseHwnd = 0;
      vibrate(15);
      act('close', { hwnd: target });
      return;
    }
    const card = ev.target.closest('.win');
    if (!card) return;
    const hwnd = Number(card.dataset.hwnd);
    if (!hwnd) return;
    selectedHwnd = hwnd;
    localStorage.setItem(HWND_KEY, String(hwnd));
    render();
    act('focus');
  });

  el.dock.addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-action]');
    if (!btn) return;
    ev.preventDefault();
    act(btn.dataset.action);
  });

  el.btnCloseFile.addEventListener('click', () => {
    if (!selectedHwnd) { toast('Nessun file selezionato', true); return; }
    vibrate(20);
    act('close', { hwnd: selectedHwnd });
  });

  document.getElementById('btn-refresh').addEventListener('click', () => {
    const icon = document.getElementById('btn-refresh');
    icon.classList.remove('spin');
    void icon.offsetWidth;
    icon.classList.add('spin');
    vibrate(5);
    enqueue(loadState);
  });

  document.getElementById('btn-again').addEventListener('click', () => startPair());
  document.getElementById('btn-retry').addEventListener('click', () => startPair());

  /* tastierino del codice */
  el.codeGrid.addEventListener('click', (ev) => {
    const digit = ev.target.closest('[data-digit]');
    if (digit) {
      if (codeInput.length < 6) {
        codeInput += digit.dataset.digit;
        vibrate(6);
        renderCode();
        if (codeInput.length === 6) submitCode();
      }
      return;
    }
    if (ev.target.id === 'code-clear') { codeInput = ''; renderCode(); return; }
    if (ev.target.id === 'code-back') { codeInput = codeInput.slice(0, -1); renderCode(); }
  });

  document.getElementById('code-cancel').addEventListener('click', () => {
    pairId = '';
    clearTimeout(watchTimer);
    startPair();
  });

  /* menu a tendina */
  const sheet = el.sheet;
  document.getElementById('btn-menu').addEventListener('click', () => {
    document.getElementById('sheet-note').textContent = state
      ? (state.pc || '') + ' · versione ' + (state.version || '')
      : '';
    show(sheet);
  });
  document.getElementById('sheet-close').addEventListener('click', () => hide(sheet));
  sheet.addEventListener('click', (ev) => { if (ev.target === sheet) hide(sheet); });

  document.getElementById('sheet-refresh').addEventListener('click', () => {
    hide(sheet);
    enqueue(loadState);
  });

  document.getElementById('sheet-fullscreen').addEventListener('click', async () => {
    hide(sheet);
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await document.documentElement.requestFullscreen();
    } catch { toast('Schermo intero non disponibile', true); }
  });

  document.getElementById('sheet-unlink').addEventListener('click', async () => {
    hide(sheet);
    try { await api('/api/unlink', { method: 'POST', body: {} }); } catch { /* pazienza */ }
    token = '';
    localStorage.removeItem(TOKEN_KEY);
    startPair();
  });

  /* vai a slide */
  const gotobox = el.gotobox;
  function openGoto() {
    gotoValue = '';
    el.gotoDisplay.textContent = '—';
    show(gotobox);
  }
  document.getElementById('btn-goto').addEventListener('click', openGoto);
  document.getElementById('goto-close').addEventListener('click', () => hide(gotobox));
  gotobox.addEventListener('click', (ev) => { if (ev.target === gotobox) hide(gotobox); });

  gotobox.addEventListener('click', (ev) => {
    const digitBtn = ev.target.closest('[data-digit]');
    if (digitBtn) {
      if (gotoValue.length < 4) gotoValue += digitBtn.dataset.digit;
      el.gotoDisplay.textContent = gotoValue || '—';
      vibrate(5);
      return;
    }
    if (ev.target.id === 'goto-clear') {
      gotoValue = '';
      el.gotoDisplay.textContent = '—';
      return;
    }
    if (ev.target.id === 'goto-go') {
      const n = Number(gotoValue);
      hide(gotobox);
      if (n > 0) act('goto', { slide: n });
    }
  });

  /* tenere acceso lo schermo mentre si presenta */
  async function keepAwake() {
    try {
      if ('wakeLock' in navigator && !wakeLock) {
        wakeLock = await navigator.wakeLock.request('screen');
        wakeLock.addEventListener('release', () => { wakeLock = null; });
      }
    } catch { /* alcuni browser lo negano: non è un problema */ }
  }
  document.addEventListener('click', keepAwake, { once: true });

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && started) {
      keepAwake();
      enqueue(loadState);
    }
  });

  /* ---------------------------------------------------------------- avvio */
  if (!token) {
    startPair();
  } else {
    started = true;
    hide(el.pairing);
    show(el.main);
    show(el.dock);
    startPolling();
    enqueue(loadState);
  }
})();
