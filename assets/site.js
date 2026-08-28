(function () {
  'use strict';

  // Current year in footer
  var y = document.getElementById('year');
  if (y) y.textContent = new Date().getFullYear();

  // Mobile menu toggle
  var toggle = document.querySelector('.nav-toggle');
  var nav = document.getElementById('site-nav');
  if (toggle && nav) {
    toggle.addEventListener('click', function () {
      var open = nav.classList.toggle('open');
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      toggle.setAttribute('aria-label', open ? 'Закрыть меню' : 'Открыть меню');
    });
    // close after choosing a link
    nav.addEventListener('click', function (e) {
      if (e.target.tagName === 'A' && nav.classList.contains('open')) {
        nav.classList.remove('open');
        toggle.setAttribute('aria-expanded', 'false');
      }
    });
  }

  // Bot-safe email: address is assembled at runtime, never in the HTML source
  var user = 'photoarchive2000', domain = 'mail.ru';
  var addr = user + '@' + domain;

  // Prefilled body: a few blank lines to write in, then an opt-in P.S. for testimonials.
  // Whoever writes about a bug/question just deletes the last paragraph; whoever leaves it
  // in is consenting to have that text published on the site under the given signature.
  var mailBody =
    'Здравствуйте!\r\n\r\n\r\n\r\n' +
    'P.S. Если это отзыв о программе — не возражаю против публикации его текста на сайте ' +
    'PhotoArchive. Подпись: имя или ник и город (например: «Анна, Казань»). Публикация ' +
    'не нужна — просто удалите этот абзац.';
  var mailHref = 'mailto:' + addr + '?subject=PhotoArchive&body=' + encodeURIComponent(mailBody);
  document.querySelectorAll('[data-mail]').forEach(function (a) {
    a.href = mailHref;
  });

  // Fallback for "Написать автору" when the OS has no default mail client: mailto: then
  // silently does nothing, with no error the page can detect directly. Heuristic: if the
  // page hasn't lost focus shortly after the click (a real mail client would switch away),
  // show the address AND the prefilled template as plain copyable text instead of leaving
  // the click looking like a dead end (the template is lost when mailto: doesn't fire).
  document.querySelectorAll('[data-mail]').forEach(function (a) {
    a.addEventListener('click', function () {
      var blurred = false;
      var onBlur = function () { blurred = true; };
      window.addEventListener('blur', onBlur);
      setTimeout(function () {
        window.removeEventListener('blur', onBlur);
        if (blurred || document.hidden) return;
        if (a.nextElementSibling && a.nextElementSibling.classList.contains('mail-fallback')) return;
        var box = document.createElement('span');
        box.className = 'mail-fallback';
        var text = document.createElement('span');
        text.className = 'mail-fallback-addr';
        text.textContent = 'Почтовая программа не открылась. Адрес: ' + addr;

        var copyAddr = document.createElement('button');
        copyAddr.type = 'button';
        copyAddr.className = 'mail-fallback-copy';
        copyAddr.textContent = 'Скопировать адрес';
        copyAddr.addEventListener('click', function () {
          navigator.clipboard.writeText(addr).then(function () {
            copyAddr.textContent = 'Скопировано';
          });
        });

        var copyBody = document.createElement('button');
        copyBody.type = 'button';
        copyBody.className = 'mail-fallback-copy';
        copyBody.textContent = 'Скопировать шаблон';
        copyBody.addEventListener('click', function () {
          navigator.clipboard.writeText(mailBody).then(function () {
            copyBody.textContent = 'Шаблон скопирован';
          });
        });

        var close = document.createElement('button');
        close.type = 'button';
        close.className = 'mail-fallback-close';
        close.setAttribute('aria-label', 'Закрыть');
        close.textContent = '×';
        close.addEventListener('click', function () { box.remove(); });

        box.appendChild(text);
        box.appendChild(copyAddr);
        box.appendChild(copyBody);
        box.appendChild(close);
        a.insertAdjacentElement('afterend', box);
      }, 900);
    });
  });

  // "PhotoArchive" brand in the header always returns to the very top of the page, regardless
  // of any sticky-header/anchor-scroll edge case (explicit scrollTo, not just relying on href="#top").
  var brand = document.querySelector('.brand');
  if (brand) {
    brand.addEventListener('click', function (e) {
      if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      e.preventDefault();
      window.scrollTo({ top: 0, behavior: 'smooth' });
      history.replaceState(null, '', location.pathname + location.search);
    });
  }

  // Live file size on the download buttons (2026-07-28). Not a new third-party request in
  // the site's "no external requests" sense -- both buttons already link straight to
  // github.com, this just reads the same repo's public release metadata. The static text
  // already in the HTML (see index.html [data-asset]) is the fallback: if the request fails,
  // is slow, or GitHub's unauthenticated rate limit (~60/h per IP) is hit, it's left as-is,
  // silently -- this is a nice-to-have, not something worth showing an error for.
  var sizeCacheKey = 'pa_release_asset_sizes_v1';
  var sizeCacheTtlMs = 24 * 60 * 60 * 1000; // release sizes only change on a new release

  function applyAssetSizes(sizesByName) {
    document.querySelectorAll('[data-asset]').forEach(function (el) {
      var size = sizesByName[el.getAttribute('data-asset')];
      if (size) el.textContent = size;
    });
  }

  function formatMB(bytes) {
    return Math.round(bytes / 1048576) + ' МБ';
  }

  var cachedSizes = null;
  try {
    var raw = localStorage.getItem(sizeCacheKey);
    if (raw) {
      var parsed = JSON.parse(raw);
      if (Date.now() - parsed.ts < sizeCacheTtlMs) cachedSizes = parsed.sizes;
    }
  } catch (e) { /* localStorage unavailable (private mode etc.) -- just re-fetch */ }

  if (cachedSizes) {
    applyAssetSizes(cachedSizes);
  } else if (window.fetch) {
    var sizeController = window.AbortController ? new AbortController() : null;
    var sizeTimeout = sizeController && setTimeout(function () { sizeController.abort(); }, 5000);
    fetch('https://api.github.com/repos/vo1012/PhotoArchive/releases/latest',
      sizeController ? { signal: sizeController.signal } : {})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data || !Array.isArray(data.assets)) return;
        var sizes = {};
        data.assets.forEach(function (a) { sizes[a.name] = formatMB(a.size); });
        applyAssetSizes(sizes);
        try {
          localStorage.setItem(sizeCacheKey, JSON.stringify({ ts: Date.now(), sizes: sizes }));
        } catch (e) { /* storage full/unavailable -- fine, just re-fetch next time */ }
      })
      .catch(function () { /* offline/rate-limited/CORS -- static fallback text stays */ })
      .finally(function () { if (sizeTimeout) clearTimeout(sizeTimeout); });
  }
})();
