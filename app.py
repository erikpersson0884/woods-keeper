import os
import subprocess
import json
import threading
import base64
import binascii
import hmac
import re
import secrets
import time
from collections import deque
from datetime import datetime
from functools import wraps
from zoneinfo import ZoneInfo
from flask import Flask, render_template, jsonify, request, send_file, session, redirect, url_for
import urllib.request
import urllib.error

from config import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    API_KEY,
    GITHUB_WEBHOOK_SECRET,
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
SWEDEN_TIMEZONE = ZoneInfo("Europe/Stockholm")


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
update_lock = threading.Lock()
active_stack_ids = set()
pending_stack_ids = deque()
pending_stack_id_set = set()
MAX_CONCURRENT_UPDATES = 3
schedule_lock = threading.Lock()
schedule_wakeup = threading.Event()
status_lock = threading.Lock()
webhook_dedup_lock = threading.Lock()
recent_webhooks = {}
WEBHOOK_DEDUPLICATION_SECONDS = 10 * 60
MAX_RECENT_WEBHOOKS = 256


def _is_duplicate_webhook(key):
    now = time.monotonic()
    with webhook_dedup_lock:
        expired_keys = [
            recent_key
            for recent_key, timestamp in recent_webhooks.items()
            if now - timestamp >= WEBHOOK_DEDUPLICATION_SECONDS
        ]
        for expired_key in expired_keys:
            del recent_webhooks[expired_key]

        if key in recent_webhooks:
            return True

        recent_webhooks[key] = now
        while len(recent_webhooks) > MAX_RECENT_WEBHOOKS:
            del recent_webhooks[next(iter(recent_webhooks))]
    return False


def append_log(message):
    with status_lock:
        status_data = load_status()
        logs = status_data.get("logs", [])
        if not isinstance(logs, list):
            logs = []
        logs.append(
            f"{datetime.now(SWEDEN_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')} {message}"
        )
        status_data["logs"] = logs[-1000:]
        save_status(status_data)


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
        queue_global_updates()

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

def _set_running_state():
    global is_running
    is_running = bool(active_stack_ids or pending_stack_ids)


def _stack_is_updating(stack_id):
    with update_lock:
        normalized_id = str(stack_id)
        return (
            normalized_id in active_stack_ids
            or normalized_id in pending_stack_id_set
        )


def _start_pending_updates():
    while True:
        with update_lock:
            if len(active_stack_ids) >= MAX_CONCURRENT_UPDATES or not pending_stack_ids:
                _set_running_state()
                return
            stack_id = pending_stack_ids.popleft()
            pending_stack_id_set.discard(stack_id)
            if stack_id in active_stack_ids:
                continue
            active_stack_ids.add(stack_id)
            _set_running_state()
        threading.Thread(
            target=_run_queued_update,
            args=(stack_id,),
            daemon=True,
        ).start()


def queue_stack_update(stack_id, priority=False):
    stack_id = str(stack_id)
    with update_lock:
        if stack_id in active_stack_ids:
            if stack_id not in pending_stack_id_set:
                if priority:
                    pending_stack_ids.appendleft(stack_id)
                else:
                    pending_stack_ids.append(stack_id)
                pending_stack_id_set.add(stack_id)
                append_log(f"stack {stack_id} update queued")
            _set_running_state()
            return "queued"
        if stack_id in pending_stack_id_set:
            return "queued"
        if len(active_stack_ids) < MAX_CONCURRENT_UPDATES:
            active_stack_ids.add(stack_id)
            _set_running_state()
            threading.Thread(
                target=_run_queued_update,
                args=(stack_id,),
                daemon=True,
            ).start()
            return "started"
        if priority:
            pending_stack_ids.appendleft(stack_id)
        else:
            pending_stack_ids.append(stack_id)
        pending_stack_id_set.add(stack_id)
        _set_running_state()
    append_log(f"stack {stack_id} update queued")
    return "queued"


def _run_queued_update(stack_id):
    try:
        run_update_process(stack_id)
    finally:
        with update_lock:
            active_stack_ids.discard(str(stack_id))
            _set_running_state()
        _start_pending_updates()


def queue_global_updates():
    stacks, portainer_error = get_portainer_stacks()
    if portainer_error is not None:
        append_log(f"global update could not list stacks: {portainer_error}")
        return
    with schedule_lock:
        excluded_stack_ids = set(load_schedule()["excluded_stack_ids"])
    for stack in stacks:
        stack_id = stack.get("Id")
        if stack_id is not None and str(stack_id) not in excluded_stack_ids:
            queue_stack_update(stack_id)
    _start_pending_updates()


def run_update_process(stack_id):
    output_lines = []
    final_status = "Failed"

    with status_lock:
        status_data = load_status()
        logs = status_data.get("logs", [])
        if not isinstance(logs, list):
            logs = []
        status_data["status"] = "Updating..."
        status_data["last_run"] = datetime.now(SWEDEN_TIMEZONE).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        target = f"stack {stack_id}"
        logs.extend(["", f"--- update started: {status_data['last_run']} ({target}) ---"])
        status_data["logs"] = logs[-1000:]
        save_status(status_data)

    try:
        cmd = ["./stack-updater.sh"]
        process_env = os.environ.copy()
        cmd.append(str(stack_id))

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=process_env
        )
        output, _ = process.communicate()

        output_lines = output.splitlines() if output else ["Update completed."]
        final_status = "Success" if process.returncode == 0 else "Failed"
    except Exception as e:
        final_status = f"Error: {str(e)}"
        output_lines.append(f"Execution Error: {str(e)}")

    with status_lock:
        latest_status = load_status()
        latest_logs = latest_status.get("logs", [])
        if not isinstance(latest_logs, list):
            latest_logs = []
        latest_status["last_run"] = status_data["last_run"]
        latest_status["status"] = final_status
        latest_status["logs"] = (latest_logs + output_lines)[-1000:]
        save_status(latest_status)


def _stack_repositories(stack_id):
    try:
        req = urllib.request.Request(
            f"{PORTAINER_URL}/api/stacks/{stack_id}/file",
            headers={"X-API-Key": API_KEY},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            stack_file = json.loads(response.read().decode()).get("StackFileContent", "")
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ):
        return set()

    if not isinstance(stack_file, str):
        return set()

    repositories = set()
    for image in re.findall(r"(?m)^\s*image:\s*[\"']?([^\"'\s#]+)", stack_file):
        image = image.split("@", 1)[0].split(":", 1)[0]
        parts = image.split("/")
        if len(parts) >= 3 and parts[0].lower() == "ghcr.io":
            repositories.add("/".join(parts[1:]))
    return repositories


def _github_stack_mapping(stacks):
    mappings = {}
    for stack in stacks:
        stack_id = stack.get("Id")
        if stack_id is None:
            continue
        for repository in _stack_repositories(stack_id):
            stack_id = str(stack_id)
            if repository in mappings and mappings[repository] != stack_id:
                mappings[repository] = None
            elif repository not in mappings:
                mappings[repository] = stack_id
    return {
        repository: stack_id
        for repository, stack_id in mappings.items()
        if stack_id is not None
    }


def _valid_github_signature(payload, signature):
    if not GITHUB_WEBHOOK_SECRET or not signature.startswith("sha256="):
        return False
    expected = hmac.new(
        GITHUB_WEBHOOK_SECRET.encode("utf-8"),
        payload,
        "sha256",
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


@app.route("/")
def index():
    if not is_authenticated():
        return render_template("login.html", csrf_token=get_csrf_token())
    return render_template("dashboard.html", csrf_token=get_csrf_token())


@app.route("/favicon.ico")
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
        stack_id = s.get("Id")
        stack_list.append({
            "id": stack_id,
            "name": s.get("Name"),
            "endpoint_id": s.get("EndpointId"),
            "status": "Active" if s.get("Status") == 1 else "Inactive",
            "excluded": str(s.get("Id")) in schedule["excluded_stack_ids"],
            "updating": _stack_is_updating(stack_id),
            "webhook_repositories": sorted(
                _stack_repositories(stack_id)
            ) if stack_id is not None else [],
        })

    return jsonify({
        "last_run": status_data.get("last_run"),
        "status": status_data.get("status"),
        "logs": status_data.get("logs", []),
        "is_running": is_running,
        "schedule": schedule,
        "portainer_connected": portainer_error is None,
        "portainer_error": portainer_error,
        "stacks": stack_list,
    })

@app.route("/api/update", methods=["POST"])
@requires_admin_auth
@csrf_protected
def trigger_update():
    data = request.get_json() or {}
    stack_id = data.get("stack_id")

    if stack_id is None:
        threading.Thread(target=queue_global_updates, daemon=True).start()
        return jsonify({"message": "Global update queued"})
    result = queue_stack_update(stack_id, priority=True)
    return jsonify({
        "message": "Update queued" if result == "queued" else "Update triggered successfully"
    })


@app.route("/api/webhooks/github", methods=["POST"])
def github_webhook():
    payload = request.get_data()
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not _valid_github_signature(payload, signature):
        append_log("GitHub webhook rejected: invalid signature")
        return jsonify({"error": "Invalid webhook signature"}), 401

    github_event = request.headers.get("X-GitHub-Event")
    if github_event not in {"package", "registry_package"}:
        append_log("GitHub webhook ignored: unsupported event")
        return jsonify({"message": "Event ignored"}), 202

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        append_log("GitHub webhook rejected: invalid JSON payload")
        return jsonify({"error": "Invalid JSON payload"}), 400

    repository = data.get("repository", {}).get("full_name")
    if repository:
        append_log(f"GitHub webhook received for {repository}")
    package = data.get("package")
    package_version = package.get("package_version", {}) if isinstance(package, dict) else {}
    container_metadata = (
        package_version.get("container_metadata", {})
        if isinstance(package_version, dict)
        else {}
    )
    package_name = package.get("name", "unknown") if isinstance(package, dict) else "unknown"
    package_type = package.get("package_type") if isinstance(package, dict) else None
    package_type = package_type.lower() if isinstance(package_type, str) else None
    package_tag = (
        container_metadata.get("tag", "unknown")
        if isinstance(container_metadata, dict)
        else "unknown"
    )
    if isinstance(package_tag, dict):
        package_tag = package_tag.get("name") or package_tag.get("digest", "unknown")
    if (
        data.get("action") not in {"published", "updated"}
        or not isinstance(package, dict)
        or package_type != "container"
        or not repository
    ):
        append_log("GitHub webhook ignored: event was not a container package publication")
        return jsonify({"message": "Container package event did not qualify for deployment"}), 202
    append_log(
        f"GitHub package {data['action']} for {repository}: "
        f"{package_name}:{package_tag}"
    )

    stacks, portainer_error = get_portainer_stacks()
    stack_id = (
        _github_stack_mapping(stacks).get(repository)
        if portainer_error is None
        else None
    )
    if not stack_id:
        append_log(f"GitHub webhook ignored for {repository}: repository is not mapped to a stack")
        return jsonify({"message": "Repository is not mapped to a stack"}), 202

    deduplication_key = (repository, package_name, str(package_tag))
    if _is_duplicate_webhook(deduplication_key):
        append_log(
            f"GitHub webhook ignored for {repository}: duplicate package publication"
        )
        return jsonify({"message": "Duplicate package publication ignored"}), 202

    append_log(f"GitHub webhook accepted for {repository}; deploying stack {stack_id}")
    update_status = queue_stack_update(stack_id, priority=True)
    return jsonify({
        "message": "Stack deployment queued" if update_status == "queued" else "Stack deployment triggered",
        "repository": repository,
        "stack_id": stack_id,
        "package": package_name,
        "tag": package_tag,
    }), 202


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


@app.route("/api/logs", methods=["DELETE"])
@requires_admin_auth
@csrf_protected
def clear_logs():
    with status_lock:
        status_data = load_status()
        status_data["logs"] = []
        save_status(status_data)
    return jsonify({"message": "Logs cleared"})


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