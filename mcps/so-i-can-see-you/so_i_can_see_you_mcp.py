#!/usr/bin/env python3
# Copyright (C) 2026 gaojizhou
# SPDX-License-Identifier: AGPL-3.0-only
"""So I Can See You: an MCP server that gives AI eyes through HiSilicon/Hipcam cameras.

Credentials are deliberately read only from environment variables.  The server
speaks MCP JSON-RPC over stdio, so it can be launched by Claude Desktop, Codex,
or another MCP host.
"""

import base64
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
PTZ_SECONDS_PER_STEP = 0.25
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


TOOLS: list[dict[str, Any]] = [
    {"name": "entrust_eyes", "description": "Entrust So I Can See You with the camera IP, username, and password so its other tools can open these eyes. Use this only when the user provides the values in the conversation and they are not already set through SEEYOU_* environment variables. The password is never echoed. Do not contact the camera yourself; always look through this server's tools.", "inputSchema": {"type": "object", "properties": {"host": {"type": "string", "description": "Camera LAN IP or hostname the user provided."}, "password": {"type": "string", "description": "Camera device password the user provided."}, "username": {"type": "string", "default": "admin", "description": "Camera login username; defaults to admin."}}, "required": ["host", "password"]}},
    {"name": "describe_the_eyes", "description": "Read the camera model, hardware version, firmware, and storage status behind these eyes.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "look_now", "description": "Look through these eyes now and return a still image. Use main quality for the clearest photograph and sub quality for a faster look.", "inputSchema": {"type": "object", "properties": {"quality": {"type": "string", "enum": ["main", "sub"], "default": "sub", "description": "Use main for the clearest photograph or sub for a faster look."}}}},
    {"name": "share_the_view", "description": "Give the user an address for watching this live view without revealing credentials. Call it only when the user explicitly asks for a live view.", "inputSchema": {"type": "object", "properties": {"quality": {"type": "string", "enum": ["main", "sub"], "default": "sub"}}}},
    {"name": "turn_the_gaze", "description": "Stop automatic following, then turn these physical eyes in one direction by a bounded amount and stop. Start gently on an unfamiliar device, wait for motion to finish, and look again before deciding the next move.", "inputSchema": {"type": "object", "properties": {"direction": {"type": "string", "enum": ["up", "down", "left", "right", "home"]}, "speed": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5}, "steps": {"type": "integer", "minimum": 1, "maximum": 30, "default": 1}}, "required": ["direction"]}},
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
                "serverInfo": {"name": "so-i-can-see-you", "version": "2.1.0"},
                "instructions": "So I Can See You gives you a carefully bounded pair of eyes. Look only through the tools this server exposes; never take stored credentials and contact the camera on your own. If the user provides an address and password, call entrust_eyes first. Use look_now to observe and turn_the_gaze for bounded manual PTZ movement. This server does not identify or automatically search for people or objects. Look only where the user has asked, describe uncertainty honestly, and let these eyes rest when the task is complete.",
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
