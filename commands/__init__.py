#!/usr/bin/env python3
"""Commands package marker for command modules.

This file intentionally keeps imports minimal to avoid side-effects when the
package is imported. Individual command modules are imported directly by bot.py
during startup and registered via their register(tree, client) function.
"""

__all__ = ["palantir", "delete", "ragebait_mo", "heart_rate"]
