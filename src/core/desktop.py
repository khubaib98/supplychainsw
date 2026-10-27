"""
The desktop edition: the whole portal on one ordinary computer.

The launcher (launcher/portal_launcher.py, shipped as the CNPK-Portal program)
keeps everything in one data folder, by default "CNPK-Portal" in the user's
home folder:

    settings.ini   port, network access, tunnel, e-mail, translation
    db.sqlite3     the database
    media/         uploaded documents
    backups/       one zip a day: database snapshot + documents
    outbox/        e-mails, saved here while no mail server is configured
    logs/          portal.log, tunnel.log
    tools/         cloudflared, if the portal had to download it (see core/tunnel.py)

This module holds what the launcher and the web interface (People page) share.
It imports nothing from Django, so the launcher can use it before Django starts.
"""
import base64
import configparser
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
import zipfile
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

HOME_ENV = "CNPK_PORTAL_HOME"
FOLDER_NAME = "CNPK-Portal"
SETTINGS_FILE = "settings.ini"
DATABASE_FILE = "db.sqlite3"
DEFAULT_PORT = 8000
DEMO_PORT = 8001
# Exit code the portal process uses to ask the launcher for a restart (new settings).
RESTART_EXIT_CODE = 75
BACKUP_PREFIX = "portal-backup-"
BACKUP_KEEP_AT_LEAST = 7
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
TUNNEL_MODES = ("off", "temporary", "token")
TEMPORARY_HOSTS = ".trycloudflare.com"  # where temporary tunnel addresses live
# Cloudflare tunnel tokens are base64 JSON, so they start with "eyJ" ('{"').
TUNNEL_TOKEN = re.compile(r"eyJ[A-Za-z0-9+/=]{40,}")


class SettingsError(Exception):
    """settings.ini contains a value the portal cannot use."""


class BackupError(Exception):
    pass


def default_home(demo=False):
    base = os.environ.get(HOME_ENV)
    home = Path(base).expanduser() if base else Path.home() / FOLDER_NAME
    return home.with_name(home.name + "-Demo") if demo else home


def prepare_home(home):
    home = Path(home)
    for folder in (home, home / "media", home / "backups", home / "logs"):
        folder.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        home.chmod(0o700)  # documents and the database are for this user only
    return home


def _retry(operation, *args, attempts=20, delay=0.5):
    """Windows keeps files locked for a moment after a program exits (or while an antivirus scans them)."""
    for attempt in range(attempts):
        try:
            return operation(*args)
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def _write_private(path, text):
    """Write atomically; readable by the owner only (the file may hold passwords)."""
    path = Path(path)
    partial = path.with_name(path.name + ".partial")
    partial.write_text(text, encoding="utf-8")
    if os.name != "nt":
        partial.chmod(0o600)
    _retry(os.replace, partial, path)


# --- settings.ini --------------------------------------------------------------

DEFAULTS = {
    "port": DEFAULT_PORT,
    "network_access": False,
    "public_url": "",
    "require_two_factor": False,
    "tunnel": "off",
    "tunnel_token": "",
    "keep_days": 30,
    "copy_to": "",
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_password": "",
    "smtp_tls": True,
    "from_address": "",
    "provider": "none",
    "anthropic_api_key": "",
    "libretranslate_url": "",
    "advanced": {},
}

SECTIONS = {
    "portal": ("port", "network_access", "public_url", "require_two_factor"),
    "tunnel": ("tunnel", "tunnel_token"),
    "backup": ("keep_days", "copy_to"),
    "email": ("smtp_host", "smtp_port", "smtp_user", "smtp_password", "smtp_tls", "from_address"),
    "translation": ("provider", "anthropic_api_key", "libretranslate_url"),
}

TEMPLATE = """\
# CN-PK Consignment Portal - settings for this computer
#
# Change a value, save this file, then restart the portal (close its window
# and start it again). Administrators can change network access, the tunnel,
# the public address and two-factor sign-in on the People page instead.

[portal]
# Port number of the portal. It is part of the address, e.g. http://192.168.1.20:{port}
port = {port}

# no  = only this computer can open the portal (safest; also right when the
#       portal is published through Cloudflare Tunnel)
# yes = other computers in the same office network or VPN can open it too
network_access = {network_access}

# Your own HTTPS address for people outside, e.g. https://portal.example.com,
# if the portal is published through your own Cloudflare tunnel (below) or
# another tunnel. Not needed for a temporary tunnel address.
public_url = {public_url}

# yes = everyone must use an authenticator app when signing in
# (recommended as soon as the portal can be reached from the internet)
require_two_factor = {require_two_factor}

[tunnel]
# Share the portal over the internet through Cloudflare Tunnel. The portal
# then runs Cloudflare's cloudflared program itself (and downloads it once
# into the "tools" folder if it is not installed on this computer).
# off       = no tunnel
# temporary = a random address such as https://....trycloudflare.com; no
#             Cloudflare account needed, but the address changes every time
#             the portal starts (for trying it out)
# token     = your own address: create a tunnel in the Cloudflare dashboard,
#             paste its token below and enter the address as public_url above
tunnel = {tunnel}
tunnel_token = {tunnel_token}

[backup]
# Every day the database and all uploaded documents are saved as one zip
# file in the "backups" folder next to this file. Older backups are deleted
# after this many days (the newest {keep_at_least} are always kept).
keep_days = {keep_days}

# Optional second folder that receives a copy of every backup: a USB disk,
# a network drive or a synced cloud folder. Example: E:\\portal-backups
copy_to = {copy_to}

[email]
# Mail server for reminder e-mails. While smtp_host is empty no e-mail is
# sent; the messages are saved in the "outbox" folder instead.
smtp_host = {smtp_host}
smtp_port = {smtp_port}
smtp_user = {smtp_user}
smtp_password = {smtp_password}
smtp_tls = {smtp_tls}
from_address = {from_address}

[translation]
# Machine translation of remarks: none, anthropic (Claude API, needs an API
# key) or libretranslate (your own LibreTranslate server).
provider = {provider}
anthropic_api_key = {anthropic_api_key}
libretranslate_url = {libretranslate_url}

[advanced]
# Any other setting from deploy/.env.example, one per line, for example:
# PORTAL_REMINDER_DAYS = 0,2,5
{advanced}
"""


def _yes_no(value):
    return "yes" if value else "no"


def _parse_bool(key, text):
    value = text.strip().lower()
    if value in {"yes", "true", "on", "1"}:
        return True
    if value in {"no", "false", "off", "0", ""}:
        return False
    raise SettingsError(f"{SETTINGS_FILE}: {key} must be yes or no (found {text!r}).")


def _parse_int(key, text, low, high):
    try:
        value = int(text.strip())
    except ValueError:
        value = None
    if value is None or not low <= value <= high:
        raise SettingsError(f"{SETTINGS_FILE}: {key} must be a whole number from {low} to {high} (found {text!r}).")
    return value


def normalize_public_url(text):
    """'https://portal.example.com/' -> 'https://portal.example.com'; '' stays ''."""
    text = (text or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.path not in {"", "/"}:
        raise SettingsError(
            f"public_url must look like https://portal.example.com, without a path (found {text!r})."
        )
    return text


def is_this_computer(hostname):
    """localhost, 127.x.x.x and ::1 (never the address other people open)."""
    hostname = (hostname or "").strip("[]").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def normalize_tunnel_token(text):
    """
    The token of a tunnel made in the Cloudflare dashboard. The whole install
    command shown there may be pasted (cloudflared.exe service install eyJ...).
    """
    text = (text or "").strip().strip("'\"")
    if not text:
        return ""
    found = max(TUNNEL_TOKEN.findall(text), key=len, default="")
    try:
        content = json.loads(base64.b64decode(found, validate=True))
    except ValueError:  # not base64, or not JSON
        content = None
    if not isinstance(content, dict) or not content.get("t"):
        raise SettingsError("This is not a Cloudflare tunnel token (it starts with eyJ).")
    return found


def _validate(values):
    values["port"] = _parse_int("port", str(values["port"]), 1, 65535)
    values["keep_days"] = _parse_int("keep_days", str(values["keep_days"]), 1, 3650)
    values["smtp_port"] = _parse_int("smtp_port", str(values["smtp_port"]), 1, 65535)
    values["public_url"] = normalize_public_url(values["public_url"])
    values["provider"] = values["provider"].strip().lower() or "none"
    if values["provider"] not in {"none", "anthropic", "libretranslate"}:
        raise SettingsError(f"{SETTINGS_FILE}: provider must be none, anthropic or libretranslate.")
    values["tunnel"] = str(values["tunnel"]).strip().lower() or "off"
    if values["tunnel"] not in TUNNEL_MODES:
        raise SettingsError(f"{SETTINGS_FILE}: tunnel must be off, temporary or token (found {values['tunnel']!r}).")
    values["tunnel_token"] = str(values["tunnel_token"]).strip()
    for key in values["advanced"]:
        if not ENV_NAME.match(key):
            raise SettingsError(f"{SETTINGS_FILE}: [advanced] setting names look like PORTAL_REMINDER_DAYS (found {key!r}).")
    return values


def settings_path(home):
    return Path(home) / SETTINGS_FILE


def load_settings(home, demo=False):
    """Read settings.ini, creating it with defaults the first time."""
    path = settings_path(home)
    values = {**DEFAULTS, "advanced": {}}
    if not path.exists():
        if demo:
            values["port"] = DEMO_PORT
        save_settings(home, values)
        return values
    parser = configparser.RawConfigParser(interpolation=None)
    parser.optionxform = str  # keep the case of [advanced] names
    try:
        parser.read_string(path.read_text(encoding="utf-8-sig"))
    except (configparser.Error, UnicodeDecodeError) as exc:
        raise SettingsError(f"{path} cannot be read: {exc}") from exc
    for section, keys in SECTIONS.items():
        if not parser.has_section(section):
            continue
        present = {name.lower(): value for name, value in parser.items(section)}
        for key in keys:
            if key in present:
                default = DEFAULTS[key]
                raw = present[key]
                values[key] = _parse_bool(key, raw) if isinstance(default, bool) else raw.strip()
    if parser.has_section("advanced"):
        values["advanced"] = {name.strip(): value.strip() for name, value in parser.items("advanced")}
    return _validate(values)


def save_settings(home, values):
    values = _validate({**DEFAULTS, **values})
    rendered = {key: (_yes_no(value) if isinstance(value, bool) else value) for key, value in values.items()}
    rendered["advanced"] = "\n".join(f"{name} = {value}" for name, value in values["advanced"].items())
    _write_private(settings_path(home), TEMPLATE.format(keep_at_least=BACKUP_KEEP_AT_LEAST, **rendered))
    return values


def update_settings(home, **changes):
    unknown = set(changes) - set(DEFAULTS)
    if unknown:
        raise SettingsError(f"Unknown setting(s): {', '.join(sorted(unknown))}")
    return save_settings(home, {**load_settings(home), **changes})


def secret_key(home):
    path = Path(home) / "secret.key"
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = secrets.token_urlsafe(50)
    _write_private(path, value + "\n")
    return value


def instance_id(home):
    """Stable, non-secret identifier of this installation (lets the launcher recognise itself)."""
    return hashlib.sha256(secret_key(home).encode("utf-8")).hexdigest()[:16]


# --- addresses ---------------------------------------------------------------------

def lan_addresses():
    """IPv4 addresses other computers in the network can use to reach this one."""
    found = []

    def add(address):
        if address and address not in found and not address.startswith(("127.", "169.254.", "0.")):
            found.append(address)

    try:
        # Connecting a UDP socket sends nothing; it only picks the outgoing interface.
        with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as probe:
            probe.connect(("192.0.2.1", 9))
            add(probe.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM):
            add(info[4][0])
    except OSError:
        pass
    return found


def access_urls(values, addresses=None):
    port = values["port"]
    urls = [{"kind": "local", "url": f"http://127.0.0.1:{port}"}]
    if values["network_access"]:
        for address in lan_addresses() if addresses is None else addresses:
            urls.append({"kind": "network", "url": f"http://{address}:{port}"})
    if values["public_url"]:
        urls.append({"kind": "public", "url": values["public_url"]})
    return urls


def django_environment(home, values, demo=False, addresses=None):
    """Environment variables that configure config/settings.py for this computer."""
    home = Path(home)
    urls = access_urls(values, addresses)
    public = values["public_url"]
    hosts = ["localhost", "127.0.0.1", "[::1]"]
    if public:
        hosts.append(urlparse(public).hostname)
    if values["tunnel"] == "temporary":
        # A new random address at every start of cloudflared, also while the portal runs.
        hosts.append(TEMPORARY_HOSTS)
    base_url = public or next((u["url"] for u in urls if u["kind"] == "network"), urls[0]["url"])
    env = {
        "DJANGO_SETTINGS_MODULE": "config.settings",
        "DJANGO_DEBUG": "0",
        "DJANGO_SECRET_KEY": secret_key(home),
        # The addresses of an office network change (DHCP), and the portal never builds
        # links from the Host header, so any host name is accepted with network access on.
        "DJANGO_ALLOWED_HOSTS": "*" if values["network_access"] else ",".join(hosts),
        "DJANGO_CSRF_TRUSTED_ORIGINS": public,
        # Plain HTTP inside the office network; HTTPS is added by the tunnel, if any.
        "DJANGO_SECURE_COOKIES": "0",
        "DJANGO_COOKIE_PREFIX": "demo_" if demo else "",
        "PORTAL_DESKTOP_HOME": str(home),
        "PORTAL_DATA_DIR": str(home),
        "PORTAL_BASE_URL": base_url,
        "PORTAL_ACCESS_URLS": json.dumps(urls),
        "PORTAL_INSTANCE": instance_id(home),
        "PORTAL_REQUIRE_2FA": "1" if values["require_two_factor"] else "0",
        "PORTAL_TUNNEL": values["tunnel"],
        "MT_PROVIDER": values["provider"],
    }
    if demo:
        env["PORTAL_NAME"] = "CN–PK Consignment Portal (DEMO)"
    if values["smtp_host"]:
        env.update({
            "DJANGO_EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
            "SMTP_HOST": values["smtp_host"],
            "SMTP_PORT": str(values["smtp_port"]),
            "SMTP_USER": values["smtp_user"],
            "SMTP_PASSWORD": values["smtp_password"],
            "SMTP_TLS": "1" if values["smtp_tls"] else "0",
        })
        if values["from_address"]:
            env["PORTAL_FROM_EMAIL"] = values["from_address"]
    else:
        env.update({
            "DJANGO_EMAIL_BACKEND": "django.core.mail.backends.filebased.EmailBackend",
            "DJANGO_EMAIL_FILE_PATH": str(home / "outbox"),
        })
    if values["anthropic_api_key"]:
        env["ANTHROPIC_API_KEY"] = values["anthropic_api_key"]
    if values["libretranslate_url"]:
        env["LIBRETRANSLATE_URL"] = values["libretranslate_url"]
    env.update(values["advanced"])
    return env


SUPERVISED_ENV = "PORTAL_SUPERVISED"


def request_restart(delay=1.0):
    """
    Ask the launcher to restart the portal once the current response has been
    sent. Only possible when the launcher started this process (it then starts
    a fresh one); returns False otherwise.
    """
    if os.environ.get(SUPERVISED_ENV) != "1":
        return False
    timer = threading.Timer(delay, os._exit, args=(RESTART_EXIT_CODE,))
    timer.daemon = True
    timer.start()
    return True


# --- tunnel state -----------------------------------------------------------------
# The launcher runs cloudflared (core/tunnel.py) and reports how it is doing in
# tunnel.json; the People page reads it. The session ties the file to the launcher
# that started this portal process, so a file left behind by a crash is ignored.

TUNNEL_STATE_FILE = "tunnel.json"
TUNNEL_SESSION_ENV = "PORTAL_TUNNEL_SESSION"


def write_tunnel_state(home, state):
    _write_private(Path(home) / TUNNEL_STATE_FILE, json.dumps(state))


def read_tunnel_state(home, session):
    """{"state": "starting" | "downloading" | "connecting" | "connected" | "failed", "url": ..., "message": ...}"""
    try:
        state = json.loads((Path(home) / TUNNEL_STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not session or not isinstance(state, dict) or state.get("session") != session:
        return {}
    return state


# --- backups -------------------------------------------------------------------------

class BackupResult(NamedTuple):
    path: Path
    copy_error: str = ""


def backup_folder(home):
    return Path(home) / "backups"


def list_backups(folder):
    folder = Path(folder)
    if not folder.is_dir():
        return []
    return sorted(path for path in folder.glob(BACKUP_PREFIX + "*.zip") if path.is_file())


def backup_time(path):
    """portal-backup-20260929-021500[-reason].zip -> datetime(2026, 9, 29, 2, 15)."""
    stamp = Path(path).name[len(BACKUP_PREFIX):len(BACKUP_PREFIX) + 15]
    try:
        return datetime.strptime(stamp, "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def latest_backup(home):
    dated = [(backup_time(path), path) for path in list_backups(backup_folder(home))]
    dated = [pair for pair in dated if pair[0] is not None]
    return max(dated) if dated else None


def prune_backups(folder, keep_days, now=None):
    now = now or datetime.now()
    dated = sorted(
        ((backup_time(path), path) for path in list_backups(folder) if backup_time(path)), reverse=True
    )
    removed = []
    for when, path in dated[BACKUP_KEEP_AT_LEAST:]:
        if now - when > timedelta(days=keep_days):
            path.unlink(missing_ok=True)
            removed.append(path)
    return removed


def _add_media(archive, media):
    if media.is_dir():
        for path in sorted(media.rglob("*")):
            if path.is_file():
                archive.write(path, "media/" + path.relative_to(media).as_posix())


def make_backup(home, reason="", keep_days=None, copy_to="", now=None):
    """
    Zip a consistent snapshot of the database (SQLite backup API, safe while
    people are working) and all uploaded documents into backups/.
    """
    home = Path(home)
    now = now or datetime.now()
    folder = backup_folder(home)
    folder.mkdir(parents=True, exist_ok=True)
    for leftover in folder.glob("*.partial"):
        leftover.unlink(missing_ok=True)
    suffix = f"-{reason}" if reason else ""
    target = folder / f"{BACKUP_PREFIX}{now:%Y%m%d-%H%M%S}{suffix}.zip"
    partial = target.with_name(target.name + ".partial")
    with tempfile.TemporaryDirectory(dir=home, prefix="backup-") as work:
        snapshot = Path(work) / DATABASE_FILE
        database = home / DATABASE_FILE
        if database.exists():
            with closing(sqlite3.connect(database, timeout=60)) as source, closing(sqlite3.connect(snapshot)) as copy:
                source.backup(copy)
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            if snapshot.exists():
                archive.write(snapshot, DATABASE_FILE)
            _add_media(archive, home / "media")
            archive.writestr("backup-info.json", json.dumps({"created": now.isoformat(timespec="seconds"), "reason": reason}))
    _retry(os.replace, partial, target)
    if keep_days:
        prune_backups(folder, keep_days, now)
    copy_error = ""
    if copy_to:
        try:
            destination = Path(copy_to).expanduser()
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, destination / target.name)
            if keep_days:
                prune_backups(destination, keep_days, now)
        except OSError as exc:
            copy_error = f"Could not copy the backup to {copy_to}: {exc}"
            logger.warning(copy_error)
    return BackupResult(target, copy_error)


def backup_if_due(home, values, now=None, every=timedelta(hours=23)):
    """The daily backup: made when the newest backup is older than a day."""
    now = now or datetime.now()
    if not (Path(home) / DATABASE_FILE).exists():
        return None
    latest = latest_backup(home)
    if latest and now - latest[0] < every:
        return None
    return make_backup(home, keep_days=values["keep_days"], copy_to=values["copy_to"], now=now)


def _safe_member(name):
    parts = Path(name.replace("\\", "/")).parts
    return (
        bool(parts)
        and not name.startswith(("/", "\\"))
        and ":" not in parts[0]
        and ".." not in parts
        and parts[0] in {DATABASE_FILE, "media", "backup-info.json"}
    )


def restore_backup(home, archive, now=None):
    """
    Put a backup back in place. The portal must not be running. The current
    database and documents are moved to a "before-restore-…" folder, not deleted.
    """
    home = Path(home)
    archive = Path(archive)
    now = now or datetime.now()
    if not archive.is_file() or not zipfile.is_zipfile(archive):
        raise BackupError(f"{archive} is not a portal backup (zip file).")
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if DATABASE_FILE not in names:
            raise BackupError(f"{archive.name} does not contain a database ({DATABASE_FILE}).")
        unsafe = [name for name in names if not _safe_member(name)]
        if unsafe:
            raise BackupError(f"{archive.name} contains unexpected files: {', '.join(unsafe[:3])}")
        with tempfile.TemporaryDirectory(dir=home, prefix="restore-") as work:
            work = Path(work)
            bundle.extractall(work)
            with closing(sqlite3.connect(work / DATABASE_FILE)) as check:
                result = check.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise BackupError(f"The database in {archive.name} is damaged: {result}")
            kept = home / f"before-restore-{now:%Y%m%d-%H%M%S}"
            kept.mkdir()
            for name in (DATABASE_FILE, DATABASE_FILE + "-wal", DATABASE_FILE + "-shm", "media"):
                if (home / name).exists():
                    _retry(shutil.move, str(home / name), str(kept / name))
            _retry(shutil.move, str(work / DATABASE_FILE), str(home / DATABASE_FILE))
            if (work / "media").is_dir():
                _retry(shutil.move, str(work / "media"), str(home / "media"))
            else:
                (home / "media").mkdir(exist_ok=True)
    return kept
