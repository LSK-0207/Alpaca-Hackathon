from typing import Any, Dict, List, Union
import numpy as np
import pandas as pd


def compute_rsi(prices: Union[pd.Series, List[float]], period: int = 14) -> float:
    """
    Computes Wilder's RSI(14) as specified in §9:
    delta[t] = close[t] - close[t-1]
    gain[t] = max(delta[t], 0)
    loss[t] = max(-delta[t], 0)
    avg_gain[14] = mean(gain[1..14])          # seed
    avg_loss[14] = mean(loss[1..14])          # seed
    avg_gain[t] = (avg_gain[t-1] * 13 + gain[t]) / 14     # t > 14
    avg_loss[t] = (avg_loss[t-1] * 13 + loss[t]) / 14     # t > 14
    RS = avg_gain[t] / avg_loss[t]
    RSI[t] = 100 - (100 / (1 + RS))
    """
    if isinstance(prices, list):
        series = pd.Series(prices, dtype=float)
    else:
        series = prices.astype(float).reset_index(drop=True)

    if len(series) < period + 1:
        raise ValueError(f"Need at least {period + 1} price points to compute RSI({period}), got {len(series)}")

    deltas = series.diff().dropna().values
    gains = np.maximum(deltas, 0.0)
    losses = np.maximum(-deltas, 0.0)

    # Initial seed (first 14 changes)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    # Wilder's smoothing for subsequent periods
    for t in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[t]) / period
        avg_loss = (avg_loss * (period - 1) + losses[t]) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return float(rsi)


def compute_macd(
    prices: Union[pd.Series, List[float]],
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> Dict[str, float]:
    """
    Computes MACD(12, 26, 9) as specified in §9:
    EMA[t, N] = close[t] * k + EMA[t-1, N] * (1 - k),   k = 2 / (N + 1)
    EMA[N, N] = mean(close[1..N])   # seed with SMA

    macd_line[t]   = EMA[t, 12] - EMA[t, 26]
    signal_line[t] = EMA(macd_line, 9)[t]
    histogram[t]   = macd_line[t] - signal_line[t]
    """
    if isinstance(prices, list):
        series = pd.Series(prices, dtype=float)
    else:
        series = prices.astype(float).reset_index(drop=True)

    min_required = slow + signal_period
    if len(series) < min_required:
        raise ValueError(f"Need at least {min_required} price points to compute MACD({fast},{slow},{signal_period}), got {len(series)}")

    values = series.values

    def calculate_ema(arr: np.ndarray, n: int) -> np.ndarray:
        ema = np.zeros(len(arr))
        k = 2.0 / (n + 1)
        ema[n - 1] = np.mean(arr[:n])
        for i in range(n, len(arr)):
            ema[i] = arr[i] * k + ema[i - 1] * (1.0 - k)
        return ema

    ema_fast = calculate_ema(values, fast)
    ema_slow = calculate_ema(values, slow)

    # Valid range for macd line starts from slow - 1
    macd_full = ema_fast - ema_slow
    macd_valid = macd_full[slow - 1 :]

    # Signal line is 9-EMA of valid macd_line
    signal_ema_valid = calculate_ema(macd_valid, signal_period)

    latest_macd = macd_valid[-1]
    latest_signal = signal_ema_valid[-1]
    latest_hist = latest_macd - latest_signal

    return {
        "macd_line": float(latest_macd),
        "macd_signal": float(latest_signal),
        "macd_histogram": float(latest_hist),
    }


def compute_signals(close_prices: List[float]) -> Dict[str, Any]:
    """Computes RSI(14), MACD(12,26,9), and latest price from historical close prices."""
    if not close_prices:
        raise ValueError("Close prices cannot be empty.")

    rsi = compute_rsi(close_prices, 14)
    macd = compute_macd(close_prices, 12, 26, 9)
    latest_price = float(close_prices[-1])

    return {
        "rsi": round(rsi, 2),
        "macd_line": round(macd["macd_line"], 4),
        "macd_signal": round(macd["macd_signal"], 4),
        "macd_histogram": round(macd["macd_histogram"], 4),
        "latest_price": round(latest_price, 2),
    }
