// Browser notifications for issues that need a person: a plan ready for review, or needs attention.
// Polls /api/attention and notifies once for each issue that newly reaches one of those statuses.
(function () {
    const button = document.getElementById('notify-toggle');
    if (!button || !('Notification' in window)) {
        if (button) button.hidden = true;
        return;
    }
    const URL = button.dataset.url;
    const SEEN = 'sf-notified';
    const POLL_MS = 15000;
    let timer = null;

    function loadSeen() {
        try { return JSON.parse(localStorage.getItem(SEEN)); } catch (e) { return null; }
    }
    function saveSeen(keys) {
        try { localStorage.setItem(SEEN, JSON.stringify(keys)); } catch (e) { /* storage blocked */ }
    }

    function show(item) {
        const plan = item.status === 'plan-ready';
        const n = new Notification(item.identifier + (plan ? ': plan ready for review' : ': needs attention'), {
            body: plan ? item.title : item.title + (item.reason ? '\n' + item.reason : ''),
            tag: item.key, // the same issue in two tabs raises one notification
        });
        n.onclick = () => { window.focus(); window.location.href = item.url; n.close(); };
    }

    async function poll() {
        let issues;
        try {
            const response = await fetch(URL, { credentials: 'same-origin' });
            if (!response.ok) return;
            issues = (await response.json()).issues;
        } catch (e) { return; }
        const keys = issues.map((i) => i.key);
        const seen = loadSeen();
        // The first poll only records what is already waiting, so enabling doesn't flood
        if (seen !== null) issues.filter((i) => !seen.includes(i.key)).forEach(show);
        saveSeen(keys);
    }

    function render() {
        const on = Notification.permission === 'granted' && localStorage.getItem('sf-notify') !== 'off';
        button.textContent = Notification.permission === 'denied' ? 'Notifications blocked' : on ? 'Notifications on' : 'Enable notifications';
        button.disabled = Notification.permission === 'denied';
        button.setAttribute('aria-pressed', String(on));
        clearInterval(timer);
        if (on) { poll(); timer = setInterval(poll, POLL_MS); }
    }

    button.addEventListener('click', async () => {
        if (Notification.permission === 'default') await Notification.requestPermission();
        else if (Notification.permission === 'granted') {
            try {
                localStorage.setItem('sf-notify', localStorage.getItem('sf-notify') === 'off' ? 'on' : 'off');
            } catch (e) { /* storage blocked */ }
        }
        render();
    });
    render();
})();
