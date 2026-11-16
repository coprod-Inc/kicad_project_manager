"""Saved-schematic/CSV BOM import and deterministic USD component costing."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import io
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


class BomError(ValueError):
    pass


def normalized(value: str) -> str:
    return re.sub(r'[^a-z0-9]', '', value.lower().replace('${', '').replace('}', ''))


def part_number(value: str) -> str:
    text = value.strip().upper()
    if re.fullmatch(r'C[1-9][0-9]*', text):
        return text
    raise BomError('Use a JLCPCB part number such as C25804.')


def amount(value: object) -> Decimal:
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise BomError('Enter a non-negative USD amount, for example 13 or 1.30.') from None
    if not result.is_finite() or result < 0:
        raise BomError('Enter a finite, non-negative USD amount.')
    return result


def positive_integer(value: object) -> int:
    number = amount(value)
    if number != number.to_integral_value() or number < 1:
        raise BomError('Quantity must be a positive whole number.')
    return int(number)


@dataclass(frozen=True)
class AssemblyRow:
    references: tuple[str, ...]
    value: str
    footprint: str
    quantity: int


@dataclass(frozen=True)
class BomPart:
    key: str
    references: tuple[str, ...]
    value: str
    footprint: str
    code: str
    quantity: int
    warning: str = ''
    code_fields: tuple[str, ...] = ()
    assembly_rows: tuple[AssemblyRow, ...] = ()


@dataclass(frozen=True)
class PriceTier:
    minimum: int
    maximum: int | None
    price: Decimal


@dataclass(frozen=True)
class Supply:
    code: str
    stock: int | None
    tiers: tuple[PriceTier, ...]
    fetched_at: float
    minimum_order: int = 1
    library_type: str = 'Unknown'

    def unit_price(self, quantity: int) -> Decimal | None:
        return next((tier.price for tier in sorted(self.tiers, key=lambda t: t.minimum, reverse=True)
                     if quantity >= tier.minimum and (tier.maximum is None or quantity <= tier.maximum)), None)


@dataclass(frozen=True)
class CostLine:
    part: BomPart
    required: int
    purchase: int
    stock: int | None
    unit_price: Decimal | None
    total: Decimal | None
    source: str


def natural_key(value: str) -> tuple:
    """Keep component/reference identifiers in human order, e.g. R2 before R10."""
    return tuple((1, int(token)) if token.isdigit() else (0, token.casefold())
                 for token in re.split(r'(\d+)', value))


def sorted_indices(values: list[tuple], column: int, descending: bool = False) -> list[int]:
    """Sort raw typed cells; unavailable values stay last in either direction."""
    known = [index for index, row in enumerate(values) if row[column] is not None]
    missing = [index for index, row in enumerate(values) if row[column] is None]
    return sorted(known, key=lambda index: values[index][column], reverse=descending) + missing


def calculate(parts: list[BomPart], boards: int, supplies: dict[str, Supply],
              overrides: dict[str, dict]) -> list[CostLine]:
    """Cost required components; MOQ affects the unit-price tier and availability."""
    boards = positive_integer(boards)
    lines = []
    for part in parts:
        required = part.quantity * boards
        supply = supplies.get(part.code)
        override = overrides.get(part.key)
        purchase = required if override or not supply else max(required, supply.minimum_order)
        if override:
            price = amount(override['paid']) / positive_integer(override['quantity'])
            source = 'Manual' + (': ' + str(override.get('seller', '')) if override.get('seller') else '')
        else:
            price = supply.unit_price(purchase) if supply else None
            source = 'JLCPCB' if supply else ('Not fetched' if part.code else 'No part number')
        lines.append(CostLine(part, required, purchase, supply.stock if supply else None,
                              price, price * required if price is not None else None, source))
    return lines


def _is_code_field(name: str) -> bool:
    return bool(re.fullmatch(r'(?:lcsc|jlcpcb|jlc)(?:partnumber|partnum|partno|partnr|part|pn|number|code|id)?', normalized(name)))


_PART_FIELD = r'(?:partnumber|partnum|partno|partnr|partcode|partid|part|pn|number|code)'


def detect_lcsc(row: dict[str, str], headers: list[str]) -> tuple[str, str, tuple[str, ...]]:
    """Resolve each component's supplier fields without assuming a project-wide mapping."""
    candidates = []
    for header in headers:
        name = normalized(header)
        explicit = _is_code_field(header)
        stem = re.sub(r'\d+', '', name)
        generic = re.fullmatch(r'(?:(?:alt|alternate|alternative|secondary))?'
                               r'(?:(?:supplier|vendor))?' + _PART_FIELD, stem)
        if not explicit and not generic:
            continue
        value = str(row.get(header) or '').strip()
        if not value:
            continue
        tagged = False
        priority = 0
        if not explicit:
            prefix = re.sub(_PART_FIELD + r'$', '', stem)
            supplier_names = ({prefix} if prefix.endswith(('supplier', 'vendor')) else
                              {prefix + 'supplier', prefix + 'vendor'})
            indices = re.findall(r'\d+', name)
            suppliers = [str(row.get(other) or '').strip() for other in headers
                         if re.sub(r'\d+', '', normalized(other)) in supplier_names
                         and re.findall(r'\d+', normalized(other)) == indices
                         and str(row.get(other) or '').strip()]
            tagged = any(re.search(r'\b(?:lcsc|jlcpcb|jlc)\b', supplier, re.I) for supplier in suppliers)
            # A C-shaped DigiKey/manufacturer identifier is not an LCSC assignment.
            if suppliers and not tagged:
                continue
            # Alternate supplier slots describe substitute parts, not conflicting
            # assignments. Prefer the primary LCSC part, then the first LCSC alt.
            alternate = prefix.startswith(('alt', 'secondary')) or bool(indices)
            priority = 10 + (int(indices[0]) if indices else 1) if alternate else (0 if tagged else 1)
        try:
            code = part_number(value)
        except BomError:
            if explicit or tagged:
                candidates.append((priority, header, ''))
        else:
            candidates.append((priority, header, code))
    if not candidates:
        return '', '', ()
    priority = min(candidate[0] for candidate in candidates)
    selected = [(header, code) for rank, header, code in candidates if rank == priority]
    codes = {code for _, code in selected}
    fields = tuple(header for header, _ in selected)
    warning = ('Conflicting or invalid JLCPCB fields: ' + ', '.join(fields)
               if '' in codes or len(codes) > 1 else '')
    return (next(iter(codes)) if not warning else '', warning, fields)


def _is_test_point(reference: str, value: str, footprint: str) -> bool:
    return bool(re.match(r'TP\d', reference, re.I)
                or normalized(value).startswith(('testpoint', 'testpad'))
                or (':' in footprint and normalized(footprint.split(':', 1)[0]).endswith(
                    ('testpoint', 'testpoints', 'testpad', 'testpads')))
                or any(normalized(section).startswith(('testpoint', 'testpad'))
                       for section in footprint.split(':')))


def read_csv(text: str) -> list[BomPart]:
    """Accept KiCad CLI, JLCPCB, and ordinary grouped BOMs without rewriting them."""
    text = text.lstrip('\ufeff')
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=',;\t')
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = reader.fieldnames or []
    if len(headers) != len(set(headers)):
        raise BomError('The BOM contains duplicate column names.')
    names = {}
    for header in headers:
        names.setdefault(normalized(header), []).append(header)

    def get(row, *aliases):
        return next((str(row.get(header) or '').strip() for alias in aliases
                     for header in names.get(alias, []) if str(row.get(header) or '').strip()), '')

    if not any(name in names for name in ('reference', 'references', 'refs', 'designator', 'designators')):
        raise BomError('The CSV needs a Reference, Refs, or Designator column.')
    groups = {}
    seen = set()
    for number, row in enumerate(reader, 2):
        if None in row:
            raise BomError(f'BOM row {number} has more values than column names.')
        if all(not str(value or '').strip() for value in row.values()):
            continue
        if any(get(row, name).lower() in ('1', 'true', 'yes', 'y', 'dnp', 'excluded')
               for name in ('dnp', 'donotpopulate', 'excludefrombom')):
            continue
        # A CSV containing both columns must use Value, even when it is blank.
        value = get(row, 'value') if 'value' in names else get(row, 'comment')
        footprint = get(row, 'footprint', 'package')
        original_refs = tuple(r.strip() for r in re.split(r'[,;\s]+', get(row, 'reference', 'references', 'refs', 'designator', 'designators')) if r.strip())
        refs = tuple(ref for ref in original_refs if not _is_test_point(ref, value, footprint))
        if original_refs and not refs:
            continue
        if not refs or any(ref in seen for ref in refs) or len(set(refs)) != len(refs):
            raise BomError(f'BOM row {number} has missing or duplicate references.')
        seen.update(refs)
        raw_qty = get(row, 'quantity', 'qty', 'quantityperpcb')
        if not raw_qty and any('-' in ref for ref in refs):
            raise BomError(f'BOM row {number} uses reference ranges; add a Quantity column.')
        quantity = positive_integer(raw_qty) if raw_qty else len(refs)
        if len(refs) != len(original_refs) and raw_qty:
            if quantity != len(original_refs):
                raise BomError(f'BOM row {number} cannot exclude test points: Quantity must match the individual reference count.')
            quantity = len(refs)
        code, warning, code_fields = detect_lcsc(row, headers)
        manufacturer = get(row, 'manufacturer', 'manufacturername')
        mpn = get(row, 'mpn', 'manufacturerpartnumber', 'mfrpart')
        identity = '\0'.join((manufacturer, mpn, value, footprint))
        # Reannotation must not discard historical paid prices.
        key = 'jlc:' + code if code else 'part:' + hashlib.sha256(identity.encode()).hexdigest()[:24]
        assembly = AssemblyRow(refs, value, footprint, quantity)
        if key in groups:
            old = groups[key]
            groups[key] = BomPart(key, old.references + refs, old.value, old.footprint,
                                  code, old.quantity + quantity, old.warning or warning,
                                  tuple(dict.fromkeys(old.code_fields + code_fields)),
                                  old.assembly_rows + (assembly,))
        else:
            groups[key] = BomPart(key, refs, value, footprint, code, quantity, warning, code_fields, (assembly,))
    if not groups:
        raise BomError('No populated BOM components remain after excluding DNP parts, excluded parts and test points.')
    return list(groups.values())


def jlc_bom_csv(parts: list[BomPart]) -> str:
    """JLCPCB assembly BOM for one board, independent of purchase quantities/prices.

    Format: https://jlcpcb.com/help/article/bill-of-materials-for-pcb-assembly
    Keep uncoded parts for manual matching; never guess or omit an assignment.
    """
    if not parts:
        raise BomError('Load a populated BOM before exporting.')
    groups, seen = {}, set()
    for part in parts:
        if part.warning:
            raise BomError(f'{", ".join(part.references)}: {part.warning}. Correct the source BOM and reload before exporting.')
        code = part_number(part.code) if part.code else ''
        rows = part.assembly_rows or (AssemblyRow(part.references, part.value, part.footprint, part.quantity),)
        for row in rows:
            refs = row.references
            if (row.quantity != len(refs) or not refs
                    or any(re.search(r'\d-.*\d', ref) or re.search(r'[\s,;]', ref) for ref in refs)):
                raise BomError('Assembly export needs individual references with quantity matching their count. Re-export the source BOM without reference ranges.')
            for ref in refs:
                if ref.upper() in seen:
                    raise BomError(f'Duplicate assembly designator {ref}; JLCPCB treats designators as uppercase.')
                seen.add(ref.upper())
            value = row.value
            footprint = row.footprint.rsplit(':', 1)[-1].strip()
            if not value or not footprint:
                raise BomError(f'{", ".join(refs)} needs a Value/Comment and Footprint before assembly export.')
            key = (value, footprint, code, part.key if not code else '')
            groups.setdefault(key, []).extend(refs)
    output = io.StringIO(newline='')
    writer = csv.writer(output, lineterminator='\r\n')
    writer.writerow(('Comment', 'Designator', 'Footprint', 'Quantity', 'LCSC Part #'))
    for (value, footprint, code, _), refs in sorted(groups.items(), key=lambda item: min(natural_key(ref) for ref in item[1])):
        writer.writerow((value, ','.join(sorted(refs, key=natural_key)), footprint, len(refs), code))
    return output.getvalue()


def kicad_cli() -> str:
    executable = shutil.which('kicad-cli')
    if executable:
        return executable
    if sys.platform == 'darwin':
        candidate = Path('/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli')
        if candidate.is_file():
            return str(candidate)
    # Windows plugins commonly inherit the bundled interpreter's directory.
    for folder in (Path(sys.executable).parent, Path(sys.executable).parent.parent / 'bin'):
        for name in ('kicad-cli.exe', 'kicad-cli'):
            if (folder / name).is_file():
                return str(folder / name)
    raise BomError('kicad-cli was not found. Add KiCad’s bin directory to PATH or import a BOM CSV.')


def load_bom(source: Path) -> list[BomPart]:
    if source.suffix.lower() != '.kicad_sch':
        return read_csv(source.read_text('utf-8-sig'))
    with tempfile.TemporaryDirectory(prefix='kpm-bom-') as folder:
        output = Path(folder) / 'bom.csv'
        try:
            result = subprocess.run([
                kicad_cli(), 'sch', 'export', 'bom', '--fields',
                'Reference,Value,Footprint,QUANTITY,DNP,EXCLUDE_FROM_BOM,*',
                '--exclude-dnp', '--ref-range-delimiter', '', '--output', str(output), str(source),
            ], capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            raise BomError('KiCad BOM export timed out. Try importing a BOM CSV.') from None
        if result.returncode or not output.is_file():
            raise BomError('KiCad could not export this saved schematic. Save it in KiCad or import a BOM CSV.')
        return read_csv(output.read_text('utf-8-sig'))
