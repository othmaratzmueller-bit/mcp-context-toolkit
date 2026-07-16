"""GOLD cidr_contains — korrekte ipaddress-Nutzung inkl. strict=False, Familien-Guard,
saubere Fehlerbehandlung. Muss auf allen Achsen gruen."""
import ipaddress


def cidr_contains(cidr: str, ip: str) -> bool:
    """True, wenn ip im Netz cidr liegt; verschiedene Familien -> False; Muell -> False."""
    try:
        net = ipaddress.ip_network(cidr, strict=False)
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.version != net.version:
        return False
    return addr in net
