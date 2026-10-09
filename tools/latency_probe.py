#!/usr/bin/env python3
"""Misst die Latenz der laufenden Kette gegen das Roh-Array, gesamt und je Stufe, damit ein Rückschritt sichtbar wird.

    python3 tools/latency_probe.py                        # 60 s, alle 10 s ein Wert je Stufe
    python3 tools/latency_probe.py --seconds 600 --window 20
    python3 tools/latency_probe.py --max-excess-ms 10     # Rückschrittprüfung: Exit 1, wenn der Ausgang >10 ms über dem Soll liegt
python3 tools/latency_probe.py --expect-max-ms 90     # absolut: Exit 1, wenn der Median darüber liegt

Ein einziger pw-record-Stream (node.autoconnect = false) nimmt Mikrofon 0 der Raw-Quelle, AUX0 von
Vorverstärkung und Echounterdrückung (nur wenn eingeschaltet) und den Ausgang „UMA-8 Call Mic“ auf, verbunden
per pw-link. Alle Spalten kommen im selben Graphzyklus an: Ihr Versatz ist die echte Latenz der Kette.
Vorverstärkung und Echounterdrückung sind nahezu linear, dort zählt die Kreuzkorrelation der Samples
(sampelgenau). Der Ausgang (Beamforming, DeepFilterNet, Begrenzer) ist es nicht, dort zählt die
Kreuzkorrelation der log-RMS-Hüllkurven (10-ms-Fenster im 1-ms-Raster). Die Hüllkurve braucht Pegelwechsel wie
Sprache; Fenster mit Stille oder gleichmäßigem Rauschen werden markiert statt gemessen.

Der Ton bleibt im Speicher; ausgegeben werden nur Zahlen, auf die Platte kommt nichts. Während der Messung läuft
die Kette wie bei einem Anruf (auch die Echo-Referenz wird verbunden). Der Stream fordert das Quantum an, mit dem
die Kette dabei läuft: mit Echounterdrückung deren node.latency (480 Samples), sonst clock.quantum (Standard 1024).
pw-records Vorgabe von 100 ms höbe es sonst, wenn niemand anderes aufnimmt, auf bis zu 2048 oder 4096.
Das ist nur die Anforderung: PipeWire nimmt das kleinste Quantum aller aktiven Knoten. Das tatsächliche Quantum
des Treibers der Raw-Quelle liest das Werkzeug nach dem Verbinden und am Ende aus pw-top und gibt es aus.

Die absolute Gesamtlatenz hängt vom Aufbau ab. Die Echounterdrückung misst hier etwa 30 ms (Quantum 256, kabelgebundener
Ausgang, Fedora mit webrtc-audio-processing 2.1; insgesamt etwa 86 ms), auf einem zweiten Aufbau (Arch mit
webrtc-audio-processing 1.3) etwa 41 ms, bei gleichem Quantum und auch mit kabelgebundenem Ausgang; vermutlich liegt es
an der WebRTC-Version (nicht gegengeprüft). PipeWire
rundet das angeforderte 480 standardmäßig auf eine Zweierpotenz ab (256, clock.power-of-two-quantum).
Als Rückschrittprüfung taugt daher --max-excess-ms (Ausgang minus Soll mit gemessener Echounterdrückung), nicht
der absolute --expect-max-ms.
Exit: 0 in Ordnung, 1 Schwelle überschritten oder nicht prüfbar, 2 Knoten fehlt oder Aufnahme scheitert.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from uma8_callmic import constants as K  # noqa: E402
from uma8_callmic import pwctl  # noqa: E402
from uma8_callmic.capture import Capture  # noqa: E402

SR = K.SAMPLE_RATE
#: Hüllkurve: RMS über 10 ms im 1-ms-Raster
ENV_FRAME, ENV_HOP = SR // 100, SR // 1000
#: Sprachband für die Hüllkurven (ohne Brummen und Rauschen ganz oben)
BAND_HZ = (150.0, 6000.0)
#: Hüllkurven-Täler zählen höchstens so weit unter dem Maximum (DeepFilterNet schließt in Pausen fast ganz)
ENV_RANGE_DB = 50.0
#: Mindestdynamik (p95 − p10) der Roh-Hüllkurve im Fenster; darunter gilt es als Stille
MIN_DYNAMICS_DB = 10.0
#: Mindestkorrelation eines gültigen Werts
MIN_STRENGTH = 0.3
#: Nach dem Verbinden verworfen: Kette wacht auf, die Spalten sind nicht im selben Zyklus verbunden worden
SETTLE_S = 1.0


class ProbeError(Exception):
    """Messung nicht möglich (Knoten fehlt, pw-record oder pw-link scheitern)."""


@dataclass(frozen=True)
class Stage:
    label: str
    port: str       # Ausgangsport, z. B. „uma8_callmic_aec:capture_AUX0“
    envelope: bool  # nichtlinear: Hüllkurve statt Samples


@dataclass(frozen=True)
class Lag:
    samples: float | None  # None: kein gültiger Wert
    strength: float
    note: str = ""          # „Stille“ oder „unsicher“

    @property
    def ms(self) -> float | None:
        return None if self.samples is None else self.samples * 1000.0 / SR


# --- Schätzung (rein, getestet in tests/test_latency_probe.py) ---------------------------------------------------

def _norm_xcorr(ref: np.ndarray, out: np.ndarray, max_lag: int) -> np.ndarray:
    """r[d] für d = 0 … max_lag: normierte Korrelation von out[n] mit ref[max_lag − d + n].
    `ref` reicht max_lag Werte weiter zurück als `out`: ref[max_lag + n] ist gleichzeitig mit out[n]."""
    ref = np.asarray(ref, np.float64)
    out = np.asarray(out, np.float64)
    n = len(out)
    if len(ref) != n + max_lag:
        raise ValueError(f"ref braucht {n + max_lag} Werte, hat {len(ref)}")
    ref = ref - ref.mean()
    out = out - out.mean()
    nfft = 1 << (len(ref) + n - 1).bit_length()
    full = np.fft.irfft(np.fft.rfft(ref, nfft) * np.conj(np.fft.rfft(out, nfft)), nfft)  # Σ ref[n + k]·out[n]
    corr = full[max_lag::-1]
    c = np.concatenate([[0.0], np.cumsum(ref * ref)])
    starts = max_lag - np.arange(max_lag + 1)
    norm = np.sqrt(np.maximum(c[starts + n] - c[starts], 0.0) * np.sum(out * out))
    return np.divide(corr, norm, out=np.zeros_like(corr), where=norm > 0)


def _peak(r: np.ndarray) -> tuple[float, float]:
    """Lage des Maximums, parabolisch zwischen den Rasterpunkten verfeinert, und sein Wert."""
    d = int(np.argmax(r))
    if 0 < d < len(r) - 1:
        a, b, c = r[d - 1], r[d], r[d + 1]
        den = a - 2 * b + c
        if den < 0:
            return d + 0.5 * (a - c) / den, float(b)
    return float(d), float(r[d])


def sample_lag(ref: np.ndarray, out: np.ndarray, max_lag: int) -> Lag:
    """Versatz in Samples, um den `out` hinter `ref` liegt (0 … max_lag), für (nahezu) lineare Stufen.
    Aufteilung von `ref` wie bei _norm_xcorr."""
    if not np.any(out) or not np.any(ref):
        return Lag(None, 0.0, "Stille")
    r = _norm_xcorr(ref, out, max_lag)
    d = int(np.argmax(r))
    if r[d] < MIN_STRENGTH:
        return Lag(None, float(r[d]), "unsicher")
    return Lag(float(d), float(r[d]))


def speech_band(x: np.ndarray) -> np.ndarray:
    spec = np.fft.rfft(np.asarray(x, np.float64))
    f = np.fft.rfftfreq(len(x), 1.0 / SR)
    spec[(f < BAND_HZ[0]) | (f > BAND_HZ[1])] = 0.0
    return np.fft.irfft(spec, len(x))


def log_envelope(x: np.ndarray, frame: int = ENV_FRAME, hop: int = ENV_HOP) -> np.ndarray:
    """RMS in dB über je `frame` Samples im Raster `hop`; Täler mehr als ENV_RANGE_DB unter dem Maximum gekappt."""
    x = np.asarray(x, np.float64)
    c = np.concatenate([[0.0], np.cumsum(x * x)])
    starts = np.arange(0, len(x) - frame + 1, hop)
    db = 10.0 * np.log10(np.maximum((c[starts + frame] - c[starts]) / frame, 1e-20))
    return np.maximum(db, db.max() - ENV_RANGE_DB)


def envelope_lag(ref: np.ndarray, out: np.ndarray, max_lag: int) -> Lag:
    """Versatz in Samples (auf 1 ms gerastert, parabolisch verfeinert) über die log-RMS-Hüllkurven, für
    nichtlineare Stufen. Aufteilung von `ref` wie bei _norm_xcorr. Zu wenig Pegelwechsel in `ref` gleichzeitig
    mit `out`: Stille."""
    lag_frames = max_lag // ENV_HOP
    e_ref = log_envelope(speech_band(np.asarray(ref)[max_lag - lag_frames * ENV_HOP:]))
    e_out = log_envelope(speech_band(out))
    now = e_ref[lag_frames:]
    if np.percentile(now, 95) - np.percentile(now, 10) < MIN_DYNAMICS_DB:
        return Lag(None, 0.0, "Stille")
    d, strength = _peak(_norm_xcorr(e_ref, e_out, lag_frames))
    if strength < MIN_STRENGTH:
        return Lag(None, strength, "unsicher")
    return Lag(d * ENV_HOP, strength)


def analyze(seg: np.ndarray, stages: list[Stage], max_lag: int) -> list[Lag]:
    """Ein Messfenster: seg = (max_lag + Fensterlänge, 1 + Stufen), Spalte 0 = Roh-Mikrofon."""
    ref = seg[:, 0]
    return [(envelope_lag if st.envelope else sample_lag)(ref, seg[max_lag:, i], max_lag)
            for i, st in enumerate(stages, 1)]


def median_ms(lags: list[Lag]) -> float | None:
    vals = [lag.ms for lag in lags if lag.ms is not None]
    return float(np.median(vals)) if vals else None


def expected_chain() -> list[tuple[str, int]]:
    """Feste Latenzen der Hauptkette (hinter der Echounterdrückung) in Samples, aus constants.py."""
    return [("Beamforming", K.BEAM_LATENCY), ("DeepFilterNet", K.DFN_LATENCY), ("Begrenzer", K.LIMIT_LATENCY)]


# --- PipeWire ----------------------------------------------------------------------------------------------------

def plan_stages(out_ports: set[str], raw_node: str | None) -> tuple[str, list[Stage]]:
    """Roh-Port und Messstufen aus den Ausgangsports (pw-link -o). Vorverstärkung und Echounterdrückung gibt es
    nur bei eingeschalteter Echounterdrückung; fehlen sie, entfallen sie."""
    raw_port = f"{raw_node}:capture_{K.RAW_POSITIONS[0]}"
    if raw_node is None or raw_port not in out_ports:
        raise ProbeError(f"Raw-Quelle des UMA-8 fehlt ({K.RAW_DEVICE}): angeschlossen, Raw-Firmware, "
                         "Profil analog-surround-71?")
    final = f"{K.SOURCE_NODE}:capture_MONO"
    if final not in out_ports:
        raise ProbeError(f"„UMA-8 Call Mic“ ({K.SOURCE_NODE}) fehlt: läuft der Dienst? "
                         f"systemctl --user status {K.SERVICE}")
    stages = [Stage(label, f"{node}:capture_{K.MIC_POSITIONS[0]}", False)
              for label, node in (("Vorverstärkung", K.PRE_NODE), ("Echounterdrückung", K.AEC_NODE))
              if f"{node}:capture_{K.MIC_POSITIONS[0]}" in out_ports]
    return raw_port, [*stages, Stage("Ausgang", final, True)]


def _ports(flag: str) -> set[str]:
    try:
        r = subprocess.run(["pw-link", flag], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ProbeError(f"pw-link {flag}: {e}") from e
    if r.returncode != 0:
        raise ProbeError(f"pw-link {flag}: {r.stderr.strip() or 'fehlgeschlagen'}")
    return {line.strip() for line in r.stdout.splitlines() if line.strip()}


#: Quantum, wenn PipeWire nichts anderes meldet (clock.quantum ab Werk)
DEFAULT_QUANTUM = 1024


def call_quantum(objs: list[dict]) -> int:
    """Quantum der Kette während eines Anrufs (pw-dump): node.latency der Echounterdrückung („480/48000“), die es
    erzwingt, solange niemand weniger verlangt; ohne sie clock.quantum aus den Metadaten „settings“."""
    for o in objs:
        props = (o.get("info") or {}).get("props") or {}
        if o.get("type") == "PipeWire:Interface:Node" and props.get("node.name") == K.AEC_NODE:
            try:
                num, den = (int(v) for v in str(props["node.latency"]).split("/"))
                return round(num * SR / den)
            except (KeyError, ValueError, ZeroDivisionError):
                break
    for o in objs:
        if o.get("type") == "PipeWire:Interface:Metadata" and o.get("props", {}).get("metadata.name") == "settings":
            for m in o.get("metadata") or []:
                if m.get("subject") == 0 and m.get("key") == "clock.quantum":
                    try:
                        return int(m["value"])
                    except (TypeError, ValueError):
                        pass
    return DEFAULT_QUANTUM


def parse_pw_top(text: str, node: str) -> int | None:
    """Quantum des Treibers von `node` aus der letzten Ausgabe von `pw-top -b` (LC_ALL=C). Treiber zeigen ihr
    Quantum in der Spalte QUANT; Folgeknoten haben dort 0 und den Namen „+ name“ hinter ihrem Treiber, dann zählt
    die nächste Zeile darüber ohne „+“. Unbekannt (None): Knoten fehlt, Quantum 0 (läuft nicht)."""
    rows: list[tuple[bool, int, str]] = []  # (Folgeknoten, QUANT, Name) der letzten Iteration
    for line in text.splitlines():
        t = line.split()
        if len(t) >= 2 and t[1] == "ID":
            rows = []  # neue Iteration: nur die letzte zählt
        elif len(t) >= 4 and t[0] in "SIRCE!" and len(t[0]) == 1 and t[1].isdigit() and t[2].isdigit():
            rows.append((t[-2] == "+", int(t[2]), t[-1]))
    for i, (follower, quant, name) in enumerate(rows):
        if name != node:
            continue
        if follower:
            quant = next((q for f, q, _ in reversed(rows[:i]) if not f), 0)
        return quant or None
    return None


def graph_quantum(node: str) -> int | None:
    """Tatsächliches Quantum der Kette, in der `node` läuft (nur lesend: pw-top, ca. 1 s); None: unbekannt.
    pw-dump nennt nur die angeforderte node.latency, nicht das aktuelle Quantum."""
    try:
        r = subprocess.run(["pw-top", "-b", "-n", "2"], capture_output=True, text=True, timeout=10,
                           env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_pw_top(r.stdout, node) if r.returncode == 0 else None


def quantum_text(actual: int | None, requested: int) -> str:
    if actual is None:
        return f"Quantum unbekannt (angefordert {requested} Samples)"
    return f"Quantum {actual} Samples ({actual * 1000 / SR:g} ms), angefordert {requested}"


def discover() -> tuple[str, list[Stage], int]:
    """Nur lesend: pw-dump (Name der Raw-Quelle, ggf. mit Suffix; Quantum) und pw-link -o."""
    try:
        objs = pwctl.dump()
    except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError) as e:
        raise ProbeError(f"pw-dump: {e}") from e
    return *plan_stages(_ports("-o"), pwctl.find_raw_source(objs)), call_quantum(objs)


def record_command(node: str, channels: int, quantum: int) -> list[str]:
    """Unverbundener Stream mit Eingängen input_AUX0 … Fordert `quantum` Samples an, damit die Kette mit dem
    Quantum eines Anrufs läuft (pw-records Vorgabe von 100 ms hebt es sonst an)."""
    props = (f'{{ node.name = {node} node.description = "UMA-8 Latenzmessung" '
             "node.autoconnect = false stream.dont-remix = true }")
    return ["pw-record", "--target", "0", "--rate", str(SR), "--latency", str(quantum), "--channels", str(channels),
            "--channel-map", ",".join(f"AUX{i}" for i in range(channels)), "-P", props,
            "--format", "f32", "--raw", "-"]


def link(src: str, dst: str) -> None:
    """pw-link; jedes Scheitern als ProbeError (Exit 2, nicht 1 wie eine überschrittene Latenz)."""
    try:
        r = subprocess.run(["pw-link", src, dst], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ProbeError(f"pw-link {src} {dst}: {e}") from e
    if r.returncode != 0:
        raise ProbeError(f"pw-link {src} {dst}: {r.stderr.strip() or 'fehlgeschlagen'}")


def _wait(cap: Capture, frames: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while cap.total() < frames:
        if not cap.alive:
            raise ProbeError("pw-record ist beendet")
        if time.monotonic() > deadline:
            raise ProbeError("keine Daten von pw-record (Kette hängt?)")
        time.sleep(0.05)


def measure(raw_port: str, stages: list[Stage], quantum: int, seconds: float, window: float,
            max_lag: float, quanta: list[int | None] | None = None) -> Iterator[tuple[float, list[Lag]]]:
    """Nimmt auf und liefert je Fenster (Zeit in s, Versätze je Stufe). Der Ringpuffer hält nur max_lag + 2 Fenster.
    Hängt das tatsächliche Quantum (graph_quantum) nach dem Verbinden und am Ende an `quanta` an."""
    node = f"uma8_latency_probe_{os.getpid()}"
    sources = [raw_port, *(st.port for st in stages)]
    inputs = [f"{node}:input_AUX{i}" for i in range(len(sources))]
    lag_n, win_n = int(max_lag * SR), int(window * SR)
    cap = Capture(node, len(sources), seconds=max_lag + 2 * window + 5, sample_rate=SR,
                  command=record_command(node, len(sources), quantum))
    try:
        deadline = time.monotonic() + 5
        while not set(inputs) <= _ports("-i"):
            if not cap.alive or time.monotonic() > deadline:
                raise ProbeError("pw-record startet nicht (keine Eingangsports)")
            time.sleep(0.1)
        for src, dst in zip(sources, inputs):
            link(src, dst)
        start = cap.total() + int(SETTLE_S * SR)
        _wait(cap, start, SETTLE_S + 10)
        node_raw = raw_port.rpartition(":")[0]
        if quanta is not None:
            quanta.append(graph_quantum(node_raw))
            print(quantum_text(quanta[-1], quantum), flush=True)
        for k in range(int((seconds * SR - lag_n) // win_n)):
            end = start + lag_n + (k + 1) * win_n
            _wait(cap, end, window + 10)
            seg = cap.span(end - lag_n - win_n, end)
            if seg is None:
                raise ProbeError("Auswertung zu langsam, Ringpuffer überholt")
            yield (end - start) / SR, analyze(seg, stages, lag_n)
        if quanta is not None:
            quanta.append(graph_quantum(node_raw))
    finally:
        cap.close()


# --- Ausgabe -----------------------------------------------------------------------------------------------------

def cell(lag: Lag) -> str:
    if lag.ms is None:
        return f"{lag.note} ({lag.strength:.2f})" if lag.note == "unsicher" else lag.note
    return f"{lag.ms:7.1f} ms ({lag.strength:.2f})"


def report(stages: list[Stage], rows: list[list[Lag]], expect_max_ms: float | None,
           max_excess_ms: float | None = None) -> int:
    print("\nZusammenfassung (Median der gültigen Fenster, Spanne, Anzahl):")
    medians = {}
    for i, st in enumerate(stages):
        lags = [row[i] for row in rows]
        vals = [lag.ms for lag in lags if lag.ms is not None]
        medians[st.label] = median_ms(lags)
        span = f"{min(vals):.1f} … {max(vals):.1f} ms, " if vals else ""
        med = f"{medians[st.label]:7.1f} ms" if vals else "       –  "
        print(f"  {st.label:<18} {med}   ({span}{len(vals)}/{len(lags)} Fenster)")

    total = medians["Ausgang"]
    before = [st.label for st in stages[:-1] if medians[st.label] is not None]
    base = medians[before[-1]] if before else 0.0
    parts = [(f"{before[-1]} (gemessen)", base)] if before else []
    parts += [(name, n * 1000.0 / SR) for name, n in expected_chain()]
    expected = sum(ms for _, ms in parts)
    print("Erwartet: " + " + ".join(f"{name} {ms:.1f}" for name, ms in parts) + f" = {expected:.1f} ms")
    if total is not None:
        unit = K.SERVICE.removesuffix(".service")
        print(f"Abweichung des Ausgangs: {total - expected:+.1f} ms (positiv: zusätzlich gepuffert, z. B. "
              f"DeepFilterNet nach Underruns: journalctl --user -u {unit} | grep -i latency)")
    if expect_max_ms is None and max_excess_ms is None:
        return 0
    if total is None:
        print("FEHLER: Gesamtlatenz nicht prüfbar, kein gültiges Fenster (Stille? Während der Messung sprechen)")
        return 1
    rc = 0
    if expect_max_ms is not None:
        if total > expect_max_ms:
            print(f"FEHLER: Gesamtlatenz {total:.1f} ms über {expect_max_ms:g} ms")
            rc = 1
        else:
            print(f"OK: Gesamtlatenz {total:.1f} ms ≤ {expect_max_ms:g} ms")
    if max_excess_ms is not None:
        excess = total - expected
        if excess > max_excess_ms:
            print(f"FEHLER: Ausgang {excess:+.1f} ms über dem Soll, erlaubt {max_excess_ms:g} ms")
            rc = 1
        else:
            print(f"OK: Ausgang {excess:+.1f} ms zum Soll ≤ {max_excess_ms:g} ms")
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=60.0, help="Messdauer in s (Standard 60)")
    ap.add_argument("--window", type=float, default=10.0, help="ein Wert je Stufe alle S Sekunden (Standard 10)")
    ap.add_argument("--max-lag", type=float, default=5.0, help="größte gesuchte Latenz in s (Standard 5)")
    ap.add_argument("--expect-max-ms", type=float, metavar="MS",
                    help="Exit 1, wenn der Median der Gesamtlatenz darüber liegt (oder nicht messbar ist); "
                         "absolut, hängt vom Aufbau ab (Quantum)")
    ap.add_argument("--max-excess-ms", type=float, metavar="MS",
                    help="Exit 1, wenn der Ausgang mehr als MS über dem Soll liegt (gemessene Echounterdrückung + "
                         "Beamforming + DeepFilterNet + Begrenzer); Rückschrittprüfung, empfohlen: 10")
    args = ap.parse_args(argv)
    if args.window < 1 or args.max_lag <= 0:
        ap.error("--window mindestens 1 s, --max-lag größer 0")
    if args.seconds < args.window + args.max_lag:
        ap.error(f"--seconds muss mindestens --window + --max-lag sein ({args.window + args.max_lag:g} s): "
                 "jedes Fenster braucht max-lag Sekunden Vorlauf")

    try:
        raw_port, stages, quantum = discover()
    except ProbeError as e:
        print(f"Fehler: {e}", file=sys.stderr)
        return 2
    print(f"Referenz: {raw_port}; angefordertes Quantum {quantum} Samples ({quantum * 1000 / SR:g} ms) wie bei einem "
          "Anruf; das tatsächliche wird nach dem Verbinden aus pw-top gelesen")
    for st in stages:
        print(f"  {st.label:<18} {st.port} ({'Hüllkurve' if st.envelope else 'Samples'})")
    if len(stages) == 1:
        print("  (Echounterdrückung aus: keine Zwischenstufen)")
    print(f"Messe {args.seconds:g} s; erster Wert nach {SETTLE_S + args.max_lag + args.window:g} s. Ausgang braucht "
          "Pegelwechsel (Sprechen); Ton bleibt im Speicher.\n")
    print(f"{'Zeit':>7}  " + "".join(f"{st.label:>22}" for st in stages))

    rows: list[list[Lag]] = []
    quanta: list[int | None] = []
    gen = measure(raw_port, stages, quantum, args.seconds, args.window, args.max_lag, quanta)
    try:
        for t, lags in gen:
            rows.append(lags)
            print(f"{t:6.0f}s  " + "".join(f"{cell(lag):>22}" for lag in lags), flush=True)
    except ProbeError as e:
        print(f"Fehler: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("abgebrochen")
    finally:
        gen.close()
    if len(quanta) > 1 and quanta[-1] != quanta[0]:
        print(f"Hinweis: Das Quantum hat sich während der Messung geändert: {quanta[0] or 'unbekannt'} → "
              f"{quanta[-1] or 'unbekannt'}")
    elif len(quanta) > 1:
        print(f"Quantum am Ende unverändert: {quanta[-1] or 'unbekannt'}")
    return report(stages, rows, args.expect_max_ms, args.max_excess_ms)


if __name__ == "__main__":
    sys.exit(main())
