# Third-party notices

Vellus is released under the MIT License (see `LICENSE`). The Vellus installer
and program folder also contain the software listed here, each under its own
licence.

## WinFsp

WinFsp - Windows File System Proxy, Copyright (C) Bill Zissimopoulos
<https://github.com/winfsp/winfsp>

The Vellus installer includes the unmodified WinFsp 2.1 installer as released
by the WinFsp project, and runs it only if WinFsp is not already installed.
WinFsp is licensed under the GPLv3 with a special exception for Free/Libre and
Open Source Software, under which Vellus uses and redistributes it. WinFsp's
full licence is installed with it as `License.txt`; its source code is
available from the repository above.

## Libraries included in the Vellus program folder

| Component | Licence | Project |
|---|---|---|
| Python 3.11 runtime | PSF-2.0 | <https://www.python.org/> |
| wxPython and wxWidgets | wxWindows Library Licence | <https://wxpython.org/> |
| pyicloud | MIT | <https://github.com/timlaing/pyicloud> |
| winfspy | BSD | <https://github.com/Scille/winfspy> |
| keyring, keyrings.alt | MIT | <https://github.com/jaraco/keyring> |
| pywin32-ctypes | BSD-3-Clause | <https://github.com/enthought/pywin32-ctypes> |
| requests | Apache-2.0 | <https://github.com/psf/requests> |
| urllib3 | MIT | <https://github.com/urllib3/urllib3> |
| certifi | MPL-2.0 | <https://github.com/certifi/python-certifi> |
| charset-normalizer | MIT | <https://github.com/jawah/charset_normalizer> |
| idna | BSD-3-Clause | <https://github.com/kjd/idna> |
| cryptography | Apache-2.0 OR BSD-3-Clause | <https://github.com/pyca/cryptography> |
| cffi | MIT-0 | <https://github.com/python-cffi/cffi> |
| pycparser | BSD-3-Clause | <https://github.com/eliben/pycparser> |
| fido2 | BSD-2-Clause, with Apache-2.0 and MPL-2.0 parts | <https://github.com/Yubico/python-fido2> |
| srp | BSD | <https://github.com/cocagne/pysrp> |
| pydantic, pydantic-core, annotated-types, typing-inspection | MIT | <https://github.com/pydantic/pydantic> |
| typing_extensions | PSF-2.0 | <https://github.com/python/typing_extensions> |
| protobuf | BSD-3-Clause | <https://github.com/protocolbuffers/protobuf> |
| numpy | BSD-3-Clause and others | <https://numpy.org/> |
| tzdata | Apache-2.0 | <https://github.com/python/tzdata> |
| tzlocal | MIT | <https://github.com/regebro/tzlocal> |
| tinyhtml | MIT OR Apache-2.0 | <https://github.com/niklasf/python-tinyhtml> |
| six | MIT | <https://github.com/benjaminp/six> |
| more-itertools, jaraco.classes, jaraco.context, jaraco.functools, backports.tarfile, zipp | MIT | <https://github.com/jaraco> |
| importlib_metadata, packaging | Apache-2.0 (packaging: or BSD-2-Clause) | <https://github.com/pypa/packaging> |
| PyInstaller bootloader | GPL-2.0-or-later with a bootloader exception | <https://pyinstaller.org/> |

## Microsoft components

The program folder also contains Microsoft's Visual C++ runtime libraries
(`VCRUNTIME140.dll`, `VCRUNTIME140_1.dll`, `MSVCP140.dll`), redistributed under
Microsoft's terms as part of the Python runtime, and `WebView2Loader.dll`,
which wxPython includes to host the Microsoft Edge WebView2 control used for
signing in on Apple's page.

## Trademarks

iCloud and iCloud Drive are trademarks of Apple Inc. Vellus is not affiliated
with, endorsed by, or supported by Apple Inc.
