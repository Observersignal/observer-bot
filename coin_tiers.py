"""
Clasificación ESTÁTICA del universo de perps de Hyperliquid por market cap.

Norma acordada (análisis del histórico paper, jul-2026, 187 operaciones):
el win rate cae monótonamente con el market cap (75% en mega-caps → 47% en
micro-caps) y las 6 liquidaciones del histórico fueron todas en small caps.
De ahí la política por tramo (ver HL_TIER_* en ws.py):

  large  (cap ≥ $1B)      → margen ×1.5, leverage normal (10x)
  small  ($100M – $1B)    → margen ×0.5, leverage reducido (5x)
  micro  (< $100M)        → NO se copia

Snapshot tomado el 2026-07-05 con CoinGecko/CoinPaprika sobre los 176 perps
activos del meta de HL, desambiguando tickers a mano contra el precio real
(GRAM = Toncoin; LIT = Lighter, no Litentry; CC = Canton; IP = Story/Data
Network). Es una clasificación de una sola vez tomada como norma: al excluir
las micro y reducir las small, las derivas de market cap no necesitan
refresco frecuente. Si un coin cambia de orden de magnitud (p.ej. una small
que se hace top-20) basta moverlo de lista aquí.

Monedas NUEVAS (no listadas aquí): tier por defecto "small" (HL_TIER_DEFAULT)
— participan a mitad de tamaño y 5x hasta que alguien las clasifique, que es
lo que suele ser cierto para un listing reciente.
"""

TIER_LARGE = "large"
TIER_SMALL = "small"
TIER_MICRO = "micro"

_LARGE = {
    "AAVE", "ADA", "ASTER", "AVAX", "BCH", "BNB", "BTC", "CC", "DOGE", "DOT",
    "ETC", "ETH", "GRAM", "HBAR", "HYPE", "ICP", "LINK", "LTC", "MNT", "MORPHO",
    "NEAR", "ONDO", "PAXG", "SKY", "SOL", "SUI", "TAO", "TRX", "UNI", "WLD",
    "WLFI", "XLM", "XMR", "XRP", "ZEC", "kPEPE", "kSHIB",
    "TON",   # ticker viejo de Toncoin en HL (renombrado a GRAM)
}

_SMALL = {
    "2Z", "AERO", "ALGO", "APE", "APT", "AR", "ARB", "ATOM", "AXS", "BSV",
    "CAKE", "CFX", "COMP", "CRV", "DASH", "DYDX", "EIGEN", "ENA", "ENS", "ETHFI",
    "FARTCOIN", "FET", "FIL", "GALA", "GRASS", "IMX", "INJ", "IOTA", "JTO", "JUP",
    "KAITO", "KAS", "LDO", "LIT", "MON", "NEO", "OP", "PENDLE", "PENGU", "POL",
    "PUMP", "PYTH", "RENDER", "RUNE", "S", "SAND", "SEI", "SPX", "STABLE", "STRK",
    "STX", "SYRUP", "TIA", "TRUMP", "VIRTUAL", "VVV", "WIF", "XPL", "ZK", "ZRO",
    "kBONK", "kFLOKI", "kLUNC",
    "IP",    # Story/Data Network ($108M) — delistado en HL, en el histórico
}

_MICRO = {
    "0G", "ACE", "AIXBT", "ALT", "ANIME", "APEX", "AVNT", "AZTEC", "BABY", "BANANA",
    "BERA", "BIGTIME", "BIO", "BLUR", "BOME", "BRETT", "CELO", "CHIP", "DYM", "FOGO",
    "GAS", "GMT", "GMX", "GOAT", "GRIFFAIN", "HEMI", "HMSTR", "HYPER", "INIT", "IO",
    "LAYER", "LINEA", "MANTA", "ME", "MEGA", "MELANIA", "MEME", "MERL", "MET", "MINA",
    "MOODENG", "MOVE", "NIL", "NOT", "NXPC", "ORDI", "PEOPLE", "PNUT", "POLYX", "POPCAT",
    "PROVE", "PURR", "RESOLV", "REZ", "RSR", "SAGA", "SKR", "SNX", "SOPH", "STBL",
    "SUPER", "SUSHI", "TNSR", "TRB", "TURBO", "UMA", "USUAL", "VINE", "W", "WCT",
    "XAI", "YGG", "ZEN", "ZETA", "ZORA", "kNEIRO",
}

TIER_BY_COIN = (
    {c: TIER_LARGE for c in _LARGE}
    | {c: TIER_SMALL for c in _SMALL}
    | {c: TIER_MICRO for c in _MICRO}
)


def tier_of(coin: str, default: str = TIER_SMALL) -> str:
    """Tier de un coin de HL. Los tickers k-prefijados (kPEPE) se buscan tal
    cual y, si no están, sin el prefijo. Desconocido → `default`."""
    if not coin:
        return default
    t = TIER_BY_COIN.get(coin)
    if t is None and coin.startswith("k"):
        t = TIER_BY_COIN.get(coin[1:])
    return t or default
