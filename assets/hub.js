/* Игротека: аккаунт и облачные сохранения.
   Подключается в <head> каждой игры и меню ДО скриптов игры.
   Если игрок вошёл, сохранения из localStorage копируются на сервер,
   а при открытии игры подтягиваются с сервера. Без сервера всё работает как раньше. */
(function () {
  'use strict';
  var me = document.currentScript;
  var BASE = new URL('../', me.src).href;          // корень игротеки
  var API = BASE + 'api/';
  var LS = window.localStorage;
  var P = Storage.prototype;
  var rawSet = P.setItem, rawRemove = P.removeItem, rawClear = P.clear;
  var USER_KEY = 'gh-user', DIRTY_KEY = 'gh-dirty';
  var own = function (k) { return String(k).indexOf('gh-') === 0; };

  function lsGet(k) { try { return LS.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { rawSet.call(LS, k, v); } catch (e) {} }
  function lsDel(k) { try { rawRemove.call(LS, k); } catch (e) {} }
  function readJSON(k, d) { try { var v = JSON.parse(lsGet(k)); return v == null ? d : v; } catch (e) { return d; } }

  function user() { return readJSON(USER_KEY, null); }
  function dirty() { var d = readJSON(DIRTY_KEY, {}); return d && typeof d === 'object' ? d : {}; }
  function gameKeys() {
    var out = [];
    try { for (var i = 0; i < LS.length; i++) { var k = LS.key(i); if (k != null && !own(k)) out.push(k); } } catch (e) {}
    return out;
  }
  function snapshot() { var o = {}; gameKeys().forEach(function (k) { o[k] = lsGet(k); }); return o; }

  // --- синхронные запросы для старта игры (до её скриптов) ---
  function xhrSync(method, path, body) {
    try {
      var x = new XMLHttpRequest();
      x.open(method, API + path, false);
      x.withCredentials = true;
      if (body) x.setRequestHeader('Content-Type', 'application/json');
      x.send(body ? JSON.stringify(body) : null);
      var j = null; try { j = JSON.parse(x.responseText); } catch (e) {}
      return { status: x.status, json: j };
    } catch (e) { return { status: 0, json: null }; }
  }

  function api(path, body, opts) {
    opts = opts || {};
    var init = { method: body ? 'POST' : 'GET', credentials: 'same-origin', keepalive: !!opts.keepalive };
    if (body) { init.headers = { 'Content-Type': 'application/json' }; init.body = JSON.stringify(body); }
    return fetch(API + path, init).then(function (r) {
      var ct = r.headers.get('Content-Type') || '';
      if (ct.indexOf('json') < 0) throw { status: r.status, offline: true, error: 'Сервер игротеки не запущен' };
      return r.json().then(function (j) { if (!r.ok) { j.status = r.status; throw j; } return j; });
    }, function () { throw { status: 0, offline: true, error: 'Нет связи с сервером' }; });
  }

  // --- отметка изменённых ключей и отправка ---
  var timer = null, sending = false, lastOk = true;
  function mark(k, deleted) {
    if (own(k) || !user()) return;
    var d = dirty(); d[k] = Date.now() + Math.random(); lsSet(DIRTY_KEY, JSON.stringify(d));
    if (!timer) timer = setTimeout(function () { timer = null; flush(); }, 3000);
  }
  function buildPatch() {
    var d = dirty(), set = {}, del = [], n = 0, size = 0;
    Object.keys(d).forEach(function (k) {
      var v = lsGet(k);
      if (v == null) del.push(k); else { set[k] = v; size += v.length; }
      n++;
    });
    return n ? { patch: { set: set, del: del }, keys: d, size: size } : null;
  }
  function clearSent(keys) {
    var d = dirty();
    // ключ, изменённый во время отправки, остаётся «грязным»
    Object.keys(keys).forEach(function (k) { if (d[k] === keys[k]) delete d[k]; });
    lsSet(DIRTY_KEY, JSON.stringify(d));
  }
  function flush(final) {
    if (!user() || sending) return Promise.resolve();
    var b = buildPatch(); if (!b) return Promise.resolve();
    sending = true;
    return api('data', b.patch, { keepalive: final && b.size < 60000 }).then(function () {
      clearSent(b.keys); lastOk = true; status('ok');
    }, function (e) {
      if (e && e.status === 401) { lsDel(USER_KEY); status('out'); }
      else { lastOk = false; status('off'); }
    }).then(function () {
      sending = false;
      // что-то поменялось во время отправки или сервер был недоступен — повторим позже
      if (user() && Object.keys(dirty()).length && !timer) {
        timer = setTimeout(function () { timer = null; flush(); }, lastOk ? 3000 : 30000);
      }
    });
  }

  P.setItem = function (k, v) {
    rawSet.call(this, k, v);
    if (this === LS) mark(String(k), false);
  };
  P.removeItem = function (k) {
    rawRemove.call(this, k);
    if (this === LS) mark(String(k), true);
  };
  P.clear = function () {
    if (this === LS) { var keys = gameKeys(); keys.forEach(function (k) { lsDel(k); mark(k, true); }); return; }
    rawClear.call(this);
  };

  document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'hidden') flush(true); });
  window.addEventListener('pagehide', function () { flush(true); });

  // --- старт: сначала отправить несохранённое, потом взять данные с сервера ---
  var startState = 'guest';
  function pull() {
    if (!user()) return 'guest';
    var d = dirty();
    if (Object.keys(d).length) {
      var b = buildPatch();
      var r0 = xhrSync('POST', 'data', b.patch);
      if (r0.status === 401) { lsDel(USER_KEY); return 'out'; }
      if (r0.status !== 200) return 'off';
      clearSent(b.keys);
    }
    var r = xhrSync('GET', 'data');
    if (r.status === 401) { lsDel(USER_KEY); return 'out'; }
    if (r.status !== 200 || !r.json || !r.json.data) return 'off';
    var data = r.json.data;
    gameKeys().forEach(function (k) { if (!(k in data)) lsDel(k); });
    Object.keys(data).forEach(function (k) { if (lsGet(k) !== data[k]) lsSet(k, data[k]); });
    return 'ok';
  }
  if (!me.hasAttribute('data-no-pull')) startState = pull();

  // --- вход, регистрация и выход (используются меню) ---
  function wipeLocal() { gameKeys().forEach(lsDel); lsDel(DIRTY_KEY); }
  var Hub = {
    base: BASE,
    api: api,
    user: user,
    state: function () { return startState; },
    flush: flush,
    register: function (login, password, name) {
      return api('register', { login: login, password: password, name: name }).then(function (j) {
        lsSet(USER_KEY, JSON.stringify(j.user));
        // прогресс, набранный без аккаунта, переезжает в новый аккаунт
        return api('data', { set: snapshot(), del: [], replace: true }).then(function () { lsDel(DIRTY_KEY); return j.user; });
      });
    },
    login: function (login, password) {
      return api('login', { login: login, password: password }).then(function (j) {
        lsSet(USER_KEY, JSON.stringify(j.user));
        if (!j.hasData) {
          return api('data', { set: snapshot(), del: [], replace: true }).then(function () { lsDel(DIRTY_KEY); return j.user; });
        }
        return api('data').then(function (r) {
          wipeLocal();
          Object.keys(r.data).forEach(function (k) { lsSet(k, r.data[k]); });
          return j.user;
        });
      });
    },
    logout: function () {
      return flush().then(function () { return api('logout', {}); }).catch(function () {}).then(function () {
        lsDel(USER_KEY); wipeLocal();
      });
    },
    forget: function () { lsDel(USER_KEY); wipeLocal(); },
    drop: function () { lsDel(USER_KEY); },
    setUser: function (u) { lsSet(USER_KEY, JSON.stringify(u)); }
  };
  window.GamesHub = Hub;

  // --- кнопка «в меню» внутри игры ---
  var dot = null;
  function status(s) { if (dot) dot.className = 'gh-dot gh-' + s; }
  if (me.hasAttribute('data-home')) {
    lsSet('gh-last', JSON.stringify({ file: location.pathname.split('/').pop(), t: Date.now() }));
    var css = '#gh-home{position:fixed;z-index:2147483000;left:0;top:50%;transform:translateY(-50%);' +
      'display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;width:30px;height:46px;padding:0;' +
      'border-radius:0 12px 12px 0;border:1px solid rgba(255,255,255,.22);border-left:none;' +
      'background:rgba(12,14,20,.5);color:#fff;text-decoration:none;opacity:.6;transition:opacity .15s;touch-action:none;' +
      '-webkit-user-select:none;user-select:none;-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px);cursor:pointer;box-sizing:border-box}' +
      '#gh-home.gh-free{border-radius:12px;border-left:1px solid rgba(255,255,255,.22)}' +
      '#gh-home:hover,#gh-home:focus-visible{opacity:1}#gh-home svg{width:16px;height:16px}' +
      '.gh-dot{width:6px;height:6px;border-radius:50%;background:#888}.gh-ok{background:#3ecf8e}.gh-off{background:#f5a524}.gh-out,.gh-guest{background:#888}';
    var addBtn = function () {
      var st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);
      var a = document.createElement('a');
      a.id = 'gh-home'; a.href = BASE + 'index.html'; a.title = 'Вернуться в меню игр (кнопку можно перетащить)';
      a.innerHTML = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 7.5 8 3l5.5 4.5M4 6.5V13h8V6.5"/></svg><i class="gh-dot"></i>';
      dot = a.querySelector('i');
      status(startState === 'ok' ? 'ok' : startState === 'off' ? 'off' : 'guest');
      var u = user(); a.title += u ? '\nАккаунт: ' + u.name : '\nИгра без аккаунта';
      var pos = readJSON('gh-btnpos', null);
      a.setAttribute('aria-label', 'Меню игр');
      if (pos) { a.style.left = Math.min(pos.x, innerWidth - 30) + 'px'; a.style.top = Math.min(pos.y, innerHeight - 46) + 'px'; a.style.transform = 'none'; if (pos.x > 2) a.classList.add('gh-free'); }
      var drag = null, moved = false;
      a.addEventListener('pointerdown', function (e) {
        var r = a.getBoundingClientRect(); drag = { dx: e.clientX - r.left, dy: e.clientY - r.top, x0: e.clientX, y0: e.clientY }; moved = false;
        try { a.setPointerCapture(e.pointerId); } catch (_) {}
        e.stopPropagation();
      });
      a.addEventListener('pointermove', function (e) {
        if (!drag) return;
        if (!moved && Math.abs(e.clientX - drag.x0) + Math.abs(e.clientY - drag.y0) < 8) return;
        moved = true;
        var x = Math.max(0, Math.min(innerWidth - a.offsetWidth, e.clientX - drag.dx));
        var y = Math.max(0, Math.min(innerHeight - a.offsetHeight, e.clientY - drag.dy));
        if (x < 12) x = 0; a.style.left = x + 'px'; a.style.top = y + 'px'; a.style.transform = 'none'; a.classList.toggle('gh-free', x > 0);
        e.stopPropagation();
      });
      a.addEventListener('pointerup', function (e) {
        if (moved) lsSet('gh-btnpos', JSON.stringify({ x: parseInt(a.style.left, 10), y: parseInt(a.style.top, 10) }));
        drag = null; e.stopPropagation();
      });
      a.addEventListener('click', function (e) {
        e.stopPropagation();
        if (moved) { e.preventDefault(); moved = false; return; }
        e.preventDefault();
        var go = function () { location.href = a.href; };
        var t = setTimeout(go, 1500); flush().then(function () { clearTimeout(t); go(); });
      });
      document.body.appendChild(a);
    };
    if (document.body) addBtn(); else document.addEventListener('DOMContentLoaded', addBtn);
  }
})();
