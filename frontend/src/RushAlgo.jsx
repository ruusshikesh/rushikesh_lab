import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import {
  LineChart, Line, BarChart, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, ReferenceLine, Cell,
  PieChart, Pie, Legend,
} from "recharts";

// ─── Design tokens ────────────────────────────────────────────────────────────
// ── THEMES ────────────────────────────────────────────────────────────────────
// Every colour in the app flows through the single object `C`, so a theme is
// just a different set of values for the same keys - no component needs to know
// which theme is active. `C` is mutated in place (rather than reassigned) so the
// 400+ existing `C.xxx` references keep working untouched.
//
// NOTE: `C.panel` is used throughout the UI but was never defined in the
// original palette, so it rendered as `undefined` (falling back to transparent).
// It's defined in every theme below - a small latent bug fixed along the way.
const THEMES = {
  dark: {
    label: "Dark",
    bg:"#06060F", surface:"#0D0D1C", card:"#12122A", panel:"#0D0D1C", border:"#1E1E3A",
    indigo:"#6366F1", cyan:"#22D3EE", green:"#10B981", red:"#F43F5E",
    amber:"#F59E0B", purple:"#A855F7", text:"#E2E8F0", muted:"#6B7280", dim:"#374151",
  },
  dim: {
    label: "Lighter Dark",
    // Same terminal feel, less black: raised surfaces, softer borders and a
    // brighter muted tone so secondary text stays readable.
    bg:"#161B26", surface:"#1E2533", card:"#232B3B", panel:"#1E2533", border:"#323B4D",
    indigo:"#818CF8", cyan:"#38BDF8", green:"#34D399", red:"#FB7185",
    amber:"#FBBF24", purple:"#C084FC", text:"#E8EDF5", muted:"#94A3B8", dim:"#4B5563",
  },
  light: {
    label: "Light",
    // Accents are DARKENED here, not reused from the dark theme: the bright
    // neons that read well on near-black fail contrast on white.
    bg:"#F5F7FA", surface:"#FFFFFF", card:"#FFFFFF", panel:"#FFFFFF", border:"#DDE3EC",
    indigo:"#4F46E5", cyan:"#0891B2", green:"#059669", red:"#E11D48",
    amber:"#D97706", purple:"#7E22CE", text:"#111827", muted:"#64748B", dim:"#CBD5E1",
  },
};

// Single font stack for the app, mirroring the --font-mono CSS variable set in
// index.html. Kept as a JS constant too because inline styles can't read CSS
// variables directly. The system-monospace fallbacks mean the UI still looks
// like a terminal if Google Fonts is unreachable (offline / blocked network).
const MONO = '"JetBrains Mono", "SF Mono", "Cascadia Mono", Consolas, ui-monospace, monospace';

const THEME_KEY = "rushalgo_theme";
const C = { ...THEMES.dark };

function applyTheme(name) {
  const t = THEMES[name] || THEMES.dark;
  Object.keys(t).forEach(k => { if (k !== "label") C[k] = t[k]; });
  try {
    document.body.style.background = C.bg;
    document.body.style.color = C.text;
  } catch (e) {}
}

// Restore the saved choice before first paint so there's no flash of the wrong
// theme. Wrapped because storage can throw in private-browsing modes.
let _savedTheme = "dark";
try {
  const s = window.localStorage.getItem(THEME_KEY);
  if (s && THEMES[s]) _savedTheme = s;
} catch (e) {}
applyTheme(_savedTheme);
const API = "http://localhost:8000";

// Live prices via a shared WebSocket push (no HTTP polling). One connection to
// /ws; we send our on-screen symbols as {type:"watch"} and the backend pushes
// prices the instant they change. Multiple components share the same socket.
const _liveBus = (() => {
  let ws = null, ready = false;
  const prices = {};                 // SYMBOL -> quote
  const listeners = new Set();       // callbacks to notify on update
  const wanted = new Map();          // SYMBOL -> refcount (union of all components)
  let sendTimer = null;

  const wsUrl = () => API.replace(/^http/, "ws") + "/ws";

  const pushWatch = () => {
    if (!ready) return;
    try { ws.send(JSON.stringify({ type: "watch", client: "live-bus",
            symbols: Array.from(wanted.keys()) })); }
    catch (e) {}
  };
  const scheduleWatch = () => { clearTimeout(sendTimer); sendTimer = setTimeout(pushWatch, 120); };

  const connect = () => {
    try {
      ws = new WebSocket(wsUrl());
      ws.onopen = () => { ready = true; pushWatch(); };
      ws.onmessage = (ev) => {
        try {
          const m = JSON.parse(ev.data);
          if (m.type === "prices" && m.data) {
            Object.assign(prices, m.data);
            listeners.forEach(fn => fn(prices));
          }
        } catch (e) {}
      };
      ws.onclose = () => { ready = false; setTimeout(connect, 1500); };  // auto-reconnect
      ws.onerror = () => { try { ws.close(); } catch (e) {} };
    } catch (e) { setTimeout(connect, 2000); }
  };

  return {
    subscribe(symbols, cb) {
      for (const s of symbols) wanted.set(s, (wanted.get(s) || 0) + 1);
      listeners.add(cb);
      if (!ws) connect(); else scheduleWatch();
      cb(prices);
      return () => {
        listeners.delete(cb);
        for (const s of symbols) {
          const n = (wanted.get(s) || 1) - 1;
          if (n <= 0) wanted.delete(s); else wanted.set(s, n);
        }
        scheduleWatch();
      };
    },
    snapshot() { return prices; },
  };
})();

function useLivePrices(symbols) {
  const [prices, setPrices] = useState(_liveBus.snapshot());
  const symKey = useMemo(() => (symbols || []).slice().sort().join(","), [symbols]);
  useEffect(() => {
    const list = symKey ? symKey.split(",") : [];
    if (list.length === 0) return;
    const unsub = _liveBus.subscribe(list, (p) => setPrices({ ...p }));
    return unsub;
  }, [symKey]);
  return prices;
}

// ─── API layer ────────────────────────────────────────────────────────────────
// FIX: lastApiError exposes the backend's actual error detail. Previously,
// `api()` threw on any non-2xx response BEFORE reading the JSON body, so
// FastAPI's helpful detail messages (e.g. "Not enough data: 50 bars, need
// >200...") were silently discarded and every caller only ever saw a generic
// fallback message. The return contract (data on success, null on failure)
// is unchanged so no existing call site needs to be rewritten.
let lastApiError = "";
async function api(path, opts = {}) {
  const ctrl = new AbortController();
  // Timeout is configurable per call via opts.timeoutMs. Default 8s keeps fast
  // calls (strategies, quotes, health) snappy. Long operations like backtests —
  // which may fetch many 100-day data chunks from the broker — pass a larger value.
  const timeoutMs = opts.timeoutMs || 8000;
  const { timeoutMs: _omit, ...fetchOpts } = opts;   // don't pass custom field to fetch
  const tid  = setTimeout(() => ctrl.abort(), timeoutMs);
  lastApiError = "";
  try {
    const r = await fetch(API + path, {
      headers: { "Content-Type": "application/json" },
      ...fetchOpts,
      signal: ctrl.signal,
    });
    clearTimeout(tid);
    let j = null;
    try { j = await r.json(); } catch { /* non-JSON or empty body */ }
    if (!r.ok) {
      lastApiError = j?.detail || j?.message || `HTTP ${r.status}`;
      return null;
    }
    return j?.data ?? j;
  } catch (err) {
    clearTimeout(tid);
    lastApiError = err?.name === "AbortError"
      ? "Request timed out — backend may be slow or unreachable"
      : (err?.message || "Network error — is the backend running?");
    return null;
  }
}

// ─── Static data ──────────────────────────────────────────────────────────────
const ALL_INDICATORS = [
  "RSI","RSI(9)","EMA(9)","EMA(20)","EMA(50)","EMA(200)",
  "SMA(20)","SMA(50)","SMA(200)","MACD","MACD Line","MACD Signal",
  "ADX","+DI","-DI","Aroon Up","Aroon Down","PSAR","CCI",
  "Stochastic K","Stochastic D","StochRSI K","Williams %R","ROC",
  "ATR","BB Upper","BB Lower","BB %B","BB Width",
  "KC Upper","KC Lower","DC Upper","DC Lower","SuperTrend","VWAP",
  "VWAP Dist%","OBV","MFI","CMF","VPT","Close","Open","High","Low",
  "Volume","Pivot PP","Pivot R1","Pivot S1","WMA(20)","DEMA(20)","TEMA(20)","HMA(20)","KAMA",
];
const COMPARATORS = ["greater_than","less_than","crosses_above","crosses_below","equals"];
const COMP_LABELS = { greater_than:"Greater than", less_than:"Less than",
  crosses_above:"Crosses above", crosses_below:"Crosses below", equals:"Equals" };
const TIMEFRAMES  = ["1min","3min","5min","15min","30min","1hr","1day"];
const CONFIRM_TFS = ["5min","15min","30min","1hr"];
const BROKERS     = { paper:"📋 Paper", fyers:"🟠 Fyers", zerodha:"🟢 Zerodha", dhan:"🔵 Dhan" };

// ─── UI atoms ─────────────────────────────────────────────────────────────────
const sel = { background:C.surface, border:`1px solid ${C.border}`, borderRadius:6,
              color:C.text, padding:"6px 10px", fontSize:12, cursor:"pointer", outline:"none" };
const Badge = ({ label, color=C.indigo }) => (
  <span style={{ background:color+"22", color, border:`1px solid ${color}44`,
    borderRadius:4, padding:"2px 7px", fontSize:10, fontWeight:700, whiteSpace:"nowrap" }}>{label}</span>
);
const Panel = ({ children, style={} }) => (
  <div style={{ background:C.card, border:`1px solid ${C.border}`, borderRadius:12,
    padding:16, ...style }}>{children}</div>
);
const StatBox = ({ label, value, sub="", color=C.text }) => (
  <div style={{ background:C.card, border:`1px solid ${C.border}`, borderRadius:10, padding:"11px 14px" }}>
    <div style={{ fontSize:9, color:C.muted, textTransform:"uppercase", letterSpacing:1 }}>{label}</div>
    <div style={{ fontSize:17, fontWeight:800, color, fontFamily:MONO, marginTop:4 }}>{value}</div>
    {sub && <div style={{ fontSize:10, color:C.muted, marginTop:2 }}>{sub}</div>}
  </div>
);
const Btn = ({ onClick, children, color=C.indigo, disabled=false, style={} }) => (
  <button onClick={onClick} disabled={disabled} style={{
    background: disabled ? C.dim : color, color:"#fff", border:"none",
    borderRadius:8, padding:"8px 16px", cursor:disabled?"not-allowed":"pointer",
    fontSize:12, fontWeight:700, ...style }}>
    {children}
  </button>
);
const SLabel = ({ children }) => (
  <div style={{ fontSize:10, color:C.muted, textTransform:"uppercase",
    letterSpacing:1, marginBottom:5 }}>{children}</div>
);

// ─── Strategy Builder ─────────────────────────────────────────────────────────
function StrategyBuilder({ onSave, strategies = [], onDelete }) {
  const [confirmDelete, setConfirmDelete] = useState(null);   // strategy pending delete confirmation
  const [editingId, setEditingId] = useState(null);           // id of strategy being edited (null = creating new)
  const [name,       setName]     = useState("My Strategy");
  const [symbol,     setSymbol]   = useState("RELIANCE");   // FIX: was missing entirely
  const [tradeType,  setType]     = useState("INTRADAY");
  const [primaryTf,  setTf]       = useState("5min");
  const [broker,     setBroker]   = useState("paper");
  const [paperMode,  setPaper]    = useState(true);
  const [conds,      setConds]    = useState([
    { id:1, indicator:"RSI", comparator:"less_than", value:"30", join:"AND" },
  ]);
  const [slPct,      setSl]       = useState(4);
  const [t1Pct,      setT1]       = useState(4);
  const [t2Pct,      setT2]       = useState(8);
  const [trailPct,   setTrail]    = useState(3);
  const [tradeAmt,   setAmt]      = useState(30000);
  const [mtfEnabled, setMtf]      = useState(true);
  const [confirmTfs, setConfirm]  = useState(["15min","30min","1hr"]);
  const [saving,     setSaving]   = useState(false);
  const [saved,      setSaved]    = useState(false);

  const addCond  = () => setConds(p=>[...p,{id:Date.now(),indicator:"EMA(20)",comparator:"greater_than",value:"EMA(50)",join:"AND"}]);
  const remCond  = id => setConds(p=>p.filter(c=>c.id!==id));
  const updCond  = (id,k,v) => setConds(p=>p.map(c=>c.id===id?{...c,[k]:v}:c));
  const toggleTf = tf => setConfirm(p=>p.includes(tf)?p.filter(x=>x!==tf):[...p,tf]);
  const rr       = t1Pct > 0 ? (t1Pct / slPct).toFixed(1) : "0";

  // Pull an existing strategy's values into the form for editing.
  const loadForEdit = (s) => {
    setEditingId(s.id);
    setName(s.name || "My Strategy");
    setSymbol(s.symbol || "RELIANCE");
    setType(s.trade_type || "INTRADAY");
    setTf(s.primary_tf || "5min");
    setBroker(s.broker || "paper");
    setPaper(s.paper_mode !== false);
    setConds((s.entry_conditions || []).map((c, i) => ({
      id: Date.now() + i, indicator: c.indicator, comparator: c.comparator,
      value: c.value, join: c.join || "AND",
    })) || []);
    const r = s.risk || {};
    setSl(r.sl_pct ?? 4); setT1(r.target1_pct ?? 4); setT2(r.target2_pct ?? 8);
    setTrail(r.trailing_sl_pct ?? 3); setAmt(r.trade_amount ?? 30000);
    const m = s.mtf || {};
    setMtf(m.enabled ?? true);
    setConfirm(m.confirm_tfs || ["15min","30min","1hr"]);
    // scroll the form into view so the user sees the populated fields
    try { window.scrollTo({ top: 0, behavior: "smooth" }); } catch {}
  };

  // Clear the form back to "new strategy" mode.
  const resetForm = () => {
    setEditingId(null);
    setName("My Strategy"); setSymbol("RELIANCE"); setType("INTRADAY"); setTf("5min");
    setBroker("paper"); setPaper(true);
    setConds([{ id:1, indicator:"RSI", comparator:"less_than", value:"30", join:"AND" }]);
    setSl(4); setT1(4); setT2(8); setTrail(3); setAmt(30000);
    setMtf(true); setConfirm(["15min","30min","1hr"]);
  };

  const save = async () => {
    setSaving(true);
    const payload = {
      // Include id when editing so the backend UPDATES this strategy instead of
      // creating a duplicate (backend does: strategy.id = strategy.id or new_id).
      ...(editingId ? { id: editingId } : {}),
      name, symbol, trade_type: tradeType,
      primary_tf: primaryTf, broker, paper_mode: paperMode,
      entry_conditions: conds.map(c=>({ indicator:c.indicator, comparator:c.comparator,
        value:c.value, join:c.join })),
      risk: { sl_pct:slPct, target1_pct:t1Pct, target2_pct:t2Pct,
              trailing_sl_pct:trailPct, trade_amount:tradeAmt, max_positions:30 },
      mtf: { enabled:mtfEnabled, primary_tf:primaryTf,
             confirm_tfs:confirmTfs, require_all:false },
    };
    const res = await api("/api/strategies", { method:"POST", body:JSON.stringify(payload) });
    setSaving(false);
    if (res) { setSaved(true); setTimeout(()=>setSaved(false),2000); onSave?.(res); setEditingId(null); }
  };

  return (
    <div style={{ display:"flex", flexDirection:"column", gap:13 }}>

      {/* Meta */}
      <Panel>
        <div style={{ display:"grid", gridTemplateColumns:"repeat(auto-fit,minmax(140px,1fr))", gap:11 }}>
          <div><SLabel>Strategy Name</SLabel>
            <input value={name} onChange={e=>setName(e.target.value)}
              style={{ ...sel, width:"100%", boxSizing:"border-box" }} /></div>
          <div><SLabel>Symbol</SLabel>
            <input value={symbol} onChange={e=>setSymbol(e.target.value.toUpperCase())}
              placeholder="e.g. RELIANCE"
              style={{ ...sel, width:"100%", boxSizing:"border-box" }} /></div>
          <div><SLabel>Trade Type</SLabel>
            <select value={tradeType} onChange={e=>setType(e.target.value)} style={{...sel,width:"100%"}}>
              <option value="INTRADAY">Intraday</option>
              <option value="POSITIONAL">Positional</option>
            </select></div>
          <div><SLabel>Primary Timeframe</SLabel>
            <select value={primaryTf} onChange={e=>setTf(e.target.value)} style={{...sel,width:"100%"}}>
              {TIMEFRAMES.map(t=><option key={t}>{t}</option>)}
            </select></div>
          <div><SLabel>Broker</SLabel>
            <select value={broker} onChange={e=>setBroker(e.target.value)} style={{...sel,width:"100%"}}>
              {Object.entries(BROKERS).map(([k,v])=><option key={k} value={k}>{v}</option>)}
            </select></div>
          <div style={{ display:"flex", alignItems:"center", gap:8, paddingTop:18 }}>
            <input type="checkbox" checked={paperMode} onChange={e=>setPaper(e.target.checked)} />
            <span style={{ fontSize:12, color:paperMode?C.amber:C.muted }}>
              {paperMode?"📋 Paper Mode (safe)":"🔴 Live Trading"}
            </span>
          </div>
        </div>
      </Panel>

      {/* Entry conditions */}
      <Panel>
        <div style={{ display:"flex", justifyContent:"space-between", marginBottom:12 }}>
          <div>
            <div style={{ fontWeight:700, color:C.text, fontSize:13 }}>Entry Conditions</div>
            <div style={{ fontSize:10, color:C.muted }}>All conditions must be met (AND) or any (OR)</div>
          </div>
          <Btn onClick={addCond} color={C.indigo} style={{ padding:"5px 12px", fontSize:11 }}>+ Add Condition</Btn>
        </div>
        {conds.map((c,i)=>(
          <div key={c.id}>
            {i > 0 && (
              <div style={{ display:"flex", alignItems:"center", gap:8, margin:"7px 0" }}>
                <div style={{ flex:1, height:1, background:C.border }} />
                <select value={c.join} onChange={e=>updCond(c.id,"join",e.target.value)}
                  style={{ ...sel, fontSize:10, padding:"3px 8px", color:C.amber, background:C.amber+"11" }}>
                  <option>AND</option><option>OR</option>
                </select>
                <div style={{ flex:1, height:1, background:C.border }} />
              </div>
            )}
            <div style={{ display:"flex", gap:8, alignItems:"center", background:C.surface,
              border:`1px solid ${C.border}`, borderRadius:8, padding:"9px 12px", flexWrap:"wrap" }}>
              <span style={{ fontSize:11, color:C.muted, fontWeight:700 }}>IF</span>
              <select value={c.indicator} onChange={e=>updCond(c.id,"indicator",e.target.value)}
                style={{ ...sel, flex:"1 1 100px" }}>
                {ALL_INDICATORS.map(x=><option key={x}>{x}</option>)}
              </select>
              <select value={c.comparator} onChange={e=>updCond(c.id,"comparator",e.target.value)}
                style={{ ...sel, color:C.cyan, flex:"1 1 130px" }}>
                {COMPARATORS.map(x=><option key={x} value={x}>{COMP_LABELS[x]}</option>)}
              </select>
              <input value={c.value} onChange={e=>updCond(c.id,"value",e.target.value)}
                style={{ ...sel, width:90 }} placeholder="value / indicator" />
              <button onClick={()=>remCond(c.id)} style={{ background:C.red+"11", color:C.red,
                border:`1px solid ${C.red}33`, borderRadius:5, padding:"4px 8px", cursor:"pointer" }}>✕</button>
            </div>
          </div>
        ))}
      </Panel>

      {/* Risk + MTF */}
      <div style={{ display:"grid", gridTemplateColumns:"repeat(auto-fit,minmax(270px,1fr))", gap:13 }}>
        <Panel>
          <div style={{ fontWeight:700, color:C.text, fontSize:13, marginBottom:12 }}>Risk Management</div>
          {[
            { l:"Stop Loss %", v:slPct, s:setSl, c:C.red, min:0.5, max:20, step:0.5 },
            { l:"Target 1 %",  v:t1Pct, s:setT1, c:C.green, min:0.5, max:20, step:0.5 },
            { l:"Target 2 %",  v:t2Pct, s:setT2, c:C.cyan,  min:1,   max:40, step:0.5 },
            { l:"Trailing SL %",v:trailPct,s:setTrail,c:C.amber,min:0.5,max:10,step:0.5 },
          ].map(f=>(
            <div key={f.l} style={{ marginBottom:11 }}>
              <div style={{ display:"flex", justifyContent:"space-between", marginBottom:4 }}>
                <span style={{ fontSize:11, color:C.muted }}>{f.l}</span>
                <span style={{ fontSize:12, fontWeight:700, color:f.c, fontFamily:MONO }}>{f.v}%</span>
              </div>
              <input type="range" min={f.min} max={f.max} step={f.step} value={f.v}
                onChange={e=>f.s(+e.target.value)} style={{ width:"100%", accentColor:f.c }} />
            </div>
          ))}
          <div style={{ marginBottom:11 }}>
            <div style={{ display:"flex", justifyContent:"space-between", marginBottom:4 }}>
              <span style={{ fontSize:11, color:C.muted }}>Trade Amount</span>
              <span style={{ fontSize:12, fontWeight:700, color:C.text, fontFamily:MONO }}>
                ₹{tradeAmt.toLocaleString()}
              </span>
            </div>
            <input type="range" min={5000} max={100000} step={5000} value={tradeAmt}
              onChange={e=>setAmt(+e.target.value)} style={{ width:"100%", accentColor:C.indigo }} />
          </div>
          <div style={{ background:rr>=2?C.green+"11":C.amber+"11", border:`1px solid ${rr>=2?C.green:C.amber}33`,
            borderRadius:6, padding:"7px 10px", fontSize:11, color:rr>=2?C.green:C.amber }}>
            Risk:Reward = 1:{rr} {rr>=2?"✅ Good":"⚠️ Aim for 1:2+"}
          </div>
        </Panel>

        <Panel>
          <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:12 }}>
            <div style={{ fontWeight:700, color:C.text, fontSize:13 }}>Multi-Timeframe Confirmation</div>
            <input type="checkbox" checked={mtfEnabled} onChange={e=>setMtf(e.target.checked)} />
          </div>
          {mtfEnabled && (
            <>
              <div style={{ fontSize:11, color:C.muted, marginBottom:10 }}>
                Signal on <Badge label={primaryTf} color={C.indigo} /> confirmed by:
              </div>
              <div style={{ display:"flex", gap:7, flexWrap:"wrap", marginBottom:12 }}>
                {CONFIRM_TFS.map(tf=>(
                  <button key={tf} onClick={()=>toggleTf(tf)} style={{
                    background:confirmTfs.includes(tf)?C.cyan+"22":C.surface,
                    color:confirmTfs.includes(tf)?C.cyan:C.muted,
                    border:`1px solid ${confirmTfs.includes(tf)?C.cyan+"44":C.border}`,
                    borderRadius:6, padding:"5px 12px", fontSize:11, cursor:"pointer",
                    fontWeight:confirmTfs.includes(tf)?700:400 }}>
                    {tf}
                  </button>
                ))}
              </div>
              <div style={{ background:C.indigo+"11", border:`1px solid ${C.indigo}22`,
                borderRadius:7, padding:"9px 11px", fontSize:11, color:C.muted, lineHeight:1.6 }}>
                <b style={{ color:C.text }}>How it works:</b><br/>
                EMA(20) &gt; EMA(50) AND SuperTrend bullish on each confirmation timeframe.
                Majority must agree before entry is allowed.
              </div>
            </>
          )}
          {!mtfEnabled && (
            <div style={{ color:C.muted, fontSize:12, padding:16, textAlign:"center" }}>
              ⚠️ MTF disabled — single timeframe signals only.<br/>
              More false signals without confirmation.
            </div>
          )}
        </Panel>
      </div>

      <div style={{ display:"flex", gap:9, justifyContent:"flex-end" }}>
        {/* FIX: removed redundant style.background — it always overrode the
            Btn component's disabled?dim:color logic, so the button never
            visually looked disabled while saving. The `color` prop alone
            already supplies the gradient when not disabled. */}
        <Btn onClick={save} disabled={saving} color={`linear-gradient(135deg,${C.indigo},#8B5CF6)`}>
          {saving ? "⏳ Saving…" : saved ? "✅ Saved!" : editingId ? "💾 Update Strategy" : "💾 Save Strategy"}
        </Btn>
        {editingId && (
          <button onClick={resetForm}
            style={{ marginLeft:10, background:"transparent", color:C.muted,
              border:`1px solid ${C.border}`, borderRadius:8, padding:"8px 14px",
              fontSize:13, cursor:"pointer" }}>
            Cancel edit
          </button>
        )}
      </div>

      {/* Saved strategies — with delete. Deleting here removes from the shared
          list, so it also disappears from the Backtest & Deploy dropdowns. */}
      <Panel>
        <SLabel>Saved Strategies ({strategies.length})</SLabel>
        {editingId && (
          <div style={{ background:`${C.indigo}22`, border:`1px solid ${C.indigo}55`,
              borderRadius:8, padding:"7px 11px", margin:"6px 0", fontSize:12, color:C.text }}>
            ✏️ Editing an existing strategy — change the fields above and click <b>Update Strategy</b>, or <span onClick={resetForm} style={{ color:C.indigo, cursor:"pointer", textDecoration:"underline" }}>cancel</span>.
          </div>
        )}
        {strategies.length === 0 ? (
          <div style={{ color:C.muted, fontSize:12, padding:"6px 0" }}>
            No saved strategies yet. Build one above and hit Save.
          </div>
        ) : (
          <div style={{ display:"flex", flexDirection:"column", gap:7, marginTop:6 }}>
            {strategies.map(s => (
              <div key={s.id} style={{ display:"flex", alignItems:"center", justifyContent:"space-between",
                  background:C.bg, border:`1px solid ${editingId===s.id ? C.indigo : C.border}`, borderRadius:8, padding:"8px 11px" }}>
                <div style={{ minWidth:0 }}>
                  <div style={{ color:C.text, fontWeight:600, fontSize:13, overflow:"hidden",
                      textOverflow:"ellipsis", whiteSpace:"nowrap" }}>{s.name}</div>
                  <div style={{ color:C.muted, fontSize:11 }}>
                    {s.symbol} · {s.primary_tf} · {s.trade_type}
                  </div>
                </div>
                {confirmDelete === s.id ? (
                  <div style={{ display:"flex", gap:6, flexShrink:0 }}>
                    <button onClick={async ()=>{ setConfirmDelete(null); await onDelete?.(s); }}
                      style={{ background:C.red, color:"#fff", border:"none", borderRadius:6,
                        padding:"5px 10px", fontSize:12, fontWeight:600, cursor:"pointer" }}>
                      Confirm
                    </button>
                    <button onClick={()=>setConfirmDelete(null)}
                      style={{ background:"transparent", color:C.muted, border:`1px solid ${C.border}`,
                        borderRadius:6, padding:"5px 10px", fontSize:12, cursor:"pointer" }}>
                      Cancel
                    </button>
                  </div>
                ) : (
                  <div style={{ display:"flex", gap:6, flexShrink:0 }}>
                    <button onClick={()=>loadForEdit(s)} title="Edit strategy"
                      style={{ background:"transparent", color:C.indigo, border:`1px solid ${C.indigo}55`,
                        borderRadius:6, padding:"5px 10px", fontSize:12, fontWeight:600, cursor:"pointer" }}>
                      ✏️ Edit
                    </button>
                    <button onClick={()=>setConfirmDelete(s.id)} title="Delete strategy"
                      style={{ background:"transparent", color:C.red, border:`1px solid ${C.red}44`,
                        borderRadius:6, padding:"5px 10px", fontSize:12, fontWeight:600, cursor:"pointer" }}>
                      🗑️ Delete
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </Panel>
    </div>
  );
}

// ─── Backtest ─────────────────────────────────────────────────────────────────
function BacktestPanel({ strategies }) {
  const [sid,    setSid]    = useState("");
  const [symbol, setSym]    = useState("RELIANCE");
  const [start,  setStart]  = useState("2023-01-01");
  const [end,    setEnd]    = useState(new Date().toISOString().slice(0,10));
  const [capital,setCap]    = useState(1000000);
  const [result, setResult] = useState(null);
  const [busy,   setBusy]   = useState(false);
  const [error,  setError]  = useState("");

  // FIX: auto-fill symbol from the strategy's own saved symbol when selected,
  // so the strategy you built is replicated faithfully in backtest (same stock
  // it was designed for) instead of silently defaulting to whatever was last typed.
  useEffect(() => {
    if (!sid) return;
    const strat = strategies.find(s => s.id === sid);
    if (strat?.symbol) setSym(strat.symbol);
  }, [sid, strategies]);

  const run = async () => {
    if (!sid) { setError("Select a strategy first"); return; }
    const strat = strategies.find(s=>s.id===sid);
    if (!strat) { setError("Strategy not found"); return; }
    setBusy(true); setResult(null); setError("");
    try {
      const res = await api("/api/backtest", { method:"POST",
        timeoutMs: 120000,   // backtests can fetch many data chunks — allow 2 min
        body: JSON.stringify({ strategy:strat, symbol, start_date:start,
                               end_date:end, initial_capital:capital }) });
      if (res?.total_trades !== undefined) setResult(res);
      else setError(lastApiError || "Backtest failed — check backend logs");
    } catch(e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const gradeColor = { "A+":C.green, A:C.cyan, B:C.indigo, C:C.amber, D:C.red };

  return (
    <div style={{ display:"flex", flexDirection:"column", gap:13 }}>
      <Panel>
        <div style={{ display:"grid", gridTemplateColumns:"repeat(auto-fit,minmax(140px,1fr))", gap:11, alignItems:"end" }}>
          <div><SLabel>Strategy</SLabel>
            <select value={sid} onChange={e=>setSid(e.target.value)} style={{...sel,width:"100%"}}>
              <option value="">Select strategy...</option>
              {strategies.map(s=><option key={s.id} value={s.id}>{s.name}</option>)}
            </select></div>
          <div><SLabel>Symbol</SLabel>
            <input value={symbol} onChange={e=>setSym(e.target.value.toUpperCase())}
              style={{ ...sel, width:"100%", boxSizing:"border-box" }} /></div>
          <div><SLabel>Start Date</SLabel>
            <input type="date" value={start} onChange={e=>setStart(e.target.value)}
              style={{ ...sel, width:"100%", boxSizing:"border-box" }} /></div>
          <div><SLabel>End Date</SLabel>
            <input type="date" value={end} onChange={e=>setEnd(e.target.value)}
              style={{ ...sel, width:"100%", boxSizing:"border-box" }} /></div>
          <div><SLabel>Capital</SLabel>
            <input type="number" value={capital} onChange={e=>setCap(+e.target.value)}
              style={{ ...sel, width:"100%", boxSizing:"border-box" }} /></div>
          <Btn onClick={run} disabled={busy}>
            {busy?"⏳ Running…":"▶ Run Backtest"}
          </Btn>
        </div>
        {error && <div style={{ background:C.red+"11", border:`1px solid ${C.red}33`,
          borderRadius:7, padding:"8px 11px", fontSize:11, color:C.red, marginTop:11 }}>
          ❌ {error}</div>}
      </Panel>

      {result && (
        <>
          {/* Score banner */}
          <Panel style={{ display:"flex", justifyContent:"space-between", alignItems:"center",
            border:`2px solid ${gradeColor[result.score_grade]||C.border}` }}>
            <div>
              <div style={{ fontSize:11, color:C.muted }}>Strategy Score</div>
              <div style={{ fontSize:32, fontWeight:900, color:gradeColor[result.score_grade]||C.text }}>
                {result.score_grade}
              </div>
              <div style={{ fontSize:12, color:C.muted }}>{result.score}/100 points</div>
            </div>
            <div style={{ textAlign:"right" }}>
              <div style={{ fontSize:11, color:C.muted }}>{result.strategy_name} · {result.symbol}</div>
              <div style={{ fontSize:22, fontWeight:800, fontFamily:MONO,
                color:result.total_return_pct>=0?C.green:C.red }}>
                {result.total_return_pct>=0?"+":""}{result.total_return_pct}%
              </div>
              <div style={{ fontSize:12, color:C.muted }}>Total return</div>
            </div>
          </Panel>

          {/* Equity curve */}
          <Panel>
            <div style={{ fontWeight:700, color:C.text, marginBottom:11 }}>Equity Curve</div>
            <ResponsiveContainer width="100%" height={200}>
              <LineChart data={result.equity_curve}>
                <CartesianGrid strokeDasharray="3 3" stroke={C.border} />
                <XAxis dataKey="date" tick={{ fontSize:9, fill:C.muted }} interval="preserveStartEnd" />
                <YAxis tick={{ fontSize:9, fill:C.muted }} tickFormatter={v=>`₹${(v/100000).toFixed(1)}L`} />
                <Tooltip formatter={(v)=>[`₹${v.toLocaleString()}`,"Portfolio"]}
                  contentStyle={{ background:C.card, border:`1px solid ${C.border}`, fontSize:11 }} />
                <ReferenceLine y={capital} stroke={C.dim} strokeDasharray="3 3" />
                <Line type="monotone" dataKey="value" stroke={C.indigo} strokeWidth={2} dot={false} />
              </LineChart>
            </ResponsiveContainer>
          </Panel>

          {/* Stats grid */}
          <div style={{ display:"grid", gridTemplateColumns:"repeat(auto-fit,minmax(110px,1fr))", gap:7 }}>
            {[
              {l:"CAGR",        v:`${result.cagr_pct}%`,                c:C.cyan  },
              {l:"Win Rate",    v:`${result.win_rate_pct}%`,            c:C.green },
              {l:"Max DD",      v:`${result.max_drawdown_pct}%`,        c:C.red   },
              {l:"Trades",      v:result.total_trades,                  c:C.text  },
              {l:"Wins / Loss", v:`${result.winning_trades}/${result.losing_trades}`,c:C.amber},
              {l:"Sharpe",      v:result.sharpe_ratio,                  c:C.indigo},
              {l:"Profit Factor",v:result.profit_factor>=999?"∞":result.profit_factor,c:C.green},
              {l:"Avg Trade",   v:`${result.avg_trade_pct}%`,           c:C.text  },
              {l:"Best Trade",  v:`+${result.best_trade_pct}%`,         c:C.green },
              {l:"Worst Trade", v:`${result.worst_trade_pct}%`,         c:C.red   },
              {l:"Final Capital",v:`₹${(result.final_capital/100000).toFixed(1)}L`,c:C.cyan},
            ].map(s=>(
              <div key={s.l} style={{ background:C.card, border:`1px solid ${C.border}`,
                borderRadius:8, padding:"9px 11px", textAlign:"center" }}>
                <div style={{ fontSize:9, color:C.muted, marginBottom:3 }}>{s.l}</div>
                <div style={{ fontSize:13, fontWeight:800, color:s.c, fontFamily:MONO }}>{s.v}</div>
              </div>
            ))}
          </div>
        </>
      )}

      {!result && !busy && (
        <Panel style={{ textAlign:"center", padding:55 }}>
          <div style={{ fontSize:36, marginBottom:11 }}>📊</div>
          <div style={{ fontSize:14, color:C.text, fontWeight:600, marginBottom:5 }}>
            Select strategy, symbol & dates → Run
          </div>
          <div style={{ fontSize:12, color:C.muted }}>
            Entry after 11:00 AM · Partial booking at T1 · No same-day re-entry<br/>
            Score grades: A+ (80+) · A (70+) · B (60+) · C (50+) · D (below 50)
          </div>
        </Panel>
      )}
    </div>
  );
}

// ─── Deploy ───────────────────────────────────────────────────────────────────
function DeployPanel({ strategies, deployments, setDeployments, notify }) {
  const [sid,   setSid]   = useState("");
  const [sym,   setSym]   = useState("RELIANCE");
  const [busy,  setBusy]  = useState(false);

  // FIX: auto-fill symbol from the strategy's saved symbol — same fix as Backtest,
  // ensures the deployed strategy trades the stock it was actually built for.
  useEffect(() => {
    if (!sid) return;
    const strat = strategies.find(s => s.id === sid);
    if (strat?.symbol) setSym(strat.symbol);
  }, [sid, strategies]);

  const deploy = async () => {
    if (!sid) return;
    const strat = strategies.find(s=>s.id===sid);
    if (!strat) return;
    setBusy(true);
    const payload = { ...strat, symbol:sym };
    const res = await api("/api/deployments", { method:"POST", body:JSON.stringify(payload) });
    setBusy(false);
    if (res?.id) {
      setDeployments(p=>[...p, res]);
      notify(`✅ '${strat.name}' deployed on ${sym}`);
    } else {
      notify(`❌ Deploy failed: ${lastApiError || "check backend"}`);
    }
  };

  const pause  = async id => {
    await api(`/api/deployments/${id}/pause`, { method:"PATCH" });
    setDeployments(p=>p.map(d=>d.id===id?{...d,status:"PAUSED"}:d));
    notify("Deployment paused");
  };
  const resume = async id => {
    await api(`/api/deployments/${id}/resume`, { method:"PATCH" });
    setDeployments(p=>p.map(d=>d.id===id?{...d,status:"LIVE"}:d));
    notify("Deployment resumed");
  };
  const stop   = async id => {
    await api(`/api/deployments/${id}`, { method:"DELETE" });
    setDeployments(p=>p.filter(d=>d.id!==id));
    notify("Deployment stopped");
  };

  const brokerC = { paper:C.cyan, fyers:"#FF6B00", zerodha:C.green, dhan:"#2563EB" };

  return (
    <div style={{ display:"flex", flexDirection:"column", gap:13 }}>
      {/* Deploy form */}
      <Panel>
        <div style={{ fontWeight:700, color:C.text, marginBottom:12 }}>Deploy Strategy</div>
        <div style={{ display:"flex", gap:11, flexWrap:"wrap", alignItems:"end" }}>
          <div style={{ flex:"1 1 180px" }}><SLabel>Strategy</SLabel>
            <select value={sid} onChange={e=>setSid(e.target.value)} style={{...sel,width:"100%"}}>
              <option value="">Select...</option>
              {strategies.map(s=><option key={s.id} value={s.id}>{s.name}</option>)}
            </select></div>
          <div><SLabel>Symbol</SLabel>
            <input value={sym} onChange={e=>setSym(e.target.value.toUpperCase())}
              style={{ ...sel, width:110 }} /></div>
          <Btn onClick={deploy} disabled={busy||!sid}>
            {busy?"Deploying…":"⚡ Deploy"}
          </Btn>
        </div>
      </Panel>

      {/* Active deployments */}
      {deployments.length === 0 && (
        <Panel style={{ textAlign:"center", padding:44 }}>
          <div style={{ fontSize:28, marginBottom:8 }}>⚡</div>
          <div style={{ color:C.muted, fontSize:13 }}>No active deployments — deploy a strategy above</div>
        </Panel>
      )}
      {deployments.map(d=>(
        <Panel key={d.id} style={{ border:`1px solid ${d.status==="LIVE"?C.green+"44":C.border}` }}>
          <div style={{ display:"flex", justifyContent:"space-between", alignItems:"flex-start", flexWrap:"wrap", gap:11 }}>
            <div>
              <div style={{ display:"flex", alignItems:"center", gap:7, marginBottom:4 }}>
                <div style={{ width:8, height:8, borderRadius:"50%",
                  background:d.status==="LIVE"?C.green:C.muted,
                  boxShadow:d.status==="LIVE"?`0 0 6px ${C.green}`:"none" }} />
                <span style={{ fontWeight:700, fontSize:14, color:C.text }}>{d.strategy?.name}</span>
                <Badge label={d.status} color={d.status==="LIVE"?C.green:C.muted} />
                <Badge label={d.paper_mode?"PAPER":"LIVE"} color={d.paper_mode?C.cyan:C.red} />
                <Badge label={d.broker} color={brokerC[d.broker]||C.muted} />
              </div>
              <div style={{ fontSize:11, color:C.muted }}>
                ID: {d.id} · Trades: {d.trade_count} · P&L today: {" "}
                <span style={{ color:d.today_pnl>=0?C.green:C.red }}>
                  ₹{(d.today_pnl||0).toLocaleString()}
                </span>
              </div>
            </div>
            <div style={{ display:"flex", gap:7 }}>
              {d.status==="LIVE"
                ? <button onClick={()=>pause(d.id)} style={{ background:C.amber+"22", color:C.amber,
                    border:`1px solid ${C.amber}44`, borderRadius:7, padding:"6px 13px",
                    cursor:"pointer", fontSize:11, fontWeight:700 }}>⏸ Pause</button>
                : <button onClick={()=>resume(d.id)} style={{ background:C.green+"22", color:C.green,
                    border:`1px solid ${C.green}44`, borderRadius:7, padding:"6px 13px",
                    cursor:"pointer", fontSize:11, fontWeight:700 }}>▶ Resume</button>
              }
              <button onClick={()=>stop(d.id)} style={{ background:C.red+"11", color:C.red,
                border:`1px solid ${C.red}33`, borderRadius:7, padding:"6px 11px",
                cursor:"pointer", fontSize:11 }}>🗑 Stop</button>
            </div>
          </div>
        </Panel>
      ))}
    </div>
  );
}

// ─── Universe screener ────────────────────────────────────────────────────────
// ─── Stock Deep Dives ──────────────────────────────────────────────────────────
// Search ONE stock → full fundamental profile from stored IndianAPI data.
// Design: lead with the score + verdict, calm spacing, color guides not decorates,
// progressive detail below the fold. All numbers coerced & guarded.
function DeepDivePanel() {
  const [query,   setQuery]   = useState("");
  const [data,    setData]    = useState(null);
  const [loading, setLoading] = useState(false);
  const [error,   setError]   = useState("");
  const liveSym = useMemo(() => (data && data.symbol ? [data.symbol] : []), [data]);
  const live = useLivePrices(liveSym, { intervalMs: 1500 });

  const fetchStock = async (sym) => {
    const s = (sym || query).trim().toUpperCase().replace(/\s+/g, "");
    if (!s) return;
    setLoading(true); setError(""); setData(null);
    const d = await api(`/api/stock/deep-dive/${encodeURIComponent(s)}`, { timeoutMs: 30000 });
    if (d) setData(d);
    else setError(lastApiError || "Stock not found in cache");
    setLoading(false);
  };

  // ── safe numeric helpers (never trust the field type) ──
  const N = v => { const n = Number(v); return (v==null || isNaN(n)) ? null : n; };
  const scoreColor = v => v>=70?C.green:v>=50?C.cyan:v>=30?C.amber:C.red;
  const scoreGrade = v => v==null?"—":v>=80?"Excellent":v>=65?"Strong":v>=50?"Fair":v>=30?"Weak":"Poor";
  const fmtCr = v => { const n=N(v); return n==null ? "—" :
    Math.abs(n)>=100000 ? `₹${(n/100000).toFixed(2)}L Cr` :
    Math.abs(n)>=1000   ? `₹${(n/1000).toFixed(1)}K Cr` :
                          `₹${n.toLocaleString("en-IN",{maximumFractionDigits:0})} Cr`; };
  const pct = v => { const n=N(v); return n==null ? "—" : `${n.toFixed(1)}%`; };
  const num = (v,d=1) => { const n=N(v); return n==null ? "—" : n.toFixed(d); };

  const CAT_LABELS = {
    profitability:"Profitability", growth:"Growth", financial_strength:"Fin. Strength",
    cash_quality:"Cash Quality", valuation:"Valuation", mgmt_ownership:"Mgmt & Ownership",
  };
  const PIE_COLORS = [C.indigo, C.cyan, C.amber, C.purple, C.green, C.red];
  const chartTip = { background:C.card, border:`1px solid ${C.border}`, borderRadius:8, fontSize:12 };

  return (
    <div style={{ display:"flex", flexDirection:"column", gap:14 }}>
      {/* Search bar */}
      <Panel style={{ display:"flex", gap:11, alignItems:"flex-end", flexWrap:"wrap" }}>
        <div style={{ flex:"1 1 280px" }}>
          <SLabel>Search a Stock — full fundamental deep dive</SLabel>
          <input value={query} onChange={e=>setQuery(e.target.value)}
            onKeyDown={e=>e.key==="Enter"&&fetchStock()}
            placeholder="Enter NSE symbol (e.g. RELIANCE, ATLANTAELE, MMTC)..."
            style={{ ...sel, width:"100%", boxSizing:"border-box" }} />
        </div>
        <Btn onClick={()=>fetchStock()} color={C.indigo} disabled={loading}>
          {loading ? "Analyzing…" : "🔍 Analyze"}
        </Btn>
      </Panel>

      {loading && (
        <Panel style={{ textAlign:"center", padding:48, color:C.muted }}>
          <div style={{ fontSize:13 }}>Building deep dive from stored fundamentals…</div>
        </Panel>
      )}
      {error && !loading && (
        <Panel style={{ textAlign:"center", padding:34 }}>
          <div style={{ fontSize:30, marginBottom:8 }}>🔍</div>
          <div style={{ color:C.text, fontSize:14, fontWeight:600, marginBottom:4 }}>{error}</div>
          <div style={{ color:C.muted, fontSize:12 }}>
            The stock may not be enriched yet, or the symbol differs. Try the exact NSE symbol.
          </div>
        </Panel>
      )}

      {data && !loading && (() => {
        const b = data.breakdown || {};
        const cats = b.category_scores || {};
        const score = N(data.score ?? b.score);
        const annual = data.annual || [];
        const px = data.current_price || {};
        const pctx = data.price_ctx || {};
        const promoter = (data.shareholding||[]).find(s=>/promoter/i.test(s.category||""));

        // ── chart series (all guarded against bad/zero/negative denominators) ──
        const trend = annual.map(r=>({ year:r.year, Revenue:N(r.revenue), Profit:N(r.net_income) }));
        const safePctOf = (part, whole) => {
          const p=N(part), w=N(whole);
          return (p==null || w==null || w<=0) ? null : +(100*p/w).toFixed(1);
        };
        const margins = annual.map(r=>({
          year:r.year,
          "Gross %": safePctOf(r.gross_profit, r.revenue),
          "Op %":    safePctOf(r.operating_income, r.revenue),
          "Net %":   safePctOf(r.net_income, r.revenue),
        }));
        const debtEq = annual.map(r=>({ year:r.year, Debt:N(r.total_debt), Equity:N(r.total_equity) }));
        // FCF = Operating Cash Flow − Capex. Capex stored negative in the API, but be
        // robust: subtract its absolute value regardless of sign.
        const cashSeries = annual.map(r=>{
          const ocf=N(r.operating_cash_flow), cx=N(r.capex);
          return { year:r.year, "Op Cash Flow":ocf,
            "FCF": (ocf==null||cx==null) ? null : +(ocf - Math.abs(cx)).toFixed(0) };
        });
        const ownPie = (data.shareholding||[])
          .filter(s=>N(s.latest)!=null)
          .map(s=>({ name:(s.category||"").replace(/Shareholding.*/i,"").trim()||s.category, value:N(s.latest) }));
        const catBars = Object.keys(CAT_LABELS).map(k=>({
          cat:CAT_LABELS[k], score:N(cats[k])!=null?Math.round(N(cats[k])):0, has:N(cats[k])!=null,
        }));
        const ch1d = N(pctx.change_1d_pct);

        return (
          <>
            {/* ── HERO: lead with score + verdict (research: one number first) ── */}
            <Panel style={{ background:`linear-gradient(135deg, ${C.card}, ${C.surface})` }}>
              <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center",
                flexWrap:"wrap", gap:18 }}>
                <div style={{ flex:"1 1 300px" }}>
                  <div style={{ display:"flex", alignItems:"center", gap:10, flexWrap:"wrap" }}>
                    <span style={{ fontFamily:MONO, fontSize:24, fontWeight:800, color:C.text }}>
                      {data.symbol}</span>
                    {data.industry && <Badge label={data.industry} color={C.purple} />}
                  </div>
                  <div style={{ fontSize:14, color:C.muted, marginTop:4 }}>{data.name}</div>
                  {(() => {
                    const lp = live[data.symbol];
                    const liveLtp = lp && lp.ltp != null ? Number(lp.ltp) : null;
                    const shownPx = liveLtp != null
                      ? liveLtp.toLocaleString("en-IN",{minimumFractionDigits:2})
                      : (px.NSE || px.BSE);
                    const shownChg = liveLtp != null && lp.change_pct != null ? lp.change_pct : ch1d;
                    const isLive = liveLtp != null && lp.live !== false;
                    if (shownPx == null) return null;
                    return (
                      <div style={{ display:"flex", alignItems:"baseline", gap:10, marginTop:10 }}>
                        <span style={{ fontSize:22, fontWeight:700, color:C.text, fontFamily:MONO }}>
                          ₹{shownPx}</span>
                        {shownChg!=null && (
                          <span style={{ fontSize:13, fontWeight:700, color:shownChg>=0?C.green:C.red }}>
                            {shownChg>=0?"▲":"▼"} {Math.abs(shownChg).toFixed(2)}%
                          </span>
                        )}
                        <span style={{ fontSize:9, fontWeight:700, letterSpacing:.5,
                          color:isLive?C.green:C.muted,
                          border:`1px solid ${isLive?C.green:C.border}`, borderRadius:5,
                          padding:"1px 6px" }}>
                          {isLive ? "● LIVE" : "LAST CLOSE"}
                        </span>
                        {N(pctx.week52_low)!=null && N(pctx.week52_high)!=null && (
                          <span style={{ fontSize:11, color:C.muted }}>
                            52w ₹{pctx.week52_low} – ₹{pctx.week52_high}
                          </span>
                        )}
                      </div>
                    );
                  })()}
                </div>
                {/* Score ring */}
                <div style={{ display:"flex", alignItems:"center", gap:16 }}>
                  <div style={{ position:"relative", width:96, height:96 }}>
                    <svg width="96" height="96" style={{ transform:"rotate(-90deg)" }}>
                      <circle cx="48" cy="48" r="42" fill="none" stroke={C.border} strokeWidth="7" />
                      <circle cx="48" cy="48" r="42" fill="none" stroke={scoreColor(score||0)} strokeWidth="7"
                        strokeLinecap="round" strokeDasharray={2*Math.PI*42}
                        strokeDashoffset={2*Math.PI*42*(1-(score||0)/100)}
                        style={{ transition:"stroke-dashoffset .6s ease" }} />
                    </svg>
                    <div style={{ position:"absolute", inset:0, display:"flex", flexDirection:"column",
                      alignItems:"center", justifyContent:"center" }}>
                      <div style={{ fontSize:26, fontWeight:900, color:scoreColor(score||0),
                        fontFamily:MONO, lineHeight:1 }}>{score!=null?score:"—"}</div>
                      <div style={{ fontSize:8, color:C.muted, letterSpacing:1 }}>/ 100</div>
                    </div>
                  </div>
                  <div>
                    <div style={{ fontSize:9, color:C.muted, textTransform:"uppercase", letterSpacing:1 }}>
                      Fundamental Score</div>
                    <div style={{ fontSize:18, fontWeight:800, color:scoreColor(score||0), marginTop:2 }}>
                      {scoreGrade(score)}</div>
                  </div>
                </div>
              </div>

              {/* data-quality flag */}
              {data.flags && Object.keys(data.flags).length>0 && (
                <div style={{ marginTop:14, background:C.amber+"11", border:`1px solid ${C.amber}44`,
                  borderRadius:8, padding:"8px 12px", fontSize:11, color:C.amber }}>
                  ⚠ Data-quality flag — values cross-checked, review advised: {Object.keys(data.flags).join(", ")}
                </div>
              )}
            </Panel>

            {/* ── KEY METRIC STRIP (research: 5–9 core elements, scannable) ── */}
            <div style={{ display:"grid", gridTemplateColumns:"repeat(auto-fit,minmax(118px,1fr))", gap:10 }}>
              <StatBox label="Market Cap" value={fmtCr(data.extracted.market_cap_cr)} />
              <StatBox label="Revenue (TTM)" value={fmtCr(data.extracted.revenue_cr)} color={C.cyan} />
              <StatBox label="Net Income" value={fmtCr(data.extracted.net_income_cr)}
                color={N(data.extracted.net_income_cr)>=0?C.green:C.red} />
              <StatBox label="Free Cash Flow" value={fmtCr(data.extracted.fcf_cr)}
                color={N(data.extracted.fcf_cr)>=0?C.green:C.red} />
              <StatBox label="ROE" value={pct(data.extracted.roe)} />
              <StatBox label="P/E" value={num(data.extracted.pe_ratio)} />
              <StatBox label="Debt/Equity" value={num(data.extracted.debt_to_equity,2)} />
              <StatBox label="Promoter" value={pct(data.extracted.promoter_holding)} color={C.purple} />
            </div>

            {/* ── Category breakdown ── */}
            <Panel>
              <div style={{ fontSize:13, fontWeight:700, marginBottom:3 }}>Score Breakdown — Six Fundamental Categories</div>
              <div style={{ fontSize:11, color:C.muted, marginBottom:14 }}>
                Weighted: Profitability 25% · Cash 20% · Strength 18% · Growth 17% · Valuation 12% · Mgmt 8%
              </div>
              <ResponsiveContainer width="100%" height={232}>
                <BarChart data={catBars} layout="vertical" margin={{ left:34, right:24 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke={C.border} horizontal={false} />
                  <XAxis type="number" domain={[0,100]} stroke={C.muted} fontSize={10} />
                  <YAxis type="category" dataKey="cat" stroke={C.muted} fontSize={11} width={92} />
                  <Tooltip contentStyle={chartTip} cursor={{ fill:C.surface+"55" }} />
                  <Bar dataKey="score" radius={[0,5,5,0]} barSize={18}>
                    {catBars.map((e,i)=><Cell key={i} fill={e.has?scoreColor(e.score):C.dim} />)}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </Panel>

            {/* ── Revenue & Profit + Margins ── */}
            <div style={{ display:"grid", gridTemplateColumns:"repeat(auto-fit,minmax(300px,1fr))", gap:14 }}>
              <Panel>
                <div style={{ fontSize:13, fontWeight:700, marginBottom:12 }}>Revenue & Profit (₹ Cr)</div>
                <ResponsiveContainer width="100%" height={210}>
                  <BarChart data={trend}>
                    <CartesianGrid strokeDasharray="3 3" stroke={C.border} vertical={false} />
                    <XAxis dataKey="year" stroke={C.muted} fontSize={10} />
                    <YAxis stroke={C.muted} fontSize={10} width={45} />
                    <Tooltip contentStyle={chartTip} cursor={{ fill:C.surface+"55" }} />
                    <Legend wrapperStyle={{ fontSize:11 }} />
                    <Bar dataKey="Revenue" fill={C.indigo} radius={[3,3,0,0]} />
                    <Bar dataKey="Profit"  fill={C.green}  radius={[3,3,0,0]} />
                  </BarChart>
                </ResponsiveContainer>
              </Panel>
              <Panel>
                <div style={{ fontSize:13, fontWeight:700, marginBottom:12 }}>Margin Trend (%)</div>
                <ResponsiveContainer width="100%" height={210}>
                  <LineChart data={margins}>
                    <CartesianGrid strokeDasharray="3 3" stroke={C.border} vertical={false} />
                    <XAxis dataKey="year" stroke={C.muted} fontSize={10} />
                    <YAxis stroke={C.muted} fontSize={10} width={45} />
                    <Tooltip contentStyle={chartTip} />
                    <Legend wrapperStyle={{ fontSize:11 }} />
                    <Line type="monotone" dataKey="Gross %" stroke={C.cyan}  dot={false} strokeWidth={2} connectNulls />
                    <Line type="monotone" dataKey="Op %"    stroke={C.amber} dot={false} strokeWidth={2} connectNulls />
                    <Line type="monotone" dataKey="Net %"   stroke={C.green} dot={false} strokeWidth={2} connectNulls />
                  </LineChart>
                </ResponsiveContainer>
              </Panel>
            </div>

            {/* ── Debt vs Equity + Cash flow ── */}
            <div style={{ display:"grid", gridTemplateColumns:"repeat(auto-fit,minmax(300px,1fr))", gap:14 }}>
              <Panel>
                <div style={{ fontSize:13, fontWeight:700, marginBottom:12 }}>Debt vs Equity (₹ Cr)</div>
                <ResponsiveContainer width="100%" height={200}>
                  <BarChart data={debtEq}>
                    <CartesianGrid strokeDasharray="3 3" stroke={C.border} vertical={false} />
                    <XAxis dataKey="year" stroke={C.muted} fontSize={10} />
                    <YAxis stroke={C.muted} fontSize={10} width={45} />
                    <Tooltip contentStyle={chartTip} cursor={{ fill:C.surface+"55" }} />
                    <Legend wrapperStyle={{ fontSize:11 }} />
                    <Bar dataKey="Equity" fill={C.green} radius={[3,3,0,0]} />
                    <Bar dataKey="Debt"   fill={C.red}   radius={[3,3,0,0]} />
                  </BarChart>
                </ResponsiveContainer>
              </Panel>
              <Panel>
                <div style={{ fontSize:13, fontWeight:700, marginBottom:12 }}>Cash Flow (₹ Cr)</div>
                <ResponsiveContainer width="100%" height={200}>
                  <BarChart data={cashSeries}>
                    <CartesianGrid strokeDasharray="3 3" stroke={C.border} vertical={false} />
                    <XAxis dataKey="year" stroke={C.muted} fontSize={10} />
                    <YAxis stroke={C.muted} fontSize={10} width={45} />
                    <Tooltip contentStyle={chartTip} cursor={{ fill:C.surface+"55" }} />
                    <Legend wrapperStyle={{ fontSize:11 }} />
                    <Bar dataKey="Op Cash Flow" fill={C.cyan}   radius={[3,3,0,0]} />
                    <Bar dataKey="FCF"          fill={C.indigo} radius={[3,3,0,0]} />
                  </BarChart>
                </ResponsiveContainer>
              </Panel>
            </div>

            {/* ── Shareholding + Analysts ── */}
            <div style={{ display:"grid", gridTemplateColumns:"repeat(auto-fit,minmax(300px,1fr))", gap:14 }}>
              <Panel>
                <div style={{ fontSize:13, fontWeight:700, marginBottom:12 }}>Shareholding Pattern</div>
                {ownPie.length>0 ? (
                  <ResponsiveContainer width="100%" height={210}>
                    <PieChart>
                      <Pie data={ownPie} dataKey="value" nameKey="name" cx="50%" cy="50%"
                        innerRadius={48} outerRadius={78} paddingAngle={2}
                        label={e=>`${e.name}: ${e.value}%`} labelLine={false} style={{ fontSize:10 }}>
                        {ownPie.map((e,i)=><Cell key={i} fill={PIE_COLORS[i%PIE_COLORS.length]} stroke={C.card} />)}
                      </Pie>
                      <Tooltip contentStyle={chartTip} />
                    </PieChart>
                  </ResponsiveContainer>
                ) : <div style={{ color:C.muted, fontSize:12, padding:20 }}>No shareholding data.</div>}
                {promoter && promoter.series && promoter.series.length>1 && (() => {
                  const first=N(promoter.series[0].pct), last=N(promoter.series[promoter.series.length-1].pct);
                  if(first==null||last==null) return null;
                  const diff=last-first;
                  return (
                    <div style={{ fontSize:11, color:C.muted, marginTop:8, textAlign:"center" }}>
                      Promoter: {first}% → {last}%{" "}
                      <span style={{ color:diff>=0?C.green:C.red, fontWeight:700 }}>
                        ({diff>=0?"▲":"▼"} {Math.abs(diff).toFixed(2)}%)
                      </span>
                    </div>
                  );
                })()}
              </Panel>
              <Panel>
                <div style={{ fontSize:13, fontWeight:700, marginBottom:12 }}>
                  Analyst Ratings <span style={{ fontSize:10, color:C.muted, fontWeight:400 }}>(context — not in score)</span></div>
                {(data.analyst||[]).some(a=>N(a.count)>0) ? (
                  <div style={{ display:"flex", flexDirection:"column", gap:8, marginTop:4 }}>
                    {data.analyst.map((a,i)=>{
                      const max = Math.max(...data.analyst.map(x=>N(x.count)||0), 1);
                      const cnt = N(a.count)||0;
                      const col = /strong buy/i.test(a.rating)?C.green:/buy/i.test(a.rating)?C.cyan:
                        /hold/i.test(a.rating)?C.amber:C.red;
                      return (
                        <div key={i} style={{ display:"flex", alignItems:"center", gap:9 }}>
                          <div style={{ fontSize:11, color:C.muted, width:78 }}>{a.rating}</div>
                          <div style={{ flex:1, background:C.surface, borderRadius:4, height:18, overflow:"hidden" }}>
                            <div style={{ width:`${100*cnt/max}%`, background:col, height:"100%",
                              transition:"width .5s ease" }} />
                          </div>
                          <div style={{ fontSize:11, color:C.text, width:22, textAlign:"right", fontWeight:700 }}>{cnt}</div>
                        </div>
                      );
                    })}
                  </div>
                ) : <div style={{ color:C.muted, fontSize:12, padding:20 }}>No analyst data.</div>}
              </Panel>
            </div>

            {/* ── Peers ── */}
            {(data.peers||[]).length>0 && (
              <Panel style={{ padding:0, overflow:"hidden" }}>
                <div style={{ fontSize:13, fontWeight:700, padding:"14px 16px 9px" }}>Peer Comparison</div>
                <div style={{ display:"grid", gridTemplateColumns:"1fr 78px 78px 78px 108px",
                  padding:"7px 16px", background:C.surface, borderBottom:`1px solid ${C.border}` }}>
                  {["Company","P/E","P/B","ROE","Market Cap"].map(h=>(
                    <div key={h} style={{ fontSize:9, color:C.muted, fontWeight:700, textTransform:"uppercase" }}>{h}</div>
                  ))}
                </div>
                {data.peers.map((p,i)=>(
                  <div key={i} style={{ display:"grid", gridTemplateColumns:"1fr 78px 78px 78px 108px",
                    padding:"9px 16px", borderBottom:i<data.peers.length-1?`1px solid ${C.border}`:"none", fontSize:11 }}>
                    <div style={{ color:C.text, overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap" }}>{p.name||"—"}</div>
                    <div style={{ fontFamily:MONO, color:C.muted }}>{num(p.pe)}</div>
                    <div style={{ fontFamily:MONO, color:C.muted }}>{num(p.pb,2)}</div>
                    <div style={{ fontFamily:MONO, color:C.muted }}>{pct(p.roe)}</div>
                    <div style={{ fontFamily:MONO, color:C.muted }}>{fmtCr(p.market_cap_cr)}</div>
                  </div>
                ))}
              </Panel>
            )}

            {/* ── Business ── */}
            {data.company_description && (
              <Panel>
                <div style={{ fontSize:13, fontWeight:700, marginBottom:8 }}>Business</div>
                <div style={{ fontSize:12, color:C.muted, lineHeight:1.65 }}>{data.company_description}</div>
              </Panel>
            )}
          </>
        );
      })()}

      {!data && !loading && !error && (
        <Panel style={{ textAlign:"center", padding:58 }}>
          <div style={{ fontSize:36, marginBottom:12 }}>🔬</div>
          <div style={{ fontSize:15, fontWeight:700, color:C.text, marginBottom:6 }}>Stock Deep Dives</div>
          <div style={{ fontSize:13, color:C.muted, lineHeight:1.6 }}>
            Search any stock for its full fundamental profile —<br/>
            score breakdown, financial trends, cash flow, ownership, and peers.
          </div>
        </Panel>
      )}
    </div>
  );
}

function RadarPanel() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [view, setView] = useState("buy");   // "buy" | "sell"

  const load = async (refresh=false) => {
    if (refresh) setRefreshing(true); else setLoading(true);
    const r = await api(`/api/radar${refresh ? "?refresh=true" : ""}`, { timeoutMs: 60000 });
    if (r && r.buy) setData(r);
    setLoading(false); setRefreshing(false);
  };
  useEffect(() => { load(false); }, []);

  const list = view === "buy" ? (data?.buy || []) : (data?.sell || []);
  const symbols = useMemo(() => list.map(x => x.symbol), [list]);
  const live = useLivePrices(symbols);

  const scoreColor = (s) => s>=75 ? C.green : s>=55 ? "#F59E0B" : C.muted;

  return (
    <div style={{ padding:20 }}>
      <div style={{ display:"flex", alignItems:"center", justifyContent:"space-between", marginBottom:14 }}>
        <div style={{ display:"flex", gap:8 }}>
          <button onClick={()=>setView("buy")} style={{
            padding:"7px 16px", borderRadius:8, border:`1px solid ${view==="buy"?C.green:C.border}`,
            background:view==="buy"?C.green+"18":"transparent", color:view==="buy"?C.green:C.muted,
            fontSize:12, fontWeight:700, cursor:"pointer" }}>
            🟢 BUY RADAR {data?.buy ? `(${data.buy.length})` : ""}
          </button>
          <button onClick={()=>setView("sell")} style={{
            padding:"7px 16px", borderRadius:8, border:`1px solid ${view==="sell"?C.red:C.border}`,
            background:view==="sell"?C.red+"18":"transparent", color:view==="sell"?C.red:C.muted,
            fontSize:12, fontWeight:700, cursor:"pointer" }}>
            🔴 SELL RADAR {data?.sell ? `(${data.sell.length})` : ""}
          </button>
        </div>
        <div style={{ display:"flex", alignItems:"center", gap:10 }}>
          {data?.date && <span style={{ fontSize:11, color:C.muted }}>
            Snapshot: {data.date} {data.generated_at ? "· " + new Date(data.generated_at).toLocaleTimeString() : ""}
          </span>}
          <button onClick={()=>load(true)} disabled={refreshing} style={{
            padding:"7px 14px", borderRadius:8, border:`1px solid ${C.border}`,
            background:"transparent", color:C.text, fontSize:12, fontWeight:600,
            cursor:refreshing?"default":"pointer", opacity:refreshing?0.5:1 }}>
            {refreshing ? "Recomputing…" : "↻ Refresh"}
          </button>
        </div>
      </div>

      <div style={{ fontSize:11, color:C.muted, marginBottom:14, lineHeight:1.5 }}>
        Screener, not an auto-trade signal. <b>Buy Radar</b> ranks fundamentally strong stocks
        (score ≥ 55) by how deep they've pulled back from their {data?.params?.lookback_days || 60}-day
        high. <b>Sell Radar</b> ranks the same universe by how strongly they've bounced off their
        {" "}{data?.params?.lookback_days || 60}-day low. Review before acting.
      </div>

      {loading ? (
        <div style={{ padding:40, textAlign:"center", color:C.muted, fontSize:13 }}>Loading radar…</div>
      ) : list.length === 0 ? (
        <div style={{ padding:40, textAlign:"center", color:C.muted, fontSize:13 }}>
          No candidates right now. Click Refresh to recompute.
        </div>
      ) : (
        <div style={{ background:C.panel, border:`1px solid ${C.border}`, borderRadius:10, overflow:"hidden" }}>
          <div style={{ display:"grid",
            gridTemplateColumns:"50px 80px 1fr 100px 90px 90px 90px 90px",
            padding:"9px 15px", background:C.surface, borderBottom:`1px solid ${C.border}` }}>
            {["#","Symbol","Name","Live Price",
              view==="buy"?"Fall %":"Bounce %","Fund. Score",view==="buy"?"Buy %":"Sell %","Range"].map(h=>(
              <div key={h} style={{ fontSize:10, fontWeight:700, color:C.muted, textTransform:"uppercase" }}>{h}</div>
            ))}
          </div>
          {list.map((s, i) => {
            const p = live[s.symbol];
            const score = view === "buy" ? s.buy_score : s.sell_score;
            const movePct = view === "buy" ? s.fall_pct : s.bounce_pct;
            return (
              <div key={s.symbol} style={{ display:"grid",
                gridTemplateColumns:"50px 80px 1fr 100px 90px 90px 90px 90px",
                padding:"9px 15px", borderBottom:`1px solid ${C.border}`, alignItems:"center" }}
                onMouseEnter={e=>e.currentTarget.style.background=C.surface+"88"}
                onMouseLeave={e=>e.currentTarget.style.background="transparent"}>
                <div style={{ fontSize:11, color:C.muted }}>{i+1}</div>
                <div style={{ fontSize:11, fontWeight:700, color:C.indigo }}>{s.symbol}</div>
                <div style={{ fontSize:11, color:C.text, overflow:"hidden",
                  textOverflow:"ellipsis", whiteSpace:"nowrap" }}>{s.name}</div>
                <div style={{ fontSize:11, fontFamily:MONO, color:C.text, fontWeight:700 }}>
                  {p && p.ltp!=null
                    ? `₹${Number(p.ltp).toLocaleString("en-IN",{minimumFractionDigits:2})}`
                    : `₹${s.last_close}`}
                  {p && p.live===false && <span style={{ fontSize:8, color:C.muted, marginLeft:3 }}>·close</span>}
                </div>
                <div style={{ fontSize:11, fontWeight:700, color:view==="buy"?C.red:C.green }}>
                  {view==="buy" ? "▼" : "▲"} {movePct}%
                </div>
                <div style={{ fontSize:11, color:C.text }}>{s.fundamental_score}</div>
                <div style={{ fontSize:12, fontWeight:800, color:scoreColor(score) }}>{score}%</div>
                <div style={{ fontSize:9, color:C.muted, fontFamily:MONO }}>
                  {s.recent_low}–{s.recent_high}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// US $ formatter: K/M/B/T abbreviation (raw USD stored, no Cr scaling - see
// data_us/fundamental_us.py note on why NSE's Cr-based fields aren't reused
// numerically for US, only the field NAMES for schema compatibility).
function fmtUSD(v) {
  if (v == null || isNaN(v)) return "-";
  const n = Number(v);
  const abs = Math.abs(n);
  if (abs >= 1e12) return "$" + (n/1e12).toFixed(2) + "T";
  if (abs >= 1e9)  return "$" + (n/1e9).toFixed(2) + "B";
  if (abs >= 1e6)  return "$" + (n/1e6).toFixed(2) + "M";
  if (abs >= 1e3)  return "$" + (n/1e3).toFixed(2) + "K";
  return "$" + n.toFixed(2);
}

function UniversePanelUS() {
  const [stocks, setStocks] = useState([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState("score");
  const [bucket, setBucket] = useState("ALL");   // market-cap segment tab
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [progress, setProgress] = useState(null);

  const load = async () => {
    setLoading(true);
    const r = await api("/api/us/universe");
    if (Array.isArray(r)) setStocks(r);
    setLoading(false);
  };
  useEffect(() => { load(); }, []);

  // Poll fetch progress while a refresh is running. The refresh itself runs in
  // a background thread on the backend (a full run takes many minutes), so we
  // poll rather than waiting on the POST response.
  useEffect(() => {
    let stop = false;
    const poll = async () => {
      const p = await api("/api/us/universe/progress");
      if (stop || !p) return;
      setProgress(p);
      if (!p.running && p.finished_at) {
        load();                 // refresh the table once the fetch completes
        setProgress(prev => prev && !prev.running ? prev : p);
      }
    };
    poll();
    const id = setInterval(poll, 2000);
    return () => { stop = true; clearInterval(id); };
  }, []);

  const doRefresh = async () => {
    setConfirmOpen(false);
    await api("/api/us/universe/refresh", { method:"POST" });
    const p = await api("/api/us/universe/progress");
    if (p) setProgress(p);
  };

  // Cooperative stop: the backend finishes and saves the symbol currently in
  // flight, then halts. Clicking Refresh afterwards resumes from that point,
  // because already-cached symbols are skipped.
  const stopFetch = async () => {
    const p = await api("/api/us/universe/stop", { method:"POST" });
    if (p) setProgress(p);
  };

  const refreshing = progress?.running;

  // US market-cap segments. Deliberately NOT a currency conversion of the NSE
  // thresholds: converting Rs 20,000 Cr gives ~$2.4B, which is a SMALL cap in
  // the US, not a large one. These are the standard US brackets, and the US
  // genuinely has a mega-cap tier (multi-trillion) that behaves differently
  // from a $12B large cap - lumping them together loses that distinction.
  // market_cap_cr holds RAW USD for US stocks (see data_us/fundamental_us.py).
  const sizeBucketUS = mc => {
    if (mc == null)      return "UNKNOWN";
    if (mc >= 200e9)     return "MEGA";    // > $200B
    if (mc >= 10e9)      return "LARGE";   // $10B - $200B
    if (mc >= 2e9)       return "MID";     // $2B - $10B
    if (mc >= 300e6)     return "SMALL";   // $300M - $2B
    return "MICRO";                         // < $300M
  };
  const BUCKETS_US = [
    { id:"ALL",   label:"All"   },
    { id:"MEGA",  label:"Mega"  },   // > $200B
    { id:"LARGE", label:"Large" },   // $10B - $200B
    { id:"MID",   label:"Mid"   },   // $2B - $10B
    { id:"SMALL", label:"Small" },   // $300M - $2B
    { id:"MICRO", label:"Micro" },   // < $300M
  ];
  const bucketCountUS = id => id === "ALL"
    ? stocks.length
    : stocks.filter(s => sizeBucketUS(s.market_cap_cr) === id).length;

  const filtered = useMemo(() => {
    // Segment first, so ranking is like-against-like, then search. Searching
    // must not change a stock's rank within its segment.
    let list = stocks.filter(s => bucket === "ALL" || sizeBucketUS(s.market_cap_cr) === bucket);
    list = [...list].sort((a,b) => (b[sortKey]||0) - (a[sortKey]||0));
    if (search.trim()) {
      const q = search.trim().toUpperCase();
      list = list.filter(s => s.symbol.includes(q) || (s.name||"").toUpperCase().includes(q));
    }
    return list;
  }, [stocks, search, sortKey, bucket]);

  return (
    <div style={{ padding:20 }}>
      <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:14 }}>
        <input placeholder="Search US symbol or name..." value={search}
          onChange={e=>setSearch(e.target.value)}
          style={{ padding:"8px 12px", borderRadius:8, border:`1px solid ${C.border}`,
            background:C.panel, color:C.text, fontSize:12, width:280 }} />
        <div style={{ display:"flex", gap:8 }}>
          <button onClick={()=>setConfirmOpen(true)} disabled={refreshing} style={{
            padding:"8px 16px", borderRadius:8, border:`1px solid ${C.border}`,
            background:"transparent", color:C.text, fontSize:12, fontWeight:600,
            cursor:refreshing?"default":"pointer", opacity:refreshing?0.5:1 }}>
            {refreshing ? "Fetching..." : "Refresh from Finnhub"}
          </button>
          {refreshing && (
            <button onClick={stopFetch} disabled={progress?.stopping} style={{
              padding:"8px 16px", borderRadius:8, border:`1px solid ${C.red}`,
              background:C.red+"18", color:C.red, fontSize:12, fontWeight:700,
              cursor:progress?.stopping?"default":"pointer",
              opacity:progress?.stopping?0.6:1 }}>
              {progress?.stopping ? "Stopping..." : "■ Stop"}
            </button>
          )}
        </div>
      </div>

      {/* Confirm dialog - a full refresh consumes API quota and takes minutes,
          so it should never fire from a stray click. Existing cached data is
          NEVER wiped: each symbol is written atomically as it's fetched, and
          already-fresh symbols are skipped entirely. */}
      {confirmOpen && (
        <div style={{ position:"fixed", inset:0, background:"#0008", zIndex:999,
          display:"flex", alignItems:"center", justifyContent:"center" }}
          onClick={()=>setConfirmOpen(false)}>
          <div onClick={e=>e.stopPropagation()} style={{ background:C.panel,
            border:`1px solid ${C.border}`, borderRadius:12, padding:24, width:460 }}>
            <div style={{ fontSize:15, fontWeight:800, color:C.text, marginBottom:10 }}>
              Refresh US fundamentals from Finnhub?
            </div>
            <div style={{ fontSize:12, color:C.muted, lineHeight:1.6, marginBottom:18 }}>
              This fetches fundamentals for the whole US universe and can take
              several minutes, consuming Finnhub API quota.
              <br /><br />
              <b style={{ color:C.text }}>Your existing data is safe.</b> Symbols already
              cached and fresh are skipped, and every symbol is written to disk the
              moment it's fetched — interrupting the run never loses data.
            </div>
            <div style={{ display:"flex", gap:10, justifyContent:"flex-end" }}>
              <button onClick={()=>setConfirmOpen(false)} style={{
                padding:"8px 16px", borderRadius:8, border:`1px solid ${C.border}`,
                background:"transparent", color:C.muted, fontSize:12,
                fontWeight:600, cursor:"pointer" }}>Cancel</button>
              <button onClick={doRefresh} style={{
                padding:"8px 18px", borderRadius:8, border:"none",
                background:C.indigo, color:"#fff", fontSize:12,
                fontWeight:700, cursor:"pointer" }}>Yes, refresh</button>
            </div>
          </div>
        </div>
      )}

      {/* Live fetch progress */}
      {progress && (progress.running || progress.finished_at) && (
        <div style={{ background:C.panel, border:`1px solid ${C.border}`,
          borderRadius:10, padding:"12px 16px", marginBottom:14 }}>
          <div style={{ display:"flex", justifyContent:"space-between",
            alignItems:"center", marginBottom:8 }}>
            <span style={{ fontSize:12, fontWeight:700,
              color:progress.running ? C.indigo : C.green }}>
              {progress.running ? "Fetching from FMP..." : "Last fetch complete"}
            </span>
            <span style={{ fontSize:11, color:C.muted, fontFamily:MONO }}>
              {progress.done}/{progress.total} · {progress.pct}%
            </span>
          </div>
          <div style={{ height:6, background:C.surface, borderRadius:3, overflow:"hidden" }}>
            <div style={{ height:"100%", width:`${progress.pct||0}%`,
              background:progress.running ? C.indigo : C.green,
              transition:"width .4s ease" }} />
          </div>
          <div style={{ display:"flex", gap:16, marginTop:8, fontSize:10, color:C.muted }}>
            <span>✓ {progress.fetched} fetched</span>
            <span>✕ {progress.failed} failed</span>
            {progress.current && <span>Current: <b style={{color:C.text}}>{progress.current}</b></span>}
          </div>
          {progress.message && (
            <div style={{ fontSize:10, color:C.muted, marginTop:6 }}>{progress.message}</div>
          )}
        </div>
      )}

      {/* Market-cap segment tabs - rank like against like */}
      <div style={{ background:C.panel, border:`1px solid ${C.border}`, borderRadius:10,
        display:"flex", gap:7, alignItems:"center", flexWrap:"wrap",
        padding:"9px 11px", marginBottom:14 }}>
        <span style={{ fontSize:9, color:C.muted, fontWeight:700,
          textTransform:"uppercase", letterSpacing:.5, marginRight:4 }}>Segment</span>
        {BUCKETS_US.map(b => {
          const active = bucket === b.id;
          return (
            <button key={b.id} onClick={()=>setBucket(b.id)}
              style={{ cursor:"pointer", border:`1px solid ${active?C.cyan:C.border}`,
                background:active?C.cyan+"22":"transparent",
                color:active?C.cyan:C.muted, borderRadius:7, padding:"5px 12px",
                fontSize:11, fontWeight:700, fontFamily:MONO,
                display:"flex", alignItems:"center", gap:6 }}>
              {b.label}
              <span style={{ fontSize:9, color:active?C.cyan:C.muted, opacity:.8,
                fontWeight:600 }}>{bucketCountUS(b.id)}</span>
            </button>
          );
        })}
      </div>

      {loading ? (
        <div style={{ padding:40, textAlign:"center", color:C.muted, fontSize:13 }}>Loading US universe...</div>
      ) : filtered.length === 0 ? (
        <div style={{ padding:40, textAlign:"center", color:C.muted, fontSize:13 }}>
          No US stocks cached yet. Click "Refresh from Finnhub" to fetch (requires FINNHUB_API_KEY configured).
        </div>
      ) : (
        <div style={{ background:C.panel, border:`1px solid ${C.border}`, borderRadius:10, overflow:"hidden" }}>
          <div style={{ display:"grid", gridTemplateColumns:"50px 80px 1fr 110px 80px 70px 90px 70px",
            padding:"9px 15px", background:C.surface, borderBottom:`1px solid ${C.border}` }}>
            {["#","Symbol","Name","Market Cap","ROE","D/E","Rev Gr.","Score"].map(h=>(
              <div key={h} style={{ fontSize:10, fontWeight:700, color:C.muted, textTransform:"uppercase" }}>{h}</div>
            ))}
          </div>
          <div style={{ maxHeight:500, overflowY:"auto" }}>
            {filtered.map((s, i) => (
              <div key={s.symbol} style={{ display:"grid",
                gridTemplateColumns:"50px 80px 1fr 110px 80px 70px 90px 70px",
                padding:"8px 15px", borderBottom:`1px solid ${C.border}`, alignItems:"center" }}
                onMouseEnter={e=>e.currentTarget.style.background=C.surface+"88"}
                onMouseLeave={e=>e.currentTarget.style.background="transparent"}>
                <div style={{ fontSize:11, color:C.muted }}>{i+1}</div>
                <div style={{ fontSize:11, fontWeight:700, color:C.indigo }}>{s.symbol}</div>
                <div style={{ fontSize:11, color:C.text, overflow:"hidden",
                  textOverflow:"ellipsis", whiteSpace:"nowrap" }}>{s.name||s.symbol}</div>
                <div style={{ fontSize:11, color:C.text, fontFamily:MONO }}>{fmtUSD(s.market_cap_cr)}</div>
                <div style={{ fontSize:11, color:C.text }}>{s.roe!=null?s.roe.toFixed(1)+"%":"-"}</div>
                <div style={{ fontSize:11, color:C.text }}>{s.debt_to_equity!=null?s.debt_to_equity.toFixed(2):"-"}</div>
                <div style={{ fontSize:11, color:C.text }}>{s.revenue_growth!=null?s.revenue_growth.toFixed(1)+"%":"-"}</div>
                <div style={{ fontSize:12, fontWeight:800,
                  color:s.score>=75?C.green:s.score>=55?"#F59E0B":C.muted }}>{s.score}</div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function RadarPanelUS() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [view, setView] = useState("buy");

  const load = async (refresh=false) => {
    if (refresh) setRefreshing(true); else setLoading(true);
    const r = await api(`/api/us/radar${refresh ? "?refresh=true" : ""}`, { timeoutMs: 120000 });
    if (r && r.buy) setData(r);
    setLoading(false); setRefreshing(false);
  };
  useEffect(() => { load(false); }, []);

  const list = view === "buy" ? (data?.buy || []) : (data?.sell || []);
  const scoreColor = (s) => s>=75 ? C.green : s>=55 ? "#F59E0B" : C.muted;

  return (
    <div style={{ padding:20 }}>
      <div style={{ display:"flex", alignItems:"center", justifyContent:"space-between", marginBottom:14 }}>
        <div style={{ display:"flex", gap:8 }}>
          <button onClick={()=>setView("buy")} style={{
            padding:"7px 16px", borderRadius:8, border:`1px solid ${view==="buy"?C.green:C.border}`,
            background:view==="buy"?C.green+"18":"transparent", color:view==="buy"?C.green:C.muted,
            fontSize:12, fontWeight:700, cursor:"pointer" }}>
            🟢 BUY RADAR (US) {data?.buy ? `(${data.buy.length})` : ""}
          </button>
          <button onClick={()=>setView("sell")} style={{
            padding:"7px 16px", borderRadius:8, border:`1px solid ${view==="sell"?C.red:C.border}`,
            background:view==="sell"?C.red+"18":"transparent", color:view==="sell"?C.red:C.muted,
            fontSize:12, fontWeight:700, cursor:"pointer" }}>
            🔴 SELL RADAR (US) {data?.sell ? `(${data.sell.length})` : ""}
          </button>
        </div>
        <button onClick={()=>load(true)} disabled={refreshing} style={{
          padding:"7px 14px", borderRadius:8, border:`1px solid ${C.border}`,
          background:"transparent", color:C.text, fontSize:12, fontWeight:600,
          cursor:refreshing?"default":"pointer", opacity:refreshing?0.5:1 }}>
          {refreshing ? "Recomputing..." : "Refresh"}
        </button>
      </div>

      {loading ? (
        <div style={{ padding:40, textAlign:"center", color:C.muted, fontSize:13 }}>Loading US radar...</div>
      ) : list.length === 0 ? (
        <div style={{ padding:40, textAlign:"center", color:C.muted, fontSize:13 }}>
          No candidates. Click Refresh (requires US universe to be populated first).
        </div>
      ) : (
        <div style={{ background:C.panel, border:`1px solid ${C.border}`, borderRadius:10, overflow:"hidden" }}>
          <div style={{ display:"grid", gridTemplateColumns:"50px 80px 1fr 90px 90px 90px 90px",
            padding:"9px 15px", background:C.surface, borderBottom:`1px solid ${C.border}` }}>
            {["#","Symbol","Name",view==="buy"?"Fall %":"Bounce %","Fund. Score",
              view==="buy"?"Buy %":"Sell %","Range"].map(h=>(
              <div key={h} style={{ fontSize:10, fontWeight:700, color:C.muted, textTransform:"uppercase" }}>{h}</div>
            ))}
          </div>
          {list.map((s, i) => {
            const score = view === "buy" ? s.buy_score : s.sell_score;
            const movePct = view === "buy" ? s.fall_pct : s.bounce_pct;
            return (
              <div key={s.symbol} style={{ display:"grid",
                gridTemplateColumns:"50px 80px 1fr 90px 90px 90px 90px",
                padding:"9px 15px", borderBottom:`1px solid ${C.border}`, alignItems:"center" }}>
                <div style={{ fontSize:11, color:C.muted }}>{i+1}</div>
                <div style={{ fontSize:11, fontWeight:700, color:C.indigo }}>{s.symbol}</div>
                <div style={{ fontSize:11, color:C.text, overflow:"hidden",
                  textOverflow:"ellipsis", whiteSpace:"nowrap" }}>{s.name}</div>
                <div style={{ fontSize:11, fontWeight:700, color:view==="buy"?C.red:C.green }}>
                  {view==="buy" ? "▼" : "▲"} {movePct}%
                </div>
                <div style={{ fontSize:11, color:C.text }}>{s.fundamental_score}</div>
                <div style={{ fontSize:12, fontWeight:800, color:scoreColor(score) }}>{score}%</div>
                <div style={{ fontSize:9, color:C.muted, fontFamily:MONO }}>
                  ${s.recent_low}-${s.recent_high}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function DeepDivePanelUS() {
  const [symbol, setSymbolInput] = useState("AAPL");
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const fetchStock = async (sym) => {
    setLoading(true); setError(""); setData(null);
    try {
      const r = await api(`/api/us/stock/deep-dive/${encodeURIComponent(sym)}`, { timeoutMs: 30000 });
      if (r) setData(r); else setError("No data found for " + sym);
    } catch (e) {
      setError("Failed to load " + sym);
    }
    setLoading(false);
  };

  return (
    <div style={{ padding:20 }}>
      <div style={{ display:"flex", gap:10, marginBottom:20 }}>
        <input value={symbol} onChange={e=>setSymbolInput(e.target.value.toUpperCase())}
          onKeyDown={e=>e.key==="Enter" && fetchStock(symbol)}
          placeholder="AAPL, MSFT, GOOGL..."
          style={{ padding:"10px 14px", borderRadius:8, border:`1px solid ${C.border}`,
            background:C.panel, color:C.text, fontSize:13, width:220 }} />
        <button onClick={()=>fetchStock(symbol)} disabled={loading} style={{
          padding:"10px 20px", borderRadius:8, border:"none", background:C.indigo,
          color:"#fff", fontSize:13, fontWeight:700, cursor:loading?"default":"pointer" }}>
          {loading ? "Loading..." : "Search"}
        </button>
      </div>

      {error && <div style={{ color:C.red, fontSize:13, marginBottom:14 }}>{error}</div>}

      {data && (
        <div style={{ background:C.panel, border:`1px solid ${C.border}`, borderRadius:12, padding:24 }}>
          <div style={{ display:"flex", alignItems:"baseline", gap:12, flexWrap:"wrap" }}>
            <div style={{ fontSize:20, fontWeight:800, color:C.text }}>{data.symbol}</div>
            {data.extras?.price != null && (
              <span style={{ fontSize:16, fontWeight:700, fontFamily:MONO,
                color:(data.extras.price_change_pct||0)>=0?C.green:C.red }}>
                ${Number(data.extras.price).toFixed(2)}
                <span style={{ fontSize:11, marginLeft:5 }}>
                  {(data.extras.price_change_pct||0)>=0?"▲":"▼"}{Math.abs(data.extras.price_change_pct||0).toFixed(2)}%
                </span>
              </span>
            )}
          </div>
          <div style={{ fontSize:13, color:C.muted, marginBottom:4 }}>{data.name}</div>
          {data.extras?.industry && (
            <div style={{ fontSize:11, color:C.muted, marginBottom:16 }}>
              {data.extras.industry}{data.extras.exchange ? ` · ${data.extras.exchange}` : ""}
              {data.extras.ipo ? ` · IPO ${data.extras.ipo}` : ""}
            </div>
          )}

          <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr 1fr 1fr", gap:16 }}>
            <div>
              <div style={{ fontSize:10, color:C.muted, textTransform:"uppercase" }}>Market Cap</div>
              <div style={{ fontSize:16, fontWeight:700, color:C.text }}>{fmtUSD(data.market_cap_cr)}</div>
            </div>
            <div>
              <div style={{ fontSize:10, color:C.muted, textTransform:"uppercase" }}>ROE</div>
              <div style={{ fontSize:16, fontWeight:700, color:C.text }}>{data.roe!=null?data.roe.toFixed(1)+"%":"-"}</div>
            </div>
            <div>
              <div style={{ fontSize:10, color:C.muted, textTransform:"uppercase" }}>Debt/Equity</div>
              <div style={{ fontSize:16, fontWeight:700, color:C.text }}>{data.debt_to_equity!=null?data.debt_to_equity.toFixed(2):"-"}</div>
            </div>
            <div>
              <div style={{ fontSize:10, color:C.muted, textTransform:"uppercase" }}>P/E Ratio</div>
              <div style={{ fontSize:16, fontWeight:700, color:C.text }}>{data.pe_ratio!=null?data.pe_ratio.toFixed(1):"-"}</div>
            </div>
            <div>
              <div style={{ fontSize:10, color:C.muted, textTransform:"uppercase" }}>Revenue</div>
              <div style={{ fontSize:16, fontWeight:700, color:C.text }}>{fmtUSD(data.revenue_cr)}</div>
            </div>
            <div>
              <div style={{ fontSize:10, color:C.muted, textTransform:"uppercase" }}>Net Income</div>
              <div style={{ fontSize:16, fontWeight:700, color:C.text }}>{fmtUSD(data.net_income_cr)}</div>
            </div>
            <div>
              <div style={{ fontSize:10, color:C.muted, textTransform:"uppercase" }}>Revenue Growth</div>
              <div style={{ fontSize:16, fontWeight:700, color:C.text }}>{data.revenue_growth!=null?data.revenue_growth.toFixed(1)+"%":"-"}</div>
            </div>
            <div>
              <div style={{ fontSize:10, color:C.muted, textTransform:"uppercase" }}>Score</div>
              <div style={{ fontSize:16, fontWeight:800,
                color:data.score>=75?C.green:data.score>=55?"#F59E0B":C.muted }}>{data.score}</div>
            </div>
          </div>

          {/* Multi-year derived metrics - the things a single snapshot can't show */}
          {data.extras && (data.extras.years_of_history || data.extras.fcf != null) && (
            <div style={{ marginTop:22, paddingTop:18, borderTop:`1px solid ${C.border}` }}>
              <div style={{ fontSize:11, fontWeight:700, color:C.text, marginBottom:12,
                textTransform:"uppercase", letterSpacing:.5 }}>Multi-Year Quality</div>
              <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr 1fr 1fr", gap:16 }}>
                {[
                  ["Years of History", data.extras.years_of_history],
                  ["Profitable Years", data.extras.profitable_years_frac != null
                      ? (data.extras.profitable_years_frac*100).toFixed(0)+"%" : null],
                  ["Revenue CAGR", data.extras.revenue_cagr != null
                      ? data.extras.revenue_cagr.toFixed(2)+"%" : null],
                  ["Growth Consistency", data.extras.revenue_growth_consistency != null
                      ? (data.extras.revenue_growth_consistency*100).toFixed(0)+"%" : null],
                  ["Free Cash Flow", data.extras.fcf != null ? fmtUSD(data.extras.fcf) : null],
                  ["FCF Margin", data.extras.fcf_margin != null
                      ? data.extras.fcf_margin.toFixed(2)+"%" : null],
                  ["Operating Margin", data.extras.operating_margin != null
                      ? data.extras.operating_margin.toFixed(2)+"%" : null],
                  ["Debt Trend", data.extras.debt_trend_pct != null
                      ? (data.extras.debt_trend_pct>=0?"+":"")+data.extras.debt_trend_pct.toFixed(1)+"%" : null],
                ].map(([label, val]) => (
                  <div key={label}>
                    <div style={{ fontSize:10, color:C.muted, textTransform:"uppercase" }}>{label}</div>
                    <div style={{ fontSize:14, fontWeight:700, color:C.text }}>{val ?? "-"}</div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Score breakdown - makes a score auditable instead of a black box */}
          {data.extras?.score_breakdown && (
            <div style={{ marginTop:22, paddingTop:18, borderTop:`1px solid ${C.border}` }}>
              <div style={{ fontSize:11, fontWeight:700, color:C.text, marginBottom:12,
                textTransform:"uppercase", letterSpacing:.5 }}>Score Breakdown</div>
              {Object.entries(data.extras.score_breakdown).map(([cat, d]) => (
                <div key={cat} style={{ display:"flex", alignItems:"center", gap:10, marginBottom:7 }}>
                  <div style={{ fontSize:11, color:C.muted, width:150, textTransform:"capitalize" }}>
                    {cat.replace(/_/g," ")}
                  </div>
                  <div style={{ flex:1, height:6, background:C.surface, borderRadius:3, overflow:"hidden" }}>
                    <div style={{ height:"100%", width:`${d.max? (d.points/d.max*100):0}%`,
                      background:(d.points/d.max)>=0.7?C.green:(d.points/d.max)>=0.4?"#F59E0B":C.red }} />
                  </div>
                  <div style={{ fontSize:11, fontFamily:MONO, color:C.text, width:64,
                    textAlign:"right" }}>{d.points}/{d.max}</div>
                </div>
              ))}
            </div>
          )}

          {/* Sentiment - shown but deliberately NOT part of the score */}
          {(data.extras?.analyst || (data.extras?.insider_recent||[]).length>0) && (
            <div style={{ marginTop:22, paddingTop:18, borderTop:`1px solid ${C.border}` }}>
              <div style={{ fontSize:11, fontWeight:700, color:C.text, marginBottom:4,
                textTransform:"uppercase", letterSpacing:.5 }}>Sentiment</div>
              <div style={{ fontSize:10, color:C.muted, marginBottom:12 }}>
                Shown for context only — deliberately excluded from the quality score.
              </div>
              {data.extras.analyst && (
                <div style={{ display:"flex", gap:8, marginBottom:12, flexWrap:"wrap" }}>
                  {[["Strong Buy",data.extras.analyst.strongBuy,C.green],
                    ["Buy",data.extras.analyst.buy,C.green],
                    ["Hold",data.extras.analyst.hold,"#F59E0B"],
                    ["Sell",data.extras.analyst.sell,C.red],
                    ["Strong Sell",data.extras.analyst.strongSell,C.red]].map(([l,v,col])=>(
                    <div key={l} style={{ padding:"5px 11px", borderRadius:7,
                      border:`1px solid ${C.border}`, fontSize:11, color:C.muted }}>
                      {l}: <b style={{ color:col }}>{v ?? 0}</b>
                    </div>
                  ))}
                </div>
              )}
              {(data.extras.insider_recent||[]).length>0 && (
                <div>
                  <div style={{ fontSize:10, color:C.muted, marginBottom:6 }}>
                    Recent insider transactions
                  </div>
                  {data.extras.insider_recent.slice(0,5).map((t,i)=>(
                    <div key={i} style={{ display:"flex", gap:12, fontSize:11,
                      color:C.muted, padding:"3px 0" }}>
                      <span style={{ width:150, color:C.text, overflow:"hidden",
                        textOverflow:"ellipsis", whiteSpace:"nowrap" }}>{t.name}</span>
                      <span style={{ width:88, color:(t.change||0)>=0?C.green:C.red }}>
                        {(t.change||0)>=0?"+":""}{Number(t.change||0).toLocaleString()}
                      </span>
                      <span style={{ width:78 }}>{t.transactionPrice? "$"+t.transactionPrice : "-"}</span>
                      <span>{t.transactionDate}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function UniversePanel() {
  const [stocks,   setStocks]   = useState([]);
  const [loading,  setLoading]  = useState(true);
  const [search,   setSearch]   = useState("");
  const [sort,     setSort]     = useState("score");
  const [bucket,   setBucket]   = useState("ALL");   // size segment tab
  const [visible,  setVisible]  = useState(() => new Set());   // symbols currently on screen
  const rowObs = useRef(null);
  const observeRow = useCallback((el, sym) => {
    if (!el || !rowObs.current) return;
    el.dataset.sym = sym;
    rowObs.current.observe(el);
  }, []);

  useEffect(()=>{
    api("/api/universe").then(d=>{
      if(Array.isArray(d)) setStocks(d);
      setLoading(false);
    });
  },[]);

  // IntersectionObserver: track which rows are visible, stream only those.
  useEffect(() => {
    const obs = new IntersectionObserver((entries) => {
      setVisible(prev => {
        const next = new Set(prev);
        for (const e of entries) {
          const sym = e.target.dataset.sym;
          if (!sym) continue;
          if (e.isIntersecting) next.add(sym); else next.delete(sym);
        }
        return next;
      });
    }, { root: null, rootMargin: "120px", threshold: 0 });
    rowObs.current = obs;
    return () => obs.disconnect();
  }, []);
  const livePrices = useLivePrices(useMemo(() => Array.from(visible), [visible]));

  const [confirmOpen, setConfirmOpen] = useState(false);
  const [progress, setProgress] = useState(null);

  // Poll scan progress. The refresh runs in a background thread on the backend
  // (a full scan takes a long time), so we poll rather than await the POST.
  useEffect(() => {
    let stop = false;
    const poll = async () => {
      const p = await api("/api/universe/progress");
      if (stop || !p) return;
      setProgress(p);
      if (!p.running && p.finished_at) {
        const d = await api("/api/universe");
        if (Array.isArray(d)) setStocks(d);
      }
    };
    poll();
    const id = setInterval(poll, 2000);
    return () => { stop = true; clearInterval(id); };
  }, []);

  const refresh = async () => {
    setConfirmOpen(false);
    await api("/api/universe/refresh", { method:"POST" });
    const p = await api("/api/universe/progress");
    if (p) setProgress(p);
  };

  // Cooperative stop - the in-flight symbol is saved before halting, so no work
  // is lost. Refresh resumes from where it stopped.
  const stopScan = async () => {
    const p = await api("/api/universe/stop", { method:"POST" });
    if (p) setProgress(p);
  };
  const refreshing = progress?.running;

  // Market-cap segmentation (₹ crore). Judge like against like: a ₹305Cr micro-cap
  // shouldn't out-rank a ₹10,000Cr mid-cap on the same board.
  const sizeBucket = mc => {
    if (mc == null)   return "UNKNOWN";
    if (mc > 20000)   return "LARGE";
    if (mc >= 5000)   return "MID";
    if (mc >= 1000)   return "SMALL";
    return "MICRO";
  };
  const BUCKETS = [
    { id:"ALL",   label:"All"    },
    { id:"LARGE", label:"Large"  },   // > ₹20,000 Cr
    { id:"MID",   label:"Mid"    },   // ₹5,000–20,000 Cr
    { id:"SMALL", label:"Small"  },   // ₹1,000–5,000 Cr
    { id:"MICRO", label:"Micro"  },   // < ₹1,000 Cr
  ];
  const bucketCount = id => id==="ALL"
    ? stocks.length
    : stocks.filter(s=>sizeBucket(s.market_cap_cr)===id).length;

  // Rank is computed on the segment + sort BEFORE the search filter, so a searched
  // stock keeps its true position in the ranking (previously it always showed 1
  // because the index came from the filtered result list).
  const ranked = stocks
    .filter(s=>bucket==="ALL" || sizeBucket(s.market_cap_cr)===bucket)
    .sort((a,b)=>(b[sort]||0)-(a[sort]||0))
    .map((s,i)=>({ ...s, _rank:i+1 }));

  const shown = ranked
    .filter(s=>(s.symbol+s.name).toLowerCase().includes(search.toLowerCase()))
    .slice(0,5000);

  const scoreColor = s => s>=70?C.green:s>=50?C.cyan:s>=30?C.amber:C.red;

  return (
    <div style={{ display:"flex", flexDirection:"column", gap:13 }}>
      <Panel style={{ display:"flex", gap:11, alignItems:"center", flexWrap:"wrap" }}>
        <div style={{ flex:"1 1 200px" }}>
          <SLabel>Search Stocks</SLabel>
          <input value={search} onChange={e=>setSearch(e.target.value)}
            placeholder="Symbol or name..."
            style={{ ...sel, width:"100%", boxSizing:"border-box" }} />
        </div>
        <div><SLabel>Sort By</SLabel>
          <select value={sort} onChange={e=>setSort(e.target.value)} style={sel}>
            <option value="score">Score</option>
            <option value="market_cap_cr">Market Cap</option>
            <option value="roe">ROE</option>
          </select></div>
        <div style={{ paddingTop:18 }}>
          <Btn onClick={()=>setConfirmOpen(true)} color={C.amber}>
            {refreshing ? "⏳ Fetching..." : "🔄 Refresh Data"}
          </Btn>
          {refreshing && (
            <Btn onClick={stopScan} color={C.red}>
              {progress?.stopping ? "Stopping..." : "■ Stop"}
            </Btn>
          )}

      {/* Confirm dialog - a full NSE scan hits IndianAPI for thousands of stocks
          and takes a long time. Existing cached data is NEVER wiped: each symbol
          is written atomically as fetched, and fresh symbols are skipped. */}
      {confirmOpen && (
        <div style={{ position:"fixed", inset:0, background:"#0008", zIndex:999,
          display:"flex", alignItems:"center", justifyContent:"center" }}
          onClick={()=>setConfirmOpen(false)}>
          <div onClick={e=>e.stopPropagation()} style={{ background:C.panel,
            border:`1px solid ${C.border}`, borderRadius:12, padding:24, width:460 }}>
            <div style={{ fontSize:15, fontWeight:800, color:C.text, marginBottom:10 }}>
              Refresh NSE fundamentals?
            </div>
            <div style={{ fontSize:12, color:C.muted, lineHeight:1.6, marginBottom:18 }}>
              This scans the NSE universe via IndianAPI and can take a long time,
              consuming API quota.
              <br /><br />
              <b style={{ color:C.text }}>Your existing data is safe.</b> Symbols already
              cached and fresh are skipped, and every symbol is written to disk the
              moment it's fetched - interrupting the run never loses data.
            </div>
            <div style={{ display:"flex", gap:10, justifyContent:"flex-end" }}>
              <button onClick={()=>setConfirmOpen(false)} style={{
                padding:"8px 16px", borderRadius:8, border:`1px solid ${C.border}`,
                background:"transparent", color:C.muted, fontSize:12,
                fontWeight:600, cursor:"pointer" }}>Cancel</button>
              <button onClick={refresh} style={{
                padding:"8px 18px", borderRadius:8, border:"none",
                background:C.indigo, color:"#fff", fontSize:12,
                fontWeight:700, cursor:"pointer" }}>Yes, refresh</button>
            </div>
          </div>
        </div>
      )}

      {progress && (progress.running || progress.finished_at) && (
        <div style={{ background:C.panel, border:`1px solid ${C.border}`,
          borderRadius:10, padding:"12px 16px", margin:"12px 0" }}>
          <div style={{ display:"flex", justifyContent:"space-between",
            alignItems:"center", marginBottom:8 }}>
            <span style={{ fontSize:12, fontWeight:700,
              color:progress.running ? C.indigo : C.green }}>
              {progress.running ? "Scanning NSE fundamentals..." : "Last scan complete"}
            </span>
            <span style={{ fontSize:11, color:C.muted, fontFamily:MONO }}>
              {progress.done}/{progress.total} · {progress.pct}%
            </span>
          </div>
          <div style={{ height:6, background:C.surface, borderRadius:3, overflow:"hidden" }}>
            <div style={{ height:"100%", width:`${progress.pct||0}%`,
              background:progress.running ? C.indigo : C.green,
              transition:"width .4s ease" }} />
          </div>
          <div style={{ display:"flex", gap:16, marginTop:8, fontSize:10, color:C.muted }}>
            <span>✓ {progress.fetched} fetched</span>
            <span>✕ {progress.failed} failed</span>
            {progress.current && <span>Current: <b style={{color:C.text}}>{progress.current}</b></span>}
          </div>
          {progress.message && (
            <div style={{ fontSize:10, color:C.muted, marginTop:6 }}>{progress.message}</div>
          )}
        </div>
      )}
        </div>
        <div style={{ paddingTop:18, fontSize:11, color:C.muted }}>
          {stocks.length} stocks approved · Updated weekly
        </div>
      </Panel>

      {/* Market-cap segment tabs — rank like against like */}
      <Panel style={{ display:"flex", gap:7, alignItems:"center", flexWrap:"wrap",
        padding:"9px 11px" }}>
        <span style={{ fontSize:9, color:C.muted, fontWeight:700,
          textTransform:"uppercase", letterSpacing:.5, marginRight:4 }}>Segment</span>
        {BUCKETS.map(b=>{
          const active = bucket===b.id;
          return (
            <button key={b.id} onClick={()=>setBucket(b.id)}
              style={{ cursor:"pointer", border:`1px solid ${active?C.cyan:C.border}`,
                background:active?C.cyan+"22":"transparent",
                color:active?C.cyan:C.muted, borderRadius:7, padding:"5px 12px",
                fontSize:11, fontWeight:700, fontFamily:MONO,
                display:"flex", alignItems:"center", gap:6 }}>
              {b.label}
              <span style={{ fontSize:9, color:active?C.cyan:C.muted, opacity:.8,
                fontWeight:600 }}>{bucketCount(b.id)}</span>
            </button>
          );
        })}
      </Panel>

      {loading ? (
        <Panel style={{ textAlign:"center", padding:44 }}>
          <div style={{ color:C.muted }}>Loading fundamental universe...</div>
        </Panel>
      ) : (
        <Panel style={{ padding:0, overflow:"hidden" }}>
          <div style={{ display:"grid",
            gridTemplateColumns:"50px 80px 1fr 100px 90px 70px 70px 80px 80px 70px",
            padding:"9px 15px", background:C.surface, borderBottom:`1px solid ${C.border}` }}>
            {["Sr No","Symbol","Name","Live Price","Market Cap","ROE","D/E","Promoter","Rev Gr.","Score"].map(h=>(
              <div key={h} style={{ fontSize:9, color:C.muted, fontWeight:700,
                textTransform:"uppercase", letterSpacing:.5 }}>{h}</div>
            ))}
          </div>
          <div style={{ maxHeight:500, overflowY:"auto" }}>
            {shown.map(s=>(
              <div key={s.symbol} ref={el=>observeRow(el, s.symbol)} style={{ display:"grid",
                gridTemplateColumns:"50px 80px 1fr 100px 90px 70px 70px 80px 80px 70px",
                padding:"8px 15px", borderBottom:`1px solid ${C.border}`,
                alignItems:"center" }}
                onMouseEnter={e=>e.currentTarget.style.background=C.surface+"88"}
                onMouseLeave={e=>e.currentTarget.style.background="transparent"}>
                <div style={{ fontSize:11, color:C.muted }}>{s._rank}</div>
                <div style={{ fontFamily:MONO, fontSize:12, color:C.cyan,
                  fontWeight:700 }}>{s.symbol}</div>
                <div style={{ fontSize:11, color:C.text, overflow:"hidden",
                  textOverflow:"ellipsis", whiteSpace:"nowrap" }}>{s.name||s.symbol}</div>
                {(() => {
                  const p = livePrices[s.symbol];
                  if (!p || p.ltp == null) return <div style={{ fontSize:11, color:C.muted }}>—</div>;
                  const chg = p.change_pct;
                  const col = chg==null ? C.text : chg>=0 ? C.green : C.red;
                  return (
                    <div style={{ fontSize:11, fontFamily:MONO, color:col, fontWeight:700 }}>
                      ₹{Number(p.ltp).toLocaleString("en-IN",{minimumFractionDigits:2})}
                      {chg!=null && <span style={{ fontSize:9, marginLeft:4, opacity:.8 }}>
                        {chg>=0?"▲":"▼"}{Math.abs(chg).toFixed(2)}%</span>}
                      {p.live===false && <span style={{ fontSize:8, color:C.muted, marginLeft:3 }}>·close</span>}
                    </div>
                  );
                })()}
                <div style={{ fontSize:11, color:C.muted }}>
                  ₹{s.market_cap_cr?.toLocaleString()||"—"}Cr</div>
                <div style={{ fontSize:11, color:s.roe>=15?C.green:C.amber }}>
                  {s.roe?.toFixed(1)||"—"}%</div>
                <div style={{ fontSize:11, color:s.debt_to_equity<=1?C.green:C.red }}>
                  {s.debt_to_equity?.toFixed(1)||"—"}</div>
                <div style={{ fontSize:11, color:s.promoter_holding>=50?C.green:C.amber }}>
                  {s.promoter_holding?.toFixed(1)||"—"}%</div>
                <div style={{ fontSize:11, color:s.revenue_growth>=15?C.green:C.amber }}>
                  {s.revenue_growth?.toFixed(1)||"—"}%</div>
                <div style={{ fontSize:13, fontWeight:800, fontFamily:MONO,
                  color:scoreColor(s.score||0) }}>{s.score||0}</div>
              </div>
            ))}
          </div>
        </Panel>
      )}
    </div>
  );
}

// ─── Portfolio ────────────────────────────────────────────────────────────────
function PortfolioPanel() {
  const [pnl,      setPnl]     = useState(null);
  const [positions,setPos]     = useState([]);

  useEffect(()=>{
    const load = () => {
      api("/api/pnl").then(d=>{ if(d) setPnl(d); });
      api("/api/positions").then(d=>{ if(Array.isArray(d)) setPos(d); });
    };
    load();
    const id = setInterval(load, 5000);
    return ()=>clearInterval(id);
  },[]);

  return (
    <div style={{ display:"flex", flexDirection:"column", gap:13 }}>
      {pnl && (
        <div style={{ display:"grid", gridTemplateColumns:"repeat(auto-fit,minmax(130px,1fr))", gap:9 }}>
          <StatBox label="Today P&L" value={`${(pnl.today_pnl||0)>=0?"+":""}₹${(pnl.today_pnl||0).toLocaleString()}`}
            color={(pnl.today_pnl||0)>=0?C.green:C.red}
            sub={`${(pnl.pnl_pct||0).toFixed(2)}% of capital`} />
          <StatBox label="Total P&L" value={`₹${(pnl.total_pnl||0).toLocaleString()}`}
            color={(pnl.total_pnl||0)>=0?C.green:C.red} />
          <StatBox label="Trades Today" value={pnl.trade_count||0} color={C.cyan} />
          <StatBox label="Kill Switch" value={pnl.kill_active?"ACTIVE":"Safe"}
            color={pnl.kill_active?C.red:C.green}
            sub={pnl.kill_active?pnl.kill_reason:"All systems normal"} />
          {/* FIX: remaining_loss is now always >=0 (backend fix) — no more Math.abs needed.
              Color now reflects how much buffer is left, and the sub-label shows the
              real configured threshold instead of a hardcoded "₹2L". */}
          <StatBox label="Daily Remaining" value={`₹${(pnl.remaining_loss||0).toLocaleString()}`}
            color={(pnl.remaining_loss||0) > Math.abs(pnl.kill_threshold||0)*0.3 ? C.green
                  : (pnl.remaining_loss||0) > 0 ? C.amber : C.red}
            sub={`Till ₹${Math.abs(pnl.kill_threshold||0).toLocaleString()} kill switch`} />
        </div>
      )}

      <Panel>
        <div style={{ fontWeight:700, color:C.text, marginBottom:12 }}>Open Positions</div>
        {positions.length === 0 ? (
          <div style={{ color:C.muted, fontSize:12, textAlign:"center", padding:22 }}>
            No open positions
          </div>
        ) : positions.map((o,i)=>(
          <div key={i} style={{ display:"flex", justifyContent:"space-between",
            alignItems:"center", padding:"9px 0", borderBottom:`1px solid ${C.border}` }}>
            <div>
              <div style={{ fontWeight:700, color:C.text }}>{o.symbol}</div>
              <div style={{ fontSize:11, color:C.muted }}>
                Entry ₹{o.price} · SL ₹{o.stop_loss} · T1 ₹{o.target1} · T2 ₹{o.target2}
              </div>
            </div>
            <div style={{ textAlign:"right" }}>
              <Badge label={o.trade_type||"INTRADAY"} color={C.cyan} />
              <div style={{ fontSize:11, color:C.muted, marginTop:3 }}>
                {o.qty} shares · {o.broker}
              </div>
            </div>
          </div>
        ))}
      </Panel>
    </div>
  );
}

// ─── Compliance ───────────────────────────────────────────────────────────────
function CompliancePanel() {
  const [status, setStatus] = useState(null);
  const [audit,  setAudit]  = useState([]);
  const [reason, setReason] = useState("Manual kill");
  const [busy,   setBusy]   = useState(false);

  const load = useCallback(async () => {
    const s = await api("/api/compliance/status");
    const a = await api("/api/compliance/audit?limit=50");
    if(s) setStatus(s);
    if(Array.isArray(a)) setAudit(a);
  }, []);

  useEffect(()=>{ load(); const id=setInterval(load,5000); return()=>clearInterval(id); },[load]);

  const kill  = async () => {
    if(!window.confirm("Activate kill switch? All deployments will be paused.")) return;
    setBusy(true);
    await api(`/api/compliance/kill?reason=${encodeURIComponent(reason)}`,{method:"POST"});
    await load(); setBusy(false);
  };
  const reset = async () => {
    await api("/api/compliance/reset",{method:"POST"}); await load();
  };

  const kill_active = status?.kill_active ?? false;
  const evtC = { KILL_ACTIVATED:C.red, KILL_RESET:C.green, ORDER_PLACED:C.cyan,
    ORDER_CLOSED:C.green, ORDER_REJECTED:C.amber, RATE_LIMITED:C.red, SIGNAL:C.muted };

  return (
    <div style={{ display:"flex", flexDirection:"column", gap:13 }}>
      <Panel style={{ border:`2px solid ${kill_active?C.red:C.border}`,
        background:kill_active?C.red+"08":C.card }}>
        <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", flexWrap:"wrap", gap:12 }}>
          <div>
            <div style={{ fontSize:16, fontWeight:800, color:kill_active?C.red:C.text }}>
              {kill_active?"⛔ KILL SWITCH ACTIVE":"🟢 Kill Switch — Ready"}
            </div>
            <div style={{ fontSize:11, color:C.muted, marginTop:3 }}>
              {kill_active?`Reason: ${status?.kill_reason}`:
               `Triggers automatically at ₹2,00,000 daily loss (20% of ₹10L capital)`}
            </div>
          </div>
          <div style={{ display:"flex", gap:9, alignItems:"center", flexWrap:"wrap" }}>
            {!kill_active && (
              <input value={reason} onChange={e=>setReason(e.target.value)}
                style={{ ...sel, width:180 }} placeholder="Reason..." />
            )}
            {kill_active
              ? <Btn onClick={reset} color={C.green}>✅ Reset — Re-enable</Btn>
              : <Btn onClick={kill} disabled={busy} color={C.red}>
                  {busy?"Activating…":"⛔ KILL SWITCH"}
                </Btn>
            }
          </div>
        </div>
      </Panel>

      {status && (
        <div style={{ display:"grid", gridTemplateColumns:"repeat(auto-fit,minmax(130px,1fr))", gap:9 }}>
          <StatBox label="Status"          value={kill_active?"HALTED":"ACTIVE"} color={kill_active?C.red:C.green} />
          <StatBox label="Orders/sec"      value={`${status.orders_last_sec}/${status.rate_limit}`} color={C.cyan} sub="SEBI limit: <10/sec" />
          <StatBox label="Today Orders"    value={status.daily_orders} color={C.text} />
          <StatBox label="Daily Remaining" value={status.daily_remaining} color={status.daily_remaining<20?C.red:C.green} />
        </div>
      )}

      <Panel>
        <div style={{ fontWeight:700, color:C.text, marginBottom:12 }}>
          Audit Log
          <span style={{ fontSize:10, color:C.muted, marginLeft:8 }}>
            Written to logs/audit_YYYYMMDD.jsonl · Retain 5 years (SEBI)
          </span>
        </div>
        {audit.length === 0 ? (
          <div style={{ color:C.muted, fontSize:12, textAlign:"center", padding:22 }}>
            No audit events yet
          </div>
        ) : (
          <div style={{ maxHeight:340, overflowY:"auto" }}>
            {[...audit].reverse().map((e,i)=>(
              <div key={i} style={{ display:"grid", gridTemplateColumns:"165px 140px 1fr",
                gap:8, padding:"6px 4px", borderBottom:`1px solid ${C.border}`, alignItems:"center" }}>
                <div style={{ fontSize:10, color:C.dim, fontFamily:MONO }}>
                  {(e.ts||"").slice(0,19).replace("T"," ")}</div>
                <Badge label={e.event} color={evtC[e.event]||C.muted} />
                <div style={{ fontSize:11, color:C.muted }}>
                  {e.symbol && <span style={{ color:C.text, fontWeight:600 }}>{e.symbol} </span>}
                  {e.dep_id  && <span style={{ color:C.dim }}>[{e.dep_id}] </span>}
                  {e.details && Object.entries(e.details).slice(0,3).map(([k,v])=>(
                    <span key={k}>{k}:{String(v).slice(0,15)} </span>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </Panel>

      <Panel style={{ background:C.indigo+"08", border:`1px solid ${C.indigo}22` }}>
        <div style={{ fontSize:11, color:C.muted, lineHeight:1.8 }}>
          <b style={{ color:C.text }}>📋 SEBI Compliance — Self-Managed Algo</b><br/>
          • Orders per second: max <b style={{ color:C.cyan }}>9</b> (SEBI self-managed threshold: &lt;10, no registration needed)<br/>
          • Kill switch: auto-triggers at 20% daily loss (₹2,00,000 on ₹10L capital)<br/>
          • Every order tagged with algo ID <code style={{ color:C.cyan }}>RA-{"{dep_id}"}</code><br/>
          • Audit trail: append-only JSONL log, retained 5 years minimum
        </div>
      </Panel>
    </div>
  );
}

// ─── Root App ─────────────────────────────────────────────────────────────────
export default function RushAlgo() {
  const [tab,         setTab]    = useState("builder");
  const [theme,       setTheme]  = useState(_savedTheme);

  // Switching theme mutates the shared `C` object, which the 400+ existing
  // `C.xxx` references read at render time - so a re-render is all that's
  // needed to repaint the whole app. `tick` forces that re-render, since
  // mutating C isn't something React can observe on its own.
  const [, setTick] = useState(0);
  const changeTheme = (name) => {
    applyTheme(name);
    setTheme(name);
    try { window.localStorage.setItem(THEME_KEY, name); } catch (e) {}
    setTick(t => t + 1);
  };
  const [strategies,  setStrats] = useState([]);
  const [deployments, setDeps]   = useState([]);
  const [backOk,      setBackOk] = useState(false);
  const [nifty,       setNifty]  = useState({price:"--,---.--",change:"0.00%",up:true,base:24832.45});
  const [toast,       setToast]  = useState("");
  // FIX: was hardcoded "₹10,00,000" in the sidebar regardless of actual configured capital
  const [capital,     setCapital]= useState(1000000);

  const notify = msg => { setToast(msg); setTimeout(()=>setToast(""),3000); };

  useEffect(()=>{
    api("/health").then(d=>{ if(d?.status==="ok") { setBackOk(true); if(d.capital) setCapital(d.capital); } });
    api("/api/strategies").then(d=>{ if(Array.isArray(d)) setStrats(d); });
    api("/api/deployments").then(d=>{ if(Array.isArray(d)) setDeps(d); });
    // NIFTY subscribes under its OWN client id so the Universe panel's
    // subscription (which changes on every scroll) can't unsubscribe it.
    // The backend streams the union of all client slots.
    const subNifty = () => fetch(API + "/api/live/subscribe", {
      method:"POST", headers:{ "Content-Type":"application/json" },
      body:JSON.stringify({ symbols:["NIFTY"], client:"nifty-header" }),
    }).catch(()=>{});
    subNifty();
    const loadNifty = async () => {
      try {
        const r = await fetch(API + "/api/live/ticks?symbols=NIFTY");
        const j = await r.json();
        const q = j && j.data ? j.data.NIFTY : null;
        if (q && q.ltp>0) setNifty({
          price: Number(q.ltp).toLocaleString("en-IN",{minimumFractionDigits:2}),
          change: `${(q.change_pct||0)>=0?"+":""}${(q.change_pct||0).toFixed(2)}%`,
          up: (q.change_pct||0)>=0, base: q.ltp });
      } catch (e) { /* ignore */ }
    };
    loadNifty();
    // Re-assert periodically so the header self-heals if the socket reconnects.
    const resub = setInterval(subNifty, 30000);
    const id = setInterval(loadNifty, 2000);
    return ()=>{ clearInterval(id); clearInterval(resub); };
  },[]);

  // FIX: backend scanner broadcasts signal/order/partial_book/close events
  // over WebSocket, but nothing in the frontend ever connected to listen.
  // Without this, every live event the scanner sends goes into a void —
  // the dashboard only ever saw state changes via 5-second REST polling.
  useEffect(()=>{
    let ws = null;
    let reconnectTimer = null;
    let stopped = false;

    const connect = () => {
      if (stopped) return;
      try {
        ws = new WebSocket(API.replace("http","ws") + "/ws");
      } catch {
        reconnectTimer = setTimeout(connect, 5000);
        return;
      }
      ws.onmessage = (evt) => {
        let msg;
        try { msg = JSON.parse(evt.data); } catch { return; }
        if (msg.type === "order") {
          notify(`⚡ Order placed: ${msg.order?.side} ${msg.order?.symbol} × ${msg.order?.qty}`);
        } else if (msg.type === "partial_book") {
          notify(`💰 Partial booking: ${msg.order?.symbol} — target 1 hit`);
        } else if (msg.type === "close") {
          const pnl = msg.order?.pnl ?? 0;
          notify(`${pnl>=0?"✅":"❌"} ${msg.order?.symbol} closed: ₹${pnl.toFixed(0)} (${msg.order?.exit_reason})`);
        } else if (msg.type === "exit_failed") {
          // CRITICAL: the broker did NOT confirm an exit — a live position is
          // still open and unmanaged. Surface this loudly; the scanner will retry
          // but the user should check their broker terminal immediately.
          notify(`🚨 EXIT NOT CONFIRMED: ${msg.symbol} still OPEN (${msg.reason}, retry #${msg.attempt}) — check your broker!`);
        } else if (msg.type === "daily_reset") {
          notify("🔄 Daily P&L and re-entry list reset");
        }
        // "signal" and "ping"/"connected" messages are high-frequency / low-value
        // for a toast — intentionally not surfaced here to avoid notification spam.
      };
      ws.onclose = () => { if (!stopped) reconnectTimer = setTimeout(connect, 5000); };
      ws.onerror = () => { try { ws.close(); } catch {} };
    };

    connect();
    return () => {
      stopped = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (ws) { try { ws.close(); } catch {} }
    };
  },[]);

  const NAV = [
    { id:"builder",    icon:"🔧", label:"Strategy Builder"    },
    { id:"backtest",   icon:"📊", label:"Backtest"             },
    { id:"forward",    icon:"🧪", label:"Forward Test"         },
    { id:"deploy",     icon:"⚡", label:"Deploy",
      badge:deployments.filter(d=>d.status==="LIVE").length || undefined },
    { id:"portfolio",  icon:"💼", label:"Portfolio"            },
    { id:"universe",   icon:"INFLAG", label:"Stock Universe - NSE" },
    { id:"radar",      icon:"INFLAG", label:"Buy & Sell Radar - NSE"},
    { id:"deepdive",   icon:"INFLAG", label:"Stock Deep Dives - NSE"},
    { id:"universe_us",   icon:"USFLAG", label:"Stock Universe - USA" },
    { id:"radar_us",      icon:"USFLAG", label:"Buy & Sell Radar - USA"},
    { id:"deepdive_us",   icon:"USFLAG", label:"Stock Deep Dives - USA"},
    { id:"compliance", icon:"🛡️", label:"Compliance"          },
  ];

  return (
    <div style={{ display:"flex", height:"100vh", background:C.bg, color:C.text,
      fontFamily:MONO, overflow:"hidden" }}>

      {/* Sidebar */}
      <div style={{ width:190, background:C.surface, borderRight:`1px solid ${C.border}`,
        display:"flex", flexDirection:"column", padding:"0 8px", flexShrink:0 }}>
        <div style={{ padding:"15px 6px 12px", borderBottom:`1px solid ${C.border}`, marginBottom:8 }}>
          <div style={{ display:"flex", alignItems:"center", gap:8 }}>
            <div style={{ width:32, height:32, background:`linear-gradient(135deg,${C.indigo},#8B5CF6)`,
              borderRadius:8, display:"flex", alignItems:"center", justifyContent:"center", fontSize:16 }}>⚡</div>
            <div>
              <div style={{ fontWeight:800, fontSize:15, color:C.text }}>Rush Algo</div>
              <div style={{ fontSize:9, color:C.muted, letterSpacing:1 }}>PERSONAL ALGO</div>
            </div>
          </div>
        </div>

        <div style={{ flex:1, overflowY:"auto" }}>
          {NAV.map(n=>(
            <div key={n.id} onClick={()=>setTab(n.id)} style={{
              display:"flex", alignItems:"center", gap:9, padding:"9px 12px",
              borderRadius:7, cursor:"pointer", marginBottom:2,
              background:tab===n.id?C.indigo+"22":"transparent",
              color:tab===n.id?C.indigo:C.muted }}>
              {n.icon === "USFLAG" ? (
                <img src="https://flagcdn.com/24x18/us.png" alt="USA"
                  style={{ width:16, height:12, borderRadius:2, display:"block" }} />
              ) : n.icon === "INFLAG" ? (
                <img src="https://flagcdn.com/24x18/in.png" alt="NSE"
                  style={{ width:16, height:12, borderRadius:2, display:"block" }} />
              ) : (
                <span style={{ fontSize:15 }}>{n.icon}</span>
              )}
              <span style={{ fontSize:12, fontWeight:tab===n.id?700:500, flex:1 }}>{n.label}</span>
              {n.badge > 0 && (
                <span style={{ background:C.green, color:"#fff", borderRadius:9,
                  padding:"1px 5px", fontSize:9, fontWeight:700 }}>{n.badge}</span>
              )}
            </div>
          ))}
        </div>

        {/* Bottom status */}
        <div style={{ borderTop:`1px solid ${C.border}`, padding:"10px 6px 14px" }}>
          <div style={{ background:backOk?C.green+"11":C.red+"11",
            border:`1px solid ${backOk?C.green+"33":C.red+"33"}`,
            borderRadius:7, padding:"7px 9px", marginBottom:7 }}>
            <div style={{ fontSize:9, color:C.muted }}>BACKEND</div>
            <div style={{ fontSize:11, fontWeight:700, color:backOk?C.green:C.red, marginTop:2 }}>
              {backOk?"● Connected":"● Offline"}
            </div>
          </div>
          <div style={{ background:C.indigo+"11", border:`1px solid ${C.indigo}22`,
            borderRadius:7, padding:"7px 9px" }}>
            <div style={{ fontSize:9, color:C.muted }}>CAPITAL</div>
            <div style={{ fontSize:12, fontWeight:700, color:C.indigo, fontFamily:MONO, marginTop:2 }}>
              ₹{capital.toLocaleString("en-IN")}
            </div>
          </div>
        </div>
      </div>

      {/* Main */}
      <div style={{ flex:1, display:"flex", flexDirection:"column", overflow:"hidden" }}>
        {/* Topbar */}
        <div style={{ background:C.surface, borderBottom:`1px solid ${C.border}`,
          padding:"9px 18px", display:"flex", alignItems:"center",
          justifyContent:"space-between", flexShrink:0 }}>
          <div>
            <div style={{ fontWeight:700, fontSize:14, color:C.text }}>
              {NAV.find(n=>n.id===tab)?.label}
            </div>
            <div style={{ fontSize:11, color:C.muted, marginTop:1 }}>
              {tab==="builder"  ? "Build strategies with 50+ indicators · MTF confirmation" :
               tab==="backtest" ? "Historical simulation · Entry after 11AM · Partial booking at T1" :
               tab==="forward"  ? "Paper trading on live data before going live" :
               tab==="deploy"   ? "Live/paper deployment · Scanner runs every 60s during market hours" :
               tab==="portfolio"? "Real-time P&L · Open positions · Kill switch status" :
               tab==="universe" ? "NSE+BSE stocks · Market cap >₹1000Cr · Fundamental filter" :
               tab==="radar"     ? "Deep pullback (Buy) & strong bounce (Sell) in fundamentally strong stocks" :
               tab==="deepdive" ? "Search one stock · Full fundamental profile from every IndianAPI data point" :
                                  "Kill switch · Rate limiter · Audit trail · SEBI compliance"}
            </div>
          </div>
          <div style={{ display:"flex", gap:8, alignItems:"center" }}>
            {/* Theme switcher - Dark / Lighter Dark / Light */}
            <div style={{ display:"flex", gap:3, background:C.surface,
              border:`1px solid ${C.border}`, borderRadius:7, padding:3 }}>
              {[["dark","\u25CF","Dark"],
                ["dim","\u25D0","Lighter Dark"],
                ["light","\u25CB","Light"]].map(([id, icon, title]) => {
                const active = theme === id;
                return (
                  <button key={id} onClick={()=>changeTheme(id)} title={title}
                    style={{ cursor:"pointer", border:"none", borderRadius:5,
                      background:active ? C.indigo+"33" : "transparent",
                      color:active ? C.indigo : C.muted,
                      padding:"3px 8px", fontSize:12, fontWeight:700,
                      fontFamily:MONO, lineHeight:1.2 }}>
                    {icon}
                  </button>
                );
              })}
            </div>
            <div style={{ background:C.green+"11", border:`1px solid ${C.green}22`,
              borderRadius:6, padding:"5px 9px", display:"flex", alignItems:"center", gap:5 }}>
              <div style={{ width:6, height:6, borderRadius:"50%", background:C.green,
                boxShadow:`0 0 5px ${C.green}` }} />
              <span style={{ fontSize:11, color:C.green, fontWeight:700 }}>NSE OPEN</span>
            </div>
            <div style={{ fontFamily:MONO, fontSize:12, color:C.cyan }}>
              NIFTY {nifty.price}
              <span style={{ color:nifty.up?C.green:C.red }}> {nifty.up?"▲":"▼"} {nifty.change}</span>
            </div>
          </div>
        </div>

        {/* Content */}
        <div style={{ flex:1, overflowY:"auto", padding:15 }}>
          {tab==="builder"  && <StrategyBuilder
            strategies={strategies}
            onSave={s=>{ setStrats(p=>[...p.filter(x=>x.id!==s.id),s]); notify(`✅ '${s.name}' saved`); }}
            onDelete={async (s)=>{
              // End-to-end delete: call backend, and ONLY on success remove from the
              // shared strategies state (which feeds backtest + deploy dropdowns too).
              const res = await api(`/api/strategies/${s.id}`, { method:"DELETE" });
              if (res || lastApiError === "") {
                setStrats(p=>p.filter(x=>x.id!==s.id));
                notify(`🗑️ '${s.name}' deleted`);
              } else {
                notify(`❌ Couldn't delete: ${lastApiError || "it may be deployed — stop deployment first"}`);
              }
            }}
          />}
          {tab==="backtest" && <BacktestPanel strategies={strategies} />}
          {tab==="forward"  && (
            <div style={{ textAlign:"center", padding:60 }}>
              <div style={{ fontSize:36, marginBottom:12 }}>🧪</div>
              <div style={{ fontSize:16, fontWeight:700, color:C.text, marginBottom:8 }}>Forward Testing</div>
              <div style={{ fontSize:13, color:C.muted, marginBottom:20 }}>
                Enable paper mode on any strategy, then deploy it.<br/>
                It runs on live market data but places no real orders.
              </div>
              <Btn onClick={()=>setTab("deploy")} color={C.cyan}>Go to Deploy →</Btn>
            </div>
          )}
          {tab==="deploy"    && <DeployPanel strategies={strategies} deployments={deployments} setDeployments={setDeps} notify={notify} />}
          {tab==="portfolio" && <PortfolioPanel />}
          {tab==="universe"  && <UniversePanel />}
          {tab==="radar"      && <RadarPanel />}
          {tab==="universe_us"  && <UniversePanelUS />}
          {tab==="radar_us"      && <RadarPanelUS />}
          {tab==="deepdive_us"   && <DeepDivePanelUS />}
          {tab==="deepdive"  && <DeepDivePanel />}
          {tab==="compliance"&& <CompliancePanel />}
        </div>
      </div>

      {toast && (
        <div style={{ position:"fixed", bottom:22, right:22, background:C.card,
          border:`1px solid ${C.indigo}55`, borderRadius:10, padding:"11px 18px",
          fontSize:13, color:C.text, zIndex:9999, boxShadow:`0 4px 22px ${C.indigo}33` }}>
          {toast}
        </div>
      )}

      <style>{`
        * { box-sizing:border-box; }
        ::-webkit-scrollbar { width:4px; height:4px; }
        ::-webkit-scrollbar-track { background:${C.bg}; }
        ::-webkit-scrollbar-thumb { background:${C.border}; border-radius:2px; }
        select option { background:${C.card}; }
      `}</style>
    </div>
  );
}
