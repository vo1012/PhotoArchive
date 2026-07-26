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
})();
