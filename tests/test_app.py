import hashlib
import hmac
import json
import tempfile
import unittest
import base64
import urllib.error
from pathlib import Path
from unittest.mock import patch

import app
import storage


class WoodsKeeperTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        storage.STATUS_FILE = str(Path(self.temp_dir.name) / "status.json")
        storage.SCHEDULE_FILE = str(Path(self.temp_dir.name) / "schedule.json")
        storage.save_status({"last_run": "Never", "status": "Idle", "logs": []})
        app.GITHUB_WEBHOOK_SECRET = "test-secret"
        app.ADMIN_USERNAME = "admin"
        app.ADMIN_PASSWORD = "password"
        app.is_running = False
        with app.webhook_dedup_lock:
            app.recent_webhooks.clear()
        self.client = app.app.test_client()

    def tearDown(self):
        app.is_running = False
        self.temp_dir.cleanup()

    def signed_webhook(self, payload, event="package"):
        body = json.dumps(payload).encode()
        signature = hmac.new(
            app.GITHUB_WEBHOOK_SECRET.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
        response = self.client.post(
            "/api/webhooks/github",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": event,
                "X-Hub-Signature-256": f"sha256={signature}",
            },
        )
        return response

    def package_payload(self, repository="owner/repository", action="published"):
        return {
            "action": action,
            "repository": {"full_name": repository},
            "package": {
                "name": "example",
                "package_type": "container",
                "package_version": {
                    "container_metadata": {"tag": "latest"},
                },
            },
        }

    def github_package_payload(self):
        payload = self.package_payload()
        payload["package"]["package_type"] = "CONTAINER"
        payload["package"]["package_version"]["container_metadata"]["tag"] = {
            "name": "latest",
            "digest": "sha256:abc123",
        }
        return payload

    def admin_headers(self):
        credentials = base64.b64encode(b"admin:password").decode()
        return {"Authorization": f"Basic {credentials}"}

    def authenticated_client(self):
        with self.client.session_transaction() as session:
            session["authenticated"] = True
            session["csrf_token"] = "csrf-token"
        return self.client

    def test_valid_package_webhook_deploys_detected_stack(self):
        with (
            patch.object(app, "get_portainer_stacks", return_value=([{"Id": 7}], None)),
            patch.object(app, "_stack_repositories", return_value={"owner/repository"}),
            patch.object(app, "run_update_process") as update,
        ):
            response = self.signed_webhook(self.package_payload())

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["stack_id"], "7")
        update.assert_called_once_with("7")

    def test_github_container_package_payload_deploys_stack(self):
        with (
            patch.object(app, "get_portainer_stacks", return_value=([{"Id": 7}], None)),
            patch.object(app, "_stack_repositories", return_value={"owner/repository"}),
            patch.object(app, "run_update_process") as update,
        ):
            response = self.signed_webhook(self.github_package_payload())

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["tag"], "latest")
        update.assert_called_once_with("7")

    def test_duplicate_package_publication_does_not_redeploy(self):
        with (
            patch.object(app, "get_portainer_stacks", return_value=([{"Id": 7}], None)),
            patch.object(app, "_stack_repositories", return_value={"owner/repository"}),
            patch.object(app, "run_update_process") as update,
        ):
            first_response = self.signed_webhook(self.github_package_payload())
            duplicate_response = self.signed_webhook(self.github_package_payload())

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(duplicate_response.status_code, 202)
        self.assertEqual(
            duplicate_response.get_json()["message"],
            "Duplicate package publication ignored",
        )
        update.assert_called_once_with("7")

    def test_different_package_tag_is_not_deduplicated(self):
        first_payload = self.github_package_payload()
        second_payload = self.github_package_payload()
        second_payload["package"]["package_version"]["container_metadata"]["tag"] = {
            "name": "stable",
            "digest": "sha256:def456",
        }
        with (
            patch.object(app, "get_portainer_stacks", return_value=([{"Id": 7}], None)),
            patch.object(app, "_stack_repositories", return_value={"owner/repository"}),
            patch.object(app, "run_update_process") as update,
        ):
            self.signed_webhook(first_payload)
            self.signed_webhook(second_payload)

        self.assertEqual(update.call_count, 2)

    def test_invalid_signature_is_rejected_and_logged(self):
        response = self.client.post(
            "/api/webhooks/github",
            data=json.dumps(self.package_payload()),
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "package",
                "X-Hub-Signature-256": "sha256=invalid",
            },
        )

        self.assertEqual(response.status_code, 401)
        self.assertTrue(
            any(
                line.endswith("GitHub webhook rejected: invalid signature")
                for line in storage.load_status()["logs"]
            )
        )

    def test_non_container_package_is_ignored(self):
        payload = self.package_payload()
        payload["package"]["package_type"] = "npm"

        with patch.object(app, "run_update_process") as update:
            response = self.signed_webhook(payload)

        self.assertEqual(response.status_code, 202)
        update.assert_not_called()
        self.assertTrue(
            any(
                line.endswith(
                    "GitHub webhook ignored: event was not a container package publication"
                )
                for line in storage.load_status()["logs"]
            )
        )

    def test_ambiguous_repository_does_not_deploy(self):
        with (
            patch.object(
                app,
                "get_portainer_stacks",
                return_value=([{"Id": 1}, {"Id": 2}], None),
            ),
            patch.object(
                app,
                "_stack_repositories",
                side_effect=[{"owner/repository"}, {"owner/repository"}],
            ),
            patch.object(app, "run_update_process") as update,
        ):
            response = self.signed_webhook(self.package_payload())

        self.assertEqual(response.status_code, 202)
        update.assert_not_called()
        self.assertEqual(
            response.get_json()["message"],
            "Repository is not mapped to a stack",
        )

    def test_stack_repositories_extracts_ghcr_images(self):
        compose = """
        services:
          app:
            image: ghcr.io/owner/repository:latest
          worker:
            image: "ghcr.io/owner/worker@sha256:abc123"
          database:
            image: postgres:16
        """

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"StackFileContent": compose}).encode()

        with patch("app.urllib.request.urlopen", return_value=Response()):
            repositories = app._stack_repositories("7")

        self.assertEqual(
            repositories,
            {"owner/repository", "owner/worker"},
        )

    def test_authentication_is_required_for_dashboard_api(self):
        response = self.client.get("/api/data")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"], "Authentication required")

    def test_invalid_basic_auth_is_rejected(self):
        response = self.client.get(
            "/api/data", headers={"Authorization": "Basic not-base64"}
        )

        self.assertEqual(response.status_code, 401)

    def test_index_renders_login_and_dashboard(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.authenticated_client()
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_favicon_is_served(self):
        response = self.client.get("/favicon.ico")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/png")

    def test_basic_auth_allows_dashboard_api(self):
        stacks = [{
            "Id": 7,
            "Name": "demo",
            "EndpointId": 1,
            "Status": 1,
        }]
        with (
            patch.object(app, "get_portainer_stacks", return_value=(stacks, None)),
            patch.object(app, "_stack_repositories", return_value={"owner/demo"}),
        ):
            response = self.client.get("/api/data", headers=self.admin_headers())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["stacks"][0]["webhook_repositories"], ["owner/demo"])

    def test_api_data_reports_portainer_error(self):
        with patch.object(
            app, "get_portainer_stacks", return_value=([], "connection failed")
        ):
            response = self.client.get("/api/data", headers=self.admin_headers())

        self.assertFalse(response.get_json()["portainer_connected"])
        self.assertEqual(response.get_json()["portainer_error"], "connection failed")

    def test_csrf_is_required_for_update(self):
        client = self.authenticated_client()

        response = client.post("/api/update", json={})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "CSRF validation failed")

    def test_update_route_starts_global_update(self):
        client = self.authenticated_client()
        with patch.object(app, "run_update_process") as update:
            response = client.post(
                "/api/update",
                json={},
                headers={"X-CSRF-Token": "csrf-token"},
            )

        self.assertEqual(response.status_code, 200)
        update.assert_called_once_with(None)

    def test_schedule_route_rejects_invalid_values(self):
        client = self.authenticated_client()

        response = client.post(
            "/api/schedule",
            json={"enabled": "yes", "interval": "never"},
            headers={"X-CSRF-Token": "csrf-token"},
        )

        self.assertEqual(response.status_code, 400)

    def test_schedule_route_saves_valid_values(self):
        client = self.authenticated_client()

        response = client.post(
            "/api/schedule",
            json={"enabled": True, "interval": "15m"},
            headers={"X-CSRF-Token": "csrf-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["interval"], "15m")
        self.assertTrue(storage.load_schedule()["enabled"])

    def test_stack_settings_updates_exclusions(self):
        client = self.authenticated_client()

        response = client.post(
            "/api/stack-settings",
            json={"stack_id": 7, "excluded": True},
            headers={"X-CSRF-Token": "csrf-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(storage.load_schedule()["excluded_stack_ids"], ["7"])

        response = client.post(
            "/api/stack-settings",
            json={"stack_id": 7, "excluded": False},
            headers={"X-CSRF-Token": "csrf-token"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(storage.load_schedule()["excluded_stack_ids"], [])

    def test_stack_settings_rejects_invalid_values(self):
        client = self.authenticated_client()
        response = client.post(
            "/api/stack-settings",
            json={"stack_id": None, "excluded": "yes"},
            headers={"X-CSRF-Token": "csrf-token"},
        )

        self.assertEqual(response.status_code, 400)

    def test_clear_logs_persists_empty_log_list(self):
        storage.save_status({
            "last_run": "now",
            "status": "Success",
            "logs": ["old log"],
        })
        client = self.authenticated_client()

        response = client.delete(
            "/api/logs",
            headers={"X-CSRF-Token": "csrf-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(storage.load_status()["logs"], [])

    def test_clear_logs_requires_csrf(self):
        client = self.authenticated_client()

        response = client.delete("/api/logs")

        self.assertEqual(response.status_code, 403)

    def test_portainer_errors_are_reported(self):
        with patch(
            "app.urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(
                "url", 503, "unavailable", {}, None
            ),
        ):
            stacks, error = app.get_portainer_stacks()

        self.assertEqual(stacks, [])
        self.assertEqual(error, "Portainer returned HTTP 503")

    def test_invalid_stack_file_returns_no_repositories(self):
        with patch(
            "app.urllib.request.urlopen",
            side_effect=urllib.error.URLError("offline"),
        ):
            self.assertEqual(app._stack_repositories("7"), set())

    def test_unsupported_github_event_is_ignored(self):
        response = self.signed_webhook(self.package_payload(), event="ping")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["message"], "Event ignored")

    def test_invalid_json_webhook_is_rejected(self):
        body = b"{invalid"
        signature = hmac.new(
            app.GITHUB_WEBHOOK_SECRET.encode(), body, hashlib.sha256
        ).hexdigest()
        response = self.client.post(
            "/api/webhooks/github",
            data=body,
            headers={
                "X-GitHub-Event": "package",
                "X-Hub-Signature-256": f"sha256={signature}",
            },
        )

        self.assertEqual(response.status_code, 400)

    def test_webhook_is_rejected_while_update_is_running(self):
        app.is_running = True
        with (
            patch.object(app, "get_portainer_stacks", return_value=([{"Id": 7}], None)),
            patch.object(app, "_stack_repositories", return_value={"owner/repository"}),
        ):
            response = self.signed_webhook(self.package_payload())

        self.assertEqual(response.status_code, 409)

    def test_updated_package_event_is_supported(self):
        with (
            patch.object(app, "get_portainer_stacks", return_value=([{"Id": 7}], None)),
            patch.object(app, "_stack_repositories", return_value={"owner/repository"}),
            patch.object(app, "run_update_process") as update,
        ):
            response = self.signed_webhook(
                self.package_payload(action="updated")
            )

        self.assertEqual(response.status_code, 202)
        update.assert_called_once_with("7")

    def test_signature_validation_rejects_missing_secret_and_bad_prefix(self):
        self.assertFalse(app._valid_github_signature(b"payload", ""))
        with patch.object(app, "GITHUB_WEBHOOK_SECRET", ""):
            self.assertFalse(
                app._valid_github_signature(b"payload", "sha256=anything")
            )

    def test_github_mapping_ignores_unmapped_and_conflicting_stacks(self):
        with patch.object(
            app,
            "_stack_repositories",
            side_effect=[{"owner/one"}, {"owner/one"}, {"owner/two"}],
        ):
            mapping = app._github_stack_mapping(
                [{"Id": 1}, {"Id": 2}, {"Name": "missing"}]
            )

        self.assertEqual(mapping, {})

    def test_update_failure_is_recorded(self):
        class Process:
            returncode = 1

            def communicate(self):
                return "failed\n", None

        with patch("app.subprocess.Popen", return_value=Process()):
            app.run_update_process("7")

        self.assertEqual(storage.load_status()["status"], "Failed")
        self.assertIn("failed", storage.load_status()["logs"])

    def test_update_process_ignores_concurrent_run(self):
        app.is_running = True

        with patch("app.subprocess.Popen") as popen:
            app.run_update_process("7")

        popen.assert_not_called()

    def test_update_process_records_execution_errors(self):
        with patch("app.subprocess.Popen", side_effect=OSError("cannot execute")):
            app.run_update_process("7")

        status = storage.load_status()
        self.assertTrue(status["status"].startswith("Error:"))
        self.assertIn("Execution Error: cannot execute", status["logs"])

    def test_login_and_logout(self):
        with self.client.session_transaction() as session:
            session["csrf_token"] = "csrf-token"
        response = self.client.post(
            "/login",
            data={
                "username": "admin",
                "password": "password",
                "csrf_token": "csrf-token",
            },
        )
        self.assertEqual(response.status_code, 302)

        with self.client.session_transaction() as session:
            self.assertTrue(session["authenticated"])
        response = self.client.post(
            "/logout", headers={"X-CSRF-Token": "csrf-token"}
        )
        self.assertEqual(response.status_code, 302)

    def test_invalid_login_is_rejected(self):
        response = self.client.post(
            "/login",
            data={"username": "wrong", "password": "wrong"},
        )

        self.assertEqual(response.status_code, 401)

    def test_update_process_preserves_existing_logs(self):
        class Process:
            returncode = 0

            def communicate(self):
                return "new line one\nnew line two\n", None

        storage.save_status(
            {
                "last_run": "old",
                "status": "Success",
                "logs": ["previous line"],
            }
        )
        with patch("app.subprocess.Popen", return_value=Process()):
            app.run_update_process("7")

        logs = storage.load_status()["logs"]
        self.assertIn("previous line", logs)
        self.assertIn("new line one", logs)
        self.assertIn("new line two", logs)


if __name__ == "__main__":
    unittest.main()
