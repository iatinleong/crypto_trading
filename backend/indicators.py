"""
通用技術指標，不綁定任何特定交易策略，供任何策略模組共用。
目前只有 MACD/EMA；之後新策略如果需要別的指標，加在這裡，不要塞進策略模組裡。
"""
from typing import List, Optional, Tuple


def _ema(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    out[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1 - k)   # type: ignore[operator]
    return out


def compute_macd(
    closes: List[float],
    fast: int = 12,
    slow: int = 26,
    sig: int = 9,
) -> Tuple[List, List, List]:
    ema_f = _ema(closes, fast)
    ema_s = _ema(closes, slow)
    macd = [
        (f - s) if f is not None and s is not None else None
        for f, s in zip(ema_f, ema_s)
    ]
    first_valid = next((i for i, v in enumerate(macd) if v is not None), len(macd))
    raw_sig = _ema([v for v in macd[first_valid:] if v is not None], sig)
    signal: List[Optional[float]] = [None] * first_valid + raw_sig
    hist = [
        (m - s) if m is not None and s is not None else None
        for m, s in zip(macd, signal)
    ]
    return macd, signal, hist
