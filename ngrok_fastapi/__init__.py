"""Bring ngrok Traffic Policy, pooling, and binding config to FastAPI.

ngrok's own official Python tooling (`ngrok-asgi`, bundled inside `ngrok-python`) already
wraps any ASGI app with zero code changes — that part isn't rebuilt here. What it's missing,
confirmed by reading its entire CLI parser directly: a `--traffic-policy` flag, pooling,
`--binding`, and any config-file support at all (`args.config` is a hard-coded fatal error).
This library fills that gap as a small FastAPI-native alternative, using FastAPI's own
lifespan hook instead of CLI flags — the same fork `ngrok-axum` made for Rust, for the same
reason: per-endpoint config like a Traffic Policy document doesn't compose cleanly as a flag.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, NamedTuple, Optional

import ngrok
from fastapi import FastAPI


class Binding(str, Enum):
    """Ingress configuration. Not part of ngrok's Traffic Policy — confirmed by checking
    the Traffic Policy actions reference directly, so this stays a standalone field rather
    than folded into `traffic_policy`."""

    PUBLIC = "public"
    # Requires `url` to end in ".internal" — enforced by ngrok itself (ERR_NGROK_9029 if it
    # doesn't), not something this library validates.
    INTERNAL = "internal"
    KUBERNETES = "kubernetes"


@dataclass
class Config:
    """Configuration for a single ngrok endpoint.

    Deliberately minimal, same reasoning as the `ngrok-nextjs` and `ngrok-axum` sibling
    projects: the granular per-module builder methods (`basic_auth`, `oauth`, header
    manipulation, CIDR restrictions, etc.) mirror ngrok's old "Edge Modules" system,
    superseded by Traffic Policy. Use `traffic_policy` for all of that instead.
    """

    # Local port this endpoint forwards to.
    port: int = 8000
    # Reserved domain for this endpoint. `None` falls back to the account's default dev
    # domain.
    url: Optional[str] = None
    # Opt in to ngrok endpoint pooling — required if another endpoint on the same session
    # would otherwise land on the same domain. ngrok does not reject or warn on that
    # collision by default: confirmed live against the real SDK, independently across the
    # JS, Rust, and Python SDKs, that two listeners opened with no domain and no pooling all
    # succeed silently, returning the identical URL, with only the most recently opened one
    # actually receiving traffic.
    pooling: bool = False
    # Raw ngrok Traffic Policy document (YAML or JSON), passed straight through. See
    # https://ngrok.com/docs/traffic-policy/.
    traffic_policy: Optional[str] = None
    binding: Optional[Binding] = None


class CollisionError(Exception):
    """Raised before any session is opened when two or more configs would silently collide
    on the same url without pooling enabled."""


def _validate_configs(configs: List[Config]) -> None:
    groups: Dict[str, List[int]] = defaultdict(list)
    for i, config in enumerate(configs):
        groups[config.url or ""].append(i)

    for url, indices in groups.items():
        if len(indices) <= 1:
            continue
        if all(configs[i].pooling for i in indices):
            continue

        label = f'"{url}"' if url else "the account's default dev domain"
        raise CollisionError(
            f"Configs at indices {indices} would all share {label}. Without endpoint "
            "pooling, only the most recently opened listener actually receives traffic — "
            "the rest go silently unreachable. Set pooling=True on each of these configs "
            "to share it intentionally, or give each its own url."
        )


def _configure_builder(builder, config: Config):
    if config.url:
        builder = builder.domain(config.url)
    if config.pooling:
        builder = builder.pooling_enabled(True)
    if config.traffic_policy:
        builder = builder.traffic_policy(config.traffic_policy)
    if config.binding:
        builder = builder.binding(config.binding.value)
    return builder


class _OpenListener(NamedTuple):
    listener: "ngrok.Listener"
    forward_task: "asyncio.Future"


async def _open_listeners(configs: List[Config]) -> List[_OpenListener]:
    _validate_configs(configs)
    session = await ngrok.SessionBuilder().authtoken_from_env().connect()

    opened = []
    for config in configs:
        builder = _configure_builder(session.http_endpoint(), config)
        listener = await builder.listen()
        print(f"Ingress established at: {listener.url()}")
        # listener.forward() is a long-running background operation, not a
        # quick "set up forwarding" call — confirmed directly, isolated from
        # FastAPI: awaiting it inline never returns even after the target
        # port starts accepting connections. It also has to be scheduled
        # *before* the local ASGI server has bound its own port, since this
        # all runs during lifespan startup, which uvicorn completes *before*
        # calling `loop.create_server()` for the app itself (confirmed by
        # reading uvicorn's own source) — so awaiting it inline here would
        # deadlock: forward() waiting on a port that only gets bound once
        # this same startup sequence finishes.
        forward_task = asyncio.ensure_future(listener.forward(f"localhost:{config.port}"))
        opened.append(_OpenListener(listener, forward_task))
    return opened


def attach(app: FastAPI, config: Optional[Config] = None) -> None:
    """Opens a single ngrok endpoint per `config` and ties its lifetime to `app`."""
    attach_many(app, [config or Config()])


def attach_many(app: FastAPI, configs: List[Config]) -> None:
    """Opens one ngrok endpoint per entry in `configs`, all on a single session, and ties
    their lifetime to `app`.

    Wraps the app's existing lifespan (composing with it, not replacing it, whether the app
    was given an explicit `lifespan=` or not) rather than using the `@app.on_event` decorators
    — those are soft-deprecated in FastAPI in favor of the lifespan protocol, and this library
    aims to be the modern path, not propagate the deprecated one.
    """
    existing_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def wrapped_lifespan(app: FastAPI):
        opened = await _open_listeners(configs)
        try:
            async with existing_lifespan(app) as state:
                yield state
        finally:
            for opened_listener in opened:
                opened_listener.forward_task.cancel()
                await opened_listener.listener.close()

    app.router.lifespan_context = wrapped_lifespan
