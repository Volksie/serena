# SPDX-License-Identifier: GPL-3.0-or-later

"""A/B test for the asyncio accept-loop hardening (LOCAL PATCH, see serena/util/accept_hardening.py).

Injects one ``OSError(WinError 64)`` into the proactor's ``accept()``, exactly as Windows raises it,
and then asks whether the listener still accepts. Stock CPython must lose the listener; the patched
loop must keep it. Both halves are asserted: a test where the stock arm also survived would mean the
injection no longer reaches the accept path, and the patched result would prove nothing.

One event loop per scenario, created and closed inside the test. An earlier version of this test
created a second ProactorEventLoop inside a coroutine already running on another, so the server was
never served and both arms reported dead.
"""

import asyncio
import sys

import pytest

from serena.util import accept_hardening

WSA_NETNAME_DELETED = 64

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the proactor accept loop is Windows only")


async def _listener_survives_one_transient_accept_error(loop: asyncio.AbstractEventLoop) -> bool:
    accepted: list[int] = []

    class Proto(asyncio.Protocol):
        def connection_made(self, transport):
            accepted.append(1)
            transport.close()

    server = await loop.create_server(Proto, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    # Sanity: it accepts before anything is broken.
    _, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=3)
    w.close()
    await asyncio.sleep(0.3)
    assert accepted, "test broken: no baseline accept"

    # Inject exactly one transient accept failure, as the real one arrives (errno 22, winerror 64).
    real_accept = loop._proactor.accept  # type: ignore[attr-defined]
    fired: list[int] = []

    def flaky_accept(sock):
        if not fired:
            fired.append(1)
            raise OSError(WSA_NETNAME_DELETED, "The specified network name is no longer available", None, WSA_NETNAME_DELETED, None)
        return real_accept(sock)

    loop._proactor.accept = flaky_accept  # type: ignore[attr-defined]

    # Provoke the accept path so the injected error fires.
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=3)
        w.close()
    except Exception:
        pass
    await asyncio.sleep(0.5)
    assert fired, "test broken: the injected error never fired"

    # THE QUESTION: is the listener still serving?
    before = len(accepted)
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=3)
        w.close()
        await asyncio.sleep(0.4)
    except Exception:
        pass
    served = len(accepted) > before
    server.close()
    await server.wait_closed()
    return served


def _run_scenario() -> bool:
    loop = asyncio.ProactorEventLoop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_listener_survives_one_transient_accept_error(loop))
    finally:
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture
def stock_loop():
    accept_hardening.uninstall()
    yield
    accept_hardening.uninstall()


def test_stock_cpython_loses_the_listener(stock_loop) -> None:
    assert not accept_hardening.is_installed()
    assert _run_scenario() is False, "stock CPython kept the listener: the injection no longer reaches accept()"


def test_patched_loop_keeps_the_listener(stock_loop) -> None:
    assert accept_hardening.install() is True
    assert accept_hardening.is_installed()
    assert _run_scenario() is True, "the patched loop lost the listener"


def test_install_is_idempotent(stock_loop) -> None:
    from asyncio import proactor_events

    def current() -> object:
        return vars(proactor_events.BaseProactorEventLoop)["_start_serving"]

    stock = current()
    assert accept_hardening.install() is True
    patched = current()
    assert patched is not stock
    assert accept_hardening.install() is True
    assert current() is patched
    accept_hardening.uninstall()
    assert current() is stock
