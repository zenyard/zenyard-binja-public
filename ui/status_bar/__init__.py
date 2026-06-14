"""Native status-bar widget package.

Kept import-free so ``coordinator`` can import the pure ``state`` module
without pulling Qt / ``binaryninjaui`` (via ``driver``) into its import graph.
Import ``install_status_bar`` directly from ``.driver`` at the registration
site.
"""

from __future__ import annotations
