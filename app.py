import os
import subprocess
import json
import threading
import base64
import binascii
import secrets
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, jsonify, request, send_file, session, redirect, url_for
import urllib.request
import urllib.error

from config import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    API_KEY,
    PORT,
    PORTAINER_URL,
    SECRET_KEY,
    validate_portainer_url,
)
from storage import (
    DEFAULT_SCHEDULE,
    SCHEDULE_INTERVALS,
    load_schedule,
    load_status,
    save_schedule,
    save_status,
)

app = Flask(__name__)
app.secret_key = SECRET_KEY or secrets.token_hex(32)


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

is_running = False
schedule_lock = threading.Lock()
schedule_wakeup = threading.Event()


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
        return render_template("login.html", csrf_token=get_csrf_token())
    return render_template("dashboard.html", csrf_token=get_csrf_token())


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
    return render_template(
        "login.html",
        csrf_token=get_csrf_token(),
        error="Invalid username or password.",
    ), 401


@app.route("/logout", methods=["POST"])
@requires_admin_auth
@csrf_protected
def logout():
    session.clear()
    return redirect(url_for("index"))



if __name__ == "__main__":
    validate_portainer_url()
    if not ADMIN_USERNAME or not ADMIN_PASSWORD:
        raise RuntimeError("ADMIN_USERNAME and ADMIN_PASSWORD must be configured")
    threading.Thread(target=scheduled_update_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)