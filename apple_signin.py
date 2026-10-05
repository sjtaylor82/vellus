"""Sign in on Apple's own web page, shown in an embedded Edge (WebView2) window.

The user types their password into Apple's page, never into this app. Once
Apple has signed them in, the browser session's cookies are read through
WebView2's DevTools interface and handed to pyicloud.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import threading
from ctypes import wintypes
from typing import Callable

import requests
import wx
import wx.html2

LOGGER = logging.getLogger(__name__)

SIGN_IN_URL = "https://www.icloud.com/"
VALIDATE_URL = "https://setup.icloud.com/setup/ws/1/validate"
SESSION_COOKIE = "X-APPLE-WEBAUTH-TOKEN"
COOKIE_DOMAINS = ("icloud.com", "apple.com")

# Positions of two methods in the ICoreWebView2 COM interface (WebView2.idl).
_CALL_DEVTOOLS_PROTOCOL_METHOD = 36
_GET_BROWSER_PROCESS_ID = 37
_S_OK, _E_NOINTERFACE = 0, ctypes.c_long(0x80004002).value
_IID_IUNKNOWN = "00000000-0000-0000-c000-000000000046"
_IID_COMPLETED_HANDLER = "5c4889f0-5ef6-4c5a-952c-d8f1b92d0574"

_HRESULT = ctypes.c_long
_QUERY = ctypes.WINFUNCTYPE(_HRESULT, ctypes.c_void_p, ctypes.c_void_p,
                            ctypes.POINTER(ctypes.c_void_p))
_REFCOUNT = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
_INVOKE = ctypes.WINFUNCTYPE(_HRESULT, ctypes.c_void_p, _HRESULT, ctypes.c_wchar_p)
_CALL = ctypes.WINFUNCTYPE(_HRESULT, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                           ctypes.c_void_p)
_GET_PID = ctypes.WINFUNCTYPE(_HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))


class _Guid(ctypes.Structure):
    _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16),
                ("d", ctypes.c_ubyte * 8)]

    def __str__(self) -> str:
        tail = bytes(self.d).hex()
        return f"{self.a:08x}-{self.b:04x}-{self.c:04x}-{tail[:4]}-{tail[4:]}"


class _Handler(ctypes.Structure):
    _fields_ = [("vtable", ctypes.POINTER(ctypes.c_void_p))]


_live_handlers: dict[int, tuple] = {}


def _process_name(pid: int) -> str:
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buffer, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(wintypes.HANDLE(handle), 0, buffer,
                                               ctypes.byref(size)):
            return buffer.value.rsplit("\\", 1)[-1].lower()
        return ""
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle))


def call_devtools(webview: wx.html2.WebView, method: str, params: dict,
                  done: Callable[[dict | None], None]) -> None:
    """Run one DevTools Protocol command in the WebView2 behind ``webview``.

    ``done`` is called on the UI thread with the decoded reply, or None if the
    command failed. Must itself be called on the UI thread.
    """
    native = webview.GetNativeBackend()
    address = int(native) if native else 0
    if not address:
        raise RuntimeError("The embedded browser is not ready.")
    vtable = ctypes.cast(address, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents

    # Confirm the interface is laid out as expected before calling into it:
    # the neighbouring slot must return the id of a real WebView2 process.
    pid = ctypes.c_uint32(0)
    result = _GET_PID(vtable[_GET_BROWSER_PROCESS_ID])(address, ctypes.byref(pid))
    if result != _S_OK or _process_name(pid.value) != "msedgewebview2.exe":
        raise RuntimeError("This version of the embedded browser is not supported.")

    handler = _Handler()
    key = ctypes.addressof(handler)

    def query_interface(this, iid, out):
        if str(ctypes.cast(iid, ctypes.POINTER(_Guid)).contents) in (
                _IID_IUNKNOWN, _IID_COMPLETED_HANDLER):
            out[0] = this
            return _S_OK
        out[0] = None
        return _E_NOINTERFACE

    def invoke(this, error, reply):
        try:
            done(json.loads(reply) if error == _S_OK and reply else None)
        except Exception:
            LOGGER.exception("DevTools reply handler failed")
        return _S_OK

    # WebView2 holds this object beyond the reply, so it is reference counted
    # and only let go once the last reference is released. The release is
    # deferred because it arrives while one of these very callbacks is running.
    references = [1]

    def add_ref(this):
        references[0] += 1
        return references[0]

    def release(this):
        references[0] -= 1
        if references[0] == 0:
            wx.CallAfter(_live_handlers.pop, key, None)
        return max(references[0], 0)

    functions = (_QUERY(query_interface), _REFCOUNT(add_ref), _REFCOUNT(release),
                 _INVOKE(invoke))
    table = (ctypes.c_void_p * 4)(*(ctypes.cast(f, ctypes.c_void_p) for f in functions))
    handler.vtable = ctypes.cast(table, ctypes.POINTER(ctypes.c_void_p))
    _live_handlers[key] = (handler, table, functions)
    result = _CALL(vtable[_CALL_DEVTOOLS_PROTOCOL_METHOD])(
        address, method, json.dumps(params), ctypes.byref(handler))
    release(None)
    if result != _S_OK:
        raise RuntimeError(f"The embedded browser refused the request ({result & 0xFFFFFFFF:#x}).")


def read_cookies(webview: wx.html2.WebView, done: Callable[[list[dict]], None]) -> None:
    """Fetch the browser's cookies, including ones page scripts cannot see."""
    call_devtools(webview, "Network.getAllCookies", {},
                  lambda reply: done((reply or {}).get("cookies", [])))


def apple_cookies(cookies: list[dict]) -> list[dict]:
    return [cookie for cookie in cookies
            if cookie.get("domain", "").lstrip(".").endswith(COOKIE_DOMAINS)]


def check_session(cookies: list[dict], user_agent: str) -> dict | None:
    """Ask Apple whether these cookies are a signed-in session.

    Returns {"apple_id", "trusted"} once the password step is complete, else None.
    """
    jar = requests.cookies.RequestsCookieJar()
    for cookie in cookies:
        jar.set(cookie["name"], cookie["value"], domain=cookie["domain"],
                path=cookie.get("path", "/"))
    try:
        response = requests.post(
            VALIDATE_URL, data="null", cookies=jar, timeout=20,
            headers={"User-Agent": user_agent, "Origin": "https://www.icloud.com",
                     "Referer": "https://www.icloud.com/", "Accept": "application/json"})
        if response.status_code != 200:
            return None
        data = response.json()
    except (requests.RequestException, ValueError):
        return None
    info = data.get("dsInfo") or {}
    apple_id = info.get("appleId") or info.get("primaryEmail")
    if not apple_id or data.get("hsaChallengeRequired"):
        return None
    return {"apple_id": apple_id, "trusted": bool(data.get("hsaTrustedBrowser"))}


def embedded_sign_in_available() -> bool:
    return wx.html2.WebView.IsBackendAvailable(wx.html2.WebViewBackendEdge)


class AppleSignInDialog(wx.Dialog):
    """Shows Apple's sign-in page and finishes once Apple reports a trusted session."""

    POLL_MS = 3000

    def __init__(self, parent, profile_dir: str):
        super().__init__(parent, title="Sign in to iCloud on Apple's page",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.cookies: list[dict] = []
        self.apple_id = ""
        self.user_agent = ""
        self._checking = False
        self._closed = False

        self.status = wx.StaticText(self, label="Loading Apple's sign-in page...")
        self.cancel_button = cancel = wx.Button(self, wx.ID_CANCEL, "&Cancel")
        # WebView2 reads its profile location from this variable. Keeping the
        # profile in the app's own folder lets Sign out remove it.
        os.environ["WEBVIEW2_USER_DATA_FOLDER"] = profile_dir
        self.browser = wx.html2.WebView.New(self, url=SIGN_IN_URL,
                                            backend=wx.html2.WebViewBackendEdge,
                                            name="Apple sign-in page")

        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(self.status, 0, wx.ALL, 10)
        layout.Add(cancel, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        layout.Add(self.browser, 1, wx.EXPAND)
        self.SetSizer(layout)
        self.SetSize((760, 820))
        self.CentreOnParent()

        cancel.Bind(wx.EVT_BUTTON, lambda event: self.finish(wx.ID_CANCEL))
        self.Bind(wx.EVT_CHAR_HOOK, self.on_key)
        self.Bind(wx.html2.EVT_WEBVIEW_LOADED, self.on_loaded, self.browser)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_poll, self.timer)
        self.timer.Start(self.POLL_MS)

    def on_loaded(self, event):
        if not self.user_agent:
            self.user_agent = self.browser.GetUserAgent()
            self.status.SetLabel("Waiting for you to sign in on Apple's page.")
            self.browser.SetFocus()

    def on_key(self, event):
        if event.GetKeyCode() == wx.WXK_F6:
            if self.browser.HasFocus():
                self.cancel_button.SetFocus()
            else:
                self.browser.SetFocus()
        else:
            event.Skip()

    def on_close(self, event):
        self.finish(wx.ID_CANCEL)

    def finish(self, code):
        if self._closed:
            return
        self._closed = True
        self.timer.Stop()
        self.EndModal(code)

    def on_poll(self, event):
        if self._checking or self._closed or not self.user_agent:
            return
        try:
            read_cookies(self.browser, self.on_cookies)
        except RuntimeError as exc:
            LOGGER.warning("Could not read the browser session: %s", exc)
            self.status.SetLabel(str(exc))
            self.timer.Stop()

    def on_cookies(self, cookies):
        cookies = apple_cookies(cookies)
        if self._closed or not any(cookie["name"] == SESSION_COOKIE for cookie in cookies):
            return
        self._checking = True
        user_agent = self.user_agent

        def worker():
            result = check_session(cookies, user_agent)
            wx.CallAfter(self.on_checked, cookies, result)
        threading.Thread(target=worker, daemon=True).start()

    def on_checked(self, cookies, result):
        self._checking = False
        if self._closed or not result:
            return
        if not result["trusted"]:
            self.status.SetLabel("Signed in. If Apple asks whether to trust this browser, "
                                 "choose Trust to finish.")
            return
        self.cookies, self.apple_id = cookies, result["apple_id"]
        self.finish(wx.ID_OK)
