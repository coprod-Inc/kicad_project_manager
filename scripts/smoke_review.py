"""Exercise release preparation and the local checklist using synthetic projects."""
from __future__ import annotations

import argparse
import faulthandler
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import traceback
from unittest.mock import patch

if os.environ.get('DISPLAY'):
    os.environ.setdefault('GDK_BACKEND', 'x11')

import wx

from project_manager import checklist
from project_manager.context import ProjectContext
from project_manager.preferences import save_template
from project_manager.release import CHANGELOG, VARIABLES, STATE as RELEASE_STATE, enable_history
from project_manager.setup import apply, prepare, read_setup
from project_manager.setup_ui import ProjectSetupPanel
from project_manager.ui import ProjectManagerFrame
from scripts.capture_screenshots import capture
from tests.fixtures import sample_project
from tests.test_templates import VARIABLES as VARIABLE_TEXT, release as template_release, template_folder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('.dev/review'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='kpm-review-') as folder:
        root = Path(folder)
        project = sample_project(root / 'board')
        source = template_folder(root / 'template')
        save_template(str(source.root), root / 'settings')
        changelog = '# Changelog\n\n## [Unreleased]\n\n### Fixed\n\n- Pending change\n\n## [0.5.0] - 2026-01-01\n\n- Old change\n'
        (project / CHANGELOG).write_text(changelog)
        (project / VARIABLES).write_text(VARIABLE_TEXT)
        (project / 'sample.kicad_sch').write_text('(kicad_sch (text "${RELEASE_TITLE_1.0.0}"))')
        snapshot = read_setup(project)
        values = dict(snapshot.metadata, PROJECT_NAME='Sensor controller', BOARD_NAME='Control board',
                      DESIGNER='Alice Martin', GIT_URL='https://example.org/board')
        apply(prepare(snapshot, values, 'CHECKED', kicad_major=10), root / 'settings')
        context = ProjectContext('10.0.7', 'sample', str(project), str(project / 'sample.kicad_pro'),
                                 str(project / 'sample.kicad_pcb'), True, True, True)
        app = wx.App(False)
        frame = ProjectManagerFrame(lambda: context, root / 'settings')
        frame.SetSize((1120, 980))
        frame.Show()
        phase = 0
        failures = []

        def check_selection_persistence_and_no_op_apply():
            board = sample_project(root / 'no-op-apply')
            (board / CHANGELOG).write_text(changelog)
            (board / VARIABLES).write_bytes(enable_history(enable_history(VARIABLE_TEXT.encode(), '1.0.0'), '1.0.2'))
            snapshot = read_setup(board)
            apply(prepare(snapshot, snapshot.metadata, 'CHECKED'), root / 'no-op-settings')
            snapshot = read_setup(board)
            originals = {path: path.read_bytes() for path in board.rglob('*') if path.is_file()}
            saved = []
            setup_panel = ProjectSetupPanel(frame.notebook, saved.append, root / 'no-op-settings')
            try:
                setup_panel.load(snapshot, snapshot.metadata)
                assert setup_panel.release_panel.version.GetValue() == '', 'Inherited history must not select a release'
                assert not (board / RELEASE_STATE).exists(), 'Opening must not write a release record'
                for version in ('1.0.2', '1.0.0', '', '1.0.2', ''):
                    setup_panel.release_panel.version.SetValue(version)
                    assert setup_panel.dirty
                    assert {change.relative for change in setup_panel.make_plan().changes} == {RELEASE_STATE}
                    setup_panel._apply(None)
                    assert not setup_panel.dirty
                    setup_panel.load(snapshot, snapshot.metadata)
                    assert setup_panel.release_panel.version.GetValue() == version, 'Selection changed after reopening'
                    assert not setup_panel.dirty
                    # Equivalent formatting remains a no-op and must clear the
                    # unsaved flag without calling the save/refresh callback.
                    setup_panel.release_panel.version.SetValue(f' v{version} ' if version else ' ')
                    assert setup_panel.dirty
                    assert not setup_panel.make_plan().changes
                    calls_before = len(saved)
                    event = wx.CommandEvent(wx.EVT_BUTTON.typeId, setup_panel.apply_button.GetId())
                    setup_panel.apply_button.GetEventHandler().ProcessEvent(event)
                    assert setup_panel.feedback.GetLabel() == 'No changes to save.'
                    assert not setup_panel.dirty, 'No-op Apply left the accepted version marked as unsaved'
                    assert len(saved) == calls_before, 'No-op Apply should not write files or start a refresh'
                    with patch.object(frame, 'setup_panel', setup_panel), \
                         patch('project_manager.ui.wx.MessageBox', side_effect=AssertionError('Unexpected discard prompt')):
                        assert frame._can_discard()
                    setup_panel.load(snapshot, snapshot.metadata)
                assert len(saved) == 5
                assert all(path.read_bytes() == data for path, data in originals.items())

                setup_panel.release_panel.version.SetValue('invalid version')
                with patch('project_manager.setup_ui.wx.MessageBox') as message:
                    setup_panel._apply(None)
                assert message.call_count == 1 and setup_panel.dirty
                setup_panel.release_panel.version.SetValue('')
                setup_panel.release_panel.notes.SetValue('### Fixed\n\n- A real unsaved change')
                assert setup_panel.dirty and setup_panel.make_plan().changes
                with patch.object(frame, 'setup_panel', setup_panel), \
                     patch('project_manager.ui.wx.MessageBox', return_value=wx.NO) as message:
                    assert not frame._can_discard()
                    assert message.call_count == 1
            finally:
                setup_panel.Hide()
                wx.YieldIfNeeded()
                setup_panel.Destroy()

        def tick(panel, index):
            panel.items.CheckItem(index, True)
            event = wx.ListEvent(wx.EVT_LIST_ITEM_CHECKED.typeId, panel.items.GetId())
            event.SetIndex(index)
            panel._checked(event)

        def check():
            nonlocal phase
            try:
                if frame._busy or any(panel._busy or panel._catalog_busy or panel._snapshot_busy
                                      for panel in frame.standards_panels):
                    wx.CallLater(100, check)
                    return
                if phase == 0:
                    assert frame.notebook.GetPageCount() == 6
                    frame.notebook.SetSelection(1)
                    setup_panel = frame.setup_panel
                    assert setup_panel.release_panel.IsShown()
                    assert setup_panel.body.IsDescendant(setup_panel.status_choice)
                    assert not setup_panel.body.IsDescendant(setup_panel.apply_button)
                    choice = setup_panel.status_choice
                    selection = choice.GetSelection()
                    wheel = wx.MouseEvent(wx.EVT_MOUSEWHEEL.typeId)
                    wheel.SetEventObject(choice)
                    assert frame._wheel_filter.FilterEvent(wheel) == wx.EventFilter.Event_Processed
                    assert choice.GetSelection() == selection
                    setup_panel.release_panel.version.SetValue('1.0.0')
                    setup_panel.release_panel.notes.SetValue('### Fixed\n\n- Corrected connector pinout\n\n### Added\n\n- Bring-up test points')
                    setup_panel.release_panel._inspect(None)
                    assert setup_panel.release_panel.history_result.GetLabel() == 'Missing body text'
                    setup_panel.release_panel.version.SetValue('1.0.1')
                    assert setup_panel.release_panel.history_result.GetLabel() == 'Not checked'
                    assert not setup_panel.release_panel.history_result.GetToolTipText()
                    setup_panel.release_panel.version.SetValue('1.0.0')
                    setup_panel.release_panel._inspect(None)
                    plan = setup_panel.make_plan()
                    assert {change.relative for change in plan.changes} == {CHANGELOG, VARIABLES, RELEASE_STATE}
                    assert (project / CHANGELOG).read_text() == changelog
                    assert setup_panel.dirty
                    template_release(source.root, '1.1.0')
                    frame._template_source_updated()
                elif phase == 1:
                    if frame.template_comparison.template.version != '1.1.0':
                        wx.CallLater(100, check)
                        return
                    assert frame.setup_panel.dirty
                    assert 'Corrected connector pinout' in frame.setup_panel.release_panel.notes.GetValue()
                    frame.setup_panel.ScrollChildIntoView(frame.setup_panel.apply_button)
                    capture(frame, args.output / 'release-preparation.png')
                    frame.SetSize((860, 640))
                elif phase == 2:
                    frame.setup_panel.ScrollChildIntoView(frame.setup_panel.apply_button)
                    wx.YieldIfNeeded()
                    assert frame.setup_panel.apply_button.GetScreenRect().bottom <= frame.setup_panel.GetScreenRect().bottom
                    assert frame.setup_panel.apply_button.GetScreenRect().right <= frame.setup_panel.GetScreenRect().right
                    capture(frame, args.output / 'release-preparation-small.png')
                    frame.setup_panel.apply_plan(frame.setup_panel.make_plan())
                elif phase == 3:
                    check_selection_persistence_and_no_op_apply()
                    assert not frame.setup_panel.dirty
                    assert frame.setup_panel.release_panel.version.GetValue() == '1.0.0'
                    frame.setup_panel.release_panel._inspect(None)
                    assert '${RELEASE_BODY_1.0.0}: missing' in frame.setup_panel.release_panel.history_result.GetToolTipText()
                    assert (project / CHANGELOG).read_text().endswith('## [0.5.0] - 2026-01-01\n\n- Old change\n')
                    assert "    - variable: '@RELEASE_TITLE_VAR@1.0.0'" in (project / VARIABLES).read_text()
                    frame.notebook.SetSelection(4)
                    frame.SetSize((1120, 980))
                    panel = frame.checklist_panel
                    panel.reviewer.ChangeValue('Alice Martin')
                    tick(panel, 0)
                    tick(panel, 1)
                    selected = wx.ListEvent(wx.EVT_LIST_ITEM_SELECTED.typeId, panel.items.GetId())
                    selected.SetIndex(1)
                    panel._selected(selected)
                    panel.comment.ChangeValue('Isolation rules reviewed against the connector pinout.')
                    assert panel.flush()
                    assert panel.items.GetItemText(1, 4).startswith('Isolation rules reviewed')
                    panel.reviewer.ChangeValue('Sam Lee')
                    tick(panel, 3)
                    panel.set_context(context)
                    assert panel.state['items']['design-intent']['reviewer'] == 'Alice Martin'
                    assert panel.state['items']['pinouts']['reviewer'] == 'Sam Lee'
                    (project / 'sample.kicad_pcb').write_text('(kicad_pcb (gr_text "Moved silkscreen"))')
                    panel.set_context(context)
                    assert not panel._changed
                    assert panel.state['items']['rules']['checked']
                    assert panel.comment.GetValue().startswith('Isolation rules reviewed')
                elif phase == 4:
                    capture(frame, args.output / 'checklist.png')
                    frame.SetSize((860, 640))
                elif phase == 5:
                    panel = frame.checklist_panel
                    assert panel.complete_button.GetScreenRect().bottom <= panel.GetScreenRect().bottom
                    assert panel.comment.GetScreenRect().bottom <= panel.GetScreenRect().bottom
                    capture(frame, args.output / 'checklist-small.png')
                    panel.reviewer.ChangeValue('Sam Lee')
                    panel._bulk(True)
                    assert panel.save_status.GetLabel() == 'All items checked'
                    panel._complete(None)
                    assert panel.save_status.GetLabel() == 'Review recorded successfully'
                    assert panel.state['completed_review']['reviewer'] == 'Sam Lee'
                    (project / 'sample.kicad_prl').write_text('{"zoom": 4}')
                    panel.set_context(context)
                    assert not panel._changed
                    (project / 'sample.kicad_pcb').write_text('(kicad_pcb (gr_text "Later design edit"))')
                    panel.set_context(context)
                    assert panel._changed
                    assert all(panel.items.IsItemChecked(i) for i in range(len(checklist.ITEMS)))
                    assert not (project / '.git').exists()
                    frame.SetSize((1120, 980))
                else:
                    capture(frame, args.output / 'completed-review-changed.png')
                    saved = json.loads((project / checklist.STATE).read_text())
                    assert saved['items']['rules']['comment'].startswith('Isolation rules reviewed')
                    assert saved['items']['design-intent']['reviewer'] == 'Alice Martin'
                    panel = frame.checklist_panel
                    panel._bulk(False)
                    assert not any(panel.items.IsItemChecked(i) for i in range(len(checklist.ITEMS)))
                    assert not panel.state.get('completed_review')
                    assert panel.items.GetItemText(1, 4).startswith('Isolation rules reviewed')
                    frame.Close()
                    return
                phase += 1
                wx.CallLater(200, check)
            except Exception as exc:
                traceback.print_exc()
                failures.append(str(exc))
                frame.Destroy()

        wx.CallLater(250, check)
        faulthandler.dump_traceback_later(30, exit=True)
        app.MainLoop()
        faulthandler.cancel_dump_traceback_later()
        if failures:
            raise SystemExit('; '.join(failures))
    print('Release preparation, schematic warnings, local checklist and completed-review changes passed')


if __name__ == '__main__':
    main()
