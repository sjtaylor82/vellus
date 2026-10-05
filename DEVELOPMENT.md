# Developing Vellus

For people building or changing Vellus. Users only need the [README](README.md).

## Set up

Requires Windows 10 or 11, Python 3.11 and [WinFsp](https://winfsp.dev/).

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
```

## Run from source

```powershell
.\.venv\Scripts\python.exe app.py
```

Add `--tray` to start hidden in the notification area.

## Test

The tests use an in-memory stand-in for iCloud and never contact Apple.

```powershell
.\.venv\Scripts\python.exe -m unittest discover tests
```

## Build

```powershell
.\build.ps1
.\build-installer.ps1
```

`build.ps1` produces `dist\Vellus\Vellus.exe`. `build-installer.ps1` needs
[Inno Setup 6](https://jrsoftware.org/isinfo.php) and produces
`installer\Output\Vellus-Setup-<version>.exe` and `Vellus-<version>.zip`. The
version comes from `VERSION` in `config.py`.

`Vellus.exe --self-test` exits with code 0 when the packaged runtime, the
WinFsp bridge and the embedded browser component all load.

The installer runs the bundled, signed `installer\winfsp-*.msi` only when WinFsp
is absent. To bundle a newer WinFsp, put its `.msi` in `installer\` and update
the `WinFspMsi` line in `installer\Vellus.iss`.

## Where things live

- `app.py`: the window, tray icon and dialogs.
- `apple_signin.py`: the embedded sign-in on Apple's page.
- `backend.py`: everything that talks to iCloud, through `pyicloud`.
- `virtual_drive.py`: the WinFsp drive letter and its local cache.
- `governor.py`: rate limiting and backing off when Apple refuses requests.
- `config.py`: settings and file locations, under `%LOCALAPPDATA%\Vellus`.

`pyicloud` is pinned to an exact version in `requirements.txt` because
`backend.py` relies on some of its internals.
