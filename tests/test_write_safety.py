"""Write-path safety tests against an in-memory stand-in for pyicloud's Drive.

The stand-in copies the two pyicloud behaviours that matter here: folder
listings are cached until refreshed, and changing an item through an outdated
listing is rejected. Nothing in this file contacts Apple.

Run with:  .venv\\Scripts\\python.exe -m unittest discover tests
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import virtual_drive  # noqa: E402
from backend import ICloudBackend, entry_key  # noqa: E402
from config import PENDING_SUFFIX  # noqa: E402
import backend as _backend_module  # noqa: E402
from single_instance import ERROR_ALREADY_EXISTS, MUTEX_NAME, SingleInstance  # noqa: E402

# Last-known folder listings are written to disk; keep the tests' ones out of
# the real app folder.
_backend_module.LISTINGS_PATH = Path(tempfile.mkdtemp(prefix="vellus-tests-")) / "listings.json"


class _FakeKernel32:
    def __init__(self, last_error=0):
        self.last_error = last_error
        self.closed = []
        self.name = None

    def SetLastError(self, value):
        # Preserve the simulated CreateMutexW result rather than this reset.
        pass

    def CreateMutexW(self, security, initial_owner, name):
        self.name = name
        return 42

    def GetLastError(self):
        return self.last_error

    def CloseHandle(self, handle):
        self.closed.append(handle)


class SingleInstanceTests(unittest.TestCase):
    def test_first_instance_holds_mutex_until_closed(self):
        kernel = _FakeKernel32()
        instance = SingleInstance(kernel)

        self.assertTrue(instance.acquire())
        self.assertEqual(MUTEX_NAME, kernel.name)
        self.assertEqual([], kernel.closed)
        instance.close()
        self.assertEqual([42], kernel.closed)

    def test_second_instance_closes_its_handle_and_refuses_to_start(self):
        kernel = _FakeKernel32(ERROR_ALREADY_EXISTS)
        instance = SingleInstance(kernel)

        self.assertFalse(instance.acquire())
        self.assertEqual([42], kernel.closed)


class VirtualDriveConfigurationTests(unittest.TestCase):
    def test_drive_letter_uses_disk_device_not_network_provider(self):
        captured = {}

        class FakeFileSystem:
            def __init__(self, mountpoint, operations, **volume_params):
                captured.update(mountpoint=mountpoint, operations=operations,
                                volume_params=volume_params)

        fake_operations = object()
        loaded = (None, None, None, FakeFileSystem, None, None, None, None, None, None, None)
        with (patch.object(virtual_drive, "_load_winfspy", return_value=loaded),
              patch.object(virtual_drive, "create_operations", return_value=fake_operations)):
            filesystem, operations = virtual_drive.create_virtual_drive(
                object(), "I:", Path("cache"), writable=False)

        self.assertIsInstance(filesystem, FakeFileSystem)
        self.assertIs(operations, fake_operations)
        self.assertEqual(captured["mountpoint"], "I:")
        self.assertEqual(captured["volume_params"]["prefix"], "")


class Item:
    """One file or folder as Apple's servers hold it."""
    registry: dict = {}

    def __init__(self, name, kind="file", content=b"", modified=None):
        self.name, self.kind, self.content = name, kind, content
        self.modified = modified or datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.etag, self.children, self.parent = 1, [], None
        self.id = f"ID{len(Item.registry) + 1}"
        Item.registry[self.id] = self

    def add(self, child):
        child.parent = self
        self.children.append(child)
        return child

    def as_data(self):
        data = {"drivewsid": self.id, "docwsid": self.id, "name": self.name,
                "etag": self.etag, "status": "OK",
                "type": "FOLDER" if self.kind == "folder" else "FILE"}
        if self.kind == "file":
            data.update(size=len(self.content), dateModified=self.modified)
        return data


class FakeResponse:
    def __init__(self, data, status_code=200):
        self.content, self.status_code = data, status_code

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        yield self.content


class FakeNode:
    """Shaped like pyicloud's DriveNode: a snapshot in ``data``, children cached."""

    def __init__(self, connection, data):
        self.drive = self.connection = connection
        self.data = data
        self.item = Item.registry[data["drivewsid"]]
        self._children = None

    name = property(lambda self: self.data["name"])
    type = property(lambda self: self.data["type"].lower())
    size = property(lambda self: self.data.get("size"))
    date_modified = property(lambda self: self.data.get("dateModified"))

    def _check_current(self):
        if self.data["etag"] != self.item.etag or self.item.parent is None:
            raise RuntimeError(f"stale reference to {self.name}")

    def get_children(self, force=False):
        if not self._children or force:
            self.drive.listings += 1
            self.data["items"] = [child.as_data() for child in self.item.children]
            self._children = [FakeNode(self.drive, data) for data in self.data["items"]]
        return self._children

    def remove(self, child):
        if not self._children:
            raise ValueError("No children to remove")
        self.data["items"].remove(child.data)
        self._children.remove(child)

    def dir(self):
        return [child.name for child in self.get_children()]

    def rename(self, name):
        self._check_current()
        self.item.name, self.item.etag = name, self.item.etag + 1
        return self.drive.reply({"items": [{"drivewsid": self.item.id, "name": name,
                                            "etag": self.item.etag, "status": "OK"}]})

    def move_to_trash(self):
        self._check_current()
        self.item.parent.children.remove(self.item)
        self.item.parent = None
        self.drive.trash.append(self.item)

    def delete(self):
        raise AssertionError("permanent delete must never be used")

    def mkdir(self, name):
        child = self.item.add(Item(name, "folder"))
        return self.drive.reply({"folders": [child.as_data()]})

    def upload(self, file_object, **kwargs):
        if self.drive.fail_uploads:
            raise ConnectionError("simulated network failure")
        self.drive.uploaded_names.append(file_object.name)
        self.drive.clock += timedelta(minutes=1)
        self.item.add(Item(os.path.basename(file_object.name), "file",
                           file_object.read(), self.drive.clock))

    def open(self, stream=True, headers=None):
        data = self.item.content
        if headers and "Range" in headers:
            start, end = headers["Range"].split("=")[1].split("-")
            return FakeResponse(data[int(start):int(end) + 1], 206)
        return FakeResponse(data)


class FakeDrive:
    """Shaped like pyicloud's DriveService: anything else is answered by ``root``."""

    def __init__(self):
        self.root_item = Item("", "folder")
        self.root_item.parent = self.root_item
        self.trash, self.uploaded_names, self.fail_uploads = [], [], False
        self.listings, self.clock = 0, datetime(2026, 6, 1, tzinfo=timezone.utc)
        self.odd_replies = False
        self.root = FakeNode(self, self.root_item.as_data())

    def __getattr__(self, attribute):
        return getattr(self.__dict__["root"], attribute)

    def reply(self, normal):
        """What Apple answers to a change; ``odd_replies`` simulates a surprise."""
        return {} if self.odd_replies else normal

    def move_nodes_to_node(self, nodes, destination):
        items = []
        for node in nodes:
            node._check_current()
            node.item.parent.children.remove(node.item)
            destination.item.add(node.item)
            node.item.etag += 1
            items.append({"drivewsid": node.item.id, "etag": node.item.etag, "status": "OK"})
        return self.reply({"items": items})

    def names(self, folder=None):
        return sorted(child.name for child in (folder or self.root_item).children)

    def content(self, name, folder=None):
        return next(c.content for c in (folder or self.root_item).children if c.name == name)


def connected_backend():
    # Each test gets its own file of last-known listings, away from the real one.
    _backend_module.LISTINGS_PATH = (
        Path(tempfile.mkdtemp(prefix="vellus-tests-")) / "listings.json")
    drive = FakeDrive()
    backend = ICloudBackend()
    backend.api = SimpleNamespace(drive=drive)
    return backend, drive


class ReplaceTests(unittest.TestCase):
    def setUp(self):
        self.backend, self.drive = connected_backend()
        self.drive.root_item.add(Item("notes.txt", content=b"old"))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.local = Path(self.tmp.name) / "notes.txt"
        self.local.write_bytes(b"new contents")

    def test_replace_keeps_previous_version_recoverable(self):
        expected = entry_key(self.backend.find("/notes.txt"))
        written = self.backend.replace(self.local, "/notes.txt", expected=expected)
        self.assertEqual(written, "/notes.txt")
        self.assertEqual(self.drive.names(), ["notes.txt"])
        self.assertEqual(self.drive.content("notes.txt"), b"new contents")
        self.assertEqual([item.content for item in self.drive.trash], [b"old"])

    def test_upload_sends_only_the_file_name(self):
        self.backend.replace(self.local, "/notes.txt")
        self.assertEqual(self.drive.uploaded_names, ["notes.txt"])

    def test_remote_change_is_not_overwritten(self):
        expected = entry_key(self.backend.find("/notes.txt"))
        self.drive.root_item.children[0].content = b"edited on the phone"
        written = self.backend.replace(self.local, "/notes.txt", expected=expected)
        self.assertNotEqual(written, "/notes.txt")
        self.assertIn("conflict from this PC", written)
        self.assertEqual(self.drive.content("notes.txt"), b"edited on the phone")
        self.assertEqual(self.drive.content(written.lstrip("/")), b"new contents")
        self.assertEqual(self.drive.trash, [])

    def test_new_file_does_not_replace_one_that_appeared(self):
        written = self.backend.replace(self.local, "/notes.txt", must_be_new=True)
        self.assertIn("conflict from this PC", written)
        self.assertEqual(self.drive.content("notes.txt"), b"old")

    def test_failed_upload_restores_original(self):
        self.drive.fail_uploads = True
        with self.assertRaises(ConnectionError):
            self.backend.replace(self.local, "/notes.txt")
        self.assertEqual(self.drive.names(), ["notes.txt"])
        self.assertEqual(self.drive.content("notes.txt"), b"old")
        self.assertEqual(self.drive.trash, [])

    def test_move_is_server_side(self):
        folder = self.drive.root_item.add(Item("Archive", "folder"))
        self.backend.move("/notes.txt", "/Archive")
        self.assertEqual(self.drive.names(), ["Archive"])
        self.assertEqual(self.drive.names(folder), ["notes.txt"])
        self.assertEqual(self.drive.uploaded_names, [])

    def test_upload_folder_after_listing_was_cached(self):
        self.backend.list("/")
        source = Path(self.tmp.name) / "Photos"
        source.mkdir()
        (source / "a.jpg").write_bytes(b"jpg")
        self.backend.upload(source, "/")
        folder = next(c for c in self.drive.root_item.children if c.name == "Photos")
        self.assertEqual(self.drive.names(folder), ["a.jpg"])


class DriveOperationTests(unittest.TestCase):
    def setUp(self):
        self.backend, self.drive = connected_backend()
        self.drive.root_item.add(Item("report.docx", content=b"original body"))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)
        self.notices = []
        self.ops = virtual_drive.create_operations(
            self.backend, self.cache,
            lambda title, message: self.notices.append((title, message)))

    def test_new_file_is_not_padded_to_allocation_size(self):
        handle = self.ops.create("\\new.txt", 0, 0, 0, None, 8192)
        self.ops.write(handle, b"hello", 0, False, False)
        self.assertEqual(self.ops.read(handle, 0, 100), b"hello")
        self.ops.cleanup(handle, "\\new.txt", 0)
        self.ops.close(handle)
        self.assertEqual(self.drive.content("new.txt"), b"hello")
        self.assertEqual(self.drive.names(), ["new.txt", "report.docx"])

    def test_edit_is_uploaded_and_readable_before_upload(self):
        handle = self.ops.open("\\report.docx", 0, 0)
        self.ops.write(handle, b"ORIGINAL", 0, False, False)
        self.assertEqual(self.ops.read(handle, 0, 100), b"ORIGINAL body")
        self.ops.close(handle)
        self.assertEqual(self.drive.content("report.docx"), b"ORIGINAL body")
        self.assertEqual(list(self.cache.rglob("*" + PENDING_SUFFIX)), [])

    def test_two_saves_through_one_handle_are_not_a_conflict(self):
        handle = self.ops.open("\\report.docx", 0, 0)
        self.ops.write(handle, b"A", 0, False, False)
        self.ops.flush(handle)
        self.ops.write(handle, b"B", 1, False, False)
        self.ops.close(handle)
        self.assertEqual(self.drive.names(), ["report.docx"])
        self.assertEqual(self.drive.content("report.docx"), b"ABiginal body")

    def test_failed_save_is_kept_reported_and_retried(self):
        handle = self.ops.open("\\report.docx", 0, 0)
        self.ops.write(handle, b"EDITED..", 0, False, False)
        self.drive.fail_uploads = True
        with self.assertRaises(ConnectionError):
            self.ops.close(handle)
        self.assertEqual(self.drive.content("report.docx"), b"original body")
        self.assertEqual([title for title, _ in self.notices], ["iCloud upload failed"])
        self.assertEqual(len(list(self.cache.rglob("*" + PENDING_SUFFIX))), 1)

        # The unsent edit is what the drive keeps showing, not the old remote file.
        again = self.ops.open("\\report.docx", 0, 0)
        self.assertEqual(self.ops.read(again, 0, 100), b"EDITED.. body")
        self.assertEqual(self.ops.get_file_info(again)["file_size"], 13)
        with self.assertRaises(ConnectionError):
            self.ops.close(again)

        self.drive.fail_uploads = False
        self.assertEqual(self.ops.retry_pending(), (1, 0))
        self.assertEqual(self.drive.content("report.docx"), b"EDITED.. body")
        self.assertEqual(list(self.cache.rglob("*" + PENDING_SUFFIX)), [])
        self.assertEqual([item.content for item in self.drive.trash], [b"original body"])

    def test_clearing_cache_keeps_unsent_changes(self):
        import config
        handle = self.ops.open("\\report.docx", 0, 0)
        self.ops.write(handle, b"EDITED..", 0, False, False)
        self.drive.fail_uploads = True
        with self.assertRaises(ConnectionError):
            self.ops.close(handle)
        original = config.CACHE_DIR
        config.CACHE_DIR = self.cache
        try:
            self.assertEqual(config.clear_cache(), 1)
        finally:
            config.CACHE_DIR = original
        self.assertEqual((self.cache / "report.docx").read_bytes(), b"EDITED.. body")
        self.assertTrue((self.cache / ("report.docx" + PENDING_SUFFIX)).exists())

    def test_edit_made_elsewhere_becomes_conflict_copy(self):
        handle = self.ops.open("\\report.docx", 0, 0)
        self.ops.write(handle, b"MINE", 0, False, False)
        self.drive.root_item.children[0].content = b"changed on another device"
        self.ops.close(handle)
        self.assertEqual(self.drive.content("report.docx"), b"changed on another device")
        conflict = [name for name in self.drive.names() if "conflict" in name]
        self.assertEqual(len(conflict), 1)
        self.assertEqual(self.drive.content(conflict[0]), b"MINEinal body")
        self.assertEqual([title for title, _ in self.notices], ["iCloud conflict"])

    def test_rename_over_existing_file_replaces_it(self):
        # The save pattern used by many editors: write a temp file, then rename
        # it over the document.
        temp = self.ops.create("\\~save.tmp", 0, 0, 0, None, 0)
        self.ops.write(temp, b"second draft", 0, False, False)
        self.ops.rename(temp, "\\~save.tmp", "\\report.docx", True)
        self.ops.cleanup(temp, "\\report.docx", 0)
        self.ops.close(temp)
        self.assertEqual(self.drive.names(), ["report.docx"])
        self.assertEqual(self.drive.content("report.docx"), b"second draft")
        self.assertEqual([item.content for item in self.drive.trash], [b"original body"])

    def test_rename_without_replace_refuses_existing_name(self):
        self.drive.root_item.add(Item("other.docx", content=b"x"))
        handle = self.ops.open("\\other.docx", 0, 0)
        with self.assertRaises(Exception):
            self.ops.rename(handle, "\\other.docx", "\\report.docx", False)
        self.assertEqual(self.drive.names(), ["other.docx", "report.docx"])

    def test_files_not_on_this_pc_are_marked_online_only(self):
        online_only = 0x400000
        handle = self.ops.open("\\report.docx", 0, 0)
        self.assertTrue(self.ops.get_file_info(handle)["file_attributes"] & online_only)
        self.ops.write(handle, b"X", 0, False, False)
        self.assertFalse(self.ops.get_file_info(handle)["file_attributes"] & online_only)
        self.ops.close(handle)

    def test_delete_goes_to_recently_deleted(self):
        handle = self.ops.open("\\report.docx", 0, 0)
        self.ops.cleanup(handle, "\\report.docx", 0x01)
        self.ops.close(handle)
        # File Explorer sees it gone at once; Apple is told in the background.
        self.assertEqual([entry.name for entry in self.backend.list("/")], [])
        self.ops.wait_for_background()
        self.assertEqual(self.drive.names(), [])
        self.assertEqual([item.name for item in self.drive.trash], ["report.docx"])


class NoRelistTests(unittest.TestCase):
    """After a change the list is updated from Apple's reply, not fetched again.

    Apple can take about twenty seconds to answer a listing requested straight
    after a change, so each of these must cost no listing at all.
    """

    def setUp(self):
        self.backend, self.drive = connected_backend()
        root = self.drive.root_item
        root.add(Item("Alpha", "folder"))
        docs = root.add(Item("Docs", "folder"))
        docs.add(Item("report.txt", content=b"r"))
        root.add(Item("notes.txt", content=b"n"))
        self.names = lambda path: [entry.name for entry in self.backend.list(path)]
        self.assertEqual(self.names("/"), ["Alpha", "Docs", "notes.txt"])
        self.assertEqual(self.names("/Docs"), ["report.txt"])
        self.listings = self.drive.listings

    def assert_no_relisting(self):
        self.assertEqual(self.drive.listings, self.listings)

    def test_new_folder_appears_at_once(self):
        self.backend.mkdir("/", "Beta")
        self.assertEqual(self.names("/"), ["Alpha", "Beta", "Docs", "notes.txt"])
        self.assert_no_relisting()
        # It is a real, usable folder: it can be opened, filled and removed.
        self.backend.mkdir("/Beta", "Inner")
        self.assertEqual(self.names("/Beta"), ["Inner"])
        self.backend.delete(["/Beta"])
        self.assertEqual(self.drive.names(), ["Alpha", "Docs", "notes.txt"])

    def test_deleted_item_disappears_at_once(self):
        self.backend.delete(["/notes.txt", "/Alpha"])
        self.assertEqual(self.names("/"), ["Docs"])
        self.assert_no_relisting()
        self.assertEqual(sorted(item.name for item in self.drive.trash), ["Alpha", "notes.txt"])

    def test_rename_shows_at_once_and_the_item_stays_usable(self):
        self.backend.rename("/notes.txt", "todo.txt")
        self.assertEqual(self.names("/"), ["Alpha", "Docs", "todo.txt"])
        self.assert_no_relisting()
        # A second change needs the version tag Apple returned for the first.
        self.backend.rename("/todo.txt", "done.txt")
        self.backend.delete(["/done.txt"])
        self.assertEqual(self.names("/"), ["Alpha", "Docs"])
        self.assert_no_relisting()

    def test_move_updates_both_folders_at_once(self):
        self.backend.move("/notes.txt", "/Docs")
        self.assertEqual(self.names("/"), ["Alpha", "Docs"])
        self.assertEqual(self.names("/Docs"), ["notes.txt", "report.txt"])
        self.assert_no_relisting()
        self.backend.delete(["/Docs/notes.txt"])
        self.assertEqual(self.names("/Docs"), ["report.txt"])

    def test_surprising_reply_from_apple_falls_back_to_a_fresh_listing(self):
        self.drive.odd_replies = True
        self.backend.mkdir("/", "Beta")
        self.backend.rename("/notes.txt", "todo.txt")
        self.backend.move("/todo.txt", "/Docs")
        self.assertEqual(self.names("/"), ["Alpha", "Beta", "Docs"])
        self.assertEqual(self.names("/Docs"), ["report.txt", "todo.txt"])
        self.assertGreater(self.drive.listings, self.listings)


class InstantFeedbackTests(unittest.TestCase):
    """Slow answers from Apple must not hold up what the user sees."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.backend, self.drive = connected_backend()
        self.backend.username = "me@example.com"
        self.drive.root_item.add(Item("Docs", "folder"))
        self.drive.root_item.add(Item("notes.txt", content=b"n"))
        self.names = lambda entries: [entry.name for entry in entries]

    def restarted(self, username="me@example.com"):
        """A new run of the app against the same iCloud account."""
        import threading
        # A new run starts with nothing cached in memory, in pyicloud either.
        self.drive.root = FakeNode(self.drive, self.drive.root_item.as_data())
        backend = ICloudBackend()
        backend.username = username
        backend.api = SimpleNamespace(drive=self.drive)
        backend.checked = threading.Event()
        backend.on_listing_updated = lambda path: backend.checked.set()
        return backend

    def test_deleted_item_leaves_the_list_before_apple_answers(self):
        self.backend.list("/")
        pending = self.backend.begin_delete(["/notes.txt"])
        # Apple has not been asked yet, and the list already shows it gone.
        self.assertEqual(self.drive.names(), ["Docs", "notes.txt"])
        self.assertEqual(self.names(self.backend.list("/")), ["Docs"])
        self.backend.finish_delete(pending)
        self.assertEqual(self.drive.names(), ["Docs"])
        self.assertEqual([item.name for item in self.drive.trash], ["notes.txt"])

    def test_item_comes_back_if_apple_refuses_the_delete(self):
        self.backend.list("/")
        pending = self.backend.begin_delete(["/notes.txt", "/Docs"])
        self.drive.root_item.children[1].etag += 1   # changed elsewhere: Apple will refuse
        with self.assertRaises(RuntimeError):
            self.backend.finish_delete(pending)
        self.assertEqual(self.names(self.backend.list("/")), ["Docs", "notes.txt"])
        self.assertEqual(self.drive.trash, [])

    def test_concurrent_startup_lists_share_one_apple_request(self):
        started = threading.Event()
        release = threading.Event()
        second_started = threading.Event()
        original = self.drive.root.get_children

        def slow_children(force=False):
            started.set()
            release.wait(5)
            return original(force)

        self.drive.root.get_children = slow_children
        results, errors = [], []

        def refresh(ready=None):
            if ready:
                ready.set()
            try:
                results.append(self.backend.list("/"))
            except Exception as exc:
                errors.append(exc)

        first = threading.Thread(target=refresh)
        second = threading.Thread(target=refresh, args=(second_started,))
        first.start()
        self.assertTrue(started.wait(5))
        second.start()
        self.assertTrue(second_started.wait(5))
        time.sleep(0.02)
        release.set()
        first.join(5)
        second.join(5)

        self.assertEqual([], errors)
        self.assertEqual(2, len(results))
        self.assertEqual(1, self.drive.listings)

    def test_last_known_list_is_shown_at_once_on_the_next_run(self):
        self.backend.list("/")
        self.drive.root_item.add(Item("added-elsewhere.txt", content=b"x"))
        later = self.restarted()
        listings = self.drive.listings
        shown = later.list("/", stale_ok=True)
        self.assertEqual(self.names(shown), ["Docs", "notes.txt"])
        # ...and iCloud is asked in the background; the fresh list follows.
        self.assertTrue(later.checked.wait(5))
        self.assertFalse(later.is_checking("/"))
        self.assertEqual(self.names(later.list("/", stale_ok=True)),
                         ["Docs", "added-elsewhere.txt", "notes.txt"])
        self.assertGreater(self.drive.listings, listings)

    def test_last_known_list_is_never_used_where_freshness_matters(self):
        self.backend.list("/")
        self.drive.root_item.add(Item("added-elsewhere.txt", content=b"x"))
        later = self.restarted()
        self.assertEqual(self.names(later.list("/")),
                         ["Docs", "added-elsewhere.txt", "notes.txt"])
        another = self.restarted()
        self.assertIsNotNone(another.find("/added-elsewhere.txt"))

    def test_last_known_lists_are_per_account_and_cleared_on_sign_out(self):
        self.backend.list("/")
        other = self.restarted("someone-else@example.com")
        listings = self.drive.listings
        other.list("/", stale_ok=True)
        self.assertGreater(self.drive.listings, listings)   # nothing saved for them: asked afresh

        original = (_backend_module.SESSION_DIR, _backend_module.forget_all_passwords)
        _backend_module.SESSION_DIR = Path(self.tmp.name) / "session"
        _backend_module.forget_all_passwords = lambda: 0
        try:
            signing_out = self.restarted()
            signing_out.api = None
            signing_out.sign_out()
        finally:
            _backend_module.SESSION_DIR, _backend_module.forget_all_passwords = original
        self.assertFalse(_backend_module.LISTINGS_PATH.exists())

    def test_file_explorer_is_not_held_up_while_icloud_is_slow(self):
        import threading
        self.backend.list("/")
        self.drive.root_item.children[1].content = b"longer now"   # notes.txt changed elsewhere
        later = self.restarted()
        apple_answers = threading.Event()
        real = self.drive.root.get_children

        def slow_listing(force=False):
            apple_answers.wait(10)
            return real(force)
        self.drive.root.get_children = slow_listing
        ops = virtual_drive.create_operations(later, Path(self.tmp.name) / "cache")
        try:
            # Browsing: the folder's contents, look-ups of names that exist and
            # names that do not, and attribute-only opens all answer at once
            # from the last-known listing.
            root = ops.open("\\\\", 0, 0x80)
            listed = [row["file_name"] for row in ops.read_directory(root, None)]
            self.assertEqual(listed, ["Docs", "notes.txt"])
            self.assertEqual(ops.get_file_info(ops.open("\\\\notes.txt", 0, 0x80))["file_size"], 1)
            with self.assertRaises(Exception):
                ops.open("\\\\desktop.ini", 0, 0x80)

            # Reading a file's contents waits for iCloud, and gets the true size.
            result = {}
            reader = threading.Thread(
                target=lambda: result.update(handle=ops.open("\\\\notes.txt", 0, 0x1)))
            reader.start()
            reader.join(0.3)
            self.assertTrue(reader.is_alive())
        finally:
            apple_answers.set()
        reader.join(5)
        self.assertEqual(ops.get_file_info(result["handle"])["file_size"], len(b"longer now"))

    def test_changes_are_remembered_for_the_next_run(self):
        self.backend.list("/")
        self.backend.mkdir("/", "New")
        self.backend.delete(["/notes.txt"])
        later = self.restarted()
        self.assertEqual(self.names(later.list("/", stale_ok=True)), ["Docs", "New"])


class RenameMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old = Path(self.tmp.name) / "Accessible iCloud Explorer"
        self.new = Path(self.tmp.name) / "Vellus"

    def test_data_follows_the_new_name(self):
        import config
        (self.old / "cache").mkdir(parents=True)
        (self.old / "settings.json").write_text('{"drive_letter": "K"}', "utf-8")
        (self.old / "cache" / "a.txt").write_bytes(b"edit")
        (self.old / "cache" / ("a.txt" + PENDING_SUFFIX)).write_text("{}", "utf-8")
        (self.old / "icloud-explorer.log").write_text("log", "utf-8")
        self.assertTrue(config.migrate_legacy_data(self.old, self.new))
        self.assertFalse(self.old.exists())
        self.assertEqual((self.new / "settings.json").read_text("utf-8"), '{"drive_letter": "K"}')
        self.assertEqual(config.pending_files(self.new / "cache"), [self.new / "cache" / "a.txt"])
        self.assertTrue((self.new / "vellus.log").exists())

    def test_existing_new_folder_is_left_alone(self):
        import config
        self.old.mkdir()
        (self.old / "settings.json").write_text("old", "utf-8")
        self.new.mkdir()
        self.assertFalse(config.migrate_legacy_data(self.old, self.new))
        self.assertTrue((self.old / "settings.json").exists())
        self.assertFalse((self.new / "settings.json").exists())

    def test_session_files_leave_the_temp_folder(self):
        import config
        source, target = Path(self.tmp.name) / "temp", Path(self.tmp.name) / "session"
        source.mkdir()
        (source / "meexamplecom.cookiejar").write_text("cookies", "utf-8")
        (source / "meexamplecom.session").write_text("session", "utf-8")
        (source / "unrelated.txt").write_text("keep", "utf-8")
        self.assertEqual(config.adopt_temp_session(source, target), 2)
        self.assertEqual(sorted(p.name for p in target.iterdir()),
                         ["meexamplecom.cookiejar", "meexamplecom.session"])
        self.assertEqual([p.name for p in source.iterdir()], ["unrelated.txt"])


class FakeService:
    """Stands in for pyicloud's PyiCloudService; never contacts Apple."""

    session_valid = False   # would Apple accept the saved/browser session?
    created = []

    def __init__(self, apple_id, password=None, cookie_directory=None, authenticate=True):
        from requests.cookies import RequestsCookieJar
        self.apple_id, self.password, self.cookie_directory = apple_id, password, cookie_directory
        self.constructed_with_authenticate = authenticate
        self.session = SimpleNamespace(send=None, cookies=RequestsCookieJar(), headers={}, data={})
        self.data, self.calls = {}, []
        FakeService.created.append(self)

    def _validate_token(self):
        from pyicloud.exceptions import PyiCloudAPIResponseException
        self.calls.append("validate")
        if not FakeService.session_valid:
            raise PyiCloudAPIResponseException("invalid", None)
        return {"dsInfo": {"dsid": "1"}, "hsaTrustedBrowser": True}

    def _update_state(self):
        self.calls.append("update_state")

    def _authenticate_with_token(self):
        self.calls.append("token")

    def authenticate(self):
        self.calls.append("authenticate")

    def logout(self):
        self.calls.append("logout")


class SignInTests(unittest.TestCase):
    USER = "me@example.com"
    BROWSER_COOKIES = [
        {"name": "X-APPLE-WEBAUTH-TOKEN", "value": "tok", "domain": ".icloud.com",
         "path": "/", "secure": True, "expires": -1},
        {"name": "X-APPLE-DS-WEB-SESSION-TOKEN", "value": "ds", "domain": ".icloud.com"},
        {"name": "aasp", "value": "a", "domain": ".idmsa.apple.com", "path": "/",
         "expires": 1900000000.5}]

    def setUp(self):
        import backend as backend_module
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session_dir = Path(self.tmp.name) / "session"
        # Everything that reaches outside the test is replaced, including the
        # sweep of Windows Credential Manager: the real one must never run here.
        self.sweeps = []
        replacements = {"PyiCloudService": FakeService,
                        "adopt_temp_session": lambda: 0, "SESSION_DIR": self.session_dir,
                        "forget_all_passwords": lambda: self.sweeps.append(True) or 0}
        for name, value in replacements.items():
            original = getattr(backend_module, name)
            setattr(backend_module, name, value)
            self.addCleanup(setattr, backend_module, name, original)
        FakeService.session_valid = False
        FakeService.created = []
        self.backend = ICloudBackend()

    def service(self):
        return FakeService.created[-1]

    def saved_session_data(self, data):
        """Make the next services start with this saved session data."""
        original = FakeService.__init__

        def with_saved_session(service, *args, **kwargs):
            original(service, *args, **kwargs)
            service.session.data.update(data)
        FakeService.__init__ = with_saved_session
        self.addCleanup(setattr, FakeService, "__init__", original)

    def test_no_password_ever_reaches_the_sign_in_code(self):
        import inspect
        import backend as backend_module
        self.assertEqual(list(inspect.signature(ICloudBackend.connect).parameters),
                         ["self", "username", "cookies", "user_agent"])
        self.assertFalse(hasattr(backend_module, "keyring"))
        FakeService.session_valid = True
        self.backend.connect(self.USER, cookies=self.BROWSER_COOKIES, user_agent="Edge/1")
        self.assertIsNone(self.service().password)

    def test_saved_session_resumes(self):
        FakeService.session_valid = True
        self.backend.connect(self.USER)
        self.assertEqual(self.service().calls, ["validate", "update_state"])
        self.assertFalse(self.service().constructed_with_authenticate)
        # The session is kept in the app's own folder, not the temp folder.
        self.assertEqual(self.service().cookie_directory, str(self.session_dir))
        self.assertTrue(self.backend.connected)

    def test_missing_or_lapsed_session_asks_for_sign_in_and_nothing_else(self):
        from backend import SignInRequired
        # Even a token left by an old password sign-in is not offered to Apple.
        self.saved_session_data({"session_token": "from-old-password-sign-in"})
        with self.assertRaises(SignInRequired):
            self.backend.connect(self.USER)
        self.assertEqual(self.service().calls, ["validate"])
        self.assertFalse(self.backend.connected)

    def test_browser_session_is_handed_over_with_its_cookies_and_identity(self):
        self.saved_session_data({"session_token": "from-old-password-sign-in"})
        FakeService.session_valid = True
        self.backend.connect(self.USER, cookies=self.BROWSER_COOKIES, user_agent="Edge/1")
        service = self.service()
        jar = service.session.cookies
        self.assertEqual(jar.get("X-APPLE-WEBAUTH-TOKEN", domain=".icloud.com"), "tok")
        self.assertEqual(jar.get("aasp", domain=".idmsa.apple.com"), "a")
        self.assertEqual(service.session.headers["User-Agent"], "Edge/1")
        self.assertEqual(service.calls, ["validate", "update_state"])
        self.assertNotIn("session_token", service.session.data)

        # The browser's identity is reused when that session is resumed later.
        ICloudBackend().connect(self.USER)
        self.assertEqual(self.service().session.headers["User-Agent"], "Edge/1")

    def test_browser_session_apple_rejects_is_reported(self):
        with self.assertRaises(RuntimeError) as caught:
            self.backend.connect(self.USER, cookies=self.BROWSER_COOKIES)
        self.assertIn("try signing in again", str(caught.exception))
        self.assertEqual(self.service().calls, ["validate"])
        self.assertFalse(self.backend.connected)

    def test_session_lapsing_while_running_is_reported_for_sign_in(self):
        FakeService.session_valid = True
        self.backend.connect(self.USER)
        notices = []
        self.backend.on_pause = lambda message, needs_sign_in: notices.append(needs_sign_in)
        self.backend.governor.after(421, "https://p01-drivews.icloud.com/x")
        self.assertEqual(notices, [True])
        self.assertEqual(self.service().calls, ["validate", "update_state"])

    def test_uninstall_clean_up_removes_data_folders_and_old_passwords(self):
        import backend as backend_module
        app_dir = Path(self.tmp.name) / "appdata" / "Vellus"
        (app_dir / "cache").mkdir(parents=True)
        (app_dir / "settings.json").write_text("{}", "utf-8")
        keep = Path(self.tmp.name) / "appdata" / "Other"
        keep.mkdir()
        # Both locations are redirected: the real ones must never be touched here.
        temp_dir = Path(self.tmp.name) / "temp"
        (temp_dir / "Vellus").mkdir(parents=True)
        for name, value in (("APP_DIR", app_dir),
                            ("tempfile", SimpleNamespace(gettempdir=lambda: str(temp_dir)))):
            original = getattr(backend_module, name)
            setattr(backend_module, name, value)
            self.addCleanup(setattr, backend_module, name, original)
        backend_module.remove_user_data()
        self.assertFalse(app_dir.exists())
        self.assertFalse((temp_dir / "Vellus").exists())
        self.assertTrue(temp_dir.exists())
        self.assertTrue(keep.exists())
        self.assertEqual(self.sweeps, [True])

    def test_sign_out_removes_everything_saved(self):
        FakeService.session_valid = True
        self.backend.connect(self.USER)
        (self.session_dir / "meexamplecom.cookiejar").write_text("cookies", "utf-8")
        service = self.service()
        self.backend.sign_out()
        self.assertIn("logout", service.calls)
        # Passwords saved by earlier versions are swept away too.
        self.assertEqual(self.sweeps, [True])
        self.assertFalse(self.session_dir.exists())
        self.assertFalse(self.backend.connected)


class GovernorTests(unittest.TestCase):
    def setUp(self):
        from governor import RequestGovernor
        self.now, self.slept, self.pauses = 0.0, [], []
        self.governor = RequestGovernor(
            on_pause=lambda message, needs_sign_in: self.pauses.append(needs_sign_in),
            clock=lambda: self.now, sleep=self.slept.append)

    def test_bursts_are_throttled_not_refused(self):
        for _ in range(self.governor.BURST):
            self.governor.before()
        self.assertEqual(self.slept, [])
        self.governor.before()
        self.assertEqual(len(self.slept), 1)
        self.assertAlmostEqual(self.slept[0], 1 / self.governor.RATE)

    def test_refusal_pauses_and_backs_off_further_each_time(self):
        from governor import ICloudPaused
        url = "https://p01-drivews.icloud.com/x"
        self.governor.after(503, url)
        with self.assertRaises(ICloudPaused):
            self.governor.before()
        self.assertAlmostEqual(self.governor.paused_for, 30)
        self.now += 31
        self.governor.before()
        self.governor.after(503, url)
        self.assertAlmostEqual(self.governor.paused_for, 60)
        self.now += 61
        self.governor.before()
        self.governor.after(200, url)
        self.governor.after(429, url)
        self.assertAlmostEqual(self.governor.paused_for, 30)
        self.assertEqual(self.pauses, [False, False, False])

    def test_expired_session_stops_requests_and_asks_for_sign_in(self):
        from governor import ICloudPaused
        self.governor.after(421, "https://p01-drivews.icloud.com/x")
        self.assertTrue(self.governor.needs_sign_in)
        self.assertEqual(self.pauses, [True])
        with self.assertRaises(ICloudPaused):
            self.governor.before()

    def test_sign_in_replies_and_ordinary_errors_do_not_pause(self):
        self.governor.after(401, "https://idmsa.apple.com/appleauth/auth/verify")
        self.governor.after(404, "https://p01-drivews.icloud.com/x")
        self.governor.after(403, "https://p01-drivews.icloud.com/x")
        self.assertEqual(self.governor.paused_for, 0)
        self.assertEqual(self.pauses, [])

    def test_every_request_from_the_session_is_governed(self):
        from governor import ICloudPaused
        sent = []
        session = SimpleNamespace(send=lambda request, **kwargs: (
            sent.append(kwargs), SimpleNamespace(status_code=503))[1])
        self.governor.install(session)
        request = SimpleNamespace(url="https://p01-drivews.icloud.com/x")
        session.send(request, timeout=None)
        with self.assertRaises(ICloudPaused):
            session.send(request)
        self.assertEqual(len(sent), 1)
        # An unresponsive server address is given up on after three seconds.
        self.assertEqual(sent[0]["timeout"], (3.0, 300.0))

    def test_slow_requests_are_logged_without_file_names(self):
        from governor import _describe
        self.assertEqual(
            _describe("https://p01-drivews.icloud.com:443/retrieveItemDetailsInFolders?dsid=1"),
            "p01-drivews.icloud.com retrieveItemDetailsInFolders")
        self.assertEqual(_describe("https://cvws.icloud-content.com/B/abc/Private%20Tax.pdf?o=x"),
                         "cvws.icloud-content.com")
        session = SimpleNamespace(send=lambda request, **kwargs: (
            setattr(self, "now", self.now + 21), SimpleNamespace(status_code=200))[1])
        self.governor.install(session)
        with self.assertLogs("governor", level="INFO") as logs:
            session.send(SimpleNamespace(url="https://p01-drivews.icloud.com/retrieveItemDetailsInFolders"))
        self.assertIn("Slow request: p01-drivews.icloud.com retrieveItemDetailsInFolders took 21.0s",
                      logs.output[0])

    def test_drive_access_is_confirmed_once_not_on_every_operation(self):
        import threading

        class Api:
            """Like pyicloud: every use of ``drive`` re-confirms access with Apple."""
            def __init__(self, drive):
                self._real, self._drive, self.confirmations = drive, None, 0
                self.refreshed = threading.Event()

            @property
            def drive(self):
                self.confirmations += 1
                self._drive = self._real
                return self._drive

            def _request_pcs_for_service(self, app_name):
                self.confirmations += 1
                self.refreshed.set()

        backend, drive = connected_backend()
        drive.root_item.add(Item("a.txt", content=b"a"))
        api = backend.api = Api(drive)
        for _ in range(5):
            backend._node("/a.txt")
        self.assertEqual(api.confirmations, 1)

        # Ten minutes later the next operation is not held up either: the
        # confirmation is repeated in the background.
        backend._pcs_checked -= backend.PCS_REFRESH_SECONDS + 1
        backend._node("/a.txt")
        self.assertTrue(api.refreshed.wait(5))
        self.assertEqual(api.confirmations, 2)

    def test_paused_listing_falls_back_to_last_known_contents(self):
        from governor import ICloudPaused
        backend, drive = connected_backend()
        drive.root_item.add(Item("a.txt", content=b"a"))
        first = backend.list("/")

        def refuse(force=False):
            raise ICloudPaused("paused")
        drive.root.get_children = refuse
        self.assertEqual(backend.list("/", force=True), first)


if __name__ == "__main__":
    unittest.main()
