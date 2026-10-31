    let stacks = [], filter = 'all', query = '', currentData = {};
    let hudFilter = 'all', hudHighlight = -1, hudSegmentsBuilt = 0;
    let logLevel = 'info';
    const $ = id => document.getElementById(id);
    const escapeHtml = value => String(value).replace(/[&<>"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[char]));
    const logLevelRank = { debug: 0, info: 1, warn: 2, error: 3 };
    const getLogLevel = line => {
        const text = String(line).toLowerCase();
        if (text.includes('error') || text.includes('failed') || text.includes('rejected')) return 'error';
        if (text.includes('warning') || text.includes('warn') || text.includes('skipping') || text.includes('already in progress')) return 'warn';
        if (text.includes('webhook received') || text.includes('github package')) return 'debug';
        return 'info';
    };
    const logClass = line => {
        const text = String(line).toLowerCase();
        if (text.includes('--- update started:')) return 'log-run';
        if (getLogLevel(line) === 'error') return 'log-error';
        if (getLogLevel(line) === 'warn') return 'log-warn';
        if (text.includes('success') || text.includes('updated') || text.includes('redeploying')) return 'log-success';
        if (text.startsWith('{') || text.startsWith('}') || text.startsWith('"')) return 'log-json';
        return 'log-info';
    };
    const renderLogs = (element, logs) => {
        const visibleLogs = logs.filter(line => logLevelRank[getLogLevel(line)] >= logLevelRank[logLevel]);
        element.innerHTML = visibleLogs.length
            ? visibleLogs.map(line => `<span class="log-line ${logClass(line)}">${escapeHtml(line)}</span>`).join('')
            : '<span class="log-empty">No logs available yet.</span>';
        element.scrollTop = element.scrollHeight;
    };
    const renderAllLogs = () => {
        renderLogs($('logs-container'), currentData.logs || []);
        renderLogs($('hud-logs'), currentData.logs || []);
    };
    const intervalText = { '15m':'15 minutes', '1h':'1 hour', '6h':'6 hours', 'daily':'day' };

    function applyTheme(theme) {
        document.documentElement.dataset.theme = theme;
        localStorage.setItem('woods-keeper-theme', theme);
        $('theme-toggle').innerText = theme === 'dark' ? 'Light mode' : 'Dark mode';
    }

    function toggleTheme() {
        applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
    }

    function applyStyle(style) {
        document.documentElement.dataset.style = style;
        localStorage.setItem('woods-keeper-style', style);
        $('style-toggle').innerText = style === 'hud' ? 'Default style' : 'HUD style';
    }

    function toggleStyle() {
        applyStyle(document.documentElement.dataset.style === 'hud' ? 'default' : 'hud');
    }

    function hudArc(radius, start, end) {
        const point = angle => [
            200 + radius * Math.cos(angle * Math.PI / 180),
            200 + radius * Math.sin(angle * Math.PI / 180)
        ];
        const [x0, y0] = point(start);
        const [x1, y1] = point(end);
        return `M${x0.toFixed(2)} ${y0.toFixed(2)} A${radius} ${radius} 0 ${end - start > 180 ? 1 : 0} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
    }

    function buildHudSegments() {
        if (hudSegmentsBuilt === stacks.length) return;
        const group = $('hud-segments');
        group.innerHTML = '';
        const step = 360 / Math.max(stacks.length, 1);
        stacks.forEach((stack, index) => {
            const segment = document.createElementNS('http://www.w3.org/2000/svg', 'path');
            segment.setAttribute('d', hudArc(172, -90 + index * step + 2, -90 + (index + 1) * step - 2));
            segment.setAttribute('tabindex', '0');
            segment.setAttribute('role', 'button');
            segment.setAttribute('aria-label', `Update ${stack.name}`);
            segment.dataset.index = index;
            segment.addEventListener('mouseenter', () => highlightHudStack(index));
            segment.addEventListener('focus', () => highlightHudStack(index));
            segment.addEventListener('mouseleave', () => highlightHudStack(-1));
            segment.addEventListener('blur', () => highlightHudStack(-1));
            segment.addEventListener('click', () => triggerHudStackUpdate(stack));
            segment.addEventListener('keydown', event => {
                if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    triggerHudStackUpdate(stack);
                }
            });
            group.appendChild(segment);
        });
        hudSegmentsBuilt = stacks.length;
    }

    function highlightHudStack(index) {
        hudHighlight = index;
        document.querySelectorAll('.hud-dashboard .seg').forEach((segment, segmentIndex) => {
            segment.classList.toggle('hl', segmentIndex === index);
        });
        document.querySelectorAll('.hud-dashboard .mod').forEach(module => {
            module.classList.toggle('hl', Number(module.dataset.index) === index);
        });
        paintHudCore();
    }

    function paintHudCore() {
        const button = $('hud-run-all');
        if (currentData.is_running) {
            button.innerHTML = '<span>SCANNING</span><small>Update in progress</small>';
        } else if (hudHighlight >= 0 && stacks[hudHighlight]) {
            const stack = stacks[hudHighlight];
            button.innerHTML = `<span class="nm">${escapeHtml(stack.name)}</span><small>${stack.status === 'Active' ? 'online' : 'offline'}${stack.excluded ? ' · skipped' : ''}<br>click to update</small>`;
        } else {
            button.innerHTML = '<span>ENGAGE</span><small>Check and update<br>all stacks</small>';
        }
    }

    function renderHud() {
        buildHudSegments();
        const running = stacks.filter(stack => stack.status === 'Active').length;
        $('hud-running').innerText = running;
        $('hud-stopped').innerText = stacks.length - running;
        $('hud-last-run').innerText = currentData.last_run || 'Never';
        $('hud-result').innerText = currentData.status || 'Idle';
        $('hud-result').className = `mono ${currentData.is_running ? 'warn' : 'ok'}`;
        $('hud-portainer-status').innerText = currentData.portainer_connected ? 'ONLINE' : 'OFFLINE';
        $('hud-portainer-status').className = currentData.portainer_connected ? 'ok' : 'bad';

        const schedule = currentData.schedule || { enabled:false, interval:'1h' };
        const intervalText = { '15m':'15 minutes', '1h':'1 hour', '6h':'6 hours', daily:'day' };
        $('hud-schedule-enabled').checked = schedule.enabled;
        $('hud-schedule-interval').value = schedule.interval;
        $('hud-schedule-interval').disabled = !schedule.enabled;
        $('hud-next-run').innerText = schedule.enabled ? `Every ${intervalText[schedule.interval]}` : 'Not scheduled';

        document.querySelectorAll('#hud-segments .seg').forEach((segment, index) => {
            const stack = stacks[index];
            segment.setAttribute('class', `seg ${stack.status === 'Active' ? 'run' : 'stop'}${stack.excluded ? ' skip' : ''}${currentData.is_running ? ' busy' : ''}${index === hudHighlight ? ' hl' : ''}`);
        });
        const visible = stacks.map((stack, index) => [stack, index]).filter(([stack]) =>
            hudFilter === 'all' || (hudFilter === 'running') === (stack.status === 'Active')
        );
        $('hud-list').innerHTML = visible.map(([stack, index]) =>
            `<div class="mod${stack.status === 'Active' ? '' : ' stopped'}${stack.excluded ? ' skipped' : ''}${index === hudHighlight ? ' hl' : ''}" data-index="${index}">
                <span class="led"></span><span class="n">${escapeHtml(stack.name)}</span>
                <label class="sk switch" title="Skip global updates"><span>SKIP</span><input type="checkbox" ${stack.excluded ? 'checked' : ''} data-hud-skip="${index}" aria-label="Skip global updates for ${escapeHtml(stack.name)}"><i></i></label>
                <button class="btn" data-hud-update="${index}" ${currentData.is_running ? 'disabled' : ''}>${currentData.is_running ? '...' : 'UPDATE'}</button>
            </div>`
        ).join('');
        renderLogs($('hud-logs'), currentData.logs || []);
        paintHudCore();
    }

    async function triggerHudStackUpdate(stack) {
        if (stack.excluded && !confirm('This stack is excluded from global updates. Are you sure you want to update it now?')) return;
        await triggerUpdate(stack.id);
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
                <div class="webhook-setting">Webhook repository
                    <code>${escapeHtml((s.webhook_repositories || []).join(', ') || 'No GHCR image detected')}</code>
                </div>
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
            renderLogs($('logs-container'), currentData.logs || []);
            render();
            renderHud();
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
    async function clearLogs() {
        const response = await fetch('/api/logs', {
            method:'DELETE',
            headers:{'X-CSRF-Token':csrfToken}
        });
        if (response.ok) {
            currentData.logs = [];
            renderAllLogs();
        } else {
            alert('Could not clear logs.');
        }
    }

    $('clear-logs').onclick = clearLogs;
    $('hud-clear-logs').onclick = clearLogs;
    document.querySelectorAll('[data-log-level]').forEach(select => {
        select.value = logLevel;
        select.onchange = event => {
            logLevel = event.target.value;
            document.querySelectorAll('[data-log-level]').forEach(item => {
                item.value = logLevel;
            });
            renderAllLogs();
        };
    });
    $('hud-run-all').onclick = () => {
        if (hudHighlight >= 0 && stacks[hudHighlight]) triggerHudStackUpdate(stacks[hudHighlight]);
        else triggerUpdate();
    };
    $('hud-filters').onclick = event => {
        const button = event.target.closest('[data-hud-filter]');
        if (!button) return;
        hudFilter = button.dataset.hudFilter;
        document.querySelectorAll('[data-hud-filter]').forEach(item => {
            item.setAttribute('aria-pressed', item === button);
        });
        renderHud();
    };
    $('hud-list').addEventListener('click', event => {
        const button = event.target.closest('[data-hud-update]');
        if (button) triggerHudStackUpdate(stacks[Number(button.dataset.hudUpdate)]);
    });
    $('hud-list').addEventListener('change', event => {
        const checkbox = event.target.closest('[data-hud-skip]');
        if (checkbox) setStackExcluded(stacks[Number(checkbox.dataset.hudSkip)].id, checkbox.checked);
    });
    $('hud-list').addEventListener('mouseover', event => {
        const module = event.target.closest('.mod');
        if (module) highlightHudStack(Number(module.dataset.index));
    });
    $('hud-list').addEventListener('mouseleave', () => highlightHudStack(-1));
    $('hud-schedule-enabled').onchange = saveSchedule;
    $('hud-schedule-interval').onchange = saveSchedule;
    const updateClock = () => {
        $('clock').innerText = new Date().toLocaleTimeString('sv-SE', {
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
            hour12: false,
            timeZone: 'Europe/Stockholm'
        });
    };
    setInterval(updateClock, 1000);
    updateClock();
    applyTheme(localStorage.getItem('woods-keeper-theme') || 'light');
    applyStyle(localStorage.getItem('woods-keeper-style') || 'default');
    setInterval(fetchData, 4000);
    fetchData();
