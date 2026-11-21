"""Extrai a tabela de disponibilidade do PDF Cruise America Flex Rate Plan.

Trabalha em nivel de caractere: as colunas (semanas) sao ancoradas pelos
centros dos anos no cabecalho, e cada celula e reconstruida pelos caracteres
que caem dentro dos limites da coluna. Somente o STATUS e mantido
(disponivel / RQ / NA) - os precos sao descartados por requisito do produto.
"""

import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime

import pdfplumber

PDF_URL = "https://www.flexrate.com/rates/CruiseAmerica-Flexrates.pdf"
PDF_PATH = "CruiseAmerica-Flexrates.pdf"
OUT_PATH = "public/cruise-america-availability.json"
RATES_PATH = None  # arquivo PRIVADO opcional com os valores brutos (nunca publicar)

MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
MONTH_RE = "|".join(MONTHS)
CATEGORIES = ["C-21", "C-25", "C-30"]

STATUS_AVAILABLE = "A"
STATUS_REQUEST = "RQ"
STATUS_UNAVAILABLE = "NA"


def group_lines(chars, tol=2.0):
    """Agrupa caracteres em linhas visuais pela coordenada 'top'."""
    lines = []
    for ch in sorted(chars, key=lambda c: (round(c["top"], 1), c["x0"])):
        for line in lines:
            if abs(line["top"] - ch["top"]) <= tol:
                line["chars"].append(ch)
                break
        else:
            lines.append({"top": ch["top"], "chars": [ch]})
    for line in lines:
        line["chars"].sort(key=lambda c: c["x0"])
    lines.sort(key=lambda l: l["top"])
    return lines


def line_string(chars, gap=1.0):
    """Retorna (texto, mapping) onde mapping[i] e o char de origem ou None."""
    out, mapping = [], []
    prev = None
    for ch in chars:
        if prev is not None and ch["x0"] - prev["x1"] > gap:
            out.append(" ")
            mapping.append(None)
        out.append(ch["text"])
        mapping.append(ch)
        prev = ch
    return "".join(out), mapping


def span_center(mapping, start, end):
    real = [c for c in mapping[start:end] if c]
    if not real:
        return None
    return (real[0]["x0"] + real[-1]["x1"]) / 2.0


def parse_station(lines):
    """Extrai nome e codigo IATA da estacao no cabecalho da pagina."""
    for line in lines[:10]:
        text, _ = line_string(line["chars"])
        text = text.strip()
        if "GMT" in text or "Flex Rate" in text:
            continue
        match = re.match(r"^([A-Z][A-Z0-9 ,.\-/&'\.]*?)\s*\(([A-Z0-9]{2,4})\)", text)
        if match:
            name = re.sub(r"\s+", " ", match.group(1)).strip()
            if len(name) >= 3:
                return name, match.group(2).strip()
    return None, None


def classify(raw):
    """Converte o conteudo bruto da celula em status de disponibilidade."""
    text = re.sub(r"[^A-Z0-9]", "", raw.upper())
    if not text:
        return ""
    if "RQ" in text:
        return STATUS_REQUEST
    if "NA" in text:
        return STATUS_UNAVAILABLE
    if re.search(r"\d", text):
        return STATUS_AVAILABLE
    return ""


def build_bounds(anchors):
    """Converte centros de coluna em faixas [lo, hi)."""
    bounds = []
    for i, a in enumerate(anchors):
        lo = -1e9 if i == 0 else (anchors[i - 1] + a) / 2.0
        hi = 1e9 if i == len(anchors) - 1 else (a + anchors[i + 1]) / 2.0
        bounds.append((lo, hi))
    return bounds


def parse_page(page):
    """Retorna (nome, codigo, {categoria: {semana: status}}) ou None."""
    lines = group_lines(page.chars)
    name, code = parse_station(lines)
    if not name:
        return None

    table = {cat: {} for cat in CATEGORIES}
    raw_cells = {cat: {} for cat in CATEGORIES}
    labels, bounds, data_x_min = [], [], None
    idx = 0

    while idx < len(lines):
        text, mapping = line_string(lines[idx]["chars"])
        month_hits = sum(1 for m in MONTHS if m in text)

        # Cabecalho: linha de "dia mes" seguida pela linha de anos
        if month_hits >= 4 and idx + 1 < len(lines):
            ytext, ymap = line_string(lines[idx + 1]["chars"])
            years = [
                (m.group(0), span_center(ymap, m.start(), m.end()))
                for m in re.finditer(r"20\d{2}", ytext)
            ]
            years = [y for y in years if y[1] is not None]

            dates = []
            for m in re.finditer(r"(\d{1,2})\s?(" + MONTH_RE + r")", text):
                c = span_center(mapping, m.start(), m.end())
                if c is not None:
                    dates.append((int(m.group(1)), m.group(2), c))

            if len(years) >= 4 and len(dates) == len(years):
                labels = []
                anchors = []
                for (year, ycenter), (day, month, _dc) in zip(years, dates):
                    labels.append(f"{day:02d} {month} {year}")
                    anchors.append(ycenter)
                bounds = build_bounds(anchors)
                data_x_min = anchors[0] - 22.0
                idx += 2
                continue

        # Linha de dados de uma categoria
        cat_match = re.match(r"^\s*(C-\d{2})", text)
        if cat_match and labels:
            category = cat_match.group(1)
            if category in CATEGORIES:
                label_end_x = None
                real = [c for c in mapping[: cat_match.end()] if c]
                if real:
                    label_end_x = real[-1]["x1"]

                buckets = {i: [] for i in range(len(bounds))}
                for ch in lines[idx]["chars"]:
                    if label_end_x is not None and ch["x1"] <= label_end_x + 0.5:
                        continue
                    if data_x_min is not None and ch["x1"] < data_x_min:
                        continue
                    cx = (ch["x0"] + ch["x1"]) / 2.0
                    for i, (lo, hi) in enumerate(bounds):
                        if lo <= cx < hi:
                            buckets[i].append((ch["x0"], ch["text"]))
                            break

                for i, items in buckets.items():
                    raw = "".join(t for _, t in sorted(items))
                    status = classify(raw)
                    if status:
                        table[category][labels[i]] = status
                        raw_cells[category][labels[i]] = raw.strip()

        idx += 1

    return name, code, table, raw_cells


def week_sort_key(label):
    day, month, year = label.split(" ")
    return datetime(int(year), MONTHS[month], int(day))


def main():
    global PDF_PATH, OUT_PATH, RATES_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default=PDF_PATH)
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--rates-out", default=None, help="JSON privado com valores brutos (opcional)")
    ap.add_argument("--download", action="store_true", help="baixa o PDF de PDF_URL antes de ler")
    args = ap.parse_args()
    PDF_PATH, OUT_PATH, RATES_PATH = args.pdf, args.out, args.rates_out
    if args.download:
        req = urllib.request.Request(PDF_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r, open(PDF_PATH, "wb") as fh:
            fh.write(r.read())
    stations, all_weeks, validity = [], set(), ""

    with pdfplumber.open(PDF_PATH) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            parsed = parse_page(page)
            if not parsed:
                print(f"[skip] p{page_no:02d}: estacao nao identificada")
                continue
            name, code, table, raw_cells = parsed

            if not validity:
                raw = page.extract_text() or ""
                m = re.search(r"Valid for all booking[^\n]*", raw)
                if m:
                    validity = re.sub(r"\s+", " ", m.group(0)).strip()

            filled = sum(len(v) for v in table.values())
            if filled == 0:
                print(f"[skip] p{page_no:02d}: {name} sem celulas")
                continue

            for cat in CATEGORIES:
                all_weeks.update(table[cat].keys())
            stations.append({"name": name, "code": code, "table": table, "raw": raw_cells})
            print(f"[ok] p{page_no:02d} {name} ({code}) -> {filled} celulas")

    weeks = sorted(all_weeks, key=week_sort_key)
    week_objs = []
    for label in weeks:
        d = week_sort_key(label)
        week_objs.append({"label": label, "iso": d.strftime("%Y-%m-%d")})

    out_stations = []
    for st in stations:
        rows = {cat: [st["table"][cat].get(lbl, "") for lbl in weeks] for cat in CATEGORIES}
        out_stations.append(
            {
                "name": st["name"],
                "code": st["code"],
                "label": f"{st['name']} ({st['code']})",
                "status": rows,
            }
        )
    out_stations.sort(key=lambda s: s["name"])

    data = {
        "source": "https://www.flexrate.com/rates/CruiseAmerica-Flexrates.pdf",
        "sourceTitle": "Cruise America Flex Rate Plan",
        "validity": validity,
        "extractedAt": datetime.now().strftime("%Y-%m-%d"),
        "updateFrequency": "semanal (toda segunda-feira)",
        "legend": {"A": "Disponível", "RQ": "Request Only", "NA": "Not Available"},
        "categories": CATEGORIES,
        "weeks": week_objs,
        "stations": out_stations,
    }

    # Salvaguarda: PDF mudou de layout ou veio vazio -> nao sobrescreve o JSON bom
    if len(out_stations) < 20 or len(weeks) < 20:
        print("ERRO: resultado suspeito (poucas estacoes/semanas). JSON nao atualizado.")
        sys.exit(1)

    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))

    if RATES_PATH:
        rates = {
            "extractedAt": data["extractedAt"],
            "validity": validity,
            "note": "Valores brutos das celulas do PDF (unidade a confirmar). PRIVADO.",
            "stations": {
                st["code"]: {cat: {w: st["raw"][cat].get(w, "") for w in weeks} for cat in CATEGORIES}
                for st in stations
            },
        }
        with open(RATES_PATH, "w", encoding="utf-8") as fh:
            json.dump(rates, fh, ensure_ascii=False, separators=(",", ":"))

    print("-" * 78)
    print(f"estacoes: {len(out_stations)} | semanas: {len(weeks)}")
    print(f"periodo: {weeks[0]} -> {weeks[-1]}")
    print(f"validade: {validity}")
    counts = {STATUS_AVAILABLE: 0, STATUS_REQUEST: 0, STATUS_UNAVAILABLE: 0, "": 0}
    for st in out_stations:
        for cat in CATEGORIES:
            for v in st["status"][cat]:
                counts[v] = counts.get(v, 0) + 1
    print("distribuicao de status:", counts)

    bon = next((s for s in out_stations if s["code"] == "BON"), None)
    if bon:
        print(f"VERIFICACAO {bon['label']} (primeiras 9 semanas):")
        for cat in CATEGORIES:
            pairs = [
                f"{weeks[i].split(' ')[0]}{weeks[i].split(' ')[1]}={bon['status'][cat][i] or '-'}"
                for i in range(9)
            ]
            print(f"  {cat}: " + " ".join(pairs))


if __name__ == "__main__":
    main()