#!/usr/bin/env python3

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
from pathlib import Path

BUILD_PACKAGES = ["nuitka", "zstandard"]

SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "__pycache__", "build", "dist", "node_modules",
    ".tox", ".mypy_cache", ".pytest_cache", "site-packages", "tests", "test", "docs",
}

CHECK_SNIPPET = (
    "import importlib.util as u, sys\n"
    "for n in sys.argv[1:]:\n"
    "    try: ok = u.find_spec(n) is not None\n"
    "    except Exception: ok = False\n"
    "    if not ok: print(n, int(n in getattr(sys, 'stdlib_module_names', ())))\n"
)

ROOT_MARKERS = ("pyproject.toml", "requirements.txt", "setup.py", ".git")

SYS_PATH_RE = re.compile(r"(?m)^\s*sys\.path\.(?:insert|append)\((.*)$")


def run(cmd: list[str], **kwargs) -> None:
    print("$", " ".join(cmd))
    if subprocess.call(cmd, **kwargs) != 0:
        sys.exit("ERROR: command failed")


def load_toml(path: Path) -> dict:
        import tomllib
        return tomllib.loads(path.read_text(encoding="utf-8"))

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


def read_project(root: Path) -> tuple[list[str], str, str | None]:
    name = None
    pyproject = root / "pyproject.toml"
    data = load_toml(pyproject) if pyproject.exists() else {}
    project = data.get("project", {})
    poetry = data.get("tool", {}).get("poetry", {})
    name = project.get("name") or poetry.get("name")

    deps = [d for d in project.get("dependencies", []) if isinstance(d, str)]
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
    venv = Path(args.output_dir).resolve() / ".venv"
    python = venv / "bin/python"
    if not python.exists():
        run([sys.executable, "-m", "venv", str(venv)])
    pip = [str(python), "-m", "pip", "install", "-q", "--disable-pip-version-check"]
    run(pip + BUILD_PACKAGES + deps + list(args.dep or []))
    if args.install_project:
        run(pip + [str(root)])
    return str(python)


def check_imports(python: str, imports: set[str], entry: Path, root: Path, env: dict) -> None:
    names = sorted(imports)
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
        if any((root /f).is_file() for f in ("pyproject.toml", "setup.py", "setup.cfg")):
            print("         The project declares its own packaging metadata.")
            print("         Add to EXTRA: --install-project")
            print("         (installs the exact versions, extras and package metadata; safer than --dep)")
        else:
            print("         Add to EXTRA: "
                  + " ".join("--dep " + n for n in missing_pip))
    for n in missing_os:
        print("WARNING: '%s' is not installed in this Python; it comes from an OS package, not pip." % n)


def build_command(args, entry: Path, root: Path, python: str, name: str) -> list[str]:
    mode = args.mode
    if mode == "auto":
        mode = "onefile"
    out = Path(args.output_dir).resolve()

    package_mode = entry.name == "__main__.py" and (entry.parent / "__init__.py").exists()

    cmd = [python, "-m", "nuitka", "--mode=" + mode, "--output-dir=" + str(out), "--assume-yes-for-downloads"]
    cmd.append("--output-filename=" + name)
    if package_mode:
        cmd.append("--python-flag=-m")

    cmd += ["--enable-plugins=" + p for p in (args.enable_plugin or [])]

    for spec in args.data_dir or []:
        src, _, dst = spec.partition("=")
        src_path = (root / src).resolve()
        cmd.append("--include-data-dir=%s=%s" % (src_path, dst or src_path.name))
    for spec in args.data_file or []:
        src, _, dst = spec.partition("=")
        cmd.append("--include-data-files=%s=%s" % (root / src, dst or ("./" if "*" in src else Path(src).name)))

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
    p.add_argument("--mode", default="auto", choices=["auto", "onefile", "standalone"],
                   help="auto: onefile")
    p.add_argument("--dep", action="append", help="extra package for the venv (repeatable)")
    p.add_argument("--install-project", action="store_true", help="also pip install the project itself (package data/metadata)")
    p.add_argument("--enable-plugin", action="append", help="extra Nuitka plugin")
    p.add_argument("--data-dir", action="append", help="SRC[=DEST] directory shipped next to the binary (relative to root)")
    p.add_argument("--data-file", action="append", help="SRC[=DEST] file or glob")
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
    deps, source, project_name = read_project(root)
    vendored = detect_sys_path(entry)
    imports = scan_imports(root, entry, vendored)
    name = (project_name and re.sub(r"[^\w.-]+", "-", project_name)) or entry.stem

    print("entry        :", entry)
    print("project root :", root)
    print("dependencies : %d (%s)" % (len([d for d in deps if not d.startswith("-")]), source))

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
        python = sys.executable
    else:
        python = prepare_env(args, deps, root)
        check_imports(python, imports, entry, root, env)

    cmd = build_command(args, entry, root, python, name)
    print("nuitka command:\n   ", " ".join(cmd))
    if args.dry_run:
        return 0

    rc = subprocess.call(cmd, cwd=str(root), env=env)
    if rc == 0:
        print("\nDONE. Output directory:", Path(args.output_dir).resolve())
    return rc


if __name__ == "__main__":
    sys.exit(main())
