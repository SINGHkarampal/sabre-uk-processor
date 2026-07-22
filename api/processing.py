# SATNAM SHRI WAHEGURU JI SATNAM SHRI WAHEGURU JI SATNAM SHRI WAHEGURU JI SATNAM SHRI WAHEGURU JI SATNAM SHRI WAHEGURU JI
# SATNAM SHRI WAHEGURU JI SATNAM SHRI WAHEGURU JI SATNAM SHRI WAHEGURU JI SATNAM SHRI WAHEGURU JI SATNAM SHRI WAHEGURU JI
# SATNAM SHRI WAHEGURU JI SATNAM SHRI WAHEGURU JI SATNAM SHRI WAHEGURU JI SATNAM SHRI WAHEGURU JI SATNAM SHRI WAHEGURU JI
# WAHEGURU JI WAHEGURU JI WAHEGURU JI WAHEGURU JI WAHEGURU JI WAHEGURU JI WAHEGURU JI WAHEGURU JI WAHEGURU JI WAHEGURU JI
# WAHEGURU JI WAHEGURU JI WAHEGURU JI WAHEGURU JI WAHEGURU JI WAHEGURU JI WAHEGURU JI WAHEGURU JI WAHEGURU JI WAHEGURU JI
print("SATNAM SHRI WAHEGURU JI SATNAM SHRI WAHEGURU JI SATNAM SHRI WAHEGURU JI SATNAM SHRI WAHEGURU JI SATNAM SHRI WAHEGURU JI")
print("WAHEGURU JI PROCESSING ENDPOINT STARTED JI WAHEGURU JI")
# SATNAM SHRI WAHEGURU JI SATNAM SHRI WAHEGURU JI SATNAM SHRI WAHEGURU JI SATNAM SHRI WAHEGURU JI SATNAM SHRI WAHEGURU JI

"""
processing.py — Vercel serverless function ("processing").

Input:  {"text": "<already extracted plain text from a Sabre ticket PDF>"}
Output: JSON with the parsed sections (names_section, flight_segments, accounting,
        remarks) plus a "mapping" object (enquiry details, itinerary text, cabin
        class, associated-products groups) built entirely from raw IATA/provider
        codes.

This file never reads a CSV, never stores a Zoho record id, and never sees a
Zoho access token. Airport display names, airline Zoho record ids, ticket
stock-code prefixes and CRM vendor ids are resolved locally, after this
endpoint's response comes back — see resolve_associated_products() and
process_data() in SSupdateSS_fixed.py.
"""

import json
import re
import datetime
from collections import defaultdict
from http.server import BaseHTTPRequestHandler
from typing import Any, Dict, List, Tuple


# ================= text / names / segments / remarks extraction =================
# (ported from Sparser_FULL_FINAL.py — the CSV-backed from_full/to_full airport
#  name lookup has been dropped: those fields were unused outside dead code, and
#  keeping any CSV lookup out of this file entirely is the whole point of the split)

def _collapse_ws(s: str) -> str:
    if s is None:
        return ""
    s = s.replace("\xa0", " ").replace(" ", " ").replace(" ", " ")
    s = s.replace("​", "").replace("‎", "").replace("﻿", "")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def extract_names_section(text: str) -> List[Dict[str, str]]:
    pattern = re.compile(
        r"(?P<passenger_ref>\d+\.\d+)\s*(?P<lastname>[A-Z][A-Z\s]*?)\s*/\s*(?P<firstname>[A-Z\s\*]+)",
        re.IGNORECASE,
    )
    out: List[Dict[str, str]] = []
    for m in pattern.finditer(text or ""):
        passenger_ref = m.group("passenger_ref")
        lastname = " ".join((m.group("lastname") or "").strip().split()).title()
        firstname = " ".join((m.group("firstname") or "").strip().split()).title()
        out.append({"passenger_ref": passenger_ref, "lastname": lastname, "firstname": firstname})
    return out


def _normalize_time_24h(token: str) -> Tuple[str, int]:
    if not token:
        return token, 0
    t = (token or "").strip().upper().replace(" ", "")
    day_shift = 0
    mshift = re.search(r"\+(\d+)$", t)
    if mshift:
        day_shift = int(mshift.group(1))
        t = t[:mshift.start()]
    m = re.match(r"^(\d{1,2}):?(\d{2})([AP](?:M)?)?$", t)
    if not m:
        m = re.match(r"^(\d)(\d{2})([AP](?:M)?)?$", t)
        if not m:
            return token, day_shift
    h = int(m.group(1))
    minute = int(m.group(2))
    ampm = (m.group(3) or "").upper()
    if ampm:
        if ampm.startswith("P") and h != 12:
            h = (h % 12) + 12
        if ampm.startswith("A") and h == 12:
            h = 0
    h = max(0, min(h, 23))
    minute = max(0, min(minute, 59))
    return f"{h:02d}:{minute:02d}", day_shift


def extract_flight_segments(text: str) -> List[Dict[str, str]]:
    lines = [re.sub(r'\s+', ' ', (ln or '').rstrip()) for ln in (text or "").splitlines()]
    candidates = []
    for i, line in enumerate(lines):
        if (
            re.search(r'\b(?:HK|GK|KK|TK|LK|SS|RR|RQ)\d*\b', line, re.I)
            and re.search(r'\b[A-Z]{3}\s*[A-Z]{3}\b', line, re.I)
            and re.search(r'\b\d{1,2}:?\d{2}(?:[AP](?:M)?)?\b\s+\b\d{1,2}:?\d{2}(?:[AP](?:M)?)?\b', line, re.I)
        ):
            cand = _collapse_ws(line)
            nxt = _collapse_ws(lines[i + 1]) if i + 1 < len(lines) else ""
            if re.match(r'^\s*/D[A-Z0-9]', nxt, re.I):
                cand += " " + nxt
            cand = re.sub(r'\s+', ' ', cand).strip()
            candidates.append(cand)

    pattern = re.compile(
        r'^\s*(?P<segment_number>\d+)\s+'
        r'(?P<airline>[A-Z0-9]{2})\s*'
        r'(?P<flight_number>\d+)\s*(?P<seat_class>[A-Z])\s+'
        r'(?P<departure_date>\d{2}[A-Z]{3})'
        r'(?:\s+(?P<weekday>[A-Z]{1,3}|\d))?\s+'
        r'(?P<from>[A-Z]{3})(?P<to>[A-Z]{3})\s*'
        r'(?:\*?\s*)?(?P<status>[A-Z]{2}\d*)\s+'
        r'(?P<departure_time>\d{1,2}:?\d{2}(?:[AP](?:M)?)?)\s+'
        r'(?P<arrival_time>\d{1,2}:?\d{2}(?:[AP](?:M)?)?)(?P<arrival_day_shift>\+\d+)?'
        r'(?:\s+(?P<arrive_date>\d{2}[A-Z]{3})(?:\s+(?P<arrive_weekday>[A-Z]{1,3}|\d))?)?'
        r'(?:.*?/(?:DC|D)(?:[A-Z0-9]{1,3})\*(?P<airline_ref>[A-Z0-9]+))?',
        re.IGNORECASE,
    )

    out = []
    for line in candidates:
        line = _collapse_ws(line)
        m = pattern.search(line)
        if not m:
            continue
        seg = m.groupdict()
        dep24, _ = _normalize_time_24h(seg.get("departure_time", ""))
        arr24, shift = _normalize_time_24h((seg.get("arrival_time", "") or "") + (seg.get("arrival_day_shift") or ""))
        seg["departure_time_24h"] = dep24
        seg["arrival_time_24h"] = arr24
        seg["arrival_day_shift"] = str(shift) if shift else None
        out.append(seg)
    return out


def extract_remarks(text: str) -> Dict[str, str]:
    m = re.search(r"\bBOOKING\s*NO\.?\s*(\d+/\d+)", text or "", re.IGNORECASE)
    number = m.group(1) if m else ""
    normalized_full = f"Booking No.{number}" if number else ""

    provider_match = re.search(r"\bFROM\s+([A-Z]{3})\b", text or "", re.IGNORECASE)
    return {
        "Booking No.": number,
        "Booking No Full": normalized_full,
        "Provider": (provider_match.group(1) if provider_match else "").upper(),
        "Raw": text or "",
    }


# ================= accounting extraction & refund netting (from SSupdateSS_fixed.py) =================

pattern_relaxed = re.compile(
    r"(?P<line>\d+)\.\s+(?P<airline_raw>R?[A-Z0-9]{2})[^A-Za-z0-9](?P<eticket>\d{10})\s*/\s*"
    r"(?P<commission>[\d.]+)\s*/\s*"
    r"(?P<fare>[\d.]+)\s*/\s*"
    r"(?P<taxes>[\d.]+)\s*/(?P<fare_type>\w+)\s*/"
    r"(?P<payment>[A-Z]{2})(?P<extra_info>[A-Za-z0-9\s]{0,40})"
    r"(?:\s+\d{1,3})?\s+"
    r"(?P<passenger_ref>\d+\.\d+)"
    r"(?P<full_name>[A-Z]+(?:\s+[A-Z]+)*)"
    r"(?:/(?P<ticket_count>\d+)/(?P<fare_basis>[A-Z])(?:/(?P<something>[A-Z]))?)?",
    re.IGNORECASE
)


def clean_broken_name(full_name: str) -> str:
    return "".join(full_name.split())


def _normalize_name(s: str) -> str:
    s = re.sub(r"\*+[A-Z]*", "", s or "")
    s = " ".join(s.split())
    return s.title()


def group_accounting_entries(text: str) -> List[str]:
    if "ACCOUNTING DATA" not in text:
        return []

    section = text.split("ACCOUNTING DATA", 1)[-1]
    lines = section.splitlines()

    entries = []
    current = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if re.match(r"^\d+\.\s+(?:R?[A-Z0-9]{2})[^A-Za-z0-9]", line):
            if current:
                current_clean = current.split(" http", 1)[0]
                entries.append(" ".join(current_clean.strip().split()))
            current = line
        else:
            current += " " + line

    if current:
        current_clean = current.split(" http", 1)[0]
        entries.append(" ".join(current_clean.strip().split()))

    return [e for e in entries if re.match(r"^\d+\.", e)]


def extract_accounting_data(text: str) -> List[Dict[str, Any]]:
    entries = group_accounting_entries(text)
    result = []
    for entry in entries:
        compact = " ".join(entry.split())
        match = pattern_relaxed.search(compact)
        if match:
            data = match.groupdict()

            airline_raw = (data.get("airline_raw") or data.get("airline") or "").strip().upper()
            is_refund = bool(airline_raw.startswith("R") and len(airline_raw) == 3)
            base_airline = airline_raw[1:] if is_refund else airline_raw
            data["airline_raw"] = airline_raw
            data["is_refund"] = is_refund
            data["airline"] = base_airline

            data["margin"] = data.get("commission")
            data["price"] = data.get("fare")
            data["tax"] = data.get("taxes")

            data["full_name"] = _normalize_name(clean_broken_name(data["full_name"]))

            if data["payment"] == "CA" and not data.get("extra_info"):
                data["extra_info"] = "No extra info for bank deposit"
            result.append(data)

    return result


def match_with_names_section(accounting_data: List[Dict[str, Any]], names: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    ref_to_name = {}
    for n in names:
        first = _normalize_name(n.get("firstname", ""))
        last = _normalize_name(n.get("lastname", ""))
        ref_to_name[n["passenger_ref"]] = f"{first} {last}".strip()

    merged = []
    for entry in accounting_data:
        if "unmatched" in entry:
            merged.append(entry)
            continue

        ref = entry.get("passenger_ref")
        canon_name = ref_to_name.get(ref) or "❓ Unknown Passenger"

        raw_acc_name = _normalize_name(entry.get("full_name", ""))

        def _tok(s: str) -> set:
            return set(re.findall(r"[A-Za-z]+", s or ""))

        tokens_ok = False
        if canon_name != "❓ Unknown Passenger":
            tokens_ok = (_tok(canon_name) <= _tok(raw_acc_name)) or (_tok(raw_acc_name) <= _tok(canon_name))

        entry["matched_name"] = canon_name
        entry["accounting_name_raw"] = raw_acc_name
        entry["name_match"] = bool(tokens_ok)

        merged.append(entry)
    return merged


def _money_to_float(v: Any) -> float:
    try:
        if v is None:
            return 0.0
        s = str(v).strip()
        if not s:
            return 0.0
        s = re.sub(r"[^0-9.\-]", "", s)
        return float(s) if s not in ("", "-", ".", "-.", ".-") else 0.0
    except Exception:
        return 0.0


def net_out_refunds(accounting_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    charges_by_key: Dict[tuple, List[Dict[str, Any]]] = {}

    for e in (accounting_data or []):
        air_raw = (e.get("airline_raw") or e.get("airline") or "").strip().upper()
        is_ref = bool(e.get("is_refund")) if "is_refund" in e else bool(air_raw.startswith("R") and len(air_raw) == 3)
        base = air_raw[1:] if (is_ref and len(air_raw) == 3) else air_raw
        e["airline_raw"] = air_raw
        e["is_refund"] = is_ref
        e["airline"] = base
        key = (base, str(e.get("eticket") or "").strip())
        if not is_ref:
            charges_by_key.setdefault(key, []).append(e)

    out: List[Dict[str, Any]] = []
    for e in (accounting_data or []):
        air_raw = (e.get("airline_raw") or e.get("airline") or "").strip().upper()
        is_ref = bool(e.get("is_refund")) if "is_refund" in e else bool(air_raw.startswith("R") and len(air_raw) == 3)
        base = air_raw[1:] if (is_ref and len(air_raw) == 3) else air_raw
        e["airline_raw"] = air_raw
        e["is_refund"] = is_ref
        e["airline"] = base
        key = (base, str(e.get("eticket") or "").strip())

        if is_ref:
            targets = charges_by_key.get(key) or []
            if targets:
                t = targets[0]
                for fld in ("commission", "fare", "taxes", "margin", "price", "tax"):
                    t_val = _money_to_float(t.get(fld))
                    r_val = abs(_money_to_float(e.get(fld)))
                    net = t_val - r_val
                    t[fld] = f"{net:.2f}"
                continue
            else:
                for fld in ("commission", "fare", "taxes", "margin", "price", "tax"):
                    r_val = abs(_money_to_float(e.get(fld)))
                    e[fld] = f"{(-r_val):.2f}"
                e["is_refund_unmatched"] = True
                out.append(e)
        else:
            out.append(e)

    return out


# ================= date/time helpers (from the appended Flight_Tickets module) =================

def _today_london_date():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("Europe/London")).date()
    except Exception:
        return datetime.datetime.now().date()


def _infer_year_for_ddMMM(ddmmm: str, ref_date=None):
    if not ddmmm:
        return None
    s = str(ddmmm).strip().upper()

    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        try:
            return int(s[:4])
        except Exception:
            return None

    if not re.match(r"^\d{2}[A-Z]{3}$", s):
        return None

    ref = ref_date or _today_london_date()
    try:
        y = int(getattr(ref, "year", datetime.datetime.now().year))
    except Exception:
        y = datetime.datetime.now().year

    try:
        dt = datetime.datetime.strptime(f"{s}{y}", "%d%b%Y").date()
    except Exception:
        return None

    if dt < ref:
        y2 = y + 1
        try:
            datetime.datetime.strptime(f"{s}{y2}", "%d%b%Y")
            return y2
        except Exception:
            return y2
    return y


def _parse_ddMMM_to_iso(ddmmm: str, ref_date=None) -> str:
    if not ddmmm:
        return ""
    s = str(ddmmm).strip().upper()

    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s

    year = _infer_year_for_ddMMM(s, ref_date=ref_date)
    if not year:
        return s

    try:
        dt = datetime.datetime.strptime(f"{s}{year}", "%d%b%Y")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return s


def _fmt_hhmm(val: str) -> str:
    if not val:
        return ""
    s = str(val).strip()
    if re.match(r"^\d{4}$", s):
        return s
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if m:
        return f"{int(m.group(1)):02d}{m.group(2)}"
    return s


# ================= cabin class mapping (Smap) — a static dict, not a CSV =================

Smap = {
    "F": "First", "A": "First Class", "P": "First Class",
    "J": "Business Class", "C": "Business Class", "D": "Business Class", "Z": "Business Class", "I": "Business Class",
    "W": "Premium Econ", "T": "Premium Econ", "R": "Premium Econ",
    "Y": "Economy", "M": "Economy", "B": "Economy", "H": "Economy", "K": "Economy",
    "Q": "Economy", "L": "Economy", "U": "Economy"
}


def _nice_cabin(cabin_code: str) -> str:
    label = Smap.get((cabin_code or "").upper(), "N/A")
    if label == "First":
        label = "First Class"
    if label == "Premium Econ":
        label = "Premium Economy"
    return label


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def _format_dep_date(dd_mmm: str, year: int) -> str:
    if not dd_mmm or len(dd_mmm) < 4:
        return dd_mmm or ""
    auto_year = _infer_year_for_ddMMM(dd_mmm) or year
    try:
        dd = int(dd_mmm[:2])
        mon = dd_mmm[2:5].title()
        return f"{_ordinal(dd)} {mon} {auto_year}"
    except Exception:
        return dd_mmm


def _fmt_seg_line(seg: dict) -> str:
    air = (seg.get("airline") or "").strip().upper()
    fno = str(seg.get("flight_number") or "").strip()
    date = _parse_ddMMM_to_iso(seg.get("departure_date") or "")
    try:
        d_h = datetime.datetime.strptime(date, "%Y-%m-%d").strftime("%d %b %Y")
    except Exception:
        d_h = date
    o = seg.get("from") or seg.get("origin") or ""
    d = seg.get("to") or seg.get("destination") or ""
    dt = _fmt_hhmm(seg.get("departure_time_24h") or seg.get("departure_time") or seg.get("dep_time") or "")
    at = _fmt_hhmm(seg.get("arrival_time_24h") or seg.get("arrival_time") or seg.get("arr_time") or "")
    seat = (seg.get("seat_class") or "")[:1].upper()
    return f"{air}{fno} {d_h} {o} {dt} → {d} {at} ({seat})".strip()


# ================= mapping: enquiry details / names liststring =================

def generate_enquiry_details(parsed_data):
    enquiry_details = "Passenger(s) : " + ", ".join(
        [f"{p['firstname']} {p['lastname']}" for p in parsed_data["names_section"]]
    ) + "\n"

    flight_itinerary_parts = []
    for seg in parsed_data.get("flight_segments", []):
        origin = seg.get('origin') or seg.get('from') or ''
        destination = seg.get('destination') or seg.get('to') or ''
        dep_date = seg.get('departure_date') or ''
        airline = seg.get('airline') or ''
        flight_number = seg.get('flight_number') or ''
        flight_itinerary_parts.append(f"{origin} to {destination} on {dep_date} ({airline}{flight_number})")
    enquiry_details += "Flight Itinerary: " + ", ".join(flight_itinerary_parts) + "\n"

    cabin_class = "N/A"
    if parsed_data.get("flight_segments"):
        first_seg = parsed_data["flight_segments"][0]
        cabin_class = first_seg.get("class") or first_seg.get("seat_class") or "N/A"
    enquiry_details += "Cabin Class: " + cabin_class + "\n"

    enquiry_details += "Ticket(s): " + ", ".join(
        [e["eticket"] for e in parsed_data["accounting"]]
    ) if parsed_data["accounting"] else "N/A"

    return enquiry_details


def namesliststring(data):
    names = ''
    for name in data['names_section']:
        name = name['firstname'] + name['lastname']
        names = names + "," + name
    return names


# ================= mapping: itinerary text (Associated-only segments) =================

def build_segments_description_associated_only(data: dict) -> str:
    segments = data.get("flight_segments") or []
    if not segments:
        return ""
    acc_airlines = {(a.get("airline") or "").strip().upper() for a in (data.get("accounting") or []) if a.get("airline")}

    seg_airlines = {(s.get("airline") or "").strip().upper() for s in segments if s.get("airline")}
    multi_segments_but_single_acc = (len(seg_airlines) > 1 and len(acc_airlines) == 1)

    assoc_indexes = set()
    try:
        _rem_vals = []
        if isinstance(data.get("remarks"), dict):
            _rem_vals = [str(v) for v in data["remarks"].values() if v is not None]
        elif isinstance(data.get("remarks"), list):
            _rem_vals = [str(v) for v in data["remarks"] if v is not None]
        _rem_text = " | ".join(_rem_vals).upper()
        m = re.search(r"(?:Z.?¥?N|\bN)\s*([0-9,\s]+)", _rem_text)
        if m:
            assoc_indexes = {x.strip() for x in m.group(1).split(",") if x.strip()}
    except Exception:
        assoc_indexes = set()

    def _is_associated(seg: dict) -> bool:
        if assoc_indexes:
            for k in ("segment_no", "segment_number", "seg_no"):
                v = seg.get(k)
                if v is not None and str(v).strip() in assoc_indexes:
                    return True
            return False
        if multi_segments_but_single_acc:
            return True
        airline = (seg.get("airline") or "").strip().upper()
        return airline in acc_airlines if airline else True

    assoc_segments = [s for s in segments if _is_associated(s)]
    lines = [_fmt_seg_line(s) for s in assoc_segments]
    return "\n".join(lines)


# ================= mapping: associated products (raw codes + {{STOCK:xx}} placeholder) =================
# NOTE: the original prepare_associated_products() resolved a provider code straight
# to a Zoho Vendor_Supplier record id and a hardcoded Zoho Product_Name1 id. Both are
# Zoho-specific ids and must not live in this file (see module docstring), so this
# version returns "provider_code" and lets local code (resolve_associated_products in
# SSupdateSS_fixed.py) attach the actual Zoho ids. Likewise the per-passenger e-ticket
# prefix (normally your internal "stock" code, from stock_iata_pairs.csv) is emitted
# as a {{STOCK:<IATA>}} placeholder token for local code to substitute.

def prepare_associated_products(data):
    associated_products = []

    provider = (data['remarks'].get('Provider', 'Unknown Provider') or '').upper().strip()

    segments = data.get("flight_segments", []) or []
    acc_airlines = {(a.get("airline") or "").strip().upper() for a in (data.get("accounting") or []) if a.get("airline")}
    seg_airlines = {(s.get("airline") or "").strip().upper() for s in segments if s.get("airline")}
    multi_segments_but_single_acc = (len(seg_airlines) > 1 and len(acc_airlines) == 1)

    def seg_order(s):
        for k in ("segment_no", "segment_number", "seg_no"):
            if s.get(k) is not None:
                try:
                    return int(re.sub(r"\D+", "", str(s.get(k))) or "9999")
                except Exception:
                    return 9999
        return 9999

    assoc_indexes = set()
    try:
        _rem_vals = []
        if isinstance(data.get("remarks"), dict):
            _rem_vals = [str(v) for v in data["remarks"].values() if v is not None]
        elif isinstance(data.get("remarks"), list):
            _rem_vals = [str(v) for v in data["remarks"] if v is not None]
        _rem_text = " | ".join(_rem_vals).upper()
        m = re.search(r"(?:Z.?¥?N|\bN)\s*([0-9,\s]+)", _rem_text)
        if m:
            assoc_indexes = {x.strip() for x in m.group(1).split(",") if x.strip()}
    except Exception:
        assoc_indexes = set()

    def _is_associated(seg: dict) -> bool:
        if assoc_indexes:
            for k in ("segment_no", "segment_number", "seg_no"):
                v = seg.get(k)
                if v is not None and str(v).strip() in assoc_indexes:
                    return True
            return False
        if multi_segments_but_single_acc:
            return True
        airline = (seg.get("airline") or "").strip().upper()
        return airline in acc_airlines if airline else True

    associated_only = [s for s in sorted(segments, key=seg_order) if _is_associated(s)]
    seg_lines_all = [_fmt_seg_line(s) for s in associated_only]
    all_segments_block = "\n".join(seg_lines_all)

    ref_to_name = {p.get("passenger_ref"): f"{(p.get('firstname') or '').strip().title()} {(p.get('lastname') or '').strip().title()}".strip()
                   for p in data.get("names_section", [])}

    rows = []
    for entry in data.get("accounting", []):
        pref = entry.get("passenger_ref")
        pax_name = ref_to_name.get(pref, "Unknown Passenger")

        try:
            fare = float(entry.get("fare", 0.0))
        except Exception:
            fare = 0.0
        try:
            taxes = float(entry.get("taxes", 0.0))
        except Exception:
            taxes = 0.0
        buy = fare + taxes

        air = (entry.get("airline", "") or "").strip().upper()
        etkt = (entry.get("eticket") or "").strip()
        stock_placeholder = f"{{{{STOCK:{air}}}}}" if air else air
        # Keep the Vercel payload compatible with the original Zoho rich-text
        # accounting layout.  The stock code is still resolved locally after
        # this response comes back.
        ticket_text = f"Etiket No. : {stock_placeholder}-{etkt}" if etkt else ""

        payment_method = entry.get("payment", "")
        original_currency = None
        card_used = "Bank Transfer"

        if payment_method == "CA":
            original_currency = None
            card_used = "Bank Transfer"
        elif payment_method == "CC":
            extra_info = (entry.get("extra_info", "") or "").replace(" ", "")
            card_used = (extra_info[:2] + " " + (extra_info.strip())[-4:]) if extra_info else ""
            two = card_used[:2]
            if two == "AX":
                last_four = card_used[-4:]
                if last_four in ['1001', '1000', '1003', '1009', '1012', '1018', '2006', '3002', '3004', '4002', '4004', '6002', '7012', '8018', '3003']:
                    original_currency = None
                elif last_four == '1006':
                    original_currency = 'EUR'
                else:
                    original_currency = 'USD'
            elif two == "VI":
                original_currency = None
            else:
                original_currency = None
            if not card_used:
                card_used = "Card"
        else:
            card_used = "Bank Transfer"

        pt = card_used.strip().replace("  ", " ")
        if original_currency == 'EUR':
            pt += " (EUR)"
        elif original_currency == 'USD':
            pt += " (USD)"

        rows.append({
            "pax_name": pax_name,
            "ticket_text": ticket_text,
            "buy": buy,
            "currency": original_currency,
            "payment_type": pt or "Bank Transfer",
        })

    groups = defaultdict(lambda: {"passengers": [], "sum_buy": 0.0, "sum_fx": 0.0})

    base = (provider, all_segments_block)
    for r in rows:
        cur = r["currency"] or "GBP"
        key = base + (cur, r["payment_type"])
        g = groups[key]
        g["passengers"].append({
            "pax_name": r["pax_name"],
            "ticket_text": r["ticket_text"],
        })
        if r["currency"] is None:
            g["sum_buy"] += float(r["buy"] or 0.0)
        else:
            g["sum_fx"] += float(r["buy"] or 0.0)

    today = datetime.datetime.now().strftime("%Y-%m-%d")
    for key, g in groups.items():
        _, seg_block, cur, payment_type = key

        # Restore the original Zoho rich-text appearance:
        #   centred passenger name
        #   one <div> per associated flight segment
        #   centred e-ticket line
        # Multiple passengers in the same grouped accounting row are separated
        # by one blank rich-text line, while buy/FX totals remain grouped.
        seg_lines = [ln.strip() for ln in (seg_block or "").splitlines() if ln.strip()]
        passenger_blocks = []
        for passenger in g["passengers"]:
            top = f"<div style='text-align:center'>{passenger['pax_name']}</div>"
            middle = "".join(f"<div>{ln}</div>" for ln in seg_lines)
            bottom_text = passenger.get("ticket_text") or ""
            bottom = f"<div style='text-align:center'>{bottom_text}</div>" if bottom_text else ""
            passenger_blocks.append(f"{top}{middle}{bottom}")
        description = "<div><br></div>".join(passenger_blocks)

        out = {
            "provider_code": provider,
            "description": description,
            "payment_date": today,
            "other_payment_type_used": [payment_type],
        }
        if cur == "GBP":
            out["buy"] = round(g["sum_buy"], 2)
            out["original_payment_currency"] = None
            out["foreign_currency_amount"] = "0"
        else:
            out["buy"] = None
            out["original_payment_currency"] = cur
            out["foreign_currency_amount"] = str(round(g["sum_fx"], 2))

        associated_products.append(out)

    return associated_products


# ================= mapping: dead-code duplicates, carried over verbatim (unused) =================
# generate_accounting_description()/compute_pax_blocks() are not called anywhere in the
# live push path (same as in the original SSupdateSS_fixed.py) — lifted as-is, no
# CSV-boundary rework needed since nothing depends on their output today.

def generate_accounting_description(data):
    pax_order = []
    for ent in data.get("accounting", []):
        ref = ent.get("passenger_ref")
        if ref is not None and ref not in pax_order:
            pax_order.append(ref)

    pax_by_ref = {}
    for p_ in data.get("names_section", []):
        first = (p_.get("firstname") or "").strip().title()
        last = (p_.get("lastname") or "").strip().upper()
        pax_by_ref[p_.get("passenger_ref")] = f"{first} {last}".strip()

    segments = data.get("flight_segments", []) or []
    seg_airlines = {(s.get("airline") or "").strip().upper() for s in segments if s.get("airline")}
    acc_airlines = {(a.get("airline") or "").strip().upper() for a in data.get("accounting", []) if a.get("airline")}
    multi_segments_but_single_acc = (len(seg_airlines) > 1 and len(acc_airlines) == 1)

    def seg_order(s):
        for k in ("segment_no", "segment_number"):
            if s.get(k) is not None:
                try:
                    return int(re.sub(r"\D+", "", str(s.get(k))) or "9999")
                except Exception:
                    return 9999
        return 9999

    from collections import OrderedDict
    acc_by_pax = OrderedDict((ref, []) for ref in pax_order)
    for ent in data.get("accounting", []):
        ref = ent.get("passenger_ref")
        if ref in acc_by_pax:
            acc_by_pax[ref].append(ent)

    descriptions = []
    for pax_ref in pax_order:
        pax_name = pax_by_ref.get(pax_ref, "Unknown Passenger")
        pax_accs = acc_by_pax.get(pax_ref, [])

        tickets = []
        for a in pax_accs:
            et = str(a.get('eticket', '') or '').strip()
            if not et:
                continue
            air = (a.get('airline') or '').strip().upper()
            tickets.append(f"Etiket No. : {{{{STOCK:{air}}}}}-{et}")

        etkt_air = (pax_accs[0].get("airline", "").strip().upper() if pax_accs else "")

        if multi_segments_but_single_acc:
            mid_lines = [_fmt_seg_line(s) for s in sorted(segments, key=seg_order)]
        else:
            chosen = None
            for s in segments:
                if (s.get("airline") or "").strip().upper() == etkt_air:
                    chosen = s
                    break
            chosen = chosen or (segments[0] if segments else None)
            mid_lines = [_fmt_seg_line(chosen)] if chosen else []

        top = f"<div style='text-align:center'>{pax_name}</div>"
        mid = "".join(f"<div>{ln}</div>" for ln in mid_lines)
        bot = f"<div style='text-align:center'>{' / '.join(tickets)}</div>"
        descriptions.append(f"{top}{mid}{bot}")

    return descriptions


def compute_pax_blocks(data):
    current_year = datetime.datetime.now().year

    pax_order = []
    seen = set()
    for ent in data.get("accounting", []):
        ref = ent.get("passenger_ref")
        if ref is not None and ref not in seen:
            seen.add(ref)
            pax_order.append(ref)

    ref_to_name = {}
    for p_ in data.get("names_section", []):
        first = (p_.get("firstname") or "").strip().title()
        last = (p_.get("lastname") or "").strip().upper()
        ref_to_name[p_.get("passenger_ref")] = f"{first} {last}".strip()

    pax_first_acc = {}
    for ent in data.get("accounting", []):
        pref = ent.get("passenger_ref")
        if pref not in pax_first_acc:
            pax_first_acc[pref] = ent

    segments = data.get("flight_segments", []) or []
    seg_airlines = {(s.get("airline") or "").strip().upper() for s in segments if s.get("airline")}
    acc_airlines = {(a.get("airline") or "").strip().upper() for a in data.get("accounting", []) if a.get("airline")}
    multi_segments_but_single_acc = (len(seg_airlines) > 1 and len(acc_airlines) == 1)

    blocks = []
    for pref in pax_order:
        pax_name = ref_to_name.get(pref, "Unknown Passenger")
        entry = pax_first_acc.get(pref, {})

        try:
            fare = float(entry.get('fare', 0.0))
        except Exception:
            fare = 0.0
        try:
            taxes = float(entry.get('taxes', 0.0))
        except Exception:
            taxes = 0.0
        buy = fare + taxes

        tickets = []
        etkt_air = (entry.get("airline", "") or "").strip().upper()
        if entry.get("eticket"):
            tickets.append(str(entry["eticket"]).strip())

        if multi_segments_but_single_acc:
            chosen = sorted(segments, key=lambda s: s.get("segment_no") or 9999)[0] if segments else None
        else:
            chosen = None
            for s in segments:
                if (s.get("airline") or "").strip().upper() == etkt_air:
                    chosen = s
                    break
            chosen = chosen or (segments[0] if segments else None)

        segments_line = _fmt_seg_line(chosen) if chosen else ""

        payment_method = entry.get('payment', '')
        original_currency = None
        other_payment_type_used = ['Bank Transfer']

        if payment_method == 'CC':
            extra_info = (entry.get('extra_info', '') or '').replace(" ", "")
            card_used = extra_info[:2] + ' ' + (extra_info.strip())[-4:] if extra_info else ''
            other_payment_type_used = [card_used or 'Card']
            last_four = (card_used[-4:] if card_used else '')
            if last_four in ['1006']:
                original_currency = 'USD'
            elif last_four in ['1005']:
                original_currency = 'EUR'

        if original_currency == 'EUR':
            other_payment_type_used[0] += ' (EUR)'
        elif original_currency == 'USD':
            other_payment_type_used[0] += ' (USD)'

        blocks.append({
            "pax_ref": pref,
            "pax_name": pax_name,
            "segments_line": segments_line,
            "ticket_air": etkt_air,
            "tickets": tickets,
            "buy": buy,
            "original_currency": original_currency,
            "other_payment_type_used": other_payment_type_used
        })
    return blocks


# ================= top-level entry point =================

def processing(text: str) -> Dict[str, Any]:
    names = extract_names_section(text)
    flights = extract_flight_segments(text)
    acc = extract_accounting_data(text)
    acc_matched = match_with_names_section(acc, names)
    acc_matched = net_out_refunds(acc_matched)
    remarks = extract_remarks(text)

    data = {
        "names_section": names,
        "flight_segments": flights,
        "accounting": acc_matched,
        "remarks": remarks,
    }

    # --- Air_Cabin_Class: exactly mirrors the original inline Spnrsend() lambda
    # (raw Smap.get(), no _nice_cabin renaming) so the Zoho field value is unchanged.
    seg_seat = (flights[0].get("seat_class") if flights else None)
    acc_fb = (acc_matched[0].get("fare_basis") if acc_matched else None)
    seat_or_fb = (seg_seat or acc_fb).upper() if (seg_seat or acc_fb) else ""
    air_cabin_class = Smap.get(seat_or_fb) if seat_or_fb else None

    # --- first segment (segment_no == 1, else first) — mirrors process_data()'s own lookup
    first_seg = None
    for seg in flights:
        seg_no_value = None
        for key in ("segment_no", "segment_number", "seg_no"):
            if seg.get(key) is not None:
                seg_no_value = seg[key]
                break
        try:
            seg_no = int(str(seg_no_value)) if seg_no_value is not None else None
        except Exception:
            seg_no = seg_no_value
        if seg_no == 1:
            first_seg = seg
            break
    if first_seg is None and flights:
        first_seg = flights[0]
    first_seg = first_seg or {}

    de_date_raw = first_seg.get("departure_date")
    ar_date_raw = first_seg.get("arrival_date") or de_date_raw
    first_segment = {
        "origin_iata": (first_seg.get("origin") or first_seg.get("from") or ""),
        "destination_iata": (first_seg.get("destination") or first_seg.get("to") or ""),
        "airline_iata": (first_seg.get("airline") or ""),
        "departure_date_iso": _parse_ddMMM_to_iso(de_date_raw or ""),
        "arrival_date_iso": _parse_ddMMM_to_iso(ar_date_raw or (de_date_raw or "")),
    }

    etickets = [str(e.get("eticket", "")).strip() for e in acc_matched if e.get("eticket")]

    mapping = {
        "enquiry_details": generate_enquiry_details(data),
        "names_liststring": namesliststring(data),
        "eticket_joined": ",".join(etickets),
        "provider_code": (remarks.get("Provider") or "").upper().strip(),
        "first_segment": first_segment,
        "air_cabin_class": air_cabin_class,
        "flight_o_d_itinerary": build_segments_description_associated_only(data),
        "associated_products": prepare_associated_products(data),
    }
    data["mapping"] = mapping
    return data


# ================= Vercel Python function entry point =================

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
            text = payload.get("text") or ""
            result = processing(text)
            body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            err = json.dumps({"error": f"{type(e).__name__}: {e}"}).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(err)
