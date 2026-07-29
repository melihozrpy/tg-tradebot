from __future__ import annotations

from app.fundamentals.base import (
    DisabledFundamentalDataProvider,
    FallbackFundamentalDataProvider,
    FundamentalDataProvider,
)
from app.fundamentals.cross_check import FundamentalCrossCheckService
from app.fundamentals.providers import FintablesMcpProvider, LicensedKapRestProvider, YahooFundamentalProvider


def _text(settings, name: str, default: str = "") -> str:
    return str(getattr(settings, name, default) or "").strip()


def _fintables(settings) -> FintablesMcpProvider | None:
    endpoint = _text(settings, "fintables_mcp_url", "https://evo.fintables.com/mcp")
    token = _text(settings, "fintables_mcp_bearer_token") or _text(
        settings, "fintables_oauth_bearer_token"
    )
    tool = _text(settings, "fintables_mcp_tool_name")
    if not token:
        return None
    return FintablesMcpProvider(
        endpoint=endpoint,
        bearer_token=token,
        tool_name=tool,
        symbol_argument=_text(settings, "fintables_mcp_symbol_argument", "symbol"),
        timeout_seconds=float(getattr(settings, "fundamental_timeout_seconds", 20.0)),
    )


def _kap(settings) -> LicensedKapRestProvider | None:
    base_url = _text(settings, "kap_rest_base_url")
    api_key = _text(settings, "kap_rest_api_key")
    if not base_url or not api_key:
        return None
    return LicensedKapRestProvider(
        base_url=base_url,
        api_key=api_key,
        endpoint_path_template=_text(settings, "kap_rest_endpoint_path", "/fundamentals/{symbol}"),
        api_key_header=_text(settings, "kap_rest_api_key_header", "X-API-Key"),
        timeout_seconds=float(getattr(settings, "fundamental_timeout_seconds", 20.0)),
    )


def build_fundamental_provider(settings) -> FundamentalDataProvider:
    """Build an explicitly configured, provenance-preserving provider chain.

    No Fintables/KAP web page is scraped. ``auto`` only selects an authorized
    adapter when its credentials exist; Yahoo is used solely when the operator
    explicitly permits the delayed secondary fallback.
    """

    mode = _text(settings, "fundamental_provider", "auto").casefold()
    fintables = _fintables(settings)
    kap = _kap(settings)
    yahoo_allowed = bool(getattr(settings, "fundamental_allow_yahoo_fallback", False))
    secondary_fallback_allowed = bool(
        getattr(settings, "fundamental_allow_secondary_fallback", False)
    )
    yahoo = YahooFundamentalProvider() if yahoo_allowed or mode == "yahoo" else None

    if mode == "fintables_mcp":
        primary = fintables
        missing = "Fintables MCP OAuth tokeni eksik."
    elif mode == "kap_rest":
        primary = kap
        missing = "Lisanslı KAP REST adresi veya API anahtarı eksik."
    elif mode == "yahoo":
        primary = yahoo
        missing = "Yahoo ikincil temel veri sağlayıcısı kullanılamıyor."
    elif mode == "auto":
        primary = fintables or kap or yahoo
        missing = "Yetkili temel veri sağlayıcısı yapılandırılmadı."
    elif mode == "disabled":
        # Eski Coolify kurulumlarında bu değer kalmış olsa bile operatör Yahoo
        # fallback'ine açıkça izin verdiyse temel analizi tamamen kapatma.
        # Kaynak ikincil/gecikmeli olarak etiketlenir; KAP verisiymiş gibi sunulmaz.
        if yahoo is not None:
            primary = yahoo
            missing = "Yahoo ikincil temel veri sağlayıcısı kullanılamıyor."
        else:
            return DisabledFundamentalDataProvider(
                "Temel analiz kapalı ve FUNDAMENTAL_ALLOW_YAHOO_FALLBACK=false."
            )
    else:
        return DisabledFundamentalDataProvider(f"Bilinmeyen temel veri sağlayıcısı: {mode}")

    if primary is None:
        return DisabledFundamentalDataProvider(missing)
    if not bool(getattr(settings, "fundamental_cross_check_enabled", False)):
        if mode == "auto" and yahoo is not None:
            ordered = tuple(
                dict.fromkeys(
                    provider for provider in (fintables, kap, yahoo) if provider is not None
                )
            )
            if len(ordered) > 1:
                return FallbackFundamentalDataProvider(*ordered)
        return primary

    secondary: FundamentalDataProvider | None = None
    secondary_mode = _text(settings, "fundamental_secondary_provider").casefold()
    explicit_secondary = {
        "fintables_mcp": fintables,
        "kap_rest": kap,
        "yahoo": yahoo,
        "disabled": None,
        "": None,
    }
    if secondary_mode not in explicit_secondary:
        return DisabledFundamentalDataProvider(
            f"Bilinmeyen ikincil temel veri sağlayıcısı: {secondary_mode}"
        )
    if secondary_mode:
        secondary = explicit_secondary[secondary_mode]
        if secondary_mode != "disabled" and secondary is None:
            return DisabledFundamentalDataProvider(
                f"İkincil temel veri sağlayıcısı yapılandırılmadı: {secondary_mode}"
            )
        if secondary is primary:
            return DisabledFundamentalDataProvider(
                "Çapraz kontrol için birincil kaynaktan bağımsız ikinci sağlayıcı gerekir."
            )
    else:
        for candidate in (kap, fintables, yahoo):
            if candidate is not None and candidate is not primary:
                secondary = candidate
                break
    return FundamentalCrossCheckService(
        primary,
        secondary,
        relative_tolerance=getattr(settings, "fundamental_cross_check_relative_tolerance", 0.03),
        absolute_tolerance=getattr(settings, "fundamental_cross_check_absolute_tolerance", 1.0),
        strict=bool(getattr(settings, "fundamental_cross_check_strict", True)),
        allow_secondary_fallback=secondary_fallback_allowed,
    )
