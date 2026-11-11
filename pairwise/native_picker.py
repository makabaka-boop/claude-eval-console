"""Read positions from the real macOS Open panel for visible mouse selection.

The browser recorder only uses this after Chrome has opened its native picker.
Accessibility is read-only here; the caller performs real mouse clicks at the
returned coordinates and then checks that the webpage received the file.
"""
from __future__ import annotations

import ctypes
import subprocess
from pathlib import Path


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _Size(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class _AXReader:
    def __init__(self):
        self.cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.ax = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        self.cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        self.cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        self.cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long, ctypes.c_uint32]
        self.cf.CFStringGetCString.restype = ctypes.c_bool
        self.cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        self.cf.CFArrayGetCount.restype = ctypes.c_long
        self.cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        self.cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        self.cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
        self.cf.CFGetTypeID.restype = ctypes.c_ulong
        self.cf.CFStringGetTypeID.restype = ctypes.c_ulong
        self.cf.CFArrayGetTypeID.restype = ctypes.c_ulong
        self.cf.CFBooleanGetTypeID.restype = ctypes.c_ulong
        self.cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
        self.cf.CFBooleanGetValue.restype = ctypes.c_bool
        self.cf.CFRelease.argtypes = [ctypes.c_void_p]
        self.ax.AXUIElementCreateApplication.argtypes = [ctypes.c_int]
        self.ax.AXUIElementCreateApplication.restype = ctypes.c_void_p
        self.ax.AXUIElementCopyAttributeValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                           ctypes.POINTER(ctypes.c_void_p)]
        self.ax.AXUIElementCopyAttributeValue.restype = ctypes.c_int
        self.ax.AXValueGetValue.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
        self.ax.AXValueGetValue.restype = ctypes.c_bool
        self.owned = []

    def close(self):
        for pointer in reversed(self.owned):
            self.cf.CFRelease(pointer)
        self.owned.clear()

    def application(self, pid):
        value = self.ax.AXUIElementCreateApplication(pid)
        self.owned.append(value)
        return value

    def attr(self, element, name):
        key = self.cf.CFStringCreateWithCString(None, name.encode(), 0x08000100)
        self.owned.append(key)
        result = ctypes.c_void_p()
        if self.ax.AXUIElementCopyAttributeValue(element, key, ctypes.byref(result)) != 0:
            return None
        self.owned.append(result.value)
        return result.value

    def string(self, element, name):
        value = self.attr(element, name)
        if not value or self.cf.CFGetTypeID(value) != self.cf.CFStringGetTypeID():
            return ""
        buffer = ctypes.create_string_buffer(4096)
        if not self.cf.CFStringGetCString(value, buffer, len(buffer), 0x08000100):
            return ""
        return buffer.value.decode("utf-8", errors="replace")

    def array(self, element, name):
        value = self.attr(element, name)
        if not value or self.cf.CFGetTypeID(value) != self.cf.CFArrayGetTypeID():
            return []
        return [self.cf.CFArrayGetValueAtIndex(value, index)
                for index in range(self.cf.CFArrayGetCount(value))]

    def boolean(self, element, name):
        value = self.attr(element, name)
        if not value or self.cf.CFGetTypeID(value) != self.cf.CFBooleanGetTypeID():
            return None
        return bool(self.cf.CFBooleanGetValue(value))

    def center(self, element):
        position = self.attr(element, "AXPosition")
        size = self.attr(element, "AXSize")
        point, dimensions = _Point(), _Size()
        if not position or not size or not self.ax.AXValueGetValue(position, 1, ctypes.byref(point)):
            return None
        if not self.ax.AXValueGetValue(size, 2, ctypes.byref(dimensions)):
            return None
        if dimensions.width < 2 or dimensions.height < 2:
            return None
        return (point.x + min(dimensions.width / 2, 90), point.y + dimensions.height / 2)


def front_chrome_pid():
    result = subprocess.run(
        ["osascript",
         "-e", 'tell application "System Events" to set p to first application process whose frontmost is true',
         "-e", 'tell application "System Events" to return {name of p, unix id of p}'],
        text=True, capture_output=True, timeout=8, check=False,
    )
    parts = result.stdout.strip().rsplit(", ", 1)
    if result.returncode or len(parts) != 2 or parts[0] not in {"Google Chrome", "Google Chrome for Testing"}:
        raise RuntimeError("文件窗口已不在录屏浏览器前台，已停止选择")
    return int(parts[1])


def button_center(labels, browser_pid: int | None = None):
    """Return the center of a visible Chrome button with an exact label."""
    wanted = {str(label).strip().casefold() for label in labels if str(label).strip()}
    if not wanted:
        return None
    reader = _AXReader()
    try:
        app = reader.application(browser_pid or front_chrome_pid())
        windows = reader.array(app, "AXWindows")
        focused = reader.attr(app, "AXFocusedWindow")
        if focused:
            windows.insert(0, focused)
        seen_windows = set()
        for window in windows:
            if window in seen_windows:
                continue
            seen_windows.add(window)
            stack = [window]
            seen = set()
            while stack and len(seen) < 2000:
                element = stack.pop()
                if element in seen:
                    continue
                seen.add(element)
                if reader.string(element, "AXRole") == "AXButton":
                    texts = {
                        reader.string(element, attribute).strip().casefold()
                        for attribute in ("AXTitle", "AXDescription", "AXValue", "AXHelp")
                    }
                    if wanted.intersection(texts) and reader.boolean(element, "AXEnabled") is not False:
                        center = reader.center(element)
                        if center:
                            return center
                stack.extend(reader.array(element, "AXChildren"))
        return None
    finally:
        reader.close()


def picker_controls(path: Path, browser_pid: int | None = None):
    """Return centers of the exact file row and Open button, or None if not ready."""
    reader = _AXReader()
    try:
        app = reader.application(browser_pid or front_chrome_pid())
        windows = reader.array(app, "AXWindows")
        focused = reader.attr(app, "AXFocusedWindow")
        if focused:
            windows.insert(0, focused)
        wanted_url = path.resolve().as_uri()
        for window in windows:
            nodes = [(window, None)]
            seen = set()
            file_row = open_button = None
            panel = False
            while nodes and len(seen) < 1500:
                element, parent = nodes.pop()
                if element in seen:
                    continue
                seen.add(element)
                role = reader.string(element, "AXRole")
                identifier = reader.string(element, "AXIdentifier")
                if identifier == "open-panel":
                    panel = True
                if role == "AXButton" and identifier == "OKButton":
                    open_button = reader.center(element)
                if (reader.string(element, "AXURL") == wanted_url
                        or (role == "AXTextField" and reader.string(element, "AXValue") == path.name)):
                    file_row = reader.center(parent or element) or reader.center(element)
                nodes.extend((child, element) for child in reader.array(element, "AXChildren"))
            if panel and file_row and open_button:
                return file_row, open_button
        return None
    finally:
        reader.close()


def open_button_ready(browser_pid: int | None = None) -> bool:
    """Read the live Open button state after a file row is selected."""
    reader = _AXReader()
    try:
        app = reader.application(browser_pid or front_chrome_pid())
        for window in reader.array(app, "AXWindows"):
            stack = [window]
            seen = set()
            while stack and len(seen) < 1500:
                element = stack.pop()
                if element in seen:
                    continue
                seen.add(element)
                if reader.string(element, "AXIdentifier") == "open-panel":
                    panel = [element]
                    while panel:
                        child = panel.pop()
                        if reader.string(child, "AXIdentifier") == "OKButton":
                            return reader.boolean(child, "AXEnabled") is True
                        panel.extend(reader.array(child, "AXChildren"))
                    break
                stack.extend(reader.array(element, "AXChildren"))
        return False
    finally:
        reader.close()
