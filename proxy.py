#!/usr/bin/env python3
"""
DeepSeek API reverse proxy that fixes opencode's reasoning_content bug.

opencode's SDK uses `reasoning_text` (Copilot field) instead of `reasoning_content`
(DeepSeek's native field). This proxy translates between the two.

Request:  reasoning_text → reasoning_content in assistant messages
          Also injects empty reasoning_content if missing (required by DeepSeek API)
Response: reasoning_content → reasoning_text in message/delta objects (including SSE)

Usage:
  python3 proxy.py [--port 18200] [--upstream https://api.deepseek.com]
"""

import json
import argparse
import logging
import re

import aiohttp
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("deepseek-proxy")


def rename_key(obj: dict, old_key: str, new_key: str) -> dict:
    if old_key in obj:
        obj[new_key] = obj.pop(old_key)
    return obj


def translate_request_body(body: dict) -> dict:
    if "messages" in body:
        for msg in body["messages"]:
            if msg.get("role") == "assistant":
                rename_key(msg, "reasoning_text", "reasoning_content")
                if "reasoning_content" not in msg:
                    msg["reasoning_content"] = ""
    return body


def translate_response_body(body: dict) -> dict:
    if "choices" in body:
        for choice in body["choices"]:
            if "message" in choice:
                rename_key(choice["message"], "reasoning_content", "reasoning_text")
            if "delta" in choice:
                rename_key(choice["delta"], "reasoning_content", "reasoning_text")
    return body


SSE_DATA_PREFIX = re.compile(rb"^data:\s*")


def translate_sse_chunk(chunk: bytes) -> bytes:
    lines = []
    for line in chunk.split(b"\n"):
        if not line.strip():
            lines.append(line)
            continue
        match = SSE_DATA_PREFIX.match(line)
        if not match:
            lines.append(line)
            continue
        data = line[match.end():]
        if data.strip() == b"[DONE]":
            lines.append(line)
            continue
        try:
            obj = json.loads(data)
            translate_response_body(obj)
            translated = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            lines.append(b"data: " + translated)
        except (json.JSONDecodeError, UnicodeDecodeError):
            lines.append(line)
    return b"\n".join(lines)


async def proxy_handler(request: web.Request) -> web.StreamResponse:
    upstream: str = request.app["upstream"]
    session: aiohttp.ClientSession = request.app["session"]

    upstream_url = upstream.rstrip("/") + request.path
    if request.query_string:
        upstream_url += "?" + request.query_string

    body = await request.read()
    is_stream = False
    if body:
        try:
            body_json = json.loads(body)
            translate_request_body(body_json)
            is_stream = body_json.get("stream", False)
            body = json.dumps(body_json, ensure_ascii=False).encode("utf-8")
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    headers = {}
    for key, value in request.headers.items():
        if key.lower() in ("host", "content-length", "transfer-encoding", "accept-encoding"):
            continue
        headers[key] = value
    headers["Host"] = upstream.split("//")[1].split("/")[0].split(":")[0]
    headers["Accept-Encoding"] = "identity"

    try:
        resp = await session.request(
            method=request.method,
            url=upstream_url,
            headers=headers,
            data=body if body else None,
            timeout=aiohttp.ClientTimeout(total=300),
        )
    except Exception as e:
        log.error(f"Upstream request failed: {e}")
        return web.Response(status=502, text=f"Upstream error: {e}")

    if is_stream and resp.content_type and "event-stream" in resp.content_type:
        response = web.StreamResponse(
            status=resp.status,
            reason=resp.reason,
            headers={
                "Content-Type": resp.content_type or "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
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
    else:
        resp_body = await resp.read()
        if resp_body:
            try:
                resp_json = json.loads(resp_body)
                translate_response_body(resp_json)
                resp_body = json.dumps(resp_json, ensure_ascii=False).encode("utf-8")
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        return web.Response(status=resp.status, body=resp_body)


async def health_handler(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def on_startup(app: web.Application):
    app["session"] = aiohttp.ClientSession()
    log.info(f"DeepSeek proxy started, upstream={app['upstream']}")


async def on_cleanup(app: web.Application):
    await app["session"].close()


def create_app(upstream: str) -> web.Application:
    app = web.Application()
    app["upstream"] = upstream
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_route("*", "/v1/{path:.*}", proxy_handler)
    app.router.add_route("*", "/{path:.*}", proxy_handler)
    app.router.add_get("/health", health_handler)
    return app


def main():
    parser = argparse.ArgumentParser(description="DeepSeek API reasoning_content proxy")
    parser.add_argument("--port", type=int, default=18200)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--upstream", type=str, default="https://api.deepseek.com")
    args = parser.parse_args()
    app = create_app(args.upstream)
    web.run_app(app, host=args.host, port=args.port, print=lambda msg: log.info(msg))


if __name__ == "__main__":
    main()
