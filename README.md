# deepseek-reasoning-proxy

A lightweight reverse proxy that fixes opencode's `reasoning_content` bug when using DeepSeek V4 Pro.

## The Problem

When using [opencode](https://github.com/opencode-ai/opencode) with DeepSeek's API, multi-turn conversations with tool calls fail with **HTTP 400 errors**. The root cause is a field name mismatch:

| | opencode (SDK) | DeepSeek (API) |
|---|---|---|
| Reasoning field | `reasoning_text` | `reasoning_content` |

opencode's SDK internally uses `reasoning_text` — a field borrowed from GitHub Copilot's schema. DeepSeek's native API, however, uses `reasoning_content`. This mismatch causes two critical issues:

1. **Outbound**: When opencode sends assistant messages back in multi-turn conversations, it includes `reasoning_text` instead of `reasoning_content`. DeepSeek doesn't recognize the field and rejects the request.
2. **Missing field**: DeepSeek V4 Pro **requires** `reasoning_content` to be present in assistant messages when tool calls are involved. opencode's SDK strips this field entirely, so even if the name were correct, the field would be absent — triggering a 400 error.

## How It Works

This proxy sits between opencode and the DeepSeek API, performing **bidirectional field name translation**:

```
┌──────────┐         ┌────────────────────────┐         ┌───────────────┐
│  opencode │ ──────► │  deepseek-reasoning-proxy │ ──────► │  DeepSeek API  │
│          │ ◄────── │                        │ ◄────── │               │
└──────────┘         └────────────────────────┘         └───────────────┘

  Request flow:   reasoning_text  →  reasoning_content
                  + inject empty reasoning_content if missing in assistant messages

  Response flow:  reasoning_content  →  reasoning_text
                  (applies to both regular JSON and SSE streaming responses)
```

### Translation Rules

**On request (opencode → DeepSeek):**
- Rename `reasoning_text` → `reasoning_content` in assistant messages
- If `reasoning_content` is missing from an assistant message, inject it as an empty string `""` (required by DeepSeek V4 Pro)

**On response (DeepSeek → opencode):**
- Rename `reasoning_content` → `reasoning_text` in `message` objects (non-streaming)
- Rename `reasoning_content` → `reasoning_text` in `delta` objects (SSE streaming)
- SSE chunks are parsed, translated, and re-emitted in real-time

## Installation

### Prerequisites

- Python 3.10+
- [aiohttp](https://pypi.org/project/aiohttp/) >= 3.9.0

### Install

```bash
git clone https://github.com/Whale-Dolphin/deepseek-reasoning-proxy.git
cd deepseek-reasoning-proxy
pip install -r requirements.txt
```

## Usage

### Direct Run

```bash
python3 proxy.py
```

Options:

| Flag | Default | Description |
|---|---|---|
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `18200` | Bind port |
| `--upstream` | `https://api.deepseek.com` | Upstream DeepSeek API URL |

```bash
# Custom port and upstream
python3 proxy.py --port 8080 --upstream https://api.deepseek.com
```

### Docker

```bash
docker build -t deepseek-reasoning-proxy .
docker run -p 18200:18200 deepseek-reasoning-proxy
```

To override default settings:

```bash
docker run -p 8080:8080 deepseek-reasoning-proxy \
  python3 proxy.py --host 0.0.0.0 --port 8080
```

### systemd (User Service)

```bash
# Copy the service template
mkdir -p ~/.config/systemd/user/
cp contrib/systemd/deepseek-proxy.service ~/.config/systemd/user/

# Edit the ExecStart path to match your install location
sed -i "s|/path/to/deepseek-reasoning-proxy|$(pwd)|g" ~/.config/systemd/user/deepseek-proxy.service

# Enable and start
systemctl --user daemon-reload
systemctl --user enable --now deepseek-proxy
```

## opencode Configuration

Point your DeepSeek provider's `baseURL` to the proxy instead of the direct API:

```json
{
  "provider": {
    "name": "deepseek",
    "baseURL": "http://127.0.0.1:18200",
    "apiKey": "your-deepseek-api-key"
  }
}
```

Or in your opencode config file:

```yaml
providers:
  deepseek:
    baseURL: http://127.0.0.1:18200
    apiKey: ${DEEPSEEK_API_KEY}
```

The proxy forwards all requests to the real DeepSeek API, transparently translating field names in both directions. Your API key is passed through unchanged.

## Architecture

```
                    ┌──────────────────────────────────┐
                    │      deepseek-reasoning-proxy     │
                    │                                  │
  opencode  ──────► │  ┌─────────────────────────┐     │ ──────►  DeepSeek API
  (reasoning_text)  │  │  Request Translation    │     │        (reasoning_content)
                    │  │  • reasoning_text →      │     │
                    │  │    reasoning_content     │     │
                    │  │  • Inject empty          │     │
                    │  │    reasoning_content     │     │
                    │  │    if missing            │     │
                    │  └─────────────────────────┘     │
                    │                                  │
                    │  ┌─────────────────────────┐     │
  opencode  ◄────── │  │  Response Translation   │     │ ◄──────  DeepSeek API
  (reasoning_text)  │  │  • reasoning_content →  │     │        (reasoning_content)
                    │  │    reasoning_text       │     │
                    │  │  • Handles JSON & SSE   │     │
                    │  │    streaming            │     │
                    │  └─────────────────────────┘     │
                    │                                  │
                    │  Port: 18200 (default)           │
                    │  Health: GET /health → "ok"      │
                    └──────────────────────────────────┘
```

## Health Check

```bash
curl http://127.0.0.1:18200/health
# → ok
```

## License

[MIT](LICENSE) © 2025 Whale-Dolphin
