"""Synthetic reproduction of the 504 timeout boundary (V2 M1; gap §6.11).

Gap analysis §6.11: "Repository code alone does not prove whether the observed
504 came from CloudFront, ALB, Uvicorn, the LLM client, or a browser timeout.
Before M0 deletes useful fixtures, M1 must inventory every synchronous endpoint
and each deployed timeout, reproduce the failing boundary, and record which
calls must move behind M3's durable 202 polling contract."

This reproduces the *mechanism* in isolation: a gateway with a read timeout in
front of an origin that answers too late. It is the shape of the deployed path

    browser -> CloudFront (origin_read_timeout) -> ALB (idle_timeout) -> Uvicorn

and it demonstrates the property that matters: past the gateway's read timeout
the caller receives a **gateway-generated 504**, never the origin's eventual
response — and the origin keeps working, unaware, so any writes it performs
still happen after the client has been told the request failed.

Deliberately safe:

- Everything binds `127.0.0.1` on an ephemeral port. No external network.
- Nothing is written to disk, no database is touched, no credential is read.
- Nothing is deployed and no AWS resource is created or changed. The deployed
  timeout *values* are gathered separately, read-only, and recorded in
  `docs/evidence/m1-timeout-inventory.md`.

Run the full-scale reproduction at the deployed values:

    python -m scripts.m1_timeout_probe --gateway-timeout 60 --origin-delay 75

`tests/test_timeout_boundary.py` runs the same code at small values, so the
finding stays protected by a test without adding a minute to the gate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict, dataclass

_GATEWAY_504_BODY = b"<html><body>504 Gateway Timeout (synthetic gateway)</body></html>"
_ORIGIN_BODY = b'{"origin": "answered"}'


@dataclass(frozen=True)
class ProbeResult:
    gateway_timeout: float
    origin_delay: float
    status: int
    # Which layer produced the response the client actually saw.
    answered_by: str
    client_elapsed: float
    # Whether the origin went on to finish its work after the client gave up.
    origin_completed: bool
    origin_elapsed: float | None


async def _start_origin(delay: float, state: dict) -> asyncio.Server:
    """An origin that takes `delay` seconds to answer — a stand-in for a
    synchronous LLM or document request."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        started = time.monotonic()
        try:
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
            await asyncio.sleep(delay)
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: %d\r\n"
                b"Connection: close\r\n\r\n" % len(_ORIGIN_BODY)
                + _ORIGIN_BODY
            )
            await writer.drain()
            # The origin finished its work regardless of whether the gateway
            # was still listening. That is the important part: an aborted
            # request does not abort the work behind it.
            state["origin_completed"] = True
            state["origin_elapsed"] = time.monotonic() - started
        except (ConnectionResetError, BrokenPipeError):
            # The gateway hung up. The work above still ran.
            state["origin_elapsed"] = time.monotonic() - started
        finally:
            writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


async def _start_gateway(origin_port: int, read_timeout: float) -> asyncio.Server:
    """A reverse proxy with a read timeout, like CloudFront's
    `origin_read_timeout` or an ALB's `idle_timeout`: past the deadline it
    generates its own 504 rather than waiting for the origin."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = b""
        while True:
            line = await reader.readline()
            request += line
            if line in (b"\r\n", b"\n", b""):
                break

        origin_writer = None
        try:
            origin_reader, origin_writer = await asyncio.open_connection(
                "127.0.0.1", origin_port
            )
            origin_writer.write(request)
            await origin_writer.drain()
            # The deadline applies to the wait for the origin's first byte.
            status_line = await asyncio.wait_for(
                origin_reader.readline(), timeout=read_timeout
            )
            remainder = await origin_reader.read()
            writer.write(status_line + remainder)
        except (asyncio.TimeoutError, TimeoutError):
            writer.write(
                b"HTTP/1.1 504 Gateway Timeout\r\n"
                b"Content-Type: text/html\r\n"
                b"Content-Length: %d\r\n"
                b"Connection: close\r\n\r\n" % len(_GATEWAY_504_BODY)
                + _GATEWAY_504_BODY
            )
        finally:
            try:
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                pass
            writer.close()
            if origin_writer is not None:
                origin_writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


async def _request(port: int, path: str = "/slow") -> tuple[int, bytes, float]:
    started = time.monotonic()
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode()
    )
    await writer.drain()
    raw = await reader.read()
    writer.close()
    elapsed = time.monotonic() - started
    status = int(raw.split(b" ", 2)[1]) if raw.startswith(b"HTTP/") else 0
    return status, raw, elapsed


async def reproduce(*, gateway_timeout: float, origin_delay: float) -> ProbeResult:
    """Run one origin/gateway pair and make a single request through it."""
    state: dict = {"origin_completed": False, "origin_elapsed": None}

    origin = await _start_origin(origin_delay, state)
    origin_port = origin.sockets[0].getsockname()[1]
    gateway = await _start_gateway(origin_port, gateway_timeout)
    gateway_port = gateway.sockets[0].getsockname()[1]

    try:
        status, raw, elapsed = await _request(gateway_port)
        answered_by = "gateway" if b"synthetic gateway" in raw else "origin"

        # Give the origin the chance to finish after the client gave up, so the
        # result can report whether the work continued behind the 504.
        if not state["origin_completed"]:
            await asyncio.sleep(max(0.0, origin_delay - elapsed) + 0.25)

        return ProbeResult(
            gateway_timeout=gateway_timeout,
            origin_delay=origin_delay,
            status=status,
            answered_by=answered_by,
            client_elapsed=round(elapsed, 3),
            origin_completed=bool(state["origin_completed"]),
            origin_elapsed=(
                round(state["origin_elapsed"], 3)
                if state["origin_elapsed"] is not None
                else None
            ),
        )
    finally:
        gateway.close()
        origin.close()
        await gateway.wait_closed()
        await origin.wait_closed()


def _under_budget_control_delay(gateway_timeout: float) -> float:
    """Delay for the `--also-under-budget` control run.

    Must always be strictly less than `gateway_timeout`, however small
    `gateway_timeout` is (M1-L-001). An earlier version used
    `max(0.5, gateway_timeout / 20)` — a flat 0.5s floor — which silently broke
    for any `gateway_timeout` below 1s: `--gateway-timeout 0.2` produced a
    control delay of 0.5s, i.e. a "control" that was itself past the deadline
    and came back as a 504, exactly like the boundary case it was supposed to
    contrast with. Capping the floor at half of `gateway_timeout` instead
    keeps the delay proportionally representative for realistic (multi-second)
    timeouts while staying correct at any scale.
    """
    return min(gateway_timeout / 2, max(gateway_timeout / 20, 0.05))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gateway-timeout",
        type=float,
        default=60.0,
        help="Gateway read timeout in seconds. The deployed CloudFront "
             "origin_read_timeout and ALB idle_timeout are both 60.",
    )
    parser.add_argument(
        "--origin-delay",
        type=float,
        default=75.0,
        help="How long the synthetic origin takes to answer.",
    )
    parser.add_argument(
        "--also-under-budget",
        action="store_true",
        help="Also run a control where the origin answers inside the deadline.",
    )
    args = parser.parse_args()

    async def run() -> None:
        results = []
        if args.also_under_budget:
            results.append(
                await reproduce(
                    gateway_timeout=args.gateway_timeout,
                    origin_delay=_under_budget_control_delay(args.gateway_timeout),
                )
            )
        results.append(
            await reproduce(
                gateway_timeout=args.gateway_timeout,
                origin_delay=args.origin_delay,
            )
        )
        for result in results:
            print(json.dumps(asdict(result), indent=2))

    asyncio.run(run())


if __name__ == "__main__":
    main()
