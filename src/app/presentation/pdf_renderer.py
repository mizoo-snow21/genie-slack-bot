"""Markdown+images → Professional PDF conversion using fpdf2.

Generates high-quality PDF research reports with colored tables, accent
headings, summary boxes, headers/footers, and chart embeddings.
"""
import io
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from fpdf import FPDF

logger = logging.getLogger(__name__)

# ─── Design constants ───────────────────────────────────────
ACCENT_COLOR = (41, 65, 122)       # Dark blue for headers, accents
ACCENT_LIGHT = (235, 242, 255)     # Light blue for summary box
TABLE_HEADER_BG = (41, 65, 122)    # Dark blue
TABLE_HEADER_FG = (255, 255, 255)  # White
TABLE_ROW_ALT = (245, 245, 250)    # Light gray for alternating rows
TABLE_BORDER = (200, 200, 210)     # Subtle border
TEXT_MUTED = (120, 120, 120)       # Muted text for headers/footers
TEXT_BLACK = (0, 0, 0)

# ─── Font resolution ────────────────────────────────────────
_SYSTEM_FONT_PATHS = [
    "/usr/share/fonts/ipa-gothic/ipag.ttf",
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
    "/usr/share/fonts/opentype/ipaexfont-gothic/ipaexg.ttf",
]


def _find_japanese_font() -> Optional[str]:
    for p in _SYSTEM_FONT_PATHS:
        if Path(p).exists():
            return p
    try:
        import japanize_matplotlib
        fp = Path(japanize_matplotlib.__file__).parent / "fonts" / "ipaexg.ttf"
        if fp.exists():
            return str(fp)
    except ImportError:
        pass
    return None


# ─── Helpers ────────────────────────────────────────────────
def _is_numeric_like(text: str) -> bool:
    value = text.strip().replace(",", "").replace("%", "").replace("¥", "").replace("$", "").replace("€", "")
    if value in {"", "-", "N/A", "n/a", "null", "None"}:
        return False
    try:
        float(value)
        return True
    except ValueError:
        return False


def _is_numeric_column(rows: list[list[str]], col_idx: int) -> bool:
    values = [row[col_idx].strip() for row in rows if col_idx < len(row)]
    values = [v for v in values if v not in {"", "-", "N/A", "n/a", "null", "None"}]
    if not values:
        return False
    return sum(1 for v in values if _is_numeric_like(v)) / len(values) >= 0.8


def _format_numeric(text: str) -> str:
    if not _is_numeric_like(text):
        return text
    try:
        val = float(text.strip().replace(",", ""))
        if val == int(val) and "." not in text.strip():
            return f"{int(val):,}"
        return f"{val:,.2f}"
    except ValueError:
        return text


def _render_inline_bold(pdf, font_name: str, text: str, left_x: float = 20, line_height: float = 6, base_size: int = 10):
    """Render text with **bold** inline. Temporarily shifts left margin for proper wrap indent."""
    saved_l_margin = pdf.l_margin
    try:
        pdf.set_left_margin(left_x)
        pdf.set_x(left_x)
        for part in re.split(r'(\*\*.*?\*\*)', text):
            if part.startswith('**') and part.endswith('**'):
                pdf.set_font(font_name, "B", base_size)
                pdf.write(line_height, part[2:-2])
            elif part:
                pdf.set_font(font_name, "", base_size)
                pdf.write(line_height, part)
        pdf.ln(line_height)
        pdf.set_font(font_name, "", base_size)
    except Exception:
        pdf.set_x(left_x)
        pdf.set_font(font_name, "", base_size)
        try:
            pdf.multi_cell(0, line_height, text.replace("**", ""))
        except Exception:
            pdf.ln(line_height)
    finally:
        pdf.set_left_margin(saved_l_margin)


# ─── ResearchPDF subclass ───────────────────────────────────
class ResearchPDF(FPDF):
    """FPDF subclass with professional header/footer."""

    def __init__(self, font_name: str = "Helvetica", report_title: str = "", job_id: str = ""):
        super().__init__()
        self._font_name = font_name
        self._report_title = report_title
        self._job_id = job_id

    def header(self):
        if self.page_no() > 1 and self._report_title:
            self.set_font(self._font_name, "I", 8)
            self.set_text_color(*TEXT_MUTED)
            self.cell(0, 5, self._report_title, align="R")
            self.ln(2)
            self.set_draw_color(200, 200, 200)
            self.line(20, self.get_y(), 190, self.get_y())
            self.ln(8)
            self.set_text_color(*TEXT_BLACK)

    def footer(self):
        self.set_y(-15)
        self.set_font(self._font_name, "I", 8)
        self.set_text_color(*TEXT_MUTED)
        self.cell(0, 10, f"Page {self.page_no()}/{{nb}}", align="C")
        self.set_text_color(*TEXT_BLACK)


# ─── Main renderer ──────────────────────────────────────────
class PdfRenderer:
    MARGIN_LEFT = 20
    MARGIN_RIGHT = 20
    MARGIN_TOP = 25
    MARGIN_BOTTOM = 20
    INDENT = 25
    PAGE_WIDTH = 170

    def render(self, markdown_report: str, job_id: str, chart_images: dict[str, bytes]) -> bytes:
        # Extract title from first H1
        title = ""
        for line in markdown_report.split("\n"):
            if line.startswith("# "):
                title = line[2:].strip()
                break

        jp_font = _find_japanese_font()
        font_name = "JapaneseFont" if jp_font else "Helvetica"

        pdf = ResearchPDF(font_name=font_name, report_title=title, job_id=job_id)
        pdf.set_margins(left=self.MARGIN_LEFT, top=self.MARGIN_TOP, right=self.MARGIN_RIGHT)
        pdf.set_auto_page_break(auto=True, margin=self.MARGIN_BOTTOM)

        if jp_font:
            pdf.add_font("JapaneseFont", "", jp_font)
            pdf.add_font("JapaneseFont", "B", jp_font)
            pdf.add_font("JapaneseFont", "I", jp_font)
            logger.info(f"Japanese font loaded: {jp_font}")
        else:
            logger.warning("No Japanese font found")

        pdf.add_page()
        pdf.set_font(font_name, size=10)
        lx = self.MARGIN_LEFT
        temp_files: list[str] = []
        in_summary = False  # Track if we're inside summary section

        try:
            lines = markdown_report.split("\n")
            i = 0
            while i < len(lines):
                line = lines[i]

                # ── Chart image ──
                img_match = re.match(r"!\[.*?\]\(/research/[^/]+/steps/(\w+)/chart\)", line.strip())
                if img_match:
                    step_id = img_match.group(1)
                    if step_id in chart_images:
                        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                        tmp.write(chart_images[step_id])
                        tmp.close()
                        temp_files.append(tmp.name)
                        try:
                            from PIL import Image as PILImage
                            with PILImage.open(tmp.name) as pil_img:
                                iw, ih = pil_img.size
                                dh = ih * (self.PAGE_WIDTH / iw)
                            if pdf.get_y() + dh > pdf.h - pdf.b_margin:
                                pdf.add_page()
                            pdf.image(tmp.name, w=self.PAGE_WIDTH)
                            pdf.ln(8)
                        except Exception as e:
                            logger.warning(f"Failed to embed chart {step_id}: {e}")
                    i += 1
                    continue

                # ── H1 Title ──
                if line.startswith("# "):
                    pdf.set_font(font_name, "B", 20)
                    pdf.set_text_color(*ACCENT_COLOR)
                    pdf.multi_cell(0, 11, line[2:].strip())
                    pdf.set_text_color(*TEXT_BLACK)
                    # Accent line under title
                    pdf.set_draw_color(*ACCENT_COLOR)
                    pdf.set_line_width(0.8)
                    pdf.line(lx, pdf.get_y() + 2, lx + 60, pdf.get_y() + 2)
                    pdf.set_line_width(0.2)
                    pdf.ln(10)
                    pdf.set_font(font_name, size=10)
                    i += 1
                    continue

                # ── H2 ──
                if line.startswith("## "):
                    heading = line[3:].strip()
                    is_prominent = heading.lower() in ("summary", "conclusion", "まとめ", "結論", "要約", "総合考察")

                    if is_prominent:
                        in_summary = heading.lower() in ("summary", "要約")
                        pdf.ln(6)
                        # Left accent bar
                        pdf.set_fill_color(*ACCENT_COLOR)
                        pdf.rect(lx, pdf.get_y(), 3, 10, style="F")
                        pdf.set_x(lx + 6)
                        pdf.set_font(font_name, "B", 16)
                        pdf.cell(0, 10, heading)
                        pdf.ln(14)
                    else:
                        in_summary = False
                        pdf.ln(4)
                        pdf.set_fill_color(*ACCENT_COLOR)
                        pdf.rect(lx, pdf.get_y() + 1, 3, 7, style="F")
                        pdf.set_x(lx + 6)
                        pdf.set_font(font_name, "B", 13)
                        pdf.cell(0, 8, heading)
                        pdf.ln(12)
                    pdf.set_font(font_name, size=10)
                    i += 1
                    continue

                # ── H3 ──
                if line.startswith("### "):
                    in_summary = False
                    pdf.ln(2)
                    pdf.set_font(font_name, "B", 11)
                    pdf.set_text_color(60, 60, 60)
                    pdf.multi_cell(0, 7, line[4:].strip())
                    pdf.set_text_color(*TEXT_BLACK)
                    pdf.ln(3)
                    pdf.set_font(font_name, size=10)
                    i += 1
                    continue

                # ── <details> skip ──
                if line.strip().startswith("<details>"):
                    i += 1
                    while i < len(lines) and lines[i].strip() != "</details>":
                        i += 1
                    if i < len(lines):
                        i += 1
                    continue

                # ── Code fence ──
                if line.strip().startswith("```"):
                    code_lines = []
                    i += 1
                    while i < len(lines) and not lines[i].strip().startswith("```"):
                        code_lines.append(lines[i])
                        i += 1
                    if i < len(lines):
                        i += 1
                    if code_lines:
                        pdf.set_font(font_name, size=8)
                        pdf.set_text_color(80, 80, 80)
                        for cl in code_lines:
                            pdf.set_x(self.INDENT)
                            try:
                                pdf.multi_cell(0, 4, cl)
                            except Exception:
                                pdf.ln(4)
                        pdf.set_text_color(*TEXT_BLACK)
                        pdf.set_font(font_name, size=10)
                    continue

                # ── Table ──
                if line.strip().startswith("|") and i + 1 < len(lines) and re.match(
                    r"\|[\s\-:|]+\|", lines[i + 1].strip()
                ):
                    table_lines = []
                    while i < len(lines) and lines[i].strip().startswith("|"):
                        table_lines.append(lines[i])
                        i += 1
                    self._render_table(pdf, table_lines, font_name)
                    pdf.ln(5)
                    continue

                # ── Separator ──
                if line.strip() == "---":
                    pdf.ln(4)
                    pdf.set_draw_color(200, 200, 200)
                    y = pdf.get_y()
                    pdf.line(lx, y, lx + self.PAGE_WIDTH, y)
                    pdf.ln(4)
                    i += 1
                    continue

                # ── Numbered list ──
                num_match = re.match(r"^(\d+)\.\s+(.*)", line.strip())
                if num_match:
                    number = num_match.group(1)
                    content = num_match.group(2)
                    prefix = f"{number}. "
                    pdf.set_x(self.INDENT)
                    pdf.set_font(font_name, "B", 10)
                    pw = pdf.get_string_width(prefix) + 1
                    pdf.cell(pw, 6, prefix)
                    text_x = self.INDENT + pw
                    if "**" in content:
                        _render_inline_bold(pdf, font_name, content, left_x=text_x)
                    else:
                        pdf.set_font(font_name, "", 10)
                        saved_lm = pdf.l_margin
                        pdf.set_left_margin(text_x)
                        try:
                            pdf.multi_cell(0, 6, content)
                        except Exception:
                            pdf.ln(6)
                        pdf.set_left_margin(saved_lm)
                    i += 1
                    continue

                # ── Bullet list ──
                if line.strip().startswith("- "):
                    bullet_text = line.strip()[2:]
                    pdf.set_x(self.INDENT)
                    pdf.set_font(font_name, "", 10)
                    pdf.cell(6, 6, "\u2022 ")
                    tx = self.INDENT + 6
                    if "**" in bullet_text:
                        _render_inline_bold(pdf, font_name, bullet_text, left_x=tx)
                    else:
                        saved_lm = pdf.l_margin
                        pdf.set_left_margin(tx)
                        try:
                            pdf.multi_cell(0, 6, bullet_text)
                        except Exception:
                            pdf.ln(6)
                        pdf.set_left_margin(saved_lm)
                    i += 1
                    continue

                # ── Empty line ──
                if not line.strip():
                    in_summary = False
                    pdf.ln(4)
                    i += 1
                    continue

                # ── Italic ──
                if re.match(r"^\*[^*].*[^*]\*$", line.strip()):
                    plain = line.strip().strip("*")
                    pdf.set_x(lx)
                    pdf.set_font(font_name, "I", 9)
                    pdf.set_text_color(*TEXT_MUTED)
                    try:
                        pdf.multi_cell(0, 5, plain)
                    except Exception:
                        pdf.ln(5)
                    pdf.set_text_color(*TEXT_BLACK)
                    pdf.set_font(font_name, size=10)
                    i += 1
                    continue

                # ── Inline bold ──
                if "**" in line:
                    _render_inline_bold(pdf, font_name, line, left_x=lx)
                    i += 1
                    continue

                # ── Regular text ──
                pdf.set_x(lx)
                try:
                    pdf.multi_cell(0, 6, line)
                except Exception:
                    pdf.ln(6)
                i += 1

        finally:
            for tf in temp_files:
                try:
                    os.unlink(tf)
                except OSError:
                    pass

        return bytes(pdf.output())

    def _render_table(self, pdf, table_lines: list[str], font_name: str):
        if len(table_lines) < 2:
            return

        header = [c.strip().replace("**", "") for c in table_lines[0].strip("|").split("|")]
        rows = []
        for line in table_lines[2:]:
            cells = [c.strip().replace("**", "") for c in line.strip("|").split("|")]
            rows.append(cells)

        num_cols = len(header)
        if num_cols == 0:
            return

        for idx, row in enumerate(rows):
            if len(row) < num_cols:
                rows[idx] = row + [""] * (num_cols - len(row))
            elif len(row) > num_cols:
                rows[idx] = row[:num_cols]

        numeric_cols = {j for j in range(num_cols) if _is_numeric_column(rows, j)}

        display_rows = []
        for row in rows:
            display_rows.append([
                _format_numeric(cell) if j in numeric_cols else cell
                for j, cell in enumerate(row)
            ])

        page_width = self.PAGE_WIDTH
        max_col_widths = []
        pdf.set_font(font_name, "B", 9)
        for j, h in enumerate(header):
            max_w = pdf.get_string_width(h) + 6
            pdf.set_font(font_name, size=9)
            for row in display_rows:
                max_w = max(max_w, pdf.get_string_width(row[j]) + 6)
            pdf.set_font(font_name, "B", 9)
            max_col_widths.append(min(max_w, page_width / 2))

        total = sum(max_col_widths)
        if total > page_width:
            max_col_widths = [w * page_width / total for w in max_col_widths]

        def _fit(text, col_w, style, sz):
            pdf.set_font(font_name, style, sz)
            if pdf.get_string_width(text) <= col_w - 3:
                return text
            lo, hi = 0, len(text)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if pdf.get_string_width(text[:mid] + "..") <= col_w - 3:
                    lo = mid
                else:
                    hi = mid - 1
            return text[:lo] + ".." if lo < len(text) else text

        # ── Header row (dark blue bg, white text) ──
        pdf.set_fill_color(*TABLE_HEADER_BG)
        pdf.set_text_color(*TABLE_HEADER_FG)
        pdf.set_draw_color(*TABLE_BORDER)
        pdf.set_font(font_name, "B", 9)
        for j, h in enumerate(header):
            align = "R" if j in numeric_cols else "C"
            pdf.cell(max_col_widths[j], 7, _fit(h, max_col_widths[j], "B", 9), border=1, align=align, fill=True)
        pdf.ln()

        # ── Data rows (alternating colors) ──
        pdf.set_text_color(*TEXT_BLACK)
        for row_idx, row in enumerate(display_rows):
            if row_idx % 2 == 1:
                pdf.set_fill_color(*TABLE_ROW_ALT)
            else:
                pdf.set_fill_color(255, 255, 255)
            pdf.set_font(font_name, size=9)
            for j in range(num_cols):
                align = "R" if j in numeric_cols else "L"
                pdf.cell(max_col_widths[j], 6, _fit(row[j], max_col_widths[j], "", 9), border=1, align=align, fill=True)
            pdf.ln()

        pdf.set_font(font_name, size=10)
        pdf.set_text_color(*TEXT_BLACK)
