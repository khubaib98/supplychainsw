# PyInstaller recipe for the desktop edition. Build with:  python launcher/build.py
#
# The result is a folder (not a single file): it starts faster and antivirus
# programs flag it far less often than a self-extracting single .exe.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821 - defined by PyInstaller
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

# The portal's own apps are named in settings (strings), so list them explicitly;
# the same goes for middleware and the waitress server.
hiddenimports = []
for package in ("config", "core", "accounts", "logistics"):
    hiddenimports += collect_submodules(package, filter=lambda name: ".tests" not in name)
for package in ("whitenoise", "waitress", "tzdata"):
    hiddenimports += collect_submodules(package)

datas = [
    (str(SRC / "templates"), "templates"),
    (str(SRC / "static"), "static"),
    (str(SRC / "locale"), "locale"),
]
datas += collect_data_files("tzdata")

a = Analysis(  # noqa: F821
    [str(ROOT / "launcher" / "portal_launcher.py")],
    pathex=[str(SRC)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "psycopg", "psycopg2", "psycopg_binary", "gunicorn", "IPython", "pytest"],
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CNPK-Portal",
    console=True,  # the window shows the addresses; closing it stops the portal
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="CNPK-Portal", upx=False)  # noqa: F821
