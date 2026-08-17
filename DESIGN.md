# ngrok + FastAPI Integration — Design Sketch (v0)

Third in the "ngrok SDK + popular framework" series, after
[`ngrok-nextjs`](../ngrok-nextjs) (JS SDK + Next.js) and [`ngrok-axum`](../ngrok-axum) (Rust
SDK + Axum). This one targets the Python SDK.

## Research: why FastAPI

**Popularity**: FastAPI has genuinely dethroned Django and Flask — the first time in Python's
history neither has been the most-used web framework. JetBrains' State of Python 2025 survey
(30,000+ respondents): FastAPI 38%, Django 35%, Flask 34%. Grew from 29%→38% in two years,
passed Flask in GitHub stars (88k vs 68.4k, Dec 2025), matches Django at ~9M monthly PyPI
downloads, and is now the default choice for new projects — over 50% of Fortune 500 companies
were using it in production by mid-2025.

**Relevance**: ngrok has a dedicated official docs page — `ngrok.com/docs/using-ngrok-with/fastAPI`
— naming FastAPI specifically, with pinned dependency versions and a lifecycle-managed
integration pattern. No equivalent page found for Flask or Django. Same signal as Axum having
the dedicated Cargo feature in the Rust project.

## The problem — confirmed by reading the SDK's own shipped tooling, not assumed

Unlike the other two projects, Python's ecosystem is not short a wrapper. `ngrok-python`
already bundles **`ngrok-asgi`**, an official CLI entry point (`[project.scripts] ngrok-asgi =
"ngrok:__main__.asgi_cli"` — not a separate package, ships inside `ngrok-python` itself) that
wraps *any* ASGI server startup command with zero code changes:

```
ngrok-asgi uvicorn app:app
ngrok-asgi gunicorn mysite.asgi:application -k uvicorn.workers.UvicornWorker
```

So the CLI-wrapper niche — the thing that took real engineering work for Next.js — is already
solved, generically, for the entire ASGI ecosystem (FastAPI, Django, Starlette, anything).
Nothing to rebuild there.

**But `ngrok-asgi` is stuck on ngrok's old config paradigm.** Read the entire parser
(`python/ngrok/ngrok_parser.py`, 138 lines, every flag) and the listener-configuration logic
(`python/ngrok/__main__.py`) directly:

- Covers nearly the whole legacy "Edge Modules" surface: `--allow-cidr`/`--deny-cidr`,
  `--basic-auth`, `--circuit-breaker`, `--compression`, `--domain`, `--oauth-provider`/
  `--oidc`, `--proxy-proto`, `--request-header`/`--response-header`, `--scheme`,
  `--webhook-verification`, `--websocket-tcp-conversion`.
- **No `--traffic-policy` flag exists at all.** Confirmed by reading the full file — not a
  grep miss. Given both prior projects' already-established, verified conclusion that Traffic
  Policy is ngrok's deliberate architectural replacement for this whole per-module system,
  `ngrok-asgi` has no path onto the thing ngrok is actually investing in.
- **No pooling** (`--pooling-enabled` equivalent) and **no `--binding`** (public/internal/
  kubernetes) anywhere.
- **Config files are explicitly disabled**: `if args.config: logging.fatal("Config file not
  supported. Exiting.")`. Everything goes through CLI flags only.
- File-descriptor passing is explicitly blocked too (`args.fd` → fatal error), even though the
  raw SDK's own `uvicorn-ngrok.py` example uses `ngrok.fd()` directly on non-Windows.

So the real, confirmed gap isn't "Python needs a tunnel-wrapper" — it's "FastAPI users have no
official path to Traffic Policy, pooling, or binding," while the two prior projects already
established those are the parts of ngrok's config surface actually worth wrapping.

## Shape of the solution: a library, not a CLI — same fork as the Rust project

- **Next.js**: no black-box command existed to add config to → CLI wrapper was the only way in.
- **Axum / FastAPI**: the developer already owns their own app object (`main.rs` / the FastAPI
  `app` instance) → a CLI can't reach into that to apply per-endpoint config the way a library
  call can. `ngrok-asgi` proves this in practice: it's a CLI, and it's exactly the config
  fields requiring per-endpoint semantics (Traffic Policy documents, structured pooling)
  that it can't cleanly expose as flags — hence why they're missing.

FastAPI's idiomatic hook is its `lifespan` context manager (startup/shutdown tied to the
app's actual lifetime). Unlike Axum, Python's ngrok SDK doesn't need to *become* the server —
`ngrok.forward(addr=...)` just opens a listener and forwards to a local port, exactly like the
existing `using-ngrok-with/fastAPI` docs example already does. So there's no connection-loop
regression to fix here (no `axum::Server`-style breaking change in this ecosystem) — the gap
is purely the config surface, which makes this a smaller, more contained build than
`ngrok-axum` was.

Working name: `ngrok-fastapi` (PyPI — checked directly, not taken; `fastapi-ngrok` also free).
No real competing prior art: broad PyPI search turned up only `pyngrok` (old, unofficial,
wraps the downloaded `ngrok` CLI binary directly — same relationship as legacy npm `ngrok` and
the Rust `ngrok-wrapper` crate had to their respective official SDKs) and a couple unrelated
tools that merely mention ngrok as an option.

Core API sketch:

```python
from fastapi import FastAPI
import ngrok_fastapi

app = FastAPI()
ngrok_fastapi.attach(app, ngrok_fastapi.Config(
    port=8000,
    url="myapp.ngrok.app",          # None = account's default dev domain
    pooling=False,
    traffic_policy="""
on_http_request:
  - actions:
      - type: basic-auth
        config:
          credentials:
            - "user:password123"
""",
    binding=None,                    # Binding.INTERNAL, etc.
))
```

`attach()` wraps the app's existing lifespan context manager (composing with it, not
replacing it) so the tunnel opens when the app starts and closes when it stops — tying the
tunnel's lifetime to the app's, the same care the JS project put into process lifecycle, just
at the tunnel level instead of the process level since there's no separate process here.

## The most important bug this project found — a real deadlock, not a testing artifact

Live-testing `attach()` end-to-end (not just unit tests) surfaced a genuine deadlock that
would have shipped broken:

1. **Confirmed directly in uvicorn's own source** (`server.py`): `await self.lifespan.startup()`
   runs completely *before* `loop.create_server(...)` — i.e. uvicorn finishes running the
   ASGI app's startup sequence before it ever binds the port the app will actually serve on.
2. **Confirmed by isolating it from FastAPI entirely**: `await listener.forward(addr)` in
   `ngrok-python` does not return once forwarding is "set up" — it's a long-running
   background operation that keeps running for as long as forwarding is active. Wrote a
   minimal standalone script (session → listener → `forward()` to a port nothing was
   listening on) and confirmed with `timeout`: it hangs indefinitely, no exception, no
   timeout of its own.

Combined: the first version of `_open_listeners` called `await listener.forward(f"localhost:{port}")`
directly inside the wrapped lifespan startup — forwarding to the *same app's own port*,
before uvicorn had bound it. That's a hard deadlock: `forward()` waits on a port that only
gets bound once lifespan startup (which includes this very call) finishes. In practice this
showed up as the app hanging forever at "Waiting for application startup," and every request
getting `ERR_NGROK_3200`/connection-refused, with no timeout or error surfaced anywhere to
explain why.

**Fix**: schedule `forward()` as a background task (`asyncio.ensure_future`, not
`asyncio.create_task` — `Listener.forward()` returns a native Future, not a plain coroutine,
so `create_task` rejects it) instead of awaiting it inline, and cancel that task alongside
`listener.close()` on shutdown. Verified the full fix live: `Application startup complete`
now appears (it never did before), a real request gets the actual FastAPI response body, and
`SIGINT` triggers a clean shutdown in ~2 seconds.

This is the kind of bug that only a real end-to-end request against a running app would ever
surface — unit tests of the collision-guard logic wouldn't have caught it, and neither would
a quick "does it compile/import" check.

## Config surface — same conclusions as the other two projects, translated again

`Config`: `port`, `url`, `pooling`, `traffic_policy`, `binding` — identical reasoning as
`ngrok-nextjs` and `ngrok-axum`. Not re-deriving it a third time: the granular per-module
fields are superseded by Traffic Policy (confirmed against ngrok's actions reference in the
first project), and `binding` stays standalone since it has no Traffic Policy equivalent
(also already confirmed). `port` plays the same role `upstream` did for the Rust project —
the local address to forward to.

## Confirmed since first draft

- **Collision guard parity — verified independently, not assumed.** Wrote a minimal
  standalone Python script (`SessionBuilder().authtoken_from_env().connect()`, then
  `session.http_endpoint().listen()` called twice, no domain, no pooling) and ran it live
  against the real `ngrok-python` SDK. Identical result to both JS and Rust: both calls
  succeeded, no error either time, both returned the identical URL. Three independent SDKs,
  three identical results — this is conclusively an ngrok platform behavior, not a
  binding-specific quirk in any one language. `ngrok-fastapi` needs the same guard the other
  two projects built, confirmed rather than inherited on faith.

## Open questions

- **Multi-endpoint**: `ngrok-nextjs` and `ngrok-axum` both ended up supporting multiple
  endpoints (`attach_many`?), with a collision guard for ngrok's confirmed same-session
  silent-collision behavior. Worth building here too, but FastAPI apps are more commonly
  single-app-per-process than Rust binaries are — check whether the multi-endpoint case is
  actually a real pattern for Python before porting the mechanism reflexively.
- **Upstreaming to `ngrok-asgi` itself**: adding `--traffic-policy`/`--pooling-enabled`/
  `--binding` to the existing official CLI would fix this for the *entire* ASGI ecosystem
  (Flask, Django, Starlette too), not just FastAPI. Worth doing either alongside or instead of
  the library — smaller, well-scoped contribution to code that already exists and is
  officially maintained. Not mutually exclusive with building `ngrok-fastapi`.
- **Distribution**: same open question as both prior projects.
