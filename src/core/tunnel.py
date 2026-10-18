"""
Cloudflare Tunnel for the desktop edition.

With `tunnel = temporary` or `tunnel = token` in settings.ini the launcher runs
Cloudflare's cloudflared program next to the portal. cloudflared connects out
to Cloudflare, which serves the portal over HTTPS to people anywhere: nothing
changes on the router, and the address of this computer stays private.

  temporary  cloudflared gets a random address from trycloudflare.com
             (https://....trycloudflare.com). No Cloudflare account is needed,
             but the address changes every time cloudflared starts and
             Cloudflare gives no uptime guarantee: for trying it out.
  token      a tunnel made in the Cloudflare dashboard, with your own address.
             cloudflared gets the token through its environment, never on
             its command line.

The cloudflared that runs: the one named by CNPK_CLOUDFLARED, else one next to
the portal program, else an installed one, else the release pinned below,
downloaded once into the data folder's tools/ and checked against its SHA-256.

All output of cloudflared goes to logs/tunnel.log; the gist (the address,
whether it is connected, the last problem) goes to tunnel.json, which the
People page reads. This module imports nothing from Django.
"""
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from core import desktop

VERSION = "2026.10.1"
DOWNLOAD_URL = "https://github.com/cloudflare/cloudflared/releases/download/{version}/{asset}"
# (system, processor) -> (release asset, SHA-256 of the asset)
DOWNLOADS = {
    ("windows", "amd64"): ("cloudflared-windows-amd64.exe",
                           "4d2490e74082c5d48dbfedaf92576fa5bd1702379ab88eb7faa0cfbe8dc891b6"),
    ("linux", "amd64"): ("cloudflared-linux-amd64", "3bfc539f45d2620c71debb7a86be0efa737ed5ccbad9eb85c787116900e5c586"),
    ("linux", "arm64"): ("cloudflared-linux-arm64", "2c65dfabf616c40efc7545d107d7a57ad5e21377e2964b7e88af19a238074a46"),
    ("darwin", "amd64"): ("cloudflared-darwin-amd64.tgz",
                          "3b0dbaf378337e9d7fb6cd235a76d1d6a7dee434c2d8edf9281164fb6a3837ab"),
    ("darwin", "arm64"): ("cloudflared-darwin-arm64.tgz",
                          "647615df99d49cd179ea5e13ef760b9bbfb33630f6ae62f87e33dfadda60adcc"),
}
OVERRIDE_ENV = "CNPK_CLOUDFLARED"
# Random words such as https://brave-otter-sample-words.trycloudflare.com (not api.trycloudflare.com)
TEMPORARY_ADDRESS = re.compile(r"https://[a-z0-9]+(?:-[a-z0-9]+)+\.trycloudflare\.com")
LOG_LINE = re.compile(r"^\S+ (DBG|INF|WRN|ERR|FTL) (.*)$")
CONNECTION = re.compile(r"\bconnIndex=(\d+)")
LOCATION = re.compile(r"\blocation=(\w+)")
ORIGIN = re.compile(r"\boriginService=(\S+)")
CONFIG = re.compile(r'\bconfig=("(?:[^"\\]|\\.)*")')
# cloudflared's messages when one of its connections to Cloudflare ends
DISCONNECTED = ("Connection terminated", "Serve tunnel error", "Unable to establish connection",
                "Lost connection with the edge", "Unregistered tunnel connection")
RETRY_SECONDS = (5, 15, 60, 300)
LOG_LIMIT = 2_000_000
_download_lock = threading.Lock()


class TunnelError(Exception):
    """cloudflared cannot be found, downloaded or started."""


# --- finding and downloading cloudflared ------------------------------------------

def platform_key():
    if sys.platform == "win32":
        return "windows", "amd64"  # also runs on Windows on Arm
    system = "darwin" if sys.platform == "darwin" else "linux"
    machine = platform.machine().lower()
    return system, {"x86_64": "amd64", "aarch64": "arm64"}.get(machine, machine)


def program_name():
    return "cloudflared.exe" if os.name == "nt" else "cloudflared"


def downloaded_program(home):
    return Path(home) / "tools" / f"cloudflared-{VERSION}{'.exe' if os.name == 'nt' else ''}"


def find_cloudflared(home, program_dir):
    """The cloudflared to run, or None when it has to be downloaded first."""
    if os.environ.get(OVERRIDE_ENV):
        return Path(os.environ[OVERRIDE_ENV])
    candidates = [Path(program_dir) / program_name()]
    if shutil.which("cloudflared"):
        candidates.append(Path(shutil.which("cloudflared")))
    if os.name == "nt":
        candidates += [Path(os.environ[name]) / "cloudflared" / "cloudflared.exe"
                       for name in ("ProgramFiles(x86)", "ProgramFiles") if os.environ.get(name)]
    else:
        # A program started from the Finder or a desktop icon does not see the shell's PATH.
        candidates += [Path("/opt/homebrew/bin/cloudflared"), Path("/usr/local/bin/cloudflared")]
    candidates.append(downloaded_program(home))
    return next((path for path in candidates if path.is_file()), None)


def _opener():
    context = ssl.create_default_context()
    try:  # a frozen macOS program has no certificate store of its own
        import certifi

        context.load_verify_locations(certifi.where())
    except (ImportError, OSError):
        pass
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))


def download(home, report=print, opener=None):
    """Download the pinned cloudflared into <home>/tools, check its SHA-256 and return its path."""
    key = platform_key()
    if key not in DOWNLOADS:
        raise TunnelError(f"There is no cloudflared download for this computer ({' '.join(key)}). "
                          f"Install cloudflared, then start the portal again.")
    asset, expected = DOWNLOADS[key]
    target = downloaded_program(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(asset + ".partial")
    digest = hashlib.sha256()
    try:
        with (opener or _opener()).open(DOWNLOAD_URL.format(version=VERSION, asset=asset), timeout=60) as response, \
                open(partial, "wb") as out:
            size = int(response.headers.get("Content-Length") or 0)
            report(f"Downloading Cloudflare's cloudflared {VERSION} from GitHub "
                   f"({f'{size / 1e6:.0f} MB, ' if size else ''}only this once)...")
            done = shown = 0
            while chunk := response.read(1 << 16):
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if size and done * 4 // size > shown:
                    shown = done * 4 // size
                    report(f"  {shown * 25}%")
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise TunnelError(f"cloudflared could not be downloaded ({exc}). Check the internet connection.") from exc
    if digest.hexdigest() != expected:
        partial.unlink(missing_ok=True)
        raise TunnelError("The downloaded cloudflared is not the expected file (wrong checksum), so it was deleted.")
    if asset.endswith(".tgz"):
        _unpack(partial)
    if os.name != "nt":
        partial.chmod(0o755)
    os.replace(partial, target)
    for old in target.parent.glob("cloudflared-*"):
        if old != target and not old.name.endswith(".partial"):
            try:
                old.unlink()
            except OSError:  # still running elsewhere
                pass
    return target


def _unpack(path):
    """The macOS releases are .tgz archives with the program inside (checked before unpacking)."""
    with tarfile.open(path, "r:gz") as archive:
        member = next((m for m in archive.getmembers() if m.isfile() and Path(m.name).name == "cloudflared"), None)
        if member is None:
            raise TunnelError("The downloaded cloudflared archive does not contain the program.")
        data = archive.extractfile(member).read()
    path.write_bytes(data)


# --- running it ---------------------------------------------------------------------

class Tunnel:
    """
    cloudflared, run by the launcher. A thread starts it (after downloading it
    if needed), reads its output and starts it again if it stops.
    """

    def __init__(self, home, program_dir, report=print):
        self.home = Path(home)
        self.program_dir = Path(program_dir)
        self.report = report
        # Given to the portal process; tunnel.json counts only with this session.
        self.session = secrets.token_hex(8)
        self.wanted = None  # (mode, token, port)
        self.public_url = ""
        self.state = {}
        self.ready = threading.Event()  # set once there is an address, a connection or a problem
        self.announce = True  # print a new temporary address (the launcher's banner shows the first one)
        self._stop = threading.Event()
        self._thread = None
        self._process = None
        self._lock = threading.Lock()  # guards _process
        self._state_lock = threading.Lock()
        self._connections = set()
        self._last_error = ""
        self._rules = []  # the public hostnames set up in the Cloudflare dashboard

    @property
    def mode(self):
        return self.wanted[0] if self.wanted else "off"

    @property
    def url(self):
        return self.state.get("url", "")

    def apply(self, values):
        """Start, restart or stop cloudflared to match the settings."""
        wanted = (values["tunnel"], values["tunnel_token"], values["port"]) if values["tunnel"] != "off" else None
        public_url_changed, self.public_url = self.public_url != values["public_url"], values["public_url"]
        if wanted == self.wanted and (wanted is None or self._thread.is_alive()):
            if wanted and public_url_changed:
                self._check_dashboard(wanted[2], self._stop)
            return
        self.stop()
        if wanted is None:
            return
        self.wanted = wanted
        self._stop = threading.Event()
        self.ready.clear()
        self._update(self._stop, state="starting")
        self._thread = threading.Thread(target=self._run, args=(*wanted, self._stop), name="tunnel", daemon=True)
        self._thread.start()

    def wait_ready(self, timeout):
        return self.ready.wait(timeout)

    def stop(self):
        self._stop.set()
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
        if self._thread is not None:
            self._thread.join(15)
        self._thread = self.wanted = None
        self.state, self._rules = {}, []
        (self.home / desktop.TUNNEL_STATE_FILE).unlink(missing_ok=True)

    def _update(self, stop, **changes):
        with self._state_lock:
            if stop.is_set():
                return
            self.state = {**self.state, **changes}
            state = {**self.state, "session": self.session, "mode": self.mode,
                     "updated": datetime.now().isoformat(timespec="seconds")}
            try:
                desktop.write_tunnel_state(self.home, state)
            except OSError:
                pass  # only the People page misses out

    def _run(self, mode, token, port, stop):
        if mode == "token" and not token:
            self._update(stop, state="failed", message="There is no tunnel token yet.")
            self.ready.set()
            self.report("\nCloudflare Tunnel: there is no tunnel token yet. Paste it on the People page "
                        "(This computer), or set tunnel_token in settings.ini.")
            return
        failures = 0
        while not stop.is_set():
            started = time.monotonic()
            try:
                code = self._run_cloudflared(self._program(stop), mode, token, port, stop)
                problem = self._last_error or f"cloudflared stopped (exit code {code})."
            except (TunnelError, OSError) as exc:
                problem = str(exc)
            if stop.wait(0.5):  # Ctrl+C reaches cloudflared too; then the launcher is stopping anyway
                return
            failures = 1 if time.monotonic() - started > 600 else failures + 1
            delay = RETRY_SECONDS[min(failures, len(RETRY_SECONDS)) - 1]
            self._update(stop, state="failed", url="", message=problem)
            self.ready.set()
            self.report(f"\nCloudflare Tunnel: {problem}\n  Trying again in {delay} seconds "
                        f"(details: {self.home / 'logs' / 'tunnel.log'}).")
            stop.wait(delay)

    def _program(self, stop):
        program = find_cloudflared(self.home, self.program_dir)
        if program is None:
            with _download_lock:  # an earlier thread may still be downloading
                program = find_cloudflared(self.home, self.program_dir)
                if program is None:
                    self._update(stop, state="downloading")
                    program = download(self.home, report=self.report)
        return program

    def _run_cloudflared(self, program, mode, token, port, stop):
        tools = self.home / "tools"
        tools.mkdir(exist_ok=True)
        config = tools / "cloudflared.yml"
        if not config.exists():
            # An own (empty) configuration keeps cloudflared away from any other tunnel's settings.
            config.write_text("# Used by the CN-PK Consignment Portal.\nno-autoupdate: true\n", encoding="utf-8")
        command = [str(program), "--config", str(config), "tunnel", "--no-autoupdate"]
        env = {name: value for name, value in os.environ.items() if not name.upper().startswith("TUNNEL_")}
        if mode == "temporary":
            command += ["--url", f"http://127.0.0.1:{port}"]
        else:
            command.append("run")
            env["TUNNEL_TOKEN"] = token
        log_path = self.home / "logs" / "tunnel.log"
        if log_path.exists() and log_path.stat().st_size > LOG_LIMIT:
            os.replace(log_path, log_path.with_name("tunnel.log.1"))
        self._connections, self._last_error = set(), ""
        with self._lock:
            if stop.is_set():
                return 0
            self._process = process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env
            )
        try:
            with open(log_path, "a", encoding="utf-8") as log:
                log.write(f"--- {datetime.now():%Y-%m-%d %H:%M:%S} {program} ({mode}) ---\n")
                for raw in process.stdout:
                    line = raw.decode("utf-8", "replace").rstrip()
                    log.write(line + "\n")
                    log.flush()
                    self._read(line, mode, port, stop)
        finally:
            if process.poll() is None:  # its output can no longer be followed (e.g. the disk is full)
                process.terminate()
        return process.wait()

    def _check_dashboard(self, port, stop):
        rules = self._rules
        if not rules:
            return
        hosts = [host for host, _service in rules]
        wanted_host = urlparse(self.public_url).hostname if self.public_url else ""
        wrong = [(host, service) for host, service in rules
                 if not (service.startswith("http://") and service.rstrip("/").endswith(f":{port}"))]
        problem = ""
        if wrong:
            host, service = wrong[0]
            problem = (f"In the Cloudflare dashboard, {host} sends visitors to {service or 'nothing'}. "
                       f"Change its service to HTTP, URL 127.0.0.1:{port}.")
        elif not wanted_host:
            problem = (f"Enter https://{hosts[0]} as the public address on the People page (This computer); "
                       f"until then the portal turns visitors away.")
        elif wanted_host not in hosts:
            problem = (f"The tunnel serves {', '.join(hosts)}, but the public address of the portal is "
                       f"{self.public_url}. Make them the same.")
        self._update(stop, hostnames=hosts, message=problem)
        if problem:
            self.report(f"\nCloudflare Tunnel: {problem}")

    def _read(self, line, mode, port, stop):
        """Follow what cloudflared reports."""
        match = LOG_LINE.match(line)
        level, message = match.groups() if match else ("", line)
        text = message.strip()
        if not text:
            return
        if "Unable to reach the origin service" in text:
            # Normal while the portal restarts; a wrong port in the dashboard is worth a word.
            origin = ORIGIN.search(text)
            if origin and not origin.group(1).rstrip("/").endswith(f":{port}"):
                problem = (f"Cloudflare sends visitors to {origin.group(1)}, but the portal is at "
                           f"http://127.0.0.1:{port}. Correct the service of the public hostname in the "
                           f"Cloudflare dashboard.")
                if self.state.get("message") != problem:
                    self._update(stop, message=problem)
                    self.report(f"\nCloudflare Tunnel: {problem}")
        elif mode == "temporary" and not self.url and level == "INF" and TEMPORARY_ADDRESS.search(text):
            self._update(stop, state="connecting", url=TEMPORARY_ADDRESS.search(text).group(0), message="")
            self.ready.set()
            if self.announce:
                self.report(f"\nInternet address (temporary): {self.url}\n  Send it to the people who should "
                            f"open the portal. It changes every time the portal starts.")
        elif text.startswith(("+", "|")):
            pass  # boxes: the temporary address (above) and the connectivity pre-checks
        elif "Updated to new configuration" in text:
            self._rules = _ingress(text) or self._rules
            self._check_dashboard(port, stop)
        elif "Registered tunnel connection" in text:
            first = not self._connections
            self._connections.add(_connection(text))
            if first:
                location = LOCATION.search(text)
                self._update(stop, state="connected", message="", location=location.group(1) if location else "")
                self.ready.set()
                self.report("Cloudflare Tunnel is connected"
                            + (f" (Cloudflare data centre {location.group(1).upper()})." if location else "."))
        elif any(phrase in text for phrase in DISCONNECTED) and CONNECTION.search(text):
            self._last_error = text
            if self._connections:
                self._connections.discard(_connection(text))
                if not self._connections:
                    self._update(stop, state="connecting", message=text[:300])
                    self.report("Cloudflare Tunnel lost its connection; cloudflared keeps trying.")
        elif (level in ("ERR", "FTL") or not match) and not message.startswith(" ") \
                and not text.startswith("Please try the following"):
            self._last_error = text[:300]  # not the indented hints that follow some errors


def _ingress(text):
    """The public hostnames set up in the Cloudflare dashboard: [(hostname, service)]."""
    found = CONFIG.search(text)
    try:
        rules = json.loads(json.loads(found.group(1)))["ingress"] if found else []
    except (ValueError, KeyError, TypeError):
        return []
    if not isinstance(rules, list):
        return []
    return [(rule["hostname"], str(rule.get("service", ""))) for rule in rules
            if isinstance(rule, dict) and rule.get("hostname")]


def _connection(message):
    found = CONNECTION.search(message)
    return found.group(1) if found else "0"
