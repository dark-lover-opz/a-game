"""Tests for the MCP server, against a fake Blender add-on socket server."""

import base64
import json
import socket
import threading

import pytest

from claude_blender_mcp import server


class FakeBlender:
    def __init__(self, handler):
        self.handler = handler
        self.requests = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            with client:
                data = b""
                while b"\n" not in data:
                    data += client.recv(4096)
                request = json.loads(data)
                self.requests.append(request)
                response = self.handler(request)
                response["id"] = request["id"]
                client.sendall(json.dumps(response).encode() + b"\n")


@pytest.fixture
def fake(monkeypatch):
    def make(handler):
        blender = FakeBlender(handler)
        monkeypatch.setattr(server, "_blender", server.BlenderConnection(port=blender.port))
        return blender
    return make


def test_round_trip_drops_none_params(fake):
    blender = fake(lambda req: {"status": "ok", "result": {"echo": req["params"]}})
    out = json.loads(server.create_object(type="cone", name="Hat", location=[0, 0, 1]))
    assert out["echo"]["type"] == "cone"
    assert out["echo"]["location"] == [0, 0, 1]
    assert "color" not in out["echo"]
    assert blender.requests[0]["command"] == "create_object"


def test_error_becomes_tool_error(fake):
    fake(lambda req: {"status": "error", "code": "permission_denied", "message": "The user denied"})
    with pytest.raises(server.ToolError, match="denied"):
        server.get_scene_info()


def test_screenshot_returns_image(fake):
    png = b"\x89PNG\r\n\x1a\nfake"
    fake(lambda req: {"status": "ok", "result": {"image_base64": base64.b64encode(png).decode()}})
    image = server.get_viewport_screenshot(view="front")
    content = image.to_image_content()
    assert base64.b64decode(content.data) == png


def test_helpful_message_when_blender_is_closed(monkeypatch):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    monkeypatch.setattr(server, "_blender", server.BlenderConnection(port=port))
    with pytest.raises(server.ToolError, match="open Blender"):
        server.blender_status()


def test_view_image_file(tmp_path):
    path = tmp_path / "ref.jpg"
    path.write_bytes(b"\xff\xd8\xffdata")
    content = server.view_image_file(str(path)).to_image_content()
    assert content.mime_type == "image/jpeg"
    with pytest.raises(server.ToolError):
        server.view_image_file(str(tmp_path / "missing.png"))
    with pytest.raises(server.ToolError):
        server.view_image_file(str(tmp_path / "notes.txt"))
