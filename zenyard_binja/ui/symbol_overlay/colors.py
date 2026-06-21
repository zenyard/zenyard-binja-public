"""Theme-aware colour selection for the symbol overlay. Qt-free.

The applied-symbol tint was originally a single light pink picked against dark
themes; on Binary Ninja's light themes (Classic, Slushee Light, Solarized
Light, Summer) it is nearly unreadable. The delegate resolves the tint per
paint against the background it actually covers, so this module only needs to
answer: given that background, which variant of the brand pink reads on it?
"""

from __future__ import annotations

Rgb = tuple[int, int, int]

APPLIED_ON_DARK: Rgb = (220, 202, 255)  # #dccaff — original pink
APPLIED_ON_LIGHT: Rgb = (122, 76, 212)  # #7a4cd4 — same hue, darker


def background_is_dark(red: int, green: int, blue: int) -> bool:
    # Rec. 601 luma; 128 splits all stock BN theme backgrounds correctly.
    return (299 * red + 587 * green + 114 * blue) // 1000 < 128


def applied_text_rgb(background: Rgb) -> Rgb:
    """The applied-symbol tint that stays readable over ``background``."""
    if background_is_dark(*background):
        return APPLIED_ON_DARK
    return APPLIED_ON_LIGHT
