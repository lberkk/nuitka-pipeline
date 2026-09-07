#!/usr/bin/env python3
"""
nuitka_build.py - give it a Python project's entry file, get a native binary.

Steps:
  1. locate project root      first parent with pyproject.toml / requirements.txt / setup.py / .git
  2. read dependencies        PEP 723 inline block > pyproject.toml > requirements.txt
  3. scan sources             Nuitka plugins from imports, warn about missing packages
  4. prepare build env        a separate venv with ONLY nuitka + the project's dependencies
  5. build the Nuitka command and run it

Usage:
  python nuitka_build.py main.py                      # creates build/.venv, builds onefile
  python nuitka_build.py main.py --mode standalone
  python nuitka_build.py pkg/__main__.py --install-project
  python nuitka_build.py main.py --dry-run            # print the command, build nothing
  python nuitka_build.py main.py -- --show-scons      # everything after "--" goes to Nuitka

Standard library only. Python >= 3.8, Linux only.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
from pathlib import Path

BUILD_PACKAGES = ["nuitka", "zstandard"]

PLUGIN_BY_IMPORT = {
    "tkinter": "tk-inter", "customtkinter": "tk-inter", "ttkbootstrap": "tk-inter",
    "PySide6": "pyside6", "PySide2": "pyside2", "PyQt6": "pyqt6", "PyQt5": "pyqt5",
}

GUI_IMPORTS = {
    "tkinter", "customtkinter", "ttkbootstrap", "PySide6", "PySide2", "PyQt6", "PyQt5",
    "kivy", "wx", "dearpygui", "flet", "toga", "pygame", "webview", "PySimpleGUI", "arcade", "pyglet",
}

SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "__pycache__", "build", "dist", "node_modules",
    ".tox", ".mypy_cache", ".pytest_cache", "site-packages", "tests", "test", "docs",
}

PY2_ONLY = {
    "Queue", "StringIO", "cStringIO", "__builtin__", "httplib", "urllib2", "urlparse",
    "md5", "simplejson", "ConfigParser", "cPickle", "HTMLParser", "xmlrpclib",
    "SocketServer", "BaseHTTPServer", "thread", "commands", "cookielib", "Tkinter",
}

PACKAGE_BY_IMPORT = {
    "PIL": "Pillow", "cv2": "opencv-python", "yaml": "PyYAML", "sklearn": "scikit-learn",
    "bs4": "beautifulsoup4", "dateutil": "python-dateutil", "serial": "pyserial",
    "Crypto": "pycryptodome", "docx": "python-docx", "fitz": "PyMuPDF",
    "OpenGL": "PyOpenGL", "gi": "PyGObject", "zmq": "pyzmq", "jwt": "PyJWT",
    "attr": "attrs", "magic": "python-magic", "usb": "pyusb",
}

OS_PACKAGE_BY_MODULE = {
    "tkinter": "python3.11-tkinter (Rocky/Fedora) or python3-tk (Debian/Ubuntu)",
    "turtle": "python3.11-tkinter",
    "idlelib": "python3.11-tkinter",
}

CHECK_SNIPPET = (
    "import importlib.util as u, sys\n"
    "for n in sys.argv[1:]:\n"
    "    try: ok = u.find_spec(n) is not None\n"
    "    except Exception: ok = False\n"
    "    if not ok: print(n, int(n in getattr(sys, 'stdlib_module_names', ())))\n"
)

ROOT_MARKERS = ("pyproject.toml", "requirements.txt", "setup.py", ".git")

PEP723_RE = re.compile(r"(?m)^# /// script$\s(?P<content>(^#(| .*)$\s)+)^# ///$")

SYS_PATH_RE = re.compile(r"(?m)^\s*sys\.path\.(?:insert|append)\((.*)$")


def run(cmd: list[str], **kwargs) -> None:
    print("$", " ".join(cmd))
    if subprocess.call(cmd, **kwargs) != 0:
        sys.exit("ERROR: command failed")


def load_toml(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    try:
        import tomllib
        return tomllib.loads(text)
    except ImportError:
        data: dict = {"project": {}}
        m = re.search(r'(?m)^name\s*=\s*"([^"]+)"', text)
        if m:
            data["project"]["name"] = m.group(1)
        m = re.search(r"(?ms)^dependencies\s*=\s*\[(.*?)\]", text)
        if m:
            data["project"]["dependencies"] = re.findall(r'"([^"]+)"', m.group(1))
        return data


def find_root(entry: Path, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    d = entry.parent
    while True:
        if any((d / marker).exists() for marker in ROOT_MARKERS):
            return d
        if d.parent == d:
            return entry.parent
        d = d.parent


def read_project(entry: Path, root: Path) -> tuple[list[str], str, str | None]:
    name = None
    pyproject = root / "pyproject.toml"
    data = load_toml(pyproject) if pyproject.exists() else {}
    project = data.get("project", {})
    poetry = data.get("tool", {}).get("poetry", {})
    name = project.get("name") or poetry.get("name")

    m = PEP723_RE.search(entry.read_text(encoding="utf-8", errors="replace"))
    if m:
        content = "".join(
            line[2:] if line.startswith("# ") else line[1:]
            for line in m.group("content").splitlines(keepends=True)
        )
        dm = re.search(r"(?ms)dependencies\s*=\s*\[(.*?)\]", content)
        return re.findall(r'"([^"]+)"', dm.group(1)) if dm else [], "PEP 723", name

    deps = [d for d in project.get("dependencies", []) if isinstance(d, str)]
    if not deps and poetry.get("dependencies"):
        deps = [n for n in poetry["dependencies"] if n.lower() != "python"]
    if deps:
        return deps, "pyproject.toml", name

    req = root / "requirements.txt"
    if req.exists():
        return ["-r", str(req)], "requirements.txt", name

    return [], "none", name

def detect_sys_path(entry: Path) -> list[Path]:
    found = []
    for call in SYS_PATH_RE.findall(entry.read_text(encoding="utf-8", errors="replace")):
        parts = re.findall(r"['\"]([^'\"]+)['\"]", call)
        if not parts:
            continue
        p = entry.parent.joinpath(*parts)
        if p.is_dir() and p not in found:
            found.append(p)
    return found


def scan_imports(root: Path, entry: Path, skip: list[Path] = ()) -> set[str]:
    files = {entry}
    skip = [str(p) for p in skip]
    for dirpath, dirnames, filenames in os.walk(root):
        if any(dirpath == s or dirpath.startswith(s + os.sep) for s in skip):
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith((".dist", ".build"))]
        files.update(Path(dirpath) / f for f in filenames if f.endswith(".py"))

    names: set[str] = set()
    for py in files:
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names.add(node.module.split(".")[0])
    return names


def prepare_env(args, deps: list[str], root: Path) -> str:
    if args.no_venv:
        python = args.python or sys.executable
        if subprocess.call([python, "-c", "import nuitka"], stderr=subprocess.DEVNULL) != 0:
            sys.exit("ERROR: Nuitka is not installed for %s   (pip install nuitka)" % python)
        return python

    venv = Path(args.venv or os.path.join(args.output_dir, ".venv")).resolve()
    python = venv / "bin/python"
    if not python.exists():
        run([args.python or sys.executable, "-m", "venv", str(venv)])
    pip = [str(python), "-m", "pip", "install", "-q", "--disable-pip-version-check"]
    run(pip + BUILD_PACKAGES + deps + list(args.dep or []))
    if args.install_project:
        run(pip + [str(root)])
    return str(python)


def check_imports(python: str, imports: set[str], entry: Path, root: Path, env: dict) -> None:
    names = sorted(n for n in imports if n not in PY2_ONLY)
    if not names:
        return
    env = dict(env)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(entry.parent), env.get("PYTHONPATH", "")) if p)
    try:
        out = subprocess.run([python, "-c", CHECK_SNIPPET] + names, cwd=str(root), env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=180)
    except OSError:
        return

    missing_pip, missing_os = [], []
    for line in out.stdout.split("\n"):
        parts = line.split()
        if len(parts) == 2:
            (missing_os if parts[1] == "1" else missing_pip).append(parts[0])
    if not (missing_pip or missing_os):
        return

    print()
    if missing_pip:
        print("WARNING: packages not found in the build environment:", ", ".join(missing_pip))
        print("         The build may finish, but the binary will fail with ModuleNotFoundError.")
        print("         Add to EXTRA: "
              + " ".join("--dep " + PACKAGE_BY_IMPORT.get(n, n) for n in missing_pip))
    for n in missing_os:
        print("WARNING: '%s' is not installed in this Python; it comes from an OS package, not pip." % n)
        if n in OS_PACKAGE_BY_MODULE:
            print("         Add to the Dockerfile: " + OS_PACKAGE_BY_MODULE[n])
    print()


def build_command(args, entry: Path, root: Path, python: str, imports: set[str], name: str) -> list[str]:
    mode = args.mode
    if mode == "auto":
        mode = "onefile"
    out = Path(args.output_dir).resolve()

    package_mode = entry.name == "__main__.py" and (entry.parent / "__init__.py").exists()

    cmd = [python, "-m", "nuitka", "--mode=" + mode, "--output-dir=" + str(out), "--assume-yes-for-downloads"]
    cmd.append("--output-filename=" + name)
    if package_mode:
        cmd.append("--python-flag=-m")
    if args.jobs:
        cmd.append("--jobs=%d" % args.jobs)

    plugins = {PLUGIN_BY_IMPORT[i] for i in imports if i in PLUGIN_BY_IMPORT} | set(args.enable_plugin or [])
    cmd += ["--enable-plugins=" + p for p in sorted(plugins)]
    cmd += ["--include-package=" + p for p in args.include_package or []]
    cmd += ["--include-package-data=" + p for p in args.include_package_data or []]

    for spec in args.data_dir or []:
        src, _, dst = spec.partition("=")
        src_path = (root / src).resolve()
        cmd.append("--include-data-dir=%s=%s" % (src_path, dst or src_path.name))
    for spec in args.data_file or []:
        src, _, dst = spec.partition("=")
        cmd.append("--include-data-files=%s=%s" % (root / src, dst or ("./" if "*" in src else Path(src).name)))

    if args.icon and mode == "onefile":
        cmd.append("--linux-icon=" + str(Path(args.icon).resolve()))

    cmd += args.nuitka_args
    cmd.append(str(entry.parent if package_mode else entry))
    return cmd


def parse_args() -> argparse.Namespace:
    argv = sys.argv[1:]
    extra: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("entry", help="the project's entry .py file")
    p.add_argument("--root", help="project root (default: detected automatically)")
    p.add_argument("-o", "--output-dir", default="build", help="output directory (default: build)")
    p.add_argument("-n", "--name", help="binary name (default: pyproject name or file name)")
    p.add_argument("--mode", default="auto", choices=["auto", "onefile", "standalone", "accelerated"],
                   help="auto: onefile")
    p.add_argument("--venv", help="build venv directory (default: <output-dir>/.venv)")
    p.add_argument("--no-venv", action="store_true", help="skip the venv, build with --python (or this python)")
    p.add_argument("--python", help="base python interpreter")
    p.add_argument("--dep", action="append", help="extra package for the venv (repeatable)")
    p.add_argument("--install-project", action="store_true", help="also pip install the project itself (package data/metadata)")
    p.add_argument("-j", "--jobs", type=int, help="parallel C compiler jobs")
    p.add_argument("--enable-plugin", action="append", help="extra Nuitka plugin")
    p.add_argument("--include-package", action="append", help="force-include a dynamically imported package")
    p.add_argument("--include-package-data", action="append", help="include a package's data files")
    p.add_argument("--data-dir", action="append", help="SRC[=DEST] directory shipped next to the binary (relative to root)")
    p.add_argument("--data-file", action="append", help="SRC[=DEST] file or glob")
    p.add_argument("--icon", help=".png embedded into the onefile binary (--linux-icon)")
    p.add_argument("--dry-run", action="store_true", help="print the command, build nothing")
    p.add_argument("--path", action="append", help="extra directory on PYTHONPATH during the build (relative to root)")
    args = p.parse_args(argv)
    args.nuitka_args = extra
    return args


def main() -> int:
    args = parse_args()
    entry = Path(args.entry).resolve()
    if not entry.is_file():
        sys.exit("ERROR: entry file not found: %s" % entry)

    root = find_root(entry, args.root)
    deps, source, project_name = read_project(entry, root)
    vendored = detect_sys_path(entry)
    imports = scan_imports(root, entry, vendored)
    name = args.name or (project_name and re.sub(r"[^\w.-]+", "-", project_name)) or entry.stem

    print("entry        :", entry)
    print("project root :", root)
    print("dependencies : %d (%s)" % (len([d for d in deps if not d.startswith("-")]), source))
    print("imports      :", ", ".join(sorted(i for i in imports if i in GUI_IMPORTS or i in PLUGIN_BY_IMPORT)) or "-")

    env = dict(os.environ, PYTHONUTF8="1")
    extra_paths = [Path(p) if os.path.isabs(p) else root / p for p in (args.path or [])]
    extra_paths += vendored
    if (root / "src").is_dir():
        extra_paths.append(root / "src")
    kept = []
    for p in extra_paths:
        if p.is_dir() and str(p) not in kept:
            kept.append(str(p))
    if kept:
        print("pythonpath   :", os.pathsep.join(kept))
        env["PYTHONPATH"] = os.pathsep.join(kept + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))

    if args.dry_run:
        python = args.python or sys.executable
    else:
        python = prepare_env(args, deps, root)
        check_imports(python, imports, entry, root, env)

    cmd = build_command(args, entry, root, python, imports, name)
    print("nuitka command:\n   ", " ".join(cmd))
    if args.dry_run:
        return 0

    rc = subprocess.call(cmd, cwd=str(root), env=env)
    if rc == 0:
        print("\nDONE. Output directory:", Path(args.output_dir).resolve())
    return rc


if __name__ == "__main__":
    sys.exit(main())
