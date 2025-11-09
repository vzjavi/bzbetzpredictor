// Table filtering for BZ Bets (robust + accurate counter)
(function () {
  const input = document.getElementById('searchInput');
  const clearBtn = document.getElementById('clearSearch');
  const table = document.getElementById('predictionsTable');
  const rows = Array.from(table.querySelectorAll('tbody tr'));
  const visibleCountEl = document.getElementById('visibleCount');
  const totalCountEl = document.getElementById('totalCount');

  const normalize = (s) => (s || '').toLowerCase().normalize('NFKD');

  const getRowText = (row) => {
    const ds = row.getAttribute('data-search');
    if (ds) return normalize(ds);
    const matchup = row.querySelector('.cell.matchup')?.textContent || '';
    const sport = row.querySelector('.cell.sport')?.textContent || '';
    const time  = row.querySelector('.cell.time')?.textContent || '';
    return normalize([matchup, sport, time].join(' '));
  };

  const index = rows.map((r) => ({ row: r, text: getRowText(r) }));
  totalCountEl.textContent = rows.length;

  const updateVisibleCount = () => {
    const count = rows.reduce((n, r) => n + (r.style.display !== 'none' ? 1 : 0), 0);
    visibleCountEl.textContent = count;
  };

  const applyFilter = (term) => {
    const q = normalize(term.trim());
    index.forEach(({ row, text }) => {
      const hit = !q || text.includes(q);
      row.style.display = hit ? '' : 'none';
    });
    updateVisibleCount();
  };

  let t;
  input?.addEventListener('input', () => {
    clearTimeout(t);
    t = setTimeout(() => applyFilter(input.value), 80);
  });

  clearBtn?.addEventListener('click', () => {
    input.value = '';
    input.focus();
    applyFilter('');
  });

  // initial render
  applyFilter(input?.value || '');
})();
