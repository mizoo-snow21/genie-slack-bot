"""Shared Japanese font registration for matplotlib on Linux containers.

Both chart_generator.py and chart_generator_quick.py need Japanese fonts.
This module consolidates the registration logic: import japanize_matplotlib
for its monkey-patch, then explicitly addfont as a belt-and-suspenders
measure for containers where the auto-patch doesn't stick.

Must be called AFTER seaborn set_style/set_palette (which can reset rcParams).
"""
from pathlib import Path

from matplotlib import font_manager, rcParams

_font_name = None


def register_japanese_font() -> None:
    """Register IPAexGothic from japanize_matplotlib into matplotlib.

    Safe to call multiple times — addfont runs once, rcParams are
    re-applied every call (sns.set_style may reset them between calls).
    """
    global _font_name

    # addfont only once
    if _font_name is None:
        try:
            import japanize_matplotlib
            jm_dir = Path(japanize_matplotlib.__file__).parent
            font_files = list(jm_dir.glob("**/*.ttf"))
            if font_files:
                font_manager.fontManager.addfont(str(font_files[0]))
                _font_name = font_manager.FontProperties(fname=str(font_files[0])).get_name()
        except Exception:
            pass

    # Always re-apply rcParams (callers may have reset them with set_style/set_theme)
    if _font_name:
        rcParams["font.family"] = _font_name
    rcParams["axes.unicode_minus"] = False
