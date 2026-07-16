"""GOLD is_safe_url — kennt alle Tarnformen (dezimal/hex/oktal/Kurzform/IPv4-mapped)
und die userinfo-Falle. Muss correctness + security gruen sein."""
import ipaddress
from urllib.parse import urlsplit


def _parse_numeric_ipv4(h):
    if not h or any(c not in "0123456789abcdefABCDEFx." for c in h):
        return None
    parts = h.split(".")
    if len(parts) > 4:
        return None
    vals = []
    for p in parts:
        if p == "":
            return None
        try:
            if p.startswith(("0x", "0X")):
                v = int(p, 16)
            elif p.startswith("0") and len(p) > 1 and all(c in "01234567" for c in p):
                v = int(p, 8)
            else:
                v = int(p, 10)
        except ValueError:
            return None
        if v < 0:
            return None
        vals.append(v)
    n = len(vals)
    if n == 1:
        num = vals[0]
    else:
        if any(v > 0xFF for v in vals[:-1]):
            return None
        fill = 4 - (n - 1)
        if vals[-1] >= (1 << (8 * fill)):
            return None
        num = 0
        for v in vals[:-1]:
            num = (num << 8) | v
        num = (num << (8 * fill)) | vals[-1]
    if num >= (1 << 32):
        return None
    return ipaddress.IPv4Address(num)


def _host_ip(host):
    if host.startswith("[") and host.endswith("]"):
        try:
            ip = ipaddress.ip_address(host[1:-1])
        except ValueError:
            return None
    else:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = _parse_numeric_ipv4(host)
            if ip is None:
                return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def is_safe_url(url: str) -> bool:
    """True nur bei http(s), ohne userinfo, Host = oeffentliches IP-Literal."""
    try:
        s = urlsplit(url)
    except ValueError:
        return False
    if s.scheme.lower() not in ("http", "https"):
        return False
    if "@" in (s.netloc or ""):
        return False
    ip = _host_ip(s.hostname or "")
    if ip is None:
        return False
    return bool(ip.is_global)
