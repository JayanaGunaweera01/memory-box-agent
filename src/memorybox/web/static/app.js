// MemoryBox: the small things the pages need. Nothing here talks to any server but this one.
(function () {
  'use strict';
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  // --- Adding photos: submit as soon as they are chosen (or dropped), and say how many. -------
  function autoSubmit(input, form, button, verb) {
    if (!input || !form) { return; }
    input.addEventListener('change', function () {
      if (!input.files.length) { return; }
      if (button) { button.textContent = verb + ' ' + input.files.length + '…'; button.disabled = true; }
      form.submit();
    });
  }
  autoSubmit($('#photos'), $('#add-form'), $('#add-button'), 'Adding');
  autoSubmit($('#side-photos'), $('#side-form'), null, 'Adding');
  var drop = $('#drop');
  if (drop) {
    ['dragenter', 'dragover'].forEach(function (e) { drop.addEventListener(e, function (ev) { ev.preventDefault(); drop.classList.add('over'); }); });
    ['dragleave', 'drop'].forEach(function (e) { drop.addEventListener(e, function () { drop.classList.remove('over'); }); });
    drop.addEventListener('drop', function (ev) {
      ev.preventDefault();
      var input = $('#photos');
      if (ev.dataTransfer && ev.dataTransfer.files.length) { input.files = ev.dataTransfer.files; input.dispatchEvent(new Event('change')); }
    });
  }

  // --- The box: find and order, without a round trip. ---------------------------------------
  var grid = $('#grid');
  if (grid) {
    var tiles = $$('li', grid);
    var find = $('#find'), sort = $('#sort'), count = $('#count'), none = $('#no-match');
    var original = tiles.slice();
    function apply() {
      var q = (find.value || '').trim().toLowerCase().split(/\s+/).filter(Boolean);
      var shown = 0;
      tiles.forEach(function (li) {
        var hay = li.dataset.search || '';
        var ok = q.every(function (w) { return hay.indexOf(w) !== -1 || (li.dataset.year || '').indexOf(w) === 0; });
        li.hidden = !ok;
        if (ok) { shown++; }
      });
      count.textContent = q.length ? shown + ' of ' + tiles.length + ' keepsakes' : tiles.length + (tiles.length === 1 ? ' keepsake' : ' keepsakes');
      none.style.display = shown ? 'none' : 'block';
    }
    function order() {
      var list = original.slice();
      var undated = function (li) { return li.dataset.year ? 0 : 1; };
      if (sort.value === 'old' || sort.value === 'new') {
        var dir = sort.value === 'old' ? 1 : -1;
        list.sort(function (a, b) {
          return undated(a) - undated(b) || dir * ((+a.dataset.year || 0) - (+b.dataset.year || 0)) || (+a.dataset.id - +b.dataset.id);
        });
      }
      list.forEach(function (li) { grid.appendChild(li); });
      try { sessionStorage.setItem('mb-sort', sort.value); } catch (e) { /* private mode */ }
    }
    find.addEventListener('input', apply);
    sort.addEventListener('change', order);
    try { var saved = sessionStorage.getItem('mb-sort'); if (saved) { sort.value = saved; order(); } } catch (e) { /* ignore */ }
    document.addEventListener('keydown', function (e) {
      if (e.key === '/' && document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'TEXTAREA') { e.preventDefault(); find.focus(); }
    });
  }

  // --- A keepsake: switch sides, zoom. -------------------------------------------------------
  var main = $('#main-photo');
  $$('[data-side]').forEach(function (b) {
    b.addEventListener('click', function () {
      main.src = b.dataset.side;
      $$('[data-side]').forEach(function (o) { o.setAttribute('aria-pressed', o === b ? 'true' : 'false'); });
    });
  });
  var box = $('#lightbox');
  if (box && main && typeof box.showModal === 'function') {
    $('#zoom').addEventListener('click', function () { $('#lightbox-img').src = main.src; box.showModal(); });
    $('#lightbox-close').addEventListener('click', function () { box.close(); });
    box.addEventListener('click', function (e) { if (e.target === box) { box.close(); } });
  }

  // --- A question it is wondering about starts the answer. -----------------------------------
  $$('[data-fill]').forEach(function (b) {
    b.addEventListener('click', function () {
      var t = $('#words');
      var line = b.dataset.fill + ' ';
      if (t.value.indexOf(b.dataset.fill) === -1) { t.value = (t.value ? t.value.trim() + '\n' : '') + line; }
      b.setAttribute('aria-pressed', 'true');
      t.focus();
      t.setSelectionRange(t.value.length, t.value.length);
    });
  });

  $$('form[data-confirm]').forEach(function (f) {
    f.addEventListener('submit', function (e) { if (!window.confirm(f.dataset.confirm)) { e.preventDefault(); } });
  });

  // Saving a correction: show it is on its way, and stop a double click sending it twice.
  $$('form[action$="/tell"], form[action$="/edit"]').forEach(function (f) {
    f.addEventListener('submit', function () { $$('button[type=submit]', f).forEach(function (b) { b.disabled = true; b.textContent = 'Saving…'; }); });
  });

  // The confirmation message fades once read, and leaves the address clean for reloading.
  var flash = $('.flash');
  if (flash && window.history.replaceState) {
    var url = new URL(window.location.href);
    url.searchParams.delete('done');
    window.history.replaceState(null, '', url.pathname + (url.search || ''));
    setTimeout(function () { flash.style.transition = 'opacity .6s'; flash.style.opacity = '0'; setTimeout(function () { flash.remove(); }, 700); }, 6000);
  }

  // --- Read aloud with the device's own voices: no service, works offline. ------------------
  if (!('speechSynthesis' in window)) { return; }
  $$('[data-speak]').forEach(function (btn) {
    btn.hidden = false;
    var label = btn.textContent;
    btn.addEventListener('click', function () {
      if (speechSynthesis.speaking) { speechSynthesis.cancel(); btn.textContent = label; return; }
      var el = $(btn.dataset.speak);
      if (!el) { return; }
      var parts = $$('h2, h3, .meta, .text', el).map(function (n) { return n.textContent.replace(/\s+/g, ' ').trim(); });
      if (!parts.length) { parts = [el.textContent.trim()]; }
      btn.textContent = 'Stop reading';
      parts.filter(Boolean).forEach(function (text, i, all) {
        var u = new SpeechSynthesisUtterance(text);
        u.rate = 0.92;
        if (i === all.length - 1) { u.onend = function () { btn.textContent = label; }; }
        speechSynthesis.speak(u);
      });
    });
  });
})();
