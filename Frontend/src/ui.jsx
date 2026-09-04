import { useState, useEffect, useRef, useContext, createContext, Fragment } from "react";
import { AppIcon } from "./Icons.jsx";

// ---------------------------------------------------------------------------
// Toasts
// ---------------------------------------------------------------------------
const ToastContext = createContext(null);

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([]);

  function push(toast) {
    const id = Math.random().toString(36).slice(2);
    setToasts((t) => [...t, { id, ...toast }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4200);
  }

  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="fixed top-4 right-4 z-[100] flex flex-col gap-2 w-80">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={`toast-in rounded-lg border px-3.5 py-3 shadow-lg backdrop-blur-md text-sm flex items-start gap-2.5 ${
              t.kind === "success"
                ? "bg-emerald-500/10 border-emerald-500/30 text-emerald-200"
                : t.kind === "error"
                ? "bg-rose-500/10 border-rose-500/30 text-rose-200"
                : "bg-slate-800/90 border-slate-700 text-slate-200"
            }`}
          >
            <AppIcon
              name={t.kind === "success" ? "checkCircle" : t.kind === "error" ? "alertTriangle" : "bell"}
              className="w-4 h-4 mt-0.5 shrink-0"
            />
            <div>
              <div className="font-medium leading-tight">{t.title}</div>
              {t.description && <div className="text-xs opacity-75 mt-0.5">{t.description}</div>}
            </div>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  return useContext(ToastContext);
}

// ---------------------------------------------------------------------------
// Basics
// ---------------------------------------------------------------------------
export function Badge({ children, tone = "slate" }) {
  const tones = {
    slate: "bg-slate-700/40 text-slate-300 border-slate-600/50",
    emerald: "bg-emerald-500/10 text-emerald-300 border-emerald-500/30",
    amber: "bg-amber-500/10 text-amber-300 border-amber-500/30",
    rose: "bg-rose-500/10 text-rose-300 border-rose-500/30",
    sky: "bg-sky-500/10 text-sky-300 border-sky-500/30",
    violet: "bg-violet-500/10 text-violet-300 border-violet-500/30",
  };
  return (
    <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium ${tones[tone]}`}>
      {children}
    </span>
  );
}

export function Spinner({ className = "w-4 h-4" }) {
  return <AppIcon name="loader" className={`${className} animate-spin-slow text-current`} />;
}

export function Button({ children, variant = "primary", className = "", ...rest }) {
  const variants = {
    primary: "bg-brand-500 hover:bg-brand-400 text-white shadow-glow disabled:opacity-40 disabled:shadow-none",
    ghost: "bg-transparent hover:bg-slate-800 text-slate-300 border border-slate-700",
    subtle: "bg-slate-800 hover:bg-slate-700 text-slate-200",
    danger: "bg-rose-600/90 hover:bg-rose-500 text-white",
  };
  return (
    <button
      className={`inline-flex items-center gap-1.5 rounded-lg px-3.5 py-2 text-sm font-medium transition-all duration-150 disabled:cursor-not-allowed ${variants[variant]} ${className}`}
      {...rest}
    >
      {children}
    </button>
  );
}

export function Card({ children, className = "" }) {
  return (
    <div className={`rounded-xl border border-slate-800 bg-slate-900/60 backdrop-blur-sm shadow-card ${className}`}>
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Progress
// ---------------------------------------------------------------------------
export function ProgressBar({ value, tone = "brand", className = "", striped = false }) {
  const tones = {
    brand: "bg-gradient-to-r from-brand-500 to-cyan-400",
    emerald: "bg-emerald-500",
    rose: "bg-rose-500",
    amber: "bg-amber-500",
  };
  return (
    <div className={`h-1.5 w-full rounded-full bg-slate-800 overflow-hidden ${className}`}>
      <div
        className={`h-full rounded-full transition-[width] duration-500 ease-out ${tones[tone]} ${striped ? "progress-stripes" : ""}`}
        style={{ width: `${Math.min(100, Math.max(0, value))}%` }}
      />
    </div>
  );
}

export function CircularProgress({ value, size = 108, stroke = 8, label, sub }) {
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const offset = c - (Math.min(100, value) / 100) * c;
  return (
    <div className="relative" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90">
        <circle cx={size / 2} cy={size / 2} r={r} stroke="rgb(30 41 59)" strokeWidth={stroke} fill="none" />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          stroke="url(#grad)"
          strokeWidth={stroke}
          fill="none"
          strokeLinecap="round"
          strokeDasharray={c}
          strokeDashoffset={offset}
          className="transition-[stroke-dashoffset] duration-700 ease-out"
        />
        <defs>
          <linearGradient id="grad" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stopColor="#6366f1" />
            <stop offset="100%" stopColor="#22d3ee" />
          </linearGradient>
        </defs>
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <span className="text-xl font-semibold text-white tabular-nums">{label}</span>
        {sub && <span className="text-[10px] text-slate-400 uppercase tracking-wide mt-0.5">{sub}</span>}
      </div>
    </div>
  );
}

export function formatElapsed(totalSeconds) {
  const m = Math.floor(totalSeconds / 60).toString().padStart(2, "0");
  const s = Math.floor(totalSeconds % 60).toString().padStart(2, "0");
  return `${m}:${s}`;
}

// ---------------------------------------------------------------------------
// Phase stepper
// ---------------------------------------------------------------------------
function statusStyles(status) {
  switch (status) {
    case "done":
      return { ring: "border-emerald-500 bg-emerald-500/15 text-emerald-300", line: "bg-emerald-500", text: "text-emerald-300" };
    case "running":
      return { ring: "border-brand-400 bg-brand-500/15 text-brand-300", line: "bg-slate-700", text: "text-brand-300" };
    case "error":
      return { ring: "border-rose-500 bg-rose-500/15 text-rose-300", line: "bg-slate-700", text: "text-rose-300" };
    default:
      return { ring: "border-slate-700 bg-slate-800/60 text-slate-500", line: "bg-slate-700", text: "text-slate-500" };
  }
}

export function PhaseStepper({ phases, activeKey, onSelect }) {
  return (
    <div className="flex items-stretch w-full">
      {phases.map((p, i) => {
        const st = statusStyles(p.status);
        const isActive = p.key === activeKey;
        return (
          <Fragment key={p.key}>
            <button
              onClick={() => onSelect && onSelect(p.key)}
              className={`flex-1 flex flex-col items-center text-center gap-2 py-1 group ${onSelect ? "cursor-pointer" : ""}`}
            >
              <div
                className={`relative w-11 h-11 rounded-full border-2 flex items-center justify-center transition-all ${st.ring} ${
                  isActive ? "ring-4 ring-brand-500/20 scale-105" : ""
                }`}
              >
                {p.status === "running" ? (
                  <Spinner className="w-5 h-5" />
                ) : p.status === "done" ? (
                  <AppIcon name="check" className="w-5 h-5" />
                ) : (
                  <AppIcon name={p.icon} className="w-5 h-5" />
                )}
              </div>
              <div>
                <div className={`text-xs font-semibold ${isActive ? "text-white" : "text-slate-300"} group-hover:text-white transition-colors`}>
                  {p.label}
                </div>
                <div className={`text-[10px] uppercase tracking-wide ${st.text}`}>{p.status}</div>
              </div>
            </button>
            {i < phases.length - 1 && (
              <div className="flex-1 flex items-center px-1 -mt-6">
                <div className={`h-0.5 w-full rounded-full ${st.line === "bg-emerald-500" ? "bg-emerald-500" : "bg-slate-700"}`} />
              </div>
            )}
          </Fragment>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Company avatar (logo or initials)
// ---------------------------------------------------------------------------
export function CompanyAvatar({ company, size = 36 }) {
  if (company.logo) {
    return (
      <div
        className="rounded-lg bg-white/95 flex items-center justify-center overflow-hidden shrink-0 p-1.5"
        style={{ width: size, height: size }}
      >
        <img src={company.logo} alt={company.short} className="w-full h-full object-contain" />
      </div>
    );
  }
  const initials = company.short
    .split(" ")
    .map((w) => w[0])
    .slice(0, 2)
    .join("")
    .toUpperCase();
  return (
    <div
      className="rounded-lg flex items-center justify-center shrink-0 font-semibold text-white/90"
      style={{ width: size, height: size, background: company.color, fontSize: size * 0.36 }}
    >
      {company.kind === "industry" ? <AppIcon name="building" className="w-1/2 h-1/2" /> : initials}
    </div>
  );
}

export function ActivityLog({ entries }) {
  const ref = useRef(null);
  useEffect(() => {
    if (ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [entries.length]);

  const levelColor = { info: "text-slate-400", success: "text-emerald-400", warn: "text-amber-400", error: "text-rose-400" };
  const levelDot = { info: "bg-slate-500", success: "bg-emerald-500", warn: "bg-amber-500", error: "bg-rose-500" };

  return (
    <div ref={ref} className="font-mono text-[12.5px] leading-relaxed h-full overflow-y-auto pr-1 scrollbar-thin">
      {entries.map((e, i) => (
        <div key={i} className="flex items-start gap-2 py-0.5">
          <span className={`mt-1.5 w-1.5 h-1.5 rounded-full shrink-0 ${levelDot[e.level]}`} />
          {e.time && <span className="text-slate-600 shrink-0">{e.time}</span>}
          <span className={levelColor[e.level]}>{e.text}</span>
        </div>
      ))}
    </div>
  );
}
