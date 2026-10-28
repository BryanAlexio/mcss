import React, { useEffect, useRef } from "react";

// Gráfica de señal en tiempo real (canvas) con ventana deslizante en segundos.
// Lee directamente del buffer circular del contexto, sin pasar por el estado de React.

const SAMPLE_RATE = 1000;
const BUF_SIZE = 16384;
const FRAME_MS = 33;
const MAX_FRAME_MS = 100;
const MAX_LAG = 600;
const START_LAG = 300;
const MARGIN = { left: 58, right: 12, top: 8, bottom: 22 };

const niceStep = (rough) => {
  const pow = Math.pow(10, Math.floor(Math.log10(rough)));
  const f = rough / pow;
  const nice = f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10;
  return nice * pow;
};

const SignalCanvas = ({
  slaveId,
  color = "#007bff",
  windowSec = 5,
  fixedRange = false,          // true: 0 – 3.3 V
  height = 400,
  getHead,
  fillWindow,
}) => {
  const canvasRef = useRef(null);
  const optsRef = useRef({});
  const stateRef = useRef({ rh: -1, lo: null, hi: null });
  const bufRef = useRef(new Float32Array(BUF_SIZE));

  optsRef.current = { windowSec, fixedRange, color };

  // Al cambiar de electrodo se reinicia la reproducción y la escala
  useEffect(() => {
    stateRef.current = { rh: -1, lo: null, hi: null };
  }, [slaveId]);

  useEffect(() => {
    let raf = 0;
    let lastDraw = 0;
    let frameMs = FRAME_MS;
    let drawCost = 0;

    const frame = (now) => {
      raf = requestAnimationFrame(frame);
      if (now - lastDraw < frameMs) return;
      const dt = lastDraw === 0 ? FRAME_MS : now - lastDraw;
      lastDraw = now;
      const t0 = performance.now();

      const canvas = canvasRef.current;
      if (!canvas) return;

      const dpr = window.devicePixelRatio || 1;
      const cw = canvas.clientWidth;
      const ch = canvas.clientHeight;
      if (cw === 0 || ch === 0) return;
      if (canvas.width !== Math.round(cw * dpr) || canvas.height !== Math.round(ch * dpr)) {
        canvas.width = Math.round(cw * dpr);
        canvas.height = Math.round(ch * dpr);
      }
      const ctx = canvas.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, cw, ch);

      const { windowSec: wsec, fixedRange: fixed, color: col } = optsRef.current;
      const W = Math.round(wsec * SAMPLE_RATE);
      const st = stateRef.current;

      const px = MARGIN.left;
      const py = MARGIN.top;
      const pw = cw - MARGIN.left - MARGIN.right;
      const ph = ch - MARGIN.top - MARGIN.bottom;

      // Marco del área de trazado
      ctx.fillStyle = "#fbfcfe";
      ctx.fillRect(px, py, pw, ph);
      ctx.strokeStyle = "#c9d1da";
      ctx.lineWidth = 1;
      ctx.strokeRect(px + 0.5, py + 0.5, pw - 1, ph - 1);

      const head = getHead(slaveId);
      if (head < 0) {
        st.rh = -1;
        st.lo = st.hi = null;
        ctx.fillStyle = "#8a94a0";
        ctx.font = "13px sans-serif";
        ctx.textAlign = "center";
        ctx.fillText("Esperando datos...", px + pw / 2, py + ph / 2);
        return;
      }

      // Cabeza de reproducción: avanza 1 muestra/ms sin pasar de los datos recibidos
      let rh = st.rh;
      if (rh < 0) {
        rh = Math.max(0, head - START_LAG);
      } else {
        rh += (dt * SAMPLE_RATE) / 1000;
        if (rh > head) rh = head;
        if (rh < head - MAX_LAG) rh = head - MAX_LAG;
      }
      st.rh = rh;

      const end = Math.floor(rh);
      const start = end - W + 1;
      const vals = bufRef.current.subarray(0, W);
      fillWindow(slaveId, vals, end);

      // ----- escala vertical -----
      let lo;
      let hi;
      if (fixed) {
        lo = 0;
        hi = 3.3;
      } else {
        let mn = Infinity;
        let mx = -Infinity;
        for (let i = 0; i < W; i++) {
          const v = vals[i];
          if (v !== v) continue;
          if (v < mn) mn = v;
          if (v > mx) mx = v;
        }
        if (mn === Infinity) { mn = 0; mx = 3.3; }
        if (st.lo === null) { st.lo = mn; st.hi = mx; }
        st.lo = mn < st.lo ? mn : st.lo + (mn - st.lo) * 0.03;
        st.hi = mx > st.hi ? mx : st.hi + (mx - st.hi) * 0.03;
        const range = Math.max(st.hi - st.lo, 0.02);
        lo = st.lo - range * 0.12;
        hi = st.hi + range * 0.12;
      }
      const span = hi - lo;
      const yOf = (v) => py + ph - ((v - lo) / span) * ph;

      // ----- rejilla horizontal + etiquetas de voltaje -----
      const step = niceStep(span / 5);
      ctx.font = "11px sans-serif";
      ctx.textAlign = "right";
      ctx.fillStyle = "#59636e";
      ctx.strokeStyle = "#e3e8ee";
      ctx.beginPath();
      for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) {
        const y = Math.round(yOf(v)) + 0.5;
        ctx.moveTo(px, y);
        ctx.lineTo(px + pw, y);
        ctx.fillText(`${v.toFixed(step < 0.1 ? 3 : 2)} V`, px - 6, y + 4);
      }
      ctx.stroke();

      // ----- rejilla vertical (cada 0.2 s fina, cada 1 s marcada) + etiquetas de tiempo -----
      const xs = pw / (W - 1);
      ctx.strokeStyle = "#eef1f5";
      ctx.beginPath();
      for (let s = Math.ceil(start / 200) * 200; s <= end; s += 200) {
        if (s % 1000 === 0) continue;
        const x = Math.round(px + (s - start) * xs) + 0.5;
        ctx.moveTo(x, py);
        ctx.lineTo(x, py + ph);
      }
      ctx.stroke();

      ctx.strokeStyle = "#d3dae3";
      ctx.fillStyle = "#59636e";
      ctx.textAlign = "center";
      ctx.beginPath();
      for (let s = Math.ceil(start / 1000) * 1000; s <= end; s += 1000) {
        const x = Math.round(px + (s - start) * xs) + 0.5;
        ctx.moveTo(x, py);
        ctx.lineTo(x, py + ph);
        ctx.fillText(`${s / SAMPLE_RATE} s`, Math.min(Math.max(x, px + 12), px + pw - 12), ch - 6);
      }
      ctx.stroke();

      // ----- trazo (envolvente mín/máx por columna: no se pierde ningún pico) -----
      ctx.save();
      ctx.beginPath();
      ctx.rect(px, py, pw, ph);
      ctx.clip();
      ctx.strokeStyle = col;
      ctx.lineWidth = 1.6;
      ctx.lineJoin = "bevel";
      ctx.beginPath();

      let pen = false;
      const cols = Math.floor(pw);
      if (W <= cols * 1.2) {
        for (let i = 0; i < W; i++) {
          const v = vals[i];
          if (v !== v) { pen = false; continue; }
          const x = px + i * xs;
          const y = yOf(v);
          if (!pen) { ctx.moveTo(x, y); pen = true; } else { ctx.lineTo(x, y); }
        }
      } else {
        const spp = W / cols;
        for (let c = 0; c < cols; c++) {
          const i0 = Math.floor(c * spp);
          const i1 = Math.max(i0 + 1, Math.min(W, Math.floor((c + 1) * spp)));
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
          const a = imn <= imx ? mn : mx;
          const b = imn <= imx ? mx : mn;
          const x = px + c + 0.5;
          const ya = yOf(a);
          const yb = yOf(b);
          if (!pen) { ctx.moveTo(x, ya); pen = true; } else { ctx.lineTo(x, ya); }
          if (yb !== ya) ctx.lineTo(x, yb);
        }
      }
      ctx.stroke();
      ctx.restore();

      // Si dibujar es lento, se baja la tasa de cuadros
      drawCost = drawCost * 0.8 + (performance.now() - t0) * 0.2;
      frameMs = Math.min(MAX_FRAME_MS, Math.max(FRAME_MS, drawCost * 1.6));
    };

    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [slaveId, getHead, fillWindow]);

  return (
    <canvas
      ref={canvasRef}
      style={{ width: "100%", height: `${height}px`, display: "block" }}
    />
  );
};

export default SignalCanvas;
