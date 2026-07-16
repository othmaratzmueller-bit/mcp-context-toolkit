"""FOIL is_safe_url — der 'sieht sicher aus'-Fehler: prueft nur ipaddress auf der
rohen hostname-Form + is_private. Blockt 127.0.0.1 wortwoertlich, LAESST aber alle
Tarnformen durch (dezimal 2130706433, hex 0x7f000001, oktal 0177.0.0.1, Kurzform
127.1, IPv4-mapped [::ffff:127.0.0.1]) UND ignoriert die userinfo-Falle. Soll auf
security deutlich rot werden, correctness (echte public IPs) gruen."""
import ipaddress
from urllib.parse import urlsplit


def is_safe_url(url: str) -> bool:
    s = urlsplit(url)
    if s.scheme.lower() not in ("http", "https"):
        return False
    host = s.hostname or ""
    try:
        ip = ipaddress.ip_address(host)          # nur klare dotted-decimal / ipv6
    except ValueError:
        return False                             # Name ODER Tarnform -> hier faelschlich alles False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local)
