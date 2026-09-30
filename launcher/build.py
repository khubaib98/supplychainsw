"""
Build the ready-made desktop program with PyInstaller:

    pip install -r launcher/requirements-build.txt
    python launcher/build.py

Result: dist/CNPK-Portal/ (the program folder) and
dist/CNPK-Portal-<version>-<platform>.zip (what people download). A program
is built for the system it is built on; GitHub Actions builds the Windows,
macOS and Linux versions (.github/workflows/desktop.yml).
"""
import os
import platform
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
NAME = "CNPK-Portal"

README = """\
CN-PK Consignment Portal - desktop edition {version}
=====================================================

START
  Windows:  double-click CNPK-Portal.exe
  macOS:    double-click CNPK-Portal (the first time: right-click it, choose Open, then Open)
  Linux:    ./CNPK-Portal

A window with the portal's addresses opens, and then your browser. On the first
start the browser shows a set-up page: create your administrator account and
name the Shipper, Receiver and Oversight organisations. Then add people on the
People page and send each person the sign-in message it prepares for you.

Keep the window open while people use the portal. Close it to stop the portal.

YOUR DATA
  Everything is kept in the folder "CNPK-Portal" in your home folder, for
  example C:\\Users\\<you>\\CNPK-Portal: the database, uploaded documents, daily
  backups and settings.ini. This program folder holds no data, so to upgrade
  you simply replace it with the new version.

LETTING OTHER PEOPLE CONNECT
  Same office network: tick "Other computers in the office network may open
  the portal" on the People page (or start with: CNPK-Portal --network).
  Other cities and countries: publish the portal through Cloudflare Tunnel.
  Step-by-step guide: docs/05-run-on-your-computer.md in the project.

DEMO
  {demo} opens a separate practice copy with sample data. Users
  shipper.manager, shipper.ops, receiver.manager, receiver.ops, oversight and
  admin all have the password Demo-Portal-2026.

MORE
  CNPK-Portal --help            all options
  CNPK-Portal backup            make a backup now
  CNPK-Portal restore FILE      put a backup back (stop the portal first)
  CNPK-Portal autostart on      start the portal when you sign in

WARNINGS YOU MAY SEE
  Windows "Windows protected your PC": click "More info", then "Run anyway"
  (the program is not code-signed).
  Windows Firewall, when network access is on: allow access on private networks.
"""

DEMO_SCRIPTS = {
    "windows": ("Start demo.cmd", '@echo off\r\n"%~dp0CNPK-Portal.exe" --demo %*\r\n'),
    "macos": ("Start demo.command", '#!/bin/sh\nexec "$(dirname "$0")/CNPK-Portal" --demo "$@"\n'),
    "linux": ("start-demo.sh", '#!/bin/sh\nexec "$(dirname "$0")/CNPK-Portal" --demo "$@"\n'),
}


def pins(path):
    found = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*([A-Za-z0-9_.-]+)(\[[^\]]*\])?==([^\s#;]+)", line)
        if match:
            found[match.group(1).lower()] = match.group(3)
    return found


def check_pins():
    """The desktop and server editions must run the same versions."""
    server = pins(ROOT / "requirements.txt")
    desktop = pins(ROOT / "launcher" / "requirements.txt")
    different = {name: (server[name], version) for name, version in desktop.items() if name in server and server[name] != version}
    if different:
        sys.exit(f"launcher/requirements.txt and requirements.txt pin different versions: {different}")


def platform_tag():
    system = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    machine = platform.machine().lower()
    machine = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64"}.get(machine, machine)
    return system, f"{system}-{machine}"


def add_extras(app, version, system):
    script_name, script = DEMO_SCRIPTS[system]
    demo = f'"{script_name}"'
    newline = "\r\n" if system == "windows" else "\n"
    (app / "README.txt").write_text(README.format(version=version, demo=demo).replace("\n", newline), encoding="utf-8")
    path = app / script_name
    path.write_text(script, encoding="utf-8", newline="")
    path.chmod(0o755)


def make_zip(folder, archive):
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                bundle.write(path, Path(folder.name) / path.relative_to(folder))  # keeps the "executable" flag


def main():
    sys.path.insert(0, str(ROOT / "src"))
    from config import VERSION

    check_pins()
    subprocess.run([sys.executable, str(ROOT / "deploy" / "compile_po.py"), str(ROOT / "src" / "locale")], check=True)
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(DIST),
         "--workpath", str(ROOT / "build"), str(ROOT / "launcher" / "cnpk-portal.spec")],
        check=True,
    )
    system, tag = platform_tag()
    app = DIST / NAME
    add_extras(app, VERSION, system)
    archive = DIST / f"{NAME}-{VERSION}-{tag}.zip"
    make_zip(app, archive)
    size = archive.stat().st_size / 1e6
    print(f"\nBuilt {app}{os.sep}\nDownload package: {archive} ({size:.0f} MB)")
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as out:
            out.write(f"archive={archive}\npackage={archive.stem}\n")


if __name__ == "__main__":
    main()
