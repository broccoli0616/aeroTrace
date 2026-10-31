"""Export the supplied challenge4 workbook into the project's runtime TSV files.

The workbook is read as data, without evaluating formulas or running macros.
Run: python -m aerotrace.import_dataset data/challenge4_ad_index.xlsx
"""
import argparse
import csv
import hashlib
import json
import posixpath
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent.parent
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
SHEETS = {
    "nprm_index": ("ad_nprm.tsv", {"record_id", "docket_number", "summary", "federal_register_link"}),
    "revision_chains": ("revision_chains.tsv", {"record_id", "version", "versions_total", "status"}),
    "data_notes": ("data_notes.tsv", {"topic", "detail"}),
}


def read_workbook(path: str | Path) -> dict[str, tuple[list[str], list[dict], int]]:
    """Read the three known sheets, skipping descriptive rows before their headers."""
    tables = {}
    with ZipFile(path) as archive:
        strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            strings = ["".join(t.text or "" for t in element.findall(".//s:t", NS))
                       for element in ET.fromstring(archive.read("xl/sharedStrings.xml")).findall("s:si", NS)]
        targets = {r.attrib["Id"]: r.attrib["Target"]
                   for r in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))}
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        for sheet in workbook.findall("s:sheets/s:sheet", NS):
            name = sheet.attrib["name"]
            if name not in SHEETS:
                continue
            target = targets[sheet.attrib[f"{{{REL_NS}}}id"]]
            part = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
            xml = ET.fromstring(archive.read(part))
            header, records, header_row = None, [], None
            for row in xml.findall("s:sheetData/s:row", NS):
                cells = {}
                for cell in row.findall("s:c", NS):
                    column = "".join(c for c in cell.attrib["r"] if c.isalpha())
                    value = cell.find("s:v", NS)
                    text = value.text or "" if value is not None else ""
                    if cell.get("t") == "inlineStr":
                        text = "".join(t.text or "" for t in cell.findall(".//s:t", NS))
                    elif cell.get("t") == "s":
                        text = strings[int(text)]
                    cells[column] = text
                if header is None:
                    if SHEETS[name][1].issubset(cells.values()):
                        header = {column: value for column, value in cells.items() if value}
                        header_row = int(row.attrib["r"])
                    continue
                if any(cells.get(column, "") for column in header):
                    records.append({field: cells.get(column, "") for column, field in header.items()})
            if header is None:
                raise ValueError(f"Missing required header in worksheet {name}.")
            tables[name] = (list(header.values()), records, header_row)
    if set(tables) != set(SHEETS):
        raise ValueError(f"Missing worksheets: {', '.join(sorted(set(SHEETS) - set(tables)))}")
    ids = [r["record_id"] for r in tables["nprm_index"][1]]
    if not ids or any(not value for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("nprm_index must have unique, nonempty record_id values.")
    if any(r["record_id"] not in set(ids) for r in tables["revision_chains"][1]):
        raise ValueError("revision_chains references a record_id absent from nprm_index.")
    return tables


def export_workbook(path: str | Path, output_dir: str | Path) -> dict:
    path, output_dir = Path(path), Path(output_dir)
    tables = read_workbook(path)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"workbook": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "sheets": {}}
    for name, (fields, records, header_row) in tables.items():
        target = output_dir / SHEETS[name][0]
        temporary = target.with_suffix(".tsv.tmp")
        with temporary.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, delimiter="\t", lineterminator="\n")
            writer.writeheader()
            writer.writerows(records)
        temporary.replace(target)
        manifest["sheets"][name] = {"file": target.name, "records": len(records), "header_row": header_row,
                                   "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
    target = output_dir / "dataset.json"
    target.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", nargs="?", type=Path, default=ROOT / "data" / "challenge4_ad_index.xlsx")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    print(json.dumps(export_workbook(args.workbook, args.output_dir), indent=2))


if __name__ == "__main__":
    main()
