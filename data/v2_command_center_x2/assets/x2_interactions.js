
(() => {
  document.documentElement.classList.add('x2-js');
  const menuFor = (toggle) => document.getElementById(toggle.dataset.x2Toggle || '');
  const triggerFor = (menu) => document.querySelector(`[data-x2-toggle="${menu.id}"]`);
  const closeMenu = (menu, trigger) => {
    if (!menu) {
      return;
    }
    menu.hidden = true;
    menu.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('trade-menu-open');
    if (trigger) {
      trigger.setAttribute('aria-expanded', 'false');
    }
  };
  const closeForToggle = (toggle) => closeMenu(menuFor(toggle), toggle);
  for (const menu of document.querySelectorAll('.trade-menu')) {
    menu.hidden = true;
    menu.setAttribute('aria-hidden', 'true');
  }
  const search = document.querySelector('[data-x2-search]');
  if (search) {
    const items = Array.from(document.querySelectorAll('[data-filter-item]'));
    search.addEventListener('input', () => {
      const term = search.value.trim().toLowerCase();
      for (const item of items) {
        const hide = term.length > 0 && !item.textContent.toLowerCase().includes(term);
        item.hidden = hide;
        if (hide) {
          const toggle = item.querySelector('[data-x2-toggle]');
          if (toggle) {
            closeForToggle(toggle);
          }
        }
      }
    });
  }
  for (const toggle of document.querySelectorAll('[data-x2-toggle]')) {
    toggle.addEventListener('click', () => {
      const menu = menuFor(toggle);
      if (!menu) {
        return;
      }
      const nextOpen = menu.hidden;
      for (const openMenu of document.querySelectorAll('.trade-menu:not([hidden])')) {
        closeMenu(openMenu, triggerFor(openMenu));
      }
      if (nextOpen) {
        menu.hidden = false;
        menu.setAttribute('aria-hidden', 'false');
        document.body.classList.add('trade-menu-open');
        toggle.setAttribute('aria-expanded', 'true');
        const panel = menu.querySelector('.trade-menu-panel');
        if (panel) {
          panel.focus();
        }
      } else {
        closeMenu(menu, toggle);
      }
    });
  }
  for (const close of document.querySelectorAll('[data-x2-close]')) {
    close.addEventListener('click', () => {
      const menu = document.getElementById(close.dataset.x2Close || '');
      closeMenu(menu, menu ? triggerFor(menu) : null);
    });
  }
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') {
      return;
    }
    const menu = document.querySelector('.trade-menu:not([hidden])');
    if (menu) {
      closeMenu(menu, triggerFor(menu));
    }
  });
  for (const cell of document.querySelectorAll('[data-day-summary]')) {
    cell.addEventListener('mouseenter', () => {
      cell.setAttribute('aria-label', cell.dataset.daySummary || 'Day story');
    });
  }
})();
