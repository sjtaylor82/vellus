from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable

from backend import Entry, ICloudBackend, entry_key, join_remote, parent_of
from config import CACHE_DIR, PENDING_SUFFIX, pending_files

LOGGER = logging.getLogger(__name__)
FILE_READ_DATA, FILE_WRITE_DATA, FILE_APPEND_DATA = 0x1, 0x2, 0x4
META_SUFFIX = ".icloudmeta"
CHUNKS_SUFFIX = ".icloudchunks"


def _load_winfspy():
    try:
        from winfspy import (BaseFileSystemOperations, CREATE_FILE_CREATE_OPTIONS,
            FILE_ATTRIBUTE, FileSystem, NTStatusAccessDenied,
            NTStatusDirectoryNotEmpty, NTStatusEndOfFile, NTStatusNotADirectory,
            NTStatusObjectNameCollision, NTStatusObjectNameNotFound)
        from winfspy.plumbing.security_descriptor import SecurityDescriptor
    except (ImportError, OSError, RuntimeError) as exc:
        raise RuntimeError("The virtual drive requires WinFsp and winfspy.") from exc
    return (BaseFileSystemOperations, CREATE_FILE_CREATE_OPTIONS, FILE_ATTRIBUTE,
        FileSystem, NTStatusAccessDenied, NTStatusDirectoryNotEmpty,
        NTStatusEndOfFile, NTStatusNotADirectory, NTStatusObjectNameCollision,
        NTStatusObjectNameNotFound, SecurityDescriptor)


def _remote_path(file_name: str) -> str:
    parts = [part for part in PureWindowsPath(file_name).parts if part not in ("\\", "/")]
    return "/" + "/".join(parts)


def _filetime(value: object | None) -> int:
    if not isinstance(value, datetime):
        value = datetime.now(timezone.utc)
    elif value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int((value.timestamp() + 11644473600) * 10_000_000)


def _sidecar(local: Path, suffix: str) -> Path:
    return local.with_name(local.name + suffix)


def read_pending(local: Path) -> dict | None:
    """Return the pending-upload record for a cached file, if it has one.

    The record marks the cached copy as the only copy of the user's changes:
    it must not be re-downloaded over, and must not be cleared, until uploaded.
    """
    try:
        return json.loads(_sidecar(local, PENDING_SUFFIX).read_text("utf-8"))
    except (OSError, ValueError):
        return None


def upload_pending(backend: ICloudBackend, local: Path, remote_path: str,
                   expected: dict | None, created: bool) -> tuple[str, Entry, Path]:
    """Send a locally changed cache file to iCloud.

    Returns the remote path written (a conflict name if the remote file had
    changed), its remote entry, and where the cached copy now lives. The
    pending record is removed only once iCloud lists the uploaded file.
    """
    written = backend.replace(local, remote_path, expected=expected, must_be_new=created)
    remote = backend.find(written)
    if remote is None:
        time.sleep(2)
        remote = backend.find(written)
    if remote is None:
        raise RuntimeError(f"{PurePosixPath(written).name} was sent to iCloud but is not "
                           "listed there yet.")
    if int(remote.size or 0) != local.stat().st_size:
        LOGGER.warning("%s is %d bytes in iCloud but %d bytes locally", written,
                       int(remote.size or 0), local.stat().st_size)
    _sidecar(local, META_SUFFIX).unlink(missing_ok=True)
    _sidecar(local, PENDING_SUFFIX).unlink(missing_ok=True)
    if written != remote_path:
        moved = local.with_name(PurePosixPath(written).name)
        local.replace(moved)
        local = moved
    _sidecar(local, META_SUFFIX).write_text(json.dumps(entry_key(remote)), "utf-8")
    return written, remote, local


def retry_pending(backend: ICloudBackend, cache_root: Path, lock: threading.RLock,
                  skip: set[Path] = frozenset()) -> tuple[int, int]:
    """Upload changes left behind by earlier failures. Returns (uploaded, failed)."""
    uploaded = failed = 0
    for local in pending_files(cache_root):
        if local in skip:
            continue
        if not local.exists():
            _sidecar(local, PENDING_SUFFIX).unlink(missing_ok=True)
            continue
        info = read_pending(local)
        if not info or "remote" not in info:
            LOGGER.error("Unreadable pending-upload record for %s", local)
            failed += 1
            continue
        with lock:
            try:
                upload_pending(backend, local, info["remote"], info.get("expected"),
                               bool(info.get("created")))
            except Exception:
                LOGGER.exception("Retry failed for %s", info["remote"])
                failed += 1
            else:
                uploaded += 1
    return uploaded, failed


@dataclass
class OpenHandle:
    entry: Entry
    local: Path | None = None
    dirty: bool = False
    created: bool = False
    # Remote version this handle's content is based on; None skips the check.
    expected: dict | None = None
    # Dirty only because an earlier upload of this file failed.
    resumed: bool = False


def create_operations(backend: ICloudBackend, cache_root: Path,
                      notify: Callable[[str, str], None] | None = None):
    """Build the file-system callbacks without registering a volume with WinFsp."""
    (BaseOperations, CreateOptions, Attr, FileSystem, AccessDenied,
     DirectoryNotEmpty, EndOfFile, NotDirectory, NameCollision, NotFound,
     SecurityDescriptor) = _load_winfspy()

    class ICloudOperations(BaseOperations):
        def __init__(self):
            super().__init__()
            self._backend = backend
            self._cache_root = cache_root
            self._mutation_lock = threading.RLock()
            self._download_locks: dict[str, threading.Lock] = {}
            self._handles: dict[int, OpenHandle] = {}
            self._background: list[threading.Thread] = []
            self._last_failure: dict[str, float] = {}
            self._security = SecurityDescriptor.from_string(
                "O:BAG:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;WD)")

        def _remote_entry(self, path: str, fresh: bool = False) -> Entry | None:
            """Find an item by path. Unless ``fresh``, a folder not yet listed in
            this run is answered from its last-known listing, so that File
            Explorer's many look-ups never wait on a slow answer from iCloud."""
            if path == "/":
                return Entry("", "/", "folder", None, None)
            name = PurePosixPath(path).name.casefold()
            try:
                for entry in self._backend.list(parent_of(path), stale_ok=not fresh):
                    if entry.name.casefold() == name:
                        return entry
            except (KeyError, NotADirectoryError):
                pass
            return None

        def _unsent_entry(self, path: str) -> Entry | None:
            """A file created on the drive that has not reached iCloud yet."""
            local = self._local_path(path)
            info = read_pending(local)
            if not info or not info.get("created") or not local.is_file():
                return None
            stat = local.stat()
            return Entry(PurePosixPath(path).name, path, "file", stat.st_size,
                         datetime.fromtimestamp(stat.st_mtime, timezone.utc))

        def _entry(self, file_name: str) -> Entry:
            path = _remote_path(file_name)
            entry = self._remote_entry(path) or self._unsent_entry(path)
            if entry is None:
                raise NotFound()
            return entry

        def _info(self, entry: Entry) -> dict:
            folder = entry.kind == "folder"
            timestamp = _filetime(entry.modified)
            attributes = int(Attr.FILE_ATTRIBUTE_DIRECTORY if folder
                             else Attr.FILE_ATTRIBUTE_ARCHIVE)
            if not folder and not self._local_path(entry.path).exists():
                # Mark files that are not on this PC yet as online-only, the way
                # cloud sync clients do, so Explorer does not download every
                # file in a folder just to draw thumbnails and previews.
                attributes |= int(Attr.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)
            size = 0 if folder else int(entry.size or 0)
            return {"file_attributes": attributes,
                "allocation_size": 0 if folder else ((size + 4095) // 4096) * 4096,
                "file_size": size, "creation_time": timestamp,
                "last_access_time": timestamp, "last_write_time": timestamp,
                "change_time": timestamp, "index_number": 0}

        def _local_path(self, remote_path: str) -> Path:
            return self._cache_root.joinpath(*PurePosixPath(remote_path).parts[1:])

        def _notify(self, title: str, message: str) -> None:
            if notify:
                try:
                    notify(title, message)
                except Exception:
                    LOGGER.exception("Could not show notification: %s", message)

        def _report_failure(self, name: str, exc: Exception) -> None:
            now = time.monotonic()
            if now - self._last_failure.get(name, -60.0) < 60:
                return
            self._last_failure[name] = now
            self._notify("iCloud upload failed",
                         f"{name} could not be uploaded ({exc}). Your change is kept on "
                         "this PC and will be retried.")

        def _local_is_authoritative(self, handle: OpenHandle, local: Path) -> bool:
            return handle.created or _sidecar(local, PENDING_SUFFIX).exists()

        def _mark_pending(self, handle: OpenHandle) -> None:
            marker = _sidecar(handle.local, PENDING_SUFFIX)
            if not marker.exists():
                if handle.resumed:
                    # The earlier change was uploaded meanwhile; this is a new one
                    # based on the version that upload produced.
                    try:
                        handle.expected = json.loads(
                            _sidecar(handle.local, META_SUFFIX).read_text("utf-8"))
                    except (OSError, ValueError):
                        handle.expected = None
                    handle.created = False
                marker.write_text(json.dumps({
                    "remote": handle.entry.path, "expected": handle.expected,
                    "created": handle.created,
                    "since": datetime.now(timezone.utc).isoformat()}), "utf-8")
            handle.resumed = False
            handle.dirty = True

        def _discard_local(self, remote_path: str) -> None:
            local = self._local_path(remote_path)
            if local.is_file():
                local.unlink(missing_ok=True)
            for suffix in (META_SUFFIX, PENDING_SUFFIX):
                _sidecar(local, suffix).unlink(missing_ok=True)
            shutil.rmtree(_sidecar(local, CHUNKS_SUFFIX), ignore_errors=True)

        def _ensure_local(self, handle: OpenHandle) -> Path:
            if handle.local is None:
                handle.local = self._local_path(handle.entry.path)
            local = handle.local
            if self._local_is_authoritative(handle, local):
                local.parent.mkdir(parents=True, exist_ok=True)
                local.touch(exist_ok=True)
                return local
            metadata = _sidecar(local, META_SUFFIX)
            cache_key = entry_key(handle.entry)
            try:
                valid = local.exists() and json.loads(metadata.read_text("utf-8")) == cache_key
            except (OSError, ValueError):
                valid = False
            if not valid:
                lock = self._download_locks.setdefault(handle.entry.path, threading.Lock())
                with lock:
                    try:
                        valid = local.exists() and json.loads(metadata.read_text("utf-8")) == cache_key
                    except (OSError, ValueError):
                        valid = False
                    if not valid:
                        self._backend.download(handle.entry.path, local)
                        metadata.write_text(json.dumps(cache_key), "utf-8")
            return local

        def _valid_complete_cache(self, handle: OpenHandle) -> bool:
            local = handle.local or self._local_path(handle.entry.path)
            handle.local = local
            if self._local_is_authoritative(handle, local):
                return local.exists()
            try:
                return (local.exists() and local.stat().st_size == int(handle.entry.size or 0)
                        and json.loads(_sidecar(local, META_SUFFIX).read_text("utf-8"))
                        == entry_key(handle.entry))
            except (OSError, ValueError):
                return False

        def _size(self, handle: OpenHandle) -> int:
            if self._valid_complete_cache(handle):
                return handle.local.stat().st_size
            return int(handle.entry.size or 0)

        def _read_range(self, handle: OpenHandle, offset: int, length: int) -> bytes:
            if self._valid_complete_cache(handle):
                with handle.local.open("rb") as stream:
                    stream.seek(offset)
                    return stream.read(length)

            local = handle.local or self._local_path(handle.entry.path)
            handle.local = local
            chunk_root = _sidecar(local, CHUNKS_SUFFIX)
            chunk_metadata = chunk_root / "metadata.json"
            cache_key = entry_key(handle.entry)
            try:
                chunks_valid = json.loads(chunk_metadata.read_text("utf-8")) == cache_key
            except (OSError, ValueError):
                chunks_valid = False
            if not chunks_valid:
                shutil.rmtree(chunk_root, ignore_errors=True)
                chunk_root.mkdir(parents=True, exist_ok=True)
                chunk_metadata.write_text(json.dumps(cache_key), "utf-8")

            block_size = 4 * 1024 * 1024
            file_size = int(handle.entry.size or 0)
            output = bytearray()
            position = offset
            requested_end = min(file_size, offset + length)
            lock = self._download_locks.setdefault(handle.entry.path, threading.Lock())
            while position < requested_end:
                block_start = (position // block_size) * block_size
                block_end = min(file_size, block_start + block_size)
                block_path = chunk_root / f"{block_start}.bin"
                with lock:
                    if not block_path.exists() or block_path.stat().st_size != block_end - block_start:
                        data, partial = self._backend.read_range(
                            handle.entry.path, block_start, block_end - block_start)
                        if not partial and len(data) == file_size:
                            local.parent.mkdir(parents=True, exist_ok=True)
                            local.write_bytes(data)
                            _sidecar(local, META_SUFFIX).write_text(
                                json.dumps(cache_key), "utf-8")
                            shutil.rmtree(chunk_root, ignore_errors=True)
                            with local.open("rb") as stream:
                                stream.seek(offset)
                                return stream.read(length)
                        block_path.write_bytes(data)
                within_block = position - block_start
                take = min(requested_end - position, block_path.stat().st_size - within_block)
                if take <= 0:
                    break
                with block_path.open("rb") as block:
                    block.seek(within_block)
                    output.extend(block.read(take))
                position += take
            return bytes(output)

        def _sync(self, handle: OpenHandle) -> None:
            if not handle.dirty or handle.entry.kind == "folder":
                return
            with self._mutation_lock:
                if not handle.dirty:
                    return
                local = self._ensure_local(handle)
                if handle.resumed and read_pending(local) is None:
                    # A retry uploaded the earlier change while this handle was open.
                    handle.dirty = handle.resumed = False
                    return
                wanted = handle.entry
                try:
                    written, remote, local = upload_pending(
                        self._backend, local, wanted.path, handle.expected, handle.created)
                except Exception as exc:
                    LOGGER.exception("Could not upload %s; the change is kept on this PC",
                                     wanted.path)
                    self._report_failure(wanted.name, exc)
                    raise
                handle.entry, handle.local = remote, local
                handle.expected = entry_key(remote)
                handle.dirty = handle.created = handle.resumed = False
                if written != wanted.path:
                    self._notify("iCloud conflict",
                                 f"{wanted.name} was changed somewhere else, so your version "
                                 f"was saved beside it as {remote.name}.")

        def wait_for_background(self) -> None:
            """Block until deletes handed to the background have finished."""
            while self._background:
                self._background.pop().join()

        def retry_pending(self) -> tuple[int, int]:
            in_use = {handle.local for handle in list(self._handles.values()) if handle.local}
            return retry_pending(self._backend, self._cache_root, self._mutation_lock, in_use)

        def get_volume_info(self):
            return {"total_size": 2**50, "free_size": 2**50, "volume_label": "iCloud Drive"}

        def get_security_by_name(self, file_name):
            entry = self._entry(file_name)
            return self._info(entry)["file_attributes"], self._security.handle, self._security.size

        def create(self, file_name, create_options, granted_access, file_attributes,
                   security_descriptor, allocation_size):
            path = _remote_path(file_name)
            try:
                self._entry(file_name)
            except NotFound:
                pass
            else:
                raise NameCollision()
            name = PurePosixPath(path).name
            if create_options & CreateOptions.FILE_DIRECTORY_FILE:
                with self._mutation_lock:
                    self._backend.mkdir(parent_of(path), name)
                handle = OpenHandle(Entry(name, path, "folder", None, None))
            else:
                # allocation_size is only a reservation hint; the file starts empty.
                local = self._local_path(path)
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(b"")
                handle = OpenHandle(Entry(name, path, "file", 0, None), local, created=True)
                self._mark_pending(handle)
            self._handles[id(handle)] = handle
            return handle

        def open(self, file_name, create_options, granted_access):
            entry = self._entry(file_name)
            # Reading or writing a file's contents needs its true size and
            # date, so that look-up is repeated against iCloud's own answer,
            # waiting for it if the one above came from a last-known listing.
            # Opens that only inspect attributes, as File Explorer's do while
            # browsing, are not held up.
            wants_data = granted_access & (FILE_READ_DATA | FILE_WRITE_DATA | FILE_APPEND_DATA)
            if entry.kind == "file" and wants_data:
                entry = (self._remote_entry(entry.path, fresh=True)
                         or self._unsent_entry(entry.path))
                if entry is None:
                    raise NotFound()
            handle = OpenHandle(entry)
            if entry.kind == "file":
                handle.expected = entry_key(entry)
                local = self._local_path(entry.path)
                info = read_pending(local)
                if info is not None and local.is_file():
                    # An earlier upload of this file failed: keep serving the local
                    # changes and try again when this handle is closed.
                    handle.local, handle.dirty, handle.resumed = local, True, True
                    handle.expected = info.get("expected")
                    handle.created = bool(info.get("created"))
                    handle.entry = Entry(entry.name, entry.path, "file",
                                         local.stat().st_size, entry.modified)
            self._handles[id(handle)] = handle
            return handle

        def close(self, file_context):
            try:
                self._sync(file_context)
            finally:
                self._handles.pop(id(file_context), None)

        def cleanup(self, file_context, file_name, flags):
            if flags & 0x01:
                path, name = file_context.entry.path, file_context.entry.name
                with self._mutation_lock:
                    # Apple can take many seconds to confirm a delete, so the
                    # item leaves the listing now and Apple is asked in the
                    # background. If Apple refuses, it reappears with a notice.
                    pending = [] if file_context.created else self._backend.begin_delete([path])
                    self._discard_local(path)
                file_context.dirty = False
                if pending:
                    def finish():
                        try:
                            self._backend.finish_delete(pending)
                        except Exception as exc:
                            LOGGER.exception("Could not delete %s", path)
                            self._notify("iCloud delete failed",
                                         f"{name} could not be deleted ({exc}). It has been "
                                         "put back.")
                    worker = threading.Thread(target=finish, daemon=True)
                    self._background.append(worker)
                    worker.start()
            else:
                self._sync(file_context)

        def flush(self, file_context):
            self._sync(file_context)
            return self._info(file_context.entry)

        def get_security(self, file_context):
            return self._security

        def set_security(self, file_context, security_information, modification_descriptor):
            return None

        def get_file_info(self, file_context):
            local = file_context.local
            # Only unsent local changes outrank what iCloud reports; an ordinary
            # cached download may be older than the remote file.
            if (file_context.entry.kind == "file" and local and local.exists()
                    and self._local_is_authoritative(file_context, local)):
                file_context.entry = Entry(file_context.entry.name, file_context.entry.path,
                    "file", file_context.local.stat().st_size, file_context.entry.modified)
            return self._info(file_context.entry)

        def set_basic_info(self, file_context, file_attributes, creation_time,
                           last_access_time, last_write_time, change_time, file_info):
            return self.get_file_info(file_context)

        def read_directory(self, file_context, marker):
            if file_context.entry.kind != "folder":
                raise NotDirectory()
            children = self._backend.list(file_context.entry.path, stale_ok=True)
            rows = [{"file_name": child.name, **self._info(child)} for child in children]
            if file_context.entry.path != "/":
                rows.extend([{"file_name": ".", **self._info(file_context.entry)},
                    {"file_name": "..", **self._info(Entry("", "/", "folder", None, None))}])
            rows.sort(key=lambda row: row["file_name"].casefold())
            if marker is None:
                return rows
            for index, row in enumerate(rows):
                if row["file_name"] == marker:
                    return rows[index + 1:]
            return []

        def get_dir_info_by_name(self, file_context, file_name):
            if file_context.entry.kind != "folder":
                raise NotDirectory()
            path = str(PureWindowsPath(file_context.entry.path.replace("/", "\\")) / file_name)
            entry = self._entry(path)
            return {"file_name": entry.name, **self._info(entry)}

        def read(self, file_context, offset, length):
            if file_context.entry.kind == "folder":
                raise NotDirectory()
            data = self._read_range(file_context, offset, length)
            if not data and offset >= self._size(file_context):
                raise EndOfFile()
            return data

        def write(self, file_context, buffer, offset, write_to_end_of_file, constrained_io):
            if file_context.entry.kind == "folder":
                raise AccessDenied()
            local = self._ensure_local(file_context)
            # Record the change before making it, so a crash cannot leave an
            # edited cache file that looks like an untouched download.
            self._mark_pending(file_context)
            with local.open("r+b") as stream:
                stream.seek(0, 2) if write_to_end_of_file else stream.seek(offset)
                if constrained_io:
                    buffer = buffer[:max(0, local.stat().st_size - stream.tell())]
                written = stream.write(buffer)
            return written

        def overwrite(self, file_context, file_attributes, replace_file_attributes, allocation_size):
            local = self._ensure_local(file_context)
            self._mark_pending(file_context)
            with local.open("r+b") as stream:
                stream.truncate(0)

        def set_file_size(self, file_context, new_size, set_allocation_size):
            if not set_allocation_size:
                local = self._ensure_local(file_context)
                self._mark_pending(file_context)
                with local.open("r+b") as stream:
                    stream.truncate(new_size)

        def can_delete(self, file_context, file_name):
            if file_context.entry.kind == "folder" and self._backend.list(file_context.entry.path):
                raise DirectoryNotEmpty()

        def _move_local(self, old_path: str, new_path: str) -> Path | None:
            old_local, new_local = self._local_path(old_path), self._local_path(new_path)
            shutil.rmtree(_sidecar(old_local, CHUNKS_SUFFIX), ignore_errors=True)
            if not old_local.exists():
                return None
            new_local.parent.mkdir(parents=True, exist_ok=True)
            old_local.replace(new_local)
            for suffix in (META_SUFFIX, PENDING_SUFFIX):
                sidecar = _sidecar(old_local, suffix)
                if sidecar.exists():
                    sidecar.replace(_sidecar(new_local, suffix))
            # Pending records name their remote destination; keep them accurate.
            moved = [new_local] if new_local.is_file() else pending_files(new_local)
            for local in moved:
                info = read_pending(local)
                if info and "remote" in info:
                    info["remote"] = new_path + info["remote"][len(old_path):]
                    _sidecar(local, PENDING_SUFFIX).write_text(json.dumps(info), "utf-8")
            return new_local

        def rename(self, file_context, file_name, new_file_name, replace_if_exists):
            old_path, new_path = file_context.entry.path, _remote_path(new_file_name)
            new_name = PurePosixPath(new_path).name
            # A change of capitalisation only is not a collision with itself.
            same_item = old_path.casefold() == new_path.casefold()
            target = None if same_item else self._remote_entry(new_path)
            unsent = None if same_item or target else self._unsent_entry(new_path)
            if target is not None or unsent is not None:
                if not replace_if_exists:
                    raise NameCollision()
                if (target or unsent).kind == "folder":
                    raise AccessDenied()
            with self._mutation_lock:
                if target is not None:
                    # The replaced file stays recoverable from Recently Deleted.
                    self._backend.delete([target.path])
                if target is not None or unsent is not None:
                    self._discard_local(new_path)
                if not file_context.created:
                    if parent_of(old_path) == parent_of(new_path):
                        self._backend.rename(old_path, new_name)
                    else:
                        self._backend.move(old_path, parent_of(new_path))
                        old_name = PurePosixPath(old_path).name
                        if old_name != new_name:
                            self._backend.rename(
                                join_remote(parent_of(new_path), old_name), new_name)
                new_local = self._move_local(old_path, new_path)
            if new_local is not None:
                file_context.local = new_local
            elif file_context.local is not None:
                file_context.local = self._local_path(new_path)
            file_context.entry = Entry(new_name, new_path,
                file_context.entry.kind, file_context.entry.size, file_context.entry.modified)

    return ICloudOperations()


def create_virtual_drive(backend: ICloudBackend, mountpoint: str, cache_root: Path,
                         writable: bool = True,
                         notify: Callable[[str, str], None] | None = None):
    FileSystem = _load_winfspy()[3]
    operations = create_operations(backend, cache_root, notify)
    filesystem = FileSystem(mountpoint, operations, sector_size=4096,
        sectors_per_allocation_unit=1, volume_serial_number=0x1C10D,
        file_info_timeout=3000, dir_info_timeout_valid=1, dir_info_timeout=3000,
        case_sensitive_search=0, case_preserved_names=1, unicode_on_disk=1,
        persistent_acls=1, read_only_volume=0 if writable else 1,
        um_file_context_is_user_context2=1, file_system_name="Vellus",
        # An empty prefix makes this a disk-backed drive letter. Supplying a
        # prefix selects WinFsp's network filesystem device; Windows Shell then
        # tries to resolve the drive through its network provider when a user
        # enters e.g. ``I:`` in Run, even though direct Explorer browsing works.
        prefix="")
    return filesystem, operations


class VirtualDrive:
    def __init__(self, backend: ICloudBackend, mountpoint: str = "I:", writable: bool = False,
                 notify: Callable[[str, str], None] | None = None) -> None:
        self.backend, self.mountpoint, self.writable = backend, mountpoint, writable
        self.cache_root, self.filesystem, self.operations = CACHE_DIR, None, None
        self.notify = notify

    @property
    def mounted(self) -> bool:
        return bool(self.filesystem and self.filesystem.started)

    def configure(self, mountpoint: str, writable: bool) -> None:
        if self.mounted:
            raise RuntimeError("Disconnect the drive before changing its settings.")
        self.mountpoint, self.writable = mountpoint, writable

    def mount(self) -> None:
        if not self.mounted:
            LOGGER.info("Mounting %s (writable=%s)", self.mountpoint, self.writable)
            self.cache_root.mkdir(parents=True, exist_ok=True)
            self.filesystem, self.operations = create_virtual_drive(
                self.backend, self.mountpoint, self.cache_root, self.writable, self.notify)
            self.filesystem.start()
            LOGGER.info("Mounted %s", self.mountpoint)

    def unmount(self) -> None:
        if self.filesystem and self.filesystem.started:
            self.filesystem.stop()
            LOGGER.info("Unmounted %s", self.mountpoint)
        self.filesystem = self.operations = None

    def pending_count(self) -> int:
        return len(pending_files(self.cache_root))

    def retry_pending(self) -> tuple[int, int]:
        """Upload changes that earlier failed to reach iCloud. Returns (uploaded, failed)."""
        if self.mounted and self.operations is not None:
            return self.operations.retry_pending()
        return retry_pending(self.backend, self.cache_root, threading.RLock())
