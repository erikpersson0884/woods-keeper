import os
import time
import subprocess
import json
import threading
from datetime import datetime
from flask import Flask, render_template_string, jsonify, request

app = Flask(__name__)

STATUS_FILE = "/app/data/status.json"

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

def run_update_process():
    global is_running
    if is_running:
        return
    is_running = True

    status_data = load_status()
    status_data["status"] = "Updating..."
    status_data["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_status(status_data)

    try:
        process = subprocess.Popen(
            ["/app/auto-update.sh"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )
        output, _ = process.communicate()

        status_data["logs"] = output.strip().split("\n") if output else ["No updates found or output captured."]
        status_data["status"] = "Success" if process.returncode == 0 else "Failed"
    except Exception as e:
        status_data["status"] = f"Error: {str(e)}"
        status_data["logs"].append(f"Execution Error: {str(e)}")

    is_running = False
    save_status(status_data)

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/api/status")
def status():
    data = load_status()
    data["is_running"] = is_running
    return jsonify(data)

@app.route("/api/update", methods=["POST"])
def trigger_update():
    if is_running:
        return jsonify({"message": "Update already in progress"}), 409
    
    thread = threading.Thread(target=run_update_process)
    thread.start()
    return jsonify({"message": "Update triggered successfully"})

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Portainer Stack Updater</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #f8fafc; padding: 2rem; max-width: 800px; margin: 0 auto; }
        .card { background: #1e293b; border-radius: 12px; padding: 1.5rem; box-shadow: 0 10px 15px -3px rgba(0,0,0,0.3); }
        h1 { margin-top: 0; color: #38bdf8; font-size: 1.5rem; }
        .info-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin: 1.5rem 0; }
        .info-box { background: #334155; padding: 1rem; border-radius: 8px; }
        .label { font-size: 0.85rem; color: #94a3b8; text-transform: uppercase; }
        .value { font-size: 1.1rem; font-weight: 600; margin-top: 4px; }
        button { background: #0284c7; color: white; border: none; padding: 12px 24px; border-radius: 8px; font-weight: 600; font-size: 1rem; cursor: pointer; transition: background 0.2s; width: 100%; }
        button:hover { background: #0369a1; }
        button:disabled { background: #475569; cursor: not-allowed; }
        .logs { background: #090d16; border-radius: 8px; padding: 1rem; margin-top: 1.5rem; font-family: monospace; font-size: 0.85rem; max-height: 250px; overflow-y: auto; color: #cbd5e1; }
        .badge { display: inline-block; padding: 4px 8px; border-radius: 4px; font-size: 0.8rem; }
        .badge.Idle { background: #334155; color: #94a3b8; }
        .badge.Success { background: #166534; color: #4ade80; }
        .badge.Failed { background: #991b1b; color: #fca5a5; }
        .badge.Updating\.\.\. { background: #854d0e; color: #fde047; }
    </style>
</head>
<body>
    <div class="card">
        <h1>🚀 Portainer Stack Updater</h1>
        <div class="info-grid">
            <div class="info-box">
                <div class="label">Last Execution</div>
                <div class="value" id="last-run">--</div>
            </div>
            <div class="info-box">
                <div class="label">Status</div>
                <div class="value"><span id="status-badge" class="badge Idle">Loading...</span></div>
            </div>
        </div>

        <button id="update-btn" onclick="runUpdate()">Check & Update Stacks Now</button>

        <h3>Latest Run Output</h3>
        <div class="logs" id="logs-container">No logs available yet.</div>
    </div>

    <script>
        async function fetchStatus() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();
                
                document.getElementById('last-run').innerText = data.last_run;
                const badge = document.getElementById('status-badge');
                badge.innerText = data.status;
                badge.className = 'badge ' + data.status;

                const btn = document.getElementById('update-btn');
                if (data.is_running) {
                    btn.disabled = true;
                    btn.innerText = 'Updating Stacks in Progress...';
                } else {
                    btn.disabled = false;
                    btn.innerText = 'Check & Update Stacks Now';
                }

                const logsContainer = document.getElementById('logs-container');
                if (data.logs && data.logs.length > 0) {
                    logsContainer.innerText = data.logs.join('\\n');
                }
            } catch (e) {
                console.error("Failed to fetch status", e);
            }
        }

        async function runUpdate() {
            document.getElementById('update-btn').disabled = true;
            document.getElementById('update-btn').innerText = 'Triggering...';
            await fetch('/api/update', { method: 'POST' });
            setTimeout(fetchStatus, 1000);
        }

        setInterval(fetchStatus, 3000);
        fetchStatus();
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)