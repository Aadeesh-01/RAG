"""
"Check if live": one HEAD request from the server to a repo's homepage or
deployment URL, reporting the status code and response time.

The URL comes from repo metadata anyone can set, so the request is guarded
against server-side request forgery:
  - https only, no credentials in the URL;
  - the host name is resolved first and every address must be public: private,
    loopback, link-local (169.254.x, cloud metadata), reserved, multicast and
    unspecified addresses are refused, IPv4-mapped IPv6 included;
  - the connection goes to the exact address that was checked: the name is
    resolved once, and the request is sent to that IP with the original host
    name kept for the Host header, TLS SNI and certificate verification, so a
    host that changes its DNS answer after the check (DNS rebinding) can't
    redirect the request to an internal address;
  - redirects are not followed blindly: each Location is resolved, checked and
    pinned the same way (at most 3 hops);
  - 5 s timeout; results (failures too) are cached for 10 minutes per URL.
Off unless CODERAG_LIVE_CHECK is set (web/settings.py).
"""

import ipaddress
import socket
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx


class Unsafe(ValueError):
    """The URL may not be requested from the server."""


def _bad_address(ip):
    addr = ipaddress.ip_address(ip.split("%")[0])
    if getattr(addr, "ipv4_mapped", None):
        addr = addr.ipv4_mapped
    return (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
            or addr.is_multicast or addr.is_unspecified or not addr.is_global)


def check_target(url, resolve=socket.getaddrinfo):
    """The addresses `url` resolves to, all public; Unsafe otherwise."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise Unsafe("Only https:// URLs are checked.")
    if parts.username or parts.password:
        raise Unsafe("URLs with credentials aren't checked.")
    try:                                         # a literal IP is checked as it is, never "resolved"
        literal_ip = ipaddress.ip_address(parts.hostname.strip("[]"))
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        if _bad_address(str(literal_ip)):
            raise Unsafe(f"{parts.hostname} is a private or local address; it isn't checked.")
        return {str(literal_ip)}
    try:
        infos = resolve(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError, UnicodeError):
        raise Unsafe(f"{parts.hostname} doesn't resolve.")
    addresses = {info[4][0] for info in infos}
    if not addresses or any(_bad_address(ip) for ip in addresses):
        raise Unsafe(f"{parts.hostname} points to a private or local address; it isn't checked.")
    return addresses


def pinned_request(url, ip):
    """
    (url with the host replaced by `ip`, headers, extensions): the request goes to
    that address while Host, SNI and certificate checks still use the host name.
    """
    parts = urlsplit(url)
    host = parts.hostname
    address = f"[{ip}]" if ":" in ip else ip
    netloc = f"{address}:{parts.port}" if parts.port else address
    target = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
    host_header = f"{host}:{parts.port}" if parts.port else host
    return target, {"Host": host_header}, {"sni_hostname": host}


def _pick(addresses):
    """One checked address to connect to (IPv4 first, then in order)."""
    return sorted(addresses, key=lambda ip: (":" in ip, ip))[0]


class LiveChecker:
    def __init__(self, transport=None, resolve=socket.getaddrinfo, ttl=600, timeout=5, max_redirects=3,
                 clock=time.monotonic):
        self.transport, self.resolve, self.ttl = transport, resolve, ttl
        self.timeout, self.max_redirects, self.clock = timeout, max_redirects, clock
        self.cache, self.lock = {}, threading.Lock()

    def check(self, url):
        """{url, final_url, status, ok, ms, checked_at} or {url, error, unsafe}; cached for `ttl`."""
        now = self.clock()
        with self.lock:
            hit = self.cache.get(url)
            if hit and now - hit[0] < self.ttl:
                return {**hit[1], "cached": True}
        result = self._check(url)
        with self.lock:
            self.cache[url] = (now, result)
            if len(self.cache) > 1000:
                self.cache = {k: v for k, v in self.cache.items() if now - v[0] < self.ttl}
        return {**result, "cached": False}

    def _check(self, url):
        checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        current, start = url, time.perf_counter()
        try:
            with httpx.Client(transport=self.transport, timeout=self.timeout, follow_redirects=False,
                              headers={"User-Agent": "coderag-live-check"}) as client:
                for _ in range(self.max_redirects + 1):
                    ip = _pick(check_target(current, self.resolve))    # resolved once, then pinned
                    target, headers, extensions = pinned_request(current, ip)
                    response = client.request("HEAD", target, headers=headers, extensions=extensions)
                    location = response.headers.get("location")
                    if 300 <= response.status_code < 400 and location:
                        current = urljoin(current, location)
                        continue
                    break
                else:
                    return {"url": url, "error": "Too many redirects.", "checked_at": checked_at}
        except Unsafe as e:
            return {"url": url, "error": str(e), "unsafe": True, "checked_at": checked_at}
        except httpx.TimeoutException:
            return {"url": url, "error": f"No response within {self.timeout:g} s.", "checked_at": checked_at}
        except httpx.HTTPError:
            return {"url": url, "error": "Couldn't connect.", "checked_at": checked_at}
        return {"url": url, "final_url": current, "status": response.status_code,
                "ok": response.status_code < 400, "ms": round((time.perf_counter() - start) * 1000),
                "checked_at": checked_at}


def candidate_urls(facts):
    """URLs a repo's facts point at, most specific first: homepage, deployments, Pages."""
    facts = facts or {}
    urls = [facts.get("homepage")] + [d.get("environment_url") for d in facts.get("deployments") or []] \
        + [facts.get("pages_url")]
    return [u for u in dict.fromkeys(urls) if u]
