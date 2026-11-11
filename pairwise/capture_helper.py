"""Launch a macOS screen recording from the authorized Python.app process.

The evaluation console can run as a detached background process.  macOS does
not grant Screen Recording access to that detached process even when the same
Python executable is enabled in System Settings.  This helper is launched by
LaunchServices as Python.app, so the permission selected by the user is the
one macOS actually applies.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Optional, Tuple


def write_state(path: Path, **values) -> None:
    payload = {"updated_at": time.time(), **values}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def native_binary() -> Path:
    source = Path(__file__).with_name("native_capture.m")
    digest = hashlib.sha256(source.read_bytes() + source.with_name("native_terminal_capture.m").read_bytes()).hexdigest()[:16]
    return source.parent.parent / ".data" / "capture-bin" / f"capture-{digest}.dylib"


def ensure_native_binary() -> Path:
    binary = native_binary()
    if not binary.is_file():
        binary.parent.mkdir(parents=True, exist_ok=True)
        temporary = binary.with_suffix(".building")
        result = subprocess.run(
            ["/usr/bin/clang", "-O2", "-dynamiclib", "-fobjc-arc", "-framework", "Foundation", "-framework", "AVFoundation",
             "-framework", "CoreGraphics", "-framework", "CoreMedia", "-framework", "AppKit",
             "-framework", "ScreenCaptureKit", str(Path(__file__).with_name("native_capture.m")), "-o", str(temporary)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode:
            raise RuntimeError(f"无法构建本机录屏助手：{result.stderr[-3000:]}")
        temporary.replace(binary)
    return binary


def capture_command(capture_target: str, max_seconds: int, output: Path, root: Path) -> list[str]:
    if capture_target != "display" and not re.fullmatch(r"(?:window:[1-9]\d*|(?:cursor:)?-?\d+,-?\d+,[1-9]\d*,[1-9]\d*)", capture_target):
        raise ValueError("invalid capture target")
    if capture_target.startswith("window:") and int(capture_target.split(":", 1)[1]) > 0xFFFFFFFF:
        raise ValueError("invalid capture target")
    return [str(native_binary()), str(root), str(output), str(max_seconds), capture_target]


def _run_native(arguments: list[str]) -> int:
    # Keep capture in the already-authorized Python.app process. A separate
    # executable receives a different TCC identity and loses that permission.
    native = ctypes.CDLL(arguments[0])
    native.run_capture.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_char_p)]
    native.run_capture.restype = ctypes.c_int
    argv = (ctypes.c_char_p * len(arguments))(*(value.encode("utf-8") for value in arguments))
    return native.run_capture(len(arguments), argv)


def pointer_park_position(capture_target: str) -> Optional[Tuple[float, float]]:
    # Page recordings deliberately show the cursor. The browser driver has
    # already positioned it inside the page before capture starts; preserve it.
    if capture_target.startswith("cursor:"):
        return None
    match = re.fullmatch(r"(-?\d+),(-?\d+),(\d+),(\d+)", capture_target)
    if not match:
        return None
    left, top, width, height = (int(value) for value in match.groups())
    return left + width / 2, top + height / 2


def park_terminal_pointer(capture_target: str) -> None:
    # Cursor rendering is disabled for terminal recordings. Keep the actual
    # pointer away from screen edges, menus and Dock; never click or focus.
    position = pointer_park_position(capture_target)
    if position is None:
        return

    class CGPoint(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

    try:
        core_graphics = ctypes.CDLL(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
        )
        core_graphics.CGWarpMouseCursorPosition.argtypes = [CGPoint]
        core_graphics.CGWarpMouseCursorPosition.restype = ctypes.c_int
        core_graphics.CGWarpMouseCursorPosition(CGPoint(*position))
    except (OSError, AttributeError):
        pass


def main() -> int:
    if len(sys.argv) != 5:
        return 2
    root = Path(sys.argv[1])
    output = Path(sys.argv[2])
    max_seconds = max(1, int(sys.argv[3]))
    capture_target = sys.argv[4].strip()
    state_path = root / "capture-state.json"
    root.mkdir(parents=True, exist_ok=True)
    write_state(state_path, phase="starting")
    try:
        park_terminal_pointer(capture_target)
        # Native callbacks own readiness, done/stop, flush and process exit.
        return _run_native(capture_command(capture_target, max_seconds, output, root))
    except BaseException as exc:
        write_state(
            state_path,
            phase="error",
            error=str(exc),
            traceback=traceback.format_exc(limit=6),
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
