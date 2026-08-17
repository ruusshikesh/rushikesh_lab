"""Rush Algo — Pydantic schemas"""
from __future__ import annotations
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class Broker(str, Enum):
    paper   = "paper"
    fyers   = "fyers"
    zerodha = "zerodha"
    dhan    = "dhan"


class Signal(str, Enum):
    BUY   = "BUY"
    SELL  = "SELL"
    WATCH = "WATCH"


class OrderStatus(str, Enum):
    PENDING  = "PENDING"
    OPEN     = "OPEN"
    CLOSED   = "CLOSED"
    REJECTED = "REJECTED"


class TradeType(str, Enum):
    intraday   = "INTRADAY"
    positional = "POSITIONAL"


class Timeframe(str, Enum):
    m1  = "1min"
    m3  = "3min"
    m5  = "5min"
    m15 = "15min"
    m30 = "30min"
    h1  = "1hr"
    d1  = "1day"


class Comparator(str, Enum):
    greater_than  = "greater_than"
    less_than     = "less_than"
    crosses_above = "crosses_above"
    crosses_below = "crosses_below"
    equals        = "equals"


class Condition(BaseModel):
    indicator:  str
    comparator: Comparator
    value:      str
    join:       str            = "AND"
    params:     Dict[str, Any] = Field(default_factory=dict)


class RiskConfig(BaseModel):
    sl_pct:          float = 4.0
    target1_pct:     float = 4.0
    target2_pct:     float = 8.0
    trailing_sl_pct: float = 3.0
    partial_book_pct:float = 50.0
    trade_amount:    float = 30_000.0
    max_positions:   int   = 30


class MTFConfig(BaseModel):
    """Multi-timeframe confirmation settings"""
    enabled:       bool        = True
    primary_tf:    Timeframe   = Timeframe.m5
    confirm_tfs:   List[str]   = Field(default_factory=lambda: ["15min", "30min", "1hr"])
    require_all:   bool        = False   # False = majority confirmation


class Strategy(BaseModel):
    id:               Optional[str]      = None
    name:             str
    description:      str                = ""
    trade_type:       TradeType          = TradeType.intraday
    primary_tf:       Timeframe          = Timeframe.m5
    symbol:           str                = "RELIANCE"   # default symbol for signal + backtest
    watchlist:        List[str]          = Field(default_factory=list)  # symbols to scan
    entry_conditions: List[Condition]
    risk:             RiskConfig         = Field(default_factory=RiskConfig)
    mtf:              MTFConfig          = Field(default_factory=MTFConfig)
    broker:           Broker             = Broker.paper
    paper_mode:       bool               = True
    algo_id:          Optional[str]      = None
    enabled:          bool               = True
    tags:             List[str]          = Field(default_factory=list)
    created_at:       datetime           = Field(
        default_factory=lambda: datetime.now(timezone.utc))


class Order(BaseModel):
    id:              Optional[str]      = None
    symbol:          str
    side:            str                = "BUY"
    qty:             int
    order_type:      str                = "LIMIT"
    price:           float
    stop_loss:       float
    target1:         float
    target2:         float
    status:          OrderStatus        = OrderStatus.PENDING
    broker:          Broker             = Broker.paper
    broker_order_id: Optional[str]      = None
    exit_broker_order_id: Optional[str] = None   # broker id of the exit/close order
    strategy_id:     str                = ""
    strategy_name:   str                = ""
    algo_id:         Optional[str]      = None
    trade_type:      TradeType          = TradeType.intraday
    entry_time:      Optional[datetime] = None
    exit_time:       Optional[datetime] = None
    exit_price:      Optional[float]    = None
    partial_booked:  bool               = False
    exit_retry_count: int               = 0     # incremented when a broker exit fails
    pnl:             Optional[float]    = None
    exit_reason:     Optional[str]      = None


class Deployment(BaseModel):
    id:          str
    strategy:    Strategy
    broker:      Broker
    paper_mode:  bool               = True
    status:      str                = "LIVE"
    deployed_at: datetime           = Field(
        default_factory=lambda: datetime.now(timezone.utc))
    today_pnl:   float              = 0.0
    total_pnl:   float              = 0.0
    trade_count: int                = 0
    open_orders: List[Order]        = Field(default_factory=list)
    closed_trades: List[Order]      = Field(default_factory=list)  # FIX: EOD report reads from here
    blocked_symbols: List[str]      = Field(default_factory=list)  # no re-entry list


class FundamentalData(BaseModel):
    symbol:           str
    name:             str
    market_cap_cr:    float
    pe_ratio:         Optional[float] = None
    roe:              Optional[float] = None
    debt_to_equity:   Optional[float] = None
    promoter_holding: Optional[float] = None
    revenue_growth:   Optional[float] = None
    profit_growth:    Optional[float] = None
    current_ratio:    Optional[float] = None
    # Absolute figures (₹ crore) — extracted from IndianAPI's financials/keyMetrics.
    # These are what let us tell a real business (ATLANTAELE, ₹1851 Cr revenue) from
    # a dormant shell (MMTC, ₹3.41 Cr revenue) — something ratios alone cannot do.
    revenue_cr:       Optional[float] = None   # TTM / latest annual operating revenue
    net_income_cr:    Optional[float] = None   # TTM / latest annual net income
    operating_income_cr: Optional[float] = None
    fcf_cr:           Optional[float] = None   # free cash flow TTM (owner-earnings proxy)
    # Self-computed growth from the real yearly series (more trustworthy than the
    # single API growth % which can be distorted by base effects / one-offs).
    revenue_growth_calc: Optional[float] = None
    profit_growth_calc:  Optional[float] = None
    score:            float           = 0.0
    last_updated:     Optional[str]   = None


class BacktestRequest(BaseModel):
    strategy:        Strategy
    symbol:          str
    start_date:      str
    end_date:        str
    initial_capital: float   = 1_000_000.0


class TradeRecord(BaseModel):
    entry_date:   str
    exit_date:    str
    entry_price:  float
    exit_price:   float
    qty:          int
    side:         str
    pnl:          float
    pnl_pct:      float
    exit_reason:  str


class BacktestResult(BaseModel):
    symbol:            str
    strategy_name:     str
    start_date:        str
    end_date:          str
    initial_capital:   float
    final_capital:     float
    total_return_pct:  float
    cagr_pct:          float
    max_drawdown_pct:  float
    win_rate_pct:      float
    total_trades:      int
    winning_trades:    int
    losing_trades:     int
    sharpe_ratio:      float
    profit_factor:     float
    avg_trade_pct:     float
    best_trade_pct:    float
    worst_trade_pct:   float
    score:             float
    score_grade:       str
    equity_curve:      List[Dict]
    trades:            List[TradeRecord]


class ApiResponse(BaseModel):
    success: bool
    message: str
    data:    Optional[Any] = None
