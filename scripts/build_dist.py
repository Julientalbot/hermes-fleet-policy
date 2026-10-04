"""Build the release artifacts into OUT_DIR (default: ./dist).

* ``fleet-policy-plugin-<v>.tar.gz`` — the plugin folder alone, top directory ``fleet-policy/``:
  extract it straight into ``$HERMES_HOME/plugins/`` (no pip).
* ``hermes_fleet_policy-<v>.tar.gz`` / ``.whl`` — sdist + wheel (``uv build`` or ``python -m build``).
* ``SHA256SUMS``.

Usage: python scripts/build_dist.py [OUT_DIR]
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "fleet_policy"


def version() -> str:
    return re.search(r'^__version__ = "([^"]+)"', (PKG / "__init__.py").read_text(), re.M).group(1)


def plugin_tarball(out: Path, ver: str) -> Path:
    target = out / f"fleet-policy-plugin-{ver}.tar.gz"

    def keep(info: tarfile.TarInfo):
        if "__pycache__" in info.name or info.name.endswith(".pyc"):
            return None
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mode = 0o755 if info.isdir() else 0o644
        return info

    with tarfile.open(target, "w:gz") as tar:
        tar.add(PKG, arcname="fleet-policy", filter=keep)
    return target


def python_dists(out: Path) -> None:
    if shutil.which("uv"):
        subprocess.run(["uv", "build", "--out-dir", str(out)], cwd=REPO, check=True)
    else:
        subprocess.run([sys.executable, "-m", "build", "--outdir", str(out)], cwd=REPO, check=True)


def main() -> int:
    out = Path(sys.argv[1]).expanduser().resolve() if len(sys.argv) > 1 else REPO / "dist"
    out.mkdir(parents=True, exist_ok=True)
    ver = version()
    for old in out.glob("*"):
        if ver in old.name or old.name == "SHA256SUMS":
            old.unlink()
    plugin_tarball(out, ver)
    python_dists(out)
    sums = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}"
            for p in sorted(out.iterdir()) if p.is_file() and ver in p.name]
    (out / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    print("\n".join(sums))
    return 0


if __name__ == "__main__":
    sys.exit(main())
