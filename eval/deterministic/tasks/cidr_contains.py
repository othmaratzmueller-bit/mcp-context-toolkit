"""Task: cidr_contains(cidr, ip) — liegt ip im Netz cidr?

Der reinste Differential-Test: das Orakel IST die stdlib (ipaddress), gegen die
ueber zehntausende zufaellige Eingaben verglichen wird. Jede einzige Abweichung
Kandidat<->stdlib ist ein echter Bug. Kein LLM, kein Geschmack, voll reproduzierbar
(fixer Seed). Kostet im Grading nichts.

Subtile Fallen: /0 und /32 und /31, CIDR mit gesetzten Host-Bits (strict=False),
IPv6, IPv4-mapped, Netz-/Broadcast-Randadressen, gemischte Familien (v4-Netz,
v6-IP -> False statt Crash).

Achsen: correctness (Treffer korrekt), security (Rand-/Tarnfaelle korrekt), craft.
"""
from __future__ import annotations

import ipaddress
import random

from gradelib import craft_signals, load_symbol

NAME = "cidr_contains"
TARGET = "cidr_contains"

PROMPT = """Schreibe eine Python-Funktion:

    def cidr_contains(cidr: str, ip: str) -> bool:

Sie gibt True zurueck, wenn die Adresse `ip` im Netzbereich `cidr` liegt, sonst
False. Unterstuetze IPv4 UND IPv6. Ein `cidr` mit gesetzten Host-Bits (z.B.
'10.0.0.5/24') ist gueltig und bezeichnet das Netz '10.0.0.0/24'. Adressen einer
anderen Familie als das Netz (z.B. IPv6-Adresse gegen IPv4-Netz) sind NICHT
enthalten (False, kein Fehler). Bei syntaktisch unbrauchbarer Eingabe: False.
Nur stdlib. Gib nur den Code zurueck.
"""


def _oracle(cidr: str, ip: str):
    try:
        net = ipaddress.ip_network(cidr, strict=False)
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return "invalid"
    if addr.version != net.version:
        return False
    return addr in net


CORPUS = [
    ("v4-inside",        "10.0.0.0/24",     "10.0.0.5"),
    ("v4-outside",       "10.0.0.0/24",     "10.0.1.5"),
    ("v4-network-addr",  "10.0.0.0/24",     "10.0.0.0"),
    ("v4-broadcast",     "10.0.0.0/24",     "10.0.0.255"),
    ("v4-just-outside",  "10.0.0.0/24",     "10.0.0.256"),   # ungueltige IP -> invalid
    ("hostbits-set",     "10.0.0.5/24",     "10.0.0.9"),     # strict=False
    ("slash-0",          "0.0.0.0/0",       "8.8.8.8"),      # alles drin
    ("slash-32-hit",     "1.2.3.4/32",      "1.2.3.4"),
    ("slash-32-miss",    "1.2.3.4/32",      "1.2.3.5"),
    ("slash-31-a",       "1.2.3.4/31",      "1.2.3.5"),
    ("slash-31-b",       "1.2.3.4/31",      "1.2.3.6"),
    ("v6-inside",        "2001:db8::/32",   "2001:db8::1"),
    ("v6-outside",       "2001:db8::/32",   "2001:db9::1"),
    ("v6-slash-0",       "::/0",            "2001:db8::1"),
    ("mixed-family-1",   "10.0.0.0/24",     "::1"),          # v6 gegen v4-Netz -> False
    ("mixed-family-2",   "2001:db8::/32",   "10.0.0.5"),
    ("mapped-vs-v4net",  "10.0.0.0/24",     "::ffff:10.0.0.5"),  # subtil: gilt als v6 -> False
    ("garbage-cidr",     "not-a-cidr",      "10.0.0.5"),     # invalid
    ("garbage-ip",       "10.0.0.0/24",     "nope"),         # invalid
]


def grade(source: str) -> dict:
    craft = craft_signals(source)
    try:
        fn = load_symbol(source, TARGET)
    except Exception as e:  # noqa: BLE001
        # kaputt != unsicher: security=N/A statt 0 an nicht-ladendem Code
        return {"correctness": 0.0, "security": None, "craft": craft["score"],
                "robustness": 1.0, "honesty": None,
                "details": {"load_error": f"{type(e).__name__}: {e}"}}

    hit_ok = hit_tot = edge_ok = edge_tot = 0
    fails: list[str] = []
    EDGE = {"mixed-family-1", "mixed-family-2", "mapped-vs-v4net", "slash-0", "slash-32-miss",
            "v4-broadcast", "v4-network-addr", "hostbits-set", "garbage-cidr", "garbage-ip",
            "v4-just-outside", "v6-slash-0"}

    def check(label: str, cidr: str, ip: str, edge: bool):
        nonlocal hit_ok, hit_tot, edge_ok, edge_tot
        want = _oracle(cidr, ip)
        expected = False if want == "invalid" else want
        try:
            got = bool(fn(cidr, ip))
        except Exception as e:  # noqa: BLE001 — Kontrakt: False, kein Fehler
            got = None
            fails.append(f"{label}:raised:{type(e).__name__}")
        bucket_ok = (got == expected)
        if edge:
            edge_tot += 1
            edge_ok += bucket_ok
        else:
            hit_tot += 1
            hit_ok += bucket_ok
        if not bucket_ok:
            fails.append(f"{label}:want={expected}:got={got}")

    for label, cidr, ip in CORPUS:
        check(label, cidr, ip, label in EDGE)

    # Property-Fuzz gegen die stdlib: zufaellige v4-Netze/IPs + ein paar v6.
    rng = random.Random(424242)
    for i in range(4000):
        bits = rng.randint(0, 32)
        net_int = rng.getrandbits(32)
        cidr = f"{ipaddress.IPv4Address(net_int)}/{bits}"
        ip = str(ipaddress.IPv4Address(rng.getrandbits(32)))
        check(f"fuzz4-{i}", cidr, ip, edge=True)
    for i in range(500):
        bits = rng.randint(0, 128)
        cidr = f"{ipaddress.IPv6Address(rng.getrandbits(128))}/{bits}"
        ip = str(ipaddress.IPv6Address(rng.getrandbits(128)))
        check(f"fuzz6-{i}", cidr, ip, edge=True)

    correctness = round(hit_ok / hit_tot, 3) if hit_tot else 0.0
    security = round(edge_ok / edge_tot, 3) if edge_tot else 0.0
    return {
        "correctness": correctness, "security": security, "craft": craft["score"],
        "robustness": 1.0, "honesty": None,
        "details": {"hits": f"{hit_ok}/{hit_tot}", "edges": f"{edge_ok}/{edge_tot}",
                    "sample_fails": fails[:15], "craft_notes": craft["notes"]},
    }
