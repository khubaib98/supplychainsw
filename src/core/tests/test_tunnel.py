import base64
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from core import desktop, tunnel

# What cloudflared 2026.10.1 prints for a temporary address (shortened).
TEMPORARY_OUTPUT = """\
2026-10-18T17:53:09Z INF Thank you for trying Cloudflare Tunnel. Doing so, without a Cloudflare account, is a quick way to experiment and try it out. See https://www.cloudflare.com/website-terms/
2026-10-18T17:53:09Z INF Requesting new quick Tunnel on trycloudflare.com...
2026-10-18T17:53:09Z INF +--------------------------------------------------------------------------------------------+
2026-10-18T17:53:09Z INF |  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |
2026-10-18T17:53:09Z INF |  https://brave-otter-sample-words.trycloudflare.com                                        |
2026-10-18T17:53:09Z INF +--------------------------------------------------------------------------------------------+
2026-10-18T17:53:09Z INF Version 2026.10.1 (Checksum 3bfc539f45d2620c71debb7a86be0efa737ed5ccbad9eb85c787116900e5c586)
2026-10-18T17:53:21Z INF |  DNS Resolution    region1.v2.argotunnel.com  FAIL    Could not lookup srv records  |
2026-10-18T17:53:22Z INF Registered tunnel connection connIndex=0 connection=0f1e event=0 ip=198.41.192.7 location=khi01 protocol=quic
"""
EDGE_ERROR = """\
2026-10-18T17:53:21Z ERR edge discovery: error looking up Cloudflare edge IPs: the DNS query failed error="lookup _v2-origintunneld._tcp.argotunnel.com on 8.8.8.8:53: no such host" event=0
2026-10-18T17:53:21Z ERR Please try the following things to diagnose this issue: event=0
2026-10-18T17:53:21Z ERR   1. ensure that argotunnel.com is returning "origintunneld" service records. event=0
2026-10-18T17:53:21Z ERR      https://developers.cloudflare.com/1.1.1.1/setting-up-1.1.1.1/ event=0
"""


def dashboard_line(service, hostname="portal.example.com"):
    config = json.dumps({"ingress": [{"hostname": hostname, "originRequest": {}, "service": service},
                                     {"service": "http_status:404"}], "warp-routing": {"enabled": False}})
    return f"2026-10-18T18:00:00Z INF Updated to new configuration config={json.dumps(config)} version=3"


def token(tunnel_id="5e1f2a7b-9c3d-4e5f-8a6b-7c8d9e0f1a2b"):
    content = {"a": "0123456789abcdef0123456789abcdef", "t": tunnel_id, "s": base64.b64encode(b"s" * 32).decode()}
    return base64.b64encode(json.dumps(content).encode()).decode()


class TunnelTestCase(SimpleTestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        desktop.prepare_home(self.home)
        self.reports = []
        self.tunnel = tunnel.Tunnel(self.home, self.home / "program", report=self.reports.append)
        self.stop = threading.Event()

    def feed(self, text, mode="temporary", port=8000):
        self.tunnel.wanted = (mode, "", port)
        for line in text.splitlines():
            self.tunnel._read(line, mode, port, self.stop)


class OutputTests(TunnelTestCase):
    def test_temporary_address_and_connection(self):
        self.feed(TEMPORARY_OUTPUT)
        state = desktop.read_tunnel_state(self.home, self.tunnel.session)
        self.assertEqual(state["url"], "https://brave-otter-sample-words.trycloudflare.com")
        self.assertEqual((state["state"], state["location"], state["mode"]), ("connected", "khi01", "temporary"))
        self.assertIn("Internet address (temporary): https://brave-otter-sample-words.trycloudflare.com",
                      self.reports[0])
        self.assertEqual(self.tunnel._last_error, "")  # the pre-check box is not an error

    def test_state_of_another_launcher_is_ignored(self):
        self.feed(TEMPORARY_OUTPUT)
        self.assertEqual(desktop.read_tunnel_state(self.home, "another-session"), {})
        self.assertEqual(desktop.read_tunnel_state(self.home, ""), {})

    def test_the_main_error_is_kept_not_the_hints(self):
        self.feed(EDGE_ERROR)
        self.assertTrue(self.tunnel._last_error.startswith("edge discovery: error looking up Cloudflare edge IPs"))
        self.feed("failed to request quick Tunnel: Post \"https://api.trycloudflare.com/tunnel\": dial tcp: i/o timeout")
        self.assertTrue(self.tunnel._last_error.startswith("failed to request quick Tunnel"))
        self.assertEqual(self.tunnel.url, "")  # the service's own address is not the portal's

    def test_lost_connection_and_back(self):
        self.feed(TEMPORARY_OUTPUT)
        self.feed('2026-10-18T18:10:00Z ERR Connection terminated error="timeout: no recent network activity" connIndex=0')
        self.assertEqual(self.tunnel.state["state"], "connecting")
        self.feed("2026-10-18T18:10:05Z INF Registered tunnel connection connIndex=0 event=0 location=sin02 protocol=quic")
        self.assertEqual((self.tunnel.state["state"], self.tunnel.state["location"]), ("connected", "sin02"))

    def test_request_errors_while_the_portal_restarts_are_normal(self):
        self.feed(TEMPORARY_OUTPUT)
        self.feed('2026-10-18T18:11:00Z ERR  error="Unable to reach the origin service. The service may be down: '
                  'dial tcp 127.0.0.1:8000: connect: connection refused" connIndex=0 event=1 '
                  'originService=http://127.0.0.1:8000')
        self.assertEqual((self.tunnel.state["state"], self.tunnel.state["message"]), ("connected", ""))

    def test_visitors_sent_to_the_wrong_port(self):
        self.feed('2026-10-18T18:11:00Z ERR  error="Unable to reach the origin service: dial tcp 127.0.0.1:8080: '
                  'connect: connection refused" connIndex=0 originService=http://localhost:8080', mode="token")
        self.assertIn("Cloudflare sends visitors to http://localhost:8080", self.tunnel.state["message"])

    def test_dashboard_settings_are_checked(self):
        self.tunnel.public_url = ""
        self.feed(dashboard_line("http://localhost:8000"), mode="token")
        self.assertEqual(self.tunnel.state["hostnames"], ["portal.example.com"])
        self.assertIn("Enter https://portal.example.com as the public address", self.tunnel.state["message"])
        self.tunnel.public_url = "https://portal.example.com"
        self.tunnel._check_dashboard(8000, self.stop)
        self.assertEqual(self.tunnel.state["message"], "")
        self.feed(dashboard_line("https://localhost:8000"), mode="token")
        self.assertIn("Change its service to HTTP, URL 127.0.0.1:8000", self.tunnel.state["message"])
        self.tunnel.public_url = "https://other.example.com"
        self.feed(dashboard_line("http://127.0.0.1:8000"), mode="token")
        self.assertIn("but the public address of the portal is https://other.example.com",
                      self.tunnel.state["message"])


class FindTests(TunnelTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch("shutil.which", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.program_dir = self.home / "program"
        self.program_dir.mkdir()

    def find(self):
        with mock.patch.dict(os.environ, {tunnel.OVERRIDE_ENV: ""}):
            return tunnel.find_cloudflared(self.home, self.program_dir)

    def test_order(self):
        if Path("/opt/homebrew/bin/cloudflared").exists() or Path("/usr/local/bin/cloudflared").exists():
            self.skipTest("cloudflared is installed on this computer")
        self.assertIsNone(self.find())
        downloaded = tunnel.downloaded_program(self.home)
        downloaded.parent.mkdir(exist_ok=True)
        downloaded.write_bytes(b"x")
        self.assertEqual(self.find(), downloaded)
        beside = self.program_dir / tunnel.program_name()
        beside.write_bytes(b"x")
        self.assertEqual(self.find(), beside)

    def test_named_in_the_environment(self):
        with mock.patch.dict(os.environ, {tunnel.OVERRIDE_ENV: "/opt/tools/cloudflared"}):
            self.assertEqual(tunnel.find_cloudflared(self.home, self.program_dir), Path("/opt/tools/cloudflared"))


class FakeResponse(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


class FakeOpener:
    def __init__(self, data):
        self.data = data
        self.urls = []

    def open(self, url, timeout):
        self.urls.append(url)
        return FakeResponse(self.data)


class DownloadTests(TunnelTestCase):
    def download(self, asset, data, digest=None):
        key = ("testos", "testcpu")
        downloads = {key: (asset, digest or hashlib.sha256(data).hexdigest())}
        opener = FakeOpener(data)
        with mock.patch.object(tunnel, "platform_key", return_value=key), \
                mock.patch.dict(tunnel.DOWNLOADS, downloads, clear=True):
            path = tunnel.download(self.home, report=self.reports.append, opener=opener)
        return path, opener

    def test_checked_and_kept(self):
        path, opener = self.download("cloudflared-linux-amd64", b"program" * 1000)
        self.assertEqual(path, tunnel.downloaded_program(self.home))
        self.assertEqual(path.read_bytes(), b"program" * 1000)
        self.assertEqual(opener.urls, [f"https://github.com/cloudflare/cloudflared/releases/download/"
                                       f"{tunnel.VERSION}/cloudflared-linux-amd64"])
        self.assertIn("100%", self.reports[-1])
        if os.name != "nt":
            self.assertTrue(os.access(path, os.X_OK))

    def test_wrong_checksum(self):
        with self.assertRaisesMessage(tunnel.TunnelError, "wrong checksum"):
            self.download("cloudflared-linux-amd64", b"tampered", digest="0" * 64)
        self.assertEqual(list((self.home / "tools").iterdir()), [])

    def test_macos_archive_is_unpacked(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
            member = tarfile.TarInfo("cloudflared")
            member.size = len(b"mac program")
            bundle.addfile(member, io.BytesIO(b"mac program"))
        path, _opener = self.download("cloudflared-darwin-arm64.tgz", archive.getvalue())
        self.assertEqual(path.read_bytes(), b"mac program")

    def test_pinned_downloads_cover_the_desktop_builds(self):
        for key in (("windows", "amd64"), ("linux", "amd64"), ("darwin", "arm64"), ("darwin", "amd64")):
            asset, digest = tunnel.DOWNLOADS[key]
            self.assertTrue(asset.startswith("cloudflared-"))
            self.assertRegex(digest, r"^[0-9a-f]{64}$")


FAKE_CLOUDFLARED = textwrap.dedent('''\
    import json, os, sys, time
    from pathlib import Path
    here = Path(os.environ["FAKE_DIR"])
    run = len(list(here.glob("run-*.json"))) + 1
    (here / f"run-{run}.json").write_text(json.dumps({
        "args": sys.argv[1:], "pid": os.getpid(),
        "token": os.environ.get("TUNNEL_TOKEN"), "loglevel": os.environ.get("TUNNEL_LOGLEVEL")}))
    if "--url" in sys.argv:
        print(f"2026-10-18T17:53:09Z INF |  https://fake-run-{run}.trycloudflare.com  |", file=sys.stderr, flush=True)
    print("2026-10-18T17:53:10Z INF Registered tunnel connection connIndex=0 location=khi01", file=sys.stderr,
          flush=True)
    if os.environ.get("FAKE_CRASH") and run == 1:
        print("2026-10-18T17:53:11Z ERR fake crash", file=sys.stderr, flush=True)
        sys.exit(1)
    time.sleep(120)
''')


@unittest.skipIf(os.name == "nt", "the stand-in for cloudflared is a script")
class ProcessTests(TunnelTestCase):
    def setUp(self):
        super().setUp()
        self.fake_dir = self.home / "fake"
        self.fake_dir.mkdir()
        fake = self.fake_dir / "cloudflared"
        fake.write_text(f"#!{sys.executable}\n{FAKE_CLOUDFLARED}", encoding="utf-8")
        fake.chmod(0o755)
        environment = mock.patch.dict(os.environ, {tunnel.OVERRIDE_ENV: str(fake), "FAKE_DIR": str(self.fake_dir),
                                                   "TUNNEL_LOGLEVEL": "debug"})
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(self.tunnel.stop)

    def values(self, **changes):
        return {**desktop.DEFAULTS, "tunnel": "temporary", **changes}

    def runs(self):
        return [json.loads(path.read_text()) for path in sorted(self.fake_dir.glob("run-*.json"))]

    def wait_for(self, condition, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.05)
        self.fail(f"timed out; reports: {self.reports}")

    def test_temporary_address(self):
        self.tunnel.apply(self.values(port=8123))
        self.assertTrue(self.tunnel.wait_ready(20))
        self.wait_for(lambda: self.tunnel.state.get("state") == "connected")
        self.assertEqual(self.tunnel.url, "https://fake-run-1.trycloudflare.com")
        run = self.runs()[0]
        self.assertEqual(run["args"], ["--config", str(self.home / "tools" / "cloudflared.yml"),
                                       "tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:8123"])
        self.assertIsNone(run["token"])
        self.assertIsNone(run["loglevel"])  # TUNNEL_* settings of this computer are not passed on
        self.assertIn("fake-run-1", (self.home / "logs" / "tunnel.log").read_text(encoding="utf-8"))
        self.tunnel.apply(self.values(port=8123, require_two_factor=True))  # nothing for the tunnel changed
        self.assertEqual(len(self.runs()), 1)
        self.tunnel.stop()
        self.assertFalse((self.home / desktop.TUNNEL_STATE_FILE).exists())
        self.wait_for(lambda: not _alive(run["pid"]))

    def test_started_again_with_a_new_address(self):
        with mock.patch.dict(os.environ, {"FAKE_CRASH": "1"}), mock.patch.object(tunnel, "RETRY_SECONDS", (0.1,)):
            self.tunnel.apply(self.values())
            self.wait_for(lambda: self.tunnel.url == "https://fake-run-2.trycloudflare.com")
        self.assertTrue(any("fake crash" in report for report in self.reports))

    def test_own_address_gets_the_token_through_the_environment(self):
        secret = token()
        self.tunnel.apply(self.values(tunnel="token", tunnel_token=secret, public_url="https://portal.example.com"))
        self.wait_for(lambda: self.tunnel.state.get("state") == "connected")
        run = self.runs()[0]
        self.assertEqual(run["token"], secret)
        self.assertEqual(run["args"][-1], "run")
        self.assertNotIn(secret, " ".join(run["args"]))
        self.assertEqual(self.tunnel.url, "")

    def test_own_address_without_a_token(self):
        self.tunnel.apply(self.values(tunnel="token"))
        self.assertTrue(self.tunnel.wait_ready(5))
        self.assertEqual(self.tunnel.state["state"], "failed")
        self.assertEqual(self.runs(), [])

    def test_turned_off(self):
        self.tunnel.apply(self.values())
        self.wait_for(lambda: self.runs())
        pid = self.runs()[0]["pid"]
        self.tunnel.apply(self.values(tunnel="off"))
        self.assertEqual(self.tunnel.mode, "off")
        self.wait_for(lambda: not _alive(pid))


def _alive(pid):
    try:
        finished, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        finished = pid  # not our child: reaped already
    if finished == pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
