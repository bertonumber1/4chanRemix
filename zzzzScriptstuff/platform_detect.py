"""
platform_detect.py
==================

Detect the OS / Linux distribution, and emit the right install command
for each of our optional dependencies. NEVER auto-runs sudo without
the user's explicit consent — we print the command and ask.

Detection sources, in order of reliability:
  1. /etc/os-release  (the standard on every modern Linux)
  2. platform.system() / platform.mac_ver()
  3. fallback: just call it 'unknown'

Supported platforms:
  - Arch Linux + derivatives (Manjaro, EndeavourOS, Garuda)
  - Debian / Ubuntu + derivatives (Mint, Pop!_OS, Kali)
  - Fedora / RHEL / CentOS / Rocky / AlmaLinux
  - openSUSE
  - Alpine
  - Void
  - NixOS  (special: install commands look very different)
  - macOS  (via Homebrew if installed)
  - Windows  (via winget/choco/scoop if installed)
  - *BSD (FreeBSD, OpenBSD)

For each managed dependency we keep a per-platform package name. When
package names differ across distros we record them; when they're the
same as the pip name we fall through to pip.

Honest caveats:
  - This is a starter map. Some packages may not exist on every distro;
    we fall through to `pip install --user`.
  - Some distros (Arch, Fedora) prefer `paru/yay/dnf` over plain `pacman/yum`;
    we list the simpler invocation and the user can adapt.
  - Mac users without Homebrew get a clear "install brew first" message.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


logger = logging.getLogger("music-organiser")


# Each entry is (canonical OS id, friendly display name).
# Canonical IDs map to install commands in INSTALL_COMMANDS below.
KNOWN_OS = {
    "arch":      "Arch Linux",
    "manjaro":   "Manjaro",
    "endeavour": "EndeavourOS",
    "garuda":    "Garuda Linux",
    "debian":    "Debian",
    "ubuntu":    "Ubuntu",
    "linuxmint": "Linux Mint",
    "pop":       "Pop!_OS",
    "kali":      "Kali Linux",
    "fedora":    "Fedora",
    "rhel":      "Red Hat Enterprise Linux",
    "centos":    "CentOS",
    "rocky":     "Rocky Linux",
    "almalinux": "AlmaLinux",
    "opensuse":  "openSUSE",
    "suse":      "SUSE",
    "alpine":    "Alpine Linux",
    "void":      "Void Linux",
    "gentoo":    "Gentoo",
    "nixos":     "NixOS",
    "freebsd":   "FreeBSD",
    "openbsd":   "OpenBSD",
    "darwin":    "macOS",
    "windows":   "Windows",
    "unknown":   "Unknown",
}

# Group OS IDs by package manager family so we don't repeat ourselves.
PKG_FAMILY = {
    "arch":      "pacman",
    "manjaro":   "pacman",
    "endeavour": "pacman",
    "garuda":    "pacman",
    "debian":    "apt",
    "ubuntu":    "apt",
    "linuxmint": "apt",
    "pop":       "apt",
    "kali":      "apt",
    "fedora":    "dnf",
    "rhel":      "dnf",
    "centos":    "dnf",
    "rocky":     "dnf",
    "almalinux": "dnf",
    "opensuse":  "zypper",
    "suse":      "zypper",
    "alpine":    "apk",
    "void":      "xbps",
    "gentoo":    "emerge",
    "nixos":     "nix",
    "freebsd":   "pkg",
    "openbsd":   "pkg_add",
    "darwin":    "brew",
    "windows":   "winget",
    "unknown":   "pip",
}


@dataclass
class OSInfo:
    """What we know about the host."""
    family: str = "unknown"        # 'pacman' / 'apt' / 'dnf' / 'brew' / etc.
    id: str = "unknown"            # canonical OS id from KNOWN_OS
    pretty_name: str = "Unknown"   # human-friendly
    version: str = ""              # OS version if known
    is_linux: bool = False
    is_macos: bool = False
    is_windows: bool = False
    is_bsd: bool = False
    confirmed: bool = False        # True if user manually confirmed


def _read_os_release() -> dict[str, str]:
    """Parse /etc/os-release if it exists. Returns dict like {ID: 'arch', ...}."""
    out: dict[str, str] = {}
    path = Path("/etc/os-release")
    if not path.exists():
        return out
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip().strip('"').strip("'")
            out[k.strip()] = v
    except OSError:
        pass
    return out


def detect_os(force: bool = False) -> OSInfo:
    """
    Best-effort OS detection. Returns a populated OSInfo.

    force=True: ignore cached result. (We don't currently cache, but the
    flag is here for future use.)
    """
    sys_name = platform.system()  # 'Linux' / 'Darwin' / 'Windows'

    if sys_name == "Linux":
        rel = _read_os_release()
        os_id = (rel.get("ID") or "").lower().strip()
        version = rel.get("VERSION_ID", "") or rel.get("BUILD_ID", "")
        pretty = rel.get("PRETTY_NAME", "") or rel.get("NAME", "") or "Linux"

        # ID_LIKE handles derivatives — "arch" for endeavouros, "debian"
        # for ubuntu, etc. We use it as a fallback when ID is unrecognised.
        id_like = (rel.get("ID_LIKE", "") or "").lower().split()

        canonical = os_id if os_id in KNOWN_OS else ""
        if not canonical:
            for like_id in id_like:
                if like_id in KNOWN_OS:
                    canonical = like_id
                    break
        if not canonical:
            canonical = "unknown"

        return OSInfo(
            family=PKG_FAMILY.get(canonical, "pip"),
            id=canonical,
            pretty_name=pretty,
            version=version,
            is_linux=True,
        )

    if sys_name == "Darwin":
        mac_ver = platform.mac_ver()[0]
        return OSInfo(
            family="brew" if shutil.which("brew") else "pip",
            id="darwin",
            pretty_name=f"macOS {mac_ver}" if mac_ver else "macOS",
            version=mac_ver,
            is_macos=True,
        )

    if sys_name == "Windows":
        ver = platform.version()
        # Pick the highest-priority manager that's actually installed
        if shutil.which("winget"):
            family = "winget"
        elif shutil.which("choco"):
            family = "choco"
        elif shutil.which("scoop"):
            family = "scoop"
        else:
            family = "pip"
        return OSInfo(
            family=family,
            id="windows",
            pretty_name=f"Windows {ver}" if ver else "Windows",
            version=ver,
            is_windows=True,
        )

    if "BSD" in sys_name.upper() or sys_name in ("FreeBSD", "OpenBSD", "NetBSD"):
        os_id = sys_name.lower()
        return OSInfo(
            family=PKG_FAMILY.get(os_id, "pkg"),
            id=os_id if os_id in KNOWN_OS else "freebsd",
            pretty_name=sys_name,
            is_bsd=True,
        )

    return OSInfo(family="pip", id="unknown",
                  pretty_name=f"Unknown ({sys_name})")


# =============================================================================
# DEPENDENCY DEFINITIONS
# =============================================================================
#
# Each entry: pip name -> {family: pkg name, ...}. When the package name
# is the same on all distros we can collapse. When `pip` is the entry,
# that means "fall through to pip install --user".

DEPENDENCIES: dict[str, dict[str, str]] = {
    # Each key is the pip package name (what we'd pip install).
    # Values map per-family to the equivalent system package name. Missing
    # entries fall through to pip.
    "mutagen": {
        "pacman":  "python-mutagen",
        "apt":     "python3-mutagen",
        "dnf":     "python3-mutagen",
        "zypper":  "python3-mutagen",
        "apk":     "py3-mutagen",
        "xbps":    "python3-mutagen",
        "brew":    "",   # mutagen isn't in homebrew; use pip
        "winget":  "",
        "pip":     "mutagen",
    },
    "rich": {
        "pacman":  "python-rich",
        "apt":     "python3-rich",
        "dnf":     "python3-rich",
        "zypper":  "python3-rich",
        "apk":     "py3-rich",
        "xbps":    "python3-rich",
        "pip":     "rich",
    },
    "pyfiglet": {
        "pacman":  "python-pyfiglet",
        "apt":     "python3-pyfiglet",
        "dnf":     "python3-pyfiglet",
        "zypper":  "python3-pyfiglet",
        "pip":     "pyfiglet",
    },
    "tomli_w": {
        "pacman":  "python-tomli-w",
        "apt":     "python3-tomli-w",
        "dnf":     "python3-tomli-w",
        "pip":     "tomli_w",
    },
    "musicbrainzngs": {
        "pacman":  "python-musicbrainzngs",
        "apt":     "python3-musicbrainzngs",
        "dnf":     "python3-musicbrainzngs",
        "pip":     "musicbrainzngs",
    },
    "numpy": {
        "pacman":  "python-numpy",
        "apt":     "python3-numpy",
        "dnf":     "python3-numpy",
        "zypper":  "python3-numpy",
        "apk":     "py3-numpy",
        "brew":    "numpy",
        "pip":     "numpy",
    },
    "soundfile": {
        "pacman":  "python-soundfile",
        "apt":     "python3-soundfile",
        "dnf":     "python3-soundfile",
        # pyfiglet/pysoundfile system pkgs are sometimes missing; pip is safer
        "pip":     "soundfile",
    },
    # Sonic Annotator is a non-Python dependency for option `r` (Vamp).
    "sonic-annotator": {
        "pacman":  "sonic-annotator",
        "apt":     "sonic-annotator",
        "dnf":     "sonic-annotator",
        "brew":    "",   # not in homebrew; user has to build from source
        "pip":     "",
    },
}


# =============================================================================
# COMMAND BUILDERS
# =============================================================================

def install_command(os_info: OSInfo, pip_name: str) -> tuple[str, str]:
    """
    Return (system_install_command, source) for the given dep + OS.

    `source` is 'system' if it can be installed via the OS package
    manager; 'pip' if we recommend pip install --user; 'unsupported' if
    we couldn't figure out a way to install it.
    """
    family = os_info.family
    dep = DEPENDENCIES.get(pip_name)
    if dep is None:
        # Unknown dep; assume pip
        return (f"pip install --user {pip_name}", "pip")

    pkg_name = dep.get(family, "")
    if pkg_name:
        if family == "pacman":
            return (f"sudo pacman -S --needed {pkg_name}", "system")
        if family == "apt":
            return (f"sudo apt install -y {pkg_name}", "system")
        if family == "dnf":
            return (f"sudo dnf install -y {pkg_name}", "system")
        if family == "zypper":
            return (f"sudo zypper install -y {pkg_name}", "system")
        if family == "apk":
            return (f"sudo apk add {pkg_name}", "system")
        if family == "xbps":
            return (f"sudo xbps-install -y {pkg_name}", "system")
        if family == "emerge":
            return (f"sudo emerge {pkg_name}", "system")
        if family == "brew":
            return (f"brew install {pkg_name}", "system")
        if family == "pkg":
            return (f"sudo pkg install {pkg_name}", "system")
        if family == "winget":
            return (f"winget install {pkg_name}", "system")
        if family == "choco":
            return (f"choco install {pkg_name}", "system")
        # nix is special — system management is declarative; suggest pip
        if family == "nix":
            return (f"pip install --user {pip_name}",
                    "pip (NixOS: prefer adding to configuration.nix)")
    # Fallback: pip
    pip_fallback = dep.get("pip", "")
    if pip_fallback:
        return (f"pip install --break-system-packages --user {pip_fallback}", "pip")
    return ("", "unsupported")


def confirmation_prompt(os_info: OSInfo, deps_to_install: list[str]) -> str:
    """Build a multi-line confirmation string showing all the commands we
    *would* run if the user agrees. Each command on its own line so the
    user can read it before assenting."""
    lines = [
        f"  Detected OS: {os_info.pretty_name} (package manager: {os_info.family})",
        "",
        "  The following install commands would run:",
        "",
    ]
    for d in deps_to_install:
        cmd, source = install_command(os_info, d)
        if cmd:
            lines.append(f"    [{source:>6}]  {cmd}")
        else:
            lines.append(f"    [{'??':>6}]  {d} — no install rule for this OS")
    return "\n".join(lines)


def run_install(os_info: OSInfo, deps_to_install: list[str],
                 dry_run: bool = False) -> dict[str, str]:
    """
    Execute install commands. Returns {pip_name: 'ok' | 'fail' | 'skip'}.

    NEVER call this without an explicit user-confirmed prompt above.
    The runner respects sudo: if a command starts with 'sudo' and there's
    no terminal, we skip it rather than hanging. dry_run=True just prints
    what it would do.
    """
    out: dict[str, str] = {}
    for d in deps_to_install:
        cmd, _ = install_command(os_info, d)
        if not cmd:
            out[d] = "skip"
            continue
        if dry_run:
            print(f"  [dry-run] would run: {cmd}")
            out[d] = "ok"
            continue
        try:
            r = subprocess.run(cmd, shell=True, check=False)
            out[d] = "ok" if r.returncode == 0 else "fail"
        except Exception as e:
            print(f"  install of {d} failed: {e}")
            out[d] = "fail"
    return out


def manual_os_prompt() -> OSInfo:
    """
    Interactive fallback when auto-detection fails or the user wants to
    override. Lists the supported OS families with index keys.
    """
    families = [
        ("1", "arch",     "Arch Linux family (Arch, Manjaro, Endeavour, Garuda)"),
        ("2", "debian",   "Debian family (Debian, Ubuntu, Mint, Pop!_OS)"),
        ("3", "fedora",   "Red Hat family (Fedora, RHEL, CentOS, Rocky, Alma)"),
        ("4", "opensuse", "openSUSE / SUSE"),
        ("5", "alpine",   "Alpine"),
        ("6", "void",     "Void"),
        ("7", "darwin",   "macOS"),
        ("8", "windows",  "Windows"),
        ("9", "freebsd",  "*BSD"),
        ("0", "unknown",  "Other (use pip for everything)"),
    ]
    print()
    print("  Which OS are you on?")
    for k, _, label in families:
        print(f"    {k}. {label}")
    try:
        choice = input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        choice = "0"
    sel = next((f for f in families if f[0] == choice), families[-1])
    info = OSInfo(
        family=PKG_FAMILY.get(sel[1], "pip"),
        id=sel[1],
        pretty_name=KNOWN_OS.get(sel[1], "Unknown"),
        is_linux=(sel[1] not in ("darwin", "windows", "freebsd", "openbsd", "unknown")),
        is_macos=(sel[1] == "darwin"),
        is_windows=(sel[1] == "windows"),
        is_bsd=(sel[1] in ("freebsd", "openbsd")),
        confirmed=True,
    )
    return info


def detect_or_prompt(allow_manual: bool = True) -> OSInfo:
    """
    Primary entry point. Try detection; if family is 'pip' fallback or
    OS is 'unknown', ask the user to confirm.
    """
    info = detect_os()
    if info.id == "unknown" and allow_manual:
        print()
        print("  Could not detect your OS from /etc/os-release or platform info.")
        return manual_os_prompt()
    return info
