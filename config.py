from __future__ import annotations

import getpass
import json
import os
import tempfile
import ctypes
from dataclasses import asdict, dataclass
from pathlib import Path


APP_NAME = "Vellus"
VERSION = "0.1.0"
# The name this app's data and saved sign-in were stored under before the rename.
LEGACY_APP_NAME = "Accessible iCloud Explorer"
_LOCAL_APP_DATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
APP_DIR = _LOCAL_APP_DATA / APP_NAME
LEGACY_APP_DIR = _LOCAL_APP_DATA / LEGACY_APP_NAME
CONFIG_PATH = APP_DIR / "settings.json"
CACHE_DIR = APP_DIR / "cache"
SESSION_DIR = APP_DIR / "session"
# Last-known folder listings, shown at once while iCloud is asked afresh.
LISTINGS_PATH = APP_DIR / "listings.json"
# Profile of the embedded browser used for signing in on Apple's page.
BROWSER_DIR = APP_DIR / "browser"
LOG_PATH = APP_DIR / "vellus.log"
PENDING_SUFFIX = ".icloudpending"


def adopt_temp_session(source: Path | None = None, target: Path | None = None) -> int:
    """Move the saved Apple session out of the Windows temp folder.

    pyicloud keeps it there by default, where disk clean-up can delete it and
    force a full password sign-in. Returns how many files were moved.
    """
    source = source or Path(tempfile.gettempdir()) / "pyicloud" / getpass.getuser()
    target = target or SESSION_DIR
    if not source.is_dir():
        return 0
    target.mkdir(parents=True, exist_ok=True)
    moved = 0
    for path in list(source.glob("*.cookiejar")) + list(source.glob("*.session")):
        destination = target / path.name
        try:
            if not destination.exists():
                destination.write_bytes(path.read_bytes())
                moved += 1
            path.unlink()
        except OSError:
            pass
    return moved


def migrate_legacy_data(old: Path | None = None, new: Path | None = None) -> bool:
    """Carry settings, cache and waiting uploads over from the app's former name.

    Returns True when data was moved. Does nothing once the new folder exists.
    """
    old, new = old or LEGACY_APP_DIR, new or APP_DIR
    if new.exists() or not old.is_dir():
        return False
    try:
        old.rename(new)
    except OSError:
        # The old folder is in use (for example by a copy of the old app).
        # Settings are the part worth having; leave everything else in place.
        new.mkdir(parents=True, exist_ok=True)
        try:
            (new / "settings.json").write_bytes((old / "settings.json").read_bytes())
        except OSError:
            pass
        return False
    old_log = new / "icloud-explorer.log"
    if old_log.exists() and not (new / "vellus.log").exists():
        old_log.rename(new / "vellus.log")
    return True


@dataclass
class Settings:
    username: str = ""
    drive_letter: str = "I"
    mount_automatically: bool = True
    writable_drive: bool = True
    close_to_tray: bool = True

    @property
    def mountpoint(self) -> str:
        letter = (self.drive_letter or "I").strip().upper()[:1]
        return f"{letter}:"


def load_settings() -> Settings:
    try:
        values = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return Settings()
    allowed = Settings.__dataclass_fields__.keys()
    return Settings(**{key: value for key, value in values.items() if key in allowed})


def save_settings(settings: Settings) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
    temporary.replace(CONFIG_PATH)


def pending_files(cache_root: Path | None = None) -> list[Path]:
    """Cached files holding changes that have not reached iCloud yet."""
    cache_root = cache_root or CACHE_DIR
    if not cache_root.exists():
        return []
    return [marker.with_name(marker.name[:-len(PENDING_SUFFIX)])
            for marker in cache_root.rglob("*" + PENDING_SUFFIX)]


def clear_cache() -> int:
    """Empty the cache, keeping changes not yet uploaded. Returns how many were kept."""
    LISTINGS_PATH.unlink(missing_ok=True)
    if not CACHE_DIR.exists():
        return 0
    pending = pending_files()
    keep = set(pending) | {path.with_name(path.name + PENDING_SUFFIX) for path in pending}
    for path in sorted(CACHE_DIR.rglob("*"), reverse=True):
        if path in keep:
            continue
        if path.is_file() or path.is_symlink():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass
    return len(pending)


def used_drive_letters() -> set[str]:
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    return {chr(ord("A") + index) for index in range(26) if mask & (1 << index)}


def available_drive_letters() -> list[str]:
    used = used_drive_letters()
    return [letter for letter in "DEFGHIJKLMNOPQRSTUVWXYZ" if letter not in used]


def drive_letter_in_use(letter: str) -> bool:
    return letter.strip().upper()[:1] in used_drive_letters()
