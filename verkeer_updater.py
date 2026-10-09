# -*- coding: utf-8 -*-
"""
Haalt de actuele files van NDW (Rijkswaterstaat open data, gratis) op en schrijft
ze naar verkeer.js en verkeer.json. scherm.html toont ze in de bovenbalk.

NIEUW: de wegnaam (A2, N65, ...) komt uit de VILD-locatietabel 6.13.A van NDW.
Die tabel wordt automatisch gedownload als vild.json nog niet bestaat.

Alleen standaard Python nodig (3.7 of nieuwer).
  python verkeer_updater.py --once   eenmaal draaien (GitHub Action)
  python verkeer_updater.py          blijft draaien, elke 3 minuten
  python verkeer_updater.py --vild   vild.json opnieuw opbouwen
"""
import gzip, io, json, os, re, struct, sys, tempfile, time, urllib.request, zipfile
import xml.etree.ElementTree as ET

URL = "https://opendata.ndw.nu/actueel_beeld.xml.gz"
VILD_URL = "https://opendata.ndw.nu/VILD6.13.A.zip"
HIER = os.path.dirname(os.path.abspath(__file__))
UIT_JS = os.path.join(HIER, "verkeer.js")
UIT_JSON = os.path.join(HIER, "verkeer.json")
DIAGNOSE = os.path.join(HIER, "ndw_diagnose.txt")
VILD_JSON = os.path.join(HIER, "vild.json")
VILD_DIAG = os.path.join(HIER, "vild_diagnose.txt")
ELK = 180          # seconden tussen twee updates
MAX_ITEMS = 10     # aantal files in het bestand
WEGEN = []         # leeg = alle wegen. Voorbeeld: ["A2", "A12", "A15"]
MIN_METERS = 0     # files korter dan dit overslaan (bijv. 500)


def ln(tag):
    return tag.rsplit("}", 1)[-1]


def rtype(e):
    for k, v in e.attrib.items():
        if k.endswith("}type") or k == "type":
            return v.split(":")[-1]
    return ""


def eerste_tekst(rec, namen):
    for el in rec.iter():
        if ln(el.tag) in namen and el.text and el.text.strip():
            return el.text.strip()
    return ""


def nummer(s):
    try:
        return float(str(s).replace(",", "."))
    except Exception:
        return 0.0


# ---------------------------------------------------------------- VILD ----

def lees_dbf(data):
    n = struct.unpack("<I", data[4:8])[0]
    hlen, rlen = struct.unpack("<HH", data[8:12])
    velden, pos = [], 32
    while pos < len(data) and data[pos] != 0x0D:
        naam = data[pos:pos + 11].split(b"\0")[0].decode("ascii", "ignore")
        velden.append((naam, data[pos + 16]))
        pos += 32

    def dec(b):
        try:
            return b.decode("utf-8").strip()
        except UnicodeDecodeError:
            return b.decode("cp1252", "ignore").strip()

    rijen = []
    for i in range(n):
        rec = data[hlen + i * rlen: hlen + (i + 1) * rlen]
        if not rec or rec[0:1] == b"*":
            continue
        off, rij = 1, {}
        for naam, lengte in velden:
            rij[naam.upper()] = dec(rec[off:off + lengte])
            off += lengte
        rijen.append(rij)
    return [v[0].upper() for v in velden], rijen


def dbf_bestanden(zf, pad=""):
    """Alle .dbf-bestanden in de zip (ook in zips binnen de zip)."""
    for naam in zf.namelist():
        low = naam.lower()
        if low.endswith(".dbf"):
            yield pad + naam, zf.read(naam)
        elif low.endswith(".zip"):
            try:
                yield from dbf_bestanden(zipfile.ZipFile(io.BytesIO(zf.read(naam))), pad + naam + "/")
            except Exception:
                pass


def kies(velden, patronen):
    for p in patronen:
        for v in velden:
            if re.search(p, v):
                return v
    return None


def weg_schoon(w):
    w = (w or "").strip().upper().replace(" ", "")
    m = re.match(r"^([AN])0*(\d+)", w)
    return (m.group(1) + m.group(2)) if m else w


def bouw_vild():
    print("VILD downloaden (eenmalig, ca. 40 MB)...")
    req = urllib.request.Request(VILD_URL, headers={"User-Agent": "weekplanning-verkeer/1.0"})
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            while True:
                blok = r.read(1 << 20)
                if not blok:
                    break
                tmp.write(blok)
        tmp.close()
        zf = zipfile.ZipFile(tmp.name)
        diag = ["Bestanden in zip: %d" % len(zf.namelist())] + ["  " + n for n in zf.namelist()[:60]]
        tabel = {}
        for naam, data in dbf_bestanden(zf):
            try:
                velden, rijen = lees_dbf(data)
            except Exception as e:
                diag.append("%s: kon niet lezen (%s)" % (naam, e))
                continue
            diag.append("")
            diag.append("%s: %d rijen" % (naam, len(rijen)))
            diag.append("  kolommen: " + ", ".join(velden))
            for rij in rijen[:2]:
                diag.append("  voorbeeld: " + json.dumps(rij, ensure_ascii=True)[:400])
            kc = kies(velden, [r"^LOC_?NR$", r"^LOC_?CODE$", r"^LCD$", r"^LOC_?LCD$", r"^LOC_?ID$", r"^TMC", r"^LOCATIE"])
            kw = kies(velden, [r"^ROAD_?NUMBER$", r"^ROAD_?NUM", r"^ROAD_?NR", r"^WEGNUMMER", r"^ROADNAME$"])
            kn = kies(velden, [r"^FIRST_?NAME$", r"^FIRSTNAME$", r"^FIRST_?NAM", r"^LOC_?DES", r"^NAME1$", r"NAAM"])
            diag.append("  gekozen -> code=%s weg=%s naam=%s" % (kc, kw, kn))
            if not kc or not kw:
                continue
            for rij in rijen:
                code = rij.get(kc, "").strip()
                try:
                    code = str(int(float(code)))
                except Exception:
                    continue
                weg = weg_schoon(rij.get(kw, ""))
                naam_ = rij.get(kn, "") if kn else ""
                if weg and code not in tabel:
                    tabel[code] = [weg, naam_]
        diag.append("")
        diag.append("Locaties met wegnummer: %d" % len(tabel))
        schrijf(VILD_DIAG, "\n".join(diag))
        if tabel:
            schrijf(VILD_JSON, json.dumps(tabel, ensure_ascii=True, separators=(",", ":")))
        return tabel
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


def laad_vild():
    if os.path.exists(VILD_JSON):
        try:
            with open(VILD_JSON, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    try:
        return bouw_vild()
    except Exception as e:
        print("VILD niet beschikbaar (%s); files komen zonder wegnaam" % e)
        return {}


# --------------------------------------------------------------- Files ----

def is_jam(rec):
    return rtype(rec).lower() in ("abnormaltraffic",)


def punt_code(rec, eind):
    for el in rec.iter():
        if ln(el.tag).startswith("alertCMethod") and ln(el.tag).endswith(eind):
            for s in el.iter():
                if ln(s.tag) == "specificLocation" and s.text and s.text.strip():
                    try:
                        return str(int(float(s.text.strip())))
                    except Exception:
                        return None
    return None


def label(rec, vild):
    p = vild.get(punt_code(rec, "PrimaryPointLocation") or "")
    s = vild.get(punt_code(rec, "SecondaryPointLocation") or "")
    weg = (p or s or ["", ""])[0]
    namen = []
    for x in (p, s):
        if x and x[1] and x[1] not in namen:
            namen.append(x[1])
    tekst = " ".join(x for x in [weg, "\u2013".join(namen[:2])] if x).strip()
    return tekst or "File", weg


def ontleden(xml_bytes, vild):
    wortel = ET.fromstring(xml_bytes)
    tellingen, jam = {}, []
    for rec in wortel.iter():
        if ln(rec.tag) != "situationRecord":
            continue
        t = rtype(rec) or "?"
        tellingen[t] = tellingen.get(t, 0) + 1
        if is_jam(rec):
            jam.append(rec)
    items = []
    for rec in jam:
        txt, weg = label(rec, vild)
        if WEGEN and weg not in WEGEN:
            continue
        meters = nummer(eerste_tekst(rec, ("queueLength", "lengthAffected")))
        seconden = nummer(eerste_tekst(rec, ("delayTimeValue",)))
        if meters < MIN_METERS:
            continue
        extra = []
        if meters >= 100:
            extra.append(("%.1f" % (meters / 1000)).replace(".", ",") + " km")
        if seconden >= 60:
            extra.append("%d min" % round(seconden / 60))
        items.append((seconden, meters, txt + (" (" + ", ".join(extra) + ")" if extra else "")))
    items.sort(key=lambda x: (x[0], x[1]), reverse=True)
    gezien, uit = set(), []
    for _, _, s in items:
        if s not in gezien:
            gezien.add(s)
            uit.append(s)
    diag = ["Situatietypen in het bestand:"] + ["  %s: %d" % kv for kv in sorted(tellingen.items())]
    diag.append("Herkende files: %d" % len(jam))
    diag.append("Locaties in VILD: %d" % len(vild))
    return uit, "\n".join(diag)


def ophalen():
    req = urllib.request.Request(URL, headers={"User-Agent": "weekplanning-verkeer/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data


def schrijf(pad, tekst):
    tmp = pad + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(tekst)
    os.replace(tmp, pad)


def run_once(eerst, vild):
    items, diag = ontleden(ophalen(), vild)
    gegevens = json.dumps({"t": int(time.time() * 1000), "n": len(items), "items": items[:MAX_ITEMS]},
                          ensure_ascii=True)
    schrijf(UIT_JS, "window.VERKEER = " + gegevens + ";\n")
    schrijf(UIT_JSON, gegevens + "\n")
    if eerst:
        schrijf(DIAGNOSE, diag)
    print(time.strftime("%H:%M:%S"), "-", len(items), "files geschreven")
    if eerst and not items:
        print("  Geen files herkend. Dat kan kloppen (rustig op de weg).")


def main():
    eenmaal = "--once" in sys.argv
    if "--vild" in sys.argv:
        bouw_vild()
        return
    vild = laad_vild()
    eerst = True
    while True:
        try:
            run_once(eerst, vild)
            eerst = False
        except Exception as e:
            print(time.strftime("%H:%M:%S"), "- mislukt:", e, "(oude verkeer.js blijft staan)")
            if eenmaal:
                raise
        if eenmaal:
            break
        time.sleep(ELK)


if __name__ == "__main__":
    main()
