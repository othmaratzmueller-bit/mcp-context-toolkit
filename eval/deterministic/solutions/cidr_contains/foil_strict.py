"""FOIL cidr_contains — der Einzeiler, der 'meistens funktioniert': ip_network mit
Default strict=True und kein Familien-Guard. Crasht bei gesetzten Host-Bits
('10.0.0.5/24') und bei gemischten Familien (v4-IP gegen v6-Netz) — beides wird als
Fehlverhalten (kein sauberes False) deterministisch erkannt. Soll auf security/edge
rot werden."""
import ipaddress


def cidr_contains(cidr: str, ip: str) -> bool:
    net = ipaddress.ip_network(cidr)            # strict=True -> ValueError bei Host-Bits
    return ipaddress.ip_address(ip) in net      # mixed family -> TypeError
