"""C1a — who owns the machine, and how everyone else finds them.

Discord allows exactly one gateway connection per bot token, so exactly one
session per machine may run the broker.  That decision is made here, and it is
made with a file lock rather than a port bind because it has to survive the
question *"is the thing on that port still ours?"*.

Three things live in this module, and they are together because they are one
decision:

**The lock** decides who is broker.  It is a HELD lock, not a waited-on one:
:func:`SingleOwnerLock.acquire` returns immediately with an answer.  The
existing ``dbos_durable_exec/startup_lock.py:51-68`` was the template for the
platform handling (``fcntl`` vs ``msvcrt``) but not for the shape -- that one
is a waiting context manager with a timeout, and a broker that only holds its
claim for the duration of a ``with`` block would be no claim at all.

**The portfile** tells everyone else where the broker listens and, crucially,
carries the TOKEN that authenticates both directions (INV-C2 outbound, INV-C18
inbound).  It is written atomically and owner-only: whoever can read it can
answer approval gates.

**Process identity** is PID *plus* start time (INV-C13).  A bare PID would be
a bug with a delay fuse: operating systems reuse PIDs, and a recycled one
would make a dead session look alive forever, so its thread would never be
archived (AC-51).

**The token is ADOPTED, never re-minted while one exists** (§3.1, AC-85a).
This is the subtle one.  A newly elected broker that mints a fresh token
introduces itself to established sessions with credentials they have never
seen; they discard everything it sends (INV-C18) and it discards everything
they send (INV-C2).  Both sides then behave exactly as INV-C1 demands -- they
carry on quietly -- so the failure is silent.  Adoption is what makes a
re-election invisible; rotation happens only when there is genuinely nothing
to adopt.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

try:  # POSIX
    import fcntl

    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - Windows
    _HAVE_FCNTL = False

try:  # Windows
    import msvcrt

    _HAVE_MSVCRT = True
except ImportError:  # pragma: no cover - POSIX
    _HAVE_MSVCRT = False

#: Redirects the bridge's state directory.  Tests use it; so does anyone who
#: keeps ``$HOME`` on a network share where file locks do not behave.
BRIDGE_DIR_ENV_VAR = "CP_DISCORD_DIR"

PORTFILE_NAME = "broker.json"
REGISTRY_NAME = "sessions.json"
LOCKFILE_NAME = "broker.lock"

#: Owner-only, both of them: the portfile holds the token, and the registry
#: holds every session's inbound port.
DIR_MODE = 0o700
FILE_MODE = 0o600

#: Loopback, always.  Never ``0.0.0.0`` (INV-C2): this socket brokers shell
#: approvals, so reachability from the network is not a feature to weigh.
LOOPBACK = "127.0.0.1"

#: 32 bytes of entropy.  The token is a bearer credential for approving shell
#: commands, so it is sized for that and not for being typed by a human.
_TOKEN_BYTES = 32


def bridge_dir() -> Path:
    """Where the portfile and the session registry live."""
    override = os.environ.get(BRIDGE_DIR_ENV_VAR)
    if override:
        return Path(override)
    return Path.home() / ".code_puppy" / "cp_discord"


def portfile_path() -> Path:
    return bridge_dir() / PORTFILE_NAME


def registry_path() -> Path:
    return bridge_dir() / REGISTRY_NAME


def lockfile_path() -> Path:
    return bridge_dir() / LOCKFILE_NAME


def ensure_bridge_dir() -> Path:
    directory = bridge_dir()
    directory.mkdir(parents=True, exist_ok=True)
    _chmod(directory, DIR_MODE)
    return directory


def _chmod(path: Path, mode: int) -> None:
    """Restrict *path*.  Best effort: POSIX enforces, Windows ignores (AC-6)."""
    try:
        os.chmod(path, mode)
    except OSError:
        logger.debug("cp_discord: could not chmod %s", path, exc_info=True)


def write_json_atomic(path: Path, payload: object) -> None:
    """Write *payload* so a reader sees either the old file or the new one.

    A torn portfile would be read as "no broker" and trigger a spurious
    election; a torn registry would look like a set of orphaned threads and
    get them archived (INV-C14).  ``os.replace`` is atomic on both platforms,
    and the temporary file is created in the SAME directory because a rename
    across filesystems is not.
    """
    ensure_bridge_dir()
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        _chmod(Path(temporary), FILE_MODE)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def read_json(path: Path) -> Optional[object]:
    """Parse *path*, treating anything unreadable as absent.

    Absent is the fail-safe answer everywhere this is used: a portfile we
    cannot parse means "no broker" (elect one), and a registry we cannot parse
    means "no known sessions" (adopt nothing).
    """
    try:
        with path.open("r", encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        logger.debug("cp_discord: %s is unreadable", path, exc_info=True)
        return None


# --------------------------------------------------------------------------- #
# The portfile
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BrokerAddress:
    """Where the broker listens and how to prove you may talk to it."""

    port: int
    token: str
    host: str = LOOPBACK


def write_portfile(address: BrokerAddress) -> None:
    """Publish *address* (§3.1: atomic, 0600)."""
    write_json_atomic(
        portfile_path(),
        {"host": address.host, "port": address.port, "token": address.token},
    )


def read_portfile() -> Optional[BrokerAddress]:
    """The published address, or ``None`` if there is no usable one.

    A portfile without a port or without a token is *not* half-usable: both
    halves are needed to reach the broker at all, so a partial file is
    reported as absent rather than as something to work around.
    """
    payload = read_json(portfile_path())
    if not isinstance(payload, dict):
        return None
    port = payload.get("port")
    token = payload.get("token")
    if not isinstance(port, int) or port <= 0:
        return None
    if not isinstance(token, str) or not token:
        return None
    host = payload.get("host")
    return BrokerAddress(
        port=port,
        token=token,
        host=host if isinstance(host, str) and host else LOOPBACK,
    )


def mint_token() -> str:
    """A fresh bearer token.  Only ever called when there is none to adopt."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def adopt_or_mint_token() -> str:
    """The token the next broker must use (§3.1, AC-85a).

    Called while the lock is already held, so nothing can write the portfile
    underneath us.  The portfile is the ONLY source of this decision -- not
    the registry, not "are there live sessions" -- because a token there would
    be a weaker security statement than one in a 0600 file that nothing else
    reads.
    """
    existing = read_portfile()
    if existing is not None:
        logger.debug("cp_discord: adopting the existing broker token")
        return existing.token
    logger.debug("cp_discord: no token to adopt, rotating")
    return mint_token()


def refresh_token_from_portfile() -> Optional[str]:
    """Re-read the token (C2's seam, AC-85a(c)/AC-85c).

    §3.1a only makes a session re-read the portfile when the broker is
    UNREACHABLE -- but after a rotation the new broker is perfectly reachable,
    so without this the session would never notice and would go silently mute
    and deaf.  C2 calls it on the 30-second tick and immediately after any
    token rejection, in either direction.
    """
    address = read_portfile()
    return address.token if address is not None else None


# --------------------------------------------------------------------------- #
# The lock
# --------------------------------------------------------------------------- #


class SingleOwnerLock:
    """A HELD, non-blocking advisory file lock: one broker per machine.

    Deliberately not ``interprocess_lock`` from ``dbos_durable_exec``: that is
    a *waiting* context manager with a timeout (ANALYSIS A5).  A candidate
    here needs an immediate yes/no, and the winner keeps the lock for its
    whole lifetime.

    :meth:`release` is as load-bearing as :meth:`acquire`.  When the broker
    THREAD dies while its process lives (INV-C22), the OS keeps the lock --
    every other session then fails the non-blocking acquire forever.  The
    holder detecting its own dead thread and releasing is the only way out
    (AC-70).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fd: Optional[int] = None

    @property
    def held(self) -> bool:
        with self._lock:
            return self._fd is not None

    def acquire(self) -> bool:
        """Try to become the broker.  Returns at once; idempotent."""
        with self._lock:
            if self._fd is not None:
                return True
            ensure_bridge_dir()
            path = lockfile_path()
            try:
                fd = os.open(str(path), os.O_RDWR | os.O_CREAT, FILE_MODE)
            except OSError:
                logger.debug("cp_discord: cannot open the lock file", exc_info=True)
                return False
            _chmod(path, FILE_MODE)
            if not _try_lock(fd):
                _close(fd)
                return False
            self._fd = fd
            return True

    def release(self) -> None:
        """Give the claim up.  Safe to call when it was never held."""
        with self._lock:
            fd, self._fd = self._fd, None
        if fd is None:
            return
        _unlock(fd)
        _close(fd)


def _try_lock(fd: int) -> bool:
    if _HAVE_FCNTL:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    if _HAVE_MSVCRT:  # pragma: no branch - one of the two always exists
        # Windows needs a real byte to lock a region of.
        try:
            os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    # No locking primitive at all: refuse rather than let two brokers run.
    # Discord would drop one of the two gateway connections anyway, and the
    # two would fight over every thread.
    return False  # pragma: no cover - unreachable on POSIX and Windows


def _unlock(fd: int) -> None:
    try:
        if _HAVE_FCNTL:
            fcntl.flock(fd, fcntl.LOCK_UN)
        elif _HAVE_MSVCRT:  # pragma: no branch
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        logger.debug("cp_discord: releasing the lock failed", exc_info=True)


def _close(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Process identity (INV-C13)
# --------------------------------------------------------------------------- #


def process_alive(pid: int) -> bool:
    """Whether *pid* currently exists.  Says nothing about WHOSE it is."""
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_process_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Someone else's process: alive, just not ours to signal.
        return True
    except OSError:
        return False
    return True


def process_start_time(pid: int) -> Optional[float]:
    """When *pid* started, or ``None`` if this platform will not say.

    The unit does not matter -- nothing compares it against a wall clock, only
    against a previously recorded value for the same PID -- but it must be
    STABLE for the life of the process and different for a recycled PID.
    """
    if os.name == "nt":
        return _windows_start_time(pid)
    return _linux_start_time(pid)


def process_identity() -> Tuple[int, Optional[float]]:
    """This process's identity: ``(pid, start_time)`` (INV-C13)."""
    pid = os.getpid()
    return pid, process_start_time(pid)


def process_matches(pid: int, started_at: Optional[float]) -> bool:
    """Whether *pid* is still the process that reported *started_at*.

    ``None`` for *started_at*, or a platform that cannot answer, degrades to a
    plain liveness check.  That direction of failure is chosen deliberately:
    refusing would archive the thread of a LIVE session, which INV-C14 and
    AC-15 forbid, while over-trusting merely delays cleanup of a dead one.
    """
    if not process_alive(pid):
        return False
    if started_at is None:
        return True
    current = process_start_time(pid)
    if current is None:
        return True
    # Filesystem/clock granularity differs per platform; a second of slack is
    # far below the interval at which a PID could plausibly be recycled.
    return abs(current - started_at) < 1.0


def _linux_start_time(pid: int) -> Optional[float]:
    """Field 22 of ``/proc/<pid>/stat``: start time in clock ticks since boot.

    Parsed from the LAST ``)`` because the second field is the executable
    name, which may itself contain spaces and parentheses.
    """
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return _bsd_start_time(pid)
    _, _, tail = content.rpartition(")")
    fields = tail.split()
    # After the comm field, field 22 overall is index 19 of the remainder.
    if len(fields) < 20:
        return None
    try:
        return float(fields[19])
    except ValueError:
        return None


def _bsd_start_time(pid: int) -> Optional[float]:
    """macOS/BSD have no ``/proc``; ``ps -o lstart=`` is the portable answer."""
    import subprocess

    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    stamp = completed.stdout.decode("utf-8", "replace").strip()
    if not stamp:
        return None
    from datetime import datetime

    try:
        return datetime.strptime(stamp, "%a %b %d %H:%M:%S %Y").timestamp()
    except ValueError:
        return None


def _windows_process_alive(pid: int) -> bool:
    handle = _windows_open_process(pid)
    if handle is None:
        return False
    try:
        import ctypes

        code = ctypes.c_ulong()
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        # 259 == STILL_ACTIVE.  A process that exited WITH 259 would be
        # misread as alive; that only delays archiving, never causes the
        # archiving of a live session's thread.
        return code.value == 259
    finally:
        _windows_close_handle(handle)


def _windows_start_time(pid: int) -> Optional[float]:
    handle = _windows_open_process(pid)
    if handle is None:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        ok = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not ok:
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return ticks / 1e7  # 100-nanosecond units -> seconds
    except (OSError, AttributeError):
        return None
    finally:
        _windows_close_handle(handle)


#: ``PROCESS_QUERY_LIMITED_INFORMATION`` -- enough for exit code and times,
#: and grantable for processes we do not own.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _windows_open_process(pid: int):
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
        return handle or None
    except (OSError, AttributeError, ValueError):
        return None


def _windows_close_handle(handle) -> None:
    try:
        import ctypes

        ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
    except (OSError, AttributeError):
        pass


__all__: Sequence[str] = (
    "BRIDGE_DIR_ENV_VAR",
    "DIR_MODE",
    "FILE_MODE",
    "LOCKFILE_NAME",
    "LOOPBACK",
    "PORTFILE_NAME",
    "REGISTRY_NAME",
    "BrokerAddress",
    "SingleOwnerLock",
    "adopt_or_mint_token",
    "bridge_dir",
    "ensure_bridge_dir",
    "lockfile_path",
    "mint_token",
    "portfile_path",
    "process_alive",
    "process_identity",
    "process_matches",
    "process_start_time",
    "read_json",
    "read_portfile",
    "refresh_token_from_portfile",
    "registry_path",
    "write_json_atomic",
    "write_portfile",
)
