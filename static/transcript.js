// The run transcript page: fold long text, search, expand all, step through errors, copy.
// The page reads fine without it; this only adds the shortcuts.
(function () {
    const groups = () => Array.from(document.querySelectorAll('.sf-tx-group'));

    // Long prompts, plans, reports and outputs fold to a few lines with a Show all button.
    // Folded only once open, since a closed <details> has no height to measure.
    function clamp(el) {
        if (el.dataset.clampDone || !el.offsetParent) return;
        el.dataset.clampDone = '1';
        el.classList.add('is-clamped');
        if (el.scrollHeight <= el.clientHeight + 8) {
            el.classList.remove('is-clamped');
            return;
        }
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'sf-tx-unclamp';
        button.textContent = 'Show all';
        button.addEventListener('click', () => {
            const folded = el.classList.toggle('is-clamped');
            button.textContent = folded ? 'Show all' : 'Show less';
        });
        el.after(button);
    }
    const clampWithin = (root) => root.querySelectorAll('[data-clamp]').forEach(clamp);
    clampWithin(document);
    document.addEventListener('toggle', (e) => { if (e.target.open) clampWithin(e.target); }, true);

    // Copy the command or output next to the button
    document.addEventListener('click', (e) => {
        const button = e.target.closest('[data-copy]');
        if (!button) return;
        const pre = button.parentElement.nextElementSibling;
        if (pre && navigator.clipboard) {
            const label = button.getAttribute('aria-label');
            navigator.clipboard.writeText(pre.textContent).then(() => {
                button.setAttribute('aria-label', 'Copied');
                setTimeout(() => button.setAttribute('aria-label', label), 1500);
            });
        }
    });

    // Expand all: every group of tool calls open, or all closed again
    const expand = document.querySelector('[data-expand]');
    function toggleAll() {
        if (!expand) return;
        const open = expand.getAttribute('aria-pressed') !== 'true';
        groups().forEach((g) => { g.open = open; });
        expand.setAttribute('aria-pressed', String(open));
        expand.firstChild.textContent = open ? 'Collapse all ' : 'Expand all ';
    }
    if (expand) expand.addEventListener('click', toggleAll);

    // Next error: open the failed call, its group, and bring it into view
    const next = document.querySelector('[data-next-error]');
    if (next) {
        const failed = Array.from(document.querySelectorAll('.sf-tx-call[data-error]'));
        const count = next.querySelector('[data-error-count]');
        let at = -1;
        next.addEventListener('click', () => {
            at = (at + 1) % failed.length;
            const call = failed[at];
            for (let d = call; d; d = d.parentElement.closest('details')) d.open = true;
            call.scrollIntoView({ block: 'start', behavior: 'smooth' });
            call.querySelector('summary').focus({ preventScroll: true });
            count.textContent = (at + 1) + '/' + failed.length;
        });
    }

    // Search: keep the steps (or event rows) that mention the text, and open the matching calls
    const search = document.querySelector('[data-search]');
    const found = document.querySelector('[data-found]');
    const none = document.querySelector('[data-none]');
    let timer = null;
    function filter() {
        const q = search.value.trim().toLowerCase();
        const items = Array.from(document.querySelectorAll('[data-step], [data-row]'));
        let shown = 0;
        items.forEach((item) => {
            const match = !q || item.textContent.toLowerCase().includes(q);
            item.hidden = !match;
            if (match) shown += 1;
            if (!q || !match) return;
            item.querySelectorAll('.sf-tx-group, .sf-tx-call').forEach((d) => {
                if (d.textContent.toLowerCase().includes(q)) d.open = true;
            });
        });
        found.textContent = q ? shown + (shown === 1 ? ' match' : ' matches') : '';
        if (none) none.hidden = shown > 0;
    }
    if (search) {
        search.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(filter, 150); });
    }

    // Keys: / to search, E to expand all, Escape to clear the search
    document.addEventListener('keydown', (e) => {
        if (e.ctrlKey || e.metaKey || e.altKey) return;
        const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || e.target.isContentEditable;
        if (e.key === 'Escape' && e.target === search) {
            search.value = '';
            filter();
            search.blur();
        } else if (typing) {
            return;
        } else if (e.key === '/' && search) {
            e.preventDefault();
            search.focus();
        } else if ((e.key === 'e' || e.key === 'E') && expand) {
            e.preventDefault();
            toggleAll();
        }
    });

    // Jump to: mark the agent message in view
    const links = new Map();
    document.querySelectorAll('.sf-tx-links a[href^="#"]').forEach((a) => links.set(a.hash.slice(1), a));
    if (links.size && 'IntersectionObserver' in window) {
        const seen = new Set();
        const observer = new IntersectionObserver((entries) => {
            entries.forEach((entry) => (entry.isIntersecting ? seen.add(entry.target.id) : seen.delete(entry.target.id)));
            const first = Array.from(links.keys()).find((id) => seen.has(id));
            links.forEach((a, id) => a.classList.toggle('is-here', id === first));
        }, { rootMargin: '0px 0px -60% 0px' });
        links.forEach((_, id) => { const el = document.getElementById(id); if (el) observer.observe(el); });
    }
})();
