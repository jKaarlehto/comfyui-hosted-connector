"""Check that the public publisher cannot copy private deployment files."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import prepare_site
import publish_site


class PublishSiteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name)
        for name in publish_site.SITE_FILES:
            (self.source / name).write_text("public", encoding="utf-8")
        (self.source / "downloads").mkdir()
        self.manifest = self.source / "downloads/release.json"
        self.manifest.write_text(json.dumps({"installerAvailable": False, "appInstallerAvailable": False}))

    def test_only_explicit_static_payload_is_returned(self):
        payload = publish_site.read_public_payload(self.source)
        self.assertEqual(set(payload), publish_site.SITE_FILES | {"downloads/release.json", ".nojekyll"})

    def test_preparation_accepts_existing_docs_marker(self):
        (self.source / ".nojekyll").write_bytes(b"")
        prepare_site.prepare_site(self.source)
        self.assertTrue((self.source / ".nojekyll").exists())
        publish_site.read_public_payload(self.source)

    def test_private_file_is_rejected_instead_of_silently_published(self):
        for name in (".env", "invitation.txt", "downloads/id_ed25519"):
            with self.subTest(name=name):
                path = self.source / name
                path.write_text("private")
                with self.assertRaisesRegex(RuntimeError, "Unexpected public file"):
                    publish_site.read_public_payload(self.source)
                path.unlink()

    def test_extra_directory_is_rejected(self):
        (self.source / ".runpod").mkdir()
        with self.assertRaisesRegex(RuntimeError, "Unexpected public directory"):
            publish_site.read_public_payload(self.source)

    def test_linked_file_is_rejected(self):
        original = publish_site._is_link
        with patch.object(
            publish_site, "_is_link", side_effect=lambda path: path.name == "index.html" or original(path)
        ):
            with self.assertRaisesRegex(RuntimeError, "Linked public path"):
                publish_site.read_public_payload(self.source)

    def test_manifest_cannot_advertise_missing_installer(self):
        self.manifest.write_text(json.dumps({"installerAvailable": True, "appInstallerAvailable": False}))
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            publish_site.read_public_payload(self.source)

    def test_unadvertised_package_is_rejected(self):
        (self.source / "downloads/HostedComfyUI.msix").write_bytes(b"package")
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            publish_site.read_public_payload(self.source)

    def test_plain_text_cannot_be_published_as_installer(self):
        self.manifest.write_text(json.dumps({"installerAvailable": True, "appInstallerAvailable": False}))
        (self.source / "downloads/HostedComfyUIConnector.exe").write_text("private")
        with self.assertRaisesRegex(RuntimeError, "not a Windows executable"):
            publish_site.read_public_payload(self.source)


class MixedRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.checkout = Path(self.temporary.name)
        publish_site._git(self.checkout, "init")
        publish_site._git(self.checkout, "config", "user.name", "Publisher Test")
        publish_site._git(self.checkout, "config", "user.email", "publisher@example.invalid")
        publish_site._git(self.checkout, "config", "core.autocrlf", "false")
        self.source = self.checkout / "deploy" / "runpod" / "owner.py"
        self.source.parent.mkdir(parents=True)
        self.source.write_text("original owner source")
        (self.checkout / "README.md").write_text("repository guide")
        self.payload = {name: b"public" for name in publish_site.SITE_FILES}
        self.payload[".nojekyll"] = b""
        self.payload["downloads/release.json"] = json.dumps(
            {"installerAvailable": True, "appInstallerAvailable": False}
        ).encode()
        self.payload["downloads/HostedComfyUIConnector.exe"] = b"MZfixture"
        publish_site.stage_public_payload(self.checkout, self.payload)
        publish_site._git(self.checkout, "add", "--", "README.md", "deploy")
        publish_site._git(self.checkout, "commit", "-m", "initial fixture")

    def test_only_docs_are_changed_or_staged_in_source_repository(self):
        self.source.write_text("local owner work")
        (self.checkout / "local-notes.txt").write_text("untracked work")
        revised = dict(self.payload)
        revised["index.html"] = b"updated public page"
        revised["downloads/release.json"] = json.dumps(
            {"installerAvailable": False, "appInstallerAvailable": False}
        ).encode()
        del revised["downloads/HostedComfyUIConnector.exe"]
        publish_site.stage_public_payload(self.checkout, revised)
        changed = set(publish_site._git(self.checkout, "diff", "--cached", "--name-only").splitlines())
        self.assertEqual(
            changed, {"docs/index.html", "docs/downloads/release.json", "docs/downloads/HostedComfyUIConnector.exe"}
        )
        self.assertEqual(self.source.read_text(), "local owner work")
        self.assertEqual((self.checkout / "README.md").read_text(), "repository guide")
        self.assertEqual((self.checkout / "local-notes.txt").read_text(), "untracked work")
        self.assertFalse((self.checkout / "docs/downloads/HostedComfyUIConnector.exe").exists())
        self.assertTrue((self.checkout / "docs/.nojekyll").exists())

    def test_unrelated_staged_source_blocks_publication(self):
        self.source.write_text("staged owner work")
        publish_site._git(self.checkout, "add", "--", "deploy/runpod/owner.py")
        revised = dict(self.payload, **{"index.html": b"must not write"})
        with self.assertRaisesRegex(RuntimeError, "Unrelated changes"):
            publish_site.stage_public_payload(self.checkout, revised)
        self.assertEqual((self.checkout / "docs/index.html").read_bytes(), b"public")
        self.assertEqual(self.source.read_text(), "staged owner work")

    def test_unexpected_docs_file_is_preserved_and_blocks_publication(self):
        extra = self.checkout / "docs/review-notes.txt"
        extra.write_text("preserve")
        with self.assertRaisesRegex(RuntimeError, "Unexpected public file"):
            publish_site.stage_public_payload(self.checkout, self.payload)
        self.assertEqual(extra.read_text(), "preserve")

    def test_parent_path_in_payload_is_rejected_before_changes(self):
        with self.assertRaisesRegex(RuntimeError, "outside the public allowlist"):
            publish_site.stage_public_payload(self.checkout, {"../README.md": b"overwritten"})
        self.assertEqual((self.checkout / "README.md").read_text(), "repository guide")

    def test_root_pages_configuration_is_rejected_before_git_changes(self):
        with (
            patch.object(
                publish_site, "_github", side_effect=[{"private": False}, {"source": {"branch": "main", "path": "/"}}]
            ),
            patch.object(publish_site, "_git") as git,
        ):
            with self.assertRaisesRegex(RuntimeError, "main /docs"):
                publish_site.publish(self.payload, "owner/repository", wait_seconds=0)
        git.assert_not_called()


if __name__ == "__main__":
    unittest.main()
