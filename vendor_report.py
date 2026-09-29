# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx>=0.27", "openpyxl>=3.1"]
# ///
"""Vendor list with contacts for a group of properties (e.g. the 18 AG properties).

Finds every vendor billed against the selected properties over the last N months
(default 12), then pulls each vendor's contact details from Rent Manager and
writes them to an Excel file (and a CSV next to it).

Read-only: it only calls GET endpoints (plus the sign-in call).

Credentials come from the same environment variables as the README:
    RM_CORP_ID (or RM_BASE_URL), RM_USERNAME, RM_PASSWORD, RM_LOCATION_ID

Choosing the properties (pick one):
    --group "AG"            Rent Manager property group whose name matches (default: AG)
    --name-prefix "AG"      properties whose ShortName or Name starts with this text
    --property-ids 1,2,3    explicit PropertyIDs

Examples:
    uv run vendor_report.py
    uv run vendor_report.py --name-prefix AG --months 12 --out AG_vendors.xlsx
"""

from __future__ import annotations

import argparse
import atexit
import csv
import datetime as dt
import os
import sys
from collections import defaultdict
from typing import Any, Iterable

import httpx

TOKEN_HEADER = "X-RM12Api-ApiToken"
PAGE_SIZE = 1000


class RentManager:
    def __init__(self) -> None:
        base_url = os.environ.get("RM_BASE_URL")
        if not base_url:
            corp_id = os.environ.get("RM_CORP_ID")
            if not corp_id:
                sys.exit("Set RM_CORP_ID (or RM_BASE_URL).")
            base_url = f"https://{corp_id}.api.rentmanager.com"
        username, password = os.environ.get("RM_USERNAME"), os.environ.get("RM_PASSWORD")
        if not username or not password:
            sys.exit("Set RM_USERNAME and RM_PASSWORD.")
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=120)
        resp = self.http.post(
            "/Authentication/AuthorizeUser",
            json={
                "Username": username,
                "Password": password,
                "LocationID": int(os.environ.get("RM_LOCATION_ID", "1")),
            },
        )
        if resp.status_code >= 400:
            sys.exit(f"Rent Manager login failed ({resp.status_code}): {resp.text[:500]}")
        token = resp.json() if resp.text.startswith('"') else resp.text.strip()
        self.http.headers[TOKEN_HEADER] = token
        # Rent Manager caps concurrent logins per user, so always sign out.
        atexit.register(self.close)

    def close(self) -> None:
        if TOKEN_HEADER in self.http.headers:
            try:
                self.http.post("/Authentication/DeAuthorize")
            except httpx.HTTPError:
                pass
            del self.http.headers[TOKEN_HEADER]

    def get_all(self, path: str, **params: Any) -> list[dict]:
        """GET every page of a list endpoint."""
        params = {k: v for k, v in params.items() if v not in (None, "")}
        rows: list[dict] = []
        page = 1
        while True:
            resp = self.http.get(path, params={**params, "pagesize": PAGE_SIZE, "pagenumber": page})
            if resp.status_code >= 400:
                raise RuntimeError(f"GET {path} failed ({resp.status_code}): {resp.text[:1000]}")
            batch = resp.json() or []
            if isinstance(batch, dict):
                batch = [batch]
            rows.extend(batch)
            if len(batch) < PAGE_SIZE:
                return rows
            page += 1


# --- picking the properties ---------------------------------------------------


def select_properties(rm: RentManager, args: argparse.Namespace) -> dict[int, dict]:
    props = {p["PropertyID"]: p for p in rm.get_all("/Properties")}

    if args.property_ids:
        ids = {int(x) for x in args.property_ids.split(",") if x.strip()}
        return {i: props[i] for i in ids if i in props}

    if args.name_prefix:
        pre = args.name_prefix.lower()
        return {
            i: p
            for i, p in props.items()
            if str(p.get("ShortName", "")).lower().startswith(pre)
            or str(p.get("Name", "")).lower().startswith(pre)
        }

    groups = rm.get_all("/PropertyGroups", embeds="Properties")
    want = args.group.lower()
    matches = [g for g in groups if str(g.get("Name", "")).strip().lower() == want] or [
        g for g in groups if want in str(g.get("Name", "")).lower()
    ]
    if not matches:
        names = ", ".join(sorted(str(g.get("Name")) for g in groups))
        sys.exit(f"No property group named like '{args.group}'. Groups found: {names}\n"
                 "Use --name-prefix or --property-ids instead.")
    selected: dict[int, dict] = {}
    for g in matches:
        for p in g.get("Properties") or []:
            pid = p.get("PropertyID")
            if pid in props:
                selected[pid] = props[pid]
    return selected


# --- contact helpers (Rent Manager field names vary a little by version) ------


def _first(*vals: Any) -> str:
    for v in vals:
        if v not in (None, ""):
            return str(v).strip()
    return ""


PHONE_TYPES: dict[int, str] = {}


def _phones(obj: dict) -> list[str]:
    out = []
    for ph in obj.get("PhoneNumbers") or []:
        num = _first(ph.get("PhoneNumber"), ph.get("Number"), ph.get("StrippedPhoneNumber"))
        if not num:
            continue
        kind = _first((ph.get("PhoneNumberType") or {}).get("Name"), ph.get("PhoneNumberTypeName"),
                      PHONE_TYPES.get(ph.get("PhoneNumberTypeID")))
        ext = _first(ph.get("Extension"))
        out.append(f"{num}{' x' + ext if ext else ''}{' (' + kind + ')' if kind else ''}")
    for key in ("Phone", "PhoneNumber", "WorkPhone", "MobilePhone"):
        if obj.get(key):
            out.append(str(obj[key]))
    return list(dict.fromkeys(out))


def _address(obj: dict) -> str:
    addrs = obj.get("Addresses") or []
    primary = next((a for a in addrs if a.get("IsPrimary")), addrs[0] if addrs else None)
    if primary:
        return _first(
            primary.get("Address"),
            ", ".join(
                x for x in (primary.get("Street"), primary.get("City"),
                            primary.get("State"), primary.get("PostalCode")) if x
            ),
        ).replace("\r\n", ", ").replace("\n", ", ")
    return ""


def _name(contact: dict) -> str:
    name = f"{_first(contact.get('FirstName'))} {_first(contact.get('LastName'))}".strip()
    return "" if name.lower() == "main contact" else name  # Rent Manager's placeholder name


def _contacts(vendor: dict) -> list[dict]:
    contacts = [c for c in vendor.get("Contacts") or [] if c.get("IsActive", True)]
    contacts.sort(key=lambda c: not c.get("IsPrimary"))
    return contacts


# --- report -------------------------------------------------------------------


def build_rows(rm: RentManager, props: dict[int, dict], since: dt.date) -> list[dict]:
    bills = rm.get_all(
        "/Bills",
        filters=f"TransactionDate,ge,{since.isoformat()}",
        embeds="BillDetails",
    )

    spend: dict[int, float] = defaultdict(float)
    bill_ids: dict[int, set] = defaultdict(set)
    vendor_props: dict[int, set] = defaultdict(set)
    last_date: dict[int, str] = {}

    for bill in bills:
        # Bills carry the vendor as AccountID (AccountType "Vendor") and their own ID as ID.
        if bill.get("AccountType", "Vendor") != "Vendor":
            continue
        vid = bill.get("VendorID", bill.get("AccountID"))
        if vid is None:
            continue
        for d in bill.get("BillDetails") or []:
            pid = d.get("PropertyID")
            if pid not in props:
                continue
            spend[vid] += float(d.get("Amount") or 0)
            bill_ids[vid].add(bill.get("BillID", bill.get("ID")))
            vendor_props[vid].add(pid)
            date = str(bill.get("TransactionDate") or "")[:10]
            if date > last_date.get(vid, ""):
                last_date[vid] = date

    if not vendor_props:
        return []

    try:
        PHONE_TYPES.update({t["PhoneNumberTypeID"]: t.get("Name", "") for t in rm.get_all("/PhoneNumberTypes")})
    except RuntimeError:
        pass
    vendors = {
        v["VendorID"]: v
        for v in rm.get_all("/Vendors", embeds="Contacts,Contacts.PhoneNumbers,Addresses,PhoneNumbers")
    }

    def prop_label(pid: int) -> str:
        p = props[pid]
        return _first(p.get("ShortName"), p.get("Name"), pid)

    rows = []
    for vid in vendor_props:
        v = vendors.get(vid, {"VendorID": vid})
        contacts = _contacts(v)
        primary = contacts[0] if contacts else {}
        others = "; ".join(
            " ".join(x for x in (
                _name(c),
                _first(c.get("Email")),
                ", ".join(_phones(c)),
            ) if x.strip())
            for c in contacts[1:]
        )
        rows.append({
            "Vendor ID": vid,
            "Vendor": _first(v.get("Name"), v.get("DisplayName"), f"Vendor {vid}"),
            "Category": _first(v.get("Category")),
            "Active": v.get("IsActive", ""),
            "Primary Contact": _name(primary),
            "Email": _first(primary.get("Email"), v.get("Email")),
            "Phone": ", ".join(_phones(primary) or _phones(v)),
            "Address": _address(v) or _address(primary),
            "Other Contacts": others,
            "Properties Served": ", ".join(sorted(prop_label(p) for p in vendor_props[vid])),
            "# Properties": len(vendor_props[vid]),
            "# Bills": len(bill_ids[vid]),
            "Total Billed": round(spend[vid], 2),
            "Last Bill Date": last_date.get(vid, ""),
        })
    rows.sort(key=lambda r: r["Vendor"].lower())
    return rows


def write_outputs(rows: list[dict], out: str) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    headers = list(rows[0].keys())
    wb = Workbook()
    ws = wb.active
    ws.title = "Vendors"
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in rows:
        ws.append([r[h] for h in headers])
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for i, h in enumerate(headers, 1):
        width = max(len(h), *(len(str(r[h])) for r in rows))
        ws.column_dimensions[get_column_letter(i)].width = min(width + 2, 60)
    money_col = headers.index("Total Billed") + 1
    for row in ws.iter_rows(min_row=2, min_col=money_col, max_col=money_col):
        row[0].number_format = '"$"#,##0.00'
    wb.save(out)

    csv_path = os.path.splitext(out)[0] + ".csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} vendors to {out} and {csv_path}")


def main(argv: Iterable[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--group", default="AG", help="property group name (default: AG)")
    ap.add_argument("--name-prefix", help="select properties whose ShortName/Name starts with this")
    ap.add_argument("--property-ids", help="comma-separated PropertyIDs")
    ap.add_argument("--months", type=int, default=12, help="look-back window in months (default: 12)")
    ap.add_argument("--out", default="AG_vendor_contacts.xlsx")
    args = ap.parse_args(argv)

    rm = RentManager()
    props = select_properties(rm, args)
    if not props:
        sys.exit("No properties matched.")
    print(f"{len(props)} properties selected: "
          + ", ".join(sorted(_first(p.get("ShortName"), p.get("Name")) for p in props.values())))
    if len(props) != 18:
        print(f"Note: expected 18 AG properties, found {len(props)}. Check the selection.")

    since = dt.date.today() - dt.timedelta(days=round(args.months * 365.25 / 12))
    rows = build_rows(rm, props, since)
    if not rows:
        sys.exit(f"No vendor bills found for those properties since {since}.")
    write_outputs(rows, args.out)


if __name__ == "__main__":
    main()
