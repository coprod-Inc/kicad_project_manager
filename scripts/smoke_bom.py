"""Exercise BOM quantity changes, paid-price dialogs, stock refresh and project isolation."""
from __future__ import annotations

import argparse
import csv
from decimal import Decimal
import faulthandler
from pathlib import Path
import threading
import time
import traceback
from tempfile import TemporaryDirectory
from unittest.mock import patch

import wx

from project_manager.bom import BomError, PriceTier, Supply
from project_manager.bom_store import load_state, save_state
from project_manager.bom_ui import COLUMNS, COLUMN_INDEX, PriceDialog
from project_manager.context import ProjectContext
from project_manager.ui import ProjectManagerFrame
from scripts.capture_screenshots import capture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('.dev/bom-stock.png'))
    args = parser.parse_args()
    app = wx.App(False)
    failures, requests = [], []
    mode = {'stock': 100, 'fail': False, 'wait': False}
    release = threading.Event()

    def supplier(code):
        requests.append(code)
        if mode['wait']:
            release.wait(3)
        if mode['fail']:
            raise BomError('Synthetic network interruption')
        return Supply(code, mode['stock'], (PriceTier(1, 9, Decimal('0.01')),
                                           PriceTier(10, None, Decimal('0.005'))), time.time(), library_type='Basic')

    with TemporaryDirectory(prefix='kpm-bom-smoke-') as folder:
        root = Path(folder)
        project = root / 'project'
        project.mkdir()
        (project / 'sample.kicad_pro').write_text('{}')
        (project / 'sample.kicad_pcb').write_text('(kicad_pcb)')
        (project / 'bom.csv').write_text('Reference,Value,Footprint,Supplier,Supplier Part Number,Alt Supplier 1,Alt Supplier Part Number 1\n'
                                        'R1,10k,0603,Digikey,ABC-ND,LCSC,C25804\n'
                                        'R2,10k,0603,LCSC,C25804,,\n'
                                        'Q1,MOSFET,SO8,,,,\n')
        save_state(project, {'version': 1, 'boards': 1, 'source': 'bom.csv', 'overrides': {}})
        context = ProjectContext('10.0.7', 'sample', str(project), str(project / 'sample.kicad_pro'),
                                 str(project / 'sample.kicad_pcb'), True, True, False)
        frame = ProjectManagerFrame(lambda: context, root / 'settings')
        frame.SetSize((1280, 820))
        frame.Show()
        panel = frame.bom_panel
        phase, started = 0, time.monotonic()

        def check_export():
            target = project / 'sample-jlcpcb-bom.csv'
            original = (project / 'bom.csv').read_bytes()

            class SaveDialog:
                def __init__(self, *args, **kwargs):
                    assert kwargs['defaultFile'] == 'sample-jlcpcb-bom.csv'
                    assert kwargs['style'] & wx.FD_OVERWRITE_PROMPT

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

                def ShowModal(self):
                    return wx.ID_OK

                def GetPath(self):
                    return str(target)

            before = len(requests)
            with patch('project_manager.bom_ui.wx.FileDialog', SaveDialog):
                event = wx.CommandEvent(wx.EVT_BUTTON.typeId, panel.export_button.GetId())
                panel.export_button.GetEventHandler().ProcessEvent(event)
            rows = list(csv.DictReader(target.read_text('utf-8-sig').splitlines()))
            assert list(rows[0]) == ['Comment', 'Designator', 'Footprint', 'LCSC Part #']
            assert rows[0]['Designator'] == 'Q1' and rows[0]['LCSC Part #'] == ''
            assert rows[1]['Designator'] == 'R1,R2' and rows[1]['LCSC Part #'] == 'C25804'
            assert len(rows) == 2 and panel.boards.GetValue() == 5, 'Export multiplied per-board references'
            assert len(requests) == before, 'Export fetched supplier data'
            assert '1 designator(s) have no LCSC' in panel.status.GetLabel()
            assert 'Q1' in panel.details.GetValue()
            assert (project / 'bom.csv').read_bytes() == original
            target = project / 'bom.csv'
            with patch('project_manager.bom_ui.wx.FileDialog', SaveDialog), \
                 patch('project_manager.bom_ui.wx.MessageBox') as error:
                panel._export_bom()
                assert error.call_count == 1
            assert (project / 'bom.csv').read_bytes() == original, 'Export overwrote source BOM'

        def click_column(column):
            event = wx.ListEvent(wx.EVT_LIST_COL_CLICK.typeId, panel.table.GetId())
            event.SetColumn(column)
            panel.table.GetEventHandler().ProcessEvent(event)

        def row_for(part):
            return panel._visible_keys.index(part.key)

        def set_price(part, reset=None):
            panel.table.Select(row_for(part))
            def edit():
                dialog = next(w for w in wx.GetTopLevelWindows() if isinstance(w, PriceDialog) and w.IsModal())
                if reset == 'empty':
                    dialog.fields['paid'].SetValue('')
                    dialog.fields['quantity'].SetValue('')
                    with patch.object(dialog, 'record', side_effect=AssertionError('Reset validated manual price')):
                        dialog._accept(None)
                elif reset == 'button':
                    dialog.fields['paid'].SetValue('invalid')
                    dialog.fields['quantity'].SetValue('invalid')
                    with patch.object(dialog, 'record', side_effect=AssertionError('Reset validated manual price')):
                        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, dialog.restore_button.GetId())
                        dialog.restore_button.GetEventHandler().ProcessEvent(event)
                else:
                    dialog.fields['paid'].SetValue('20')
                    dialog.fields['quantity'].SetValue('10')
                    dialog._accept(None)
            wx.CallLater(60, edit)
            panel._edit_price()

        def check():
            nonlocal phase
            try:
                if time.monotonic() - started > 20:
                    raise AssertionError('BOM smoke timed out')
                if frame._busy or panel._loading or panel._refreshing:
                    wx.CallLater(80, check)
                    return
                if phase == 0:
                    frame.notebook.SetSelection(frame.notebook.FindPage(panel))
                    phase = 1
                elif phase == 1:
                    assert len(panel.parts) == 2
                    if 'C25804' not in panel.supplies:
                        wx.CallLater(80, check)
                        return
                    assert 'incomplete' in panel.status.GetLabel()
                    assert 'partial' in panel.per_board.GetLabel()
                    count = len(requests)
                    panel.boards.SetValue(5)
                    panel._quantity_changed()
                    assert len(requests) == count, 'Quantity change made a network request'
                    assert panel.table.GetItemText(0, COLUMN_INDEX['unit_price']) == '0.005000'
                    assert panel.table.GetItemText(0, COLUMN_INDEX['library_type']) == 'Basic'
                    assert panel.parts[0].code_fields == ('Alt Supplier Part Number 1', 'Supplier Part Number')
                    assert panel.export_button.IsEnabled()
                    check_export()
                    panel.table.Select(1)

                    def enter_price():
                        dialog = next(w for w in wx.GetTopLevelWindows() if isinstance(w, PriceDialog) and w.IsModal())
                        dialog.fields['paid'].SetValue('13')
                        dialog.fields['quantity'].SetValue('10')
                        dialog.fields['seller'].SetValue('External purchase')
                        dialog._accept(None)

                    wx.CallLater(80, enter_price)
                    panel._edit_price()
                    assert load_state(project)['overrides'][panel.parts[1].key]['paid'] == '13'
                    assert panel.table.GetItemText(1, COLUMN_INDEX['unit_price']) == '1.300000'
                    assert 'incomplete' not in panel.status.GetLabel()
                    assert 'USD 6.55' in panel.total.GetLabel(), panel.total.GetLabel()
                    manual, coded = panel.parts[1], panel.parts[0]
                    for column in range(len(COLUMNS)):
                        for descending in (False, True):
                            click_column(column)
                            assert panel._sort_descending == descending
                            assert panel._selected().key == manual.key, 'Sorting changed the selected component'
                    click_column(COLUMN_INDEX['total'])
                    click_column(COLUMN_INDEX['total'])
                    assert panel._visible_keys[0] == manual.key, 'Expensive component is not first'
                    # Paid-price reset works after sorting and preserves other overrides.
                    for reset in ('empty', 'button', 'toolbar'):
                        set_price(coded)
                        assert panel.table.GetItemText(row_for(coded), COLUMN_INDEX['unit_price']) == '2.000000'
                        assert panel.table.GetItemText(row_for(coded), COLUMN_INDEX['library_type']) == 'Basic'
                        if reset == 'toolbar':
                            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, panel.restore_button.GetId())
                            panel.restore_button.GetEventHandler().ProcessEvent(event)
                        else:
                            set_price(coded, reset)
                        assert coded.key not in load_state(project)['overrides']
                        assert manual.key in load_state(project)['overrides'], 'Reset changed another component'
                        assert panel.table.GetItemText(row_for(coded), COLUMN_INDEX['unit_price']) == '0.005000'
                        assert panel._selected().key == coded.key
                    assert 'USD 6.55' in panel.total.GetLabel()
                    assert panel._sort_column == COLUMN_INDEX['total'] and panel._sort_descending
                    mode['stock'] = 5
                    panel.refresh_supply(force=True)
                    phase = 2
                elif phase == 2:
                    assert '1 shortages' in panel.status.GetLabel(), panel.status.GetLabel()
                    assert panel.stock_changes['C25804'] == -95
                    assert panel.table.GetItemText(row_for(panel.parts[1]), COLUMN_INDEX['unit_price']) == '1.300000'
                    assert panel._visible_keys[0] == panel.parts[1].key, 'Refresh discarded the sort order'
                    capture(frame, args.output)
                    frame.SetSize((860, 640))
                    phase = 21
                elif phase == 21:
                    assert panel.auto.GetScreenRect().right <= panel.GetScreenRect().right
                    assert panel.details.GetScreenRect().bottom <= panel.GetScreenRect().bottom
                    assert panel.table.GetSize().height > 100
                    capture(frame, args.output.with_name(args.output.stem + '-small.png'))
                    mode['fail'] = True
                    panel.refresh_supply(force=True)
                    phase = 3
                elif phase == 3:
                    assert 'Synthetic' in panel.errors['C25804']
                    assert panel.supplies['C25804'].stock == 5, 'Failure discarded last known stock'
                    assert 'USD 6.55' in panel.total.GetLabel()
                    generation = panel._generation
                    # A result delivered after a context change cannot populate the new project.
                    panel.set_context(None)
                    panel._supply_loaded(generation, 'C25804', supplier('C25804') if not mode['fail'] else None,
                                         'Late result', 1, 1)
                    assert not panel.supplies and not panel.parts and not panel.errors
                    assert not panel.price_button.IsEnabled()
                    assert not panel.export_button.IsEnabled()
                    # Closing a page while a worker runs must not touch destroyed controls.
                    mode['fail'], mode['wait'] = False, True
                    panel.set_context(context)
                    phase = 4
                elif phase == 4:
                    panel.refresh_supply(force=True)
                    mode['wait'] = False
                    frame.Close()
                    release.set()
                    assert panel._closed
                    return
                wx.CallLater(100, check)
            except Exception as exc:
                traceback.print_exc()
                failures.append(str(exc) or type(exc).__name__)
                release.set()
                panel.close()
                frame.Destroy()

        with patch('project_manager.bom_ui.fetch_supply', side_effect=supplier):
            wx.CallLater(150, check)
            faulthandler.dump_traceback_later(25, exit=True)
            app.MainLoop()
            faulthandler.cancel_dump_traceback_later()
        release.set()
    if failures:
        raise SystemExit('; '.join(failures))
    print('BOM GUI: automatic supplier fields, per-board assembly export and source protection, '
          'all-column sorting, stable selection, library type, all three paid-price reset paths, '
          'cached quantities, shortages, failure retention and stale-result isolation passed')


if __name__ == '__main__':
    main()
