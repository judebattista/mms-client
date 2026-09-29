"""Helpers for packaging/build-deb.sh, run with the staged (bundled) Python.

    deb_helpers.py build-info --out FILE --version V --package-version PV --commit SHA --dirty 0|1
                              --source-date YYYY-MM-DD --python-version X.Y.Z --arch ARCH
                              --requirements REQ.txt --wheel WHEEL
    deb_helpers.py copyright  --out FILE --site-packages DIR --python-prefix DIR

``build-info`` writes /opt/ied-client/BUILD-INFO.json (read by ``ied_client.buildinfo``);
``copyright`` writes /usr/share/doc/ied-client/copyright from the licence metadata and licence
files of every bundled distribution plus CPython's.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from email.parser import Parser
from pathlib import Path

BUILD_INFO_KIND = "ied-client-build"  # ied_client.buildinfo.BUILD_INFO_KIND


def _requirements(path: Path) -> list[str]:
    """``name==version`` lines of a uv-exported requirements file."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+==[^\s\\;]+)", line)
        if m:
            out.append(m.group(1))
    return out


def build_info(ns: argparse.Namespace) -> None:
    wheel = Path(ns.wheel)
    data = {
        "kind": BUILD_INFO_KIND,
        "schema_version": 1,
        "version": ns.version,
        "package_version": ns.package_version,
        "architecture": ns.arch,
        "git_commit": ns.commit,
        "git_dirty": ns.dirty == "1",
        "source_date": ns.source_date,
        "python_version": ns.python_version,
        "project_wheel": {"name": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()},
        "requirements": _requirements(Path(ns.requirements)),
    }
    Path(ns.out).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _dist_infos(site: Path) -> list[Path]:
    return sorted(site.glob("*.dist-info"), key=lambda p: p.name.lower())


def _licence_files(dist_info: Path) -> list[Path]:
    lic_dir = dist_info / "licenses"
    files = sorted(p for p in lic_dir.rglob("*") if p.is_file()) if lic_dir.is_dir() else []
    files += sorted(p for p in dist_info.iterdir() if p.is_file() and re.match(r"(?i)(licen[cs]e|copying|notice)", p.name))
    return files


def _licence_label(meta) -> str:
    if meta.get("License-Expression"):
        return meta["License-Expression"]
    classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    if classifiers:
        return "; ".join(classifiers)
    lic = (meta.get("License") or "").strip()
    return lic.splitlines()[0] if lic else "see licence text below"


def copyright_file(ns: argparse.Namespace) -> None:
    site = Path(ns.site_packages)
    py = Path(ns.python_prefix)
    lines = [
        "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/",
        "Upstream-Name: ied-client",
        "Comment: This package bundles a private CPython and the Python distributions listed below",
        " under /opt/ied-client. ied-client itself is GPL-3.0-or-later because libiec61850 (bundled",
        " in pyiec61850-ng) is GPLv3 / commercial and pyiec61850-ng is GPLv3. Distribution outside",
        " the organisation needs review (MMS-PROTOCOL-SPEC.md section 1).",
        "",
        "Files: /opt/ied-client/python/lib/python3.12/site-packages/ied_client/*",
        " /opt/ied-client/python/lib/python3.12/site-packages/mms_protocol/*",
        "License: GPL-3.0-or-later",
        " On Debian systems the full text is in /usr/share/common-licenses/GPL-3.",
        "",
    ]
    texts: list[tuple[str, str]] = []
    for di in _dist_infos(site):
        meta = Parser().parsestr(di.joinpath("METADATA").read_text(encoding="utf-8", errors="replace"))
        name, version = meta.get("Name", di.name), meta.get("Version", "?")
        if name.lower().replace("_", "-") == "ied-client":
            continue
        top = site / name.lower().replace("-", "_")
        lines += [
            f"Files: {top}/*" if top.exists() else f"Files: {site}/{name} (see its dist-info)",
            f"Comment: {name} {version}",
            f"License: {_licence_label(meta)}",
            "",
        ]
        for f in _licence_files(di):
            texts.append((f"{name} {version}: {f.relative_to(di)}", f.read_text(encoding="utf-8", errors="replace")))
    cpython_licence = py / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "LICENSE.txt"
    lines += ["Files: /opt/ied-client/python/*", f"Comment: CPython {sys.version.split()[0]} (python-build-standalone)",
              "License: PSF-2.0 (and the licences of the libraries it bundles; text below)", ""]
    if cpython_licence.is_file():
        texts.append((f"CPython {sys.version.split()[0]}: LICENSE.txt", cpython_licence.read_text(encoding="utf-8", errors="replace")))
    for title, text in texts:
        lines += ["-" * 100, title, "-" * 100, text.rstrip("\n"), ""]
    Path(ns.out).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="deb_helpers.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build-info")
    for opt in ("out", "version", "package-version", "commit", "dirty", "source-date", "python-version", "arch",
                "requirements", "wheel"):
        b.add_argument(f"--{opt}", required=True)
    c = sub.add_parser("copyright")
    for opt in ("out", "site-packages", "python-prefix"):
        c.add_argument(f"--{opt}", required=True)
    ns = p.parse_args(argv)
    {"build-info": build_info, "copyright": copyright_file}[ns.cmd](ns)
    return 0


if __name__ == "__main__":
    sys.exit(main())
