# Vellus

Vellus gives your iCloud Drive a drive letter on Windows, so your files appear
in File Explorer and in every program's Open and Save dialogs. It is built to
work well with screen readers and the keyboard.

Vellus is an independent, open-source project. It is not affiliated with,
endorsed by, or supported by Apple Inc.

## Before you start

- Vellus reaches iCloud through an unofficial interface that Apple does not
  publish or support. It may stop working without notice, and Apple could
  restrict an account that uses it. Keep backups of anything important.
- You need Windows 10 or 11.
- On your iPhone, under Settings, your name, iCloud, **Access iCloud Data on
  the Web** must be on. Vellus has only been tested with Advanced Data
  Protection turned off.

## Install

1. Download the latest `Vellus` zip from the
   [releases page](https://github.com/sjtaylor82/vellus/releases) and extract it.
2. Run `Vellus-Setup`. Windows may warn that the publisher is unknown, because
   the installer is not yet code-signed.
3. If Setup says it needs to install WinFsp, allow it. WinFsp is the free,
   open-source component that provides the drive letter.

## Sign in

Vellus opens Apple's own sign-in page. You type your password there, not into
Vellus, which never sees or stores it.

- Tick **Keep me signed in**. With it, Apple should keep you signed in for
  about 30 days after you last used Vellus; without it, for about half an hour.
- If Apple asks whether to trust the browser, choose **Trust**.

When Apple eventually signs you out, Vellus tells you and offers to open the
sign-in page again.

## Use

Once you are signed in, iCloud Drive appears as drive `I:`. Use it like any
other drive. Files download when you open them.

The Vellus window lists the same files and is a simple file manager in its own
right. Closing it leaves Vellus running in the notification area, with the
drive still connected. To quit, press Ctrl+Q or choose Exit from the
notification area icon.

Deleting anything, from the drive or the window, moves it to **Recently
Deleted** in iCloud, where it can be recovered for 30 days.

## Keyboard commands in the Vellus window

- Enter: open a file or folder
- Backspace or Alt+Up: go up to the parent folder
- F5: refresh
- Ctrl+Shift+N: new folder
- F2: rename
- Delete: delete
- Ctrl+U: upload files
- Ctrl+D: download the selected items
- Ctrl+C, Ctrl+X, Ctrl+V: copy, cut, paste
- Ctrl+Shift+C: copy the selected item's drive path
- Ctrl+A: select all
- Ctrl+comma: settings
- Alt+P: move to the path box
- Alt+L: move to the file list
- Ctrl+Q: quit

## Settings

Open Settings with Ctrl+comma.

- **Drive letter**: `I:` unless you choose another.
- **Mount drive letter upon launch**: connect the drive as soon as Vellus starts.
- **Permit Write Access**: when off, Vellus is read-only. Nothing in iCloud can
  be changed from the drive or the window.
- **Keep running when closed**: when off, closing the window disconnects the
  drive and quits.

To start Vellus with Windows, press Win+R, type `shell:startup`, and put a
shortcut to Vellus in the folder that opens.

## Sign out and uninstall

**Sign out**, on the Drive menu, ends the session with Apple and removes the
saved session and all cached copies of your files from this PC. Nothing in
iCloud is deleted.

Uninstall Vellus from Windows Settings, under Apps. The uninstaller offers to
remove your Vellus settings and cached files too. WinFsp is left in place.

## If something goes wrong

Choose **Open log** from the notification area icon's menu. The log records
what Vellus did and any errors, without passwords. Include the relevant lines
when you [report a problem](https://github.com/sjtaylor82/vellus/issues), after
checking them for file names you would rather not share.

## Licence and acknowledgements

Vellus is released under the [MIT License](LICENSE).

The drive letter is provided by WinFsp:

> WinFsp - Windows File System Proxy, Copyright (C) Bill Zissimopoulos
> <https://github.com/winfsp/winfsp>

Vellus is built on [pyicloud](https://github.com/timlaing/pyicloud),
[winfspy](https://github.com/Scille/winfspy) and
[wxPython](https://wxpython.org/). The licences of everything in the installer
are listed in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

iCloud and iCloud Drive are trademarks of Apple Inc.

Building Vellus from source is covered in [DEVELOPMENT.md](DEVELOPMENT.md).
