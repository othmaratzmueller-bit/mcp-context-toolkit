"""Selbst-Test der Gold-Referenzen: beweist, dass beide safe_join-Referenzen die 4
Killer-Edge-Cases (Traversal, absolute, Symlink-Grenze, Praefix-Falle) wirklich dicht
machen. Dient als Korrektheits-Orakel des Code-Quality-Evals. Reine stdlib."""
import importlib.util
import os
import tempfile


def _load(name):
    p = os.path.join(os.path.dirname(__file__), f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.safe_join


def run(safe_join, label):
    fails = []
    def ok(name, cond):
        if not cond:
            fails.append(name)
    with tempfile.TemporaryDirectory() as root:
        base = os.path.join(root, "base")
        evil = os.path.join(root, "base-evil")
        os.makedirs(os.path.join(base, "a"))
        os.makedirs(evil)
        open(os.path.join(evil, "x"), "w").close()

        # 1. Normaler Join bleibt drin
        ok("join-inside", safe_join(base, "a", "b").startswith(os.path.realpath(base)))
        # 2. internes .. erlaubt (a/../b -> base/b)
        try:
            r = safe_join(base, "a/../b")
            ok("internal-dotdot", os.path.realpath(base) == os.path.dirname(r) or r.endswith(os.sep + "b"))
        except ValueError:
            fails.append("internal-dotdot-raised")
        # 3. .. Ausbruch -> ValueError
        try:
            safe_join(base, "..", "etc"); fails.append("escape-not-caught")
        except ValueError:
            pass
        # 4. absolute part -> ValueError
        try:
            safe_join(base, "/etc/passwd"); fails.append("absolute-not-caught")
        except ValueError:
            pass
        # 5. Praefix-Falle: ../base-evil/x muss ausserhalb sein
        try:
            safe_join(base, "..", "base-evil", "x"); fails.append("prefix-trap-not-caught")
        except ValueError:
            pass
        # 6. Symlink-Grenze: base/link -> evil, dann link/x muss gefangen werden
        link = os.path.join(base, "link")
        try:
            os.symlink(evil, link)
            try:
                safe_join(base, "link", "x"); fails.append("symlink-escape-not-caught")
            except ValueError:
                pass
        except OSError:
            pass  # kein Symlink-Support -> Test ueberspringen
        # 7. leere / '.' parts ignoriert
        try:
            safe_join(base, "", ".", "a")
        except ValueError:
            fails.append("empty-dot-raised")
    print(f"{label}: {'ALLE GRUEN' if not fails else 'FAIL ' + str(fails)}")
    return not fails


if __name__ == "__main__":
    import sys
    good = all([run(_load("opus"), "opus-ref"), run(_load("sonnet"), "sonnet-ref")])
    sys.exit(0 if good else 1)
