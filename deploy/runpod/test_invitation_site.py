"""Check private invitation links and the public asset boundary without network access."""

import base64
import contextlib
import hashlib
import io
import json
import sys
import tempfile
import types
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

import hosted
import prepare_site


class InvitationSiteTests(unittest.TestCase):
    def test_link_reuses_existing_invitation_offline_without_printing_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            folder = state_dir / "shares" / "alice"
            folder.mkdir(parents=True)
            payload = b'{"version":1,"endpoint":"fixture123","key":"private-fixture","ssh_key":"private-key"}'
            invitation = base64.b64encode(payload).decode()
            (folder / "invitation.txt").write_text(invitation)
            state = {"pod_id": "fixture", "guests": {"alice": {}}, "invitation_site": "https://example.test/connect/"}
            (state_dir / "state.json").write_text(json.dumps(state))
            output = io.StringIO()
            with (
                patch.object(hosted.sys, "argv", ["hosted.py", "link", "--state-dir", directory, "--guest", "alice"]),
                patch.object(hosted.owner, "private_file") as secure,
                patch.object(hosted.owner, "api_key", side_effect=AssertionError("Network credentials not needed")),
                contextlib.redirect_stdout(output),
            ):
                hosted.main()
            link = (folder / "invitation-link.txt").read_text().strip()
            self.assertEqual(link.split("#")[0], "https://example.test/connect/")
            token = link.split("#")[1]
            self.assertEqual(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)), payload)
            self.assertEqual((folder / "invitation.txt").read_text(), invitation)
            self.assertNotIn(token, output.getvalue())
            self.assertNotIn("private-fixture", output.getvalue())
            secure.assert_called_once_with((folder / "invitation-link.txt").resolve())

    def test_rejects_unsafe_site_urls(self):
        for url in (
            "http://example.test",
            "https://user:pass@example.test",
            "https://example.test/?secret=a",
            "https://example.test/#x",
            "javascript:alert(1)",
            "https://example.test/\n",
        ):
            with self.subTest(url=url), self.assertRaises(RuntimeError):
                hosted.invitation_site(url)
        self.assertEqual(hosted.invitation_site("https://example.test/subdir"), "https://example.test/subdir/")

    def test_revoked_or_unknown_guest_cannot_get_link(self):
        args = types.SimpleNamespace(guest="revoked", state_dir=Path("unused"))
        with self.assertRaisesRegex(RuntimeError, "share first"):
            hosted.write_invitation_link(args, {"guests": {}, "invitation_site": "https://example.test/"})

    def test_public_staging_only_includes_explicit_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installer = root / "fixture.exe"
            installer.write_bytes(b"test installer")
            (root / ".env").write_text("OWNER_SECRET=must-not-ship")
            output = root / "public"
            prepare_site.prepare_site(output, installer)
            files = {path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()}
            self.assertEqual(
                files, set(prepare_site.SITE_FILES) | {"downloads/HostedComfyUIConnector.exe", "downloads/release.json"}
            )
            release = json.loads((output / "downloads" / "release.json").read_text())
            self.assertEqual(release, {"installerAvailable": True, "appInstallerAvailable": False})
            self.assertIn(
                'href="downloads/HostedComfyUIConnector.exe?v=' + hashlib.sha256(installer.read_bytes()).hexdigest() + '"',
                (output / "index.html").read_text(encoding="utf-8"),
            )
            (output / ".env").write_text("owner-secret")
            with self.assertRaisesRegex(RuntimeError, "unexpected file"):
                prepare_site.prepare_site(output, installer)

    def test_unsigned_msix_is_not_staged(self):
        with (
            patch.object(
                sys,
                "argv",
                [
                    "prepare_site.py",
                    "--output",
                    "unused",
                    "--appinstaller",
                    "fixture.appinstaller",
                    "--package",
                    "fixture.msix",
                ],
            ),
            patch.object(prepare_site.subprocess, "run", side_effect=RuntimeError("Untrusted signature")),
            patch.object(prepare_site, "prepare_site") as stage,
        ):
            with self.assertRaisesRegex(RuntimeError, "Untrusted signature"):
                prepare_site.main()
            stage.assert_not_called()

    def test_staged_html_references_exact_staged_asset_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            prepare_site.prepare_site(output)
            for page, assets in (
                ("index.html", ("connect.js", "style.css")),
                ("installed.html", ("installed.js", "style.css")),
            ):
                references = HtmlAssets()
                references.feed((output / page).read_text(encoding="utf-8"))
                self.assertEqual(
                    set(references.urls),
                    {asset + "?v=" + hashlib.sha256((output / asset).read_bytes()).hexdigest() for asset in assets},
                )

    def test_asset_version_changes_only_for_changed_bytes(self):
        site_files = {
            "index.html": b'<link href="style.css"><script src="connect.js"></script>',
            "installed.html": b"<link href='style.css'><script src='installed.js'></script>",
            "style.css": b"body { color: black; }",
            "connect.js": b"first connect script",
            "installed.js": b"first installed script",
        }
        prepare_site.version_site_assets(site_files)
        original = dict(site_files)
        prepare_site.version_site_assets(site_files)
        self.assertEqual(site_files, original, "Repeated staging must produce stable URLs")
        site_files["connect.js"] = b"changed connect script"
        prepare_site.version_site_assets(site_files)
        self.assertNotEqual(site_files["index.html"], original["index.html"])
        self.assertEqual(site_files["installed.html"], original["installed.html"])
        self.assertIn(hashlib.sha256(site_files["connect.js"]).hexdigest().encode(), site_files["index.html"])
        site_files["style.css"] = b"changed shared style"
        prepare_site.version_site_assets(site_files)
        style_hash = hashlib.sha256(site_files["style.css"]).hexdigest().encode()
        self.assertIn(style_hash, site_files["index.html"])
        self.assertIn(style_hash, site_files["installed.html"])

    def test_unexpected_asset_reference_stops_staging(self):
        site_files = {
            "index.html": b'<script src="https://example.test/connect.js"></script>',
            "installed.html": b"",
            "connect.js": b"script",
        }
        with self.assertRaisesRegex(RuntimeError, "expected local asset"):
            prepare_site.version_site_assets(site_files)


class HtmlAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script" and "src" in attributes:
            self.urls.append(attributes["src"])
        elif tag == "link" and attributes.get("rel") == "stylesheet":
            self.urls.append(attributes["href"])


if __name__ == "__main__":
    unittest.main()
