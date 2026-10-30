import hashlib
import hmac
import json
import tempfile
import unittest
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
        app.is_running = False
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
