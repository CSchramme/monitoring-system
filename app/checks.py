"""Check implementations (pull) and evaluation of pushed data."""

import asyncio
import ipaddress
import re
import shutil
import ssl
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import httpx

PULL_TYPES = ("http", "tcp", "ping", "dns")
PUSH_TYPES = ("push",)
MONITOR_TYPES = PULL_TYPES + PUSH_TYPES

HTTP_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
THRESHOLD_OPS = (">", ">=", "<", "<=", "==", "!=")

_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9_](?:[A-Za-z0-9_\-.]*[A-Za-z0-9_])?\.?$")
_STATUS_ITEM_RE = re.compile(r"^(\d{3})(?:-(\d{3}))?$|^([1-5])xx$", re.IGNORECASE)
_METRIC_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]{0,63}$")

# Cache for TLS certificate expiry lookups: (host, port) -> (checked_at, not_after_epoch)
_CERT_CACHE: dict[tuple[str, int], tuple[float, float]] = {}
_CERT_CACHE_TTL = 6 * 3600


@dataclass
class CheckResult:
    ok: bool
    latency: float | None = None  # milliseconds
    message: str = ""
    metrics: dict[str, float] = field(default_factory=dict)


# --------------------------------------------------------------------------- validation helpers


def is_valid_host(host: str) -> bool:
    if not host or host.startswith("-"):
        return False
    with suppress(ValueError):
        ipaddress.ip_address(host.strip("[]"))
        return True
    return bool(_HOSTNAME_RE.match(host))


def parse_host_port(target: str) -> tuple[str, int]:
    """Parse 'host:port' or '[v6addr]:port'. Raises ValueError on invalid input."""
    target = target.strip()
    if target.startswith("["):
        host, sep, rest = target[1:].partition("]")
        if not sep or not rest.startswith(":"):
            raise ValueError("Format: [IPv6]:Port")
        port_str = rest[1:]
    else:
        host, sep, port_str = target.rpartition(":")
        if not sep:
            raise ValueError("Format: host:port")
    if not is_valid_host(host):
        raise ValueError(f"Ungültiger Host: {host!r}")
    if not port_str.isdigit() or not 1 <= int(port_str) <= 65535:
        raise ValueError(f"Ungültiger Port: {port_str!r}")
    return host, int(port_str)


def parse_status_spec(spec: str) -> list[tuple[int, int]]:
    """Parse an expected-status spec like '200-399', '200,204' or '2xx,301'."""
    ranges: list[tuple[int, int]] = []
    for item in (part.strip() for part in spec.split(",")):
        if not item:
            continue
        match = _STATUS_ITEM_RE.match(item)
        if not match:
            raise ValueError(f"Ungültiger Statuscode-Ausdruck: {item!r}")
        if match.group(3):
            base = int(match.group(3)) * 100
            ranges.append((base, base + 99))
        else:
            low = int(match.group(1))
            high = int(match.group(2) or low)
            if high < low:
                raise ValueError(f"Ungültiger Bereich: {item!r}")
            ranges.append((low, high))
    if not ranges:
        raise ValueError("Mindestens ein Statuscode erforderlich")
    return ranges


def status_matches(code: int, spec: str) -> bool:
    return any(low <= code <= high for low, high in parse_status_spec(spec))


def is_valid_metric_name(name: str) -> bool:
    return bool(_METRIC_NAME_RE.match(name))


# --------------------------------------------------------------------------- pull checks


async def check_http(monitor: dict[str, Any]) -> CheckResult:
    cfg = monitor["config"]
    timeout = monitor["timeout"]
    url = monitor["target"]
    verify = cfg.get("verify_tls", True)

    async with httpx.AsyncClient(
        verify=verify,
        follow_redirects=cfg.get("follow_redirects", True),
        timeout=timeout,
        headers={"User-Agent": "monitoring-system/1.0"},
    ) as client:
        start = time.perf_counter()
        response = await client.request(
            cfg.get("method", "GET"),
            url,
            headers=cfg.get("headers") or None,
            content=cfg.get("body") or None,
        )
        latency = (time.perf_counter() - start) * 1000
        body = response.text if cfg.get("keyword") else ""

    expected = cfg.get("expected_status", "200-399")
    if not status_matches(response.status_code, expected):
        return CheckResult(False, latency, f"HTTP {response.status_code} (erwartet {expected})")

    keyword = cfg.get("keyword") or ""
    if keyword:
        found = keyword in body
        if cfg.get("keyword_invert") and found:
            return CheckResult(False, latency, f"Unerwünschter Text gefunden: {keyword!r}")
        if not cfg.get("keyword_invert") and not found:
            return CheckResult(False, latency, f"Text nicht gefunden: {keyword!r}")

    result = CheckResult(True, latency, f"HTTP {response.status_code}")

    warn_days = int(cfg.get("cert_expiry_days", 14) or 0)
    parts = urlsplit(url)
    if parts.scheme == "https" and verify and warn_days > 0 and parts.hostname:
        try:
            days_left = await cert_days_left(parts.hostname, parts.port or 443, timeout)
        except Exception as exc:  # the HTTP check itself succeeded; report but don't fail
            result.message += f" (Zertifikat nicht prüfbar: {exc})"
        else:
            result.metrics["cert_days"] = round(days_left, 1)
            if days_left < warn_days:
                result.ok = False
                result.message = f"TLS-Zertifikat läuft in {days_left:.0f} Tagen ab"
    return result


async def cert_days_left(host: str, port: int, timeout: float) -> float:
    key = (host, port)
    cached = _CERT_CACHE.get(key)
    now = time.time()
    if cached and now - cached[0] < _CERT_CACHE_TTL:
        return (cached[1] - now) / 86400

    context = ssl.create_default_context()
    _, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, ssl=context, server_hostname=host), timeout
    )
    try:
        cert = writer.get_extra_info("peercert") or {}
    finally:
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()
    not_after = ssl.cert_time_to_seconds(cert["notAfter"])
    _CERT_CACHE[key] = (now, not_after)
    return (not_after - now) / 86400


async def check_tcp(monitor: dict[str, Any]) -> CheckResult:
    host, port = parse_host_port(monitor["target"])
    start = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host.strip("[]"), port), monitor["timeout"])
    except (asyncio.TimeoutError, TimeoutError):
        return CheckResult(False, None, f"Timeout beim Verbinden mit Port {port}")
    except OSError as exc:
        return CheckResult(False, None, f"Port {port} nicht erreichbar: {exc.strerror or exc}")
    latency = (time.perf_counter() - start) * 1000
    writer.close()
    with suppress(Exception):
        await writer.wait_closed()
    return CheckResult(True, latency, f"Port {port} offen")


_PING_TIME_RE = re.compile(r"time[=<]\s*([\d.]+)\s*ms")


async def check_ping(monitor: dict[str, Any]) -> CheckResult:
    host = monitor["target"].strip()
    if not is_valid_host(host):
        return CheckResult(False, None, f"Ungültiger Host: {host!r}")
    binary = shutil.which("ping")
    if not binary:
        return CheckResult(False, None, "ping ist auf dem Server nicht installiert")
    timeout = monitor["timeout"]
    proc = await asyncio.create_subprocess_exec(
        binary, "-n", "-c", "1", "-W", str(timeout), host.strip("[]"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout + 2)
    except (asyncio.TimeoutError, TimeoutError):
        with suppress(ProcessLookupError):
            proc.kill()
        return CheckResult(False, None, "Timeout")
    text = output.decode(errors="replace")
    match = _PING_TIME_RE.search(text)
    if proc.returncode != 0 or not match:
        last_line = text.strip().splitlines()[-1] if text.strip() else "keine Antwort"
        return CheckResult(False, None, f"Keine Antwort: {last_line[:200]}")
    latency = float(match.group(1))
    return CheckResult(True, latency, f"Antwort in {latency:.1f} ms")


async def check_dns(monitor: dict[str, Any]) -> CheckResult:
    host = monitor["target"].strip()
    expected = (monitor["config"].get("expected") or "").strip()
    loop = asyncio.get_running_loop()
    start = time.perf_counter()
    try:
        infos = await asyncio.wait_for(loop.getaddrinfo(host, None), monitor["timeout"])
    except OSError as exc:
        return CheckResult(False, None, f"Auflösung fehlgeschlagen: {exc.strerror or exc}")
    latency = (time.perf_counter() - start) * 1000
    addresses = sorted({info[4][0] for info in infos})
    if expected and expected not in addresses:
        return CheckResult(False, latency, f"{expected} nicht in Antwort: {', '.join(addresses)}")
    return CheckResult(True, latency, ", ".join(addresses))


CHECKS: dict[str, Callable[[dict[str, Any]], Awaitable[CheckResult]]] = {
    "http": check_http,
    "tcp": check_tcp,
    "ping": check_ping,
    "dns": check_dns,
}


async def run_check(monitor: dict[str, Any]) -> CheckResult:
    handler = CHECKS[monitor["type"]]
    limit = monitor["timeout"] + 5
    try:
        return await asyncio.wait_for(handler(monitor), limit)
    except (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException):
        return CheckResult(False, None, f"Timeout nach {monitor['timeout']} s")
    except httpx.HTTPError as exc:
        return CheckResult(False, None, f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)
    except Exception as exc:
        return CheckResult(False, None, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- push evaluation

_UP_WORDS = {"up", "ok", "success", "1", "true", "healthy"}
_DOWN_WORDS = {"down", "fail", "failed", "error", "critical", "0", "false", "unhealthy"}


def parse_push_status(value: Any) -> bool | None:
    """Map a reported status to up (True) / down (False). None if unrecognised."""
    if value is None or value == "":
        return True
    text = str(value).strip().lower()
    if text in _UP_WORDS:
        return True
    if text in _DOWN_WORDS:
        return False
    return None


def _compare(left: float, op: str, right: float) -> bool:
    return {
        ">": left > right,
        ">=": left >= right,
        "<": left < right,
        "<=": left <= right,
        "==": left == right,
        "!=": left != right,
    }[op]


def evaluate_push(
    status_ok: bool,
    message: str,
    metrics: dict[str, float],
    thresholds: list[dict[str, Any]],
    latency: float | None = None,
) -> CheckResult:
    """Combine a pushed status with the monitor's metric thresholds.

    A threshold describes the *alarm* condition, e.g. {"metric": "cpu", "op": ">", "value": 90}.
    """
    violations = []
    for rule in thresholds:
        value = metrics.get(rule["metric"])
        if value is None:
            continue
        if _compare(value, rule["op"], rule["value"]):
            violations.append(f"{rule['metric']}={value:g} {rule['op']} {rule['value']:g}")
    ok = status_ok and not violations
    if violations:
        text = "Grenzwert überschritten: " + ", ".join(violations)
        message = f"{text} – {message}" if message else text
    elif not message:
        message = "Signal empfangen" if status_ok else "Fehler gemeldet"
    return CheckResult(ok, latency, message, metrics)
