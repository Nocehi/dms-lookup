#!/usr/bin/env python3
"""Private, request-scoped runtime storage for Dank Lookup.

The runtime directory is session-local whenever XDG_RUNTIME_DIR is available.
The /tmp fallback is namespaced by uid, verified to be owned by the caller, and
never made group/world accessible.
"""

from __future__ import annotations

import atexit
import errno
import fcntl
import json
import os
import pathlib
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from typing import Any, Callable, Iterable, Mapping


RUNTIME_NAME = "dank-lookup"
MAX_AGE_SECONDS = 24 * 60 * 60
BACKEND_SOCKET_NAME = "backend.sock"
BACKEND_START_LOCK_NAME = "backend-start.lock"
MAX_REQUEST_FRAME_BYTES = 1024 * 1024
MAX_RESPONSE_FRAME_BYTES = 16 * 1024 * 1024
MAX_TRANSPORT_QUERY_BYTES = 64 * 1024
_FRAME_HEADER = struct.Struct(">I")
_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")
_CLEANUP_BASENAME = re.compile(r"(?:open|blob)-[A-Za-z0-9_.-]+\.txt\Z")
_TRANSPORT_ACTIONS = frozenset(
    {"initial_selection", "source_change", "mode_change", "relookup", "lookup", "cancel"}
)


class RuntimeSecurityError(RuntimeError):
    """Raised when an existing runtime path is not safe to use."""


class TransportProtocolError(RuntimeError):
    """A bounded private-transport frame or envelope is invalid."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _private_umask() -> None:
    os.umask(0o077)


def _validate_owned_directory(path: pathlib.Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeSecurityError(f"runtime path is not a real directory: {path}")
    if info.st_uid != os.getuid():
        raise RuntimeSecurityError(f"runtime path is not owned by current user: {path}")
    if stat.S_IMODE(info.st_mode) != 0o700:
        path.chmod(0o700)


def runtime_dir(create: bool = True) -> pathlib.Path:
    """Return the private runtime directory, creating it with mode 0700."""

    _private_umask()
    xdg = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if xdg:
        base = pathlib.Path(xdg)
        try:
            base_info = base.stat()
            base_ok = base.is_dir() and base_info.st_uid == os.getuid()
        except OSError:
            base_ok = False
        if base_ok:
            target = base / RUNTIME_NAME
        else:
            target = pathlib.Path("/tmp") / f"{RUNTIME_NAME}-{os.getuid()}"
    else:
        target = pathlib.Path("/tmp") / f"{RUNTIME_NAME}-{os.getuid()}"

    if create:
        try:
            target.mkdir(mode=0o700, parents=False, exist_ok=True)
        except FileNotFoundError:
            # A valid XDG_RUNTIME_DIR was checked above; fallback bases already exist.
            raise RuntimeSecurityError(f"runtime parent disappeared: {target.parent}")
        _validate_owned_directory(target)
    return target


def safe_identifier(value: str, fallback: str = "request") -> str:
    cleaned = _SAFE_ID.sub("-", str(value)).strip(".-")[:96]
    return cleaned or fallback


def atomic_write_bytes(path: pathlib.Path, payload: bytes, mode: int = 0o600) -> None:
    """Atomically replace a regular file inside the private runtime directory."""

    parent = runtime_dir(create=True)
    try:
        resolved_parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise RuntimeSecurityError(f"invalid runtime destination: {path}") from exc
    if resolved_parent != parent.resolve(strict=True):
        raise RuntimeSecurityError(f"destination escapes runtime directory: {path}")
    if path.is_symlink():
        raise RuntimeSecurityError(f"refusing symlink destination: {path}")

    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    tmp_path = pathlib.Path(temporary)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        path.chmod(mode)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_json(path: pathlib.Path, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    atomic_write_bytes(path, payload)


def request_path(request_id: str) -> pathlib.Path:
    return runtime_dir() / f"request-{safe_identifier(request_id)}.json"


def text_path(request_id: str) -> pathlib.Path:
    return runtime_dir() / f"open-{safe_identifier(request_id)}.txt"


def blob_path(request_id: str, reference: str) -> pathlib.Path:
    return runtime_dir() / (
        f"blob-{safe_identifier(request_id)}-{safe_identifier(reference, fallback='full')}.txt"
    )


def backend_socket_path(create: bool = True) -> pathlib.Path:
    return runtime_dir(create=create) / BACKEND_SOCKET_NAME


def encode_transport_frame(value: Mapping[str, Any], max_bytes: int) -> bytes:
    try:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise TransportProtocolError("invalid_frame_json") from exc
    if len(payload) < 2 or len(payload) > max_bytes:
        raise TransportProtocolError("frame_too_large")
    return _FRAME_HEADER.pack(len(payload)) + payload


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        part = connection.recv(size - len(chunks))
        if not part:
            raise TransportProtocolError("unexpected_eof")
        chunks.extend(part)
    return bytes(chunks)


def receive_transport_frame(connection: socket.socket, max_bytes: int) -> dict[str, Any]:
    length = _FRAME_HEADER.unpack(_recv_exact(connection, _FRAME_HEADER.size))[0]
    if length < 2 or length > max_bytes:
        raise TransportProtocolError("invalid_frame_length")
    try:
        value = json.loads(_recv_exact(connection, length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TransportProtocolError("invalid_frame_json") from exc
    if not isinstance(value, dict):
        raise TransportProtocolError("invalid_frame_shape")
    return value


def send_transport_frame(
    connection: socket.socket,
    value: Mapping[str, Any],
    max_bytes: int,
) -> None:
    connection.sendall(encode_transport_frame(value, max_bytes))


def transport_peer_uid(connection: socket.socket) -> int:
    try:
        credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        _pid, uid, _gid = struct.unpack("3i", credentials)
    except (AttributeError, OSError, struct.error) as exc:
        raise TransportProtocolError("peer_credentials_unavailable") from exc
    return uid


def validate_transport_envelope(value: Mapping[str, Any]) -> dict[str, Any]:
    action = str(value.get("action", ""))
    if action not in _TRANSPORT_ACTIONS:
        raise TransportProtocolError("invalid_action")
    request_id = str(value.get("request_id", ""))
    try:
        uuid.UUID(request_id)
    except (ValueError, AttributeError) as exc:
        raise TransportProtocolError("invalid_request_id") from exc
    sequence = value.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise TransportProtocolError("invalid_sequence")
    normalized = dict(value)
    normalized["action"] = action
    normalized["request_id"] = request_id
    normalized["sequence"] = sequence
    if action == "cancel":
        if value.get("query") not in (None, ""):
            raise TransportProtocolError("cancel_contains_query")
        normalized.pop("query", None)
        return normalized
    query = value.get("query")
    if not isinstance(query, str):
        raise TransportProtocolError("invalid_query")
    try:
        query_size = len(query.encode("utf-8"))
    except UnicodeError as exc:
        raise TransportProtocolError("invalid_query") from exc
    if query_size > MAX_TRANSPORT_QUERY_BYTES:
        raise TransportProtocolError("query_too_large")
    for key in ("source", "mode", "config"):
        if key in value and value[key] is not None and not isinstance(value[key], str):
            raise TransportProtocolError(f"invalid_{key}")
    return normalized


TransportHandler = Callable[[dict[str, Any], threading.Event], Mapping[str, Any]]


class _ActiveTransportRequest:
    def __init__(self) -> None:
        self.cancel_event = threading.Event()
        self.response_committed = False


class PrivateBackendServer:
    """Concurrent fixed-socket server with bounded frames and metadata cancellation."""

    def __init__(
        self,
        handler: TransportHandler,
        *,
        idle_timeout: float = 30.0,
        path: pathlib.Path | None = None,
        expected_uid: int | None = None,
    ) -> None:
        self.handler = handler
        self.idle_timeout = max(0.05, float(idle_timeout))
        self.path = path or backend_socket_path()
        self.expected_uid = os.getuid() if expected_uid is None else int(expected_uid)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._active: dict[tuple[str, int], _ActiveTransportRequest] = {}
        self._workers: set[threading.Thread] = set()
        self._last_activity = time.monotonic()
        self._bound_socket_identity: tuple[int, int] | None = None

    def _prepare_socket_path(self) -> None:
        root = runtime_dir()
        if self.path.parent.resolve(strict=True) != root.resolve(strict=True):
            raise RuntimeSecurityError("backend socket escapes private runtime directory")
        while True:
            try:
                info = self.path.lstat()
            except FileNotFoundError:
                return
            if info.st_uid != os.getuid() or not stat.S_ISSOCK(info.st_mode):
                raise RuntimeSecurityError("refusing unsafe existing backend socket")
            identity = (info.st_dev, info.st_ino)

            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(0.1)
                probe.connect(str(self.path))
            except FileNotFoundError:
                continue
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    continue
                if exc.errno != errno.ECONNREFUSED:
                    raise RuntimeSecurityError(
                        "cannot prove existing backend socket is stale"
                    ) from exc
            else:
                raise RuntimeSecurityError("backend server already running")
            finally:
                probe.close()

            try:
                current = self.path.lstat()
            except FileNotFoundError:
                return
            if (current.st_dev, current.st_ino) != identity:
                continue
            if current.st_uid != os.getuid() or not stat.S_ISSOCK(current.st_mode):
                raise RuntimeSecurityError("backend socket changed during stale check")
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            return

    def _cancel(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        key = (str(envelope["request_id"]), int(envelope["sequence"]))
        with self._lock:
            active = self._active.get(key)
            matched = active is not None and not active.response_committed
            if matched:
                active.cancel_event.set()
        return {
            "request_id": key[0],
            "sequence": key[1],
            "status": "cancel_ack",
            "matched": matched,
        }

    def _commit_response(
        self,
        key: tuple[str, int],
        active: _ActiveTransportRequest,
        response: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Atomically choose cancellation or commit before transport output."""

        with self._lock:
            if self._active.get(key) is not active:
                raise TransportProtocolError("request_not_active")
            if active.cancel_event.is_set():
                response = {
                    "request_id": key[0],
                    "sequence": key[1],
                    "status": "cancelled",
                }
            active.response_committed = True
            return response

    def _serve_connection(self, connection: socket.socket) -> None:
        response: Mapping[str, Any]
        key: tuple[str, int] | None = None
        active: _ActiveTransportRequest | None = None
        try:
            connection.settimeout(10.0)
            if transport_peer_uid(connection) != self.expected_uid:
                raise TransportProtocolError("peer_uid_mismatch")
            envelope = validate_transport_envelope(
                receive_transport_frame(connection, MAX_REQUEST_FRAME_BYTES)
            )
            if envelope["action"] == "cancel":
                response = self._cancel(envelope)
            else:
                key = (envelope["request_id"], envelope["sequence"])
                active = _ActiveTransportRequest()
                with self._lock:
                    if key in self._active:
                        raise TransportProtocolError("duplicate_request")
                    self._active[key] = active
                response = dict(self.handler(envelope, active.cancel_event))
                response.setdefault("request_id", key[0])
                response.setdefault("sequence", key[1])
                response = self._commit_response(key, active, response)
            send_transport_frame(connection, response, MAX_RESPONSE_FRAME_BYTES)
        except TransportProtocolError as exc:
            try:
                send_transport_frame(
                    connection,
                    {"error": {"code": exc.code}},
                    MAX_RESPONSE_FRAME_BYTES,
                )
            except (OSError, TransportProtocolError):
                pass
        except (OSError, ValueError, TypeError):
            pass
        finally:
            if key is not None:
                with self._lock:
                    if self._active.get(key) is active:
                        self._active.pop(key, None)
            connection.close()
            with self._lock:
                self._workers.discard(threading.current_thread())
                self._last_activity = time.monotonic()

    def serve(self) -> int:
        self._prepare_socket_path()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self.path))
            bound_info = self.path.lstat()
            if bound_info.st_uid != os.getuid() or not stat.S_ISSOCK(bound_info.st_mode):
                raise RuntimeSecurityError("bound backend path is not an owned socket")
            self._bound_socket_identity = (bound_info.st_dev, bound_info.st_ino)
            self.path.chmod(0o600)
            current_info = self.path.lstat()
            if (current_info.st_dev, current_info.st_ino) != self._bound_socket_identity:
                raise RuntimeSecurityError("backend socket changed during bind")
            server.listen(32)
            server.settimeout(0.05)
            while not self._stop.is_set():
                try:
                    connection, _address = server.accept()
                except socket.timeout:
                    with self._lock:
                        idle = not self._active and (
                            time.monotonic() - self._last_activity >= self.idle_timeout
                        )
                    if idle:
                        break
                    continue
                with self._lock:
                    self._last_activity = time.monotonic()
                worker = threading.Thread(
                    target=self._serve_connection,
                    args=(connection,),
                    daemon=True,
                )
                with self._lock:
                    self._workers.add(worker)
                worker.start()
        finally:
            server.close()
            with self._lock:
                workers = list(self._workers)
            for worker in workers:
                worker.join(timeout=2.0)
            try:
                info = self.path.lstat()
                if (
                    self._bound_socket_identity is not None
                    and info.st_uid == os.getuid()
                    and stat.S_ISSOCK(info.st_mode)
                    and (info.st_dev, info.st_ino) == self._bound_socket_identity
                ):
                    self.path.unlink()
            except OSError:
                pass
            finally:
                self._bound_socket_identity = None
        return 0


def request_backend(
    envelope: Mapping[str, Any],
    *,
    timeout: float = 10.0,
    path: pathlib.Path | None = None,
) -> dict[str, Any]:
    normalized = validate_transport_envelope(envelope)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(max(0.05, float(timeout)))
        client.connect(str(path or backend_socket_path()))
        send_transport_frame(client, normalized, MAX_REQUEST_FRAME_BYTES)
        return receive_transport_frame(client, MAX_RESPONSE_FRAME_BYTES)
    finally:
        client.close()


def _cleanup_ttl_seconds() -> float:
    try:
        return min(24 * 60 * 60, max(0.01, float(os.environ.get("DANK_LOOKUP_FULL_TTL_SECONDS", "300"))))
    except ValueError:
        return 300.0


def remove_private_file(
    path: pathlib.Path,
    min_age_seconds: float = 0.0,
    *,
    expected_device: int | None = None,
    expected_inode: int | None = None,
    expected_mtime_ns: int | None = None,
) -> bool:
    """Safely unlink one owned regular runtime file once it reaches min_age."""

    root = runtime_dir(create=False)
    if not root.exists():
        return False
    _validate_owned_directory(root)
    if path.parent.resolve(strict=True) != root.resolve(strict=True):
        raise RuntimeSecurityError("cleanup target escapes runtime directory")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise RuntimeSecurityError("cleanup target is not an owned regular file")
    if expected_device is not None and info.st_dev != expected_device:
        return False
    if expected_inode is not None and info.st_ino != expected_inode:
        return False
    if expected_mtime_ns is not None and info.st_mtime_ns != expected_mtime_ns:
        return False
    if time.time() - info.st_mtime < min_age_seconds:
        return False
    path.unlink()
    if root.parent == pathlib.Path("/tmp"):
        try:
            root.rmdir()
        except OSError:
            pass
    return True


def _cleanup_cli_path(explicit: str | os.PathLike[str] | None = None) -> pathlib.Path:
    candidates: list[pathlib.Path] = []
    if explicit is not None:
        candidates.append(pathlib.Path(os.path.expanduser(os.fspath(explicit))))
    invoked = pathlib.Path(sys.argv[0])
    if invoked.name == "dank-lookup":
        candidates.append(invoked)
    installed = shutil.which("dank-lookup")
    if installed:
        candidates.append(pathlib.Path(installed))
    candidates.append(pathlib.Path(__file__).resolve().parent.parent / "bin" / "dank-lookup")
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved
    raise RuntimeSecurityError("dank-lookup cleanup helper is unavailable")


def cleanup_private_basename_after_ttl(
    basename: str,
    ttl_seconds: float,
    *,
    expected_device: int,
    expected_inode: int,
    expected_mtime_ns: int,
) -> bool:
    """Sleep for a bounded TTL and remove only the originally scheduled file."""

    name = str(basename)
    if pathlib.Path(name).name != name or not _CLEANUP_BASENAME.fullmatch(name):
        raise RuntimeSecurityError("invalid cleanup basename")
    ttl = min(24 * 60 * 60, max(0.01, float(ttl_seconds)))
    time.sleep(ttl)
    return remove_private_file(
        runtime_dir(create=False) / name,
        min_age_seconds=ttl,
        expected_device=int(expected_device),
        expected_inode=int(expected_inode),
        expected_mtime_ns=int(expected_mtime_ns),
    )


def schedule_private_cleanup(
    path: pathlib.Path,
    ttl_seconds: float | None = None,
    *,
    cli_path: str | os.PathLike[str] | None = None,
) -> None:
    """Start the same CLI as a detached, metadata-only TTL helper."""

    ttl = _cleanup_ttl_seconds() if ttl_seconds is None else max(0.01, float(ttl_seconds))
    root = runtime_dir()
    if path.parent.resolve(strict=True) != root.resolve(strict=True):
        raise RuntimeSecurityError("cleanup target escapes runtime directory")
    if not _CLEANUP_BASENAME.fullmatch(path.name):
        raise RuntimeSecurityError("invalid cleanup basename")
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise RuntimeSecurityError("cleanup target is not an owned mode-0600 regular file")
    command = [
        os.fspath(_cleanup_cli_path(cli_path)),
        "cleanup-file",
        "--basename",
        path.name,
        "--ttl",
        repr(min(24 * 60 * 60, ttl)),
        "--device",
        str(info.st_dev),
        "--inode",
        str(info.st_ino),
        "--mtime-ns",
        str(info.st_mtime_ns),
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
    # Keep a live reference and reap in long-running callers without making an
    # Open action wait for the TTL. Short-lived callers may exit first, in
    # which case the detached child is reparented normally by the OS.
    threading.Thread(target=process.wait, daemon=True).start()


def write_full_blobs(request_id: str, blobs: Mapping[str, str]) -> list[str]:
    """Write only oversized complete bodies, never a query/result envelope."""

    cleanup_runtime(MAX_AGE_SECONDS)
    written: list[str] = []
    for reference, body in blobs.items():
        destination = blob_path(request_id, reference)
        atomic_write_bytes(destination, str(body).encode("utf-8"))
        schedule_private_cleanup(destination)
        written.append(str(reference))
    return written


def resolve_full_blob(request_id: str, reference: str, refresh_ttl: bool = False) -> pathlib.Path:
    path = blob_path(request_id, reference)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeSecurityError("full reference is not an owned regular file")
    if refresh_ttl:
        os.utime(path, None, follow_symlinks=False)
        schedule_private_cleanup(path)
    return path


def write_result(result: dict[str, Any], vault: dict[str, Any]) -> bool:
    """Write request data and advance active.json only for a non-stale sequence.

    Returns True when this request became active. Every request keeps its own
    artifact, so a stale completion cannot destroy the newer request's data.
    """

    root = runtime_dir()
    request_id = safe_identifier(str(result.get("request_id", "request")))
    sequence = int(result.get("sequence", 0) or 0)
    envelope = {"result": result, "vault": vault}
    atomic_write_json(request_path(request_id), envelope)

    lock_path = root / "active.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    accepted = False
    try:
        os.fchmod(lock_fd, 0o600)
        with os.fdopen(lock_fd, "r+", encoding="utf-8", closefd=True) as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            active_path = root / "active.json"
            current_sequence = -1
            try:
                current = json.loads(active_path.read_text(encoding="utf-8"))
                current_sequence = int(current.get("sequence", -1))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
            if sequence >= current_sequence:
                atomic_write_json(
                    active_path,
                    {"request_id": request_id, "sequence": sequence, "path": request_path(request_id).name},
                )
                accepted = True
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except BaseException:
        try:
            os.close(lock_fd)
        except OSError:
            pass
        raise
    return accepted


def read_request(request_id: str) -> dict[str, Any]:
    path = request_path(request_id)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeSecurityError("request artifact is not a private regular file")
    return json.loads(path.read_text(encoding="utf-8"))


def find_full_entry(request_id: str, entry_id: str) -> str:
    envelope = read_request(request_id)
    entry = envelope.get("vault", {}).get("entries", {}).get(str(entry_id))
    if not isinstance(entry, dict):
        raise KeyError(entry_id)
    value = entry.get("full_body")
    if not isinstance(value, str):
        raise KeyError(entry_id)
    return value


def cleanup_runtime(max_age_seconds: int = MAX_AGE_SECONDS, keep: Iterable[pathlib.Path] = ()) -> int:
    """Remove old request/full files owned by this process' user."""

    root = runtime_dir(create=False)
    if not root.exists():
        return 0
    _validate_owned_directory(root)
    protected = {p.resolve() for p in keep}
    cutoff = time.time() - max(0, max_age_seconds)
    removed = 0
    for path in root.iterdir():
        if path.name in {
            "active.json",
            "active.lock",
            "server.sock",
            BACKEND_SOCKET_NAME,
            BACKEND_START_LOCK_NAME,
        }:
            continue
        try:
            info = path.lstat()
            if path.resolve() in protected or info.st_uid != os.getuid() or info.st_mtime >= cutoff:
                continue
            if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def remove_fallback_at_exit() -> None:
    """Best-effort cleanup for fallback directories created by this process."""

    target = runtime_dir(create=False)
    if target.parent != pathlib.Path("/tmp"):
        return
    try:
        _validate_owned_directory(target)
        if not any(target.iterdir()):
            target.rmdir()
    except OSError:
        pass


atexit.register(remove_fallback_at_exit)
