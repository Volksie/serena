# SPDX-License-Identifier: GPL-3.0-or-later

"""Keep the asyncio listening socket alive across transient accept() errors on Windows.

LOCAL PATCH (CodeMem). PERMANENT: an upgrade reverts it, re-apply it. Not proposed upstream, because
the bug is CPython's, not Serena's, and the fix is a monkey-patch of a private asyncio method.

THE BUG. In Lib/asyncio/proactor_events.py, ``BaseProactorEventLoop._start_serving``, the accept
loop treats any ``OSError`` from ``accept()`` as fatal to the *listening* socket and re-arms only on
the success path::

    except OSError as exc:
        if sock.fileno() != -1:
            self.call_exception_handler({'message': 'Accept failed on a socket', ...})
            sock.close()                  # closes the LISTENING socket
    else:
        self._accept_futures[sock.fileno()] = f
        f.add_done_callback(loop)         # re-arms ONLY on the success path

So one transient, per-connection error stops the server accepting for good while the process, the
event loop and the web dashboard keep running - which is why watching the process never catches it.
Identical in CPython 3.13 and 3.14. The Unix path in ``selector_events.py`` handles the same class of
error by design (``except ConnectionAbortedError: continue``).

OBSERVED on a streamable-http server at 127.0.0.1:24290 as ``Accept failed on a socket`` +
``OSError [WinError 64]`` (ERROR_NETNAME_DELETED - the peer vanished before accept() completed):
15 times in three days on Serena 1.7.0 during benchmark runs (one fresh client per question, so
constant connect/disconnect churn), and again 82 seconds into a cold start of 2.0.0.dev0 on
2026-09-19, when a client hit the 45s tool timeout and reset. Each one was an outage until a watchdog
restarted the server - and a restart pays the language servers' warm-up again, which is the cost a
long-lived server exists to avoid.

THIS PATCH restores the Unix behaviour: a transient error re-arms the accept loop instead of closing
the listener. A genuinely dead listening socket (``fileno() == -1``) still ends the loop, and a
bounded retry cap stops a persistent failure becoming a spin.

FATALITY IS DECIDED BY THE SOCKET, NOT BY THE ERROR CODE. The first draft of this patch decided from
``errno`` and its own test caught it: Windows derives errno from the winerror, and WinError 64 arrives
as errno 22 (EINVAL) - ``OSError(22, 'The specified network name is no longer available', None, 64,
None)`` - which an allow/deny list classified as unrecoverable. The listening socket's own validity is
the only reliable signal.

WHY A MONKEY-PATCH. uvicorn hardcodes ``asyncio.ProactorEventLoop`` on Windows
(uvicorn/loops/asyncio.py) and never consults the event-loop policy, so switching to the selector
loop via policy is silently ignored. Patching the class method applies however the loop is built, as
long as :func:`install` runs before the loop starts serving. It used to live in ``sitecustomize.py``
in the venv, which every ``uv tool install`` silently dropped; as a module called from
``start_mcp_server`` it arrives with the install like every other fork change.
"""

import logging
import sys

log = logging.getLogger(__name__)

_REARM_DELAY = 0.05
"""seconds to wait before re-arming accept() after a transient error"""
_MAX_CONSECUTIVE = 200
"""consecutive transient errors after which the listener is closed after all, to stop a hot loop"""

_state: dict[str, object] = {"original": None}
"""the stock method under "original" once installed, kept so :func:`uninstall` can restore it (tests)"""


def is_installed() -> bool:
    return _state["original"] is not None


def install() -> bool:
    """Patch ``BaseProactorEventLoop._start_serving`` so transient accept() errors keep the listener.

    :return: True if the patch is (now) in place; False on platforms other than Windows, where the
        proactor loop is not used and nothing is changed.
    """
    if sys.platform != "win32":
        return False
    if is_installed():
        return True

    from asyncio import exceptions, proactor_events, trsock

    def _start_serving(
        self,
        protocol_factory,
        sock,
        sslcontext=None,
        server=None,
        backlog=100,
        ssl_handshake_timeout=None,
        ssl_shutdown_timeout=None,
    ):
        consecutive = 0

        def loop(f=None):
            nonlocal consecutive
            try:
                if f is not None:
                    conn, addr = f.result()
                    protocol = protocol_factory()
                    if sslcontext is not None:
                        self._make_ssl_transport(
                            conn,
                            protocol,
                            sslcontext,
                            server_side=True,
                            extra={"peername": addr},
                            server=server,
                            ssl_handshake_timeout=ssl_handshake_timeout,
                            ssl_shutdown_timeout=ssl_shutdown_timeout,
                        )
                    else:
                        self._make_socket_transport(conn, protocol, extra={"peername": addr}, server=server)
                if self.is_closed():
                    return
                consecutive = 0
                f = self._proactor.accept(sock)
            except exceptions.CancelledError:
                sock.close()
            except OSError as exc:
                if sock.fileno() == -1:
                    return
                consecutive += 1
                if consecutive > _MAX_CONSECUTIVE:
                    self.call_exception_handler(
                        {
                            "message": "Accept failed on a socket",
                            "exception": exc,
                            "socket": trsock.TransportSocket(sock),
                        }
                    )
                    sock.close()
                    return
                # THE FIX. Transient, per-connection: keep the listener and try again.
                log.warning("transient accept() error, listener kept: %r (consecutive=%d)", exc, consecutive)
                if not self.is_closed():
                    self.call_later(_REARM_DELAY, loop)
            else:
                self._accept_futures[sock.fileno()] = f
                f.add_done_callback(loop)

        self.call_soon(loop)

    _state["original"] = proactor_events.BaseProactorEventLoop._start_serving  # ty: ignore[unresolved-attribute]
    proactor_events.BaseProactorEventLoop._start_serving = _start_serving  # ty: ignore[unresolved-attribute]
    log.info("asyncio accept-loop hardening installed (transient accept() errors keep the listener)")
    return True


def uninstall() -> None:
    """Restore the stock method. Exists for the tests; the server never calls it."""
    if not is_installed():
        return
    from asyncio import proactor_events

    proactor_events.BaseProactorEventLoop._start_serving = _state["original"]  # ty: ignore[unresolved-attribute]
    _state["original"] = None
