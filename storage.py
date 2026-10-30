import json
import os

STATUS_FILE = "data/status.json"
SCHEDULE_FILE = "data/schedule.json"
WEBHOOK_FILE = "data/webhooks.json"
SCHEDULE_INTERVALS = {
    "15m": 15 * 60,
    "1h": 60 * 60,
    "6h": 6 * 60 * 60,
    "daily": 24 * 60 * 60,
}
DEFAULT_SCHEDULE = {"enabled": False, "interval": "1h", "excluded_stack_ids": []}


def load_status():
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, "r") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return {"last_run": "Never", "status": "Idle", "logs": []}


def save_status(data):
    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    with open(STATUS_FILE, "w") as f:
        json.dump(data, f)


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
                        str(stack_id)
                        for stack_id in schedule.get("excluded_stack_ids", [])
                    ],
                }
        except (OSError, json.JSONDecodeError):
            pass
    return DEFAULT_SCHEDULE.copy()


def save_schedule(schedule):
    os.makedirs(os.path.dirname(SCHEDULE_FILE), exist_ok=True)
    with open(SCHEDULE_FILE, "w") as f:
        json.dump(schedule, f)


def load_webhook_mappings():
    if os.path.exists(WEBHOOK_FILE):
        try:
            with open(WEBHOOK_FILE, "r") as f:
                mappings = json.load(f)
            if isinstance(mappings, dict):
                return {
                    str(repository): str(stack_id)
                    for repository, stack_id in mappings.items()
                    if isinstance(repository, str) and repository
                }
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def save_webhook_mappings(mappings):
    os.makedirs(os.path.dirname(WEBHOOK_FILE), exist_ok=True)
    with open(WEBHOOK_FILE, "w") as f:
        json.dump(mappings, f)
