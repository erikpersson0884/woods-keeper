import os
import subprocess
import json
import threading
import base64
import binascii
import secrets
from datetime import datetime
from dotenv import load_dotenv
from functools import wraps
from urllib.parse import urlparse
from flask import Flask, render_template_string, jsonify, request, send_file, session, redirect, url_for
import urllib.request
import urllib.error

load_dotenv()

app = Flask(__name__)

STATUS_FILE = "data/status.json"
SCHEDULE_FILE = "data/schedule.json"
PORTAINER_URL = os.environ.get("PORTAINER_URL", "https://localhost:9000")
API_KEY = os.environ.get("API_KEY", "")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
ALLOW_INSECURE_PORTAINER_HTTP = (
    os.environ.get("ALLOW_INSECURE_PORTAINER_HTTP", "").lower() == "true"
)
PORT=int(os.environ.get("PORT", 5000))


def validate_portainer_url():
    parsed_url = urlparse(PORTAINER_URL)
    allowed_schemes = {"https"}
    if ALLOW_INSECURE_PORTAINER_HTTP:
        allowed_schemes.add("http")
    if parsed_url.scheme not in allowed_schemes or not parsed_url.netloc:
        required_scheme = "HTTP or HTTPS" if ALLOW_INSECURE_PORTAINER_HTTP else "HTTPS"
        raise RuntimeError(
            f"PORTAINER_URL must be an absolute {required_scheme} URL"
        )


def requires_admin_auth(view):
    @wraps(view)
    def authenticated_view(*args, **kwargs):
        if session.get("authenticated") is True:
            return view(*args, **kwargs)
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Basic "):
            return _authentication_required_response()

        try:
            encoded_credentials = authorization[6:].strip()
            decoded_credentials = base64.b64decode(
                encoded_credentials, validate=True
            ).decode("utf-8")
            username, password = decoded_credentials.split(":", 1)
        except (ValueError, UnicodeDecodeError, binascii.Error):
            return _authentication_required_response()

        if not (
            secrets.compare_digest(username, ADMIN_USERNAME)
            and secrets.compare_digest(password, ADMIN_PASSWORD)
        ):
            return _authentication_required_response()

        return view(*args, **kwargs)

    return authenticated_view


def _authentication_required_response():
    response = jsonify({"error": "Authentication required"})
    response.status_code = 401
    response.headers["WWW-Authenticate"] = 'Basic realm="Woods Keeper"'
    return response


def get_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def csrf_protected(view):
    @wraps(view)
    def protected_view(*args, **kwargs):
        if session.get("authenticated") is True:
            expected_token = session.get("csrf_token")
            submitted_token = (
                request.headers.get("X-CSRF-Token")
                or request.form.get("csrf_token")
            )
            if (
                not expected_token
                or not submitted_token
                or not secrets.compare_digest(expected_token, submitted_token)
            ):
                return jsonify({"error": "CSRF validation failed"}), 403
        return view(*args, **kwargs)

    return protected_view


def is_authenticated():
    if session.get("authenticated") is True:
        return True

    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Basic "):
        return False
    try:
        decoded_credentials = base64.b64decode(
            authorization[6:].strip(), validate=True
        ).decode("utf-8")
        username, password = decoded_credentials.split(":", 1)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return False
    return (
        secrets.compare_digest(username, ADMIN_USERNAME)
        and secrets.compare_digest(password, ADMIN_PASSWORD)
    )

def load_status():
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_run": "Never", "status": "Idle", "logs": []}

def save_status(data):
    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    with open(STATUS_FILE, "w") as f:
        json.dump(data, f)

is_running = False
schedule_lock = threading.Lock()
schedule_wakeup = threading.Event()
SCHEDULE_INTERVALS = {
    "15m": 15 * 60,
    "1h": 60 * 60,
    "6h": 6 * 60 * 60,
    "daily": 24 * 60 * 60,
}
DEFAULT_SCHEDULE = {"enabled": False, "interval": "1h", "excluded_stack_ids": []}


def load_schedule():
    if os.path.exists(SCHEDULE_FILE):
        try:
            with open(SCHEDULE_FILE, "r") as f:
                schedule = json.load(f)
            if (
                isinstance(schedule, dict)
                and schedule.get("interval") in SCHEDULE_INTERVALS
            ):
                return {
                    "enabled": bool(schedule.get("enabled", False)),
                    "interval": schedule["interval"],
                    "excluded_stack_ids": [
                        str(stack_id) for stack_id in schedule.get("excluded_stack_ids", [])
                    ],
                }
        except (OSError, json.JSONDecodeError):
            pass
    return DEFAULT_SCHEDULE.copy()


def save_schedule(schedule):
    os.makedirs(os.path.dirname(SCHEDULE_FILE), exist_ok=True)
    with open(SCHEDULE_FILE, "w") as f:
        json.dump(schedule, f)


def scheduled_update_loop():
    while True:
        with schedule_lock:
            schedule = load_schedule()
        if not schedule["enabled"]:
            schedule_wakeup.wait()
            schedule_wakeup.clear()
            continue

        interval = SCHEDULE_INTERVALS[schedule["interval"]]
        if schedule_wakeup.wait(timeout=interval):
            schedule_wakeup.clear()
            continue
        run_update_process()

def get_portainer_stacks():
    try:
        req = urllib.request.Request(
            f"{PORTAINER_URL}/api/stacks",
            headers={"X-API-Key": API_KEY}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode()), None
    except urllib.error.HTTPError as error:
        return [], f"Portainer returned HTTP {error.code}"
    except (urllib.error.URLError, TimeoutError) as error:
        return [], f"Could not connect to Portainer: {error.reason if isinstance(error, urllib.error.URLError) else error}"
    except (json.JSONDecodeError, UnicodeDecodeError):
        return [], "Portainer returned an invalid response"

def run_update_process(stack_id=None):
    global is_running
    if is_running:
        return
    is_running = True

    status_data = load_status()
    status_data["status"] = "Updating..."
    status_data["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_status(status_data)

    try:
        cmd = ["./auto-update.sh"]
        process_env = os.environ.copy()
        if stack_id:
            cmd.append(str(stack_id))
        else:
            with schedule_lock:
                excluded_stack_ids = load_schedule()["excluded_stack_ids"]
            if excluded_stack_ids:
                process_env["EXCLUDED_STACK_IDS"] = ",".join(excluded_stack_ids)
            else:
                process_env.pop("EXCLUDED_STACK_IDS", None)

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=process_env
        )
        output, _ = process.communicate()

        status_data["logs"] = output.strip().split("\n") if output else ["Update completed."]
        status_data["status"] = "Success" if process.returncode == 0 else "Failed"
    except Exception as e:
        status_data["status"] = f"Error: {str(e)}"
        status_data["logs"].append(f"Execution Error: {str(e)}")

    is_running = False
    save_status(status_data)

@app.route("/")
def index():
    if not is_authenticated():
        return render_template_string(LOGIN_TEMPLATE, csrf_token=get_csrf_token())
    return render_template_string(HTML_TEMPLATE, csrf_token=get_csrf_token())


@app.route("/favicon.ico")
@requires_admin_auth
def favicon():
    favicon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "favicon.png")
    return send_file(favicon_path, mimetype="image/png")



@app.route("/api/data")
@requires_admin_auth
def api_data():
    status_data = load_status()
    with schedule_lock:
        schedule = load_schedule()
    stacks, portainer_error = get_portainer_stacks()
    
    stack_list = []
    for s in stacks:
        stack_list.append({
            "id": s.get("Id"),
            "name": s.get("Name"),
            "endpoint_id": s.get("EndpointId"),
            "status": "Active" if s.get("Status") == 1 else "Inactive",
            "excluded": str(s.get("Id")) in schedule["excluded_stack_ids"],
        })

    return jsonify({
        "last_run": status_data.get("last_run"),
        "status": status_data.get("status"),
        "logs": status_data.get("logs", []),
        "is_running": is_running,
        "schedule": schedule,
        "portainer_connected": portainer_error is None,
        "portainer_error": portainer_error,
        "stacks": stack_list
    })

@app.route("/api/update", methods=["POST"])
@requires_admin_auth
@csrf_protected
def trigger_update():
    if is_running:
        return jsonify({"message": "Update already in progress"}), 409
    
    data = request.get_json() or {}
    stack_id = data.get("stack_id")

    thread = threading.Thread(target=run_update_process, args=(stack_id,))
    thread.start()
    return jsonify({"message": "Update triggered successfully"})


@app.route("/api/schedule", methods=["POST"])
@requires_admin_auth
@csrf_protected
def update_schedule():
    data = request.get_json() or {}
    interval = data.get("interval")
    enabled = data.get("enabled")
    if interval not in SCHEDULE_INTERVALS or not isinstance(enabled, bool):
        return jsonify({
            "error": "enabled must be a boolean and interval must be one of "
            f"{', '.join(SCHEDULE_INTERVALS)}"
        }), 400

    with schedule_lock:
        schedule = load_schedule()
        schedule.update({"enabled": enabled, "interval": interval})
        save_schedule(schedule)
    schedule_wakeup.set()
    return jsonify(schedule)


@app.route("/api/stack-settings", methods=["POST"])
@requires_admin_auth
@csrf_protected
def update_stack_settings():
    data = request.get_json() or {}
    stack_id = data.get("stack_id")
    excluded = data.get("excluded")
    if stack_id is None or not isinstance(excluded, bool):
        return jsonify({"error": "stack_id and excluded are required"}), 400

    stack_id = str(stack_id)
    with schedule_lock:
        schedule = load_schedule()
        excluded_stack_ids = set(schedule["excluded_stack_ids"])
        if excluded:
            excluded_stack_ids.add(stack_id)
        else:
            excluded_stack_ids.discard(stack_id)
        schedule["excluded_stack_ids"] = sorted(excluded_stack_ids)
        save_schedule(schedule)
    return jsonify({"stack_id": stack_id, "excluded": excluded})


@app.route("/login", methods=["POST"])
def login():
    username = request.form.get("username", "")
    password = request.form.get("password", "")
    submitted_token = request.form.get("csrf_token")
    if (
        secrets.compare_digest(username, ADMIN_USERNAME)
        and secrets.compare_digest(password, ADMIN_PASSWORD)
        and submitted_token
        and secrets.compare_digest(get_csrf_token(), submitted_token)
    ):
        session["authenticated"] = True
        return redirect(url_for("index"))
    return render_template_string(
        LOGIN_TEMPLATE,
        csrf_token=get_csrf_token(),
        error="Invalid username or password.",
    ), 401


@app.route("/logout", methods=["POST"])
@requires_admin_auth
@csrf_protected
def logout():
    session.clear()
    return redirect(url_for("index"))


LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Sign in - Stack Manager</title>
    <style>
        :root { color-scheme: light; --bg:#eef1f6; --panel:#fff; --line:#dde2ec; --ink:#18202f; --muted:#617088; --accent:#2a6df4; --error:#b4443c; }
        * { box-sizing:border-box; }
        body { margin:0; min-height:100vh; display:grid; place-items:center; padding:18px; background:var(--bg); color:var(--ink); font:15px/1.5 system-ui,sans-serif; }
        .login { width:min(100%, 390px); padding:28px; background:var(--panel); border:1px solid var(--line); border-radius:14px; }
        h1 { margin:0 0 6px; font-size:24px; }
        p { margin:0 0 22px; color:var(--muted); }
        label { display:grid; gap:6px; margin-top:14px; font-weight:600; }
        input { width:100%; padding:10px 12px; border:1px solid var(--line); border-radius:8px; font:inherit; }
        button { width:100%; margin-top:22px; padding:11px 16px; border:0; border-radius:9px; background:var(--accent); color:#fff; font:600 15px system-ui,sans-serif; cursor:pointer; }
        .error { margin:0 0 12px; color:var(--error); }
    </style>
</head>
<body>
    <main class="login">
        <h1>Stack Manager</h1>
        <p>Sign in to manage your Portainer stacks.</p>
        {% if error %}<div class="error" role="alert">{{ error }}</div>{% endif %}
        <form method="post" action="/login">
            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
            <label>Username<input name="username" autocomplete="username" required autofocus></label>
            <label>Password<input type="password" name="password" autocomplete="current-password" required></label>
            <button type="submit">Sign in</button>
        </form>
    </main>
</body>
</html>
"""

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <title>Stack Manager</title>
    <link rel="icon" href="/favicon.ico" type="image/png">
    <style>
        :root { --bg:#eef1f6; --panel:#fff; --line:#dde2ec; --ink:#18202f; --muted:#617088; --accent:#2a6df4; --ok:#16895a; --ok-bg:#e0f4ea; --off:#b4443c; --off-bg:#fbe7e5; --term:#141a26; --term-ink:#c9d3e6; --track:#c8cfdd; color-scheme:light; }
        :root[data-theme="dark"] { --bg:#0e1420; --panel:#161e2e; --line:#243049; --ink:#e6ecf7; --muted:#8d9bb5; --accent:#5b8dff; --ok:#4cd39b; --ok-bg:#12372b; --off:#f08a80; --off-bg:#3b1b1d; --term:#0a0f19; --term-ink:#c9d3e6; --track:#2c3a57; color-scheme:dark; }
        * { box-sizing:border-box; }
        body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 "IBM Plex Sans", system-ui, sans-serif; }
        main { max-width:980px; margin:0 auto; padding:28px 18px 56px; display:grid; gap:18px; }
        h1 { font-size:26px; font-weight:600; margin:0; letter-spacing:-.01em; }
        h2 { font-size:17px; font-weight:600; margin:0; }
        .muted { color:var(--muted); }
        .panel { background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:22px; }
        button { font:inherit; cursor:pointer; }
        button:disabled { opacity:.6; cursor:progress; }
        :focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
        .top { display:flex; justify-content:space-between; align-items:flex-start; gap:16px; flex-wrap:wrap; }
        .theme-toggle { border:1px solid var(--line); background:var(--panel); color:var(--ink); border-radius:8px; padding:7px 12px; font-weight:500; }
        .theme-toggle:hover { border-color:var(--accent); color:var(--accent); }
        .header-actions { display:flex; gap:8px; align-items:center; }
        .logout { border:1px solid var(--line); background:var(--panel); color:var(--muted); border-radius:8px; padding:7px 12px; font:inherit; cursor:pointer; }
        .logout:hover { border-color:var(--accent); color:var(--accent); }
        .conn { display:inline-flex; align-items:center; gap:8px; font-size:13px; color:var(--muted); margin-top:6px; }
        .dot { width:8px; height:8px; border-radius:50%; background:var(--ok); flex:none; }
        .dot.off { background:var(--off); }
        .primary { background:var(--accent); color:#fff; border:0; border-radius:10px; padding:11px 20px; font-weight:600; display:inline-flex; gap:8px; align-items:center; }
        .run { display:grid; grid-template-columns:1fr auto; gap:18px 24px; align-items:center; }
        .meta { display:flex; gap:28px; flex-wrap:wrap; }
        .meta div { display:grid; gap:2px; }
        .meta span:first-child { font-size:13px; color:var(--muted); }
        .badge { display:inline-block; padding:1px 10px; border-radius:99px; font-size:13px; font-weight:500; background:var(--ok-bg); color:var(--ok); width:fit-content; }
        .badge.busy { background:var(--track); color:var(--ink); }
        .sched { grid-column:1/-1; display:flex; align-items:center; gap:12px; flex-wrap:wrap; padding-top:16px; border-top:1px solid var(--line); }
        select, input[type=search] { font:inherit; color:var(--ink); background:var(--bg); border:1px solid var(--line); border-radius:8px; padding:7px 10px; }
        select:disabled { opacity:.5; }
        .switch { position:relative; display:inline-flex; align-items:center; gap:10px; cursor:pointer; user-select:none; }
        .switch input { position:absolute; opacity:0; width:100%; height:100%; margin:0; cursor:pointer; }
        .switch i { width:36px; height:20px; border-radius:99px; background:var(--track); position:relative; transition:background .15s; flex:none; }
        .switch i:after { content:""; position:absolute; top:3px; left:3px; width:14px; height:14px; border-radius:50%; background:#fff; transition:transform .15s; }
        .switch input:checked+i { background:var(--accent); }
        .switch input:checked+i:after { transform:translateX(16px); }
        .health { display:flex; height:10px; border-radius:99px; overflow:hidden; background:var(--track); margin:14px 0 8px; }
        .health b { display:block; transition:width .3s; }
        .health .r { background:var(--ok); } .health .s { background:var(--off); }
        .legend { display:flex; gap:20px; font-size:13px; color:var(--muted); flex-wrap:wrap; }
        .legend span { display:inline-flex; align-items:center; gap:7px; }
        .bar { display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin:18px 0 14px; }
        .bar input[type=search] { flex:1; min-width:180px; }
        .chips { display:flex; gap:6px; }
        .chip { border:1px solid var(--line); background:transparent; color:var(--muted); border-radius:99px; padding:6px 14px; font-size:14px; }
        .chip[aria-pressed=true] { background:var(--ink); color:var(--panel); border-color:var(--ink); }
        .grid { display:grid; grid-template-columns:repeat(2,1fr); gap:10px; }
        .stack-card { display:grid; grid-template-columns:1fr auto; gap:8px 12px; align-items:center; border:1px solid var(--line); border-radius:12px; padding:14px 16px; background:var(--bg); }
        .stack-card h3 { margin:0; font-size:15px; font-weight:600; display:flex; align-items:center; gap:9px; min-width:0; overflow-wrap:anywhere; }
        .state { font-size:13px; color:var(--muted); margin-left:17px; }
        .stack-card.stopped h3 .dot { background:var(--off); }
        .stack-card .skip { grid-column:1/-1; font-size:13px; color:var(--muted); padding-top:10px; border-top:1px solid var(--line); justify-content:space-between; display:flex; width:100%; }
        .stack-card.skipped { opacity:.7; }
        .ghost { border:1px solid var(--line); background:var(--panel); color:var(--ink); border-radius:8px; padding:6px 14px; font-weight:500; display:inline-flex; gap:8px; align-items:center; }
        .ghost:hover { border-color:var(--accent); color:var(--accent); }
        .empty { grid-column:1/-1; text-align:center; padding:28px; color:var(--muted); }
        .logs-head { display:flex; justify-content:space-between; align-items:center; margin-bottom:14px; }
        .link { background:none; border:0; color:var(--muted); text-decoration:underline; padding:0; }
        pre { margin:0; background:var(--term); color:var(--term-ink); border-radius:10px; padding:16px; font:13px/1.7 "IBM Plex Mono", ui-monospace, monospace; max-height:260px; overflow:auto; white-space:pre-wrap; overflow-wrap:anywhere; }
        @media (max-width:720px) { .grid { grid-template-columns:1fr; } .run { grid-template-columns:1fr; } .run .primary { justify-content:center; } }
    </style>
</head>
<body>
<main>
    <header class="top">
        <div>
            <h1>Stack Manager</h1>
            <div class="conn"><span id="connection-dot" class="dot"></span><span id="portainer-status">Connecting to Portainer...</span></div>
        </div>
        <div class="header-actions">
            <button class="theme-toggle" id="theme-toggle" type="button" onclick="toggleTheme()">Dark mode</button>
            <form method="post" action="/logout">
                <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                <button class="logout" type="submit">Sign out</button>
            </form>
        </div>
    </header>

    <section class="panel run" aria-label="Global update">
        <div class="meta">
            <div><span>Last global run</span><span id="last-run">Never</span></div>
            <div><span>Result</span><span class="badge" id="status-badge">Idle</span></div>
            <div><span>Next run</span><span id="next-run">Not scheduled</span></div>
        </div>
        <button class="primary" id="global-update-btn" onclick="triggerUpdate()">Check and update all stacks</button>
        <div class="sched">
            <label class="switch"><input type="checkbox" id="schedule-enabled" onchange="saveSchedule()"><i></i>Run on a schedule</label>
            <select id="schedule-interval" onchange="saveSchedule()">
                <option value="15m">Every 15 minutes</option><option value="1h">Every hour</option>
                <option value="6h">Every 6 hours</option><option value="daily">Every day</option>
            </select>
            <span class="muted" id="schedule-status"></span>
        </div>
    </section>

    <section class="panel" aria-label="Managed stacks">
        <div class="top"><h2>Stacks</h2></div>
        <div class="health" id="health" role="img" aria-label="Stack health"></div>
        <div class="legend" id="legend"></div>
        <div class="bar">
            <input type="search" id="stack-search" placeholder="Search stacks" aria-label="Search stacks">
            <div class="chips">
                <button class="chip" data-filter="all" aria-pressed="true">All</button>
                <button class="chip" data-filter="running" aria-pressed="false">Running</button>
                <button class="chip" data-filter="stopped" aria-pressed="false">Stopped</button>
            </div>
        </div>
        <div class="grid" id="stacks-grid"><div class="empty">Loading stacks from Portainer...</div></div>
    </section>

    <section class="panel" aria-label="Execution logs">
        <div class="logs-head"><h2>Execution logs</h2><button class="link" id="clear-logs">Clear</button></div>
        <pre id="logs-container" aria-live="polite">No logs available yet.</pre>
    </section>
</main>
<script>
    let stacks = [], filter = 'all', query = '', currentData = {};
    const csrfToken = {{ csrf_token|tojson }};
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
</script>
</body>
</html>
"""

if __name__ == "__main__":
    validate_portainer_url()
    if not ADMIN_USERNAME or not ADMIN_PASSWORD:
        raise RuntimeError("ADMIN_USERNAME and ADMIN_PASSWORD must be configured")
    threading.Thread(target=scheduled_update_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)