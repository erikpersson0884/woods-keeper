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
from flask import Flask, render_template_string, jsonify, request, send_file
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
                }
        except (OSError, json.JSONDecodeError):
            pass
    return {"enabled": False, "interval": "1h"}


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
        if stack_id:
            cmd.append(str(stack_id))

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
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
    return render_template_string(HTML_TEMPLATE)


@app.route("/favicon.ico")
def favicon():
    favicon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "favicon.png")
    return send_file(favicon_path, mimetype="image/png")



@app.route("/api/data")
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
            "status": "Active" if s.get("Status") == 1 else "Inactive"
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
def update_schedule():
    data = request.get_json() or {}
    interval = data.get("interval")
    enabled = data.get("enabled")
    if interval not in SCHEDULE_INTERVALS or not isinstance(enabled, bool):
        return jsonify({
            "error": "enabled must be a boolean and interval must be one of "
            f"{', '.join(SCHEDULE_INTERVALS)}"
        }), 400

    schedule = {"enabled": enabled, "interval": interval}
    with schedule_lock:
        save_schedule(schedule)
    schedule_wakeup.set()
    return jsonify(schedule)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Portainer Stack Manager</title>
    <link rel="icon" href="/favicon.ico" type="image/png">

    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #f8fafc; padding: 2rem; max-width: 900px; margin: 0 auto; }
        .card { background: #1e293b; border-radius: 12px; padding: 1.5rem; box-shadow: 0 10px 15px -3px rgba(0,0,0,0.3); margin-bottom: 1.5rem; }
        h1, h2 { margin-top: 0; color: #38bdf8; }
        table { width: 100%; border-collapse: collapse; margin-top: 1rem; }
        th, td { text-align: left; padding: 12px; border-bottom: 1px solid #334155; }
        th { color: #94a3b8; font-size: 0.85rem; text-transform: uppercase; }
        button { background: #0284c7; color: white; border: none; padding: 8px 16px; border-radius: 6px; font-weight: 600; cursor: pointer; transition: background 0.2s; }
        button:hover { background: #0369a1; }
        button:disabled { background: #475569; cursor: not-allowed; }
        .btn-global { width: 100%; padding: 12px; font-size: 1rem; margin-top: 1rem; background: #059669; }
        .btn-global:hover { background: #047857; }
        .logs { background: #090d16; border-radius: 8px; padding: 1rem; margin-top: 1rem; font-family: monospace; font-size: 0.85rem; max-height: 200px; overflow-y: auto; color: #cbd5e1; }
        .badge { display: inline-block; padding: 4px 8px; border-radius: 4px; font-size: 0.8rem; }
        .badge.Idle { background: #334155; color: #94a3b8; }
        .badge.Success { background: #166534; color: #4ade80; }
        .badge.Failed { background: #991b1b; color: #fca5a5; }
        .badge.Updating\.\.\. { background: #854d0e; color: #fde047; }
        .schedule { display: flex; gap: 0.75rem; align-items: center; flex-wrap: wrap; margin-top: 1rem; }
        .schedule select, .schedule button { padding: 8px; border-radius: 6px; border: 1px solid #475569; }
        .schedule select { background: #0f172a; color: #f8fafc; }
        .schedule-status { color: #94a3b8; font-size: 0.9rem; }
    </style>
</head>
<body>
    <div class="card">
        <h1>⚙️ Portainer Stack Manager</h1>
        <div style="display: flex; gap: 1rem; align-items: center;">
            <div><strong>Last Global Run:</strong> <span id="last-run">--</span></div>
            <div><strong>Status:</strong> <span id="status-badge" class="badge Idle">Loading...</span></div>
        </div>

        <button class="btn-global" id="global-update-btn" onclick="triggerUpdate()">Check & Update All Stacks Now</button>
        <div class="schedule">
            <label>
                <input type="checkbox" id="schedule-enabled" onchange="saveSchedule()">
                Run global update on schedule
            </label>
            <select id="schedule-interval" onchange="saveSchedule()">
                <option value="15m">Every 15 minutes</option>
                <option value="1h">Every hour</option>
                <option value="6h">Every 6 hours</option>
                <option value="daily">Once a day</option>
            </select>
            <span class="schedule-status" id="schedule-status"></span>
        </div>
    </div>

    <div class="card">
        <h2>Managed Stacks</h2>
        <div id="portainer-status" style="margin-top: 0.5rem; color: #4ade80;"></div>
        <table>
            <thead>
                <tr>
                    <th>Stack Name</th>
                    <th>Status</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody id="stacks-table">
                <tr><td colspan="3" style="text-align: center; color: #94a3b8;">Loading stacks from Portainer...</td></tr>
            </tbody>
        </table>
    </div>

    <div class="card">
        <h2>Execution Logs</h2>
        <div class="logs" id="logs-container">No logs available yet.</div>
    </div>

    <script>
        async function fetchData() {
            try {
                const res = await fetch('/api/data');
                const data = await res.json();

                const portainerStatus = document.getElementById('portainer-status');
                if (data.portainer_connected) {
                    portainerStatus.innerText = 'Connected to Portainer';
                    portainerStatus.style.color = '#4ade80';
                } else {
                    portainerStatus.innerText = data.portainer_error;
                    portainerStatus.style.color = '#fca5a5';
                }
                
                document.getElementById('last-run').innerText = data.last_run;
                if (data.schedule) {
                    document.getElementById('schedule-enabled').checked = data.schedule.enabled;
                    document.getElementById('schedule-interval').value = data.schedule.interval;
                    document.getElementById('schedule-status').innerText = data.schedule.enabled
                        ? 'Enabled'
                        : 'Disabled';
                }
                const badge = document.getElementById('status-badge');
                badge.innerText = data.status;
                badge.className = 'badge ' + data.status;

                const btn = document.getElementById('global-update-btn');
                if (data.is_running) {
                    btn.disabled = true;
                    btn.innerText = 'Update in Progress...';
                } else {
                    btn.disabled = false;
                    btn.innerText = 'Check & Update All Stacks Now';
                }

                const tbody = document.getElementById('stacks-table');
                if (data.stacks && data.stacks.length > 0) {
                    tbody.innerHTML = data.stacks.map(s => `
                        <tr>
                            <td><strong>${s.name}</strong></td>
                            <td>${s.status}</td>
                            <td><button onclick="triggerUpdate(${s.id})" ${data.is_running ? 'disabled' : ''}>Update Stack</button></td>
                        </tr>
                    `).join('');
                } else {
                    tbody.innerHTML = `<tr><td colspan="3" style="text-align: center; color: #94a3b8;">No stacks found or failed to connect to Portainer API.</td></tr>`;
                }

                const logsContainer = document.getElementById('logs-container');
                if (data.logs && data.logs.length > 0) {
                    logsContainer.innerText = data.logs.join('\\n');
                }
            } catch (e) {
                console.error("Failed to fetch dashboard data", e);
            }
        }

        async function triggerUpdate(stackId = null) {
            document.getElementById('global-update-btn').disabled = true;
            await fetch('/api/update', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ stack_id: stackId })
            });
            setTimeout(fetchData, 1000);
        }

        async function saveSchedule() {
            const status = document.getElementById('schedule-status');
            status.innerText = 'Saving...';
            const response = await fetch('/api/schedule', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    enabled: document.getElementById('schedule-enabled').checked,
                    interval: document.getElementById('schedule-interval').value
                })
            });
            status.innerText = response.ok ? 'Saved' : 'Save failed';
            if (!response.ok) {
                setTimeout(fetchData, 500);
            }
        }

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