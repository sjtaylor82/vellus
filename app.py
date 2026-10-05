from __future__ import annotations

import ctypes
import os
import shutil
import sys

# PyInstaller's windowed bootloader intentionally provides no console streams.
# pyicloud evaluates sys.stdout.isatty() while importing, so supply harmless
# streams before importing any third-party modules.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")
if sys.stdin is None:
    sys.stdin = open(os.devnull, "r", encoding="utf-8")

import tempfile
import threading
import traceback
from datetime import datetime
import logging
from pathlib import Path, PurePosixPath, PureWindowsPath

import wx
import wx.adv

from app_logging import setup_logging
from apple_signin import AppleSignInDialog, embedded_sign_in_available
from backend import Entry, ICloudBackend, SignInRequired, remove_user_data
from config import (APP_DIR, BROWSER_DIR, CACHE_DIR, LOG_PATH, VERSION, Settings,
                    available_drive_letters, clear_cache, drive_letter_in_use,
                    load_settings, migrate_legacy_data, save_settings)
from single_instance import SingleInstance
from virtual_drive import VirtualDrive, _load_winfspy

LOGGER = logging.getLogger(__name__)

ABOUT_TEXT = f"""Vellus {VERSION}
An accessible Windows drive letter for iCloud Drive.
https://github.com/sjtaylor82/vellus

Vellus is an independent, open-source project released under the MIT License. It is not affiliated with, endorsed by, or supported by Apple Inc. iCloud and iCloud Drive are trademarks of Apple Inc.

Vellus reaches iCloud through an unofficial interface that Apple does not publish or support. It may stop working, and Apple could restrict an account that uses it. Keep backups of anything important.

The drive letter is provided by:
WinFsp - Windows File System Proxy, Copyright (C) Bill Zissimopoulos
https://github.com/winfsp/winfsp

Vellus also includes pyicloud, winfspy, wxPython and other open-source libraries. Their licences are listed in THIRD-PARTY-NOTICES.md in the Vellus program folder.
"""


def format_size(size: int | None) -> str:
    if size is None:
        return ""
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} {unit}" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def install_crash_handler() -> None:
    def report(exception_type, exception, exception_traceback):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        log_path = APP_DIR / "crash.log"
        details = "".join(traceback.format_exception(
            exception_type, exception, exception_traceback))
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"\n[{datetime.now().isoformat()}]\n{details}")
        try:
            wx.MessageBox(
                f"Vellus could not continue.\n\n{exception}\n\n"
                f"Details were saved to:\n{log_path}",
                "Vellus error", wx.OK | wx.ICON_ERROR)
        except Exception:
            pass

    sys.excepthook = report


class SettingsDialog(wx.Dialog):
    def __init__(self, parent, settings: Settings):
        super().__init__(parent, title="Vellus settings")
        panel = wx.Panel(self)
        # The label is created just before its field so screen readers pair them.
        drive_label = wx.StaticText(panel, label="&Drive letter:")
        letters = sorted(set(available_drive_letters()) | {settings.drive_letter})
        self.drive_letter = wx.Choice(panel, choices=[f"{letter}:" for letter in letters],
                                      name="Drive letter")
        self.drive_letter.SetSelection(letters.index(settings.drive_letter))
        self.auto_mount = wx.CheckBox(panel, label="&Mount drive letter upon launch")
        self.auto_mount.Value = settings.mount_automatically
        self.writable = wx.CheckBox(panel, label="&Permit Write Access")
        self.writable.Value = settings.writable_drive
        self.close_to_tray = wx.CheckBox(panel, label="&Keep running when closed")
        self.close_to_tray.Value = settings.close_to_tray
        form = wx.FlexGridSizer(1, 2, 8, 8)
        form.Add(drive_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.drive_letter)
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(form, 0, wx.ALL, 12)
        for control in (self.auto_mount, self.writable, self.close_to_tray):
            layout.Add(control, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        panel.SetSizer(layout)
        buttons = self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        if buttons:
            outer.Add(buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 12)
        self.SetSizer(outer)
        self.SetClientSize((480, 230))
        # Start on the first setting rather than the OK button.
        self.drive_letter.SetFocus()


class TrayIcon(wx.adv.TaskBarIcon):
    def __init__(self, frame):
        super().__init__()
        self.frame = frame
        icon = wx.ArtProvider.GetIcon(wx.ART_HARDDISK, wx.ART_OTHER, (16, 16))
        self.SetIcon(icon, "Vellus")
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_DCLICK, lambda event: frame.show_window())

    def CreatePopupMenu(self):
        menu = wx.Menu()
        for label, handler in (("Open iCloud Drive", self.frame.on_open_drive),
                               ("Show Vellus", lambda event: self.frame.show_window()),
                               ("Connect drive", self.frame.on_mount_drive),
                               ("Disconnect drive", self.frame.on_unmount_drive),
                               ("Refresh", lambda event: self.frame.navigate(self.frame.current_path, force=True)),
                               ("Sign in again...",
                                lambda event: self.frame.sign_in(exit_on_cancel=False)),
                               ("Sign out...", self.frame.on_sign_out),
                               ("Upload waiting changes",
                                lambda event: self.frame.retry_pending_uploads(announce=True)),
                               ("Clear cache", self.frame.on_clear_cache),
                               ("Open log", self.frame.on_open_log),
                               ("Settings...", self.frame.on_settings)):
            item = menu.Append(wx.ID_ANY, label)
            menu.Bind(wx.EVT_MENU, handler, item)
        menu.AppendSeparator()
        item = menu.Append(wx.ID_EXIT, "Exit")
        menu.Bind(wx.EVT_MENU, self.frame.on_exit, item)
        return menu

    def notify(self, title, message):
        try:
            self.ShowBalloon(title, message, 5000)
        except (AttributeError, RuntimeError):
            pass


class MainFrame(wx.Frame):
    # The file list takes its spoken name from this label, so it doubles as
    # the loading indicator for screen reader users.
    FILES_LABEL = "Files and fo&lders:"
    LISTING_LABEL = "&Listing files and folders..."

    def __init__(self):
        super().__init__(None, title="Vellus", size=(1000, 650))
        self.backend = ICloudBackend()
        self.backend.on_pause = lambda message, needs_sign_in: wx.CallAfter(
            self.on_requests_paused, message, needs_sign_in)
        self.backend.on_listing_updated = lambda path: wx.CallAfter(self.on_listing_updated, path)
        self.settings = load_settings()
        if not self.settings.username:
            # Signed out: make sure no browser profile from an earlier sign-in remains.
            shutil.rmtree(BROWSER_DIR, ignore_errors=True)
        self.drive = VirtualDrive(
            self.backend, self.settings.mountpoint, self.settings.writable_drive,
            notify=lambda title, message: wx.CallAfter(self.tray.notify, title, message))
        self.exiting = False
        self.signing_in = False
        self.tray_hint_shown = False
        self.tray = TrayIcon(self)
        self.current_path = "/"
        self.entries: list[Entry] = []
        self.clipboard: tuple[str, list[str]] | None = None
        self._build_ui()
        self.retrying_pending = False
        self.pending_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda event: self.retry_pending_uploads(), self.pending_timer)
        self.pending_timer.Start(5 * 60 * 1000)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        wx.CallAfter(self.initial_sign_in)

    def _build_ui(self):
        panel = wx.Panel(self)
        # Labels are created just before their controls so screen readers pair them.
        address_label = wx.StaticText(panel, label="iCloud &path:")
        self.address = wx.TextCtrl(panel, value="/", style=wx.TE_PROCESS_ENTER, name="iCloud path")
        self.address.Bind(wx.EVT_TEXT_ENTER, lambda event: self.navigate(self.address.Value))
        files_label = self.files_label = wx.StaticText(panel, label=self.FILES_LABEL)
        self.files = wx.ListCtrl(panel, style=wx.LC_REPORT, name="Files and folders")
        for heading, width in (("Name", 420), ("Type", 110), ("Size", 110), ("Modified", 200)):
            self.files.AppendColumn(heading, width=width)
        self.files.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_open)
        self.files.Bind(wx.EVT_CONTEXT_MENU, self.on_context_menu)
        self.files.Bind(wx.EVT_KEY_DOWN, self.on_list_key)
        self.status = self.CreateStatusBar()
        self.status.SetStatusText("Not signed in")
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(address_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        layout.Add(self.address, 0, wx.ALL | wx.EXPAND, 8)
        layout.Add(files_label, 0, wx.LEFT | wx.RIGHT, 8)
        layout.Add(self.files, 1, wx.ALL | wx.EXPAND, 8)
        panel.SetSizer(layout)
        self._build_menu()

    def _build_menu(self):
        bar = wx.MenuBar()
        file_menu = wx.Menu()
        commands = [
            (wx.ID_OPEN, "&Open\tEnter", self.on_open),
            (wx.ID_SAVE, "&Download to...\tCtrl+D", self.on_download),
            (wx.ID_ANY, "Copy Windows &path\tCtrl+Shift+C", self.on_copy_windows_path),
            (wx.ID_ANY, "Upload &files...\tCtrl+U", self.on_upload),
            (wx.ID_NEW, "New &folder\tCtrl+Shift+N", self.on_new_folder),
            (wx.ID_ANY, "&Rename\tF2", self.on_rename),
            (wx.ID_DELETE, "&Delete\tDelete", self.on_delete),
        ]
        # Commands that change what is in iCloud; unavailable unless the
        # "Permit Write Access" setting is on.
        changing = (self.on_upload, self.on_new_folder, self.on_rename, self.on_delete,
                    self.on_cut, self.on_paste)
        self.change_items = []
        for ident, label, handler in commands:
            item = file_menu.Append(ident, label)
            self.Bind(wx.EVT_MENU, handler, item)
            if handler in changing:
                self.change_items.append(item)
        file_menu.AppendSeparator()
        exit_item = file_menu.Append(wx.ID_EXIT, "E&xit\tCtrl+Q")
        self.Bind(wx.EVT_MENU, self.on_exit, exit_item)
        edit = wx.Menu()
        for ident, label, handler in (
            (wx.ID_COPY, "&Copy\tCtrl+C", self.on_copy),
            (wx.ID_CUT, "Cu&t\tCtrl+X", self.on_cut),
            (wx.ID_PASTE, "&Paste\tCtrl+V", self.on_paste),
            (wx.ID_SELECTALL, "Select &all\tCtrl+A", self.on_select_all),
        ):
            item = edit.Append(ident, label)
            self.Bind(wx.EVT_MENU, handler, item)
            if handler in changing:
                self.change_items.append(item)
        self.apply_change_setting()
        view = wx.Menu()
        refresh = view.Append(wx.ID_REFRESH, "&Refresh\tF5")
        self.Bind(wx.EVT_MENU, lambda event: self.navigate(self.current_path, force=True), refresh)
        up = view.Append(wx.ID_UP, "Go &up\tAlt+Up")
        self.Bind(wx.EVT_MENU, self.on_up, up)
        drive = wx.Menu()
        mount = drive.Append(wx.ID_ANY, "&Connect iCloud drive")
        unmount = drive.Append(wx.ID_ANY, "&Disconnect iCloud drive")
        open_drive = drive.Append(wx.ID_ANY, "Open in File &Explorer")
        settings = drive.Append(wx.ID_PREFERENCES, "&Settings...\tCtrl+,")
        drive.AppendSeparator()
        sign_in_again = drive.Append(wx.ID_ANY, "Sign &in again...")
        sign_out = drive.Append(wx.ID_ANY, "Sign &out...")
        self.Bind(wx.EVT_MENU, lambda event: self.sign_in(exit_on_cancel=False), sign_in_again)
        self.Bind(wx.EVT_MENU, self.on_sign_out, sign_out)
        self.Bind(wx.EVT_MENU, self.on_mount_drive, mount)
        self.Bind(wx.EVT_MENU, self.on_unmount_drive, unmount)
        self.Bind(wx.EVT_MENU, self.on_open_drive, open_drive)
        self.Bind(wx.EVT_MENU, self.on_settings, settings)
        bar.Append(file_menu, "&File")
        bar.Append(edit, "&Edit")
        bar.Append(view, "&View")
        bar.Append(drive, "&Drive")
        help_menu = wx.Menu()
        about = help_menu.Append(wx.ID_ABOUT, "&About Vellus")
        self.Bind(wx.EVT_MENU, self.on_about, about)
        bar.Append(help_menu, "&Help")
        self.SetMenuBar(bar)

    def apply_change_setting(self):
        """Make the change commands available only when changes are allowed."""
        for item in self.change_items:
            item.Enable(self.settings.writable_drive)

    def changes_allowed(self) -> bool:
        if self.settings.writable_drive:
            return True
        wx.MessageBox("Vellus is read-only. To change files, tick Permit Write Access in "
                      "Settings.",
                      "Read-only", wx.OK | wx.ICON_INFORMATION, self)
        return False

    def run_task(self, message, work, success=None, error=None):
        self.status.SetStatusText(message)
        def runner():
            try:
                result = work()
            except Exception as exc:
                if isinstance(exc, SignInRequired):
                    # Expected whenever there is no saved session; the sign-in
                    # window that follows says all that needs saying.
                    LOGGER.info("%s", exc)
                    wx.CallAfter(self.status.SetStatusText, str(exc))
                else:
                    LOGGER.exception("Task failed: %s", message)
                    wx.CallAfter(wx.MessageBox, str(exc), "iCloud error", wx.OK | wx.ICON_ERROR, self)
                    wx.CallAfter(self.status.SetStatusText, f"Failed: {exc}")
                    wx.CallAfter(self.tray.notify, "Vellus", str(exc))
                if error:
                    wx.CallAfter(error, exc)
            else:
                # "Ready" first, so that a status set by the success handler stays.
                wx.CallAfter(self.status.SetStatusText, "Ready")
                if success:
                    wx.CallAfter(success, result)
        threading.Thread(target=runner, daemon=True).start()

    def initial_sign_in(self, exit_on_cancel=True):
        """Resume the saved session, or open Apple's sign-in page if there is none."""
        if self.settings.username:
            # Only a missing or lapsed session leads to the sign-in page; any
            # other failure (no network, say) is reported and left at that.
            self.run_task("Signing in...",
                lambda: self.backend.connect(self.settings.username), self.on_connected,
                lambda exc: (self.sign_in(exit_on_cancel)
                             if isinstance(exc, SignInRequired) else None))
        else:
            self.sign_in(exit_on_cancel)

    def on_requests_paused(self, message, needs_sign_in):
        self.status.SetStatusText(message)
        if needs_sign_in:
            self.sign_in(exit_on_cancel=False, signed_out=True)
        else:
            self.tray.notify("Vellus paused", message)

    def sign_in(self, exit_on_cancel=True, signed_out=False):
        """Sign in on Apple's own page, shown in an embedded browser window."""
        if self.signing_in:
            return
        self.signing_in = True
        accepted = False
        try:
            self.show_window()
            if not embedded_sign_in_available():
                wx.MessageBox(
                    "Signing in needs the Microsoft Edge WebView2 Runtime, which was not "
                    "found on this PC. Install it from Microsoft, then start Vellus again.\n\n"
                    "https://developer.microsoft.com/microsoft-edge/webview2/",
                    "Sign in to iCloud", wx.OK | wx.ICON_ERROR, self)
            else:
                lead = ("Apple has signed Vellus out, so you need to sign in again."
                        if signed_out else "Apple's sign-in page opens next.")
                if wx.MessageBox(
                        f"{lead}\n\nYou are encouraged to tick \"Keep me signed in\" on "
                        "Apple's page for seamless access. Without it, Apple signs Vellus "
                        "out after about half an hour.",
                        "Sign in to iCloud", wx.OK | wx.CANCEL | wx.ICON_INFORMATION,
                        self) == wx.OK:
                    BROWSER_DIR.mkdir(parents=True, exist_ok=True)
                    dialog = AppleSignInDialog(self, str(BROWSER_DIR))
                    accepted = dialog.ShowModal() == wx.ID_OK
                    cookies, apple_id, user_agent = (dialog.cookies, dialog.apple_id,
                                                     dialog.user_agent)
                    dialog.Destroy()
        finally:
            self.signing_in = False
        if accepted:
            self.settings.username = apple_id
            save_settings(self.settings)
            self.run_task(
                "Signing in...",
                lambda: self.backend.connect(apple_id, cookies=cookies, user_agent=user_agent),
                self.on_connected)
        elif exit_on_cancel:
            self.on_exit(None)

    def on_sign_out(self, event):
        waiting = self.drive.pending_count()
        message = ("Sign out of iCloud on this PC?\n\nThis disconnects the drive and removes "
                   "the saved sign-in and all cached copies of your files from this PC. "
                   "Nothing in iCloud is deleted.")
        if waiting:
            message += (f"\n\nWARNING: {waiting} change(s) have not been uploaded to iCloud "
                        "yet and will be lost.")
        if wx.MessageBox(message, "Sign out", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
                         self) != wx.YES:
            return

        def work():
            self.drive.unmount()
            self.backend.sign_out()
            for folder in (CACHE_DIR, BROWSER_DIR, Path(tempfile.gettempdir()) / "Vellus"):
                shutil.rmtree(folder, ignore_errors=True)

        def done(_):
            self.settings.username = ""
            save_settings(self.settings)
            self.entries = []
            self.files.DeleteAllItems()
            self.status.SetStatusText("Signed out")
            self.sign_in(exit_on_cancel=False)
        self.run_task("Signing out...", work, done)

    def on_connected(self, _):
        if "--tray" in sys.argv:
            self.Hide()
        # Signed in: the list is on its way, though the drive connects first.
        self.files_label.SetLabel(self.LISTING_LABEL)
        if self.settings.mount_automatically:
            if self.ensure_drive_letter_available():
                self.run_task(f"Mounting iCloud as {self.settings.mountpoint}...",
                    self.drive.mount, lambda unused: self.on_mounted(load_root=True),
                    lambda exc: self.navigate("/"))
            else:
                self.navigate("/")
        else:
            self.navigate("/")

    def focused_name(self) -> str | None:
        index = self.files.GetFocusedItem()
        return self.entries[index].name if 0 <= index < len(self.entries) else None

    def navigate(self, path: str, force: bool = False, select: str | None = None):
        """Show a folder. ``select`` names the item to land on; by default a
        reload stays on the current item and a new folder starts at the top."""
        normalized = "/" + str(PurePosixPath(path)).lstrip("/")
        if select is None and normalized == self.current_path:
            select = self.focused_name()
        def loaded(entries):
            self.current_path = normalized
            self.address.ChangeValue(normalized)
            self.show_entries(entries, select)
            self.files.SetFocus()
        self.files_label.SetLabel(self.LISTING_LABEL)
        # A folder not yet seen in this run is shown at once from its last-known
        # contents while iCloud is asked afresh; on_listing_updated follows.
        self.run_task(f"Loading {normalized}",
                      lambda: self.backend.list(normalized, force=force, stale_ok=not force),
                      loaded, lambda exc: self.files_label.SetLabel(self.FILES_LABEL))

    def show_entries(self, entries, select=None):
        """Fill the list and land on the item named ``select`` (else the first)."""
        self.entries = entries
        self.files.DeleteAllItems()
        for entry in entries:
            row = self.files.InsertItem(self.files.ItemCount, entry.name)
            self.files.SetItem(row, 1, "File folder" if entry.kind == "folder" else "File")
            self.files.SetItem(row, 2, format_size(entry.size))
            self.files.SetItem(row, 3, str(entry.modified or ""))
        checking = self.backend.is_checking(self.current_path)
        self.status.SetStatusText(f"{len(entries)} items" +
                                  (", checking iCloud for changes" if checking else ""))
        self.files_label.SetLabel(self.FILES_LABEL)
        if entries:
            wanted = (select or "").casefold()
            index = next((i for i, entry in enumerate(entries)
                          if entry.name.casefold() == wanted), 0)
            self.files.Select(index)
            self.files.Focus(index)
            self.files.EnsureVisible(index)

    def on_listing_updated(self, path):
        """iCloud has answered for a folder that was shown from its last-known contents."""
        if path != self.current_path:
            return
        try:
            entries = self.backend.list(path, stale_ok=True)
        except Exception:
            return
        if entries != self.entries:
            # Changed since last time: redraw, staying on the same item and
            # without pulling focus from wherever the user is.
            self.show_entries(entries, self.focused_name())
        else:
            self.status.SetStatusText(f"{len(entries)} items")

    def selected(self) -> list[Entry]:
        result, index = [], -1
        while True:
            index = self.files.GetNextItem(index, state=wx.LIST_STATE_SELECTED)
            if index == -1:
                return result
            result.append(self.entries[index])

    def on_open(self, event):
        items = self.selected()
        if len(items) != 1:
            return
        item = items[0]
        if item.kind == "folder":
            self.navigate(item.path)
            return
        if self.drive.mounted:
            os.startfile(self.windows_path(item.path))
            return
        target = Path(tempfile.gettempdir()) / "Vellus" / item.name
        self.run_task(f"Downloading {item.name}", lambda: self.backend.download(item.path, target),
                      lambda path: os.startfile(path))

    def on_download(self, event):
        items = self.selected()
        if not items:
            return
        dialog = wx.DirDialog(self, "Choose download folder")
        if dialog.ShowModal() == wx.ID_OK:
            folder = Path(dialog.Path)
            self.run_task("Downloading selection...", lambda: [self.backend.download_tree(i.path, folder / i.name) for i in items])
        dialog.Destroy()

    def on_upload(self, event):
        if not self.changes_allowed():
            return
        dialog = wx.FileDialog(self, "Choose files to upload", style=wx.FD_OPEN | wx.FD_MULTIPLE | wx.FD_FILE_MUST_EXIST)
        if dialog.ShowModal() == wx.ID_OK:
            paths = [Path(p) for p in dialog.Paths]
            self.run_task("Uploading...", lambda: [self.backend.upload(p, self.current_path) for p in paths],
                          lambda _: self.navigate(self.current_path))
        dialog.Destroy()

    def on_copy(self, event):
        paths = [item.path for item in self.selected()]
        if paths:
            self.clipboard = ("copy", paths)
            self.status.SetStatusText(f"Copied {len(paths)} items")

    def windows_path(self, remote_path: str) -> str:
        relative = PurePosixPath(remote_path).parts[1:]
        return str(PureWindowsPath(self.drive.mountpoint + "\\", *relative))

    def on_copy_windows_path(self, event):
        items = self.selected()
        if not items:
            return
        if not self.drive.mounted:
            wx.MessageBox("Connect the iCloud drive before copying a Windows path.",
                          "iCloud drive", wx.OK | wx.ICON_INFORMATION, self)
            return
        value = "\r\n".join(self.windows_path(item.path) for item in items)
        if wx.TheClipboard.Open():
            try:
                wx.TheClipboard.SetData(wx.TextDataObject(value))
                wx.TheClipboard.Flush()
                self.status.SetStatusText(f"Copied {len(items)} Windows path(s)")
            finally:
                wx.TheClipboard.Close()
        else:
            wx.MessageBox("The Windows clipboard is busy. Try again.", "Clipboard",
                          wx.OK | wx.ICON_ERROR, self)

    def on_cut(self, event):
        if not self.changes_allowed():
            return
        paths = [item.path for item in self.selected()]
        if paths:
            self.clipboard = ("cut", paths)
            self.status.SetStatusText(f"Cut {len(paths)} items")

    def on_paste(self, event):
        if not self.clipboard or not self.changes_allowed():
            return
        operation, paths = self.clipboard
        action = self.backend.copy if operation == "copy" else self.backend.move
        verb = "Copying" if operation == "copy" else "Moving"
        self.run_task(f"{verb}...", lambda: [action(p, self.current_path) for p in paths],
                      lambda _: self.navigate(self.current_path))

    def on_delete(self, event):
        items = self.selected()
        if not items or not self.changes_allowed():
            return
        names = "\n".join(item.name for item in items[:10])
        if wx.MessageBox(f"Move to iCloud Recently Deleted?\n\n{names}", "Confirm delete",
                         wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING, self) != wx.YES:
            return
        # Land on the item after the deleted ones, or the one before if they were last.
        doomed = {item.name for item in items}
        last = max(index for index, entry in enumerate(self.entries) if entry.name in doomed)
        survivors = ([entry.name for entry in self.entries[last + 1:] if entry.name not in doomed]
                     or [entry.name for entry in reversed(self.entries[:last])
                         if entry.name not in doomed])
        land_on = survivors[0] if survivors else None
        folder = self.current_path

        def work():
            # Apple can take many seconds to confirm, so the items leave the
            # list first and Apple is asked afterwards. If Apple refuses, the
            # backend puts them back and the error handler shows them again.
            pending = self.backend.begin_delete(item.path for item in items)
            wx.CallAfter(self.navigate, folder, False, land_on)
            self.backend.finish_delete(pending)
        self.run_task("Moving to Recently Deleted...", work,
                      lambda _: self.status.SetStatusText("Moved to Recently Deleted"),
                      lambda exc: self.navigate(folder))

    def on_rename(self, event):
        items = self.selected()
        if len(items) != 1 or not self.changes_allowed():
            return
        value = wx.GetTextFromUser("New name:", "Rename", items[0].name, self)
        if value and value != items[0].name:
            self.run_task("Renaming...", lambda: self.backend.rename(items[0].path, value),
                          lambda _: self.navigate(self.current_path, select=value))

    def on_new_folder(self, event):
        if not self.changes_allowed():
            return
        name = wx.GetTextFromUser("Folder name:", "New folder", parent=self)
        if name:
            self.run_task("Creating folder...", lambda: self.backend.mkdir(self.current_path, name),
                          lambda _: self.navigate(self.current_path, select=name))

    def on_select_all(self, event):
        for index in range(self.files.ItemCount):
            self.files.Select(index)

    def on_up(self, event):
        if self.current_path != "/":
            # Land on the folder just left, as File Explorer does.
            current = PurePosixPath(self.current_path)
            self.navigate(str(current.parent), select=current.name)

    def on_list_key(self, event):
        # Backspace goes up only from the list, so it still edits the path box.
        if event.GetKeyCode() == wx.WXK_BACK and not event.HasAnyModifiers():
            self.on_up(event)
        else:
            event.Skip()

    def on_context_menu(self, event):
        menu = wx.Menu()
        for label, handler in (("Open", self.on_open), ("Download to...", self.on_download),
                               ("Copy Windows path", self.on_copy_windows_path),
                               ("Copy", self.on_copy), ("Cut", self.on_cut),
                               ("Rename", self.on_rename), ("Delete", self.on_delete)):
            if not self.settings.writable_drive and handler in (
                    self.on_cut, self.on_rename, self.on_delete):
                continue
            item = menu.Append(wx.ID_ANY, label)
            self.Bind(wx.EVT_MENU, handler, item)
        self.PopupMenu(menu)
        menu.Destroy()

    def on_mount_drive(self, event):
        if not self.backend.connected:
            self.initial_sign_in(exit_on_cancel=False)
            return
        if not self.ensure_drive_letter_available():
            return
        mountpoint = self.settings.mountpoint
        self.run_task(f"Mounting iCloud as {mountpoint}...", self.drive.mount,
                      lambda _: self.on_mounted())

    def ensure_drive_letter_available(self) -> bool:
        if self.drive.mounted:
            return True
        if not drive_letter_in_use(self.settings.drive_letter):
            self.drive.configure(self.settings.mountpoint, self.settings.writable_drive)
            return True
        choices = available_drive_letters()
        if not choices:
            wx.MessageBox("No drive letters are currently available.", "iCloud drive",
                          wx.OK | wx.ICON_ERROR, self)
            return False
        dialog = wx.SingleChoiceDialog(
            self,
            f"{self.settings.mountpoint} is already in use. Choose another drive letter:",
            "iCloud drive letter", [f"{letter}:" for letter in choices])
        accepted = dialog.ShowModal() == wx.ID_OK
        if accepted:
            self.settings.drive_letter = dialog.GetStringSelection().rstrip(":")
            self.drive.configure(self.settings.mountpoint, self.settings.writable_drive)
            save_settings(self.settings)
            LOGGER.info("Selected replacement drive letter %s", self.settings.mountpoint)
        dialog.Destroy()
        return accepted

    def on_mounted(self, load_root=False):
        message = f"iCloud is mounted as {self.drive.mountpoint}"
        self.status.SetStatusText(message)
        self.tray.notify("Vellus", message)
        LOGGER.info(message)
        if load_root:
            self.navigate("/")
        self.retry_pending_uploads()

    def retry_pending_uploads(self, announce=False):
        """Upload changes that an earlier save could not deliver to iCloud."""
        if self.retrying_pending or not self.backend.connected:
            return
        if not self.drive.pending_count():
            if announce:
                wx.MessageBox("There are no changes waiting to be uploaded.", "iCloud drive",
                              wx.OK | wx.ICON_INFORMATION, self)
            return
        self.retrying_pending = True

        def finished(result):
            self.retrying_pending = False
            uploaded, failed = result
            LOGGER.info("Pending uploads: %d uploaded, %d failed", uploaded, failed)
            if failed:
                self.tray.notify("Vellus",
                    f"{failed} change(s) are still waiting to be uploaded. They are kept on "
                    "this PC and will be retried.")
            elif uploaded:
                self.tray.notify("Vellus", f"Uploaded {uploaded} waiting change(s).")

        def runner():
            try:
                result = self.drive.retry_pending()
            except Exception:
                LOGGER.exception("Retrying pending uploads failed")
                result = (0, self.drive.pending_count())
            wx.CallAfter(finished, result)
        threading.Thread(target=runner, daemon=True).start()

    def on_unmount_drive(self, event):
        mountpoint = self.drive.mountpoint
        self.run_task(f"Disconnecting {mountpoint}...", self.drive.unmount,
                      lambda _: self.status.SetStatusText(f"{mountpoint} disconnected"))

    def on_open_drive(self, event):
        if not self.drive.mounted:
            wx.MessageBox("Mount the iCloud drive first.", "iCloud drive", wx.OK | wx.ICON_INFORMATION, self)
            return
        os.startfile(self.drive.mountpoint + "\\")

    def on_clear_cache(self, event):
        if self.drive.mounted:
            wx.MessageBox("Disconnect the drive before clearing its cache.", "iCloud cache",
                          wx.OK | wx.ICON_INFORMATION, self)
            return
        def cleared(kept):
            self.status.SetStatusText("Cache cleared")
            if kept:
                wx.MessageBox(f"{kept} file(s) with changes not yet uploaded to iCloud were kept.",
                              "iCloud cache", wx.OK | wx.ICON_INFORMATION, self)
        self.run_task("Clearing cache...", clear_cache, cleared)

    def on_about(self, event):
        # A read-only text box, so a screen reader can review it line by line.
        dialog = wx.Dialog(self, title="About Vellus")
        text = wx.TextCtrl(dialog, value=ABOUT_TEXT, name="About Vellus",
                           style=wx.TE_MULTILINE | wx.TE_READONLY)
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(text, 1, wx.ALL | wx.EXPAND, 12)
        layout.Add(dialog.CreateButtonSizer(wx.CLOSE), 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 12)
        dialog.SetSizer(layout)
        dialog.SetClientSize((560, 360))
        dialog.SetEscapeId(wx.ID_CLOSE)
        dialog.Bind(wx.EVT_BUTTON, lambda e: dialog.EndModal(wx.ID_CLOSE), id=wx.ID_CLOSE)
        text.SetFocus()
        text.SetInsertionPoint(0)
        dialog.ShowModal()
        dialog.Destroy()

    def on_open_log(self, event):
        if not LOG_PATH.exists():
            wx.MessageBox("No log has been written yet.", "Vellus log",
                          wx.OK | wx.ICON_INFORMATION, self)
            return
        os.startfile(LOG_PATH)

    def on_settings(self, event):
        dialog = SettingsDialog(self, self.settings)
        if dialog.ShowModal() == wx.ID_OK:
            letter = dialog.drive_letter.GetStringSelection().rstrip(":")
            if len(letter) != 1 or not letter.isalpha():
                wx.MessageBox("Choose a drive letter.", "Invalid drive letter",
                              wx.OK | wx.ICON_ERROR, self)
            else:
                try:
                    candidate = Settings(username=self.settings.username, drive_letter=letter,
                        mount_automatically=dialog.auto_mount.Value,
                        writable_drive=dialog.writable.Value,
                        close_to_tray=dialog.close_to_tray.Value)
                    occupied_by_self = self.drive.mounted and self.drive.mountpoint == candidate.mountpoint
                    if drive_letter_in_use(letter) and not occupied_by_self:
                        raise RuntimeError(f"{candidate.mountpoint} is already in use. Choose another letter.")
                    # Only the drive letter and write access need the drive to be
                    # disconnected; every other setting applies straight away.
                    drive_changed = (candidate.mountpoint != self.drive.mountpoint
                                     or candidate.writable_drive != self.drive.writable)
                    if drive_changed and not self.drive.mounted:
                        self.drive.configure(candidate.mountpoint, candidate.writable_drive)
                    self.settings = candidate
                    save_settings(candidate)
                    self.apply_change_setting()
                    if drive_changed and self.drive.mounted and wx.MessageBox(
                            f"Saved. {self.drive.mountpoint} has to reconnect before the new "
                            "drive letter or write access applies to it. Close any files "
                            "you have open from the drive first.\n\nReconnect now?",
                            "Settings", wx.YES_NO | wx.ICON_QUESTION, self) == wx.YES:
                        def reconnect():
                            self.drive.unmount()
                            self.drive.configure(candidate.mountpoint, candidate.writable_drive)
                            self.drive.mount()
                        self.run_task("Reconnecting the drive...", reconnect,
                                      lambda _: self.on_mounted())
                except Exception as exc:
                    wx.MessageBox(str(exc), "Settings error", wx.OK | wx.ICON_ERROR, self)
        dialog.Destroy()

    def show_window(self):
        self.Show()
        self.Restore()
        self.Raise()

    def on_exit(self, event):
        self.exiting = True
        self.Close()

    def on_close(self, event):
        LOGGER.info("Close requested (exiting=%s, can_veto=%s, tray_installed=%s)",
                    self.exiting, event.CanVeto(), self.tray.IsIconInstalled())
        if not self.exiting and event.CanVeto() and self.settings.close_to_tray:
            self.Hide()
            event.Veto()
            if not self.tray_hint_shown:
                self.tray_hint_shown = True
                connection = (f"{self.drive.mountpoint} stays connected."
                              if self.drive.mounted else "The drive is not connected.")
                self.tray.notify("Vellus is still running",
                                 f"{connection} Use the notification area icon to reopen or exit.")
            return
        self.pending_timer.Stop()
        try:
            self.drive.unmount()
        finally:
            self.tray.RemoveIcon()
            self.tray.Destroy()
            event.Skip()


if __name__ == "__main__":
    if "--remove-user-data" in sys.argv:
        # Run by the uninstaller when the user asks for their data to go too.
        remove_user_data()
        os._exit(0)
    instance = None
    if "--self-test" not in sys.argv:
        instance = SingleInstance()
        if not instance.acquire():
            ctypes.windll.user32.MessageBoxW(
                None,
                "Vellus is already running. Look for it in the notification area.",
                "Vellus",
                0x40,
            )
            os._exit(0)
    migrated = migrate_legacy_data()
    setup_logging()
    if migrated:
        LOGGER.info("Moved settings and cache from the app's former name")
    LOGGER.info("Starting Vellus %s %s", VERSION,
                "packaged" if getattr(sys, "frozen", False) else "from source")
    install_crash_handler()
    if "--self-test" in sys.argv:
        _load_winfspy()
        check = wx.App(False)
        LOGGER.info("Self-test: sign-in on Apple's page available = %s",
                    embedded_sign_in_available())
        # wxPython starts native helper threads while importing. A hard exit is
        # intentional here so the windowless packaging check cannot linger.
        os._exit(0)
    app = wx.App(False)
    frame = MainFrame()
    if "--tray" not in sys.argv:
        frame.Show()
    app.MainLoop()
