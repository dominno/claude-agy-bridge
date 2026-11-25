"""How command handlers write to the terminal: the app.console writers (answers to stdout, failures and refusals
to stderr), re-exported so every handler imports them from one place."""
from __future__ import annotations

from ..app.console import fail, refused, say

__all__ = ["fail", "refused", "say"]
