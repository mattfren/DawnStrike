(() => {
  const filters = document.querySelectorAll('[data-filter]');
  const cards = document.querySelectorAll('.trade-card');
  const normalize = (value) => (value || '').toString().toLowerCase().trim();
  const apply = () => {
    const state = {};
    filters.forEach((filter) => { state[filter.dataset.filter] = normalize(filter.value); });
    cards.forEach((card) => {
      let visible = true;
      ['date', 'symbol', 'strategy', 'result', 'evidence'].forEach((key) => {
        const wanted = state[key];
        const actual = normalize(card.dataset[key]);
        if (wanted && !actual.includes(wanted)) visible = false;
      });
      card.classList.toggle('hidden-by-filter', !visible);
    });
  };
  filters.forEach((filter) => filter.addEventListener('input', apply));
  filters.forEach((filter) => filter.addEventListener('change', apply));
  document.querySelectorAll('.advanced-drawer').forEach((drawer) => {
    drawer.addEventListener('toggle', () => {
      if (drawer.open) drawer.dataset.opened = 'true';
    });
  });
})();