"""
internet.py — порт src/lua/core/internet.lua + src/c/internet.c

TCP/SSL + HTTP. Возможности:
  * Content-Length / chunked / до EOF
  * автоматическое следование за редиректами 301/302/303/307/308 (до 5)
  * handle.response() → code, message, headers, length (OC-порядок)
  * headers — Lua-таблицы (не Python dict/list), доступны как headers["X"][1]
  * блокирующие операции пумпят pygame (окно не зависает)
"""
from __future__ import annotations
import re
import time
import socket
import ssl
import logging
import urllib.parse

from core import component as component_module

try:
    from core import computer as _computer
except Exception:
    _computer = None

logger = logging.getLogger("lupi.internet")

DEFAULT_TIMEOUT = 10.0
POLL_INTERVAL = 0.02
MAX_REDIRECTS = 5


# ── Helpers ───────────────────────────────────────────────────────────────────

def _from_lua(s):
    if not isinstance(s, str):
        return s
    try:
        return s.encode('latin-1').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def _is_lua_table(x) -> bool:
    if isinstance(x, (str, bytes, int, float, bool, list, dict, tuple, type(None))):
        return False
    return hasattr(x, "items") or hasattr(x, "keys")


def _clean_args(args):
    args = list(args)
    if args and _is_lua_table(args[0]):
        args = args[1:]
    return args


def _pump():
    """Даём pygame обработать события во время ожидания сети."""
    if _computer is None:
        return
    hook = getattr(_computer, "_pump_hook", None)
    if hook is None:
        return
    try:
        hook()
    except Exception:
        pass


class TCPSocket:
    def __init__(self, host: str, port: int, use_ssl: bool = False):
        self._closed = False
        raw = socket.create_connection((host, port), timeout=DEFAULT_TIMEOUT)

        if use_ssl:
            ctx = ssl.create_default_context()
            raw.settimeout(POLL_INTERVAL)
            self._sock = ctx.wrap_socket(
                raw, server_hostname=host, do_handshake_on_connect=False)
            self._do_handshake()
        else:
            self._sock = raw
            self._sock.settimeout(POLL_INTERVAL)

    def _do_handshake(self):
        deadline = time.monotonic() + DEFAULT_TIMEOUT
        while True:
            if time.monotonic() > deadline:
                raise socket.timeout("ssl handshake timeout")
            try:
                self._sock.do_handshake()
                return
            except socket.timeout:
                _pump()
                continue
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                _pump()
                continue
            except ssl.SSLError as e:
                raise OSError(str(e))

    def read_bytes(self, n: int = 65535, timeout: float = DEFAULT_TIMEOUT):
        if self._closed:
            return None
        deadline = time.monotonic() + timeout
        while True:
            if time.monotonic() > deadline:
                return b""
            try:
                data = self._sock.recv(n)
                if not data:
                    return None
                return data
            except socket.timeout:
                _pump()
                continue
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError):
                _pump()
                continue
            except OSError:
                return None

    def write_bytes(self, data: bytes, timeout: float = DEFAULT_TIMEOUT):
        if self._closed:
            return None
        deadline = time.monotonic() + timeout
        total = 0
        while total < len(data):
            if time.monotonic() > deadline:
                return None
            try:
                sent = self._sock.send(data[total:])
                if sent <= 0:
                    return None
                total += sent
            except socket.timeout:
                _pump()
                continue
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError):
                _pump()
                continue
            except OSError:
                return None
        return total

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                self._sock.close()
            except Exception:
                pass


class InternetComponent:

    def is_http_enabled(self, *_): return True
    def is_tcp_enabled(self, *_):  return True

    # ── internet.connect(address[, port]) ─────────────────────────────────────
    def connect(self, *raw_args):
        args = _clean_args(raw_args)

        address = args[0] if len(args) >= 1 else None
        port    = args[1] if len(args) >= 2 and isinstance(args[1], int) \
                            and not isinstance(args[1], bool) else None

        if not isinstance(address, str):
            return None, "invalid address"
        address = _from_lua(address)

        if port is None:
            if ":" in address:
                host, _, p = address.rpartition(":")
                try:
                    port = int(p)
                    address = host
                except ValueError:
                    port = 80
            else:
                port = 80

        try:
            sock = TCPSocket(address, port)
        except OSError as e:
            return None, str(e)

        state = {"closed": False}

        def finish_connect(*_):
            if state["closed"]:
                raise RuntimeError("connection closed")
            return True

        def read(*raw_a):
            if state["closed"]:
                return None, "connection lost"
            a = _clean_args(raw_a)
            n = 65535
            if a and isinstance(a[0], int) and not isinstance(a[0], bool):
                n = a[0]
            data = sock.read_bytes(n)
            if data is None:
                state["closed"] = True
                sock.close()
                return None, "connection lost"
            if data == b"":
                return ""
            return data.decode("latin-1")

        def write(*raw_a):
            if state["closed"]:
                return None, "connection lost"
            a = _clean_args(raw_a)
            if not a:
                return None, "no data"
            value = a[0]
            if isinstance(value, bytes):
                raw = value
            elif isinstance(value, str):
                raw = value.encode("latin-1", errors="replace")
            else:
                raw = str(value).encode("utf-8")
            written = sock.write_bytes(raw)
            if written is None:
                state["closed"] = True
                sock.close()
                return None, "connection lost"
            return written

        def close_conn(*_):
            state["closed"] = True
            sock.close()

        return {
            "finishConnect": finish_connect,
            "read": read,
            "write": write,
            "close": close_conn,
        }

    # ── Одно HTTP-соединение ──────────────────────────────────────────────────
    def _do_request_once(self, hostname, port, use_ssl, method, path, host,
                         body, hdrs):
        """Всегда возвращает (handle, None) или (None, reason)."""
        try:
            sock = TCPSocket(hostname, port, use_ssl=use_ssl)
        except OSError as e:
            return None, str(e)

        lines = [
            f"{method} {path} HTTP/1.1",
            f"Host: {host}",
            "Connection: close",
            "User-Agent: LuPI/1.0",
            "Accept: */*",
        ]
        existing = {k.lower() for k in hdrs}
        if body and "content-type" not in existing:
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        if body:
            hdrs["Content-Length"] = str(len(body))
        for k, v in hdrs.items():
            lines.append(f"{k}: {v}")
        header_str = "\r\n".join(lines) + "\r\n\r\n"

        if sock.write_bytes(header_str.encode("latin-1", errors="replace")) is None:
            sock.close()
            return None, "send failed"
        if body and sock.write_bytes(body) is None:
            sock.close()
            return None, "send failed"

        state = {
            "header": None,
            "header_original": {},
            "buffer": b"",
            "payload": b"",
            "has_content_length": False,
            "content_length": 0,
            "delivered": 0,
            "chunked": False,
            "chunk_state": "size",
            "chunk_remaining": 0,
            "chunk_done": False,
            "eof": False,
            "closed": False,
        }

        def _need_more():
            if state["eof"]:
                return -1
            chunk = sock.read_bytes(8192)
            if chunk is None:
                state["eof"] = True
                return -1
            if chunk == b"":
                return 0
            state["buffer"] += chunk
            return 1

        def finish_connect(*_):
            if state["header"] is not None:
                return True

            deadline = time.monotonic() + DEFAULT_TIMEOUT
            empty_retries = 0
            while b"\r\n\r\n" not in state["buffer"]:
                if time.monotonic() > deadline:
                    return None, "timeout"
                r = _need_more()
                if r == -1:
                    return None, "connection lost"
                if r == 0:
                    empty_retries += 1
                    if empty_retries > 500:
                        return None, "timeout"
                    continue
                empty_retries = 0

            header_bytes, _, remainder = state["buffer"].partition(b"\r\n\r\n")
            state["buffer"] = remainder

            header = {}
            header_original: dict[str, list[str]] = {}
            hlines = header_bytes.split(b"\r\n")
            if hlines:
                m = re.match(rb"HTTP/\S+\s+(\d+)\s*(.*)", hlines[0])
                if m:
                    header["status"]  = m.group(1).decode("latin-1")
                    header["message"] = m.group(2).decode("latin-1")
                for line in hlines[1:]:
                    if b":" not in line:
                        continue
                    k_raw, _, v_raw = line.partition(b":")
                    k_orig = k_raw.strip().decode("latin-1")
                    k_lc   = k_orig.lower()
                    v_str  = v_raw.strip().decode("latin-1")
                    header[k_lc] = v_str

                    for key_variant in {k_orig, k_lc,
                                        "-".join(p.capitalize()
                                                 for p in k_orig.split("-"))}:
                        arr = header_original.setdefault(key_variant, [])
                        if v_str not in arr:
                            arr.append(v_str)

            status = header.get("status", "")

            te = header.get("transfer-encoding", "").lower()
            if "chunked" in te:
                state["chunked"] = True
                state["has_content_length"] = False
                state["content_length"] = 0
                state["chunk_state"] = "size"
                state["chunk_remaining"] = 0
                state["chunk_done"] = False
            elif "content-length" in header:
                try:
                    state["content_length"] = int(header["content-length"])
                except ValueError:
                    state["content_length"] = 0
                state["has_content_length"] = True
            else:
                state["has_content_length"] = False
                state["content_length"] = 0

            if status.startswith("1") or status in ("204", "304"):
                state["has_content_length"] = True
                state["content_length"] = 0
                state["chunk_done"] = True

            state["header"] = header
            state["header_original"] = header_original
            logger.debug("HTTP %s %s, chunked=%s, content-length=%s",
                         status, header.get("message", ""),
                         state["chunked"], state["content_length"])
            return True

        def _decode_chunked_once():
            deadline = time.monotonic() + DEFAULT_TIMEOUT
            empty_retries = 0
            while True:
                if state["chunk_done"]:
                    return False
                if time.monotonic() > deadline:
                    state["chunk_done"] = True
                    return False

                if state["chunk_state"] == "size":
                    idx = state["buffer"].find(b"\r\n")
                    if idx < 0:
                        r = _need_more()
                        if r == -1:
                            state["chunk_done"] = True
                            return False
                        if r == 0:
                            empty_retries += 1
                            if empty_retries > 500:
                                state["chunk_done"] = True
                                return False
                            continue
                        empty_retries = 0
                        continue
                    line = state["buffer"][:idx]
                    state["buffer"] = state["buffer"][idx + 2:]
                    size_str = line.split(b";", 1)[0].strip()
                    try:
                        size = int(size_str, 16)
                    except ValueError:
                        logger.warning("Bad chunk size: %r", size_str)
                        state["chunk_done"] = True
                        return False
                    if size == 0:
                        state["chunk_done"] = True
                        return False
                    state["chunk_remaining"] = size
                    state["chunk_state"] = "data"
                    continue

                if state["chunk_state"] == "data":
                    if state["chunk_remaining"] == 0:
                        state["chunk_state"] = "crlf"
                        continue
                    if not state["buffer"]:
                        r = _need_more()
                        if r == -1:
                            state["chunk_done"] = True
                            return False
                        if r == 0:
                            empty_retries += 1
                            if empty_retries > 500:
                                state["chunk_done"] = True
                                return False
                            continue
                        empty_retries = 0
                        continue
                    take = min(len(state["buffer"]), state["chunk_remaining"])
                    state["payload"] += state["buffer"][:take]
                    state["buffer"] = state["buffer"][take:]
                    state["chunk_remaining"] -= take
                    if state["chunk_remaining"] == 0:
                        state["chunk_state"] = "crlf"
                    return True

                if state["chunk_state"] == "crlf":
                    while len(state["buffer"]) < 2:
                        r = _need_more()
                        if r == -1:
                            state["chunk_done"] = True
                            return False
                        if r == 0:
                            empty_retries += 1
                            if empty_retries > 500:
                                state["chunk_done"] = True
                                return False
                            continue
                        empty_retries = 0
                    state["buffer"] = state["buffer"][2:]
                    state["chunk_state"] = "size"
                    continue

        # ── handle.response() → code, message, headers, length (OC-порядок) ─
        # ВАЖНО: headers должны быть Lua-таблицей (а не Python dict/list),
        # иначе Lupa обернёт их как userdata и headers["X"][1] упадёт.
        def response(*_):
            if state["header"] is None:
                r = finish_connect()
                if r is None:
                    return None, "connection failed"

            header = state["header"] or {}
            try:
                code = int(header.get("status", "0"))
            except ValueError:
                code = 0
            message = header.get("message", "")

            raw_hdrs = {k: list(v) for k, v in state["header_original"].items()}
            hdrs_out = component_module._convert(raw_hdrs)

            length = state["content_length"] if state["has_content_length"] else 0
            return code, message, hdrs_out, length

        def read(*raw_a):
            if state["closed"]:
                return None
            if state["header"] is None:
                r = finish_connect()
                if r is None:
                    return None, "connection failed"

            a = _clean_args(raw_a)
            n = 65535
            if a and isinstance(a[0], int) and not isinstance(a[0], bool):
                n = a[0]
            if n == float('inf') or n > 10**9:
                n = 10**9

            if state["chunked"]:
                while len(state["payload"]) < n and not state["chunk_done"]:
                    if not _decode_chunked_once():
                        break
                if state["payload"]:
                    out = state["payload"][:n]
                    state["payload"] = state["payload"][n:]
                    return out.decode("latin-1")
                if state["chunk_done"] or state["eof"]:
                    return None
                return ""

            if state["has_content_length"]:
                if state["delivered"] >= state["content_length"]:
                    return None
                need = state["content_length"] - state["delivered"]
                if state["buffer"]:
                    take = min(len(state["buffer"]), need)
                    out = state["buffer"][:take]
                    state["buffer"] = state["buffer"][take:]
                    state["delivered"] += take
                    return out.decode("latin-1")
                empty_retries = 0
                while not state["buffer"]:
                    r = _need_more()
                    if r == -1:
                        return None
                    if r == 0:
                        empty_retries += 1
                        if empty_retries > 500:
                            return None
                        continue
                    empty_retries = 0
                take = min(len(state["buffer"]), need)
                out = state["buffer"][:take]
                state["buffer"] = state["buffer"][take:]
                state["delivered"] += take
                return out.decode("latin-1")

            if state["buffer"]:
                out = state["buffer"]
                state["buffer"] = b""
                state["delivered"] += len(out)
                return out.decode("latin-1")
            if state["eof"]:
                return None
            empty_retries = 0
            while True:
                r = _need_more()
                if r == -1:
                    return None
                if r == 0:
                    empty_retries += 1
                    if empty_retries > 500:
                        return None
                    continue
                break
            out = state["buffer"]
            state["buffer"] = b""
            state["delivered"] += len(out)
            return out.decode("latin-1")

        def close(*_):
            state["closed"] = True
            try:
                sock.close()
            except Exception:
                pass

        handle = {
            "finishConnect": finish_connect,
            "response":      response,
            "read":          read,
            "close":         close,
        }
        return handle, state

    # ── internet.request(url[, data[, headers[, method]]]) ────────────────────
    def request(self, *raw_args):
        args = _clean_args(raw_args)

        url     = args[0] if len(args) > 0 else None
        data    = args[1] if len(args) > 1 else None
        headers = args[2] if len(args) > 2 else None
        method  = args[3] if len(args) > 3 else None

        if not isinstance(url, str):
            return None, "invalid url"
        url = _from_lua(url)

        if method is None:
            method = "POST" if data is not None else "GET"
        method = str(method).upper()

        if data is None:
            body = b""
        elif isinstance(data, bytes):
            body = data
        elif isinstance(data, str):
            body = data.encode("latin-1", errors="replace")
        else:
            body = str(data).encode("utf-8")

        hdrs: dict[str, str] = {}
        if headers is not None:
            try:
                if hasattr(headers, "items"):
                    for k, v in headers.items():
                        hdrs[_from_lua(str(k))] = _from_lua(str(v))
                elif hasattr(headers, "keys"):
                    for k in headers.keys():
                        hdrs[_from_lua(str(k))] = _from_lua(str(headers[k]))
            except Exception as e:
                logger.warning("Cannot parse headers: %s", e)

        current_url    = url
        current_method = method
        current_body   = body
        current_hdrs   = dict(hdrs)

        for _ in range(MAX_REDIRECTS + 1):
            parsed = urllib.parse.urlparse(current_url)
            host = parsed.netloc
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            use_ssl = parsed.scheme == "https"
            port = parsed.port or (443 if use_ssl else 80)
            hostname = parsed.hostname or host

            handle, state = self._do_request_once(
                hostname, port, use_ssl, current_method, path, host,
                current_body, dict(current_hdrs),
            )
            if handle is None:
                return None, state   # state тут = reason

            r = handle["finishConnect"]()
            if r is None:
                handle["close"]()
                return None, "connection failed"

            status_str = state["header"].get("status", "0")
            try:
                status = int(status_str)
            except ValueError:
                status = 0

            if status in (301, 302, 303, 307, 308):
                location = state["header"].get("location")
                handle["close"]()
                if not location:
                    return None, "redirect without location"
                new_url = urllib.parse.urljoin(current_url, location)

                logger.debug("HTTP %d redirect: %s -> %s",
                             status, current_url, new_url)

                current_url = new_url
                if status == 303 or (status in (301, 302)
                                     and current_method == "POST"):
                    current_method = "GET"
                    current_body = b""
                    current_hdrs = {
                        k: v for k, v in current_hdrs.items()
                        if k.lower() not in ("content-length", "content-type")
                    }
                continue

            return handle

        return None, "too many redirects"

    def as_proxy(self) -> dict:
        return {
            "isHttpEnabled": self.is_http_enabled,
            "isTcpEnabled":  self.is_tcp_enabled,
            "connect":       self.connect,
            "request":       self.request,
        }


def start() -> str:
    comp = InternetComponent()
    addr = component_module.register(None, "internet", comp.as_proxy())
    logger.info("Internet component registered: %s", addr)
    return addr