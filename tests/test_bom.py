from decimal import Decimal
import csv
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from project_manager.bom import BomError, BomPart, PriceTier, Supply, calculate, jlc_bom_csv, load_bom, natural_key, read_csv, sorted_indices
from project_manager.bom_jlc import DETAIL_URL, fetch_supply, parse_supply
from project_manager.bom_store import SupplyCache, load_state, save_state


def response(code='C25804'):
    return {'code': 200, 'data': {
        'componentCode': code, 'stockCount': 100, 'overseasStockCount': 80,
        'prices': [{'startNumber': 1, 'endNumber': 9, 'productPrice': '0.0022'},
                   {'startNumber': 10, 'endNumber': -1, 'productPrice': '0.00175'}],
        'jlcPrices': None, 'minPurchaseNum': 1, 'componentLibraryType': 'base',
    }}


class BomTests(unittest.TestCase):
    def test_mixed_supplier_fields_prefer_primary_over_substitute_per_component(self):
        parts = read_csv('Reference,Value,Footprint,Supplier,Supplier Part Number,Alt Supplier 1,Alt Supplier Part Number 1\n'
                         'C1,100n,0603,Digikey,ABC-ND,LCSC,C111\n'
                         'U1,IC,SO8,LCSC,C222,LCSC,C333\n')
        self.assertEqual([part.code for part in parts], ['C111', 'C222'])
        self.assertEqual(parts[0].code_fields, ('Alt Supplier Part Number 1',))
        self.assertEqual(parts[1].code_fields, ('Supplier Part Number',))
        self.assertFalse(any(part.warning for part in parts))

    def test_numbered_supplier_slots_choose_first_lcsc_alternative(self):
        part = read_csv('Reference,Value,Supplier,Supplier Part Number,Alt Supplier 1,Alt Supplier Part Number 1,Alt Supplier 2,Alt Supplier Part Number 2\n'
                        'U1,IC,Mouser,123-ABC,Digikey,C111,LCSC,C222\n')[0]
        self.assertEqual(part.code, 'C222')
        part = read_csv('Reference,Value,Alt Supplier 2,Alt Supplier Part Number 2,Alt Supplier 1,Alt Supplier Part Number 1\n'
                        'U1,IC,LCSC,C222,LCSC,C111\n')[0]
        self.assertEqual(part.code, 'C111')

    def test_generic_primary_and_alternate_part_fields_are_detected_without_supplier_labels(self):
        for field in ('Part Number', 'Alt Part Number', 'Alternate Part Number 1', 'Supplier Part Number', 'Alt PN 2'):
            with self.subTest(field=field):
                part = read_csv(f'Reference,Value,{field}\nR1,10k, c25804 \n')[0]
                self.assertEqual(part.code, 'C25804')
                self.assertEqual(part.code_fields, (field,))

    def test_identifiers_and_non_lcsc_suppliers_are_not_guessed_as_lcsc(self):
        part = read_csv('Reference,Value,MPN,IPN,Manufacturer Part Number,Supplier,Supplier Part Number\n'
                        'C123,C456,C789,C111,C222,Digikey,C333\n')[0]
        self.assertEqual(part.code, '')
        self.assertFalse(part.warning)

    def test_invalid_selected_supplier_and_equal_priority_conflicts_are_reported(self):
        for text in (
            'Reference,Value,Supplier,Supplier Part Number,Alt Supplier 1,Alt Supplier Part Number 1\nU1,IC,LCSC,bad,LCSC,C222\n',
            'Reference,Value,LCSC,Supplier,Supplier Part Number\nU1,IC,C111,LCSC,C222\n',
        ):
            with self.subTest(text=text):
                part = read_csv(text)[0]
                self.assertEqual(part.code, '')
                self.assertTrue(part.warning)

    def test_jlc_export_has_exact_headers_and_individual_per_board_designators(self):
        parts = read_csv('Reference,Value,Footprint,LCSC,DNP,EXCLUDE_FROM_BOM\n'
                         'R10,10k,Resistor_SMD:R_0603,C25804,,\n'
                         'R2,10k,Resistor_SMD:R_0603,C25804,,\n'
                         'R3,10k,Resistor_SMD:R_0603,C25804,yes,\n'
                         'R4,10k,Resistor_SMD:R_0603,C25804,,1\n')
        calculate(parts, 100, {}, {})
        exported = jlc_bom_csv(parts)
        rows = list(csv.reader(io.StringIO(exported)))
        self.assertEqual(rows, [['Comment', 'Designator', 'Footprint', 'Quantity', 'LCSC Part #'],
                                ['10k', 'R2,R10', 'R_0603', '2', 'C25804']])
        self.assertIn('\r\n', exported)
        self.assertEqual(read_csv(exported)[0].quantity, 2)

    def test_test_points_are_excluded_before_cost_grouping_and_export(self):
        parts = read_csv('Reference,Value,Footprint,LCSC\n'
                         'R1,10k,0603,C25804\n'
                         'TP1,10k,0603,C25804\n'
                         'tp2,Pad,Custom:Pad,C111\n'
                         'X1,Test_Point,Custom:Pad,C222\n'
                         'X2,Probe,TestPoint:TestPoint_Pad,C333\n'
                         'X3,Probe,Custom:TestPoint_Pad,C444\n'
                         'X4,Clock probe,#example - Testpoints:Pad,C555\n')
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].references, ('R1',))
        self.assertEqual(parts[0].quantity, 1)
        self.assertEqual(calculate(parts, 5, {}, {})[0].required, 5)
        rows = list(csv.DictReader(io.StringIO(jlc_bom_csv(parts))))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['Designator'], rows[0]['Quantity']), ('R1', '1'))

    def test_mixed_grouped_rows_remove_only_test_points_and_adjust_quantity(self):
        for text in (
            'Reference,Value,Footprint,Quantity\n"R1,TP1,R2",10k,0603,3\n',
            'Reference,Value,Footprint\n"R1,TP1,R2",10k,0603\n',
        ):
            with self.subTest(text=text):
                part = read_csv(text)[0]
                self.assertEqual(part.references, ('R1', 'R2'))
                self.assertEqual(part.quantity, 2)
        with self.assertRaisesRegex(BomError, 'Quantity must match'):
            read_csv('Reference,Value,Footprint,Quantity\n"R1,TP1,R2",10k,0603,10\n')

    def test_export_comment_uses_value_even_when_comment_and_mpn_are_also_present(self):
        parts = read_csv('Reference,Value,Comment,MPN,Footprint,LCSC\n'
                         'R1,"10k  1%",Purchase note,ABC,Library:R_0603,C25804\n')
        row = list(csv.DictReader(io.StringIO(jlc_bom_csv(parts))))[0]
        self.assertEqual(row['Comment'], '10k  1%')
        self.assertEqual(row['Quantity'], '1')
        with self.assertRaisesRegex(BomError, 'Value/Comment'):
            jlc_bom_csv(read_csv('Reference,Value,Comment,Footprint\nR1,,Purchase note,0603\n'))

    def test_export_keeps_mixed_footprints_and_values_when_costing_aggregates_by_code(self):
        parts = read_csv('Reference,Value,Footprint,LCSC\n'
                         'R1,10k,Library:A,C25804\nR2,10K,Library:B,C25804\n')
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].quantity, 2)
        rows = list(csv.DictReader(io.StringIO(jlc_bom_csv(parts))))
        self.assertEqual([(r['Designator'], r['Comment'], r['Footprint']) for r in rows],
                         [('R1', '10k', 'A'), ('R2', '10K', 'B')])

    def test_export_quotes_fields_and_includes_parts_without_lcsc_for_manual_matching(self):
        parts = read_csv('Reference,Value,Footprint,MPN\nQ1,"FET, 60V",Package:SO8,ABC\nQ2,"FET, 60V",Package:SO8,DEF\n')
        rows = list(csv.DictReader(io.StringIO(jlc_bom_csv(parts))))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r['Comment'] == 'FET, 60V' and r['LCSC Part #'] == '' for r in rows))

    def test_export_rejects_ambiguous_codes_ranges_quantity_mismatches_and_case_duplicates(self):
        for text in (
            'Reference,Value,Footprint,LCSC,JLCPCB\nU1,IC,SO8,C123,C456\n',
            'Reference,Value,Footprint,Quantity\nR1-R10,10k,0603,10\n',
            'Reference,Value,Footprint,Quantity\nR1,10k,0603,10\n',
            'Reference,Value,Footprint\nR1,10k,0603\nr1,10k,0603\n',
            'Reference,Value,Footprint\nR1,10k,\n',
        ):
            with self.subTest(text=text), self.assertRaises(BomError):
                jlc_bom_csv(read_csv(text))

    def test_schematic_import_exports_all_fields_without_editing_the_source(self):
        with TemporaryDirectory() as folder:
            source = Path(folder) / 'board.kicad_sch'
            source.write_text('(kicad_sch)')
            before = source.read_bytes()

            def export(arguments, **kwargs):
                self.assertIn('--exclude-dnp', arguments)
                self.assertIn('Reference,Value,Footprint,QUANTITY,DNP,EXCLUDE_FROM_BOM,*', arguments)
                Path(arguments[arguments.index('--output') + 1]).write_text(
                    'Reference,Value,Footprint,Supplier,Supplier Part Number\nU1,IC,SO8,LCSC,C123\n')
                from subprocess import CompletedProcess
                return CompletedProcess(arguments, 0)

            with patch('project_manager.bom.kicad_cli', return_value='kicad-cli'), \
                 patch('project_manager.bom.subprocess.run', side_effect=export):
                self.assertEqual(load_bom(source)[0].code, 'C123')
            self.assertEqual(source.read_bytes(), before)

    def test_jlc_csv_and_schematic_aliases_aggregate_before_tier_selection(self):
        csv = ('Designator,Value,Footprint,Quantity,LCSC Part #,DNP\n'
               'R1,10k,0603,1,C25804,\nR2,10k,0603,1,C25804,\n'
               'R3,10k,0603,1,C25804,yes\n')
        parts = read_csv(csv)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].references, ('R1', 'R2'))
        supply = parse_supply('C25804', response())
        line = calculate(parts, 5, {'C25804': supply}, {})[0]
        self.assertEqual(line.required, 10)
        self.assertEqual(line.unit_price, Decimal('0.00175'))
        self.assertEqual(line.total, Decimal('0.01750'))
        self.assertEqual(supply.stock, 80)

    def test_excluded_flags_dnp_and_generic_supplier_metadata(self):
        parts = read_csv('Refs,Value,${QUANTITY},${EXCLUDE_FROM_BOM},JLCPCB Part Number,JLCPCB Rotation\n'
                         'R1,10k,1,,C25804,90\nR2,20k,1,1,C123,\n')
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].code, 'C25804')

    def test_conflicting_aliases_are_not_silently_priced(self):
        part = read_csv('Reference,Value,LCSC,JLCPCB\nU1,IC,C123,C456\n')[0]
        self.assertEqual(part.code, '')
        self.assertTrue(part.warning)
        self.assertIsNone(calculate([part], 1, {}, {})[0].total)

    def test_missing_prices_remain_unknown_and_zero_manual_price_is_valid(self):
        part = read_csv('Reference,Value,Footprint\nQ1,MOSFET,SO8\n')[0]
        self.assertIsNone(calculate([part], 1, {}, {})[0].total)
        line = calculate([part], 3, {}, {part.key: {'paid': '0', 'quantity': 1}})[0]
        self.assertEqual(line.total, Decimal(0))

    def test_manual_price_survives_reannotation_refresh_restart_and_cache_loss(self):
        part = read_csv('Reference,Value,Footprint\nQ1,MOSFET,SO8\n')[0]
        reannotated = read_csv('Reference,Value,Footprint\nQ99,MOSFET,SO8\n')[0]
        self.assertEqual(part.key, reannotated.key)
        with TemporaryDirectory() as folder:
            root = Path(folder)
            state = load_state(root)
            state['overrides'][part.key] = {'paid': '13', 'quantity': 10, 'currency': 'USD', 'seller': 'External'}
            save_state(root, state)
            before = (root / 'kpm-bom.json').read_bytes()
            cache = SupplyCache(root / 'user-data')
            supply = parse_supply('C25804', response())
            cache.write(supply)
            self.assertEqual(cache.read('C25804'), supply)
            (cache.root / 'C25804.json').unlink()
            self.assertIsNone(cache.read('C25804'))
            line = calculate([reannotated], 20, {}, load_state(root)['overrides'])[0]
            self.assertEqual(line.total, Decimal('26.0'))
            self.assertEqual((root / 'kpm-bom.json').read_bytes(), before)

    def test_moq_selects_unit_price_tier_but_total_counts_only_required_components(self):
        part = read_csv('Reference,Value,LCSC\nR1,10k,C25804\n')[0]
        data = response()
        data['data']['minPurchaseNum'] = 10
        line = calculate([part], 1, {'C25804': parse_supply('C25804', data)}, {})[0]
        self.assertEqual((line.required, line.purchase), (1, 10))
        self.assertEqual(line.unit_price, Decimal('0.00175'))
        self.assertEqual(line.total, Decimal('0.00175'))

    def test_reel_moq_does_not_inflate_board_cost_at_different_batch_sizes(self):
        parts = read_csv('Reference,Value,Footprint,LCSC\n"R1,R2",10k,0603,C25804\n')
        supply = Supply('C25804', 10000, (PriceTier(5000, None, Decimal('0.0018')),), 0, minimum_order=5000)
        for boards, required, purchase in ((1, 2, 5000), (5, 10, 5000), (3000, 6000, 6000)):
            with self.subTest(boards=boards):
                line = calculate(parts, boards, {'C25804': supply}, {})[0]
                self.assertEqual((line.required, line.purchase), (required, purchase))
                self.assertEqual(line.total, Decimal('0.0018') * required)
                self.assertEqual(line.total / boards, Decimal('0.0036'))

    def test_invalid_quantities_duplicate_references_and_ranges_rejected(self):
        for text in ('Reference,Quantity\nR1,0\n', 'Reference,Quantity\nR1,1.5\n',
                     'Reference,Quantity\nR1,1\nR1,1\n', 'Reference,Value\nR1-R10,10k\n'):
            with self.subTest(text=text), self.assertRaises(BomError):
                read_csv(text)

    def test_corrupted_persistent_state_is_not_silently_reset(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'kpm-bom.json'
            for text in ('{bad json', '{"version":2}',
                         '{"version":1,"overrides":{"x":{"paid":"NaN","quantity":1,"currency":"USD"}}}'):
                path.write_text(text)
                with self.assertRaises(BomError):
                    load_state(root)
                self.assertEqual(path.read_text(), text)

    def test_jlc_parser_rejects_mismatched_ids_and_invalid_tiers(self):
        data = response('C123')
        with self.assertRaises(BomError):
            parse_supply('C25804', data)
        data = response()
        data['data']['prices'][0]['productPrice'] = 'NaN'
        with self.assertRaises(BomError):
            parse_supply('C25804', data)

    def test_empty_prices_keep_stock_and_never_invent_cost(self):
        data = response()
        data['data']['prices'] = []
        supply = parse_supply('C25804', data)
        self.assertEqual(supply.stock, 80)
        self.assertIsNone(supply.unit_price(10))

    def test_jlc_frontend_price_list_preferred_and_raw_precision_preserved(self):
        data = response()
        data['data']['priceList'] = [{'startNumber': 1, 'endNumber': -1, 'productPrice': '0.0001234567'}]
        supply = parse_supply('C25804', data)
        self.assertEqual(supply.unit_price(50000), Decimal('0.0001234567'))

    def test_client_only_calls_jlcpcb_and_timeout_does_not_make_up_price(self):
        with patch('project_manager.bom_jlc.urllib.request.urlopen', side_effect=TimeoutError) as request:
            with self.assertRaises(BomError):
                fetch_supply('C25804')
            self.assertEqual(request.call_args.args[0].full_url, DETAIL_URL + 'C25804')
            self.assertEqual(request.call_args.kwargs['timeout'], 10)

    def test_sort_prices_numerically_and_keep_unpriced_rows_last_in_both_orders(self):
        cells = [(Decimal('9.5'),), (Decimal('100'),), (None,), (Decimal('2'),), (Decimal('0'),)]
        self.assertEqual(sorted_indices(cells, 0), [4, 3, 0, 1, 2])
        self.assertEqual(sorted_indices(cells, 0, True), [1, 0, 3, 4, 2])

    def test_sort_identifiers_naturally_and_dates_chronologically(self):
        identifiers = [(natural_key(value),) for value in ('C100', 'C2', 'C11')]
        self.assertEqual(sorted_indices(identifiers, 0), [1, 2, 0])
        references = [(natural_key(value),) for value in ('R10', 'R2', 'R1')]
        self.assertEqual(sorted_indices(references, 0), [2, 1, 0])
        dates = [(200,), (None,), (100,)]
        self.assertEqual(sorted_indices(dates, 0, True), [0, 2, 1])

    def test_library_classification_is_reported_not_guessed(self):
        for raw, preferred, expected in (('base', None, 'Basic'), ('expand', None, 'Extended'),
                                          ('expand', True, 'Preferred'), ('base', True, 'Basic'),
                                          (None, None, 'Unknown'), ('future-type', None, 'Unknown')):
            with self.subTest(raw=raw, preferred=preferred):
                data = response()
                data['data']['componentLibraryType'] = raw
                data['data']['preferredComponentFlag'] = preferred
                self.assertEqual(parse_supply('C25804', data).library_type, expected)

    def test_library_type_cache_roundtrip_and_old_cache_keeps_its_prices(self):
        with TemporaryDirectory() as folder:
            cache = SupplyCache(Path(folder))
            supply = parse_supply('C25804', response())
            cache.write(supply)
            self.assertEqual(cache.read('C25804').library_type, 'Basic')
            path = cache.root / 'C25804.json'
            old = json.loads(path.read_text())
            old.pop('library_type')
            path.write_text(json.dumps(old))
            restored = cache.read('C25804')
            self.assertEqual(restored.library_type, 'Unknown')
            self.assertEqual(restored.tiers, supply.tiers)


if __name__ == '__main__':
    unittest.main()
