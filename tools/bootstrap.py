"""Create .venv next to the project and install the hash-pinned dependencies.

Standard library only, so it runs on a bare Python install. It is a no-op when the
lock file has not changed since the last successful install, so launchers can call it
every time.

    python tools/bootstrap.py          # runtime deps (requirements.lock)
    python tools/bootstrap.py --dev    # plus pytest, ruff, pip-audit
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
MIN_PY = (3, 12)


def venv_python() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dev", action="store_true", help="also install test and lint tools")
    a = ap.parse_args()

    if sys.version_info < MIN_PY:
        print(f"Scope1008 needs Python {MIN_PY[0]}.{MIN_PY[1]} or newer, this is "
              f"{sys.version.split()[0]}. Get it from python.org.", file=sys.stderr)
        return 1

    lock = ROOT / ("requirements-dev.lock" if a.dev else "requirements.lock")
    if not lock.is_file():
        print(f"Missing {lock.name}; the checkout looks incomplete.", file=sys.stderr)
        return 1
    digest = hashlib.sha256(lock.read_bytes()).hexdigest()
    stamp = VENV / f".installed-{lock.stem}"

    py = venv_python()
    if not py.is_file():
        print("Creating virtual environment in .venv ...")
        venv.EnvBuilder(with_pip=True, clear=False).create(VENV)

    if stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == digest:
        return 0

    print(f"Installing dependencies from {lock.name} (hash-checked) ...")
    # Argument list, no shell: nothing here is interpolated into a command line.
    cmd = [str(py), "-m", "pip", "install", "--disable-pip-version-check",
           "--require-hashes", "--only-binary=:all:", "-r", str(lock)]
    r = subprocess.run(cmd, cwd=ROOT, check=False)  # noqa: S603
    if r.returncode != 0:
        print("Dependency install failed; see the pip output above.", file=sys.stderr)
        return r.returncode
    stamp.write_text(digest, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
