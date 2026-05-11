"""
Tests for report/pdf_export.py — PDF generation.
"""

from report.pdf_export import generate_pdf, _safe


class TestSafe:
    def test_latin1_passthrough(self):
        assert _safe("Hello World") == "Hello World"

    def test_permille_replaced(self):
        result = _safe("BAC: 2.5\u2030")
        assert "\u2030" not in result

    def test_special_chars_replaced(self):
        result = _safe("a \u2260 b")  # ≠
        assert "\u2260" not in result

    def test_empty_string(self):
        assert _safe("") == ""


class TestGeneratePdf:
    def test_returns_bytes(self):
        report_data = {
            "summary_text": "10 BAC readings from 2025-01-01 to 2025-04-01.",
            "top_suspects": [
                {"ingredient": "Rice", "lift": 2.5, "n": 10,
                 "mean_bac_present": 1.2, "assessment": "Strong suspect"},
            ],
            "safe_ingredients": [
                {"ingredient": "Chicken", "lift": 0.5, "n": 8},
            ],
            "medication_comparison": [
                {"period": "none", "mean_bac": 0.8, "n_readings": 30,
                 "top_3_suspects": ["Rice"]},
            ],
            "combinations": [
                {"pair": ["Rice", "Banana"], "count": 5, "mean_bac": 1.5},
            ],
            "caveats": ["This is not medical advice."],
        }
        summary = {
            "date_min": "2025-01-01",
            "date_max": "2025-04-01",
            "total_readings": 50,
        }
        result = generate_pdf(report_data, summary)
        assert isinstance(result, (bytes, bytearray))
        assert len(result) > 100
        # PDF magic bytes
        assert bytes(result[:5]) == b"%PDF-"

    def test_empty_data(self):
        report_data = {
            "summary_text": "",
            "top_suspects": [],
            "safe_ingredients": [],
            "medication_comparison": [],
            "combinations": [],
            "caveats": [],
        }
        summary = {"date_min": "?", "date_max": "?", "total_readings": 0}
        result = generate_pdf(report_data, summary)
        assert bytes(result[:5]) == b"%PDF-"

    def test_unicode_in_ingredients(self):
        """Ensure unicode characters don't crash PDF generation."""
        report_data = {
            "summary_text": "BAC: 2.5\u2030, threshold \u2260 0",
            "top_suspects": [
                {"ingredient": "Cr\u00e8me Br\u00fbl\u00e9e", "lift": 1.5, "n": 5,
                 "mean_bac_present": 0.8, "assessment": "Moderate"},
            ],
            "safe_ingredients": [],
            "medication_comparison": [],
            "combinations": [],
            "caveats": ["Correlation \u2260 causation"],
        }
        summary = {"date_min": "2025-01-01", "date_max": "2025-04-01", "total_readings": 10}
        result = generate_pdf(report_data, summary)
        assert bytes(result[:5]) == b"%PDF-"
