#!/usr/bin/env python3
# Copyright (C) 2026 gaojizhou
# SPDX-License-Identifier: AGPL-3.0-only
"""A small, dependency-free MCP server for HiSilicon/Hipcam IP cameras.

Credentials are deliberately read only from environment variables.  The server
speaks MCP JSON-RPC over stdio, so it can be launched by Claude Desktop, Codex,
or another MCP host.
"""

import base64
import io
import json
import os
import re
import shutil
import sys
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


# Camera IP and password are read ONLY from these module globals. They are
# seeded from environment variables at startup and may be updated at runtime by
# the set_camera_credentials tool when the user supplies them in the
# conversation. No other code path accepts them, and the server never contacts
# the camera except through these values.
HOST = os.environ.get("CAMERA_HOST", "")
USERNAME = os.environ.get("CAMERA_USERNAME", "admin")
PASSWORD = os.environ.get("CAMERA_PASSWORD", "")
HTTP_TIMEOUT = float(os.environ.get("CAMERA_TIMEOUT_SECONDS", "10"))
MCP_DIR = os.path.dirname(os.path.abspath(__file__))
YOLO_MODEL = os.environ.get("CAMERA_YOLO_MODEL", os.path.join(MCP_DIR, "yolo11n.pt"))
MAX_CENTERING_MOVES = 12
MIN_SEARCH_MOVE_STEPS = 4
PTZ_SETTLE_SECONDS = 2.0
PTZ_SETTLE_TIMEOUT_SECONDS = 8.0
PTZ_STABLE_FRAME_INTERVAL_SECONDS = 0.4
PTZ_SECONDS_PER_STEP = 0.25
NATIVE_TRACKING_CENTER_TIMEOUT_SECONDS = float(os.environ.get("CAMERA_TRACKING_CENTER_TIMEOUT_SECONDS", "20"))
NATIVE_TRACKING_SAMPLE_INTERVAL_SECONDS = 0.5
_detector: Any | None = None
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05", "2024-10-07")
DEFAULT_PROTOCOL_VERSION = "2024-11-05"


def camera_url(path: str, params: dict[str, str] | None = None) -> str:
    query = urllib.parse.urlencode(params or {})
    return f"http://{HOST}{path}" + (f"?{query}" if query else "")


def store_credentials(host: str, username: str, password: str) -> None:
    """Write the camera IP and credentials into the module globals every camera
    request reads. This is the only supported way to provide credentials that
    arrive through the conversation; camera access consults these globals only.
    """
    global HOST, USERNAME, PASSWORD
    HOST, USERNAME, PASSWORD = host, username, password


def request(path: str, params: dict[str, str] | None = None, method: str = "GET") -> tuple[bytes, str]:
    if not HOST:
        raise ValueError("camera IP is not configured; set CAMERA_HOST or call set_camera_credentials")
    if not PASSWORD:
        raise ValueError("camera password is not configured; set CAMERA_PASSWORD or call set_camera_credentials")
    token = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
    encoded = urllib.parse.urlencode(params or {}).encode() if method == "POST" else None
    url = camera_url(path, None if method == "POST" else params)
    req = urllib.request.Request(url, data=encoded, headers={"Authorization": f"Basic {token}"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
            return response.read(), response.headers.get_content_type()
    except urllib.error.HTTPError as error:
        if error.code == 401:
            raise ValueError("camera rejected the configured username or password") from error
        raise RuntimeError(f"camera returned HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"cannot connect to camera at {HOST}: {error.reason}") from error


def parse_firmware_variables(body: bytes) -> dict[str, str]:
    """Parse the simple JavaScript variable format returned by Hi3510 CGI."""
    text = body.decode("utf-8", errors="replace")
    return dict(re.findall(r'var\s+([A-Za-z0-9_]+)\s*=\s*"([^"]*)"', text))


def set_native_smart_tracking(enabled: bool) -> None:
    """Set native SmartTrack and fail if firmware readback disagrees."""
    expected = "1" if enabled else "0"
    request(
        "/cgi-bin/hi3510/param.cgi",
        {"cmd": "setsmartrackattr", "-smartrack_enable": expected},
        method="POST",
    )
    body, _ = request("/cgi-bin/hi3510/param.cgi", {"cmd": "getsmartrackattr"})
    actual = parse_firmware_variables(body).get("smartrack_enable")
    if actual != expected:
        action = "enable" if enabled else "disable"
        raise RuntimeError(f"the camera did not {action} native smart tracking")


def prepare_person_tracking() -> None:
    """Enable native person tracking and hide firmware detection rectangles.

    Preserve the camera's existing smart-recognition type and threshold while
    changing only the rectangle overlay. Read both settings back so find_person
    never silently continues after a firmware that ignored either command.
    """
    smd_attr, _ = request("/cgi-bin/hi3510/param.cgi", {"cmd": "getsmdattr"})
    smd_ex, _ = request("/cgi-bin/hi3510/param.cgi", {"cmd": "getsmdex"})
    settings = {**parse_firmware_variables(smd_attr), **parse_firmware_variables(smd_ex)}
    smd_type = settings.get("smd_type", "0")
    smd_threshold = settings.get("smd_gthresh", settings.get("smd_threshold", "34"))

    set_native_smart_tracking(True)
    request(
        "/cgi-bin/hi3510/param.cgi",
        {"cmd": "setsmdex", "-smd_rect": "0", "-smd_type": smd_type, "-smd_gthresh": smd_threshold},
        method="POST",
    )
    rectangle_body, _ = request("/cgi-bin/hi3510/param.cgi", {"cmd": "getsmdex"})
    rectangle = parse_firmware_variables(rectangle_body)
    if rectangle.get("smd_rect") != "0":
        raise RuntimeError("the camera did not disable the recognition-object rectangle")


def capture_rtsp_frame(quality: str) -> bytes:
    """Return one JPEG frame. Stream 11 is main and stream 12 is sub."""
    if not HOST:
        raise ValueError("camera IP is not configured; set CAMERA_HOST or call set_camera_credentials")
    if not PASSWORD:
        raise ValueError("camera password is not configured; set CAMERA_PASSWORD or call set_camera_credentials")
    stream = "11" if quality == "main" else "12"
    username = urllib.parse.quote(USERNAME, safe="")
    password = urllib.parse.quote(PASSWORD, safe="")
    rtsp_url = f"rtsp://{username}:{password}@{HOST}:554/{stream}"
    ffmpeg = find_ffmpeg()
    try:
        capture = subprocess.run(
            [ffmpeg, "-loglevel", "error", "-rtsp_transport", "tcp", "-i", rtsp_url, "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
            capture_output=True, timeout=HTTP_TIMEOUT + 10, check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"could not capture an RTSP frame from stream /{stream}") from error
    if not capture.stdout:
        raise RuntimeError(f"stream /{stream} returned an empty frame")
    return capture.stdout


def find_ffmpeg() -> str:
    """Locate FFmpeg without assuming a Linux-specific installation path."""
    configured = os.environ.get("CAMERA_FFMPEG_PATH")
    if configured:
        path = os.path.expanduser(configured)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
        raise RuntimeError(f"CAMERA_FFMPEG_PATH is not an executable file: {configured}")
    discovered = shutil.which("ffmpeg")
    if discovered:
        return discovered
    for candidate in ("/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError("FFmpeg was not found; install it or set CAMERA_FFMPEG_PATH")


def move_ptz(direction: str, speed: int, steps: int) -> None:
    # This firmware labels vertical CGI actions from the scene's perspective:
    # its raw "down" action tilts the lens up, and raw "up" tilts it down.
    # Horizontal actions already match the user-facing camera direction.
    codes = {"up": "down", "down": "up", "left": "left", "right": "right", "home": "home"}
    if direction not in codes:
        raise ValueError("unsupported direction")
    if direction == "home":
        request("/cgi-bin/hi3510/ptzctrl.cgi", {"-step": "0", "-act": "home", "-speed": str(speed)})
        return
    # This firmware treats -step=0 as a continuous move. Bound every command
    # ourselves, then explicitly stop it before analysing another frame.
    request("/cgi-bin/hi3510/ptzctrl.cgi", {"-step": "0", "-act": codes[direction], "-speed": str(speed)})
    time.sleep(PTZ_SECONDS_PER_STEP * steps)
    request("/cgi-bin/hi3510/ptzctrl.cgi", {"-step": "0", "-act": "stop", "-speed": str(speed)})


def wait_for_ptz_settle() -> None:
    """Wait for consecutive stable video frames before running detection.

    The camera exposes no PTZ-position/status endpoint. If it remains in
    motion beyond the bounded timeout, issue its documented stop CGI before
    returning, so later detection never evaluates a moving scene.
    """
    from PIL import Image, ImageChops, ImageStat

    def grayscale_frame() -> Image.Image:
        return Image.open(io.BytesIO(capture_rtsp_frame("sub"))).convert("L").resize((80, 45))

    time.sleep(PTZ_SETTLE_SECONDS)
    previous = grayscale_frame()
    stable_pairs = 0
    deadline = time.monotonic() + PTZ_SETTLE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(PTZ_STABLE_FRAME_INTERVAL_SECONDS)
        current = grayscale_frame()
        difference = ImageStat.Stat(ImageChops.difference(previous, current)).mean[0]
        if difference < 3.0:
            stable_pairs += 1
            if stable_pairs >= 2:
                return
        else:
            stable_pairs = 0
        previous = current
    request("/cgi-bin/hi3510/ptzctrl.cgi", {"-step": "0", "-act": "stop", "-speed": "1"})
    time.sleep(PTZ_SETTLE_SECONDS)


def select_target(matches: list[dict[str, Any]]) -> dict[str, Any] | None:
    return max(matches, key=lambda match: match["confidence"], default=None)


def target_offset(target: dict[str, Any]) -> tuple[float, float]:
    x1, y1, x2, y2 = target["box_xyxy"]
    width, height = target.get("image_size", [640, 360])
    dx = ((x1 + x2) / 2 / width) - 0.5
    dy = ((y1 + y2) / 2 / height) - 0.5
    target["center_offset"] = {"x": round(dx, 3), "y": round(dy, 3)}
    return dx, dy


def center_target(target: dict[str, Any], tolerance: float, speed: int, reverse_x: bool, reverse_y: bool) -> tuple[bool, str | None, float]:
    """Move one axis and return its pre-move error for feedback calibration."""
    dx, dy = target_offset(target)
    if abs(dx) <= tolerance and abs(dy) <= tolerance:
        return True, None, 0.0
    axis = "x" if abs(dx) >= abs(dy) else "y"
    error = dx if axis == "x" else dy
    if axis == "x":
        direction = "right" if error > 0 else "left"
        if reverse_x:
            direction = "left" if direction == "right" else "right"
    else:
        direction = "down" if error > 0 else "up"
        if reverse_y:
            direction = "up" if direction == "down" else "down"
    # Once a target exists, use one small axis-only correction per frame.
    move_ptz(direction, speed, 1)
    wait_for_ptz_settle()
    return False, axis, abs(error)


def detect_objects(jpeg: bytes, targets: list[str], colors: list[str], confidence: float) -> list[dict[str, Any]]:
    """Run YOLO lazily so ordinary camera tools have no ML startup cost."""
    global _detector
    try:
        import numpy as np
        from PIL import Image
        from ultralytics import YOLO
    except ImportError as error:
        raise RuntimeError(
            "YOLO is not installed; install this MCP's dependencies from its requirements.txt"
        ) from error
    if _detector is None:
        _detector = YOLO(YOLO_MODEL)  # Downloads the selected official weight file on first use.
    image = Image.open(io.BytesIO(jpeg)).convert("RGB")
    result = _detector(image, conf=confidence, verbose=False)[0]
    if result.boxes is None:
        return []
    wanted = {target.lower() for target in targets}
    requested_colors = {color.lower() for color in colors}
    matches: list[dict[str, Any]] = []
    for box in result.boxes:
        class_id = int(box.cls[0].item())
        label = str(result.names[class_id])
        if wanted and label.lower() not in wanted:
            continue
        x1, y1, x2, y2 = (round(float(value), 1) for value in box.xyxy[0].tolist())
        # Use the interior of the detected object to reduce surrounding pavement
        # and background. This is an appearance filter, not a trained color model.
        width, height = x2 - x1, y2 - y1
        inset = image.crop((x1 + width * 0.15, y1 + height * 0.15, x2 - width * 0.15, y2 - height * 0.15))
        pixels = np.asarray(inset, dtype=np.float32)
        brightness = float(pixels.mean(axis=2).mean()) if pixels.size else 255.0
        dark_ratio = float((pixels.mean(axis=2) < 75).mean()) if pixels.size else 0.0
        appearance = "black" if brightness < 105 and dark_ratio > 0.35 else "not_black"
        if requested_colors and appearance not in requested_colors:
            continue
        matches.append({"label": label, "appearance": appearance, "appearance_metrics": {"mean_brightness": round(brightness, 1), "dark_pixel_ratio": round(dark_ratio, 3)}, "confidence": round(float(box.conf[0].item()), 3), "box_xyxy": [x1, y1, x2, y2], "image_size": [image.width, image.height]})
    return matches


def detect_current_view(targets: list[str], colors: list[str], confidence: float, attempts: int = 3) -> list[dict[str, Any]]:
    """Require only one hit across a few frames to handle low-light variation."""
    for attempt in range(attempts):
        matches = detect_objects(capture_rtsp_frame("sub"), targets, colors, confidence)
        if matches:
            return matches
        if attempt + 1 < attempts:
            time.sleep(0.15)
    return []


def wait_for_native_person_center(confidence: float, tolerance: float) -> tuple[dict[str, Any], bytes, int]:
    """Wait for firmware SmartTrack, then verify centering on the returned frame.

    No PTZ command is issued here: once YOLO has found a person, native camera
    tracking has exclusive control of the motors. The final main-stream JPEG is
    itself checked so the image returned to the client is known to be centred.
    """
    deadline = time.monotonic() + NATIVE_TRACKING_CENTER_TIMEOUT_SECONDS
    observations = 0
    while time.monotonic() < deadline:
        observations += 1
        sub_matches = detect_objects(capture_rtsp_frame("sub"), ["person"], [], confidence)
        target = select_target(sub_matches)
        if target is not None:
            dx, dy = target_offset(target)
            if abs(dx) <= tolerance and abs(dy) <= tolerance:
                high_res = capture_rtsp_frame("main")
                main_target = select_target(detect_objects(high_res, ["person"], [], confidence))
                if main_target is not None:
                    main_dx, main_dy = target_offset(main_target)
                    if abs(main_dx) <= tolerance and abs(main_dy) <= tolerance:
                        return main_target, high_res, observations
        time.sleep(NATIVE_TRACKING_SAMPLE_INTERVAL_SECONDS)
    raise RuntimeError(
        f"native SmartTrack did not centre the detected person within "
        f"{NATIVE_TRACKING_CENTER_TIMEOUT_SECONDS:g} seconds"
    )


TOOLS: list[dict[str, Any]] = [
    {"name": "set_camera_credentials", "description": "Store the camera IP, username, and password into this server's internal global variables so the other tools can reach the camera. Use this only when the user provides these values in the conversation and they are not already set through environment variables. The server reads the camera IP and password ONLY from these globals and never echoes the password back. Do not use these values to contact the camera yourself; always go through this server's tools.", "inputSchema": {"type": "object", "properties": {"host": {"type": "string", "description": "Camera LAN IP or hostname the user provided."}, "password": {"type": "string", "description": "Camera device password the user provided."}, "username": {"type": "string", "default": "admin", "description": "Camera login username; defaults to admin."}}, "required": ["host", "password"]}},
    {"name": "camera_info", "description": "Read the camera model, hardware version, firmware, and storage status.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "snapshot", "description": "Capture a current JPEG still image from the selected RTSP stream. If the AI itself recognizes a sought person in this image, it must not stop while that person is off-centre: use bounded ptz_move corrections and fresh snapshots until the person is at frame centre.", "inputSchema": {"type": "object", "properties": {"quality": {"type": "string", "enum": ["main", "sub"], "default": "sub", "description": "Main is stream /11; sub is stream /12."}}}},
    {"name": "rtsp_url", "description": "Return an RTSP stream URL without exposing credentials. Use it with a client configured with the same camera credentials if requested.", "inputSchema": {"type": "object", "properties": {"quality": {"type": "string", "enum": ["main", "sub"], "default": "sub"}}}},
    {"name": "ptz_move", "description": "Disable native SmartTrack, then move the PTZ by a bounded number of steps. It does not change the recognition-object rectangle setting. Every directional move is followed by an explicit stop. This changes camera settings and physical position.", "inputSchema": {"type": "object", "properties": {"direction": {"type": "string", "enum": ["up", "down", "left", "right", "home"]}, "speed": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5}, "steps": {"type": "integer", "minimum": 1, "maximum": 30, "default": 1}}, "required": ["direction"]}},
    {"name": "find_person", "description": "Preferred tool whenever the user asks to find a person. It first enables native SmartTrack and disables the recognition-object rectangle. YOLO checks the view and moves the PTZ search plan only while no person is visible. As soon as YOLO finds a person, manual PTZ corrections stop and the tool waits for native SmartTrack to centre them. The exact final high-resolution image is YOLO-verified as centred, then native SmartTrack is disabled before the image is returned.", "inputSchema": {"type": "object", "properties": {"moves": {"type": "array", "items": {"type": "string", "enum": ["up", "down", "left", "right"]}, "maxItems": 24, "default": ["left", "left", "left", "left"]}, "speed": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3}, "steps_per_move": {"type": "integer", "minimum": 1, "maximum": 30, "default": 4}, "confidence": {"type": "number", "minimum": 0.1, "maximum": 0.95, "default": 0.45}, "center_tolerance": {"type": "number", "minimum": 0.03, "maximum": 0.3, "default": 0.06}}}},
    {"name": "patrol_detect", "description": "Disable native SmartTrack, without changing the recognition-object rectangle, then use YOLO to search for objects. Prefer find_person for people. Search only if no target is found, then use manual visual-feedback PTZ corrections until the target is centred.", "inputSchema": {"type": "object", "properties": {"targets": {"type": "array", "items": {"type": "string"}, "default": ["person", "car", "truck", "bus", "motorcycle", "bicycle", "cat", "dog"]}, "colors": {"type": "array", "items": {"type": "string", "enum": ["black"]}, "default": []}, "moves": {"type": "array", "items": {"type": "string", "enum": ["up", "down", "left", "right"]}, "maxItems": 24, "default": ["left", "left", "left", "left"], "description": "Search plan used only when current view has no match. Each search move is large."}, "speed": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3}, "steps_per_move": {"type": "integer", "minimum": 1, "maximum": 30, "default": 4}, "confidence": {"type": "number", "minimum": 0.1, "maximum": 0.95, "default": 0.5}, "center_tolerance": {"type": "number", "minimum": 0.03, "maximum": 0.3, "default": 0.06}}}},
]


def text_result(value: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": value}]}



def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be an object")
    if name == "set_camera_credentials":
        host = arguments.get("host", "")
        password = arguments.get("password", "")
        username = arguments.get("username", "admin")
        if not isinstance(host, str) or not host.strip():
            raise ValueError("host must be a non-empty string")
        if not isinstance(password, str) or not password:
            raise ValueError("password must be a non-empty string")
        if not isinstance(username, str) or not username.strip():
            raise ValueError("username must be a non-empty string")
        store_credentials(host.strip(), username.strip(), password)
        return text_result(f"Stored camera credentials for host {HOST} (user {USERNAME}); the password was saved without being echoed.")
    if name == "find_person":
        prepare_person_tracking()
        arguments = {"confidence": 0.45, "center_tolerance": 0.06, **arguments, "targets": ["person"], "colors": [], "_native_tracking": True}
        name = "patrol_detect"
    if name == "camera_info":
        body, _ = request("/cgi-bin/hi3510/param.cgi", {"cmd": "getserverinfo"})
        return text_result(body.decode("utf-8", errors="replace"))
    if name == "snapshot":
        jpeg = capture_rtsp_frame(arguments.get("quality", "sub"))
        return {"content": [{"type": "image", "data": base64.b64encode(jpeg).decode(), "mimeType": "image/jpeg"}]}
    if name == "rtsp_url":
        if not HOST:
            raise ValueError("camera IP is not configured; set CAMERA_HOST or call set_camera_credentials")
        stream = "11" if arguments.get("quality", "sub") == "main" else "12"
        return text_result(f"rtsp://{HOST}:554/{stream}")
    if name == "ptz_move":
        direction = arguments["direction"]
        speed = int(arguments.get("speed", 5))
        steps = int(arguments.get("steps", 1))
        if not 1 <= speed <= 10:
            raise ValueError("speed must be between 1 and 10")
        if not 1 <= steps <= 30:
            raise ValueError("steps must be between 1 and 30")
        set_native_smart_tracking(False)
        move_ptz(direction, speed, steps)
        return text_result("PTZ command sent.")
    if name == "patrol_detect":
        targets = arguments.get("targets", ["person", "car", "truck", "bus", "motorcycle", "bicycle", "cat", "dog"])
        colors = arguments.get("colors", [])
        moves = arguments.get("moves", ["left", "left", "left", "left"])
        if not isinstance(targets, list) or not all(isinstance(target, str) for target in targets):
            raise ValueError("targets must be an array of YOLO class names")
        if not isinstance(colors, list) or not all(color == "black" for color in colors):
            raise ValueError("colors may contain only the supported appearance filter: black")
        if not isinstance(moves, list) or len(moves) > 24 or not all(move in {"up", "down", "left", "right"} for move in moves):
            raise ValueError("moves must contain at most 24 up/down/left/right values")
        confidence = float(arguments.get("confidence", 0.5))
        if not 0.1 <= confidence <= 0.95:
            raise ValueError("confidence must be between 0.1 and 0.95")
        speed, steps = int(arguments.get("speed", 3)), int(arguments.get("steps_per_move", 2))
        if not 1 <= speed <= 10 or not 1 <= steps <= 30:
            raise ValueError("speed must be between 1 and 10; steps_per_move must be between 1 and 30")
        center_tolerance = float(arguments.get("center_tolerance", 0.06))
        if not 0.03 <= center_tolerance <= 0.3:
            raise ValueError("center_tolerance must be 0.03–0.3")
        native_tracking = bool(arguments.get("_native_tracking"))
        if not native_tracking:
            set_native_smart_tracking(False)
        positions = ["current view", *[f"after {move} move {index + 1}" for index, move in enumerate(moves)]]
        for index, position in enumerate(positions):
            matches = detect_current_view(targets, colors, confidence)
            if matches:
                target = select_target(matches)
                assert target is not None
                if native_tracking:
                    target, high_res, observations = wait_for_native_person_center(confidence, center_tolerance)
                    encoded_high_res = base64.b64encode(high_res).decode()
                    # Keep firmware tracking in exclusive control through final
                    # centring and capture. Disable it only after the exact JPEG
                    # to be returned has passed main-stream YOLO verification.
                    set_native_smart_tracking(False)
                    summary = {"found": True, "position": position, "object": target, "centered": True, "centering_method": "camera_native_smarttrack", "native_tracking_observations": observations, "manual_centering_adjustments": 0, "native_tracking_enabled_after_capture": False, "camera_locked_on_position": True, "note": "YOLO found the person; native SmartTrack centred them, the returned main-stream frame was verified, and SmartTrack was then disabled."}
                    return {"content": [{"type": "text", "text": json.dumps(summary, ensure_ascii=False)}, {"type": "image", "data": encoded_high_res, "mimeType": "image/jpeg"}]}
                centered = False
                adjustments = 0
                reverse_x = False
                reverse_y = False
                while adjustments < MAX_CENTERING_MOVES:
                    centered, axis, previous_error = center_target(target, center_tolerance, speed, reverse_x, reverse_y)
                    if centered:
                        centered = True
                        break
                    adjustments += 1
                    refreshed = detect_current_view(targets, colors, confidence)
                    target = select_target(refreshed)
                    if target is None:
                        break
                    new_dx, new_dy = target_offset(target)
                    new_error = abs(new_dx if axis == "x" else new_dy)
                    if abs(new_dx) <= center_tolerance and abs(new_dy) <= center_tolerance:
                        centered = True
                        break
                    # If the selected-axis error did not shrink, this firmware's
                    # movement direction is opposite our initial mapping. Flip
                    # that mapping for the remaining bounded adjustments.
                    if new_error >= previous_error * 0.98:
                        if axis == "x":
                            reverse_x = not reverse_x
                        else:
                            reverse_y = not reverse_y
                if target is None:
                    return text_result(json.dumps({"found": False, "target_lost_during_centering": True, "views_checked": index + 1}, ensure_ascii=False))
                high_res = capture_rtsp_frame("main")
                summary = {"found": True, "position": position, "object": target, "centered": centered, "centering_adjustments": adjustments, "direction_calibration": {"horizontal_reversed": reverse_x, "vertical_reversed": reverse_y}, "camera_locked_on_position": True, "note": "The scan stopped at its final centering position."}
                return {"content": [{"type": "text", "text": json.dumps(summary, ensure_ascii=False)}, {"type": "image", "data": base64.b64encode(high_res).decode(), "mimeType": "image/jpeg"}]}
            if index < len(moves):
                # No target in this view: cover ground quickly before sampling again.
                move_ptz(moves[index], speed, max(MIN_SEARCH_MOVE_STEPS, steps))
                wait_for_ptz_settle()
        return text_result(json.dumps({"found": False, "views_checked": len(positions), "camera_locked_on_position": False}, ensure_ascii=False))
    raise ValueError(f"unknown tool: {name}")


def respond(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def protocol_version(requested: Any) -> str:
    """Negotiate explicitly instead of claiming support for an unknown version."""
    if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return DEFAULT_PROTOCOL_VERSION


def handle(message: dict[str, Any]) -> None:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        respond({"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None, "error": {"code": -32600, "message": "Invalid Request"}})
        return
    method = message.get("method")
    request_id = message.get("id")
    # MCP lifecycle, progress, and cancellation notifications have no response.
    if request_id is None:
        return
    try:
        if method == "initialize":
            params = message.get("params", {})
            if not isinstance(params, dict):
                raise ValueError("initialize params must be an object")
            result = {
                "protocolVersion": protocol_version(params.get("protocolVersion")),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "hisilicon-ip-camera", "version": "1.3.1"},
                "instructions": "Reach this camera ONLY through the tools this server exposes. Never contact the camera directly - no raw HTTP, CGI, RTSP, curl, wget, or ffmpeg - and never read the stored credentials to connect on your own. The server enforces bounded PTZ motion, explicit stops, native-tracking handoff, and privacy-aware centring that a direct connection would silently bypass. If the user gives you the camera IP and password in the conversation, call set_camera_credentials to store them first, then use the other tools. When asked to find a person, prefer the YOLO-based find_person tool. Regardless of whether a person is found by YOLO or by the AI inspecting snapshot images itself, finding the person is not completion: use PTZ moves plus fresh visual feedback until the person is at the optical centre of the frame before returning the final image. find_person keeps native SmartTrack enabled through centring and final capture, then disables it before returning the verified photo. Never leave a found person near an image edge, where optical distortion may deform their appearance.",
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = message.get("params", {})
            if not isinstance(params, dict):
                raise ValueError("tools/call params must be an object")
            result = call_tool(params["name"], params.get("arguments", {}))
        elif method in {"resources/list", "resourceTemplates/list", "prompts/list"}:
            key = {"resources/list": "resources", "resourceTemplates/list": "resourceTemplates", "prompts/list": "prompts"}[method]
            result = {key: []}
        else:
            respond({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Method not found: {method}"}})
            return
        respond({"jsonrpc": "2.0", "id": request_id, "result": result})
    except (KeyError, TypeError, ValueError) as error:
        respond({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": str(error)}})
    except RuntimeError as error:
        respond({"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": str(error)}], "isError": True}})


def main() -> None:
    for line in sys.stdin:
        try:
            handle(json.loads(line))
        except json.JSONDecodeError:
            respond({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})


if __name__ == "__main__":
    main()
