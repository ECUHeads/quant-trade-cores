import { useState, useEffect, useCallback } from "react";
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, AreaChart, Area } from "recharts";

const API = "http://localhost:8000";

// ── Mock data for demo
const MOCK_SIGNALS = [
  { signal_id: "SIG-001", asset: "NVDA", action: "BUY", side: "LONG", timestamp: "2026-03-19T14:30:00Z", pricing_zone: { entry_range: [182.0, 182.5], take_profit: 187.75, stop_loss: 179.56, risk_reward: "1:2.5" }, strategy_type: "Trend Following", news_catalyst: "NVDA beats Q2 EPS by 15%, raises guidance", llm_cio_comment: "Strong earnings catalyst aligned with VWAP pullback. Low VIX environment favorable.", ml_score: 76, confidence: 0.81, gate19_action: "EXECUTE", status: "ACTIVE" },
  { signal_id: "SIG-002", asset: "TSLA", action: "SELL", side: "SHORT", timestamp: "2026-03-19T13:45:00Z", pricing_zone: { entry_range: [245.0, 245.8], take_profit: 238.0, stop_loss: 249.5, risk_reward: "1:1.8" }, strategy_type: "Mean Reversion", news_catalyst: "Tesla lowers Q3 delivery guidance", llm_cio_comment: "Negative catalyst confirmed by price action below VWAP. Reduced size due to elevated VIX.", ml_score: 68, confidence: 0.72, gate19_action: "REDUCE", status: "WON" },
  { signal_id: "SIG-003", asset: "META", action: "BUY", side: "LONG", timestamp: "2026-03-19T10:15:00Z", pricing_zone: { entry_range: [520.0, 521.5], take_profit: 535.0, stop_loss: 515.0, risk_reward: "1:2.1" }, strategy_type: "Trend Following", news_catalyst: "Goldman upgrades META to Buy, PT $600", llm_cio_comment: "Analyst upgrade with strong institutional flow. EXECUTE full size.", ml_score: 82, confidence: 0.88, gate19_action: "EXECUTE", status: "WON" },
  { signal_id: "SIG-004", asset: "AMD", action: "BUY", side: "LONG", timestamp: "2026-03-18T11:00:00Z", pricing_zone: { entry_range: [165.0, 166.0], take_profit: 174.0, stop_loss: 162.0, risk_reward: "1:2.0" }, strategy_type: "Trend Following", news_catalyst: "AMD announces new AI chip partnership", llm_cio_comment: "Product launch catalyst. Sector momentum positive.", ml_score: 71, confidence: 0.75, gate19_action: "EXECUTE", status: "LOST" },
];

const MOCK_EQUITY = Array.from({ length: 30 }, (_, i) => ({
  date: `Mar ${i + 1}`, equity: 5000 + Math.floor(Math.random() * 200 - 50) * (i + 1) / 10,
  pnl: Math.floor(Math.random() * 100 - 30),
}));
MOCK_EQUITY.forEach((d, i) => { if (i > 0) d.equity = MOCK_EQUITY[i - 1].equity + d.pnl; });

const MOCK_STATS = { total_signals: 47, active_signals: 1, win_rate_pct: 63.8, total_pnl_usd: 842.50, profit_factor: 2.14, max_drawdown_usd: 125.30, winners: 30, losers: 17 };

// ── Theme
const C = {
  bg: "#0a0e17", card: "#111827", border: "#1e293b", accent: "#3b82f6",
  green: "#10b981", red: "#ef4444", yellow: "#f59e0b", text: "#e2e8f0",
  muted: "#64748b", surface: "#1e293b",
};

// ── Signal Card
function SignalCard({ signal, isVip }) {
  const isBull = signal.action === "BUY" || signal.side === "LONG";
  const accentColor = isBull ? C.green : C.red;
  const pz = signal.pricing_zone || {};
  const entry = pz.entry_range || [0, 0];
  const mid = (entry[0] + entry[1]) / 2;
  const tpPct = mid > 0 ? ((pz.take_profit - mid) / mid * 100).toFixed(1) : "0";
  const slPct = mid > 0 ? ((pz.stop_loss - mid) / mid * 100).toFixed(1) : "0";

  const statusColors = { ACTIVE: C.green, WON: C.green, LOST: C.red, CANCELLED: C.yellow };

  return (
    <div style={{ background: C.card, borderRadius: 16, border: `1px solid ${C.border}`, overflow: "hidden", marginBottom: 16 }}>
      {/* Header */}
      <div style={{ background: `linear-gradient(135deg, ${accentColor}22, ${accentColor}08)`, borderBottom: `1px solid ${accentColor}33`, padding: "16px 20px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <span style={{ fontSize: 28 }}>{isBull ? "🟢" : "🔴"}</span>
          <div>
            <div style={{ fontSize: 22, fontWeight: 800, color: C.text, fontFamily: "'JetBrains Mono', monospace" }}>{signal.asset}</div>
            <div style={{ fontSize: 12, color: C.muted }}>{isBull ? "LONG" : "SHORT"} • {signal.strategy_type}</div>
          </div>
        </div>
        <div style={{ textAlign: "right" }}>
          <span style={{ background: statusColors[signal.status] || C.muted, color: "#fff", padding: "4px 12px", borderRadius: 20, fontSize: 11, fontWeight: 700 }}>{signal.status}</span>
          <div style={{ fontSize: 11, color: C.muted, marginTop: 4 }}>{new Date(signal.timestamp).toLocaleString()}</div>
        </div>
      </div>

      {/* Pricing */}
      <div style={{ padding: "16px 20px", display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <PriceRow label="📍 Entry" value={`$${entry[0]?.toFixed(2)} – $${entry[1]?.toFixed(2)}`} color={C.text} />
        <PriceRow label="⚖️ R:R" value={pz.risk_reward || "1:2"} color={C.accent} />
        <PriceRow label="🎯 TP" value={`$${pz.take_profit?.toFixed(2)}`} sub={`${tpPct > 0 ? "+" : ""}${tpPct}%`} color={C.green} />
        <PriceRow label="🛡️ SL" value={`$${pz.stop_loss?.toFixed(2)}`} sub={`${slPct}%`} color={C.red} />
      </div>

      {/* ML + Catalyst */}
      <div style={{ padding: "0 20px 12px", display: "flex", gap: 8, flexWrap: "wrap" }}>
        <Tag text={`🧠 ML: ${signal.ml_score}`} />
        <Tag text={`📊 Conf: ${(signal.confidence * 100).toFixed(0)}%`} />
        <Tag text={`🤖 ${signal.gate19_action}`} />
      </div>

      {signal.news_catalyst && (
        <div style={{ padding: "0 20px 12px", fontSize: 13, color: C.muted }}>📰 {signal.news_catalyst}</div>
      )}

      {/* LLM Comment (VIP only) */}
      {isVip && signal.llm_cio_comment && (
        <div style={{ margin: "0 20px 16px", background: C.surface, borderRadius: 10, padding: 14 }}>
          <div style={{ fontSize: 11, color: C.accent, fontWeight: 700, marginBottom: 4 }}>🤖 AI CIO Analysis</div>
          <div style={{ fontSize: 13, color: C.text, lineHeight: 1.5, fontStyle: "italic" }}>"{signal.llm_cio_comment}"</div>
        </div>
      )}
    </div>
  );
}

function PriceRow({ label, value, sub, color }) {
  return (
    <div>
      <div style={{ fontSize: 11, color: C.muted, marginBottom: 2 }}>{label}</div>
      <div style={{ fontSize: 16, fontWeight: 700, color, fontFamily: "'JetBrains Mono', monospace" }}>
        {value} {sub && <span style={{ fontSize: 12, fontWeight: 400, opacity: 0.7 }}>{sub}</span>}
      </div>
    </div>
  );
}

function Tag({ text }) {
  return <span style={{ background: C.surface, color: C.muted, padding: "4px 10px", borderRadius: 6, fontSize: 11, fontWeight: 600 }}>{text}</span>;
}

// ── Stat Card
function StatCard({ label, value, sub, color }) {
  return (
    <div style={{ background: C.card, borderRadius: 12, border: `1px solid ${C.border}`, padding: 20 }}>
      <div style={{ fontSize: 12, color: C.muted, marginBottom: 6 }}>{label}</div>
      <div style={{ fontSize: 28, fontWeight: 800, color: color || C.text, fontFamily: "'JetBrains Mono', monospace" }}>{value}</div>
      {sub && <div style={{ fontSize: 12, color: C.muted, marginTop: 4 }}>{sub}</div>}
    </div>
  );
}

// ── Main App
export default function App() {
  const [page, setPage] = useState("signals");
  const [isVip, setIsVip] = useState(true);
  const [signals, setSignals] = useState(MOCK_SIGNALS);
  const [stats, setStats] = useState(MOCK_STATS);
  const [equity, setEquity] = useState(MOCK_EQUITY);

  const navItems = [
    { id: "signals", label: "⚡ Live Signals", vipOnly: true },
    { id: "performance", label: "📊 Performance", vipOnly: false },
    { id: "admin", label: "⚙️ Admin", vipOnly: true },
  ];

  return (
    <div style={{ background: C.bg, minHeight: "100vh", color: C.text, fontFamily: "'Inter', -apple-system, sans-serif" }}>
      {/* ── Navbar */}
      <nav style={{ background: C.card, borderBottom: `1px solid ${C.border}`, padding: "0 24px", display: "flex", alignItems: "center", justifyContent: "space-between", height: 60, position: "sticky", top: 0, zIndex: 50 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 24 }}>
          <div style={{ fontSize: 20, fontWeight: 800, background: "linear-gradient(135deg, #3b82f6, #8b5cf6)", WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent" }}>
            QUANT AGENT
          </div>
          <div style={{ display: "flex", gap: 4 }}>
            {navItems.map(n => (
              <button key={n.id} onClick={() => setPage(n.id)}
                style={{ background: page === n.id ? C.accent + "22" : "transparent", color: page === n.id ? C.accent : C.muted, border: "none", padding: "8px 16px", borderRadius: 8, cursor: "pointer", fontSize: 13, fontWeight: 600, transition: "all 0.2s" }}>
                {n.label}
              </button>
            ))}
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <button onClick={() => setIsVip(v => !v)}
            style={{ background: isVip ? C.green + "22" : C.surface, color: isVip ? C.green : C.muted, border: `1px solid ${isVip ? C.green + "44" : C.border}`, padding: "6px 14px", borderRadius: 20, cursor: "pointer", fontSize: 12, fontWeight: 700 }}>
            {isVip ? "👑 VIP" : "👤 Guest"}
          </button>
        </div>
      </nav>

      {/* ── Content */}
      <div style={{ maxWidth: 1200, margin: "0 auto", padding: 24 }}>

        {/* ═══ LIVE SIGNALS ═══ */}
        {page === "signals" && (
          <div>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 20 }}>
              <h1 style={{ fontSize: 24, fontWeight: 800, margin: 0 }}>
                {isVip ? "⚡ Live Signal Feed" : "📋 Signal History (Delayed)"}
              </h1>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <span style={{ width: 8, height: 8, borderRadius: "50%", background: C.green, animation: "pulse 2s infinite" }} />
                <span style={{ fontSize: 12, color: C.muted }}>Engine Active</span>
              </div>
            </div>

            {!isVip && (
              <div style={{ background: `linear-gradient(135deg, ${C.accent}15, #8b5cf615)`, border: `1px solid ${C.accent}33`, borderRadius: 12, padding: 20, marginBottom: 20, textAlign: "center" }}>
                <div style={{ fontSize: 16, fontWeight: 700, marginBottom: 6 }}>🔒 สัญญาณ Real-time สำหรับ VIP เท่านั้น</div>
                <div style={{ fontSize: 13, color: C.muted, marginBottom: 12 }}>คุณกำลังดูสัญญาณที่จบแล้ว (delayed 1 ชั่วโมง)</div>
                <button style={{ background: C.accent, color: "#fff", border: "none", padding: "10px 28px", borderRadius: 8, cursor: "pointer", fontWeight: 700 }}>
                  สมัคร VIP — ฿999/เดือน
                </button>
              </div>
            )}

            {signals
              .filter(s => isVip || s.status !== "ACTIVE")
              .map(s => <SignalCard key={s.signal_id} signal={s} isVip={isVip} />)}
          </div>
        )}

        {/* ═══ PERFORMANCE ═══ */}
        {page === "performance" && (
          <div>
            <h1 style={{ fontSize: 24, fontWeight: 800, marginBottom: 20 }}>📊 Performance & Backtest</h1>

            {/* Stats Grid */}
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 12, marginBottom: 28 }}>
              <StatCard label="Win Rate" value={`${stats.win_rate_pct}%`} color={stats.win_rate_pct >= 50 ? C.green : C.red} sub={`${stats.winners}W / ${stats.losers}L`} />
              <StatCard label="Total P&L" value={`$${stats.total_pnl_usd?.toLocaleString()}`} color={stats.total_pnl_usd >= 0 ? C.green : C.red} />
              <StatCard label="Profit Factor" value={stats.profit_factor} color={C.accent} />
              <StatCard label="Max Drawdown" value={`$${stats.max_drawdown_usd}`} color={C.yellow} />
              <StatCard label="Total Signals" value={stats.total_signals} sub={`${stats.active_signals} active`} />
            </div>

            {/* Equity Curve */}
            <div style={{ background: C.card, borderRadius: 16, border: `1px solid ${C.border}`, padding: 24, marginBottom: 20 }}>
              <h3 style={{ fontSize: 16, fontWeight: 700, marginBottom: 16, margin: 0 }}>📈 Equity Curve</h3>
              <ResponsiveContainer width="100%" height={300}>
                <AreaChart data={equity}>
                  <defs>
                    <linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor={C.accent} stopOpacity={0.3} />
                      <stop offset="95%" stopColor={C.accent} stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke={C.border} />
                  <XAxis dataKey="date" tick={{ fontSize: 11, fill: C.muted }} />
                  <YAxis tick={{ fontSize: 11, fill: C.muted }} domain={["auto", "auto"]} />
                  <Tooltip contentStyle={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 8, fontSize: 13 }} />
                  <Area type="monotone" dataKey="equity" stroke={C.accent} fill="url(#eqGrad)" strokeWidth={2} />
                </AreaChart>
              </ResponsiveContainer>
            </div>

            {/* Closed Signals Table */}
            <div style={{ background: C.card, borderRadius: 16, border: `1px solid ${C.border}`, padding: 24 }}>
              <h3 style={{ fontSize: 16, fontWeight: 700, marginBottom: 16, margin: 0 }}>📋 Recent Closed Signals</h3>
              <div style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                  <thead>
                    <tr style={{ borderBottom: `1px solid ${C.border}` }}>
                      {["Asset", "Side", "Entry", "TP", "SL", "Status", "ML"].map(h => (
                        <th key={h} style={{ textAlign: "left", padding: "10px 12px", color: C.muted, fontWeight: 600, fontSize: 11 }}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {signals.filter(s => s.status !== "ACTIVE").map(s => (
                      <tr key={s.signal_id} style={{ borderBottom: `1px solid ${C.border}11` }}>
                        <td style={{ padding: "10px 12px", fontWeight: 700 }}>{s.asset}</td>
                        <td style={{ padding: "10px 12px", color: s.side === "LONG" ? C.green : C.red }}>{s.side}</td>
                        <td style={{ padding: "10px 12px", fontFamily: "monospace" }}>${s.pricing_zone.entry_range[0]?.toFixed(2)}</td>
                        <td style={{ padding: "10px 12px", fontFamily: "monospace", color: C.green }}>${s.pricing_zone.take_profit?.toFixed(2)}</td>
                        <td style={{ padding: "10px 12px", fontFamily: "monospace", color: C.red }}>${s.pricing_zone.stop_loss?.toFixed(2)}</td>
                        <td style={{ padding: "10px 12px" }}>
                          <span style={{ background: s.status === "WON" ? C.green + "22" : C.red + "22", color: s.status === "WON" ? C.green : C.red, padding: "3px 10px", borderRadius: 12, fontSize: 11, fontWeight: 700 }}>{s.status}</span>
                        </td>
                        <td style={{ padding: "10px 12px", color: C.muted }}>{s.ml_score}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        )}

        {/* ═══ ADMIN ═══ */}
        {page === "admin" && (
          <div>
            <h1 style={{ fontSize: 24, fontWeight: 800, marginBottom: 20 }}>⚙️ Admin Control Panel</h1>

            {/* Engine Status */}
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 12, marginBottom: 24 }}>
              <div style={{ background: C.card, borderRadius: 12, border: `1px solid ${C.green}44`, padding: 20 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
                  <span style={{ width: 10, height: 10, borderRadius: "50%", background: C.green }} />
                  <span style={{ fontSize: 14, fontWeight: 700 }}>Engine Status</span>
                </div>
                <div style={{ fontSize: 24, fontWeight: 800, color: C.green }}>HEALTHY</div>
                <div style={{ fontSize: 12, color: C.muted }}>Last signal: 12 min ago</div>
              </div>
              <StatCard label="Active Users" value="156" sub="42 VIP" color={C.accent} />
              <StatCard label="MRR" value="฿41,958" color={C.green} sub="42 × ฿999" />
              <StatCard label="Active Signals" value={stats.active_signals} color={C.yellow} />
            </div>

            {/* Active Signals (with Cancel button) */}
            <div style={{ background: C.card, borderRadius: 16, border: `1px solid ${C.border}`, padding: 24, marginBottom: 20 }}>
              <h3 style={{ fontSize: 16, fontWeight: 700, marginBottom: 16, margin: 0 }}>🔴 Active Signals (Manual Override)</h3>
              {signals.filter(s => s.status === "ACTIVE").map(s => (
                <div key={s.signal_id} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: 14, background: C.surface, borderRadius: 10, marginBottom: 8 }}>
                  <div>
                    <span style={{ fontWeight: 700 }}>{s.asset}</span>
                    <span style={{ color: C.muted, marginLeft: 8, fontSize: 12 }}>{s.signal_id}</span>
                    <span style={{ color: s.side === "LONG" ? C.green : C.red, marginLeft: 8, fontSize: 12, fontWeight: 600 }}>{s.side}</span>
                  </div>
                  <button onClick={() => alert(`Cancel ${s.signal_id}? (API call to /admin/cancel/${s.signal_id})`)}
                    style={{ background: C.red, color: "#fff", border: "none", padding: "8px 20px", borderRadius: 8, cursor: "pointer", fontWeight: 700, fontSize: 12 }}>
                    🚨 CANCEL
                  </button>
                </div>
              ))}
              {signals.filter(s => s.status === "ACTIVE").length === 0 && (
                <div style={{ textAlign: "center", padding: 20, color: C.muted }}>ไม่มี active signals</div>
              )}
            </div>

            {/* User Management */}
            <div style={{ background: C.card, borderRadius: 16, border: `1px solid ${C.border}`, padding: 24 }}>
              <h3 style={{ fontSize: 16, fontWeight: 700, marginBottom: 16, margin: 0 }}>👥 User Management</h3>
              <div style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                  <thead>
                    <tr style={{ borderBottom: `1px solid ${C.border}` }}>
                      {["User", "Role", "LINE", "Telegram", "VIP Expires"].map(h => (
                        <th key={h} style={{ textAlign: "left", padding: "10px 12px", color: C.muted, fontWeight: 600, fontSize: 11 }}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {[
                      { name: "trader_john", role: "VIP", line: true, tg: true, expires: "2026-04-19" },
                      { name: "crypto_amy", role: "VIP", line: false, tg: true, expires: "2026-03-25" },
                      { name: "newbie_99", role: "GUEST", line: true, tg: false, expires: null },
                    ].map((u, i) => (
                      <tr key={i} style={{ borderBottom: `1px solid ${C.border}11` }}>
                        <td style={{ padding: "10px 12px", fontWeight: 600 }}>{u.name}</td>
                        <td style={{ padding: "10px 12px" }}>
                          <span style={{ background: u.role === "VIP" ? C.green + "22" : C.surface, color: u.role === "VIP" ? C.green : C.muted, padding: "3px 10px", borderRadius: 12, fontSize: 11, fontWeight: 700 }}>{u.role}</span>
                        </td>
                        <td style={{ padding: "10px 12px" }}>{u.line ? "✅" : "—"}</td>
                        <td style={{ padding: "10px 12px" }}>{u.tg ? "✅" : "—"}</td>
                        <td style={{ padding: "10px 12px", color: C.muted, fontSize: 12 }}>{u.expires || "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        )}
      </div>

      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&family=JetBrains+Mono:wght@400;700;800&display=swap');
        * { box-sizing: border-box; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: ${C.bg}; }
        ::-webkit-scrollbar-thumb { background: ${C.border}; border-radius: 3px; }
      `}</style>
    </div>
  );
}
