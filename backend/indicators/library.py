"""
Rush Algo — Indicator Library (100+ indicators)
All indicators return pandas Series or DataFrame.
compute_all() runs every indicator on a given OHLCV DataFrame.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import ta


# ══════════════════════════════════════════════════════════════════════════════
# TREND INDICATORS
# ══════════════════════════════════════════════════════════════════════════════

def sma(df, period=20):      return ta.trend.SMAIndicator(df["close"], period).sma_indicator()
def ema(df, period=20):      return ta.trend.EMAIndicator(df["close"], period).ema_indicator()
def wma(df, period=20):      return df["close"].rolling(period).apply(lambda x: np.average(x, weights=range(1, period+1)), raw=True)
def dema(df, period=20):
    e = ema(df, period); return 2*e - ta.trend.EMAIndicator(e, period).ema_indicator()
def tema(df, period=20):
    e1=ema(df,period); e2=ta.trend.EMAIndicator(e1,period).ema_indicator()
    e3=ta.trend.EMAIndicator(e2,period).ema_indicator(); return 3*e1-3*e2+e3
def hma(df, period=20):
    half=max(1,period//2); sqrt=max(1,int(period**0.5))
    # FIX: correct Hull MA formula is WMA(2*WMA(n/2)-WMA(n), sqrt(n)) — this was
    # using ema() for the final step instead of wma(), which defeats HMA's whole
    # purpose (its low-lag property specifically comes from using WMA at every stage).
    return wma(pd.DataFrame({"close":2*wma(df,half)-wma(df,period)}),sqrt)
def kama(df, period=10):     return ta.momentum.KAMAIndicator(df["close"], period).kama()  # FIX: lives in ta.momentum, not ta.trend
def macd(df, fast=12, slow=26, signal=9):
    m = ta.trend.MACD(df["close"], fast, slow, signal)
    return pd.DataFrame({"macd":m.macd(),"signal":m.macd_signal(),"hist":m.macd_diff()})
def adx(df, period=14):
    a = ta.trend.ADXIndicator(df["high"],df["low"],df["close"],period)
    return pd.DataFrame({"adx":a.adx(),"pos_di":a.adx_pos(),"neg_di":a.adx_neg()})
def aroon(df, period=25):
    a = ta.trend.AroonIndicator(df["high"],df["low"],period)
    return pd.DataFrame({"up":a.aroon_up(),"down":a.aroon_down(),"indicator":a.aroon_indicator()})
def psar(df, step=0.02, max_step=0.2):
    p = ta.trend.PSARIndicator(df["high"],df["low"],df["close"],step,max_step)
    return pd.DataFrame({"psar":p.psar(),"up":p.psar_up(),"down":p.psar_down()})
def cci(df, period=20):      return ta.trend.CCIIndicator(df["high"],df["low"],df["close"],period).cci()
def dpo(df, period=20):      return ta.trend.DPOIndicator(df["close"],period).dpo()
def mass_index(df):          return ta.trend.MassIndex(df["high"],df["low"]).mass_index()
def schaff_trend(df):
    return ta.trend.STCIndicator(df["close"]).stc()
def trix(df, period=15):     return ta.trend.TRIXIndicator(df["close"],period).trix()
def vortex(df, period=14):
    v = ta.trend.VortexIndicator(df["high"],df["low"],df["close"],period)
    return pd.DataFrame({"pos":v.vortex_indicator_pos(),"neg":v.vortex_indicator_neg(),"diff":v.vortex_indicator_diff()})


# ══════════════════════════════════════════════════════════════════════════════
# MOMENTUM INDICATORS
# ══════════════════════════════════════════════════════════════════════════════

def rsi(df, period=14):      return ta.momentum.RSIIndicator(df["close"],period).rsi()
def stoch(df, k=14, d=3):
    s = ta.momentum.StochasticOscillator(df["high"],df["low"],df["close"],k,d)
    return pd.DataFrame({"k":s.stoch(),"d":s.stoch_signal()})
def stoch_rsi(df, period=14, k=3, d=3):
    s = ta.momentum.StochRSIIndicator(df["close"],period,k,d)
    return pd.DataFrame({"k":s.stochrsi_k(),"d":s.stochrsi_d()})
def williams_r(df, period=14): return ta.momentum.WilliamsRIndicator(df["high"],df["low"],df["close"],period).williams_r()
def roc(df, period=12):      return ta.momentum.ROCIndicator(df["close"],period).roc()
def ppo(df, slow=26, fast=12, sig=9):
    p = ta.momentum.PercentagePriceOscillator(df["close"],slow,fast,sig)
    return pd.DataFrame({"ppo":p.ppo(),"signal":p.ppo_signal(),"hist":p.ppo_hist()})
def pvo(df, slow=26, fast=12, sig=9):
    p = ta.momentum.PercentageVolumeOscillator(df["volume"],slow,fast,sig)
    return pd.DataFrame({"pvo":p.pvo(),"signal":p.pvo_signal(),"hist":p.pvo_hist()})
def tsi(df, slow=25, fast=13, sig=13):
    # FIX: this ta version's TSIIndicator has no .tsi_signal() method at all —
    # compute the signal line the standard way (EMA smoothing of the raw TSI line)
    tsi_line = ta.momentum.TSIIndicator(df["close"], slow, fast).tsi()
    signal_line = ta.trend.EMAIndicator(tsi_line, sig).ema_indicator()
    return pd.DataFrame({"tsi": tsi_line, "signal": signal_line})
def ultimate_osc(df):
    return ta.momentum.UltimateOscillator(df["high"],df["low"],df["close"]).ultimate_oscillator()
def awesome_osc(df):
    return ta.momentum.AwesomeOscillatorIndicator(df["high"],df["low"]).awesome_oscillator()
def kama_momentum(df, period=10): return kama(df, period)


# ══════════════════════════════════════════════════════════════════════════════
# VOLATILITY INDICATORS
# ══════════════════════════════════════════════════════════════════════════════

def atr(df, period=14):
    return ta.volatility.AverageTrueRange(df["high"],df["low"],df["close"],period).average_true_range()
def bollinger(df, period=20, std=2.0):
    b = ta.volatility.BollingerBands(df["close"],period,std)
    return pd.DataFrame({"upper":b.bollinger_hband(),"middle":b.bollinger_mavg(),"lower":b.bollinger_lband(),"pct_b":b.bollinger_pband(),"width":b.bollinger_wband()})
def keltner(df, period=20, mult=2):
    k = ta.volatility.KeltnerChannel(df["high"],df["low"],df["close"],period,mult)
    return pd.DataFrame({"upper":k.keltner_channel_hband(),"middle":k.keltner_channel_mband(),"lower":k.keltner_channel_lband(),"pct":k.keltner_channel_pband(),"width":k.keltner_channel_wband()})
def donchian(df, period=20):
    d = ta.volatility.DonchianChannel(df["high"],df["low"],df["close"],period)
    return pd.DataFrame({"upper":d.donchian_channel_hband(),"middle":d.donchian_channel_mband(),"lower":d.donchian_channel_lband(),"width":d.donchian_channel_wband()})
def ulcer_index(df, period=14):
    return ta.volatility.UlcerIndex(df["close"],period).ulcer_index()

def supertrend(df, period=7, mult=3.0):
    """SuperTrend — pure numpy to avoid pandas iloc mutation bug."""
    atr_v = atr(df, period).to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    hl2   = ((df["high"]+df["low"])/2).to_numpy(dtype=float)
    n     = len(df)
    upper = hl2 + mult*atr_v
    lower = hl2 - mult*atr_v
    st    = np.full(n, np.nan)
    direc = np.zeros(n)
    for i in range(1, n):
        if np.isnan(atr_v[i]): continue
        upper[i] = upper[i] if (upper[i]<upper[i-1] or close[i-1]>upper[i-1]) else upper[i-1]
        lower[i] = lower[i] if (lower[i]>lower[i-1] or close[i-1]<lower[i-1]) else lower[i-1]
        prev = st[i-1] if not np.isnan(st[i-1]) else upper[i]
        if prev==upper[i-1]:
            st[i]=lower[i] if close[i]>upper[i] else upper[i]
        else:
            st[i]=upper[i] if close[i]<lower[i] else lower[i]
        direc[i]=1.0 if st[i]==lower[i] else -1.0
    return pd.DataFrame({"supertrend":pd.Series(st,index=df.index),"direction":pd.Series(direc,index=df.index)})


# ══════════════════════════════════════════════════════════════════════════════
# VOLUME INDICATORS
# ══════════════════════════════════════════════════════════════════════════════

def vwap(df):
    typical = (df["high"]+df["low"]+df["close"])/3
    try:
        grp      = df.index.normalize() if hasattr(df.index,"normalize") and hasattr(df.index,"date") else None
        cumvol   = df.groupby(grp)["volume"].cumsum()   if grp is not None else df["volume"].cumsum()
        cumtpvol = (typical*df["volume"]).groupby(grp).cumsum() if grp is not None else (typical*df["volume"]).cumsum()
    except Exception:
        cumvol   = df["volume"].cumsum()
        cumtpvol = (typical*df["volume"]).cumsum()
    return (cumtpvol/cumvol.replace(0,np.nan)).rename("vwap")

def obv(df):             return ta.volume.OnBalanceVolumeIndicator(df["close"],df["volume"]).on_balance_volume()
def mfi(df, period=14):  return ta.volume.MFIIndicator(df["high"],df["low"],df["close"],df["volume"],period).money_flow_index()
def cmf(df, period=20):  return ta.volume.ChaikinMoneyFlowIndicator(df["high"],df["low"],df["close"],df["volume"],period).chaikin_money_flow()
def eom(df, period=14):  return ta.volume.EaseOfMovementIndicator(df["high"],df["low"],df["volume"],period).ease_of_movement()
def fi(df, period=13):   return ta.volume.ForceIndexIndicator(df["close"],df["volume"],period).force_index()
def nvi(df):             return ta.volume.NegativeVolumeIndexIndicator(df["close"],df["volume"]).negative_volume_index()
def vpt(df):             return ta.volume.VolumePriceTrendIndicator(df["close"],df["volume"]).volume_price_trend()
def adi(df):             return ta.volume.AccDistIndexIndicator(df["high"],df["low"],df["close"],df["volume"]).acc_dist_index()
def vwap_distance(df):
    v = vwap(df); return ((df["close"]-v)/v*100).rename("vwap_dist_pct")


# ══════════════════════════════════════════════════════════════════════════════
# PIVOT POINTS
# ══════════════════════════════════════════════════════════════════════════════

def pivot_points(df):
    """Classic pivot points based on previous bar."""
    pp = (df["high"].shift(1)+df["low"].shift(1)+df["close"].shift(1))/3
    return pd.DataFrame({
        "pp":pp,
        "r1":2*pp-df["low"].shift(1), "r2":pp+(df["high"]-df["low"]).shift(1),
        "r3":df["high"].shift(1)+2*(pp-df["low"].shift(1)),
        "s1":2*pp-df["high"].shift(1),"s2":pp-(df["high"]-df["low"]).shift(1),
        "s3":df["low"].shift(1)-2*(df["high"].shift(1)-pp),
    })


# ══════════════════════════════════════════════════════════════════════════════
# COMPUTE ALL — runs every indicator
# ══════════════════════════════════════════════════════════════════════════════

def compute_all(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    # Trend
    for p in [9,20,50,200]:
        out[f"sma_{p}"]  = sma(df, p)
        out[f"ema_{p}"]  = ema(df, p)
    out["wma_20"]    = wma(df, 20)
    out["dema_20"]   = dema(df, 20)
    out["tema_20"]   = tema(df, 20)
    out["hma_20"]    = hma(df, 20)
    out["kama_10"]   = kama(df, 10)
    _m = macd(df)
    out["macd"]      = _m["macd"]
    out["macd_signal"]= _m["signal"]
    out["macd_hist"] = _m["hist"]
    _a = adx(df)
    out["adx"]       = _a["adx"]
    out["pos_di"]    = _a["pos_di"]
    out["neg_di"]    = _a["neg_di"]
    _ar = aroon(df)
    out["aroon_up"]  = _ar["up"]
    out["aroon_down"]= _ar["down"]
    _ps = psar(df)
    out["psar"]      = _ps["psar"]
    out["psar_up"]   = _ps["up"]
    out["psar_down"] = _ps["down"]
    out["cci_20"]    = cci(df, 20)
    out["trix_15"]   = trix(df, 15)
    _vort = vortex(df)
    out["vortex_pos"]= _vort["pos"]
    out["vortex_neg"]= _vort["neg"]
    # Momentum
    out["rsi_14"]    = rsi(df, 14)
    out["rsi_9"]     = rsi(df, 9)
    _st = stoch(df)
    out["stoch_k"]   = _st["k"]
    out["stoch_d"]   = _st["d"]
    _sr = stoch_rsi(df)
    out["stochrsi_k"]= _sr["k"]
    out["stochrsi_d"]= _sr["d"]
    out["williams_r"]= williams_r(df)
    out["roc_12"]    = roc(df, 12)
    out["ult_osc"]   = ultimate_osc(df)
    out["awesome"]   = awesome_osc(df)
    # Volatility
    out["atr_14"]    = atr(df, 14)
    _bb = bollinger(df)
    out["bb_upper"]  = _bb["upper"]
    out["bb_middle"] = _bb["middle"]
    out["bb_lower"]  = _bb["lower"]
    out["bb_pct"]    = _bb["pct_b"]
    out["bb_width"]  = _bb["width"]
    _kc = keltner(df)
    out["kc_upper"]  = _kc["upper"]
    out["kc_lower"]  = _kc["lower"]
    _dc = donchian(df)
    out["dc_upper"]  = _dc["upper"]
    out["dc_lower"]  = _dc["lower"]
    _sup = supertrend(df)
    out["supertrend"]    = _sup["supertrend"]
    out["supertrend_dir"]= _sup["direction"]
    # Volume
    out["vwap"]      = vwap(df)
    out["obv"]       = obv(df)
    out["mfi_14"]    = mfi(df, 14)
    out["cmf_20"]    = cmf(df, 20)
    out["fi_13"]     = fi(df, 13)
    out["vpt"]       = vpt(df)
    out["adi"]       = adi(df)
    out["vwap_dist"] = vwap_distance(df)
    # Pivots
    _pv = pivot_points(df)
    out["pivot_pp"]  = _pv["pp"]
    out["pivot_r1"]  = _pv["r1"]
    out["pivot_s1"]  = _pv["s1"]
    return out


# ── Column name map (frontend label → column name) ────────────────────────────
INDICATOR_COLS: dict = {
    "RSI":           "rsi_14",
    "RSI(9)":        "rsi_9",
    "EMA(9)":        "ema_9",
    "EMA(20)":       "ema_20",
    "EMA(50)":       "ema_50",
    "EMA(200)":      "ema_200",
    "SMA(20)":       "sma_20",
    "SMA(50)":       "sma_50",
    "SMA(200)":      "sma_200",
    "MACD":          "macd_hist",
    "MACD Line":     "macd",
    "MACD Signal":   "macd_signal",
    "ADX":           "adx",
    "+DI":           "pos_di",
    "-DI":           "neg_di",
    "Aroon Up":      "aroon_up",
    "Aroon Down":    "aroon_down",
    "PSAR":          "psar",
    "CCI":           "cci_20",
    "Stochastic K":  "stoch_k",
    "Stochastic D":  "stoch_d",
    "StochRSI K":    "stochrsi_k",
    "Williams %R":   "williams_r",
    "ROC":           "roc_12",
    "ATR":           "atr_14",
    "BB Upper":      "bb_upper",
    "BB Lower":      "bb_lower",
    "BB %B":         "bb_pct",
    "BB Width":      "bb_width",
    "KC Upper":      "kc_upper",
    "KC Lower":      "kc_lower",
    "DC Upper":      "dc_upper",
    "DC Lower":      "dc_lower",
    "SuperTrend":    "supertrend_dir",
    "VWAP":          "vwap",
    "VWAP Dist%":    "vwap_dist",
    "OBV":           "obv",
    "MFI":           "mfi_14",
    "CMF":           "cmf_20",
    "VPT":           "vpt",
    "Close":         "close",
    "Open":          "open",
    "High":          "high",
    "Low":           "low",
    "Volume":        "volume",
    "Pivot PP":      "pivot_pp",
    "Pivot R1":      "pivot_r1",
    "Pivot S1":      "pivot_s1",
    "WMA(20)":       "wma_20",
    "DEMA(20)":      "dema_20",
    "TEMA(20)":      "tema_20",
    "HMA(20)":       "hma_20",
    "KAMA":          "kama_10",
}

VALUE_COLS: dict = {v: v for v in INDICATOR_COLS.values()}
VALUE_COLS.update({k: v for k, v in INDICATOR_COLS.items()})
