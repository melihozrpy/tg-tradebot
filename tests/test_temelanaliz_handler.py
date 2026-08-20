from __future__ import annotations

from app.services.company_analysis_service import CompanyAnalysis
from app.telegram.fundamental_handlers import format_fundamental_analysis


def test_detailed_fundamental_card_marks_missing_fields_instead_of_inventing() -> None:
    result = CompanyAnalysis(
        symbol="THYAO",
        name="Türk Hava Yolları",
        sector="Ulaştırma",
        industry="Havayolu",
        summary="Test özeti",
        status="DENGELİ",
        score=60,
        positives=("Serbest nakit akışı pozitif",),
        risks=("Borç/özsermaye %80.0",),
        metrics={"trailing_pe": 6.0, "price_to_book": 1.2, "enterprise_to_ebitda": 4.0, "total_debt": 100_000_000, "total_cash": 20_000_000, "market_cap": 500_000_000},
        quarterly_trends=(),
        valuation_lines=(),
        financial_period="2026-06-30",
        kap_url="https://www.kap.org.tr/tr/search/THYAO/1",
        source="test • primary",
        data_coverage=80,
    )
    text = format_fundamental_analysis(result)

    assert "F/K: 6.00x" in text
    assert "A/D (aktif/pasif): doğrulanmadı" in text
    assert "Yabancı oranı / halka açıklık / ortaklık yapısı: yapılandırılmış kaynakta doğrulanmadı" in text
    assert "İş Yatırım şirket kartı:" in text
