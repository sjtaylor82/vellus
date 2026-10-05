from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import logging
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import threading
import time
from typing import Callable, Iterable
from uuid import uuid4

from pyicloud import PyiCloudService
from pyicloud.exceptions import PyiCloudAPIResponseException
from requests.cookies import create_cookie

from config import APP_DIR, APP_NAME, LISTINGS_PATH, SESSION_DIR, adopt_temp_session
from governor import ICloudPaused, RequestGovernor


# Names under which earlier versions saved the Apple Account password in
# Windows Credential Manager. Vellus no longer handles passwords at all; these
# are kept only so that old entries can be cleaned up.
SERVICE_NAME = "Vellus"
LEGACY_SERVICE_NAME = "Accessible iCloud Explorer"
LOGGER = logging.getLogger(__name__)


def forget_all_passwords(services: tuple[str, ...] = (SERVICE_NAME, LEGACY_SERVICE_NAME)) -> int:
    """Remove every password an earlier version saved, whichever account it was for.

    They were filed under the service name and, once a second one was saved,
    under "<account>@<service>" as well.
    """
    try:
        from win32ctypes.pywin32 import pywintypes, win32cred
    except ImportError:
        return 0
    try:
        saved = [credential["TargetName"] for credential in win32cred.CredEnumerate() or []]
    except pywintypes.error:
        saved = []
    removed = 0
    for target in saved:
        if any(target == service or target.endswith(f"@{service}") for service in services):
            try:
                win32cred.CredDelete(Type=win32cred.CRED_TYPE_GENERIC, TargetName=target)
                removed += 1
            except pywintypes.error:
                pass
    return removed


def remove_user_data() -> None:
    """Remove everything Vellus keeps on this PC. Used when uninstalling."""
    forget_all_passwords()
    for folder in (APP_DIR, Path(tempfile.gettempdir()) / APP_NAME):
        shutil.rmtree(folder, ignore_errors=True)


class SignInRequired(RuntimeError):
    """There is no usable saved session; the user has to sign in."""


@dataclass(frozen=True)
class Entry:
    name: str
    path: str
    kind: str
    size: int | None
    modified: object | None


def join_remote(parent: str, name: str) -> str:
    path = PurePosixPath(parent) / name
    return "/" if str(path) == "." else "/" + str(path).lstrip("/")


def parent_of(remote_path: str) -> str:
    value = str(PurePosixPath(remote_path).parent)
    return "/" if value == "." else value


def entry_key(entry: Entry) -> dict:
    """Identify one remote version of a file, for cache and conflict checks."""
    return {"size": int(entry.size or 0), "modified": str(entry.modified or "")}


def conflict_name(name: str) -> str:
    path = PurePosixPath(name)
    stamp = datetime.now().strftime("%Y-%m-%d %H%M%S")
    return f"{path.stem} (conflict from this PC {stamp}){path.suffix}"


class _NamedUpload:
    """Present a local file to pyicloud under a chosen remote name.

    pyicloud names the uploaded file after ``file_object.name``, which for an
    ordinary open file is its full local path.
    """

    def __init__(self, stream, name: str) -> None:
        self._stream = stream
        self.name = name

    def __getattr__(self, attribute):
        return getattr(self._stream, attribute)


class ICloudBackend:
    PCS_REFRESH_SECONDS = 600

    def __init__(self) -> None:
        self.api = None
        self.username = ""
        self._list_cache: dict[str, tuple[float, list[Entry]]] = {}
        self._cache_lock = threading.RLock()
        self._list_locks: dict[str, threading.Lock] = {}
        self._refresh_paths: set[str] = set()
        # Called with (message, needs_sign_in) when requests to Apple are paused.
        self.on_pause: Callable[[str, bool], None] | None = None
        # Called with a folder path when a listing shown from the last-known
        # copy has been checked against iCloud.
        self.on_listing_updated: Callable[[str], None] | None = None
        self._saved: dict | None = None
        self._revalidating: set[str] = set()
        self.governor = RequestGovernor(
            lambda message, needs_sign_in: self.on_pause and self.on_pause(message, needs_sign_in))
        self._drive_lock = threading.Lock()
        self._pcs_checked = float("-inf")
        self._pcs_refreshing = False

    def connect(self, username: str, cookies: list[dict] | None = None,
                user_agent: str | None = None) -> None:
        """Start an iCloud session.

        ``cookies`` and the browser's ``user_agent`` come from a sign-in on
        Apple's own page. Without them, the previously saved session is resumed.
        Vellus never sees or stores the account password.
        """
        started = time.perf_counter()
        self.username = username.strip()
        self._saved = None
        # Keep the Apple session somewhere durable; an earlier version left it
        # in the temp folder, where a clean-up could delete it.
        adopt_temp_session()
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        api = PyiCloudService(self.username, None, cookie_directory=str(SESSION_DIR),
                              authenticate=False)
        self.governor.reset()
        self.governor.install(api.session)

        # The session is used with the identity of the browser that made it.
        agent_file = SESSION_DIR / "user-agent.txt"
        if cookies:
            agent_file.write_text(user_agent or "", "utf-8")
            for cookie in cookies:
                expires = cookie.get("expires") or -1
                api.session.cookies.set_cookie(create_cookie(
                    cookie["name"], cookie["value"], domain=cookie["domain"],
                    path=cookie.get("path", "/"), secure=bool(cookie.get("secure")),
                    expires=int(expires) if expires > 0 else None))
            # Tokens left by an earlier password sign-in belong to a session
            # this one replaces.
            for key in ("session_token", "trust_token"):
                api.session.data.pop(key, None)
        try:
            saved_agent = agent_file.read_text("utf-8").strip()
        except OSError:
            saved_agent = ""
        if saved_agent:
            api.session.headers["User-Agent"] = saved_agent

        # The session lasts as long as Apple's page made it last: about 30 days
        # if "Keep me signed in" was ticked there, about half an hour if not.
        try:
            api.data = api._validate_token()
            api._update_state()
        except PyiCloudAPIResponseException:
            if cookies:
                raise RuntimeError(
                    "Apple signed you in on its page, but that session could not be passed "
                    "to Vellus. Please try signing in again.") from None
            LOGGER.info("The saved iCloud session is no longer valid")
            raise SignInRequired("Sign in to iCloud to continue.") from None
        self.api = api
        LOGGER.info("Connected account %s in %.2fs", self._masked_username(), time.perf_counter() - started)

    def _masked_username(self) -> str:
        if "@" not in self.username:
            return "<configured account>"
        local, domain = self.username.split("@", 1)
        return f"{local[:2]}***@{domain}"

    def disconnect(self) -> None:
        self.api = None
        self.invalidate()
        LOGGER.info("Disconnected")

    def sign_out(self) -> None:
        """End the iCloud session and remove the saved sign-in from this PC."""
        if self.api is not None:
            try:
                self.api.logout()
            except Exception:
                LOGGER.warning("Apple did not confirm the sign-out", exc_info=True)
        forget_all_passwords()
        shutil.rmtree(SESSION_DIR, ignore_errors=True)
        LISTINGS_PATH.unlink(missing_ok=True)
        self._saved = None
        self.disconnect()
        self.username = ""
        LOGGER.info("Signed out and removed the saved session")

    @property
    def connected(self) -> bool:
        return self.api is not None


    def _refresh_pcs(self, api) -> None:
        try:
            api._request_pcs_for_service("iclouddrive")
        except Exception:
            LOGGER.info("Could not refresh iCloud Drive web access", exc_info=True)
        finally:
            self._pcs_checked = time.monotonic()
            self._pcs_refreshing = False

    def _drive_service(self):
        """Return pyicloud's Drive service without re-confirming access every time.

        pyicloud's ``api.drive`` asks Apple to confirm web access to the account
        on every use, which would add a request to every file operation. Here
        that confirmation blocks only the first use; afterwards it is repeated
        in the background every few minutes.
        """
        api = self.api
        if api is None:
            raise RuntimeError("Not connected")
        with self._drive_lock:
            cached = getattr(api, "_drive", None)
            if cached is None:
                cached = api.drive
                self._pcs_checked = time.monotonic()
            elif (time.monotonic() - self._pcs_checked > self.PCS_REFRESH_SECONDS
                    and not self._pcs_refreshing):
                self._pcs_refreshing = True
                threading.Thread(target=self._refresh_pcs, args=(api,), daemon=True).start()
        return cached

    def _node(self, remote_path: str):
        node, current = self._drive_service(), "/"
        for part in PurePosixPath(remote_path).parts:
            if part in ("/", ""):
                continue
            wanted = part.casefold()
            # pyicloud caches each folder's children. Bypass that cache for a
            # folder this app has changed, and once more before giving up.
            with self._cache_lock:
                stale = current in self._refresh_paths
            match = next((child for child in node.get_children(force=stale)
                          if child.name.casefold() == wanted), None)
            if match is None and not stale:
                match = next((child for child in node.get_children(force=True)
                              if child.name.casefold() == wanted), None)
            if match is None:
                raise KeyError(f"No child named '{part}' exists")
            node, current = match, join_remote(current, part)
        return node

    def find(self, remote_path: str) -> Entry | None:
        """Look up one item from a fresh listing of its parent folder."""
        name = PurePosixPath(remote_path).name.casefold()
        try:
            entries = self.list(parent_of(remote_path), force=True)
        except (KeyError, NotADirectoryError):
            return None
        return next((entry for entry in entries if entry.name.casefold() == name), None)

    def invalidate(self, remote_path: str | None = None) -> None:
        with self._cache_lock:
            if remote_path is None:
                self._list_cache.clear()
                self._refresh_paths.clear()
            else:
                prefix = remote_path.rstrip("/") + "/"
                for path in list(self._list_cache):
                    if path == remote_path or path.startswith(prefix):
                        self._list_cache.pop(path, None)
                self._refresh_paths.add(remote_path)

    @staticmethod
    def _entry_from(child, parent_path: str) -> Entry:
        # pyicloud treats app_library and other non-file containers as
        # directories. Mirroring that rule makes app folders navigable.
        kind = "file" if child.type == "file" else "folder"
        return Entry(child.name, join_remote(parent_path, child.name), kind,
                     getattr(child, "size", None),
                     getattr(child, "date_modified", getattr(child, "modified", None)))

    @staticmethod
    def _sorted(entries) -> list[Entry]:
        return sorted(entries, key=lambda item: (item.kind != "folder", item.name.casefold()))

    # Apple can take about twenty seconds to answer a folder listing requested
    # straight after a change to that folder. So after a change the listing is
    # not fetched again: Apple's reply to the change already says what the
    # folder now holds, and the cached listing is updated from it, as Apple's
    # own web app does. Anything unexpected falls back to a fresh listing.

    def _folder(self, remote_path: str):
        """The pyicloud node for a folder (for "/", the root node itself)."""
        node = self._node(remote_path)
        return getattr(node, "root", node)

    def _update_listing(self, parent_path: str, add: Entry | None = None,
                        remove: str | None = None) -> None:
        with self._cache_lock:
            cached = self._list_cache.get(parent_path)
            if cached is None:
                return
            gone = {name.casefold() for name in (remove, add.name if add else None) if name}
            entries = [entry for entry in cached[1] if entry.name.casefold() not in gone]
            if add is not None:
                entries.append(add)
            entries = self._sorted(entries)
            self._list_cache[parent_path] = (time.monotonic(), entries)
            self._remember_listing(parent_path, entries)

    def _forget_listings_under(self, remote_path: str) -> None:
        prefix = remote_path.rstrip("/") + "/"
        with self._cache_lock:
            for path in list(self._list_cache):
                if path == remote_path or path.startswith(prefix):
                    self._list_cache.pop(path, None)
            saved = self._saved_listings()
            stale = [path for path in saved if path == remote_path or path.startswith(prefix)]
            for path in stale:
                del saved[path]
            if stale:
                self._write_saved()

    # Apple sometimes takes about twenty seconds to list a folder, most often
    # the first time after starting. The last listing seen is therefore kept on
    # disk, shown at once the next time, and replaced when the fresh one arrives.

    def _saved_listings(self) -> dict:
        if self._saved is None:
            try:
                raw = json.loads(LISTINGS_PATH.read_text("utf-8"))
                folders = raw["folders"] if raw.get("account") == self.username else {}
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                folders = {}
            self._saved = folders if isinstance(folders, dict) else {}
        return self._saved

    def _saved_listing(self, remote_path: str) -> list[Entry] | None:
        rows = self._saved_listings().get(remote_path)
        if rows is None:
            return None
        try:
            return [Entry(name, join_remote(remote_path, name), kind, size,
                          datetime.fromisoformat(modified) if modified else None)
                    for name, kind, size, modified in rows]
        except (ValueError, TypeError):
            return None

    def _remember_listing(self, remote_path: str, entries: list[Entry]) -> None:
        with self._cache_lock:
            saved = self._saved_listings()
            saved.pop(remote_path, None)
            saved[remote_path] = [
                [entry.name, entry.kind, entry.size,
                 entry.modified.isoformat() if isinstance(entry.modified, datetime) else None]
                for entry in entries]
            while len(saved) > 300:
                del saved[next(iter(saved))]
            self._write_saved()

    def _write_saved(self) -> None:
        try:
            LISTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary = LISTINGS_PATH.with_suffix(".tmp")
            temporary.write_text(
                json.dumps({"account": self.username, "folders": self._saved}), "utf-8")
            temporary.replace(LISTINGS_PATH)
        except OSError:
            LOGGER.warning("Could not save the folder listing", exc_info=True)

    def is_checking(self, remote_path: str) -> bool:
        """True while a last-known listing is being checked against iCloud."""
        with self._cache_lock:
            return remote_path in self._revalidating

    def _revalidate(self, remote_path: str) -> None:
        with self._cache_lock:
            if remote_path in self._revalidating:
                return
            self._revalidating.add(remote_path)

        def work():
            try:
                self.list(remote_path)
            except KeyError:
                # The folder is gone; stop offering its old contents.
                self._forget_listings_under(remote_path)
            except Exception:
                LOGGER.info("Could not check %s against iCloud", remote_path, exc_info=True)
            finally:
                with self._cache_lock:
                    self._revalidating.discard(remote_path)
                if self.on_listing_updated:
                    self.on_listing_updated(remote_path)
        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _attach(folder, node) -> None:
        """Add a node to a folder's cached children, if the folder has been listed."""
        if getattr(folder, "_children", None) is not None:
            folder._children.append(node)
            items = folder.data.get("items")
            if isinstance(items, list) and node.data not in items:
                items.append(node.data)

    def list(self, remote_path: str, force: bool = False,
             stale_ok: bool = False) -> list[Entry]:
        """List a folder. With ``stale_ok``, a folder not yet listed in this run is
        answered at once from the last-known copy while iCloud is asked afresh;
        ``on_listing_updated`` is called when that check finishes."""
        with self._cache_lock:
            cached = self._list_cache.get(remote_path)
            refresh = force or remote_path in self._refresh_paths
            if cached and not refresh and time.monotonic() - cached[0] < 30:
                return cached[1]
            if stale_ok and not refresh and cached is None:
                saved = self._saved_listing(remote_path)
                if saved is not None:
                    self._revalidate(remote_path)
                    return saved
            path_lock = self._list_locks.setdefault(remote_path, threading.Lock())
        with path_lock:
            with self._cache_lock:
                cached = self._list_cache.get(remote_path)
                refresh = force or remote_path in self._refresh_paths
                if cached and not refresh and time.monotonic() - cached[0] < 30:
                    return cached[1]
            started = time.perf_counter()
            try:
                folder = self._node(remote_path)
                children = folder.get_children(force=refresh)
            except ICloudPaused:
                # While requests are paused, an older listing beats an error.
                if cached:
                    return cached[1]
                raise
            result = self._sorted(self._entry_from(child, remote_path) for child in children)
            with self._cache_lock:
                self._list_cache[remote_path] = (time.monotonic(), result)
                self._refresh_paths.discard(remote_path)
                self._remember_listing(remote_path, result)
            LOGGER.info("Listed %s: %d items in %.2fs%s", remote_path, len(result),
                        time.perf_counter() - started, " (forced)" if force else "")
            return result

    def download(self, remote_path: str, destination: Path,
                 progress: Callable[[int], None] | None = None) -> Path:
        LOGGER.info("Downloading %s", remote_path)
        node = self._node(remote_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        received = 0
        with node.open(stream=True) as response, destination.open("wb") as output:
            response.raise_for_status()
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    output.write(chunk)
                    received += len(chunk)
                    if progress:
                        progress(received)
        return destination

    def read_range(self, remote_path: str, start: int, length: int) -> tuple[bytes, bool]:
        """Read a byte range. The bool is true when Apple honored the range."""
        if length <= 0:
            return b"", True
        end = start + length - 1
        LOGGER.info("Reading range %s bytes=%d-%d", remote_path, start, end)
        node = self._node(remote_path)
        with node.open(stream=True, headers={"Range": f"bytes={start}-{end}"}) as response:
            response.raise_for_status()
            data = response.content
            partial = response.status_code == 206
        LOGGER.info("Received %d bytes for %s (partial=%s)", len(data), remote_path, partial)
        return data, partial

    def download_tree(self, remote_path: str, destination: Path,
                      status: Callable[[str], None] | None = None) -> None:
        node = self._node(remote_path)
        if node.type == "file":
            self.download(remote_path, destination)
            return
        destination.mkdir(parents=True, exist_ok=True)
        for name in node.dir():
            child_path = join_remote(remote_path, name)
            if status:
                status(child_path)
            self.download_tree(child_path, destination / name, status)

    @staticmethod
    def _note_fallback(operation: str, response, exc: Exception) -> None:
        """Record the shape (field names only) of a reply that could not be used."""
        shape = sorted(response)[:12] if isinstance(response, dict) else type(response).__name__
        first = next((value[0] for value in (response.values() if isinstance(response, dict) else [])
                      if isinstance(value, list) and value and isinstance(value[0], dict)), {})
        LOGGER.info("Apple's reply to %s was not as expected (%s: %s; fields %s; item fields %s); "
                    "the folder will be listed afresh", operation, type(exc).__name__, exc,
                    shape, sorted(first)[:20])

    @staticmethod
    def _accepted(item) -> bool:
        return bool(item.get("etag")) and item.get("status", "OK") == "OK"

    # Apple can take around fifteen seconds to confirm a move to Recently
    # Deleted. Deleting is therefore done in two steps, so that callers can show
    # the item as gone straight away: begin_delete takes it out of the cached
    # listing, finish_delete asks Apple, and puts it back if Apple refuses.

    def begin_delete(self, remote_paths: Iterable[str]) -> list[tuple]:
        pending = []
        for path in remote_paths:
            parent_path = parent_of(path)
            node = self._node(path)
            folder = self._folder(parent_path)
            entry = self._entry_from(node, parent_path)
            try:
                folder.remove(node)
            except (ValueError, KeyError, AttributeError):
                folder = None  # not in a cached listing; refresh once deleted
            else:
                self._update_listing(parent_path, remove=entry.name)
            pending.append((path, node, folder, entry))
        return pending

    def finish_delete(self, pending: list[tuple]) -> None:
        for index, (path, node, folder, entry) in enumerate(pending):
            try:
                node.move_to_trash()
            except Exception:
                # Put this item, and any not yet attempted, back in the listing.
                for path, node, folder, entry in pending[index:]:
                    if folder is not None:
                        self._attach(folder, node)
                        self._update_listing(parent_of(path), add=entry)
                raise
            if folder is None:
                self.invalidate(parent_of(path))
            self._forget_listings_under(path)
            LOGGER.info("Moved to Recently Deleted: %s", path)

    def delete(self, remote_paths: Iterable[str]) -> None:
        self.finish_delete(self.begin_delete(remote_paths))

    def rename(self, remote_path: str, new_name: str) -> None:
        parent_path = parent_of(remote_path)
        node = self._node(remote_path)
        response = node.rename(new_name)
        try:
            item = response["items"][0]
            if not self._accepted(item) or "name" not in item:
                raise KeyError("unexpected reply")
            node.data["name"], node.data["etag"] = item["name"], item["etag"]
            if "extension" in item:
                node.data["extension"] = item["extension"]
            else:
                node.data.pop("extension", None)
            if node.name.casefold() != new_name.casefold():
                raise KeyError("Apple chose a different name")
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            self._note_fallback("rename", response, exc)
            self.invalidate(parent_path)
        else:
            self._update_listing(parent_path, add=self._entry_from(node, parent_path),
                                 remove=PurePosixPath(remote_path).name)
            self._forget_listings_under(remote_path)
        LOGGER.info("Renamed %s to %s", remote_path, new_name)

    def mkdir(self, parent: str, name: str) -> None:
        folder = self._folder(parent)
        response = folder.mkdir(name)
        try:
            data = response["folders"][0]
            if (not data.get("drivewsid") or data.get("status", "OK") != "OK"
                    or str(data.get("name", "")).casefold() != name.casefold()):
                raise KeyError("unexpected reply")
            data.setdefault("type", "FOLDER")
            node = type(folder)(folder.connection, data)
            self._attach(folder, node)
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            self._note_fallback("new folder", response, exc)
            self.invalidate(parent)
        else:
            self._update_listing(parent, add=self._entry_from(node, parent))
        LOGGER.info("Created folder %s", join_remote(parent, name))

    def _upload_as(self, local_path: Path, remote_folder: str, name: str) -> None:
        with local_path.open("rb") as source:
            self._node(remote_folder).upload(_NamedUpload(source, name),
                                             mtime=local_path.stat().st_mtime)

    def upload(self, local_path: Path, remote_folder: str) -> None:
        if local_path.is_dir():
            self.mkdir(remote_folder, local_path.name)
            target = join_remote(remote_folder, local_path.name)
            for child in local_path.iterdir():
                self.upload(child, target)
        else:
            self._upload_as(local_path, remote_folder, local_path.name)
        self.invalidate(remote_folder)
        LOGGER.info("Uploaded %s to %s", local_path.name, remote_folder)

    def replace(self, local_path: Path, remote_path: str, expected: dict | None = None,
                must_be_new: bool = False) -> str:
        """Upload local_path as remote_path and return the path actually written.

        Nothing newer is ever overwritten: if the remote file no longer matches
        ``expected`` (or exists at all when ``must_be_new``), the local file is
        uploaded beside it under a conflict name instead. Otherwise the previous
        version is renamed aside for the duration of the upload and then moved
        to Recently Deleted.
        """
        parent, name = parent_of(remote_path), PurePosixPath(remote_path).name
        current = self.find(remote_path)
        if current is not None and current.kind == "folder":
            raise IsADirectoryError(remote_path)
        if current is not None and (
                must_be_new or (expected is not None and entry_key(current) != expected)):
            target = conflict_name(name)
            self._upload_as(local_path, parent, target)
            self.invalidate(parent)
            LOGGER.warning("%s changed in iCloud; uploaded local changes as %s",
                           remote_path, target)
            return join_remote(parent, target)

        backup_name = None
        if current is not None:
            backup_name = f".{name}.upload-{uuid4().hex}.bak"
            self._node(current.path).rename(backup_name)
            self.invalidate(parent)
        try:
            self._upload_as(local_path, parent, name)
        except Exception as exc:
            self.invalidate(parent)
            if backup_name:
                try:
                    self._node(join_remote(parent, backup_name)).rename(name)
                except Exception:
                    LOGGER.exception("Could not restore %s from %s", remote_path, backup_name)
                    raise RuntimeError(
                        f"Uploading {name} failed and the original could not be restored. "
                        f"It is preserved in the same iCloud folder as '{backup_name}'.") from exc
                finally:
                    self.invalidate(parent)
            raise
        self.invalidate(parent)
        if backup_name:
            try:
                self._node(join_remote(parent, backup_name)).move_to_trash()
            except Exception:
                LOGGER.exception("Uploaded %s but could not move the previous version (%s) "
                                 "to Recently Deleted", remote_path, backup_name)
            self.invalidate(parent)
        LOGGER.info("Replaced %s", remote_path)
        return remote_path

    def move(self, source_path: str, destination_folder: str) -> None:
        source_parent = parent_of(source_path)
        node = self._node(source_path)
        destination = self._folder(destination_folder)
        response = self._drive_service().move_nodes_to_node([node], destination)
        try:
            item = response["items"][0]
            if not self._accepted(item):
                raise KeyError("unexpected reply")
            self._folder(source_parent).remove(node)
            node.data["etag"] = item["etag"]
            self._attach(destination, node)
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            self._note_fallback("move", response, exc)
            self.invalidate(source_parent)
            self.invalidate(destination_folder)
        else:
            self._update_listing(source_parent, remove=PurePosixPath(source_path).name)
            self._update_listing(destination_folder,
                                 add=self._entry_from(node, destination_folder))
            self._forget_listings_under(source_path)
        LOGGER.info("Moved %s to %s", source_path, destination_folder)

    def copy(self, source_path: str, destination_folder: str) -> None:
        import tempfile
        with tempfile.TemporaryDirectory(prefix="vellus-") as tmp:
            local = Path(tmp) / PurePosixPath(source_path).name
            self.download_tree(source_path, local)
            self.upload(local, destination_folder)
