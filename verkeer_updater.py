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
import csv, datetime, gzip, io, json, os, re, sqlite3, struct, sys, tempfile, time, urllib.parse, urllib.request, zipfile
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

# --- Nieuws en weer in de balk (volgorde in het scherm: verkeer, NOS, weer, Omroep Brabant)
PLAATS = "Eindhoven"      # plaats voor de weersvoorspelling
NOS_URL = "https://feeds.nos.nl/nosnieuwsalgemeen"
BRABANT_URLS = ["https://rss.omroepbrabant.nl/", "http://rss.omroepbrabant.nl/"]
BRABANT_SITE = "https://www.omroepbrabant.nl/"   # voor automatisch zoeken naar de RSS-link
MAX_NIEUWS = 6            # aantal koppen per bron
EXTRA_DIAG = os.path.join(HIER, "extra_diagnose.txt")


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


def lees_csv(data):
    try:
        tekst = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        tekst = data.decode("cp1252", "ignore")
    eerste = tekst.split("\n", 1)[0]
    sep = max(";,\t|", key=lambda c: eerste.count(c))
    lees = csv.DictReader(io.StringIO(tekst), delimiter=sep)
    rijen = [{(k or "").upper().strip(): (v or "").strip() for k, v in r.items()} for r in lees]
    return [(k or "").upper().strip() for k in (lees.fieldnames or [])], rijen


def lees_sqlite(data, naam):
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    try:
        tmp.write(data)
        tmp.close()
        con = sqlite3.connect(tmp.name)
        tabs = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
        for t in tabs:
            if t.lower().startswith(("gpkg_", "rtree_", "sqlite_")):
                continue
            kol = [(r[1], (r[2] or "").upper()) for r in con.execute('pragma table_info("%s")' % t)]
            kol = [k for k in kol if "BLOB" not in k[1] and "GEOM" not in k[1] and "POINT" not in k[1]
                   and "LINE" not in k[1] and "POLYGON" not in k[1]]
            if not kol:
                continue
            sel = ",".join('"%s"' % k[0] for k in kol)
            rijen = [{kol[i][0].upper(): ("" if v is None else str(v)) for i, v in enumerate(r)}
                     for r in con.execute('select %s from "%s"' % (sel, t))]
            yield naam + "::" + t, [k[0].upper() for k in kol], rijen
        con.close()
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


def tabellen(zf, pad=""):
    """Alle tabellen in de zip: dbf, csv, gpkg/sqlite (ook zips binnen de zip)."""
    for naam in zf.namelist():
        low = naam.lower()
        try:
            if low.endswith(".dbf"):
                v, r = lees_dbf(zf.read(naam))
                yield pad + naam, v, r
            elif low.endswith((".csv", ".txt")) and "readme" not in low:
                v, r = lees_csv(zf.read(naam))
                if v and r:
                    yield pad + naam, v, r
            elif low.endswith((".gpkg", ".sqlite", ".db")):
                yield from lees_sqlite(zf.read(naam), pad + naam)
            elif low.endswith(".zip"):
                yield from tabellen(zipfile.ZipFile(io.BytesIO(zf.read(naam))), pad + naam + "/")
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
        for naam, velden, rijen in tabellen(zf):
            diag.append("")
            diag.append("%s: %d rijen" % (naam, len(rijen)))
            diag.append("  kolommen: " + ", ".join(velden))
            for rij in rijen[:2]:
                diag.append("  voorbeeld: " + json.dumps(rij, ensure_ascii=True)[:400])
            kc = kies(velden, [r"^LOC_?NR$", r"^LOC_?CODE$", r"^LOCATIE_?CODE$", r"^LCD$", r"^LOC_?LCD$", r"^LOC_?ID$",
                               r"^TMC", r"^LOCATIE", r"^CODE$"])
            kw = kies(velden, [r"^ROAD_?NUMBER$", r"^ROAD_?NUM", r"^ROAD_?NR", r"^WEGNUMMER", r"^WEG_?NR", r"^ROADNAME$",
                               r"^WEG", r"ROAD"])
            kn = kies(velden, [r"^FIRST_?NAME$", r"^FIRSTNAME$", r"^FIRST_?NAM", r"^LOC_?DES", r"^NAME1$", r"NAAM", r"NAME"])
            if not kc:
                for v in velden:
                    waarden = [r.get(v, "") for r in rijen[:2000]]
                    if waarden and all(w.isdigit() and len(w) <= 5 for w in waarden) and len(set(waarden)) > 0.9 * len(waarden):
                        kc = v
                        break
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
        try:
            schrijf(VILD_DIAG, "VILD opbouwen mislukt: %r" % (e,))
        except Exception:
            pass
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


def haal(url, tijd=25):
    req = urllib.request.Request(url, headers={"User-Agent": "weekplanning-verkeer/1.0",
                                               "Accept": "application/xml, application/rss+xml, application/json, */*"})
    with urllib.request.urlopen(req, timeout=tijd) as r:
        data = r.read()
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data


def rss_koppen(data, maxn):
    wortel = ET.fromstring(data)
    uit = []
    for it in wortel.iter():
        if ln(it.tag) in ("item", "entry"):
            for ch in it:
                if ln(ch.tag) == "title" and ch.text and ch.text.strip():
                    t = re.sub(r"\s+", " ", ch.text).strip()
                    if t not in uit:
                        uit.append(t)
                    break
        if len(uit) >= maxn:
            break
    return uit


def haal_nos(diag):
    k = rss_koppen(haal(NOS_URL), MAX_NIEUWS)
    diag.append("NOS: %d koppen" % len(k))
    return k


def haal_brabant(diag):
    kandidaten = list(BRABANT_URLS)
    try:  # ook automatisch zoeken naar de RSS-link op de website
        html = haal(BRABANT_SITE).decode("utf-8", "ignore")
        for m in re.finditer(r"<link[^>]+(?:rss|atom)\+xml[^>]*>", html, re.I):
            h = re.search(r'href=["\']([^"\']+)', m.group(0))
            if h:
                kandidaten.append(urllib.parse.urljoin(BRABANT_SITE, h.group(1)))
    except Exception as e:
        diag.append("Brabant: website niet te lezen (%s)" % e)
    for u in kandidaten:
        try:
            k = rss_koppen(haal(u), MAX_NIEUWS)
            if k:
                diag.append("Brabant: %d koppen via %s" % (len(k), u))
                return k
            diag.append("Brabant: geen koppen via %s" % u)
        except Exception as e:
            diag.append("Brabant: %s mislukt (%s)" % (u, e))
    return []


WMO = {0: "zonnig", 1: "overwegend zonnig", 2: "half bewolkt", 3: "bewolkt", 45: "mist", 48: "mist",
       51: "lichte motregen", 53: "motregen", 55: "zware motregen", 56: "ijzel", 57: "ijzel",
       61: "lichte regen", 63: "regen", 65: "zware regen", 66: "ijzelregen", 67: "ijzelregen",
       71: "lichte sneeuw", 73: "sneeuw", 75: "zware sneeuw", 77: "sneeuwkorrels",
       80: "lichte buien", 81: "buien", 82: "zware buien", 85: "sneeuwbuien", 86: "zware sneeuwbuien",
       95: "onweer", 96: "onweer met hagel", 99: "zwaar onweer met hagel"}
DAGEN = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]


def haal_weer(diag):
    q = urllib.parse.urlencode({"name": PLAATS, "count": 1, "language": "nl", "format": "json"})
    geo = json.loads(haal("https://geocoding-api.open-meteo.com/v1/search?" + q))
    plek = geo["results"][0]
    q = urllib.parse.urlencode({
        "latitude": plek["latitude"], "longitude": plek["longitude"],
        "current": "temperature_2m,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": "Europe/Amsterdam", "forecast_days": 3})
    d = json.loads(haal("https://api.open-meteo.com/v1/forecast?" + q))
    c, dg = d["current"], d["daily"]
    delen = ["%s nu %d\u00b0 %s, wind %d km/u" % (plek["name"], round(c["temperature_2m"]),
                                                WMO.get(c["weather_code"], ""), round(c["wind_speed_10m"]))]
    for i, naam in enumerate(["Vandaag", "Morgen", None]):
        if i >= len(dg["time"]):
            break
        if naam is None:
            y, m, dd = map(int, dg["time"][i].split("-"))
            naam = DAGEN[datetime.date(y, m, dd).weekday()].capitalize()
        kans = dg["precipitation_probability_max"][i]
        tekst = "%s %d\u00b0 tot %d\u00b0, %s" % (naam, round(dg["temperature_2m_min"][i]),
                                                round(dg["temperature_2m_max"][i]),
                                                WMO.get(dg["weather_code"][i], ""))
        if kans is not None and kans >= 30:
            tekst += ", %d%% kans op neerslag" % kans
        delen.append(tekst)
    diag.append("Weer: %s" % plek["name"])
    return "  \u2022  ".join(delen)


def run_once(eerst, vild):
    items, diag = ontleden(ophalen(), vild)
    oud = {}
    try:
        with open(UIT_JSON, encoding="utf-8") as f:
            oud = json.load(f)
    except Exception:
        pass
    extra, edia = {}, []
    for sleutel, fn, leeg in (("nos", haal_nos, []), ("weer", haal_weer, ""), ("brabant", haal_brabant, [])):
        waarde = leeg
        try:
            waarde = fn(edia)
        except Exception as e:
            edia.append("%s mislukt: %r" % (sleutel, e))
        extra[sleutel] = waarde or oud.get(sleutel, leeg)  # bij een storing blijft de vorige waarde staan
    data = {"t": int(time.time() * 1000), "n": len(items), "items": items[:MAX_ITEMS]}
    data.update(extra)
    gegevens = json.dumps(data, ensure_ascii=True)
    schrijf(UIT_JS, "window.VERKEER = " + gegevens + ";\n")
    schrijf(UIT_JSON, gegevens + "\n")
    schrijf(EXTRA_DIAG, "\n".join(edia))
    if eerst:
        schrijf(DIAGNOSE, diag)
    print(time.strftime("%H:%M:%S"), "-", len(items), "files geschreven;", " | ".join(edia))
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
