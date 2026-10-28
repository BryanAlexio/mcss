#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Figuras de los Capitulos 5 y 6 (MCSS) a partir de validacion_mcss.json.

Cada figura se guarda en un PNG independiente, con el tamano pensado para la
tesis (ancho completo = 6.3 in, media pagina = 3.2 in) y 300 dpi.

Uso:
    python generar_figuras_mcss.py validacion_mcss.json [carpeta_salida]
        [--hwsw comparacion_hw_sw.csv]        -> figura_latencia_sincronizacion.png
        [--sesiones tablas/sesiones.csv]      -> pcap_stats.png (columnas pcap_*)

Definiciones (las mismas del analizador):
    offset_inicio = primera muestra de la fuente - INICIO   (+ = arranco tarde)
    offset_fin    = ultima muestra de la fuente - FIN       (- = termino antes)
"""
import sys, json, argparse
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import SymLogNorm, ListedColormap
from matplotlib.patches import Patch
from matplotlib.lines import Line2D

# ------------------------------------------------------------------ estilo
W_FULL, W_HALF = 6.3, 3.2
C_BASAL, C_COND = "#0072B2", "#E69F00"          # condicion (Okabe-Ito, seguro para daltonismo)
C_OK, C_WARN, C_CRIT = "#2E9E5B", "#E69F00", "#C0392B"
C_INK, C_MUTED, C_GRID = "#222222", "#6b6b6b", "#dddddd"
C_CH41, C_CH21 = "#0072B2", "#D55E00"
COND_COLOR = {"basal": C_BASAL, "conduccion": C_COND}
COND_LABEL = {"basal": "Basal", "conduccion": "Conducción"}

SRC = [("EEG", "EEG"), ("ECG", "ECG"), ("LiDAR", "LiDAR"), ("VBOX sync", "VBOX"),
       ("Video main", "CAM 1"), ("Video secondary1", "CAM 2"), ("Video secondary2", "CAM 3")]
SRC_KEY = dict(SRC)

plt.rcParams.update({
    "font.size": 7.5, "axes.titlesize": 8, "axes.labelsize": 7.5,
    "xtick.labelsize": 7, "ytick.labelsize": 7, "legend.fontsize": 7,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.edgecolor": C_MUTED, "axes.labelcolor": C_INK, "text.color": C_INK,
    "xtick.color": C_INK, "ytick.color": C_INK,
    "axes.grid": True, "grid.color": C_GRID, "grid.linewidth": 0.5, "axes.axisbelow": True,
    "figure.dpi": 100, "savefig.dpi": 300, "savefig.bbox": "tight", "savefig.pad_inches": 0.04,
})


def save(fig, out, name):
    fig.savefig(out / name)
    plt.close(fig)
    print("  ok ", name)


def strip(ax, groups, values, colors, rng, jitter=0.16, size=14):
    for i, (v, c) in enumerate(zip(values, colors)):
        v = np.asarray(v, float)
        ax.scatter(i + rng.uniform(-jitter, jitter, len(v)), v, c=c, s=size, zorder=3,
                   edgecolor="white", linewidth=0.4)


def box(ax, data, widths=0.5):
    ax.boxplot(data, positions=range(len(data)), widths=widths, showfliers=False,
               medianprops=dict(color=C_INK, lw=1.0), boxprops=dict(color=C_MUTED, lw=0.8),
               whiskerprops=dict(color=C_MUTED, lw=0.8), capprops=dict(color=C_MUTED, lw=0.8))



def sym_ticks(ax, axis="y", lin=10, lo=None, hi=None):
    """Marcas legibles para escalas simlog: 0 y +-10^k (sin exponentes negativos)."""
    from matplotlib.ticker import FixedLocator, FuncFormatter
    a = ax.yaxis if axis == "y" else ax.xaxis
    lim = ax.get_ylim() if axis == "y" else ax.get_xlim()
    k0 = int(np.log10(lin)); ks = [k for k in range(k0, 7) if 10 ** k <= max(abs(lim[0]), abs(lim[1])) * 1.01]
    tk = sorted({0} | {s * 10 ** k for k in ks for s in (1, -1) if lim[0] <= s * 10 ** k <= lim[1]})
    a.set_major_locator(FixedLocator(tk))
    a.set_major_formatter(FuncFormatter(lambda v, _: "0" if v == 0 else f"{v:,.0f}".replace(",", " ").replace("-", "\u2212")))
    a.set_minor_locator(FixedLocator([]))

def cond_legend(ax, loc="best"):
    h = [Line2D([], [], marker="o", ls="", color=COND_COLOR[k], markeredgecolor="white",
                markersize=5, label=COND_LABEL[k]) for k in ("basal", "conduccion")]
    ax.legend(handles=h, loc=loc, frameon=False)


# ------------------------------------------------------------------ datos
def cargar(path):
    J = json.load(open(path, encoding="utf-8"))
    S = sorted(J["sesiones"], key=lambda s: s["start_epoch"])
    ses = pd.DataFrame([{
        "sid": s["session_id"], "persona": s["persona"], "sc": s["scenario"], "cond": s["condition"],
        "dur_min": s["duration_s"] / 60, "setup_ms": s["setup_latency_ms"],
        "veredicto": s["veredicto"], "alertas": list((s.get("alertas") or {}).keys()),
    } for s in S])
    ses["idx"] = np.arange(1, len(ses) + 1)
    fu = pd.DataFrame([{**{k: f[k] for k in ("source", "offset_inicio_ms", "offset_fin_ms",
                                            "coverage_pct", "deficit_s")}, "sid": s["session_id"],
                        "cond": s["condition"], "sc": s["scenario"]}
                       for s in S for f in s["fuentes"]])
    mu = pd.DataFrame([{**{k: m[k] for k in ("signal", "fs_hz", "fs_error_ppm", "period_std_ms",
                                            "period_max_ms", "n_channels")},
                        "sid": s["session_id"], "sc": s["scenario"], "cond": s["condition"]}
                       for s in S for m in s["muestreo"]])
    vi = pd.DataFrame([{"sid": s["session_id"], "camera": v["camera"], "gap_fin_s": v["gap_fin_s"],
                        "gap_ini_s": v["gap_ini_s"], "bitrate": v["bitrate_mbps"], "gb": v["size_gb"]}
                       for s in S for v in s["video"]])
    pa = pd.DataFrame([{"a": p["a"], "b": p["b"], "di": p["d_ini_ms"], "df": p["d_fin_ms"]}
                       for s in S for p in s["pares_fuentes"]])
    return J, ses, fu, mu, vi, pa


# ------------------------------------------------------------------ Cap. 5
def fig_cohorte(J, ses, out):
    esp = J["protocolo_esperado"]
    rango = {("pista", "basal"): esp["duracion_min"]["pista/basal"],
             ("pista", "conduccion"): esp["duracion_min"]["pista/conduccion"],
             ("ciudad", "basal"): esp["duracion_min"]["ciudad/basal"],
             ("ciudad", "conduccion"): esp["duracion_min"]["ciudad/conduccion"]}
    tol = esp.get("tolerancia_rango", 0.1)
    cols = [("pista", "basal"), ("pista", "conduccion"), ("ciudad", "basal"), ("ciudad", "conduccion")]
    pers = sorted(ses.persona.unique())
    tot = ses.groupby(["persona", "sc", "cond"]).agg(m=("dur_min", "sum"), n=("sid", "size"))
    M = np.full((len(pers), 4), np.nan); state = np.zeros((len(pers), 4), int); txt = {}
    for i, p in enumerate(pers):
        for j, (sc, c) in enumerate(cols):
            if (p, sc, c) in tot.index:
                m, n = tot.loc[(p, sc, c)]
                lo, hi = rango[(sc, c)]
                ok = lo * (1 - tol) <= m <= hi * (1 + tol)
                state[i, j] = 1 if ok else 2
                txt[(i, j)] = f"{m:.1f}" + (" (2 partes)" if n > 1 else "")
    cmap = ListedColormap(["#eceff1", "#7fc99a", "#f2a65a"])
    fig, ax = plt.subplots(figsize=(W_FULL * 0.8, 0.27 * len(pers) + 1.0))
    ax.imshow(state, cmap=cmap, vmin=0, vmax=2, aspect="auto")
    for (i, j), t in txt.items():
        ax.text(j, i, t, ha="center", va="center", fontsize=6.5)
    ax.set_xticks(range(4))
    ax.set_xticklabels(["Pista\nbasal", "Pista\nconducción", "Ciudad\nbasal", "Ciudad\nconducción"])
    ax.xaxis.tick_top(); ax.set_yticks(range(len(pers)))
    ax.set_yticklabels([f"P{k+1:02d}" for k in range(len(pers))])
    ax.grid(False)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.legend(handles=[Patch(color="#7fc99a", label="Dentro del intervalo previsto"),
                       Patch(color="#f2a65a", label="Fuera del intervalo"),
                       Patch(color="#eceff1", label="Sin sesión")],
              loc="upper center", bbox_to_anchor=(0.5, -0.01), ncol=3, frameon=False)
    ax.set_title("Duración total por participante (min)", pad=22)
    save(fig, out, "cohorte_personas.png")


# ------------------------------------------------------------------ Cap. 6
def fig_disponibilidad(ses, fu, out):
    M = np.array([[((fu.sid == s) & (fu.source == k)).any() for s in ses.sid] for k, _ in SRC], float)
    fig, ax = plt.subplots(figsize=(W_FULL, 2.1))
    ax.imshow(M, aspect="auto", cmap=ListedColormap(["#e3e6e8", "#2c7fb8"]), interpolation="nearest")
    ax.set_yticks(range(len(SRC))); ax.set_yticklabels([l for _, l in SRC])
    ax.set_xticks(np.arange(0, len(ses), 5)); ax.set_xticklabels(np.arange(1, len(ses) + 1, 5))
    ax.set_xlabel("Sesión (orden cronológico)"); ax.grid(False)
    ax.legend(handles=[Patch(color="#2c7fb8", label="Disponible"), Patch(color="#e3e6e8", label="No registrada")],
              loc="upper center", bbox_to_anchor=(0.5, -0.32), ncol=2, frameon=False)
    save(fig, out, "03_heatmap_estados.png")


def fig_duracion(ses, out):
    grupos = [("pista", "basal"), ("pista", "conduccion"), ("ciudad", "basal"), ("ciudad", "conduccion")]
    lab = ["Pista\nbasal", "Pista\nconducción", "Ciudad\nbasal", "Ciudad\nconducción"]
    data = [ses[(ses.sc == a) & (ses.cond == b)].dur_min.values for a, b in grupos]
    rng = np.random.default_rng(1)
    fig, ax = plt.subplots(figsize=(W_FULL * 0.6, 2.6))
    box(ax, data); strip(ax, grupos, data, [COND_COLOR[b] for _, b in grupos], rng)
    ax.set_xticks(range(4)); ax.set_xticklabels([f"{l}\n(n={len(d)})" for l, d in zip(lab, data)])
    ax.set_ylabel("Duración de la sesión (min)")
    save(fig, out, "duracion_grupos.png")


def fig_latencia(csv, out):
    c = pd.read_csv(csv)
    hw = pd.concat([c.dt_hw_ini_us, c.dt_hw_fin_us]); sw = pd.concat([c.lat_sw_ini_ms, c.lat_sw_fin_ms])
    h = hw[hw < 150]
    fig, ax = plt.subplots(1, 2, figsize=(W_FULL, 2.4))
    ax[0].hist(h, bins=np.arange(50, 98, 2), color=C_BASAL, edgecolor="white", lw=0.5)
    ax[0].axvline(h.mean(), color=C_INK, lw=0.8, ls="--")
    ax[0].set_xlabel("Retardo flanco GPIO 10 → trama UART (µs)"); ax[0].set_ylabel("Mediciones")
    ax[0].set_title(f"(a) Hardware: N={len(h)}, media {h.mean():.1f} µs, σ={h.std(ddof=0):.1f} µs", loc="left")
    ax[1].hist(sw, bins=np.arange(-3, 3.01, 0.4), color=C_COND, edgecolor="white", lw=0.5)
    ax[1].axvline(sw.mean(), color=C_INK, lw=0.8, ls="--")
    ax[1].set_xlabel("Instante de procesamiento − instante reconstruido (ms)")
    ax[1].set_title(f"(b) Software: N={len(sw)}, media {sw.mean():.3f} ms, σ={sw.std(ddof=0):.3f} ms", loc="left")
    fig.tight_layout()
    save(fig, out, "figura_latencia_sincronizacion.png")


def _offset_strip(fu, col, titulo, out, name):
    fig, ax = plt.subplots(figsize=(W_HALF, 2.9))
    rng = np.random.default_rng(0)
    data, cols = [], []
    for k, _ in SRC:
        g = fu[fu.source == k].dropna(subset=[col]); data.append(g[col].values)
    box(ax, data, 0.5)
    for i, (k, _) in enumerate(SRC):
        g = fu[fu.source == k].dropna(subset=[col])
        ax.scatter(i + rng.uniform(-0.16, 0.16, len(g)), g[col], c=[COND_COLOR[x] for x in g.cond],
                   s=12, zorder=3, edgecolor="white", linewidth=0.3)
    ax.set_yscale("symlog", linthresh=10); ax.axhline(0, color=C_INK, lw=0.7, ls="--")
    ax.set_xticks(range(len(SRC))); ax.set_xticklabels([l for _, l in SRC], rotation=35, ha="right")
    ax.set_ylabel("ms (escala simlog)"); sym_ticks(ax, "y")
    cond_legend(ax, "upper left" if "inicio" in col else "upper right")
    save(fig, out, name)


def fig_offsets(fu, out):
    _offset_strip(fu, "offset_inicio_ms", "inicio", out, "offset_inicio_fuente.png")
    _offset_strip(fu, "offset_fin_ms", "fin", out, "offset_fin_fuente.png")


def fig_pares(pa, out):
    ordenN = ["LiDAR", "EEG", "ECG", "Video main", "Video secondary1", "Video secondary2", "VBOX sync"]
    lab = ["LiDAR", "EEG", "ECG", "CAM 1", "CAM 2", "CAM 3", "VBOX"]
    for col, name in (("di", "offset_pares_inicio.png"), ("df", "offset_pares_fin.png")):
        M = np.zeros((7, 7))
        med = pa.groupby(["a", "b"])[col].median()
        for (a, b), v in med.items():
            if a in ordenN and b in ordenN:
                i, j = ordenN.index(a), ordenN.index(b); M[i, j] = v; M[j, i] = -v
        fig, ax = plt.subplots(figsize=(W_HALF, 2.9))
        im = ax.imshow(M, cmap="RdBu_r", norm=SymLogNorm(10, vmin=-400, vmax=400))
        for i in range(7):
            for j in range(7):
                ax.text(j, i, f"{M[i,j]:.0f}", ha="center", va="center", fontsize=6,
                        color="white" if abs(M[i, j]) > 120 else C_INK)
        ax.set_xticks(range(7)); ax.set_xticklabels(lab, rotation=35, ha="right")
        ax.set_yticks(range(7)); ax.set_yticklabels(lab); ax.grid(False)
        cb = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.03); cb.ax.tick_params(labelsize=6)
        cb.set_label("ms (fila − columna)", fontsize=6.5)
        save(fig, out, name)


def fig_sesion(sid, fu, out, name):
    g = fu[fu.sid == sid].set_index("source").reindex([k for k, _ in SRC])
    fig, ax = plt.subplots(figsize=(W_HALF, 2.7))
    y = np.arange(len(SRC)); h = 0.36
    ax.barh(y - h / 2, g.offset_inicio_ms, h, color=C_BASAL, label="Inicio")
    ax.barh(y + h / 2, g.offset_fin_ms, h, color=C_CRIT, label="Fin")
    ax.set_xscale("symlog", linthresh=10); ax.axvline(0, color=C_INK, lw=0.7)
    ax.set_yticks(y); ax.set_yticklabels([l for _, l in SRC]); ax.invert_yaxis()
    ax.set_xlabel("ms (escala simlog)"); sym_ticks(ax, "x")
    ax.legend(frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.25), ncol=2)
    ax.grid(axis="y", visible=False)
    for yi, (a, b) in enumerate(zip(g.offset_inicio_ms, g.offset_fin_ms)):
        ax.text(1.0, yi, f"{a:+.0f} / {b:+.0f}", transform=ax.get_yaxis_transform(), fontsize=6,
                va="center", ha="left", color=C_MUTED)
    save(fig, out, name)


def fig_diagnostico(ses, out):
    cod = ["INICIO_DESFASADO", "FIN_DESFASADO", "COBERTURA_BAJA", "VIDEO_TRUNCADO",
           "VBOX_FUERA_VENTANA", "FS_ANOMALA", "VIDEO_FRAMES"]
    etq = ["Inicio desfasado", "Fin desfasado", "Cobertura baja", "Video truncado",
           "VBOX fuera de ventana", "Frecuencia anómala", "Fotogramas"]
    col = {"OK": C_OK, "REVISAR": C_WARN, "CRITICO": C_CRIT}
    n = len(ses)
    fig, (a0, a1) = plt.subplots(2, 1, figsize=(W_FULL, 3.0), sharex=True,
                                 gridspec_kw={"height_ratios": [1, 7], "hspace": 0.06})
    a0.imshow([[0]] * 0 or np.zeros((1, n)), aspect="auto", cmap=ListedColormap(["white"]))
    for i, v in enumerate(ses.veredicto):
        a0.add_patch(plt.Rectangle((i - 0.5, -0.5), 1, 1, color=col[v]))
    a0.set_xlim(-0.5, n - 0.5); a0.set_ylim(-0.5, 0.5); a0.set_yticks([0]); a0.set_yticklabels(["Veredicto"])
    a0.grid(False); a0.spines["left"].set_visible(False); a0.spines["bottom"].set_visible(False)
    a0.tick_params(length=0)
    for r, c in enumerate(cod):
        for i, al in enumerate(ses.alertas):
            if c in al:
                a1.add_patch(plt.Rectangle((i - 0.5, r - 0.5), 1, 1, color=C_CRIT, lw=0))
    a1.set_ylim(len(cod) - 0.5, -0.5); a1.set_yticks(range(len(cod))); a1.set_yticklabels(etq)
    a1.set_xticks(np.arange(0, n, 5)); a1.set_xticklabels(np.arange(1, n + 1, 5))
    a1.set_xlabel("Sesión (orden cronológico)"); a1.grid(axis="y", visible=False)
    a0.legend(handles=[Patch(color=col[k], label=l) for k, l in (("OK", "Correcta"), ("REVISAR", "Por revisar"),
                                                               ("CRITICO", "Crítica"))],
              loc="lower center", bbox_to_anchor=(0.5, 1.05), ncol=3, frameon=False)
    save(fig, out, "diagnostico_sesiones.png")


def fig_cobertura(fu, out):
    fig, ax = plt.subplots(figsize=(W_HALF, 2.9)); rng = np.random.default_rng(2)
    data = [(100 - fu[fu.source == k].coverage_pct).clip(lower=1e-3).values for k, _ in SRC]
    box(ax, data)
    for i, (k, _) in enumerate(SRC):
        g = fu[fu.source == k]
        ax.scatter(i + rng.uniform(-0.16, 0.16, len(g)), (100 - g.coverage_pct).clip(lower=1e-3),
                   c=[COND_COLOR[x] for x in g.cond], s=12, zorder=3, edgecolor="white", linewidth=0.3)
    ax.set_yscale("log"); ax.axhline(0.5, color=C_CRIT, lw=0.9, ls="--")
    ax.text(len(SRC) - 0.5, 0.6, "criterio: 0.5 %", ha="right", fontsize=6.5, color=C_CRIT)
    ax.set_xticks(range(len(SRC))); ax.set_xticklabels([l for _, l in SRC], rotation=35, ha="right")
    ax.set_ylabel("Tiempo sin datos (% de la sesión, log; ≤0.001 en el piso)")
    cond_legend(ax, "upper left")
    save(fig, out, "cobertura_fuente.png")


def fig_deficit(fu, out):
    fig, ax = plt.subplots(figsize=(W_HALF, 2.9)); rng = np.random.default_rng(3)
    data = [fu[fu.source == k].deficit_s.values for k, _ in SRC]
    box(ax, data)
    for i, (k, _) in enumerate(SRC):
        g = fu[fu.source == k]
        ax.scatter(i + rng.uniform(-0.16, 0.16, len(g)), g.deficit_s, c=[COND_COLOR[x] for x in g.cond],
                   s=12, zorder=3, edgecolor="white", linewidth=0.3)
    ax.set_yscale("symlog", linthresh=1); ax.axhline(0, color=C_INK, lw=0.7, ls="--")
    ax.set_xticks(range(len(SRC))); ax.set_xticklabels([l for _, l in SRC], rotation=35, ha="right")
    ax.set_ylabel("Duración sesión − duración fuente (s, simlog)"); sym_ticks(ax, "y", lin=1)
    cond_legend(ax, "upper left")
    save(fig, out, "deficit_fuente.png")


def fig_eeg(ses, mu, out):
    e = mu[mu.signal == "EEG"].merge(ses[["sid", "idx"]], on="sid").sort_values("idx")
    cc = np.where(e.n_channels < 30, C_CH21, C_CH41)
    leg = [Line2D([], [], marker="o", ls="", color=C_CH41, markeredgecolor="white", markersize=5, label="38–41 canales"),
           Line2D([], [], marker="o", ls="", color=C_CH21, markeredgecolor="white", markersize=5, label="21 canales")]
    fig, ax = plt.subplots(figsize=(W_HALF, 2.6))
    ax.scatter(e.idx, e.fs_error_ppm, c=cc, s=14, zorder=3, edgecolor="white", linewidth=0.3)
    ax.axhline(500, color=C_CRIT, lw=0.9, ls="--"); ax.text(63, 520, "criterio: 500 ppm", fontsize=6.5, color=C_CRIT, ha="right")
    ax.set_xlabel("Sesión (orden cronológico)"); ax.set_ylabel("Error de frecuencia de muestreo (ppm)")
    ax.set_ylim(0, 700); ax.legend(handles=leg, frameon=False, loc="upper left", bbox_to_anchor=(0.0, 0.62))
    save(fig, out, "eeg_drift_ppm.png")
    fig, ax = plt.subplots(figsize=(W_HALF, 2.6))
    ax.scatter(e.idx, e.period_std_ms, c=cc, s=14, zorder=3, edgecolor="white", linewidth=0.3)
    ax.axhline(0.5, color=C_CRIT, lw=0.9, ls="--"); ax.text(63, 0.52, "criterio: 0.5 ms", fontsize=6.5, color=C_CRIT, ha="right")
    ax.set_ylim(0, 0.65); ax.set_xlabel("Sesión (orden cronológico)")
    ax.set_ylabel("Desviación estándar del periodo (ms)")
    save(fig, out, "eeg_jitter.png")


def fig_video(vi, out):
    cams = [("Video main", "CAM 1"), ("Video secondary1", "CAM 2"), ("Video secondary2", "CAM 3")]
    fig, ax = plt.subplots(figsize=(W_HALF, 2.6)); rng = np.random.default_rng(4)
    data = [vi[vi.camera == k].gap_fin_s.values for k, _ in cams]
    box(ax, data)
    for i, d in enumerate(data):
        ax.scatter(i + rng.uniform(-0.16, 0.16, len(d)), d, c=[C_CRIT if v > 1 else C_BASAL for v in d],
                   s=14, zorder=3, edgecolor="white", linewidth=0.3)
    ax.set_yscale("symlog", linthresh=1); ax.axhline(1, color=C_CRIT, lw=0.9, ls="--")
    ax.text(2.45, 1.15, "criterio: 1 s", ha="right", fontsize=6.5, color=C_CRIT)
    ax.set_xticks(range(3)); ax.set_xticklabels([l for _, l in cams])
    ax.set_ylabel("Segundos que terminó antes de FIN (simlog)"); sym_ticks(ax, "y", lin=1)
    save(fig, out, "video_truncamiento.png")


def fig_pcap(csv, out):
    s = pd.read_csv(csv); s = s[s.get("has_lidar", True) == True] if "has_lidar" in s else s
    fig, ax = plt.subplots(1, 2, figsize=(W_FULL, 2.4))
    ax[0].bar(range(len(s)), s.pcap_rate_pkt_s, color=C_BASAL, width=0.8)
    ax[0].set_ylabel("Tasa de recepción (paquetes/s)"); ax[0].set_xlabel("Sesión con LiDAR")
    ax[1].bar(range(len(s)), s.pcap_size_gb, color=C_COND, width=0.8)
    ax[1].set_ylabel("Tamaño del archivo PCAP (GB)"); ax[1].set_xlabel("Sesión con LiDAR")
    fig.tight_layout(); save(fig, out, "pcap_stats.png")
    print("     PCAP: tasa", s.pcap_rate_pkt_s.agg(["mean", "min", "max"]).round(0).to_dict(),
          "| GB", s.pcap_size_gb.agg(["mean", "min", "max"]).round(2).to_dict())


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("json"); ap.add_argument("salida", nargs="?", default="figuras")
    ap.add_argument("--hwsw"); ap.add_argument("--sesiones")
    a = ap.parse_args()
    out = Path(a.salida); (out / "Ch5").mkdir(parents=True, exist_ok=True); (out / "Ch6").mkdir(exist_ok=True)
    J, ses, fu, mu, vi, pa = cargar(a.json)
    print(f"{len(ses)} sesiones, {ses.persona.nunique()} participantes")
    print("Capitulo 5"); fig_cohorte(J, ses, out / "Ch5")
    o = out / "Ch6"; print("Capitulo 6")
    fig_disponibilidad(ses, fu, o); fig_duracion(ses, o)
    if a.hwsw: fig_latencia(a.hwsw, o)
    fig_offsets(fu, o); fig_pares(pa, o)
    ej = {"pista": "andresqq2_pista_20260923_172116", "ciudad": "andres1_ciudad_20260928_125214"}
    for k, sid in ej.items():
        if sid in set(ses.sid): fig_sesion(sid, fu, o, f"sesion_ejemplo_{k}.png")
    fig_diagnostico(ses, o); fig_cobertura(fu, o); fig_deficit(fu, o)
    fig_eeg(ses, mu, o); fig_video(vi, o)
    if a.sesiones: fig_pcap(a.sesiones, o)
    else: print("  (pcap_stats.png omitido: falta --sesiones tablas/sesiones.csv)")


if __name__ == "__main__":
    main()
