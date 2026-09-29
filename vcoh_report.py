# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx>=0.27", "openpyxl>=3.1"]
# ///
"""Community-owned home (V-COH) rehab status for one community.

Rebuilds what the "CPCM V-COH Inventory" Report Writer report shows (Rent Manager's
API can't run Report Writer reports) and adds rehab spend:

  * every home asset for the community (asset tag starts with the community code),
    its status, purchase price and sale price
  * the open "Make Ready" service issue for the home: checklist progress, current
    step and the latest dated note
  * bills posted to the home, either tagged to the home's unit or naming its lot
    ("Lot 243") in the bill comment, split into acquisition, rehab and utilities

Uses the same RM_* environment variables as vendor_report.py (set RM_TOKEN_FILE to
reuse one Rent Manager session between runs).

    uv run vcoh_report.py --community CDMH --out CDMH_VCOH.xlsx
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
from collections import defaultdict

from vendor_report import RentManager

REHAB_STATUSES = {"Make Ready", "Delivered", "Ready"}
LOT_RE = re.compile(r"\blot\s*#?\s*(\d+)\b", re.I)
# Looser references ("#403 construction", "unit 218", "824 carpet removal"); only
# trusted when the number is one of the community's lots.
LOOSE_LOT_RE = re.compile(r"(?:#\s*|\bunit\s+)(\d{1,4})\b|^\s*(\d{1,4})\s+(?=[a-z])", re.I)
NOTE_DATE_RE = re.compile(r"^\s*(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\b", re.M)


def lot_of(text: str) -> str | None:
    m = LOT_RE.search(text or "") or re.search(r"-\s*(\d+)\s*$", text or "")
    return m.group(1).lstrip("0") if m else None


def comment_lot(text: str, lots: set) -> str | None:
    lot = lot_of_comment(text)
    if lot in lots:
        return lot
    for m in LOOSE_LOT_RE.finditer(text or ""):
        num = (m.group(1) or m.group(2)).lstrip("0")
        if num in lots:
            return num
    return None


def lot_of_comment(text: str) -> str | None:
    m = LOT_RE.search(text or "")
    return m.group(1).lstrip("0") if m else None


def cost_bucket(gl: dict) -> str:
    # 1775 (Community Owned Homes) holds capitalized rehab and site prep, not the
    # purchase price, which comes from the asset record.
    ref, name = str(gl.get("Reference", "")), str(gl.get("Name", ""))
    if ref.startswith("57"):
        return "Utilities"
    if ref == "1775" or "COH" in name or ref.startswith(("56", "59", "60")):
        return "Rehab / Make Ready"
    return "Other"


def checklist_summary(issue: dict) -> tuple[int, int, str]:
    items = sorted(issue.get("CheckListItems") or [], key=lambda c: c.get("SortOrder", 0))
    # The first item is often just the checklist's heading; skip it when it has no step number.
    items = [c for c in items if re.match(r"\s*(MAKE READY|\d+\))", c.get("Description", ""), re.I)]
    done = sum(1 for c in items if c.get("IsClosed"))
    nxt = next((c for c in items if not c.get("IsClosed")), None)
    step = ""
    if nxt:
        step = nxt["Description"].strip().splitlines()[-1 if nxt["Description"].upper().startswith("MAKE READY") else 0]
    return done, len(items), step.strip()


def latest_note(issue: dict) -> tuple[str, str]:
    text = (issue.get("Description") or "").replace("\r\n", "\n")
    marks = list(NOTE_DATE_RE.finditer(text))
    if not marks:
        return "", " ".join(text.split())[:300]
    last = marks[-1]
    return last.group(1), " ".join(text[last.end():].split())[:300]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--community", default="CDMH", help="community code / asset tag prefix (default CDMH)")
    ap.add_argument("--since", default="2025-01-01", help="earliest bill date to include")
    ap.add_argument("--out", default="VCOH_rehab_status.xlsx")
    args = ap.parse_args()
    code = args.community.upper()

    rm = RentManager()
    props = {p["PropertyID"]: p for p in rm.get_all("/Properties")}
    community = next(p for p in props.values() if str(p.get("ShortName", "")).upper() == code)

    assets = [
        a for a in rm.get_all("/Assets", embeds="UserDefinedValues,AssetStatus,AssetType")
        if str(a.get("AssetTag", "")).upper().replace(" ", "").startswith(code)
    ]
    homes = {a["FinancialUnitID"]: a for a in assets}
    home_props = {a["FinancialPropertyID"] for a in assets} | {community["PropertyID"]}
    lot_to_unit = {
        lot_of(a["AssetTag"]): u for u, a in homes.items() if "unknown" not in a["AssetTag"].lower()
    }
    lot_to_unit.pop(None, None)

    issues = rm.get_all(
        "/ServiceManagerIssues",
        filters="Units.UnitID,in,(%s)" % ",".join(map(str, homes)),
        embeds="Category,Status,ChecklistItems,Units",
    )
    issue_by_unit: dict[int, dict] = {}
    for i in sorted(issues, key=lambda i: i.get("CreateDate", "")):
        if "make ready" in i.get("Title", "").lower() or i.get("Category", {}).get("Name") == "INVENTORY":
            for u in i.get("Units") or []:
                issue_by_unit[u["UnitID"]] = i  # latest one wins

    gl = {g["GLAccountID"]: g for g in rm.get_all("/GLAccounts")}
    vendors = {v["VendorID"]: v.get("Name", "") for v in rm.get_all("/Vendors", fields="VendorID,Name")}
    bills = rm.get_all("/Bills", filters=f"TransactionDate,ge,{args.since}", embeds="BillDetails")

    spend: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    cost_rows, unassigned = [], []
    for b in bills:
        for d in b.get("BillDetails") or []:
            if d.get("PropertyID") not in home_props:
                continue
            g = gl.get(d.get("GLAccountID"), {})
            bucket = cost_bucket(g)
            comment = d.get("Comment") or b.get("Comment") or ""
            unit, how = None, ""
            if d.get("UnitID") in homes:
                unit, how = d["UnitID"], "Tagged to home"
            elif bucket != "Other" and (lot := comment_lot(comment, set(lot_to_unit))):
                unit, how = lot_to_unit[lot], "Lot named in comment"
            row = {
                "Date": str(b.get("TransactionDate", ""))[:10],
                "Vendor": vendors.get(b.get("AccountID"), ""),
                "GL": f'{g.get("Reference", "")} {g.get("Name", "")}'.strip(),
                "Bucket": bucket,
                "Amount": round(float(d.get("Amount") or 0), 2),
                "Comment": " ".join(comment.split()),
                "Books": props.get(d["PropertyID"], {}).get("ShortName", ""),
            }
            if unit is not None:
                spend[unit][bucket] += row["Amount"]
                spend[unit][how] += row["Amount"]
                cost_rows.append({"Home": homes[unit]["AssetTag"], "Matched By": how, **row})
            elif bucket == "Rehab / Make Ready" and "COH" in row["GL"]:
                unassigned.append(row)

    today = dt.date.today()
    status_rows = []
    for unit, a in homes.items():
        status = (a.get("AssetStatus") or {}).get("Name") or ""
        if status not in REHAB_STATUSES:
            continue
        udf = {u["Name"]: u.get("Value") for u in a.get("UserDefinedValues") or []}
        issue = issue_by_unit.get(unit)
        done, total, step = checklist_summary(issue) if issue else (0, 0, "")
        note_date, note = latest_note(issue) if issue else ("", "")
        s = spend.get(unit, {})
        opened = str(issue.get("CreateDate", ""))[:10] if issue else ""
        status_rows.append({
            "Home": a["AssetTag"],
            "Asset Status": status,
            "Type": "New" if issue and "new coh" in issue.get("Title", "").lower() else ("Used" if issue else ""),
            "Model": a.get("ModelNumber", ""),
            "Year": udf.get("Manufactured Year", ""),
            "Size": a.get("Dimensions", ""),
            "Phase": udf.get("Phase", ""),
            "Purchase Date": str(a.get("PurchaseDate", ""))[:10],
            "Purchase Price": a.get("PurchasePrice") or 0,
            "Rehab Spend": round(s.get("Rehab / Make Ready", 0), 2),
            "Utilities": round(s.get("Utilities", 0), 2),
            "Total Invested": round((a.get("PurchasePrice") or 0) + s.get("Rehab / Make Ready", 0)
                                    + s.get("Utilities", 0), 2),
            "List / Sale Price": a.get("SalePrice") or 0,
            "Make Ready SI": issue.get("ServiceManagerIssueID") if issue else "NONE",
            "SI Opened": opened,
            "Days Open": (today - dt.date.fromisoformat(opened)).days if opened else "",
            "Checklist": f"{done}/{total}" if total else "",
            "% Complete": round(done / total, 2) if total else "",
            "Current Step": step,
            "Target Completion": udf.get("Expected Close Date") or udf.get("Delivery Date") or "Not recorded",
            "Last Note Date": note_date,
            "Last Note": note,
        })
    status_order = {"Make Ready": 0, "Delivered": 1, "Ready": 2}
    status_rows.sort(key=lambda r: (status_order[r["Asset Status"]], -(r["% Complete"] or 0), r["Home"]))
    cost_rows.sort(key=lambda r: (r["Home"], r["Date"]))
    write(args.out, community, status_rows, cost_rows, unassigned)


def write(out, community, status_rows, cost_rows, unassigned) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font
    from openpyxl.utils import get_column_letter

    money = {"Purchase Price", "Rehab Spend", "Utilities",
             "Total Invested", "List / Sale Price", "Amount"}
    wb = Workbook()
    sheets = [("Rehab Status", status_rows), ("Cost Detail", cost_rows),
              ("Unassigned COH Costs", unassigned)]
    for n, (title, rows) in enumerate(sheets):
        ws = wb.active if n == 0 else wb.create_sheet()
        ws.title = title
        if not rows:
            ws.append(["No rows"])
            continue
        headers = list(rows[0])
        ws.append(headers)
        for c in ws[1]:
            c.font = Font(bold=True)
        for r in rows:
            ws.append([r[h] for h in headers])
        ws.freeze_panes = "B2"
        ws.auto_filter.ref = ws.dimensions
        for i, h in enumerate(headers, 1):
            col = get_column_letter(i)
            width = max(len(h), *(len(str(r[h])) for r in rows))
            ws.column_dimensions[col].width = min(width + 2, 70 if h in ("Last Note", "Comment", "Current Step") else 28)
            for cell in ws[col][1:]:
                if h in money:
                    cell.number_format = '"$"#,##0.00'
                elif h == "% Complete":
                    cell.number_format = "0%"
                if h in ("Last Note", "Current Step"):
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
    wb.save(out)
    print(f"{community.get('Name')}: {len(status_rows)} homes in rehab pipeline, "
          f"{len(cost_rows)} matched cost lines, {len(unassigned)} unassigned COH lines -> {out}")


if __name__ == "__main__":
    main()
