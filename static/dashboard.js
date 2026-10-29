    let stacks = [], filter = 'all', query = '', currentData = {};
    const $ = id => document.getElementById(id);
    const escapeHtml = value => String(value).replace(/[&<>"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[char]));
    const intervalText = { '15m':'15 minutes', '1h':'1 hour', '6h':'6 hours', 'daily':'day' };

    function applyTheme(theme) {
        document.documentElement.dataset.theme = theme;
        localStorage.setItem('woods-keeper-theme', theme);
        $('theme-toggle').innerText = theme === 'dark' ? 'Light mode' : 'Dark mode';
    }

    function toggleTheme() {
        applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
    }

    function renderHealth() {
        const running = stacks.filter(s => s.status === 'Active').length;
        const stopped = stacks.length - running;
        const total = stacks.length || 1;
        $('health').innerHTML = `<b class="r" style="width:${running / total * 100}%"></b><b class="s" style="width:${stopped / total * 100}%"></b>`;
        $('legend').innerHTML = `<span><i class="dot"></i>${running} running</span><span><i class="dot off"></i>${stopped} stopped</span><span>${stacks.length} total</span>`;
    }

    function renderStacks() {
        const visible = stacks.filter(s =>
            (filter === 'all' || (filter === 'running') === (s.status === 'Active')) &&
            String(s.name).toLowerCase().includes(query)
        );
        $('stacks-grid').innerHTML = visible.length ? visible.map(s => `
            <article class="stack-card ${s.status === 'Active' ? '' : 'stopped'} ${s.excluded ? 'skipped' : ''}">
                <div>
                    <h3><span class="dot"></span>${escapeHtml(s.name)}</h3>
                    <div class="state">${s.status === 'Active' ? 'Running' : 'Stopped'}</div>
                </div>
                <button class="ghost" onclick="triggerStackUpdate(${s.id}, ${s.excluded})" ${currentData.is_running ? 'disabled' : ''}>Update</button>
                <label class="skip switch">Skip global updates
                    <input type="checkbox" ${s.excluded ? 'checked' : ''} onchange="setStackExcluded(${s.id}, this.checked)"><i></i>
                </label>
            </article>
        `).join('') : '<div class="empty">No stacks match your search. Clear the search or switch the filter to All.</div>';
    }

    function render() {
        renderHealth();
        renderStacks();
        const schedule = currentData.schedule || { enabled:false, interval:'1h' };
        $('schedule-enabled').checked = schedule.enabled;
        $('schedule-interval').value = schedule.interval;
        $('schedule-interval').disabled = !schedule.enabled;
        $('schedule-status').innerText = schedule.enabled ? 'Enabled' : 'Disabled';
        $('next-run').innerText = schedule.enabled ? `Every ${intervalText[schedule.interval]}` : 'Not scheduled';
    }

    async function fetchData() {
        try {
            const response = await fetch('/api/data');
            currentData = await response.json();
            stacks = currentData.stacks || [];
            $('portainer-status').innerText = currentData.portainer_connected ? 'Connected to Portainer' : currentData.portainer_error;
            $('connection-dot').className = `dot${currentData.portainer_connected ? '' : ' off'}`;
            $('last-run').innerText = currentData.last_run;
            const badge = $('status-badge');
            badge.innerText = currentData.status;
            badge.className = `badge${currentData.is_running ? ' busy' : ''}`;
            $('global-update-btn').disabled = currentData.is_running;
            $('global-update-btn').innerText = currentData.is_running ? 'Updating...' : 'Check and update all stacks';
            $('logs-container').innerText = (currentData.logs || []).join('\\n') || 'No logs available yet.';
            render();
        } catch (error) {
            console.error('Failed to fetch dashboard data', error);
        }
    }

    async function triggerUpdate(stackId = null) {
        if (stackId === null) $('global-update-btn').disabled = true;
        await fetch('/api/update', { method:'POST', headers:{'Content-Type':'application/json', 'X-CSRF-Token':csrfToken}, body:JSON.stringify({stack_id:stackId}) });
        setTimeout(fetchData, 1000);
    }

    async function triggerStackUpdate(stackId, excluded) {
        if (excluded && !confirm('This stack is excluded from global updates. Are you sure you want to pull new images and update it now?')) return;
        await triggerUpdate(stackId);
    }

    async function setStackExcluded(stackId, excluded) {
        const response = await fetch('/api/stack-settings', { method:'POST', headers:{'Content-Type':'application/json', 'X-CSRF-Token':csrfToken}, body:JSON.stringify({stack_id:stackId, excluded}) });
        if (!response.ok) { alert('Could not save the stack update setting.'); fetchData(); }
        else { const stack = stacks.find(s => s.id === stackId); if (stack) stack.excluded = excluded; renderStacks(); }
    }

    async function saveSchedule() {
        const enabled = $('schedule-enabled').checked;
        const interval = $('schedule-interval').value;
        const response = await fetch('/api/schedule', { method:'POST', headers:{'Content-Type':'application/json', 'X-CSRF-Token':csrfToken}, body:JSON.stringify({enabled, interval}) });
        if (response.ok) { currentData.schedule = await response.json(); render(); }
        else { alert('Could not save the schedule.'); fetchData(); }
    }

    $('stack-search').oninput = event => { query = event.target.value.trim().toLowerCase(); renderStacks(); };
    document.querySelectorAll('[data-filter]').forEach(button => button.onclick = () => {
        filter = button.dataset.filter;
        document.querySelectorAll('[data-filter]').forEach(item => item.setAttribute('aria-pressed', item === button));
        renderStacks();
    });
    $('clear-logs').onclick = () => { $('logs-container').innerText = ''; };
    applyTheme(localStorage.getItem('woods-keeper-theme') || 'light');
    setInterval(fetchData, 4000);
    fetchData();
