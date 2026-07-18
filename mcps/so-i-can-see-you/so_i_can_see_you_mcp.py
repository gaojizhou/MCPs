#!/usr/bin/env python3
# Copyright (C) 2026 gaojizhou
# SPDX-License-Identifier: AGPL-3.0-only
"""So I Can See You: an MCP server that gives AI eyes through HiSilicon/Hipcam cameras.

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
from typing import Any, Callable


# Camera IP and password are read ONLY from these module globals. They are
# seeded from environment variables at startup and may be updated at runtime by
# the entrust_eyes tool when the user supplies them in the
# conversation. No other code path accepts them, and the server never contacts
# the camera except through these values.
HOST = os.environ.get("SEEYOU_HOST", "")
USERNAME = os.environ.get("SEEYOU_USERNAME", "admin")
PASSWORD = os.environ.get("SEEYOU_PASSWORD", "")
HTTP_TIMEOUT = float(os.environ.get("SEEYOU_TIMEOUT_SECONDS", "10"))
MCP_DIR = os.path.dirname(os.path.abspath(__file__))
YOLO_MODEL = os.environ.get("SEEYOU_YOLO_MODEL", os.path.join(MCP_DIR, "yolo11n.pt"))
MAX_CENTERING_MOVES = 12
MIN_SEARCH_MOVE_STEPS = 4
PTZ_SETTLE_SECONDS = 2.0
PTZ_SETTLE_TIMEOUT_SECONDS = 8.0
PTZ_STABLE_FRAME_INTERVAL_SECONDS = 0.4
PTZ_SECONDS_PER_STEP = 0.25
NATIVE_TRACKING_CENTER_TIMEOUT_SECONDS = float(os.environ.get("SEEYOU_TRACKING_CENTER_TIMEOUT_SECONDS", "20"))
NATIVE_TRACKING_SAMPLE_INTERVAL_SECONDS = 0.5
_detector: Any | None = None
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05", "2024-10-07")
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
ProgressCallback = Callable[[str], None]


def no_progress(_message: str) -> None:
    """Default callback for clients that did not request MCP progress."""


def progress_notifier(token: str | int | None) -> ProgressCallback:
    """Create a monotonically increasing MCP progress notification sender."""
    if token is None or isinstance(token, bool) or not isinstance(token, (str, int)):
        return no_progress
    current = 0

    def notify(message: str) -> None:
        nonlocal current
        current += 1
        respond({
            "jsonrpc": "2.0",
            "method": "notifications/progress",
            "params": {"progressToken": token, "progress": current, "message": message},
        })

    return notify


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
        raise ValueError("the eyes have no address; set SEEYOU_HOST or call entrust_eyes")
    if not PASSWORD:
        raise ValueError("the eyes have no password; set SEEYOU_PASSWORD or call entrust_eyes")
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


def prepare_person_tracking(progress: ProgressCallback = no_progress) -> None:
    """Enable native person tracking and hide firmware detection rectangles.

    Preserve the camera's existing smart-recognition type and threshold while
    changing only the rectangle overlay. Read both settings back so seek_a_person
    never silently continues after a firmware that ignored either command.
    """
    progress("Reading the camera's person-recognition settings")
    smd_attr, _ = request("/cgi-bin/hi3510/param.cgi", {"cmd": "getsmdattr"})
    smd_ex, _ = request("/cgi-bin/hi3510/param.cgi", {"cmd": "getsmdex"})
    settings = {**parse_firmware_variables(smd_attr), **parse_firmware_variables(smd_ex)}
    smd_type = settings.get("smd_type", "0")
    smd_threshold = settings.get("smd_gthresh", settings.get("smd_threshold", "34"))

    progress("Enabling native SmartTrack")
    set_native_smart_tracking(True)
    progress("Disabling recognition-object rectangles while preserving detection settings")
    request(
        "/cgi-bin/hi3510/param.cgi",
        {"cmd": "setsmdex", "-smd_rect": "0", "-smd_type": smd_type, "-smd_gthresh": smd_threshold},
        method="POST",
    )
    rectangle_body, _ = request("/cgi-bin/hi3510/param.cgi", {"cmd": "getsmdex"})
    rectangle = parse_firmware_variables(rectangle_body)
    if rectangle.get("smd_rect") != "0":
        raise RuntimeError("the camera did not disable the recognition-object rectangle")
    progress("Camera tracking settings are ready")


def capture_rtsp_frame(quality: str) -> bytes:
    """Return one JPEG frame. Stream 11 is main and stream 12 is sub."""
    if not HOST:
        raise ValueError("the eyes have no address; set SEEYOU_HOST or call entrust_eyes")
    if not PASSWORD:
        raise ValueError("the eyes have no password; set SEEYOU_PASSWORD or call entrust_eyes")
    stream = "11" if quality == "main" else "12"
    username = urllib.parse.quote(USERNAME, safe="")
    password = urllib.parse.quote(PASSWORD, safe="")
    stream_url = f"rtsp://{username}:{password}@{HOST}:554/{stream}"
    ffmpeg = find_ffmpeg()
    try:
        capture = subprocess.run(
            [ffmpeg, "-loglevel", "error", "-rtsp_transport", "tcp", "-i", stream_url, "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
            capture_output=True, timeout=HTTP_TIMEOUT + 10, check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"could not capture an RTSP frame from stream /{stream}") from error
    if not capture.stdout:
        raise RuntimeError(f"stream /{stream} returned an empty frame")
    return capture.stdout


def find_ffmpeg() -> str:
    """Locate FFmpeg without assuming a Linux-specific installation path."""
    configured = os.environ.get("SEEYOU_FFMPEG_PATH")
    if configured:
        path = os.path.expanduser(configured)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
        raise RuntimeError(f"SEEYOU_FFMPEG_PATH is not an executable file: {configured}")
    discovered = shutil.which("ffmpeg")
    if discovered:
        return discovered
    for candidate in ("/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError("FFmpeg was not found; install it or set SEEYOU_FFMPEG_PATH")


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


def wait_for_native_person_center(
    confidence: float,
    tolerance: float,
    progress: ProgressCallback = no_progress,
) -> tuple[dict[str, Any], bytes, int]:
    """Wait for firmware SmartTrack, then verify centering on the returned frame.

    No PTZ command is issued here: once YOLO has found a person, native camera
    tracking has exclusive control of the motors. The final main-stream JPEG is
    itself checked so the image returned to the client is known to be centred.
    """
    deadline = time.monotonic() + NATIVE_TRACKING_CENTER_TIMEOUT_SECONDS
    observations = 0
    while time.monotonic() < deadline:
        observations += 1
        progress(f"Waiting for native SmartTrack to centre the person (observation {observations})")
        sub_matches = detect_objects(capture_rtsp_frame("sub"), ["person"], [], confidence)
        target = select_target(sub_matches)
        if target is not None:
            dx, dy = target_offset(target)
            if abs(dx) <= tolerance and abs(dy) <= tolerance:
                progress("The person is centred in the preview; verifying the final high-resolution frame")
                high_res = capture_rtsp_frame("main")
                main_target = select_target(detect_objects(high_res, ["person"], [], confidence))
                if main_target is not None:
                    main_dx, main_dy = target_offset(main_target)
                    if abs(main_dx) <= tolerance and abs(main_dy) <= tolerance:
                        progress("The final high-resolution frame is centred")
                        return main_target, high_res, observations
                progress("High-resolution verification was not centred; continuing to observe")
        time.sleep(NATIVE_TRACKING_SAMPLE_INTERVAL_SECONDS)
    raise RuntimeError(
        f"native SmartTrack did not centre the detected person within "
        f"{NATIVE_TRACKING_CENTER_TIMEOUT_SECONDS:g} seconds"
    )


TOOLS: list[dict[str, Any]] = [
    {"name": "entrust_eyes", "description": "Entrust So I Can See You with the camera IP, username, and password so its other tools can open these eyes. Use this only when the user provides the values in the conversation and they are not already set through SEEYOU_* environment variables. The password is never echoed. Do not contact the camera yourself; always look through this server's tools.", "inputSchema": {"type": "object", "properties": {"host": {"type": "string", "description": "Camera LAN IP or hostname the user provided."}, "password": {"type": "string", "description": "Camera device password the user provided."}, "username": {"type": "string", "default": "admin", "description": "Camera login username; defaults to admin."}}, "required": ["host", "password"]}},
    {"name": "describe_the_eyes", "description": "Read the camera model, hardware version, firmware, and storage status behind these eyes.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "look_now", "description": "Look through these eyes now and return a still image. Use main quality for the final photograph and sub quality for a quick look. If a sought person is off-centre, keep using small turn_the_gaze corrections and fresh looks until they are centred.", "inputSchema": {"type": "object", "properties": {"quality": {"type": "string", "enum": ["main", "sub"], "default": "sub", "description": "Use main for the clearest photograph or sub for a faster look."}}}},
    {"name": "share_the_view", "description": "Give the user an address for watching this live view without revealing credentials. Call it only when the user explicitly asks for a live view.", "inputSchema": {"type": "object", "properties": {"quality": {"type": "string", "enum": ["main", "sub"], "default": "sub"}}}},
    {"name": "turn_the_gaze", "description": "Stop automatic following, then turn these physical eyes in one direction by a bounded amount and stop. Start gently on an unfamiliar device, wait for motion to finish, and look again before deciding the next move.", "inputSchema": {"type": "object", "properties": {"direction": {"type": "string", "enum": ["up", "down", "left", "right", "home"]}, "speed": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5}, "steps": {"type": "integer", "minimum": 1, "maximum": 30, "default": 1}}, "required": ["direction"]}},
    {"name": "seek_a_person", "description": "Preferred whenever the user asks these eyes to find a person. Look at the current view first and turn through the search plan only while nobody is visible. Once someone is found, let the camera bring them to the centre, take and verify the final high-resolution photograph, then stop following before returning it.", "inputSchema": {"type": "object", "properties": {"moves": {"type": "array", "items": {"type": "string", "enum": ["up", "down", "left", "right"]}, "maxItems": 24, "default": ["left", "left", "left", "left"]}, "speed": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3}, "steps_per_move": {"type": "integer", "minimum": 1, "maximum": 30, "default": 4}, "confidence": {"type": "number", "minimum": 0.1, "maximum": 0.95, "default": 0.45}, "center_tolerance": {"type": "number", "minimum": 0.03, "maximum": 0.3, "default": 0.06}}}},
    {"name": "search_the_view", "description": "Search the surrounding view for requested objects such as vehicles or animals. Look here first, turn through the plan only when nothing matches, then make small gaze corrections until the chosen target is near the centre and return a clear photograph. Prefer seek_a_person for people.", "inputSchema": {"type": "object", "properties": {"targets": {"type": "array", "items": {"type": "string"}, "default": ["person", "car", "truck", "bus", "motorcycle", "bicycle", "cat", "dog"]}, "colors": {"type": "array", "items": {"type": "string", "enum": ["black"]}, "default": []}, "moves": {"type": "array", "items": {"type": "string", "enum": ["up", "down", "left", "right"]}, "maxItems": 24, "default": ["left", "left", "left", "left"], "description": "Directions to look through only when the current view has no match."}, "speed": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3}, "steps_per_move": {"type": "integer", "minimum": 1, "maximum": 30, "default": 4}, "confidence": {"type": "number", "minimum": 0.1, "maximum": 0.95, "default": 0.5}, "center_tolerance": {"type": "number", "minimum": 0.03, "maximum": 0.3, "default": 0.06}}}},
]


def text_result(value: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": value}]}



def call_tool(name: str, arguments: dict[str, Any], progress: ProgressCallback = no_progress) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be an object")
    if name == "entrust_eyes":
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
        return text_result(f"The eyes are entrusted to host {HOST} (user {USERNAME}); the password was remembered without being echoed.")
    if name == "seek_a_person":
        progress("Starting person search")
        prepare_person_tracking(progress)
        arguments = {"confidence": 0.45, "center_tolerance": 0.06, **arguments, "targets": ["person"], "colors": [], "_native_tracking": True}
        name = "search_the_view"
    if name == "describe_the_eyes":
        body, _ = request("/cgi-bin/hi3510/param.cgi", {"cmd": "getserverinfo"})
        return text_result(body.decode("utf-8", errors="replace"))
    if name == "look_now":
        progress("Capturing a camera frame")
        jpeg = capture_rtsp_frame(arguments.get("quality", "sub"))
        return {"content": [{"type": "image", "data": base64.b64encode(jpeg).decode(), "mimeType": "image/jpeg"}]}
    if name == "share_the_view":
        if not HOST:
            raise ValueError("the eyes have no address; set SEEYOU_HOST or call entrust_eyes")
        stream = "11" if arguments.get("quality", "sub") == "main" else "12"
        return text_result(f"rtsp://{HOST}:554/{stream}")
    if name == "turn_the_gaze":
        direction = arguments["direction"]
        speed = int(arguments.get("speed", 5))
        steps = int(arguments.get("steps", 1))
        if not 1 <= speed <= 10:
            raise ValueError("speed must be between 1 and 10")
        if not 1 <= steps <= 30:
            raise ValueError("steps must be between 1 and 30")
        progress("Disabling native SmartTrack before manual PTZ movement")
        set_native_smart_tracking(False)
        progress(f"Turning the camera {direction}")
        move_ptz(direction, speed, steps)
        return text_result("PTZ command sent.")
    if name == "search_the_view":
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
            progress("Disabling native SmartTrack before object search")
            set_native_smart_tracking(False)
        positions = ["current view", *[f"after {move} move {index + 1}" for index, move in enumerate(moves)]]
        for index, position in enumerate(positions):
            progress(f"Running YOLO in {position} (view {index + 1} of {len(positions)})")
            matches = detect_current_view(targets, colors, confidence)
            if matches:
                progress(f"YOLO found a target in {position}")
                target = select_target(matches)
                assert target is not None
                if native_tracking:
                    progress("Stopping manual search and giving native SmartTrack exclusive PTZ control")
                    target, high_res, observations = wait_for_native_person_center(confidence, center_tolerance, progress)
                    encoded_high_res = base64.b64encode(high_res).decode()
                    # Keep firmware tracking in exclusive control through final
                    # centring and capture. Disable it only after the exact JPEG
                    # to be returned has passed main-stream YOLO verification.
                    progress("Final photograph verified; disabling native SmartTrack")
                    set_native_smart_tracking(False)
                    progress("Person search complete")
                    summary = {"found": True, "position": position, "object": target, "centered": True, "centering_method": "camera_native_smarttrack", "native_tracking_observations": observations, "manual_centering_adjustments": 0, "native_tracking_enabled_after_capture": False, "gaze_fixed_on_position": True, "note": "YOLO found the person; native SmartTrack centred them, the returned main-stream frame was verified, and SmartTrack was then disabled."}
                    return {"content": [{"type": "text", "text": json.dumps(summary, ensure_ascii=False)}, {"type": "image", "data": encoded_high_res, "mimeType": "image/jpeg"}]}
                centered = False
                adjustments = 0
                reverse_x = False
                reverse_y = False
                while adjustments < MAX_CENTERING_MOVES:
                    progress(f"Manually centring the target (adjustment {adjustments + 1} of {MAX_CENTERING_MOVES})")
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
                summary = {"found": True, "position": position, "object": target, "centered": centered, "centering_adjustments": adjustments, "direction_calibration": {"horizontal_reversed": reverse_x, "vertical_reversed": reverse_y}, "gaze_fixed_on_position": True, "note": "The search stopped with the gaze fixed on its final centering position."}
                return {"content": [{"type": "text", "text": json.dumps(summary, ensure_ascii=False)}, {"type": "image", "data": base64.b64encode(high_res).decode(), "mimeType": "image/jpeg"}]}
            if index < len(moves):
                # No target in this view: cover ground quickly before sampling again.
                progress(f"No target found; turning {moves[index]} to inspect the next view")
                move_ptz(moves[index], speed, max(MIN_SEARCH_MOVE_STEPS, steps))
                progress("Waiting for the camera image to settle")
                wait_for_ptz_settle()
        progress("Search complete; no matching target was found")
        return text_result(json.dumps({"found": False, "views_checked": len(positions), "gaze_fixed_on_position": False}, ensure_ascii=False))
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
                "serverInfo": {"name": "so-i-can-see-you", "version": "2.0.0"},
                "instructions": "So I Can See You gives you a carefully bounded pair of eyes. Look only through the tools this server exposes; never take stored credentials and contact the camera on your own. If the user provides an address and password, call entrust_eyes first. Use seek_a_person when asked to find someone. Seeing a person near the edge is not completion: keep using gentle gaze corrections and fresh looks until they are centred. seek_a_person follows only through centring and final capture, then stops following before returning the photograph. Look only where the user has asked, describe uncertainty honestly, and let these eyes rest when the task is complete.",
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = message.get("params", {})
            if not isinstance(params, dict):
                raise ValueError("tools/call params must be an object")
            metadata = params.get("_meta", {})
            if metadata is not None and not isinstance(metadata, dict):
                raise ValueError("tools/call _meta must be an object")
            token = (metadata or {}).get("progressToken")
            result = call_tool(params["name"], params.get("arguments", {}), progress_notifier(token))
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
