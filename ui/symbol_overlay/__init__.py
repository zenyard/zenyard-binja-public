"""Symbols-sidebar overlay package.

Tints rows in Binary Ninja's "Symbols" sidebar whose function/global has had an
AI inference applied, giving at-a-glance feedback on what the plugin has touched.

Kept import-free (no Qt / ``binaryninjaui`` at package import). Import
``install_symbol_overlay`` directly from ``.driver`` at the registration site.
"""

from __future__ import annotations
