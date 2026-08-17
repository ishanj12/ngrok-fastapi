# ngrok-fastapi

Bring ngrok Traffic Policy, pooling, and binding config to FastAPI — via FastAPI's own
lifespan hook, with one call:

```python
from fastapi import FastAPI
import ngrok_fastapi

app = FastAPI()
ngrok_fastapi.attach(app, ngrok_fastapi.Config(port=8000))
```

## What it is

A small FastAPI-native library, not a CLI. `attach()`/`attach_many()` wrap an app's existing
lifespan context manager (composing with it, not replacing it) so an ngrok tunnel opens when
the app starts and closes when it stops. Config — reserved domain, pooling, a raw Traffic
Policy document, ingress binding — is passed as real Python objects (`Config`, `Binding`)
instead of CLI flags or files.

This sits alongside, not instead of, ngrok's own official Python tooling —
**`ngrok-asgi`**, a CLI entry point bundled inside `ngrok-python` itself (not a separate
package), which already wraps any ASGI app with zero code changes:

```
ngrok-asgi uvicorn app:app
```

`ngrok-asgi` covers the legacy per-module config surface (`--basic-auth`, `--oauth-provider`,
`--allow-cidr`, etc.) but — confirmed by reading its entire CLI parser directly — has no
`--traffic-policy` flag, no pooling support, no `--binding`, and explicitly disables config
files (`args.config` → hard-coded fatal error). `ngrok_fastapi.attach()` covers exactly that
gap.

## Setup

1. Sign up at [ngrok.com](https://ngrok.com) and grab an authtoken from
   [the dashboard](https://dashboard.ngrok.com/authtokens).
2. Set it as an env var: `NGROK_AUTHTOKEN=your_token_here`
3. `pip install ngrok-fastapi` and call `attach()` — see [`examples/basic.py`](examples/basic.py).

## Config

```python
ngrok_fastapi.attach(app, ngrok_fastapi.Config(
    port=8000,
    url="your-reserved-domain.ngrok.app",  # None = account's default dev domain
    pooling=False,
    traffic_policy="""
on_http_request:
  - actions:
      - type: basic-auth
        config:
          credentials:
            - "user:password123"
""",
    binding=None,  # ngrok_fastapi.Binding.INTERNAL, etc.
))
```

See [`examples/with_config.py`](examples/with_config.py) for a working example.

| Field | Type | Description |
|---|---|---|
| `port` | `int` | Local port this endpoint forwards to. |
| `url` | `Optional[str]` | Reserved domain for this endpoint. `None` falls back to the account's default dev domain. |
| `pooling` | `bool` | Opt in to ngrok endpoint pooling — required if another endpoint on the same session would otherwise collide on the same domain. See [Collisions](#collisions). |
| `traffic_policy` | `Optional[str]` | A raw [ngrok Traffic Policy](https://ngrok.com/docs/traffic-policy/) document (YAML or JSON) — the mechanism for auth, IP restrictions, header manipulation, webhook verification, and more. |
| `binding` | `Optional[Binding]` | `PUBLIC` / `INTERNAL` / `KUBERNETES` ingress configuration. Not part of Traffic Policy (checked ngrok's actions reference directly — no equivalent exists), so it stays a standalone field. `INTERNAL` requires `url` to end in `.internal`, enforced by ngrok itself (`ERR_NGROK_9029` if it doesn't). |

## Multiple endpoints

`attach_many(app, configs)` opens one endpoint per `Config`, all on a single session, and
ties all of them to the same app's lifespan. `attach(app, config)` is just
`attach_many(app, [config])`.

## Collisions

- **Same-session, no domain / same domain, no pooling**: does **not** error on its own —
  confirmed live, independently, against all three SDKs in this series (JS, Rust, Python):
  two listeners opened this way both succeed with the identical URL, no error either time,
  only the most recently opened one actually receiving traffic. `attach_many` guards against
  it before opening any session: pass configs that would collide without `pooling=True` on
  all of them, and it raises `CollisionError` immediately.
- **Cross-session claim** (another running agent, or a dashboard-configured Cloud Endpoint
  already bound to that domain): ngrok rejects this loudly on its own (`ERR_NGROK_334`).

## A real deadlock this project found and fixed

Live end-to-end testing (not just unit tests) surfaced a genuine bug: `uvicorn` runs the
ASGI app's lifespan *startup* completely before binding the port the app serves on
(confirmed by reading `uvicorn`'s own source). Separately, `ngrok-python`'s
`listener.forward(addr)` is a long-running background operation, not a quick "set up
forwarding" call — awaiting it inline never returns, even after the target starts accepting
connections (confirmed in isolation from FastAPI entirely).

Combined, calling `await listener.forward(...)` directly inside lifespan startup — targeting
the same app's own port, before uvicorn has bound it — is a hard deadlock. The fix:
`forward()` is scheduled as a background task (`asyncio.ensure_future`, not
`asyncio.create_task` — it returns a native Future, not a plain coroutine) instead of being
awaited inline, and cancelled alongside `listener.close()` on shutdown. This is why
`ngrok_fastapi.attach()` exists as more than a thin pass-through — the naive version of this
integration deadlocks on every request.

## Development

```
python3 -m venv venv && source venv/bin/activate
pip install -e ".[dev]"
pytest
python examples/basic.py
```

See [`DESIGN.md`](DESIGN.md) for the design rationale and everything confirmed empirically
along the way.

## License

MIT OR Apache-2.0
