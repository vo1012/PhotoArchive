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
  document.querySelectorAll('[data-mail]').forEach(function (a) {
    a.href = 'mailto:' + addr + '?subject=PhotoArchive';
  });

  // Fallback for "Написать автору" when the OS has no default mail client: mailto: then
  // silently does nothing, with no error the page can detect directly. Heuristic: if the
  // page hasn't lost focus shortly after the click (a real mail client would switch away),
  // show the address as plain copyable text instead of leaving the click looking like a dead end.
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
        text.textContent = 'Почтовая программа не открылась: ' + addr;
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'mail-fallback-copy';
        btn.textContent = 'Скопировать адрес';
        btn.addEventListener('click', function () {
          navigator.clipboard.writeText(addr).then(function () {
            btn.textContent = 'Скопировано';
            setTimeout(function () { btn.textContent = 'Скопировать адрес'; }, 1800);
          });
        });
        box.appendChild(text);
        box.appendChild(btn);
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
})();
