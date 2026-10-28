import React, { useEffect, useRef, useState } from "react";
import Control from "../components/Control";
import {
  useControlContext,
  ECG_SAMPLE_RATE,
  ECG_RING_CAPACITY,
} from "../components/ControlContext";

// ==================== CONFIGURACIÓN ====================
const DERIVACIONES = [
  { id: "I", name: "I", color: "#00DDFF" },
  { id: "II", name: "II", color: "#00FF88" },
  { id: "III", name: "III", color: "#FFD700" },
  { id: "aVR", name: "aVR", color: "#FF6B6B" },
  { id: "aVL", name: "aVL", color: "#4ECDC4" },
  { id: "aVF", name: "aVF", color: "#95E1D3" },
  { id: "V1", name: "V1", color: "#A8E6CF" },
  { id: "V2", name: "V2", color: "#FFD3B6" },
  { id: "V3", name: "V3", color: "#FFAAA5" },
  { id: "V4", name: "V4", color: "#FF8B94" },
  { id: "V5", name: "V5", color: "#A8D8EA" },
  { id: "V6", name: "V6", color: "#AA96DA" },
];

const WINDOW_OPTIONS = [2.5, 5, 10]; // segundos visibles
const DEFAULT_WINDOW_S = 5;
const FRAME_MS = 33;                 // ~30 fps
const MAX_FRAME_MS = 100;            // piso de 10 fps en equipos muy lentos
const MAX_LAG = 600;                 // retardo máximo de reproducción (0.6 s)
const START_LAG = 300;               // retardo con el que arranca (0.3 s)
const LABEL_WIDTH = 50;

// ==================== FRECUENCIA CARDÍACA ====================
// Detección de picos R sobre el valor absoluto de la señal sin línea base, de modo que
// funciona igual si la etapa analógica invierte la polaridad. Costo O(n).
const estimateBpm = (vals, fs) => {
  const n = vals.length;
  const x = new Float32Array(n);

  let sum = 0;
  let cnt = 0;
  for (let i = 0; i < n; i++) {
    if (vals[i] === vals[i]) { sum += vals[i]; cnt++; }
  }
  if (cnt < fs * 2.5) return 0;
  const mean = sum / cnt;

  // NaN -> media (los huecos no deben crear picos)
  const prefix = new Float64Array(n + 1);
  for (let i = 0; i < n; i++) {
    x[i] = vals[i] === vals[i] ? vals[i] : mean;
    prefix[i + 1] = prefix[i] + x[i];
  }

  // Quitar línea base con media móvil de 0.6 s
  const half = Math.round(fs * 0.3);
  const dev = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const a = Math.max(0, i - half);
    const b = Math.min(n, i + half + 1);
    dev[i] = Math.abs(x[i] - (prefix[b] - prefix[a]) / (b - a));
  }

  // Umbral = 60 % del percentil 99.5 (robusto frente a un pico aislado)
  const sample = [];
  for (let i = 0; i < n; i += 5) sample.push(dev[i]);
  sample.sort((p, q) => p - q);
  const p995 = sample[Math.min(sample.length - 1, Math.floor(sample.length * 0.995))];
  if (!(p995 > 0)) return 0;
  const thr = 0.6 * p995;

  const refractory = Math.round(fs * 0.3); // máx ≈ 200 lpm
  const peaks = [];
  let i = 0;
  while (i < n) {
    if (dev[i] > thr) {
      let best = i;
      let j = i;
      const limit = Math.min(n, i + Math.round(fs * 0.08));
      while (j < limit) {
        if (dev[j] > dev[best]) best = j;
        j++;
      }
      peaks.push(best);
      i = best + refractory;
    } else {
      i++;
    }
  }

  if (peaks.length < 3) return 0;

  const rr = [];
  for (let k = 1; k < peaks.length; k++) rr.push(peaks[k] - peaks[k - 1]);
  rr.sort((p, q) => p - q);
  const medianRR = rr[Math.floor(rr.length / 2)];
  const bpm = Math.round((60 * fs) / medianRR);

  return bpm >= 30 && bpm <= 220 ? bpm : 0;
};

// ==================== DIBUJO ====================
const fitCanvas = (canvas) => {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  if (w === 0 || h === 0) return null;

  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h };
};

// Rejilla tipo papel ECG: línea fina cada 0.2 s, marcada cada 1 s. Se ancla al
// sample_num, así se desplaza junto con la señal.
const drawGrid = (ctx, w, h, start, W, endSample) => {
  const xs = (w - 1) / (W - 1);
  const first = Math.ceil(start / 200) * 200;

  ctx.lineWidth = 1;

  ctx.strokeStyle = "rgba(0, 90, 130, 0.28)";
  ctx.beginPath();
  for (let s = first; s <= endSample; s += 200) {
    if (s % 1000 === 0) continue;
    const x = Math.round((s - start) * xs) + 0.5;
    ctx.moveTo(x, 0);
    ctx.lineTo(x, h);
  }
  ctx.stroke();

  ctx.strokeStyle = "rgba(0, 130, 180, 0.6)";
  ctx.beginPath();
  for (let s = Math.ceil(start / 1000) * 1000; s <= endSample; s += 1000) {
    const x = Math.round((s - start) * xs) + 0.5;
    ctx.moveTo(x, 0);
    ctx.lineTo(x, h);
  }
  ctx.stroke();

  ctx.strokeStyle = "rgba(0, 90, 130, 0.35)";
  ctx.beginPath();
  ctx.moveTo(0, Math.round(h / 2) + 0.5);
  ctx.lineTo(w, Math.round(h / 2) + 0.5);
  ctx.stroke();
};

// Traza la señal. Con más muestras que píxeles se dibuja la envolvente mín/máx por
// columna (no se pierde ningún pico R); los NaN cortan la línea.
const drawTrace = (ctx, w, h, vals, lo, span, color) => {
  const W = vals.length;
  const yOf = (v) => h - ((v - lo) / span) * h;

  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.lineJoin = "bevel";
  ctx.beginPath();

  let pen = false;

  if (W <= w * 1.2) {
    const xs = (w - 1) / (W - 1);
    for (let i = 0; i < W; i++) {
      const v = vals[i];
      if (v !== v) { pen = false; continue; }
      const x = i * xs;
      const y = yOf(v);
      if (!pen) { ctx.moveTo(x, y); pen = true; } else { ctx.lineTo(x, y); }
    }
  } else {
    const spp = W / w;
    for (let col = 0; col < w; col++) {
      const i0 = Math.floor(col * spp);
      const i1 = Math.max(i0 + 1, Math.min(W, Math.floor((col + 1) * spp)));

      let mn = Infinity;
      let mx = -Infinity;
      let imn = -1;
      let imx = -1;
      for (let i = i0; i < i1; i++) {
        const v = vals[i];
        if (v !== v) continue;
        if (v < mn) { mn = v; imn = i; }
        if (v > mx) { mx = v; imx = i; }
      }

      if (imn < 0) { pen = false; continue; }

      const first = imn <= imx ? mn : mx;
      const second = imn <= imx ? mx : mn;
      const x = col + 0.5;
      const y1 = yOf(first);
      const y2 = yOf(second);

      if (!pen) { ctx.moveTo(x, y1); pen = true; } else { ctx.lineTo(x, y1); }
      if (y2 !== y1) ctx.lineTo(x, y2);
    }
  }
  ctx.stroke();
};

// ==================== COMPONENTE ====================
const DerivacionesECG = () => {
  const {
    isRecording,
    wsConnected,
    derivationStats,
    getDerivationHead,
    fillDerivationWindow,
    clearDerivations,
  } = useControlContext();

  const [windowSec, setWindowSec] = useState(DEFAULT_WINDOW_S);
  const [showStats, setShowStats] = useState(false);
  const [paused, setPaused] = useState(false);
  const [heartRate, setHeartRate] = useState(0);

  const canvasRefs = useRef({});
  const axisRef = useRef(null);
  const windowRef = useRef(Math.round(DEFAULT_WINDOW_S * ECG_SAMPLE_RATE));
  const pausedRef = useRef(false);
  const renderHeadRef = useRef(-1);
  const scaleRef = useRef({});
  const bufRef = useRef(new Float32Array(ECG_RING_CAPACITY));
  const lastFrameRef = useRef(0);

  windowRef.current = Math.round(windowSec * ECG_SAMPLE_RATE);
  pausedRef.current = paused;

  // ---------- Bucle de dibujo (no depende de renders de React) ----------
  useEffect(() => {
    let raf = 0;
    let lastDraw = 0;
    let frameMs = FRAME_MS;   // se adapta al costo real de dibujar (equipos lentos)
    let drawCost = 0;         // promedio móvil del tiempo de dibujo (ms)

    const frame = (now) => {
      raf = requestAnimationFrame(frame);

      if (now - lastDraw < frameMs) return;
      const dt = lastDraw === 0 ? FRAME_MS : now - lastDraw;
      lastDraw = now;
      const drawStart = performance.now();

      const W = windowRef.current;
      const head = getDerivationHead();

      // --- cabeza de reproducción: avanza a 1 muestra/ms, nunca pasa de los datos ---
      let rh = renderHeadRef.current;
      if (head < 0) {
        rh = -1;
        scaleRef.current = {};
      } else if (!pausedRef.current) {
        if (rh < 0) {
          rh = Math.max(0, head - START_LAG);
        } else {
          rh += (dt * ECG_SAMPLE_RATE) / 1000;
          if (rh > head) rh = head;
          if (rh < head - MAX_LAG) rh = head - MAX_LAG; // ponerse al día tras una ráfaga
        }
      }
      renderHeadRef.current = rh;

      const endSample = Math.floor(rh);
      const start = endSample - W + 1;
      const vals = bufRef.current.subarray(0, W);

      for (let d = 0; d < DERIVACIONES.length; d++) {
        const deriv = DERIVACIONES[d];
        const canvas = canvasRefs.current[deriv.id];
        if (!canvas) continue;
        const fit = fitCanvas(canvas);
        if (!fit) continue;
        const { ctx, w, h } = fit;

        ctx.clearRect(0, 0, w, h);
        if (rh < 0) continue;

        drawGrid(ctx, w, h, start, W, endSample);

        fillDerivationWindow(deriv.id, vals, endSample);

        // Escala vertical: se ensancha al instante y se contrae despacio (sin saltos)
        let mn = Infinity;
        let mx = -Infinity;
        for (let i = 0; i < W; i++) {
          const v = vals[i];
          if (v !== v) continue;
          if (v < mn) mn = v;
          if (v > mx) mx = v;
        }
        if (mn === Infinity) continue; // ventana sin datos

        let sc = scaleRef.current[deriv.id];
        if (!sc) sc = scaleRef.current[deriv.id] = { lo: mn, hi: mx };
        sc.lo = mn < sc.lo ? mn : sc.lo + (mn - sc.lo) * 0.03;
        sc.hi = mx > sc.hi ? mx : sc.hi + (mx - sc.hi) * 0.03;

        const range = Math.max(sc.hi - sc.lo, 0.01);
        const lo = sc.lo - range * 0.15;
        const span = range * 1.3;

        drawTrace(ctx, w, h, vals, lo, span, deriv.color);

        ctx.fillStyle = "rgba(170,170,170,0.7)";
        ctx.font = "9px monospace";
        ctx.textAlign = "left";
        ctx.fillText(`${(range * 1000).toFixed(0)} mV·p-p`, 4, 10);
      }

      // --- eje de tiempo ---
      const axis = axisRef.current;
      if (axis) {
        const fa = fitCanvas(axis);
        if (fa) {
          const { ctx, w, h } = fa;
          ctx.clearRect(0, 0, w, h);
          if (rh >= 0) {
            const xs = (w - 1) / (W - 1);
            ctx.fillStyle = "#8aa";
            ctx.font = "10px monospace";
            ctx.textAlign = "center";
            for (let s = Math.ceil(start / 1000) * 1000; s <= endSample; s += 1000) {
              const x = (s - start) * xs;
              const label = `${s / ECG_SAMPLE_RATE} s`;
              ctx.fillText(label, Math.min(Math.max(x, 14), w - 14), h - 4);
            }
          }
        }
      }

      // Si dibujar tarda mucho, se baja la tasa de cuadros para no saturar el navegador
      drawCost = drawCost * 0.8 + (performance.now() - drawStart) * 0.2;
      frameMs = Math.min(MAX_FRAME_MS, Math.max(FRAME_MS, drawCost * 1.6));
    };

    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [getDerivationHead, fillDerivationWindow]);

  // ---------- Frecuencia cardíaca (1 vez por segundo, sobre la derivación II) ----------
  useEffect(() => {
    const timer = setInterval(() => {
      const head = getDerivationHead();
      if (head < 0) {
        setHeartRate(0);
        return;
      }
      const hrWindow = new Float32Array(5 * ECG_SAMPLE_RATE);
      fillDerivationWindow("II", hrWindow, head);
      const bpm = estimateBpm(hrWindow, ECG_SAMPLE_RATE);
      if (bpm > 0) setHeartRate(bpm);
    }, 1000);
    return () => clearInterval(timer);
  }, [getDerivationHead, fillDerivationWindow]);

  const handleClearAll = () => {
    if (window.confirm("¿Limpiar todas las gráficas? Esta acción no se puede deshacer.")) {
      clearDerivations();
      renderHeadRef.current = -1;
      scaleRef.current = {};
      setHeartRate(0);
    }
  };

  const totalSamples = Object.values(derivationStats).reduce((sum, s) => sum + (s.samples || 0), 0);
  const totalBatches = Object.values(derivationStats).reduce((sum, s) => sum + (s.batches || 0), 0);
  const derivationsWithData = Object.values(derivationStats).filter((s) => s.samples > 0).length;
  const elapsedSec = Math.max(0, ...Object.values(derivationStats).map((s) => s.lastSample + 1)) / ECG_SAMPLE_RATE;

  const btn = (color, border, bg) => ({
    backgroundColor: bg,
    color,
    border: `1px solid ${border}`,
    padding: "4px 10px",
    borderRadius: "4px",
    fontSize: "11px",
    fontWeight: "bold",
    cursor: "pointer",
    fontFamily: "monospace",
  });

  return (
    <div
      style={{
        width: "100vw",
        height: "100vh",
        backgroundColor: "#000000",
        color: "#FFFFFF",
        fontFamily: "monospace",
        overflow: "hidden",
        position: "relative",
        display: "flex",
        flexDirection: "column",
      }}
    >
      <Control />

      {/* Indicador de estado (compacto; el detalle aparece con el botón 📊) */}
      <div
        style={{
          position: "absolute",
          top: "76px",
          right: "12px",
          zIndex: 1000,
          backgroundColor: "rgba(0,0,0,0.78)",
          padding: showStats ? "8px 12px" : "4px 10px",
          borderRadius: "8px",
          border: `2px solid ${wsConnected ? "#00FF88" : "#FF6B6B"}`,
          fontSize: "9px",
          pointerEvents: "none",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
          <div
            style={{
              width: "8px",
              height: "8px",
              borderRadius: "50%",
              backgroundColor: wsConnected ? "#00FF88" : "#FF6B6B",
              animation: wsConnected ? "pulse 2s infinite" : "none",
            }}
          ></div>
          <div style={{ fontSize: "10px", fontWeight: "bold" }}>
            {wsConnected ? "CONECTADO" : "DESCONECTADO"}
          </div>
          {heartRate > 0 && (
            <div style={{ color: "#00FF88", fontWeight: "bold", fontSize: "11px" }}>
              ❤️ {heartRate} lpm
            </div>
          )}
        </div>

        {showStats && (
          <div style={{ color: "#AAA", lineHeight: "1.4", marginTop: "5px" }}>
            <div>📊 {derivationsWithData}/12 derivaciones</div>
            <div>📈 {totalSamples.toLocaleString()} muestras</div>
            <div>📦 {totalBatches} lotes</div>
            <div>⏱️ {elapsedSec.toFixed(1)} s de registro</div>
            <div style={{ color: "#00DDFF" }}>🪟 ventana {windowSec} s</div>
          </div>
        )}
      </div>

      {/* Panel de derivaciones: las 12 tiras se reparten el alto disponible */}
      <div
        style={{
          flex: 1,
          minHeight: 0,
          display: "flex",
          flexDirection: "column",
          backgroundColor: "#000000",
          paddingTop: "6px",
          overflowY: "auto",
        }}
      >
        {DERIVACIONES.map((deriv, index) => (
          <div
            key={deriv.id}
            style={{
              flex: "1 1 0",
              minHeight: "46px",
              display: "flex",
              flexDirection: "row",
              gap: "5px",
              padding: "2px 10px",
              borderBottom: index < DERIVACIONES.length - 1 ? "1px solid #003344" : "none",
            }}
          >
            <div
              style={{
                width: `${LABEL_WIDTH}px`,
                flexShrink: 0,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                color: "#FFFFFF",
                fontSize: "18px",
                fontWeight: "bold",
                backgroundColor: "rgba(0,0,0,0.7)",
                borderRadius: "8px",
                border: `2px solid ${deriv.color}`,
                flexDirection: "column",
              }}
            >
              <div>{deriv.name}</div>
              {showStats && (
                <div style={{ fontSize: "8px", color: "#AAA" }}>
                  {(derivationStats[deriv.id]?.samples || 0).toLocaleString()}
                </div>
              )}
            </div>

            <div style={{ flex: 1, minWidth: 0, position: "relative" }}>
              <canvas
                ref={(el) => { canvasRefs.current[deriv.id] = el; }}
                style={{ position: "absolute", inset: 0, width: "100%", height: "100%" }}
              />
              {!(derivationStats[deriv.id]?.samples > 0) && (
                <div
                  style={{
                    position: "absolute",
                    inset: 0,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    color: "#666",
                    fontSize: "11px",
                  }}
                >
                  Esperando datos de {deriv.name}...
                </div>
              )}
            </div>
          </div>
        ))}

        {/* Eje de tiempo */}
        <div style={{ display: "flex", gap: "5px", padding: "0 10px", flexShrink: 0, height: "18px" }}>
          <div style={{ width: `${LABEL_WIDTH}px`, flexShrink: 0 }} />
          <div style={{ flex: 1, minWidth: 0, position: "relative" }}>
            <canvas
              ref={axisRef}
              style={{ position: "absolute", inset: 0, width: "100%", height: "100%" }}
            />
          </div>
        </div>
      </div>

      {/* Panel inferior */}
      <div
        style={{
          flexShrink: 0,
          backgroundColor: "#001122",
          borderTop: "1px solid #004466",
          padding: "5px 15px",
          height: "40px",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          fontSize: "11px",
        }}
      >
        <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
          <button style={btn("#FF6B6B", "#FF6B6B", "#660000")} onClick={handleClearAll}>
            🗑️ LIMPIAR
          </button>

          <button
            style={btn("#00DDFF", "#00DDFF", paused ? "#006688" : "#003366")}
            onClick={() => setPaused((p) => !p)}
            title="Congelar la imagen (los datos siguen llegando)"
          >
            {paused ? "▶ SEGUIR" : "⏸ PAUSA"}
          </button>

          <button style={btn("#00DDFF", "#00DDFF", "#003366")} onClick={() => setShowStats(!showStats)}>
            {showStats ? "📊 -" : "📊 +"}
          </button>

          <div style={{ display: "flex", gap: "2px", marginLeft: "6px" }}>
            {WINDOW_OPTIONS.map((s) => (
              <button
                key={s}
                style={btn("#00DDFF", "#00DDFF", windowSec === s ? "#006688" : "#003366")}
                onClick={() => setWindowSec(s)}
                title={`Mostrar ${s} segundos`}
              >
                {s} s
              </button>
            ))}
          </div>
        </div>

        <div style={{ color: "#CCCCCC", fontSize: "10px" }}>
          {isRecording ? (
            <span style={{ color: "#00FF88", fontWeight: "bold" }}>
              🔴 GRABANDO | {elapsedSec.toFixed(1)} s | {totalSamples.toLocaleString()} muestras
            </span>
          ) : (
            <span>{ECG_SAMPLE_RATE} Hz • 3.3 V • {windowSec} s VISIBLES</span>
          )}
        </div>

        <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
          {heartRate > 0 && (
            <div
              style={{
                backgroundColor: "rgba(0,255,136,0.2)",
                padding: "4px 10px",
                borderRadius: "4px",
                border: "1px solid #00FF88",
                fontSize: "12px",
                fontWeight: "bold",
                color: "#00FF88",
              }}
            >
              ❤️ {heartRate} lpm
            </div>
          )}
        </div>
      </div>

      <style>{`
        @keyframes pulse {
          0%, 100% { opacity: 1; transform: scale(1); }
          50% { opacity: 0.7; transform: scale(1.1); }
        }
      `}</style>
    </div>
  );
};

export default DerivacionesECG;
