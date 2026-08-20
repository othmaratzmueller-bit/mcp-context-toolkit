from mcp_context_toolkit.engine import RulesEngine
from mcp_context_toolkit.models import Rule, RuleApplyTo, RuleComplianceRef, RuleConflict

# Aus den Paket-Metadaten lesen statt danebenschreiben. Zwei Versions-Strings
# driften zwangslaeufig auseinander: hier stand rc6, waehrend pyproject.toml
# schon bei rc9 war — und die Zahl, die ein Fremdnutzer per
# `import mcp_context_toolkit; .__version__` sieht, war die falsche. Jetzt gibt
# es nur noch EINE Stelle (pyproject.toml).
#
# Der Rueckfall greift nur, wenn das Paket gar nicht installiert ist (Import
# direkt aus dem Quellbaum) — dann gibt es keine Metadaten, aus denen sich eine
# Wahrheit lesen liesse, und eine erfundene Zahl waere schlimmer als ein
# ehrliches "unbekannt".
try:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version
    __version__ = _pkg_version("mcp-context-toolkit")
except PackageNotFoundError:  # pragma: no cover — nur ohne Installation
    __version__ = "0.0.0+unbekannt"
finally:
    # Die Import-Namen wieder wegraeumen: `__all__` steuert nur `import *`,
    # nicht den Attributzugriff — ohne das waere `mcp_context_toolkit.
    # PackageNotFoundError` Teil der oeffentlichen Oberflaeche dieses Pakets.
    del PackageNotFoundError, _pkg_version
__all__ = ["Rule", "RuleApplyTo", "RuleComplianceRef", "RuleConflict", "RulesEngine"]
