#!/usr/bin/env python3
"""
nuitka_build.py - Bir Python projesinin giris dosyasini ver, Nuitka ile native derle.

Akis:
  1. proje kokunu bul         pyproject.toml / requirements.txt / setup.py / .git olan ilk ust dizin
  2. bagimliliklari oku       PEP 723 satir-ici blok > pyproject.toml > requirements.txt
  3. kaynaklari tara          import'lardan gerekli Nuitka plugin'leri ve GUI olup olmadigi
  4. build ortamini hazirla   ayri bir venv'e SADECE nuitka + projenin bagimliliklari kurulur
  5. Nuitka komutunu kur      ve calistir

Kullanim:
  python nuitka_build.py main.py                      # build/.venv olusturur, onefile derler
  python nuitka_build.py main.py --mode standalone
  python nuitka_build.py pkg/__main__.py --install-project
  python nuitka_build.py main.py --dry-run            # sadece komutu goster, derleme yapma
  python nuitka_build.py main.py -- --show-scons      # "--" sonrasi dogrudan Nuitka'ya gider

Sadece standart kutuphane kullanir. Python >= 3.8.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
from pathlib import Path

WINDOWS = sys.platform == "win32"
MACOS = sys.platform == "darwin"

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

ROOT_MARKERS = ("pyproject.toml", "requirements.txt", "setup.py", ".git")

PEP723_RE = re.compile(r"(?m)^# /// script$\s(?P<content>(^#(| .*)$\s)+)^# ///$")

def run(cmd: list[str], **kwargs) -> None:
    print("$", " ".join(cmd))
    if subprocess.call(cmd, **kwargs) != 0:
        sys.exit("HATA: komut basarisiz oldu")

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

    return [], "yok", name

def scan_imports(root: Path, entry: Path) -> set[str]:
    files = {entry}
    for dirpath, dirnames, filenames in os.walk(root):
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
            sys.exit("HATA: Nuitka bu yorumlayicida kurulu degil: %s   (pip install nuitka)" % python)
        return python

    venv = Path(args.venv or os.path.join(args.output_dir, ".venv")).resolve()
    python = venv / ("Scripts/python.exe" if WINDOWS else "bin/python")
    if not python.exists():
        run([args.python or sys.executable, "-m", "venv", str(venv)])
    pip = [str(python), "-m", "pip", "install", "-q", "--disable-pip-version-check"]
    run(pip + BUILD_PACKAGES + deps + list(args.dep or []))
    if args.install_project:
        run(pip + [str(root)])
    return str(python)

def build_command(args, entry: Path, root: Path, python: str, imports: set[str], name: str) -> list[str]:
    gui = bool(imports & GUI_IMPORTS) and not args.console
    mode = args.mode
    if mode == "auto":
        mode = "app" if (MACOS and gui) else "onefile"
    out = Path(args.output_dir).resolve()

    package_mode = entry.name == "__main__.py" and (entry.parent / "__init__.py").exists()

    cmd = [python, "-m", "nuitka", "--mode=" + mode, "--output-dir=" + str(out), "--assume-yes-for-downloads"]
    if not (MACOS and mode in ("app", "app-dist")):
        cmd.append("--output-filename=" + name + (".exe" if WINDOWS else ""))
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

    if args.icon:
        icon = str(Path(args.icon).resolve())
        if WINDOWS:
            cmd.append("--windows-icon-from-ico=" + icon)
        elif MACOS and mode in ("app", "app-dist"):
            cmd.append("--macos-app-icon=" + icon)
        elif not MACOS and mode == "onefile":
            cmd.append("--linux-icon=" + icon)

    if WINDOWS:
        cmd.append("--windows-console-mode=" + ("disable" if gui else "force"))
    if MACOS and mode in ("app", "app-dist"):
        cmd += ["--macos-app-name=" + name, "--macos-app-mode=" + ("gui" if gui else "background")]

    cmd += args.nuitka_args                              # "--" sonrasi ham Nuitka bayraklari
    cmd.append(str(entry.parent if package_mode else entry))
    return cmd


def parse_args() -> argparse.Namespace:
    argv = sys.argv[1:]
    extra: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("entry", help="projenin giris .py dosyasi")
    p.add_argument("--root", help="proje koku (varsayilan: otomatik bulunur)")
    p.add_argument("-o", "--output-dir", default="build", help="cikti dizini (varsayilan: build)")
    p.add_argument("-n", "--name", help="ikili/uygulama adi (varsayilan: pyproject adi veya dosya adi)")
    p.add_argument("--mode", default="auto", choices=["auto", "onefile", "standalone", "app", "app-dist", "accelerated"],
                   help="auto: onefile; macOS + GUI ise app")
    p.add_argument("--venv", help="build venv dizini (varsayilan: <output-dir>/.venv)")
    p.add_argument("--no-venv", action="store_true", help="venv kurma, --python (veya bu python) ile dogrudan derle")
    p.add_argument("--python", help="temel python yorumlayicisi")
    p.add_argument("--dep", action="append", help="venv'e ek paket (tekrarlanabilir)")
    p.add_argument("--install-project", action="store_true", help="projeyi de venv'e pip install et (paket verisi/metadata icin)")
    p.add_argument("-j", "--jobs", type=int, help="paralel C derleme sayisi")
    p.add_argument("--enable-plugin", action="append", help="ek Nuitka plugin'i")
    p.add_argument("--include-package", action="append", help="dinamik import edilen paketi zorla dahil et")
    p.add_argument("--include-package-data", action="append", help="paketin veri dosyalarini dahil et")
    p.add_argument("--data-dir", action="append", help="SRC[=HEDEF] dizini ikilinin yanina kopyala (koke gore)")
    p.add_argument("--data-file", action="append", help="SRC[=HEDEF] dosya veya glob")
    p.add_argument("--icon", help=".ico/.icns/.png; platforma gore dogru bayraga cevrilir")
    p.add_argument("--console", action="store_true", help="GUI tespit edilse de konsolu acik birak")
    p.add_argument("--dry-run", action="store_true", help="komutu goster, derleme")
    args = p.parse_args(argv)
    args.nuitka_args = extra
    return args


def main() -> int:
    args = parse_args()
    entry = Path(args.entry).resolve()
    if not entry.is_file():
        sys.exit("HATA: giris dosyasi yok: %s" % entry)

    root = find_root(entry, args.root)
    deps, source, project_name = read_project(entry, root)
    imports = scan_imports(root, entry)
    name = args.name or (project_name and re.sub(r"[^\w.-]+", "-", project_name)) or entry.stem

    print("giris        :", entry)
    print("proje koku   :", root)
    print("bagimlilik   : %d (%s)" % (len([d for d in deps if not d.startswith("-")]), source))
    print("import'lar   :", ", ".join(sorted(i for i in imports if i in GUI_IMPORTS or i in PLUGIN_BY_IMPORT)) or "-")

    if args.dry_run:
        python = args.python or sys.executable
    else:
        python = prepare_env(args, deps, root)

    cmd = build_command(args, entry, root, python, imports, name)
    print("nuitka komutu:\n   ", " ".join(cmd))
    if args.dry_run:
        return 0

    env = dict(os.environ, PYTHONUTF8="1")
    if (root / "src").is_dir():
        env["PYTHONPATH"] = str(root / "src") + os.pathsep + env.get("PYTHONPATH", "")
    rc = subprocess.call(cmd, cwd=str(root), env=env)
    if rc == 0:
        print("\nTAMAM. Cikti dizini:", Path(args.output_dir).resolve())
    return rc


if __name__ == "__main__":
    sys.exit(main())
