"""
PDF report generator for ABS Tracker.

Uses fpdf2 (pure Python, no system dependencies).
"""

from datetime import datetime
from fpdf import FPDF


def _safe(text: str) -> str:
    """Replace characters unsupported by built-in Helvetica (latin-1 only)."""
    return str(text).encode("latin-1", errors="replace").decode("latin-1")


class _ReportPDF(FPDF):
    """Custom PDF with header/footer."""

    def __init__(self, generated: str, date_range: str):
        super().__init__()
        self._generated = generated
        self._date_range = date_range

    def header(self):
        self.set_font("Helvetica", "B", 16)
        self.cell(0, 10, "ABS Diet Analysis Report", new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", "", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 5, f"Generated: {self._generated}  |  Data: {self._date_range}  |  BAC values in permille", new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)
        self.ln(4)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(150, 150, 150)
        self.cell(0, 10, f"ABS Tracker  |  Page {self.page_no()}/{{nb}}", align="C")

    def section_title(self, title: str):
        self.set_font("Helvetica", "B", 12)
        self.set_text_color(52, 73, 94)
        self.cell(0, 8, title, new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)
        self.ln(2)

    def body_text(self, text: str):
        self.set_font("Helvetica", "", 9)
        self.multi_cell(0, 5, text)
        self.ln(2)

    def add_table(self, headers: list[str], rows: list[list[str]], col_widths: list[int] | None = None):
        if not rows:
            self.set_font("Helvetica", "I", 9)
            self.cell(0, 6, "No data available.", new_x="LMARGIN", new_y="NEXT")
            self.ln(3)
            return

        usable = self.w - self.l_margin - self.r_margin
        if col_widths is None:
            col_widths = [int(usable / len(headers))] * len(headers)
        # Adjust last column to fill remaining space
        col_widths[-1] = int(usable - sum(col_widths[:-1]))

        # Header row
        self.set_font("Helvetica", "B", 8)
        self.set_fill_color(230, 230, 240)
        for i, h in enumerate(headers):
            self.cell(col_widths[i], 6, h, border=1, fill=True)
        self.ln()

        # Data rows
        self.set_font("Helvetica", "", 8)
        for j, row in enumerate(rows):
            if j % 2 == 1:
                self.set_fill_color(245, 245, 250)
                fill = True
            else:
                fill = False
            for i, val in enumerate(row):
                self.cell(col_widths[i], 5.5, _safe(str(val)[:50]), border=1, fill=fill)
            self.ln()
        self.ln(3)


def generate_pdf(report_data: dict, summary: dict) -> bytes:
    """
    Build a PDF report from analysis data using fpdf2.

    Parameters
    ----------
    report_data : dict
        Output of generate_report() from ai/template_engine.py.
    summary : dict
        The summary dict from _build_results_json().

    Returns
    -------
    bytes
        PDF file content.
    """
    date_min = summary.get("date_min", "?")
    date_max = summary.get("date_max", "?")
    generated = datetime.now().strftime("%d %B %Y, %H:%M")
    date_range = f"{date_min} to {date_max}"

    pdf = _ReportPDF(generated, date_range)
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=20)

    # Summary
    summary_text = report_data.get("summary_text", "")
    if summary_text:
        pdf.set_fill_color(240, 240, 255)
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(0, 5, _safe(summary_text), fill=True)
        pdf.ln(4)

    # Top Suspects
    suspects = report_data.get("top_suspects", [])
    pdf.section_title("Top Suspect Ingredients")
    pdf.add_table(
        ["Ingredient", "Lift", "n", "Avg BAC", "Assessment"],
        [
            [s["ingredient"], f"{s['lift']:.2f}", str(s["n"]),
             f"{s['mean_bac_present']:.3f}", s.get("assessment", "")]
            for s in suspects
        ],
        [45, 20, 15, 25, 0],
    )

    # Safe Ingredients
    safe = report_data.get("safe_ingredients", [])
    pdf.section_title("Likely Safe Ingredients")
    pdf.add_table(
        ["Ingredient", "Lift", "n"],
        [[s["ingredient"], f"{s['lift']:.2f}", str(s["n"])] for s in safe[:15]],
        [60, 30, 0],
    )

    # Medication Comparison
    med = report_data.get("medication_comparison", [])
    pdf.section_title("Medication Period Comparison")
    pdf.add_table(
        ["Period", "Mean BAC", "Readings", "Top Suspects"],
        [
            [
                c["period"],
                f"{c['mean_bac']:.3f}" if c["mean_bac"] is not None else "N/A",
                str(c["n_readings"]),
                ", ".join(c.get("top_3_suspects", [])),
            ]
            for c in med
        ],
        [40, 25, 22, 0],
    )

    # Risky Combinations
    combos = report_data.get("combinations", [])
    pdf.section_title("Risky Combinations")
    pdf.add_table(
        ["Combination", "Co-occurrences", "Avg BAC"],
        [
            [
                ", ".join(c.get("pair", c.get("ingredients", []))),
                str(c.get("count", 0)),
                f"{c.get('mean_bac', 0):.3f}",
            ]
            for c in combos
        ],
        [90, 30, 0],
    )

    # Caveats
    caveats = report_data.get("caveats", [])
    if caveats:
        pdf.section_title("Caveats")
        pdf.set_font("Helvetica", "", 9)
        for c in caveats:
            pdf.multi_cell(0, 5, _safe(f"  -  {c}"))
            pdf.ln(1)

    return pdf.output()
