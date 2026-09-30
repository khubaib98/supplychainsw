"""
End-to-end check of the desktop edition, run by CI on every build:

    python launcher/smoke_test.py dist/CNPK-Portal/CNPK-Portal          (the built program)
    python launcher/smoke_test.py python launcher/portal_launcher.py    (from source)

With a temporary data folder it: completes the first-run set-up like a
browser would, adds a person who signs in and chooses a password, checks
static files, translations, time zones and HTTPS-tunnel headers, changes a
setting (the portal restarts itself), makes a backup while the portal runs,
stops it, restores the backup, starts again, and finally runs the demo.
Only the standard library is used.
"""
import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

PORT = 8765
DEMO_PORT = 8766
PASSWORD = "Harbour-Lantern-2026"
DIRECT = urllib.request.ProxyHandler({})  # never send local requests through a proxy


class SmokeTestFailure(Exception):
    pass


def check(condition, message):
    if not condition:
        raise SmokeTestFailure(message)


def health(port):
    try:
        with urllib.request.build_opener(DIRECT).open(f"http://127.0.0.1:{port}/healthz/", timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Browser:
    """Just enough of a browser: cookies, CSRF tokens, no automatic redirects."""

    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"
        self.opener = urllib.request.build_opener(
            DIRECT, urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), NoRedirect()
        )
        self.page = ""

    def request(self, path, data=None, headers=None):
        body = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
        request = urllib.request.Request(self.base + path, data=body, headers=headers or {})
        try:
            with self.opener.open(request, timeout=60) as response:
                return response.status, response.headers.get("Location"), response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            return error.code, error.headers.get("Location"), error.read().decode("utf-8", "replace")

    def get(self, path, expect=200):
        status, location, text = self.request(path)
        check(status == expect, f"GET {path}: expected {expect}, got {status} {location or ''}\n{text[:1500]}")
        self.page = text
        return location if status in (301, 302) else text

    def post(self, path, fields, expect=302, headers=None):
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', self.page)
        check(token, f"no form on the page before POST {path}")
        status, location, text = self.request(path, {"csrfmiddlewaretoken": token.group(1), **fields}, headers)
        check(status == expect, f"POST {path}: expected {expect}, got {status} {location or ''}\n{text[:3000]}")
        self.page = text
        return location if status in (301, 302) else text

    def sign_in(self, username, password, headers=None):
        self.get("/login/")
        return self.post("/login/", {"username": username, "password": password}, headers=headers)


class Portal:
    def __init__(self, command, home, port, demo=False):
        self.command = list(command)
        self.home = Path(home)
        self.port = port
        self.options = ["--data-dir", str(self.home), *(["--demo"] if demo else [])]
        self.log_path = self.home.parent / f"{self.home.name}-launcher.log"
        self.process = None

    def run(self, *args, expect=0):
        result = subprocess.run(
            [*self.command, *self.options, *args], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=300,
        )
        check(result.returncode == expect,
              f"{' '.join(args)}: exit code {result.returncode}\n{result.stdout}\n{result.stderr}")
        return result.stdout

    def start(self):
        log = open(self.log_path, "a", encoding="utf-8")  # noqa: SIM115 - kept open for the child
        extra = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
        self.process = subprocess.Popen(
            [*self.command, *self.options, "--port", str(self.port), "--no-browser"],
            stdout=log, stderr=subprocess.STDOUT, **extra,
        )
        log.close()
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            check(self.process.poll() is None, "the portal exited while starting")
            if health(self.port):
                return
            time.sleep(1)
        raise SmokeTestFailure("the portal did not start within 5 minutes")

    def stop(self):
        self.run("stop")
        self.process.wait(60)
        check(health(self.port) is None, "the portal still answers after stop")

    def launcher_output(self):
        return self.log_path.read_text(encoding="utf-8", errors="replace") if self.log_path.exists() else ""

    def diagnostics(self):
        portal_log = self.home / "logs" / "portal.log"
        text = portal_log.read_text(encoding="utf-8", errors="replace")[-6000:] if portal_log.exists() else ""
        return f"--- launcher output ({self.home.name}) ---\n{self.launcher_output()}\n--- portal.log ---\n{text}"


def step(title):
    print(f"* {title}", flush=True)


def real_portal(command, work):
    portal = Portal(command, work / "data", PORT)
    step("first start")
    portal.start()
    try:
        exercise(portal)
    except BaseException:
        if portal.process and portal.process.poll() is None:
            subprocess.run([*portal.command, *portal.options, "stop"], capture_output=True, timeout=120)
        raise
    return portal


def exercise(portal):
    token = re.search(r"/setup/\?token=([A-Za-z0-9_-]+)", portal.launcher_output())
    check(token, "the launcher did not print a set-up link")

    step("first-run set-up")
    admin = Browser(PORT)
    admin.get("/setup/", expect=403)
    check(admin.get(f"/setup/?token={token.group(1)}", expect=302) == "/setup/", "set-up link not accepted")
    admin.get("/setup/")
    location = admin.post("/setup/", {
        "name": "Smoke Test", "username": "admin", "email": "", "password1": PASSWORD, "password2": PASSWORD,
        "shipper_name": "Shenzhen Test Shipper", "shipper_name_local": "深圳测试发货", "receiver_name": "Rawalpindi Test Receiver",
        "receiver_name_local": "", "oversight_name": "Top management",
    })
    check(location == "/people/", f"set-up ended at {location}")
    people = admin.get("/people/")
    check("Rawalpindi Test Receiver" in people and f"http://127.0.0.1:{PORT}" in people, "People page incomplete")

    step("static files (portal, admin, Urdu font)")
    for path in ("/static/css/portal.css", "/static/js/portal.js", "/static/admin/css/base.css",
                 "/static/vendor/fonts/noto-nastaliq-urdu-arabic-400-normal.woff2"):
        status, _location, _text = admin.request(path)
        check(status == 200, f"{path}: {status}")
    admin.get("/admin/")

    step("adding a person")
    admin.get("/people/new/")
    receiver = re.search(r'<option value="(\d+)">Rawalpindi Test Receiver', admin.page)
    check(receiver, "Receiver organisation missing from the form")
    location = admin.post("/people/new/", {
        "first_name": "Ahmed", "last_name": "Raza", "username": "ahmed.raza", "email": "",
        "organization": receiver.group(1), "level": "manager", "language": "ur", "timezone": "",
    })
    welcome = admin.get(location)
    temporary = re.search(r'id="temp-password">([^<]+)<', welcome)
    check(temporary, "no temporary password shown")
    check("عارضی پاس ورڈ" in welcome, "the ready-to-send message is not in Urdu")

    step("first sign-in with the temporary password (Urdu, Pakistan time)")
    person = Browser(PORT)
    check(person.sign_in("ahmed.raza", temporary.group(1)) == "/", "sign-in failed")
    check(person.get("/", expect=302) == "/account/password/", "no forced password change")
    person.get("/account/password/")
    check(person.post("/account/password/", {"new_password1": PASSWORD, "new_password2": PASSWORD}) == "/",
          "password change failed")
    dashboard = person.get("/")
    check("ڈیش بورڈ" in dashboard, "the dashboard is not in Urdu")
    check("PKT" in dashboard, "Pakistan time zone missing (time zone data not bundled?)")

    step("sign-in through an HTTPS tunnel (X-Forwarded-Proto from this computer)")
    tunnel = Browser(PORT)
    headers = {"X-Forwarded-Proto": "https", "Origin": f"https://127.0.0.1:{PORT}"}
    check(tunnel.sign_in("ahmed.raza", PASSWORD, headers=headers) == "/", "sign-in through the tunnel failed")

    step("changing a setting restarts the portal")
    before = health(PORT)["boot"]
    admin.get("/people/")
    admin.post("/people/computer/", {"network_access": "on", "public_url": ""}, expect=200)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        answer = health(PORT)
        if answer and answer["boot"] != before:
            break
        time.sleep(1)
    else:
        raise SmokeTestFailure("the portal did not come back after the settings change")
    check("network_access = yes" in (portal.home / "settings.ini").read_text(encoding="utf-8"), "setting not saved")
    admin.get("/people/")

    step("backup while the portal runs")
    saved = re.search(r"Backup saved: (.+\.zip)", portal.run("backup"))
    check(saved, "the backup command did not report a file")
    archive = Path(saved.group(1).strip())
    with zipfile.ZipFile(archive) as bundle:
        check("db.sqlite3" in bundle.namelist(), "the backup has no database")

    step("a change after the backup, then stop")
    admin.get("/people/new/")
    admin.post("/people/new/", {
        "first_name": "Later", "last_name": "", "username": "later.person", "email": "",
        "organization": receiver.group(1), "level": "viewer", "language": "", "timezone": "",
    })
    portal.stop()

    step("restore the backup and start again")
    portal.run("restore", str(archive))
    portal.start()
    check(Browser(PORT).sign_in("ahmed.raza", PASSWORD) == "/", "restored account cannot sign in")
    again = Browser(PORT)
    check(again.sign_in("admin", PASSWORD) == "/", "administrator cannot sign in after restore")
    listing = again.get("/people/")
    check("ahmed.raza" in listing and "later.person" not in listing, "the restore did not bring back the backup")
    portal.stop()


def demo_portal(command, work):
    step("demo")
    demo = Portal(command, work / "demo", DEMO_PORT, demo=True)
    demo.start()
    try:
        browser = Browser(DEMO_PORT)
        check(browser.sign_in("shipper.manager", "Demo-Portal-2026") == "/", "demo sign-in failed")
        found = re.search(r'href="/consignments/(CSG-\d{4}-\d{4})/"', browser.get("/consignments/?q=PO-2026-0142"))
        check(found, "flagship demo consignment missing")
        packing = browser.get(f"/consignments/{found.group(1)}/packing/")
        check("DCJM" in packing and "装箱" in packing, "demo packing page incomplete")
        check("CST" in browser.page, "China time zone missing")
        item = browser.get(f"/i/{found.group(1)}-DCJM-017/")
        check("双通道干扰模块" in item, "item page not in Chinese")
    finally:
        demo.stop()
    return demo


def main(command):
    work = Path(tempfile.mkdtemp(prefix="cnpk-smoke-"))
    portals = []
    try:
        portals.append(Portal(command, work / "data", PORT))
        real_portal(command, work)
        portals.append(Portal(command, work / "demo", DEMO_PORT, demo=True))
        demo_portal(command, work)
    except (SmokeTestFailure, subprocess.TimeoutExpired) as exc:
        print(f"\nSMOKE TEST FAILED: {exc}\n", flush=True)
        for portal in portals:
            print(portal.diagnostics(), flush=True)
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("\nAll smoke tests passed.", flush=True)
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1:]))
