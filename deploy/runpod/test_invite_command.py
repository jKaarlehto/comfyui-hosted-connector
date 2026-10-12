"""Automatic invitation preparation without cloud mutations."""

import contextlib
import io
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import hosted


class InviteCommandTests(unittest.TestCase):
    def test_starter_polling_resolves_replacement_before_updating_access(self):
        args = types.SimpleNamespace(timeout=60)
        state = {"broker_id": "starter", "pod_id": "old"}
        responses = [
            {"id": "job1", "status": "IN_QUEUE"},
            {"status": "IN_PROGRESS"},
            {"status": "COMPLETED", "output": {"state": "starting", "message": "Recovering"}},
            {"id": "job2", "status": "IN_QUEUE"},
            {"status": "COMPLETED", "output": {"state": "ready"}},
        ]

        def resolve(key, current, path):
            current["pod_id"] = "replacement"
            return "replacement"

        def ready(args, key, current):
            self.assertEqual(current["pod_id"], "replacement")

        with (
            patch.object(hosted.owner, "request", side_effect=responses) as request,
            patch.object(hosted.owner, "resolve_pod", side_effect=resolve),
            patch.object(hosted, "require_running", side_effect=ready) as running,
            patch.object(hosted.time, "sleep"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            hosted.wait_for_workspace(args, "owner", state, Path("state.json"))
        running.assert_called_once()
        self.assertEqual([c.args[1] for c in request.call_args_list], ["POST", "GET", "GET", "POST", "GET"])
        self.assertEqual(request.call_args_list[0].args[3], {"input": {"action": "connect"}})

    def test_failure_or_timeout_does_not_update_tester_access(self):
        for result in (
            {"id": "job", "status": "FAILED"},
            {"id": "job", "status": "COMPLETED", "output": {"state": "unavailable"}},
            {"id": "job", "status": "COMPLETED", "output": {"error": "failed"}},
            {"id": "job", "status": "IN_QUEUE"},
        ):
            with (
                patch.object(hosted.owner, "request", return_value=result),
                patch.object(hosted.owner, "resolve_pod") as resolve,
                patch.object(hosted.time, "monotonic", side_effect=[0, 0, 2, 2]),
                patch.object(hosted.time, "sleep"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                with self.assertRaises(RuntimeError):
                    hosted.wait_for_workspace(types.SimpleNamespace(timeout=1), "key", {"broker_id": "starter"}, Path("state"))
                resolve.assert_not_called()

    def test_empty_installation_prepares_template_then_starter_then_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "state.json")
            args = types.SimpleNamespace(state_dir=Path(directory), env_file=Path(directory, ".env"), guest="niko")
            state = {}
            calls = []

            def template(*a, **kw):
                calls.append("template")
                path.write_text(json.dumps({"template_id": "template", "config": {"storage": "global"}}))

            def starter(a, key, current, target):
                self.assertEqual(current["template_id"], "template")
                calls.append("starter")
                current["broker_id"] = "starter"
                target.write_text(json.dumps(current))

            with (
                patch.object(hosted.subprocess, "run", side_effect=template),
                patch.object(hosted, "setup", side_effect=starter),
                patch.object(hosted, "wait_for_workspace", side_effect=lambda *a: calls.append("ready")),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                hosted.prepare_invitation(args, "key", state, path)
            self.assertEqual(calls, ["template", "starter", "ready"])

    def test_configured_workspace_does_not_repeat_setup(self):
        state = {"template_id": "template", "broker_id": "starter", "config": {"storage": "global"}}
        with (
            patch.object(hosted.subprocess, "run") as template,
            patch.object(hosted, "setup") as starter,
            patch.object(hosted, "wait_for_workspace") as ready,
        ):
            hosted.prepare_invitation(types.SimpleNamespace(guest="niko"), "key", state, Path("state"))
        template.assert_not_called()
        starter.assert_not_called()
        ready.assert_called_once()

    def test_invite_and_revoke_prepare_before_changing_legacy_access(self):
        for command in ("invite", "share", "revoke"):
            with tempfile.TemporaryDirectory() as directory:
                Path(directory, "state.json").write_text(json.dumps({"broker_id": "starter", "guests": {"niko": {}}}))
                calls = []
                with (
                    patch.object(hosted.sys, "argv", ["hosted.py", command, "--guest", "niko", "--state-dir", directory]),
                    patch.object(hosted.owner, "api_key", return_value="key"),
                    patch.object(hosted, "prepare_invitation", side_effect=lambda *a: calls.append("ready")),
                    patch.object(hosted, "wait_for_workspace", side_effect=lambda *a: calls.append("ready")),
                    patch.object(hosted, "invite", side_effect=lambda *a: calls.append("invite")),
                    patch.object(hosted, "revoke", side_effect=lambda *a: calls.append("revoke")),
                ):
                    hosted.main()
                self.assertEqual(calls, ["ready", "revoke" if command == "revoke" else "invite"])

    def test_gateway_invite_alias_does_not_start_pod(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "state.json").write_text(json.dumps({"gateway": "https://example.workers.dev"}))
            with (
                patch.object(hosted.sys, "argv", ["hosted.py", "invite", "--state-dir", directory]),
                patch.object(hosted.gateway_owner, "invite"),
                patch.object(hosted, "copy_launchers"),
                patch.object(hosted, "write_invitation_link"),
                patch.object(hosted.owner, "api_key") as key,
                patch.object(hosted, "wait_for_workspace") as ready,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                hosted.main()
            key.assert_not_called()
            ready.assert_not_called()


if __name__ == "__main__":
    unittest.main()
