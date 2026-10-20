'use strict';
(() => {
  const cache = new Map();
  const normalize = value => value.trim().toLowerCase().replace(/^\/?r\//, '').replace(/\/$/, '');
  const countFormat = new Intl.NumberFormat('en', {notation:'compact', maximumFractionDigits:1});
  const closeAll = [];

  function setupSearch(inputId, formId) {
    const input = document.getElementById(inputId);
    const form = document.getElementById(formId);
    const panel = el('div', 'search-dropdown');
    panel.hidden = true;
    const heading = el('div', 'search-dropdown-heading', 'COMMUNITIES');
    const list = el('div', 'search-options');
    list.id = `${inputId}-suggestions`;
    list.setAttribute('role', 'listbox');
    list.setAttribute('aria-label', 'Suggested subreddits');
    const status = el('div', 'search-dropdown-status');
    status.setAttribute('role', 'status');
    const hint = el('div', 'search-dropdown-hint', '↑ ↓ to choose · Enter to open');
    panel.append(heading, list, status, hint);
    form.append(panel);
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-controls', list.id);
    input.setAttribute('aria-expanded', 'false');
    input.setAttribute('aria-haspopup', 'listbox');
    input.spellcheck = false;
    let rows = [], selected = -1, timer, abort, sequence = 0;

    function close() {
      ++sequence;
      clearTimeout(timer);
      abort?.abort();
      panel.hidden = true;
      form.classList.remove('search-open');
      input.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
      selected = -1;
    }
    closeAll.push(close);

    function choose(row) {
      input.value = row.displayName || row.subreddit;
      close();
      input.blur();
      navigate(communityLink(row.subreddit));
    }

    function highlight(name, query) {
      const fragment = document.createDocumentFragment();
      const at = name.toLowerCase().indexOf(query);
      fragment.append(document.createTextNode('r/'));
      if (at < 0) fragment.append(document.createTextNode(name));
      else {
        fragment.append(document.createTextNode(name.slice(0, at)));
        fragment.append(el('mark', '', name.slice(at, at + query.length)));
        fragment.append(document.createTextNode(name.slice(at + query.length)));
      }
      return fragment;
    }

    function select(index) {
      selected = index;
      Array.from(list.children).forEach((option, i) => {
        option.setAttribute('aria-selected', String(i === selected));
        option.classList.toggle('active', i === selected);
      });
      if (selected >= 0) {
        input.setAttribute('aria-activedescendant', list.children[selected].id);
        list.children[selected].scrollIntoView({block:'nearest'});
      } else input.removeAttribute('aria-activedescendant');
    }

    function render(items, query, message = '') {
      rows = items.filter(row => typeof row.subreddit === 'string' && row.subreddit.includes(query)).slice(0, 12);
      selected = -1;
      input.removeAttribute('aria-activedescendant');
      list.replaceChildren();
      rows.forEach((row, i) => {
        const option = el('div', 'search-option');
        option.id = `${list.id}-${i}`;
        option.setAttribute('role', 'option');
        option.setAttribute('aria-selected', 'false');
        const avatar = el('span', 'search-avatar', row.subreddit[0].toUpperCase());
        avatar.setAttribute('aria-hidden', 'true');
        const copy = el('div', 'search-option-copy');
        const name = el('span', 'search-option-name');
        name.append(highlight(row.displayName || row.subreddit, query));
        const count = el('span', 'search-option-members', Number.isInteger(row.members)
          ? `${countFormat.format(row.members)} members` : 'Member count unavailable');
        if (Number.isInteger(row.members)) count.title = `${row.members.toLocaleString('en')} members · Directory-reported total`;
        copy.append(name, count);
        const arrow = el('span', 'search-option-arrow', '↗');
        arrow.setAttribute('aria-hidden', 'true');
        option.append(avatar, copy, arrow);
        option.addEventListener('pointerdown', event => event.preventDefault());
        option.addEventListener('click', () => choose(row));
        option.addEventListener('pointermove', () => select(i));
        list.append(option);
      });
      status.textContent = message;
      status.hidden = !message;
      hint.hidden = !rows.length;
      panel.hidden = false;
      form.classList.add('search-open');
      input.setAttribute('aria-expanded', 'true');
    }

    function search() {
      clearTimeout(timer);
      abort?.abort();
      const token = ++sequence;
      const query = normalize(input.value);
      if (!query) {close(); return;}
      if (!/^[a-z0-9_]{1,50}$/.test(query)) {
        render([], query, 'Use letters, numbers, and underscores.');
        return;
      }
      const cached = cache.get(query);
      if (cached && cached.expires > Date.now()) {
        render(cached.rows, query, cached.message);
        return;
      }
      // Narrow immediately while the latest server result is on its way.
      render(rows, query, 'Searching communities…');
      timer = setTimeout(async () => {
        abort = new AbortController();
        try {
          const response = await fetch('/api/v1/subreddits/suggest?' + new URLSearchParams({q:query}), {signal:abort.signal});
          if (!response.ok) throw new Error('Search unavailable');
          const data = await response.json();
          if (token !== sequence || document.activeElement !== input) return;
          if (!Array.isArray(data.subreddits)) throw new Error('Invalid search response');
          const message = !data.available ? 'Live search is unavailable. Showing loaded communities.'
            : !data.subreddits.length ? 'No matching communities. Try another name or press Search.' : '';
          cache.set(query, {rows:data.subreddits, message, expires:Date.now() + (data.available ? 300000 : 15000)});
          if (cache.size > 80) cache.delete(cache.keys().next().value);
          render(data.subreddits, query, message);
        } catch (error) {
          if (error.name === 'AbortError' || token !== sequence || document.activeElement !== input) return;
          const local = communities.filter(row => row.subreddit.includes(query)).map(row => ({...row, members:null}));
          render(local, query, 'Search is unavailable. You can still search by exact name.');
        }
      }, 180);
    }

    input.addEventListener('input', event => {if (!event.isComposing) search();});
    input.addEventListener('compositionend', search);
    input.addEventListener('focus', search);
    input.addEventListener('keydown', event => {
      if (event.isComposing) return;
      if (event.key === 'Escape') {event.preventDefault();close();return;}
      if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
        event.preventDefault();
        if (panel.hidden) {search();return;}
        if (rows.length) select(event.key === 'ArrowDown' ? (selected + 1) % rows.length : (selected <= 0 ? rows.length - 1 : selected - 1));
      } else if (event.key === 'Enter' && !panel.hidden && selected >= 0) {
        event.preventDefault();
        choose(rows[selected]);
      } else if (event.key === 'Tab') close();
    });
    form.addEventListener('submit', close);
    form.addEventListener('focusout', event => {if (!form.contains(event.relatedTarget)) close();});
    document.addEventListener('pointerdown', event => {if (!form.contains(event.target)) close();});
  }
  setupSearch('global-query', 'global-search');
  setupSearch('explore-query', 'explore-search');
  window.addEventListener('hashchange', () => closeAll.forEach(close => close()));
})();
