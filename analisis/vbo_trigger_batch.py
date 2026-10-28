"""
vbo_trigger_batch.py - Recorre TODAS las sesiones del MCSS y calcula, con los
archivos .vbo ORIGINALES (sin recortar), las métricas de telemetría que usan los
Capítulos 6, 7 y 8:

  * desfase del reloj del VBOX respecto a la computadora, medido con el canal
    Trigger (primer flanco HIGH->LOW  vs  INICIO)
  * discrepancia al final (flanco LOW->HIGH  vs  FIN)
  * deriva relativa (ppm) = duración Trigger / duración eventos - 1
  * frecuencia efectiva de muestreo, cobertura y margen del registro respecto a
    la ventana INICIO->FIN
  * resumen estadístico (n, media, desv. est., mediana, mín, máx)

USO
  python vbo_trigger_batch.py RAIZ [--out salida] [--csv nombre.csv]
  python vbo_trigger_batch.py --vbo 1VBOX0024.vbo --events sesion_events.txt   (una sola sesión)

  RAIZ = cualquier carpeta que contenga sesiones (se busca a cualquier profundidad, así que
         sirve la carpeta del día, p. ej. pista_23_09_2026, o la de toda la campaña):
         RAIZ/david1_pista_20260923_162333/VBOX0075.VBO  (+ VBOX0075_trigSync.vbo, ignorado)
                                           Events/*_events_*.txt

Búsqueda del .vbo en cada sesión (primer acierto):
  1. un .vbo/.VBO en la raíz de la sesión (estructura actual)
  2. si no, en originales/, VBOX/ o VBOX/originales/
  (se ignoran los *_trigSync.vbo, que ya están recortados y corregidos)

Sólo usa la biblioteca estándar.
"""
import argparse
import csv
import datetime
import io
import statistics as st
import sys
from pathlib import Path

VERSION = "2026-10-13 v3 tiempos-neutros + horas UTC/local"
THRESH = 1.5
CHANNEL = "trigger"


# ---------------------------------------------------------------- utilidades
def hms_to_s(txt):
    """HHMMSS.mmm -> segundos desde medianoche."""
    t = float(txt)
    n = int(t)
    return (n // 10000) * 3600 + ((n % 10000) // 100) * 60 + n % 100 + (t - n)


# ---- conversiones de hora (mismas que vbo_trigger_sync.py: 6 decimales, envuelve a 24 h) ----
def _split_us(secs):
    us = int(round(secs * 1_000_000)) % (86400 * 1_000_000)
    s, f = divmod(us, 1_000_000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return h, m, s, f


def seconds_to_hms(secs):
    """segundos UTC (desenrollados o no) -> HH:MM:SS.ffffff, envuelto a 24 h."""
    h, m, s, f = _split_us(secs)
    return f"{h:02d}:{m:02d}:{s:02d}.{f:06d}"


def norm_half_day(x):
    """Diferencia de horas normalizada a [-12 h, +12 h)."""
    return (x + 43200.0) % 86400.0 - 43200.0


def epoch_to_local_str(epoch):
    dt = datetime.datetime.fromtimestamp(epoch)      # zona de la PC, igual que el sync
    return dt.strftime("%H:%M:%S.") + f"{dt.microsecond:06d}"


def read_events(path):
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    ev = {}
    for row in csv.DictReader(io.StringIO(text.replace("\r\n", "\n"))):
        row = {k.strip(): v for k, v in row.items() if k}
        name, ep = (row.get("Event") or "").strip(), (row.get("EpochTime") or "").strip()
        if name and ep:
            ev[name] = float(ep)          # el último gana (igual que el script original)
    return ev


def read_vbo(path):
    """Devuelve (tiempos desenrollados [s], trigger [V]) leyendo sólo 2 columnas."""
    trig_col = None
    times, trig = [], []
    in_data = False
    day, prev = 0, None
    with open(path, "r", encoding="latin-1", newline=None) as f:
        names_next = False
        for line in f:
            s = line.strip()
            if not in_data:
                low = s.lower()
                if low == "[column names]":
                    names_next = True
                elif names_next:
                    cols = s.split()
                    for j, c in enumerate(cols):
                        if c.lower() == CHANNEL:
                            trig_col = j
                    names_next = False
                elif low == "[data]":
                    if trig_col is None:
                        raise ValueError("canal Trigger no encontrado")
                    in_data = True
                continue
            if not s:
                continue
            p = s.split()
            if len(p) <= trig_col:
                continue
            try:
                t = hms_to_s(p[1])
                v = float(p[trig_col])
            except ValueError:
                continue
            if prev is not None and t + day * 86400 < prev - 43200:
                day += 1                      # cruce de medianoche
            t += day * 86400
            prev = t
            times.append(t)
            trig.append(v)
    return times, trig


def auto_threshold(trig):
    """Umbral = punto medio entre los dos niveles reales del canal, sea cual sea su polaridad
    (reposo en +10 V y sesión en 0 V, o reposo en -10 V y sesión en 0 V, etc.).
    Los niveles se obtienen como los dos grupos más poblados (cubetas de 0.5 V) separados >= 1 V;
    así el nivel de reposo cuenta aunque dure <2 % del registro (los percentiles no lo veían).
    Si no hay dos niveles separados >= 1 V se usa el umbral fijo de 1.5 V (sin flancos)."""
    bins = {}
    for v in trig:
        b = bins.setdefault(round(v / 0.5), [0, 0.0])
        b[0] += 1
        b[1] += v
    ranked = sorted(bins.values(), key=lambda b: -b[0])
    main = ranked[0][1] / ranked[0][0]
    second = None
    for n, s in ranked[1:]:
        if n >= 5 and abs(s / n - main) >= 1.0:       # >= 5 muestras (50 ms) y >= 1 V de separación
            second = s / n
            break
    if second is None:
        return (THRESH, min(trig), max(trig))
    lo, hi = sorted((main, second))
    return ((lo + hi) / 2.0, lo, hi)


def transitions(times, trig, thr=THRESH):
    out, prev = [], None
    for t, v in zip(times, trig):
        hi = v > thr
        if prev is not None and hi != prev:
            out.append(("HL" if prev else "LH", t))
        prev = hi
    return out


def pick_edges(tr, dur_ev):
    """Elige (flanco_inicio, flanco_fin) SIN depender de la polaridad: el par de flancos (i<j,
    cualquier sentido) cuya separación se parece más a la duración de los eventos FIN-INICIO.
    Un par de sentidos opuestos (entrada/salida de la meseta de sesión) gana en caso de empate
    práctico (penalización de 10 ms al par del mismo sentido). Ignora así pulsos espurios al
    inicio/final, y sesiones donde el trigger quedó invertido o con un solo tipo de flanco.
    Devuelve None si hay < 2 flancos."""
    best = None
    for i in range(len(tr)):
        for j in range(i + 1, len(tr)):
            cost = abs((tr[j][1] - tr[i][1]) - dur_ev) + (0.0 if tr[i][0] != tr[j][0] else 0.01)
            if best is None or cost < best[0]:
                best = (cost, tr[i], tr[j])
    return None if best is None else (best[1], best[2])


def _vbos(d):
    """.vbo/.VBO (cualquier mayúscula) originales, sin los _trigSync."""
    if not d.is_dir():
        return []
    return [p for p in sorted(d.iterdir())
            if p.is_file() and p.suffix.lower() == ".vbo" and "trigsync" not in p.name.lower()]


def find_vbo(sess):
    # estructura actual: VBOX00NN.VBO en la raíz de la sesión (junto a VBOX00NN_trigSync.vbo)
    for sub in (".", "originales", "VBOX", "Vbox", "VBOX/originales"):
        c = _vbos(sess if sub == "." else sess / sub)
        if c:
            return c[0]
    return None


def find_sessions(root):
    """Toda carpeta (a cualquier profundidad) que contenga una subcarpeta Events/."""
    root = Path(root)
    found = [p.parent for p in root.rglob("*") if p.is_dir() and p.name.lower() == "events"]
    return sorted(set(found)) or sorted(p for p in root.iterdir() if p.is_dir())


def find_events(sess):
    for d in (sess / "Events", sess / "events", sess):
        if d.is_dir():
            c = [p for p in sorted(d.iterdir()) if p.is_file() and p.suffix.lower() == ".txt"
                 and "events" in p.name.lower()]
            if c:
                return c[0]
    return None


# ------------------------------------------------------------------- análisis
def analyze(vbo, evp, name):
    ev = read_events(evp)
    if "INICIO" not in ev or "FIN" not in ev:
        raise ValueError("faltan INICIO/FIN")
    ini_ep, fin_ep = ev["INICIO"], ev["FIN"]
    ini = ini_ep % 86400.0                       # s desde medianoche UTC
    dur_ev = fin_ep - ini_ep
    fin = ini + dur_ev

    t, v = read_vbo(vbo)
    if len(t) < 10:
        raise ValueError("VBO vacío")
    thr, lo_v, hi_v = auto_threshold(v)
    tr = transitions(t, v, thr)
    r = {"sesion": name, "vbo": Path(vbo).name, "n_muestras": len(t),
         "trig_umbral_V": thr, "trig_nivel_bajo_V": lo_v, "trig_nivel_alto_V": hi_v,
         "n_HL": sum(a == "HL" for a, _ in tr), "n_LH": sum(a == "LH" for a, _ in tr)}

    # alinear el reloj de la VBO con el día de INICIO (resuelve días enteros y cruce 00:00)
    k = round((ini - t[0]) / 86400.0)
    shift = k * 86400.0
    t = [x + shift for x in t]
    tr = [(a, b + shift) for a, b in tr]

    r["evento_INICIO_utc"] = seconds_to_hms(ini)
    r["evento_FIN_utc"] = seconds_to_hms(fin)
    r["evento_INICIO_local"] = epoch_to_local_str(ini_ep)
    r["evento_FIN_local"] = epoch_to_local_str(fin_ep)
    r["dur_eventos_s"] = dur_ev
    r["dur_vbo_s"] = t[-1] - t[0]
    r["frec_efectiva_hz"] = (len(t) - 1) / (t[-1] - t[0])
    r["periodo_mediano_ms"] = st.median(b - a for a, b in zip(t, t[1:])) * 1000
    r["vbo_ini_menos_INICIO_s"] = t[0] - ini     # >0: el registro empieza después
    r["vbo_fin_menos_FIN_s"] = t[-1] - fin       # >0: el registro termina después
    a_, b_ = max(t[0], ini), min(t[-1], fin)
    r["cobertura_pct"] = 100.0 * max(0.0, b_ - a_) / dur_ev
    r["max_intervalo_ms"] = max(y - x for x, y in zip(t, t[1:])) * 1000

    # INICIO/FIN del Trigger = par de flancos que mejor coincide con la duración de los eventos,
    # IGUAL para cualquier polaridad. El sentido de los flancos solo se reporta como referencia:
    #   normal    = reposo alto, sesión en nivel bajo (HL al INICIO, LH al FIN)
    #   INVERTIDA = reposo bajo, sesión en nivel alto/0 V (LH al INICIO, HL al FIN)
    hl = None
    l = None
    if tr:
        pair = pick_edges(tr[:200], dur_ev)
        if pair:
            s_edge, e_edge = pair
            l = e_edge[1]
            r["dur_trigger_s"] = l - s_edge[1]
            r["dif_dur_trigger_eventos_ms"] = (r["dur_trigger_s"] - dur_ev) * 1000
            r["deriva_ppm"] = (r["dur_trigger_s"] / dur_ev - 1.0) * 1e6
            r["trig_fin_menos_FIN_ms"] = (l - fin) * 1000         # marca de FIN (flanco de fin)
        else:
            # un solo flanco: es el INICIO; sin flanco de fin se toma el ULTIMO registro del VBO como FIN
            s_edge = tr[0]
            l = t[-1]
            r["dur_trigger_s"] = l - s_edge[1]
            r["dif_dur_trigger_eventos_ms"] = (r["dur_trigger_s"] - dur_ev) * 1000
            r["deriva_ppm"] = (r["dur_trigger_s"] / dur_ev - 1.0) * 1e6
            r["trig_fin_menos_FIN_ms"] = (l - fin) * 1000
            r["nota"] = "sin flanco de fin: FIN = ultimo registro del VBO"
        if len(tr) != 2:
            r["nota_flancos"] = f"{len(tr)} flancos en el Trigger: se uso el par que coincide con FIN-INICIO"
        h = s_edge[1]
        hl = [s_edge]
        r["trig_polaridad"] = "normal" if s_edge[0] == "HL" else "INVERTIDA"   # solo referencia
        r["trig_inicio_menos_INICIO_ms"] = (h - ini) * 1000          # marca de INICIO (flanco de inicio)
        # correccion de reloj que aplica el sync: INICIO - inicio del Trigger (norm. a +-12 h)
        r["offset_reloj_s"] = norm_half_day(ini - h)
        if l is not None:
            r["delta_fin_corregido_s"] = norm_half_day((l + r["offset_reloj_s"]) - fin)
        r["trig_inicio_utc"] = seconds_to_hms(h)
        r["trig_fin_utc"] = seconds_to_hms(l) if l is not None else ""
        # nivel de la sesión (mediana entre las marcas) y de reposo, para documentar la polaridad
        w_end = l if l is not None else h + dur_ev
        in_v = sorted(x for tt, x in zip(t, v) if h <= tt <= w_end)
        if in_v:
            r["trig_nivel_sesion_V"] = in_v[len(in_v) // 2]
    else:
        r["nota"] = (f"sin flancos (niveles {lo_v:.2f}-{hi_v:.2f} V, umbral {thr:.2f} V)")
    # todos los flancos, en segundos relativos a INICIO (para revisar sesiones atípicas)
    r["flancos_s_rel_INICIO"] = " ".join(f"{a}@{x - ini:.3f}" for a, x in tr)

    # métricas SOLO dentro de la ventana de la sesión (entre flancos; si falta alguno, usa INICIO/FIN
    # reconstruidos con el desfase del primer flanco) - es lo que importa para cobertura y huecos
    w0 = hl[0][1] if hl else ini
    w1 = (l if (hl and l is not None) else w0 + dur_ev)
    win = [x for x in t if w0 <= x <= w1]
    if len(win) > 10:
        r["ventana_n_muestras"] = len(win)
        r["ventana_frec_hz"] = (len(win) - 1) / (win[-1] - win[0])
        r["ventana_cobertura_pct"] = 100.0 * min(1.0, (len(win) * 0.01) / (w1 - w0))
        r["ventana_max_intervalo_ms"] = max(b - a for a, b in zip(win, win[1:])) * 1000
        r["ventana_huecos_gt_50ms"] = sum(1 for a, b in zip(win, win[1:]) if b - a > 0.05)
    return r


def summary(rows, key, unit=""):
    xs = [r[key] for r in rows if key in r and r[key] is not None]
    if not xs:
        return f"  {key}: sin datos"
    sd = st.stdev(xs) if len(xs) > 1 else 0.0
    return (f"  {key:<30} n={len(xs):>2}  media={st.mean(xs):>11.3f}  sd={sd:>10.3f}  "
            f"mediana={st.median(xs):>11.3f}  min={min(xs):>11.3f}  max={max(xs):>11.3f} {unit}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?")
    ap.add_argument("--vbo")
    ap.add_argument("--events")
    ap.add_argument("--out", default=".")
    ap.add_argument("--csv", default="vbo_trigger_resumen_v2.csv")
    a = ap.parse_args()

    print(f"vbo_trigger_batch version {VERSION}")
    rows, errs = [], []
    if a.vbo and a.events:
        rows.append(analyze(a.vbo, a.events, Path(a.vbo).stem))
    elif a.root:
        for sess in find_sessions(a.root):
            vbo, evp = find_vbo(sess), find_events(sess)
            if not vbo:
                continue                      # sesión basal o sin VBOX
            if not evp:
                errs.append((sess.name, "sin archivo de eventos")); continue
            try:
                r = analyze(vbo, evp, sess.name)
                rows.append(r)
                print(f"OK  {sess.name}  inicio={r.get('trig_inicio_menos_INICIO_ms', float('nan')):.1f} ms  "
                      f"fin={r.get('trig_fin_menos_FIN_ms', float('nan')):.1f} ms  {r.get('nota', '')}")
            except Exception as e:
                errs.append((sess.name, str(e))); print(f"ERR {sess.name}: {e}")
    else:
        ap.error("indica RAIZ o --vbo y --events")

    if not rows:
        print("Sin resultados"); return
    keys = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    for k in ("trig_polaridad", "nota", "nota_flancos"):   # referencia: al final
        if k in keys:
            keys.remove(k); keys.append(k)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    with open(out / a.csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys); w.writeheader(); w.writerows(rows)

    print("\n" + "=" * 100)
    print(f"RESUMEN ({len(rows)} sesiones)")
    print("=" * 100)
    for k, u in [("trig_inicio_menos_INICIO_ms", "ms"), ("trig_fin_menos_FIN_ms", "ms"),
                 ("dif_dur_trigger_eventos_ms", "ms"), ("deriva_ppm", "ppm"),
                 ("vbo_ini_menos_INICIO_s", "s"), ("vbo_fin_menos_FIN_s", "s"),
                 ("ventana_frec_hz", "Hz"), ("ventana_cobertura_pct", "%"),
                 ("ventana_max_intervalo_ms", "ms")]:
        print(summary(rows, k, u))
    fin = [r["vbo_fin_menos_FIN_s"] for r in rows]
    print(f"\n  |fin|<1 s: {sum(abs(x) < 1 for x in fin)}   |fin|>=1 s: {sum(abs(x) >= 1 for x in fin)}")
    print(f"  sin Trigger completo: {[r['sesion'] for r in rows if 'nota' in r]}")
    for n, e in errs:
        print(f"  ERROR {n}: {e}")
    print(f"\nCSV: {out / a.csv}")


if __name__ == "__main__":
    main()