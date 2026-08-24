// Search + league/pick filtering for BZ Bets
(function () {
  const input = document.getElementById('searchInput');
  const clearBtn = document.getElementById('clearSearch');
  const table = document.getElementById('predictionsTable');
  if (!table) return;

  const rows = Array.from(table.querySelectorAll('tbody tr.game-row'));
  const visibleCountEl = document.getElementById('visibleCount');
  const totalCountEl = document.getElementById('totalCount');
  const chips = Array.from(document.querySelectorAll('.filter-chip'));

  let leagueFilter = 'all';
  let pickFilter = 'all';

  const normalize = (s) => (s || '').toLowerCase().normalize('NFKD');

  const index = rows.map((row) => ({
    row,
    text: normalize(row.getAttribute('data-search') || row.textContent),
    league: row.getAttribute('data-league') || '',
    pick: row.getAttribute('data-pick') || '',
  }));

  if (totalCountEl) totalCountEl.textContent = rows.length;

  function applyFilters() {
    const q = normalize(input?.value?.trim() || '');

    index.forEach(({ row, text, league, pick }) => {
      const searchHit = !q || text.includes(q);
      const leagueHit = leagueFilter === 'all' || league === leagueFilter;
      const pickHit =
        pickFilter === 'all' ||
        pick === pickFilter ||
        (pickFilter === 'play' && (pick === 'OVER' || pick === 'UNDER'));

      row.style.display = searchHit && leagueHit && pickHit ? '' : 'none';
    });

    if (visibleCountEl) {
      visibleCountEl.textContent = rows.reduce(
        (count, row) => count + (row.style.display !== 'none' ? 1 : 0),
        0
      );
    }
  }

  function setChipState(type, value) {
    chips
      .filter((chip) => chip.dataset.filterType === type)
      .forEach((chip) => {
        const isActive =
          (type === 'league' && chip.dataset.filterValue === value) ||
          (type === 'pick' && chip.dataset.filterValue === value);
        chip.classList.toggle('active', isActive);
      });
  }

  let debounce;
  input?.addEventListener('input', () => {
    clearTimeout(debounce);
    debounce = setTimeout(applyFilters, 80);
  });

  clearBtn?.addEventListener('click', () => {
    input.value = '';
    input.focus();
    applyFilters();
  });

  chips.forEach((chip) => {
    chip.addEventListener('click', () => {
      const type = chip.dataset.filterType;
      const value = chip.dataset.filterValue;

      if (type === 'league') {
        leagueFilter = value;
        setChipState('league', value);
      } else if (type === 'pick') {
        pickFilter = pickFilter === value ? 'all' : value;
        chips
          .filter((item) => item.dataset.filterType === 'pick')
          .forEach((item) =>
            item.classList.toggle('active', item.dataset.filterValue === pickFilter)
          );
      }

      applyFilters();
    });
  });

  applyFilters();
})();
