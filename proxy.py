#!/usr/bin/env python3
"""
DeepSeek API reverse proxy that fixes opencode / Claude Code reasoning field mismatches.

Behavior:
- Request:
  - rename reasoning_text -> reasoning_content when present
  - inject empty reasoning_content only for assistant messages that actually contain tool_calls
  - preserve the original raw request body when no rewrite is needed
- Response:
  - rename reasoning_content -> reasoning_text in message/delta objects
  - preserve raw response bytes when no rewrite is needed
  - translate SSE event-stream chunks on the fly

Usage:
  python3 proxy.py [--host 0.0.0.0] [--port 18200] [--upstream https://api.deepseek.com]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from typing import Any, Tuple
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("deepseek-proxy")

SSE_DATA_PREFIX = re.compile(rb"^data:\s*")

REQUEST_HOP_BY_HOP = {
    "host",
    "content-length",
    "transfer-encoding",
    "content-encoding",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "upgrade",
    "accept-encoding",
}

RESPONSE_HOP_BY_HOP = {
    "content-length",
    "transfer-encoding",
    "content-encoding",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "upgrade",
}


def rename_key(obj: dict[str, Any], old_key: str, new_key: str) -> bool:
    """
    Rename a key if present.
    Returns True when a mutation occurred.
    """
    if old_key in obj:
        obj[new_key] = obj.pop(old_key)
        return True
    return False


def translate_request_payload(payload: dict[str, Any]) -> bool:
    """
    Mutate only the exact assistant turns DeepSeek requires.

    Returns True if the payload was modified.
    """
    changed = False
    messages = payload.get("messages")

    if not isinstance(messages, list):
        return False

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "assistant":
            continue

        # Preserve compatibility with SDKs that use reasoning_text.
        if "reasoning_text" in msg:
            if "reasoning_content" not in msg:
                msg["reasoning_content"] = msg["reasoning_text"]
            del msg["reasoning_text"]
            changed = True

        # DeepSeek requires reasoning_content to be present for assistant turns
        # that involved tool calls.
        if msg.get("tool_calls") and "reasoning_content" not in msg:
            msg["reasoning_content"] = ""
            changed = True

    return changed


def translate_response_payload(payload: dict[str, Any]) -> bool:
    """
    Mutate response JSON in-place.

    Returns True if the payload was modified.
    """
    changed = False
    choices = payload.get("choices")

    if not isinstance(choices, list):
        return False

    for choice in choices:
        if not isinstance(choice, dict):
            continue

        message = choice.get("message")
        if isinstance(message, dict):
            if rename_key(message, "reasoning_content", "reasoning_text"):
                changed = True

        delta = choice.get("delta")
        if isinstance(delta, dict):
            if rename_key(delta, "reasoning_content", "reasoning_text"):
                changed = True

    return changed


def translate_sse_chunk(chunk: bytes) -> bytes:
    """
    Translate SSE data lines only.
    """
    lines: list[bytes] = []

    for line in chunk.split(b"\n"):
        if not line.strip():
            lines.append(line)
            continue

        match = SSE_DATA_PREFIX.match(line)
        if not match:
            lines.append(line)
            continue

        data = line[match.end() :]
        if data.strip() == b"[DONE]":
            lines.append(line)
            continue

        try:
            obj = json.loads(data)
            if isinstance(obj, dict):
                translate_response_payload(obj)
                translated = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                lines.append(b"data: " + translated)
            else:
                lines.append(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            lines.append(line)

    return b"\n".join(lines)


def build_upstream_url(upstream: str, request: web.Request) -> str:
    upstream = upstream.rstrip("/")
    url = upstream + request.path
    if request.query_string:
        url += "?" + request.query_string
    return url


def upstream_host(upstream: str) -> str:
    parsed = urlsplit(upstream)
    return parsed.netloc


def filtered_headers(headers: aiohttp.typedefs.LooseHeaders, blocked: set[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in blocked:
            continue
        out[key] = value
    return out


async def proxy_handler(request: web.Request) -> web.StreamResponse:
    upstream: str = request.app["upstream"]
    session: aiohttp.ClientSession = request.app["session"]

    upstream_url = build_upstream_url(upstream, request)

    raw_body = await request.read()
    forward_body = raw_body
    is_stream = False

    if raw_body:
        try:
            parsed = json.loads(raw_body)
            if isinstance(parsed, dict):
                is_stream = bool(parsed.get("stream", False))
                changed = translate_request_payload(parsed)
                if changed:
                    forward_body = json.dumps(parsed, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    headers = filtered_headers(request.headers, REQUEST_HOP_BY_HOP)
    headers["Host"] = upstream_host(upstream)
    headers["Accept-Encoding"] = "identity"

    try:
        async with session.request(
            method=request.method,
            url=upstream_url,
            headers=headers,
            data=forward_body if forward_body else None,
            timeout=aiohttp.ClientTimeout(total=300),
        ) as resp:
            response_headers = filtered_headers(resp.headers, RESPONSE_HOP_BY_HOP)

            if is_stream and "event-stream" in resp.headers.get("Content-Type", ""):
                response_headers["Content-Type"] = resp.headers.get("Content-Type", "text/event-stream")
                response_headers["Cache-Control"] = "no-cache"
                response_headers["Connection"] = "keep-alive"

                response = web.StreamResponse(
                    status=resp.status,
                    reason=resp.reason,
                    headers=response_headers,
                )
                await response.prepare(request)

                buffer = b""
                async for chunk in resp.content.iter_any():
                    buffer += chunk
                    while b"\n\n" in buffer:
                        event_end = buffer.index(b"\n\n") + 2
                        event_data = buffer[:event_end]
                        buffer = buffer[event_end:]
                        await response.write(translate_sse_chunk(event_data))

                if buffer.strip():
                    await response.write(translate_sse_chunk(buffer))

                await response.write_eof()
                return response

            resp_body = await resp.read()

            if resp_body:
                try:
                    resp_json = json.loads(resp_body)
                    if isinstance(resp_json, dict) and translate_response_payload(resp_json):
                        resp_body = json.dumps(resp_json, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass

            return web.Response(
                status=resp.status,
                reason=resp.reason,
                headers=response_headers,
                body=resp_body,
            )

    except Exception as e:
        log.exception("Upstream request failed")
        return web.Response(status=502, text="Upstream error")


async def health_handler(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def on_startup(app: web.Application) -> None:
    app["session"] = aiohttp.ClientSession()
    log.info("DeepSeek proxy started, upstream=%s", app["upstream"])


async def on_cleanup(app: web.Application) -> None:
    await app["session"].close()


def create_app(upstream: str) -> web.Application:
    app = web.Application()
    app["upstream"] = upstream
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    # Put health first so it is not shadowed by catch-alls.
    app.router.add_get("/health", health_handler)
    app.router.add_route("*", "/v1/{path:.*}", proxy_handler)
    app.router.add_route("*", "/{path:.*}", proxy_handler)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="DeepSeek API reasoning_content proxy")
    parser.add_argument("--port", type=int, default=18200)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--upstream", type=str, default="https://api.deepseek.com")
    args = parser.parse_args()

    app = create_app(args.upstream)
    web.run_app(app, host=args.host, port=args.port, print=lambda msg: log.info(msg))


if __name__ == "__main__":
    main()
