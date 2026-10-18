"""
One-click launcher for the CN-PK Consignment Portal (the desktop edition).

Double-click the CNPK-Portal program built from this file, or run
`python launcher/portal_launcher.py`. It

  1. keeps all data in one folder (CNPK-Portal in your home folder);
  2. prepares the database on the first start and upgrades it later
     (after making a backup);
  3. starts the portal and opens it in your browser;
  4. sends reminders every 15 minutes and makes a backup every day.

Colleagues connect through the office network or through Cloudflare Tunnel,
which the launcher runs itself (`--tunnel`, see core/tunnel.py and
docs/05-run-on-your-computer.md). `CNPK-Portal --help` lists every option.

There are two processes: this launcher, which shows the console window and
watches over the portal, and the portal itself (the hidden `_serve`
command). When an administrator changes settings in the browser, or the
portal stops unexpectedly, the launcher starts it again.
"""
import argparse
import json
import logging
import os
import plistlib
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from collections import deque
from contextlib import closing
from logging.handlers import RotatingFileHandler
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
if not FROZEN:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from config import VERSION  # noqa: E402
from core import desktop  # noqa: E402
from core.tunnel import Tunnel  # noqa: E402

NAME = "CN-PK Consignment Portal"
PROGRAM = "CNPK-Portal"
CONFIG_ERROR = 2
DEMO_PASSWORD = "Demo-Portal-2026"
DEMO_MARKER = "DEMO-DATA.txt"
# Where the program is; IT staff may put cloudflared next to it.
PROGRAM_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
# Local health checks must never go through a proxy configured on the computer.
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class LauncherError(Exception):
    """A problem the user can fix; shown without a traceback."""


# --- small helpers -----------------------------------------------------------------

def self_command():
    """How to start this program again (for the portal process and autostart)."""
    if FROZEN:
        return [sys.executable]
    return [sys.executable, str(Path(__file__).resolve())]


def portal_answer(port, timeout=1.5):
    """The /healthz/ answer of a portal on this port of this computer, or None."""
    try:
        with LOCAL.open(f"http://127.0.0.1:{port}/healthz/", timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


def port_in_use(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def count_users(home):
    database = Path(home) / desktop.DATABASE_FILE
    if not database.exists():
        return 0
    try:
        with closing(sqlite3.connect(database, timeout=10)) as db:
            return db.execute("SELECT COUNT(*) FROM auth_user").fetchone()[0]
    except sqlite3.Error:
        return 0


def open_browser(url):
    if sys.platform.startswith(("linux", "freebsd")) and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return  # no desktop: a text-mode browser would take over this window
    bundled = FROZEN and sys.platform.startswith("linux")
    saved = os.environ.get("LD_LIBRARY_PATH")
    if bundled:
        # The browser must not load this bundle's libraries.
        original = os.environ.get("LD_LIBRARY_PATH_ORIG")
        if original is None:
            os.environ.pop("LD_LIBRARY_PATH", None)
        else:
            os.environ["LD_LIBRARY_PATH"] = original
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 - opening the browser is a convenience only
        pass
    finally:
        if bundled:
            if saved is None:
                os.environ.pop("LD_LIBRARY_PATH", None)
            else:
                os.environ["LD_LIBRARY_PATH"] = saved


def say(text):
    print(text, flush=True)


def pause_before_closing():
    """A double-clicked program's window closes at once; keep error messages readable."""
    if os.name != "nt" or not sys.stdin or not sys.stdin.isatty():
        return
    try:
        import ctypes

        attached = ctypes.windll.kernel32.GetConsoleProcessList((ctypes.c_ulong * 4)(), 4)
    except Exception:  # noqa: BLE001
        return
    if attached <= 1:  # nobody else (such as a command prompt) uses this window
        try:
            input("\nPress Enter to close this window.")
        except EOFError:
            pass


def set_window_title(title):
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleTitleW(title)
        except Exception:  # noqa: BLE001
            pass


def use_readable_output():
    """Never crash on a character the console cannot show (e.g. Chinese in a Windows pipe)."""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")


class InstanceLock:
    """One portal per data folder. The operating system releases the lock if the launcher dies."""

    def __init__(self, home):
        self.path = Path(home) / "portal.lock"
        self.info_path = Path(home) / "portal.running.json"
        self.handle = None

    def acquire(self):
        handle = open(self.path, "a+b")  # noqa: SIM115 - held open while the portal runs
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def write_info(self, **info):
        self.info_path.write_text(json.dumps(info), encoding="utf-8")

    def read_info(self):
        try:
            return json.loads(self.info_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def release(self):
        if self.handle is not None:
            self.info_path.unlink(missing_ok=True)
            self.handle.close()
            self.handle = None

    def is_held_elsewhere(self):
        if self.acquire():
            self.release()
            return False
        return True


# --- the launcher (supervisor) ----------------------------------------------------------

def apply_command_line_settings(args, home):
    values = desktop.load_settings(home, demo=args.demo)
    changes = {}
    if args.port:
        changes["port"] = args.port
    if args.network:
        changes["network_access"] = True
    if args.local:
        changes["network_access"] = False
    if args.tunnel:
        changes["tunnel"] = args.tunnel
    if changes:
        values = desktop.update_settings(home, **changes)
    return values


def reset_demo_data(home):
    for name in (desktop.DATABASE_FILE, desktop.DATABASE_FILE + "-wal", desktop.DATABASE_FILE + "-shm"):
        (home / name).unlink(missing_ok=True)
    for folder in ("media", "outbox", "backups"):
        shutil.rmtree(home / folder, ignore_errors=True)
    desktop.prepare_home(home)


def portal_urls(values, tunnel):
    urls = desktop.access_urls(values)
    if tunnel.url:
        urls.append({"kind": "public", "url": tunnel.url})
    return urls


def print_banner(home, values, token, demo, tunnel):
    labels = {"local": "On this computer", "network": "Office network", "public": "From the internet"}
    urls = portal_urls(values, tunnel)
    lines = [f"{NAME}{' - DEMO' if demo else ''}   (version {VERSION})", ""]
    for entry in urls:
        lines.append(f"  {labels[entry['kind']] + ':':20}{entry['url']}")
    if tunnel.mode == "temporary" and tunnel.url:
        lines.append(f"  {'':20}(temporary: a new address every time the portal starts)")
    elif tunnel.mode == "temporary":
        lines.append(f"  {'From the internet:':20}" + ("Cloudflare Tunnel is not working yet (see above)"
                                                       if tunnel.state.get("state") == "failed"
                                                       else "Cloudflare Tunnel is starting (the address follows)"))
    elif tunnel.mode == "token" and not values["public_url"]:
        lines.append(f"  {'From the internet:':20}enter the address of the tunnel as public address on the People page")
    lines += ["", f"  Data folder:        {home}"]
    if not values["network_access"] and not values["public_url"] and tunnel.mode == "off":
        lines += ["  Only this computer can open the portal. Start with --network for the office network,",
                  "  or with --tunnel for people anywhere (see the guide)."]
    elif (values["public_url"] or tunnel.mode != "off") and not values["require_two_factor"]:
        lines.append("  The portal can be opened from the internet: on the People page, require two-factor sign-in.")
    if token:
        lines += ["", "  FIRST START: open this link to set up the portal (it works only once):",
                  f"  {urls[0]['url']}/setup/?token={token}"]
    if demo:
        lines += ["", f"  Demo users, all with password {DEMO_PASSWORD}:",
                  "  shipper.manager, shipper.ops, receiver.manager, receiver.ops, oversight, admin"]
    lines += ["", "  Keep this window open while people use the portal.",
              "  To stop the portal, close this window or press Ctrl+C."]
    rule = "=" * 76
    print("\n".join([rule, *lines, rule]), flush=True)


def wait_until_ready(port, process, instance, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        answer = portal_answer(port)
        if answer and answer.get("instance") == instance:
            return True
        time.sleep(0.4)
    return False


class StopRequested(Exception):
    """SIGTERM (the `stop` command, or the system shutting down)."""


def _stop_on_sigterm(signum, frame):
    raise StopRequested


def wait_for(process):
    """Wait for the portal process; short waits keep Ctrl+C responsive on Windows."""
    while True:
        try:
            return process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            continue


def stop_child(process, grace=3):
    if process.poll() is None:
        try:
            process.wait(grace)  # after Ctrl+C the portal is already on its way out
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(15)
            except subprocess.TimeoutExpired:
                process.kill()


def start(args, home):
    home = desktop.prepare_home(home)
    if args.demo:
        marker = home / DEMO_MARKER
        if (home / desktop.DATABASE_FILE).exists() and not marker.exists():
            raise LauncherError(f"{home} holds real data. The demo needs a folder of its own.")
        marker.write_text("This folder holds the practice copy of the portal with demo data. It can be deleted.\n",
                          encoding="utf-8")
    lock = InstanceLock(home)
    if not lock.acquire():
        url = lock.read_info().get("url") or f"http://127.0.0.1:{desktop.load_settings(home, demo=args.demo)['port']}"
        print(f"The portal is already running: {url}")
        if args.port or args.network or args.local or args.fresh or args.tunnel:
            print("It keeps its current settings. Stop it first (close its window) to start it differently.")
        if not args.no_browser:
            open_browser(url)
        return 0
    try:
        values = apply_command_line_settings(args, home)
        if args.fresh:
            if not args.demo:
                raise LauncherError("--fresh only works together with --demo (it deletes the demo data).")
            reset_demo_data(home)
        return supervise(args, home, lock, values)
    finally:
        lock.release()


def supervise(args, home, lock, values):
    port = values["port"]
    if port_in_use(port):
        raise LauncherError(
            f"Port {port} is already used by another program.\n"
            f"Close that program, or start the portal on another port, for example:  {PROGRAM} --port 8080"
        )
    token = "" if args.demo or count_users(home) else secrets.token_urlsafe(24)
    instance = desktop.instance_id(home)
    local_url = f"http://127.0.0.1:{port}"
    lock.write_info(pid=os.getpid(), port=port, url=local_url, version=VERSION)
    set_window_title(f"{NAME}{' (DEMO)' if args.demo else ''} - {local_url}")
    tunnel = Tunnel(home, PROGRAM_DIR, report=say)
    try:
        tunnel.announce = False
        tunnel.apply(values)
        if tunnel.mode == "temporary":
            tunnel.wait_ready(20)  # a few seconds as a rule; what comes later is printed when it comes
        print_banner(home, values, token, args.demo, tunnel)
        tunnel.announce = True
        return run_portal(args, home, lock, values, token, instance, tunnel)
    finally:
        tunnel.stop()


def run_portal(args, home, lock, values, token, instance, tunnel):
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop_on_sigterm)
    port = values["port"]
    local_url = f"http://127.0.0.1:{port}"
    command = [*self_command(), "--data-dir", str(home), *(["--demo"] if args.demo else []), "_serve"]
    crashes = deque(maxlen=5)
    first_start = True
    while True:
        env = dict(os.environ, **{desktop.SUPERVISED_ENV: "1", "PORTAL_SETUP_TOKEN": token,
                                  desktop.TUNNEL_SESSION_ENV: tunnel.session})
        process = subprocess.Popen(command, env=env)
        try:
            if wait_until_ready(port, process, instance) and first_start:
                first_start = False
                if not args.no_browser:
                    open_browser(f"{local_url}/setup/?token={token}" if token else f"{local_url}/")
            code = wait_for(process)
        except (KeyboardInterrupt, StopRequested) as reason:
            tunnel.stop()
            stop_child(process, grace=0 if isinstance(reason, StopRequested) else 3)
            print("\nThe portal has stopped.", flush=True)
            return 0
        if code == 0:
            print("\nThe portal has stopped.", flush=True)
            return 0
        if code == CONFIG_ERROR:
            raise LauncherError("The portal could not start (see the message above).")
        if code == desktop.RESTART_EXIT_CODE:
            values = desktop.load_settings(home, demo=args.demo)
            port, token = values["port"], ""
            local_url = f"http://127.0.0.1:{port}"
            lock.write_info(pid=os.getpid(), port=port, url=local_url, version=VERSION)
            tunnel.apply(values)
            print("\nRestarting with the new settings. The portal can now be opened at:", flush=True)
            for entry in portal_urls(values, tunnel):
                print(f"  {entry['url']}", flush=True)
            continue
        crashes.append(time.monotonic())
        if len(crashes) == crashes.maxlen and crashes[-1] - crashes[0] < 600:
            raise LauncherError(f"The portal stopped five times in ten minutes. Details: {home / 'logs' / 'portal.log'}")
        print(f"\nThe portal stopped unexpectedly (code {code}). Starting it again in 5 seconds...", flush=True)
        time.sleep(5)


# --- the portal process ---------------------------------------------------------------------

def configure_django(home, demo):
    values = desktop.load_settings(home, demo=demo)
    os.environ.update(desktop.django_environment(home, values, demo=demo))
    import django

    django.setup()
    return values


def configure_logging(home):
    """Everything goes to logs/portal.log; the window only shows errors."""
    root = logging.getLogger()
    for handler in root.handlers:
        if type(handler) is logging.StreamHandler:
            handler.setLevel(logging.ERROR)
    log_file = RotatingFileHandler(Path(home) / "logs" / "portal.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    log_file.setLevel(logging.INFO)
    log_file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(log_file)
    root.setLevel(logging.INFO)


def migrate(home, values):
    from django.core.management import call_command
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    if executor.migration_plan(executor.loader.graph.leaf_nodes()):
        if executor.loader.applied_migrations:
            result = desktop.make_backup(home, reason="before-upgrade", keep_days=values["keep_days"],
                                         copy_to=values["copy_to"])
            print(f"Upgrading the database (a backup was made first: {result.path.name})...", flush=True)
        else:
            print("Preparing the database (first start)...", flush=True)
        call_command("migrate", interactive=False, verbosity=0)
    connection.close()


def load_demo_data():
    from django.contrib.auth import get_user_model
    from django.core.management import call_command

    if not get_user_model().objects.exists():
        print("Loading the demo data...", flush=True)
        call_command("seed_demo", password=DEMO_PASSWORD)


def background_jobs(home, stop):
    """Reminders and translation retries every 15 minutes; a backup once a day."""
    from django.db import connection

    from core import translation as mt
    from core.management.commands.run_scheduler import retry_translations
    from logistics.reminders import run_reminders

    log = logging.getLogger("portal.jobs")
    while True:
        try:
            sent = run_reminders()
            mt.reset_cooldown()
            retry_translations()
            if sent:
                log.info("Reminders sent: %d", len(sent))
        except Exception:  # noqa: BLE001 - keep going; the next cycle tries again
            log.exception("Reminder cycle failed")
        try:
            result = desktop.backup_if_due(home, desktop.load_settings(home))
            if result:
                log.info("Daily backup saved: %s", result.path)
                if result.copy_error:
                    log.warning(result.copy_error)
        except Exception:  # noqa: BLE001
            log.exception("Daily backup failed")
        finally:
            connection.close()
        if stop.wait(900):
            return


def serve(args, home):
    home = desktop.prepare_home(home)
    values = configure_django(home, args.demo)
    configure_logging(home)
    migrate(home, values)
    if args.demo:
        load_demo_data()

    # Django is already set up; get_wsgi_application() would set it up again and
    # replace the logging configuration made above.
    from django.core.handlers.wsgi import WSGIHandler
    from waitress import create_server

    host = "0.0.0.0" if values["network_access"] else "127.0.0.1"
    try:
        server = create_server(
            WSGIHandler(),
            host=host,
            port=values["port"],
            threads=8,
            ident="cnpk-portal",
            channel_timeout=120,
            # A tunnel or reverse proxy on this computer (Cloudflare Tunnel, Caddy) reports the
            # visitor's HTTPS and address; such headers from anywhere else are removed.
            trusted_proxy="127.0.0.1",
            trusted_proxy_count=1,
            trusted_proxy_headers={"x-forwarded-proto", "x-forwarded-for"},
            clear_untrusted_proxy_headers=True,
        )
    except OSError as exc:
        print(f"\nPort {values['port']} cannot be used ({exc}).\n"
              f"Another program is probably using it. Start the portal with another port, e.g. {PROGRAM} --port 8080",
              flush=True)
        return CONFIG_ERROR

    stop = threading.Event()
    threading.Thread(target=background_jobs, args=(home, stop), name="portal-jobs", daemon=True).start()
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
    logging.getLogger("portal").info("Portal %s serving on %s:%s", VERSION, host, values["port"])
    try:
        server.run()
    finally:
        stop.set()
    return 0


# --- other commands --------------------------------------------------------------------

def backup(args, home):
    if not (home / desktop.DATABASE_FILE).exists():
        raise LauncherError(f"There is no portal data in {home} yet.")
    values = desktop.load_settings(home, demo=args.demo)
    result = desktop.make_backup(home, reason="manual", keep_days=values["keep_days"], copy_to=values["copy_to"])
    print(f"Backup saved: {result.path}")
    if result.copy_error:
        print(result.copy_error)
    return 0


def restore(args, home):
    archive = Path(args.archive).expanduser().resolve()
    lock = InstanceLock(home)
    if not home.exists():
        desktop.prepare_home(home)
    if not lock.acquire():
        raise LauncherError("The portal is running. Stop it first (close its window), then restore.")
    try:
        kept = desktop.restore_backup(home, archive)
    except desktop.BackupError as exc:
        raise LauncherError(str(exc)) from exc
    finally:
        lock.release()
    print(f"Restored {archive.name} into {home}.")
    print(f"The data it replaced was moved to {kept}.")
    print("Start the portal as usual; the database is upgraded automatically if needed.")
    return 0


def stop(args, home):
    lock = InstanceLock(home)
    if not lock.is_held_elsewhere():
        print("The portal is not running.")
        return 0
    pid = lock.read_info().get("pid")
    if not pid:
        raise LauncherError("The portal is running, but its process is unknown. Close its window instead.")
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
    else:
        os.kill(pid, signal.SIGTERM)
    for _attempt in range(60):
        time.sleep(0.5)
        if not lock.is_held_elsewhere():
            print("The portal has stopped.")
            return 0
    raise LauncherError("The portal did not stop within 30 seconds.")


def manage(args, home):
    """Run a Django management command, e.g. `CNPK-Portal manage changepassword admin`."""
    home = desktop.prepare_home(home)
    values = configure_django(home, args.demo)
    migrate(home, values)  # e.g. `manage createsuperuser` before the first start
    from django.core.management import execute_from_command_line

    execute_from_command_line([f"{PROGRAM} manage", *args.args])
    return 0


def _quote(part):
    return f'"{part}"'


def autostart(args, home):
    command = [*self_command(), "--no-browser", "--data-dir", str(home), *(["--demo"] if args.demo else [])]
    if os.name == "nt":
        target = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / f"{PROGRAM}.cmd"
        content = (
            "@echo off\r\n"
            f"rem Starts the {NAME} when you sign in to Windows. Delete this file to stop that.\r\n"
            f'start "{NAME}" /min {" ".join(_quote(part) for part in command)}\r\n'
        )
    elif sys.platform == "darwin":
        target = Path.home() / "Library" / "LaunchAgents" / "org.cnpk.portal.plist"
        content = plistlib.dumps({
            "Label": "org.cnpk.portal",
            "ProgramArguments": command,
            "RunAtLoad": True,
            "StandardOutPath": str(home / "logs" / "launcher.log"),
            "StandardErrorPath": str(home / "logs" / "launcher.log"),
        }).decode("utf-8")
    else:
        target = Path.home() / ".config" / "autostart" / f"{PROGRAM.lower()}.desktop"
        content = (
            "[Desktop Entry]\nType=Application\n"
            f"Name={NAME}\nExec={' '.join(_quote(part) for part in command)}\nTerminal=false\n"
        )
    if args.state == "off":
        target.unlink(missing_ok=True)
        print(f"The portal no longer starts automatically ({target} removed).")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    print(f"The portal now starts automatically when you sign in ({target}).")
    return 0


# --- command line ---------------------------------------------------------------------------

def build_parser():
    def add_common(parser, suppress):
        default = argparse.SUPPRESS if suppress else None
        parser.add_argument("--data-dir", metavar="FOLDER", default=default,
                            help=f"where the portal keeps its data (default: {desktop.FOLDER_NAME} in your home folder)")
        parser.add_argument("--demo", action="store_true", default=argparse.SUPPRESS if suppress else False,
                            help="use the practice copy with demo data (kept apart from real data, port 8001)")

    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description=f"Start the {NAME} on this computer and open it in the browser.",
        epilog="Guide: docs/05-run-on-your-computer.md",
    )
    add_common(parser, suppress=False)
    parser.add_argument("--version", action="version", version=f"{NAME} {VERSION}")
    parser.add_argument("--port", type=int, help="port number (remembered), default 8000")
    access = parser.add_mutually_exclusive_group()
    access.add_argument("--network", action="store_true",
                        help="let other computers in the office network open the portal (remembered)")
    access.add_argument("--local", action="store_true", help="only this computer may open the portal (remembered)")
    parser.add_argument("--tunnel", nargs="?", const="temporary", choices=desktop.TUNNEL_MODES,
                        help="share the portal over the internet with Cloudflare Tunnel (remembered): temporary "
                             "(the default; a random address, no account needed), token (your own address, set up "
                             "on the People page) or off")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    parser.add_argument("--fresh", action="store_true", help="with --demo: start again from fresh demo data")

    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub = commands.add_parser("backup", help="make a backup now (also while the portal runs)")
    add_common(sub, suppress=True)
    sub = commands.add_parser("restore", help="put a backup back (the portal must be stopped)")
    add_common(sub, suppress=True)
    sub.add_argument("archive", help="the backup file, e.g. backups/portal-backup-20260929-021500.zip")
    sub = commands.add_parser("stop", help="stop a running portal")
    add_common(sub, suppress=True)
    sub = commands.add_parser("autostart", help="start the portal when you sign in to this computer")
    add_common(sub, suppress=True)
    sub.add_argument("state", choices=["on", "off"])
    sub = commands.add_parser("manage", help="run a Django management command (advanced)")
    add_common(sub, suppress=True)
    sub.add_argument("args", nargs=argparse.REMAINDER)
    sub = commands.add_parser("_serve")  # internal: the portal process started by the launcher
    add_common(sub, suppress=True)
    return parser


COMMANDS = {"backup": backup, "restore": restore, "stop": stop, "autostart": autostart, "manage": manage, "_serve": serve}


def main(argv=None):
    use_readable_output()
    args = build_parser().parse_args(argv)
    home = Path(args.data_dir).expanduser().resolve() if args.data_dir else desktop.default_home(args.demo)
    try:
        if args.command is None:
            return start(args, home)
        return COMMANDS[args.command](args, home)
    except (LauncherError, desktop.SettingsError, desktop.BackupError) as exc:
        print(f"\n{exc}\n", file=sys.stderr, flush=True)
        pause_before_closing()
        return CONFIG_ERROR
    except (KeyboardInterrupt, StopRequested):
        return 0
    except Exception:  # noqa: BLE001 - show what happened before the window closes
        traceback.print_exc()
        pause_before_closing()
        return 1


if __name__ == "__main__":
    sys.exit(main())
