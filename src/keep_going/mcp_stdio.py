"""Minimal MCP stdio server for Keep Going tools."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from keep_going.config import Config
from keep_going.decision.policy_runtime import runtime_policy_path
from keep_going.eval.replay import run_eval
from keep_going.decision.reply import build_decision_reply

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "keep-going"
SERVER_VERSION = "0.1.0"
TRANSPORT_HEADER = "headers"
TRANSPORT_NDJSON = "ndjson"


@dataclass(frozen=True)
class FramedMessage:
    payload: dict[str, Any]
    transport: str


def run_stdio_server(cfg: Config) -> None:
    while True:
        framed = read_framed_message(sys.stdin.buffer)
        if framed is None:
            return
        response = handle_request(cfg, framed.payload)
        if response is not None:
            write_message(sys.stdout.buffer, response, transport=framed.transport)
            sys.stdout.buffer.flush()


def handle_request(cfg: Config, request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    try:
        if method == "initialize":
            return _result(request_id, _initialize_result())
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return _result(request_id, {})
        if method == "tools/list":
            return _result(request_id, {"tools": _tools()})
        if method == "tools/call":
            return _result(request_id, _call_tool(cfg, request.get("params") or {}))
        return _error(request_id, -32601, f"method not found: {method}")
    except Exception as exc:
        return _error(request_id, -32000, str(exc))


def read_framed_message(stream: BinaryIO) -> FramedMessage | None:
    first = _read_non_empty_line(stream)
    if first is None:
        return None
    if first.lstrip().startswith(b"{"):
        return FramedMessage(json.loads(first.decode("utf-8")), TRANSPORT_NDJSON)
    return FramedMessage(_read_content_length_message(stream, first), TRANSPORT_HEADER)


def read_message(stream: BinaryIO) -> dict[str, Any] | None:
    framed = read_framed_message(stream)
    return framed.payload if framed is not None else None


def write_message(stream: BinaryIO, message: dict[str, Any], *, transport: str = TRANSPORT_HEADER) -> None:
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if transport == TRANSPORT_NDJSON:
        stream.write(payload + b"\n")
        return
    stream.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload)


def _read_non_empty_line(stream: BinaryIO) -> bytes | None:
    while True:
        line = stream.readline()
        if line == b"":
            return None
        if line in (b"\r\n", b"\n"):
            continue
        return line


def _read_content_length_message(stream: BinaryIO, first: bytes) -> dict[str, Any]:
    headers: dict[str, str] = {}
    line = first
    while True:
        if line in (b"\r\n", b"\n"):
            break
        key, _, value = line.decode("ascii").partition(":")
        headers[key.lower()] = value.strip()
        line = stream.readline()
        if line == b"":
            raise ValueError("incomplete MCP headers")
    length = int(headers.get("content-length", "0"))
    if length <= 0:
        raise ValueError("missing Content-Length header")
    payload = stream.read(length)
    if len(payload) != length:
        raise ValueError("incomplete MCP payload")
    return json.loads(payload.decode("utf-8"))


def _initialize_result() -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def _tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "keep_going_reply",
            "description": "Answer an AI agent question as the user's Keep Going.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "project": {"type": "string"},
                    "recent_context": {"type": "string"},
                    "top_k": {"type": "integer", "minimum": 1, "default": 5},
                    "generate": {"type": "boolean", "default": False},
                },
                "required": ["question"],
                "additionalProperties": False,
            },
        },
        {
            "name": "keep_going_eval",
            "description": "Replay held-out user turns and write a Keep Going evaluation report. Set generate=true only when external Anthropic API use is approved.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "holdout_ratio": {"type": "number", "exclusiveMinimum": 0, "maximum": 1, "default": 0.1},
                    "limit": {"type": "integer", "minimum": 1, "default": 30},
                    "top_k": {"type": "integer", "minimum": 1, "default": 5},
                    "out": {"type": "string"},
                    "generate": {"type": "boolean", "default": False},
                },
                "additionalProperties": False,
            },
        },
    ]


def _call_tool(cfg: Config, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    args = params.get("arguments") or {}
    if name == "keep_going_reply":
        payload = _keep_going_reply(cfg, args)
    elif name == "keep_going_eval":
        payload = _keep_going_eval(cfg, args)
    else:
        return _tool_error(f"unknown tool: {name}")
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
        "isError": False,
    }


def _keep_going_reply(cfg: Config, args: dict[str, Any]) -> dict[str, Any]:
    labels = cfg.paths.data_dir / "labels" / "labeled.jsonl"
    turns = cfg.paths.data_dir / "turns" / "turns.jsonl"
    return build_decision_reply(
        question=str(args.get("question") or ""),
        project=str(args.get("project") or ""),
        policy_path=runtime_policy_path(cfg.paths.artifacts_dir / "decision-policy.yaml"),
        examples_path=labels if labels.exists() else turns,
        recent_context=str(args.get("recent_context") or args.get("context") or ""),
        top_k=int(args.get("top_k") or 5),
        model=cfg.models.decision,
        generate=bool(args.get("generate", False)),
    )


def _keep_going_eval(cfg: Config, args: dict[str, Any]) -> dict[str, Any]:
    out = args.get("out")
    path = run_eval(
        cfg,
        holdout_ratio=float(args.get("holdout_ratio") or 0.1),
        limit=int(args.get("limit") or 30),
        top_k=int(args.get("top_k") or 5),
        out_path=Path(out) if out else None,
        generate=bool(args.get("generate", False)),
    )
    return {"report_path": str(path)}


def _tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
