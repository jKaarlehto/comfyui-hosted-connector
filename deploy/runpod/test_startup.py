"""Verify release selection and startup Git updates without cloud access."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import bootstrap
import runpod


class ReleaseTests(unittest.TestCase):
    def test_stable_resolves_latest_release_on_every_start(self):
        releases = [
            io.BytesIO(json.dumps({"tag_name": tag, "draft": False, "prerelease": False}).encode())
            for tag in ("v0.35.0", "v0.36.0")
        ]
        with patch.object(bootstrap.urllib.request, "urlopen", side_effect=releases) as open_url:
            self.assertEqual(bootstrap.resolve_comfy_ref("stable"), "refs/tags/v0.35.0")
            self.assertEqual(bootstrap.resolve_comfy_ref("stable"), "refs/tags/v0.36.0")
        self.assertEqual(open_url.call_count, 2)
        for call in open_url.call_args_list:
            self.assertEqual(call.args[0].full_url, "https://api.github.com/repos/Comfy-Org/ComfyUI/releases/latest")
            self.assertFalse(call.args[0].has_header("Authorization"))
            self.assertEqual(call.kwargs["timeout"], 30)

    def test_stable_rejects_draft_prerelease_and_invalid_tags(self):
        valid = {"tag_name": "v0.36.0", "draft": False, "prerelease": False}
        invalid = [None, {}, {**valid, "draft": True}, {**valid, "prerelease": True}]
        invalid += [{**valid, "tag_name": tag} for tag in (None, "master", "v0.36.0-rc1", "../tag", "v0.36.0\n")]
        for release in invalid:
            with (
                self.subTest(release=release),
                patch.object(
                    bootstrap.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(release).encode())
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "valid stable"):
                    bootstrap.resolve_comfy_ref("stable")

    def test_release_lookup_failure_does_not_fall_back_to_master(self):
        with patch.object(bootstrap.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")):
            with self.assertRaisesRegex(RuntimeError, "Could not resolve"):
                bootstrap.resolve_comfy_ref("stable")

    def test_explicit_refs_do_not_query_releases(self):
        with patch.object(bootstrap.urllib.request, "urlopen") as open_url:
            for revision in ("master", "feature/test", "v0.36.0", "a" * 40):
                self.assertEqual(bootstrap.resolve_comfy_ref(revision), revision)
            for revision in ("", "-flag", "main\n", "$(command)"):
                with self.assertRaises(ValueError):
                    bootstrap.resolve_comfy_ref(revision)
        open_url.assert_not_called()

    def test_new_owner_setup_defaults_to_stable_without_changing_plugin_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(sys, "argv", ["runpod.py", "setup", "--state-dir", directory]),
                patch.object(runpod, "api_key", return_value="test"),
                patch.object(runpod, "save_state"),
                patch.object(runpod, "resolve_pod"),
                patch.object(runpod, "setup") as setup,
            ):
                runpod.main()
        args = setup.call_args.args[0]
        self.assertEqual(args.comfy_ref, "stable")
        self.assertEqual(args.ref, "install-startup-requirements")


class GitStartupTests(unittest.TestCase):
    def setUp(self):
        if not shutil.which("git"):
            self.skipTest("Git is required")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.source = Path(directory.name, "source")
        self.checkout = Path(directory.name, "checkout")
        subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(self.source)], check=True)
        self.git("config", "user.name", "Startup test")
        self.git("config", "user.email", "test@example.invalid")
        self.commit("main")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.source), *args], text=True).strip()

    def commit(self, content):
        (self.source / "version.txt").write_text(content)
        self.git("add", "version.txt")
        self.git("commit", "--quiet", "-m", content)
        return self.git("rev-parse", "HEAD")

    def local_remote_env(self, url):
        return {
            **os.environ,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "url." + self.source.as_uri() + ".insteadOf",
            "GIT_CONFIG_VALUE_0": url,
            "GIT_TERMINAL_PROMPT": "0",
        }

    def test_plugin_checkout_fetches_configured_branch_again_on_restart(self):
        self.git("checkout", "--quiet", "-b", "install-startup-requirements")
        first = self.commit("plugin first")
        env = self.local_remote_env("git@github.com:test/plugin.git")
        self.assertEqual(
            bootstrap.checkout_plugin(self.checkout, "test/plugin", "install-startup-requirements", env), first
        )
        second = self.commit("plugin updated")
        self.assertEqual(
            bootstrap.checkout_plugin(self.checkout, "test/plugin", "install-startup-requirements", env), second
        )
        self.assertEqual((self.checkout / "version.txt").read_text(), "plugin updated")
        self.assertEqual(bootstrap.checkout_plugin(self.checkout, "test/plugin", first, env), first)

    def test_comfy_hook_fails_before_startup_on_fetch_or_checkout_failure(self):
        git_root = Path(shutil.which("git")).resolve().parents[1]
        bundled_bash = git_root / "bin/bash.exe"
        bash = str(bundled_bash) if bundled_bash.is_file() else shutil.which("bash")
        if not bash:
            self.skipTest("Bash is required for the startup hook")
        first = self.git("rev-parse", "HEAD")
        self.git("tag", "v0.36.0")
        self.commit("next release")
        self.git("tag", "v0.37.0")
        subprocess.run(["git", "init", "--quiet", str(self.checkout)], check=True)
        env = self.local_remote_env("https://github.com/Comfy-Org/ComfyUI.git")
        env.update(COMFYUI_DIR=self.checkout.as_posix(), NOTCH_COMFY_REF="refs/tags/v0.36.0")
        script = bootstrap.COMFY_UPDATE_SCRIPT + '\necho "STARTUP_CONTINUED"\n'
        success = subprocess.run([bash, "-c", script], env=env, capture_output=True, text=True, check=True)
        self.assertIn(first + " (refs/tags/v0.36.0)", success.stdout)
        self.assertIn("STARTUP_CONTINUED", success.stdout)
        for revision in ("missing-ref", "refs/tags/v0.37.0"):
            with self.subTest(revision=revision):
                (self.checkout / "version.txt").write_text("uncommitted work")
                failed = subprocess.run(
                    [bash, "-c", script], env={**env, "NOTCH_COMFY_REF": revision}, capture_output=True, text=True
                )
                self.assertNotEqual(failed.returncode, 0)
                self.assertNotIn("STARTUP_CONTINUED", failed.stdout)
                head = subprocess.check_output(
                    ["git", "-C", str(self.checkout), "rev-parse", "HEAD"], text=True
                ).strip()
                self.assertEqual(head, first)


if __name__ == "__main__":
    unittest.main()
