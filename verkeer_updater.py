# -*- coding: utf-8 -*-
"""
Haalt elke paar minuten de actuele files voor heel Nederland op (NDW / Rijkswaterstaat open data,
gratis, geen aanmelding) en schrijft ze naar verkeer.js en verkeer.json naast dit bestand.
scherm.html leest verkeer.js en laat de files in de bovenste balk voorbijkomen.

Alleen standaard Python nodig (3.7 of nieuwer). Starten: dubbelklik op start_verkeer.bat
Eenmalig testen: python verkeer_updater.py --once
"""
import gzip, json, os, re, sys, time, urllib.request
import xml.etree.ElementTree as ET

URL = "https://opendata.ndw.nu/actueel_beeld.xml.gz"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "verkeer.js")
DIAG = os.path.join(HERE, "ndw_diagnose.txt")
EVERY = 180      # seconden tussen twee updates
MAX_ITEMS = 10   # aantal files in het scherm
JAM_WORDS = ("stationary", "queuing", "queue", "slowtraffic", "heavytraffic", "congestion")

def ln(tag):
    return tag.rsplit("}", 1)[-1]

def rtype(e):
    for k, v in e.attrib.items():
        if k.endswith("}type") or k == "type":
            return v.split(":")[-1]
    return ""

def first_text(rec, names):
    for el in rec.iter():
        if ln(el.tag) in names and el.text and el.text.strip():
            return el.text.strip()
    return ""

def comments(rec):
    out = []
    for el in rec.iter():
        if ln(el.tag) == "generalPublicComment":
            for v in el.iter():
                if ln(v.tag) == "value" and v.text and v.text.strip():
                    out.append(v.text.strip())
    return out

def is_jam(rec):
    t = rtype(rec).lower()
    blob = " ".join([t] + [(el.text or "").lower() for el in rec.iter()
                           if ln(el.tag) in ("abnormalTrafficType", "queueType", "congestionType")])
    return "abnormaltraffic" in t or "congestion" in t or any(w in blob for w in ("stationarytraffic", "queuingtraffic"))

def num(s):
    try:
        return float(str(s).replace(",", "."))
    except Exception:
        return 0.0

def cut(s, n=100):
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "…"

def outline(rec, maxlines=120):
    lines = []
    def walk(e, depth):
        if len(lines) >= maxlines:
            return
        attrs = " ".join("%s=%s" % (ln(k), v) for k, v in e.attrib.items())
        txt = (e.text or "").strip()
        lines.append("  " * depth + ln(e.tag) + (" [" + attrs + "]" if attrs else "") + (": " + txt[:80] if txt else ""))
        for c in e:
            walk(c, depth + 1)
    walk(rec, 0)
    return "\n".join(lines)

def parse(xml_bytes):
    root = ET.fromstring(xml_bytes)
    counts, jams = {}, []
    for rec in root.iter():
        if ln(rec.tag) != "situationRecord":
            continue
        t = rtype(rec) or "?"
        counts[t] = counts.get(t, 0) + 1
        if is_jam(rec):
            jams.append(rec)
    items = []
    for rec in jams:
        road = first_text(rec, ("roadNumber", "roadName"))
        cm = comments(rec)
        base = cut(cm[0]) if cm else ""
        if road and not base.upper().startswith(road.upper()):
            base = (road + " " + base).strip()
        if not base:
            base = rtype(rec)
        meters = num(first_text(rec, ("queueLength", "lengthAffected")))
        secs = num(first_text(rec, ("delayTimeValue", "delay")))
        extra = []
        if meters >= 100:
            extra.append(("%.1f" % (meters / 1000)).replace(".", ",") + " km")
        if secs >= 60:
            extra.append("%d min" % round(secs / 60))
        items.append((secs, meters, base + (" (" + ", ".join(extra) + ")" if extra else "")))
    items.sort(key=lambda x: (x[0], x[1]), reverse=True)
    seen, out = set(), []
    for _, _, s in items:
        if s not in seen:
            seen.add(s)
            out.append(s)
    diag = ["Situatietypes in het bestand:"] + ["  %s: %d" % kv for kv in sorted(counts.items())]
    diag.append("Herkende files: %d" % len(jams))
    for i, rec in enumerate(jams[:3], 1):
        diag.append("\n--- opbouw filebericht %d (elke regel: element [kenmerken]: waarde) ---\n%s" % (i, outline(rec)))
    return out, "\n".join(diag)

def fetch():
    req = urllib.request.Request(URL, headers={"User-Agent": "weekplanning-verkeer/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data

def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)

def run_once(first):
    items, diag = parse(fetch())
    data = json.dumps({"t": int(time.time() * 1000), "n": len(items), "items": items[:MAX_ITEMS]}, ensure_ascii=False)
    write_atomic(OUT, "window.VERKEER = " + data + ";\n")
    write_atomic(os.path.join(HERE, "verkeer.json"), data + "\n")
    if first:
        write_atomic(DIAG, diag)
    print(time.strftime("%H:%M:%S"), "-", len(items), "files geschreven naar verkeer.js")
    if first and not items:
        print("   Geen files herkend. Dat kan kloppen (rustig op de weg), of het bestand is anders opgebouwd.")
        print("   Kijk in ndw_diagnose.txt en stuur die naar Claude als er toch files zouden moeten staan.")

def main():
    once = "--once" in sys.argv
    first = True
    while True:
        try:
            run_once(first)
            first = False
        except Exception as e:
            print(time.strftime("%H:%M:%S"), "- mislukt:", e, "(oude verkeer.js blijft staan, volgende poging volgt)")
        if once:
            break
        time.sleep(EVERY)

if __name__ == "__main__":
    main()
