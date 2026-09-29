"""
Compile gettext catalogues (.po -> .mo) without the GNU gettext tools, so
the Docker build and Windows machines need nothing beyond pip.

    python deploy/compile_po.py src/locale
"""
import pathlib
import sys

import polib


def main(root):
    compiled = 0
    for po_path in sorted(pathlib.Path(root).glob("*/LC_MESSAGES/*.po")):
        catalogue = polib.pofile(str(po_path))
        catalogue.save_as_mofile(str(po_path.with_suffix(".mo")))
        compiled += 1
        print(f"compiled {po_path} ({catalogue.percent_translated()}% translated)")
    if not compiled:
        sys.exit(f"no .po files found under {root}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "src/locale")
