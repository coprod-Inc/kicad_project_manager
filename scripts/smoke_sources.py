"""Exercise background source checks and explicit pulls without Internet access."""
from __future__ import annotations

import argparse
import faulthandler
import os
from pathlib import Path
import time
import traceback

if os.environ.get('DISPLAY'):
    os.environ.setdefault('GDK_BACKEND', 'x11')

import wx

from project_manager.source_repository import save_repository
from project_manager.source_repository_ui import RepositoryDialog, SourceRepositoryPanel
from tests.test_source_repository import SourceRepositoryTests
from scripts.capture_screenshots import capture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    fixture = SourceRepositoryTests()
    fixture.setUp()
    try:
        fixture.advance()
        storage = fixture.base / 'settings'
        save_repository(fixture.local, str(fixture.remote), 'main', True, storage)
        initial = fixture.git(fixture.local, 'rev-parse', 'HEAD')
        app = wx.App(False)
        frame = wx.Frame(None, size=(850, 180))
        updated = []
        panel = SourceRepositoryPanel(frame, storage, lambda: updated.append(True))
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(panel, 1, wx.EXPAND | wx.ALL, 12)
        frame.SetSizer(layout)
        frame.Show()
        failures = []
        with RepositoryDialog(frame, fixture.local, {}) as dialog:
            assert dialog.url.GetValue() == str(fixture.remote)
            assert dialog.branch.GetValue() == 'main'
            assert dialog.automatic.GetValue()
            def inspect_dialog():
                try:
                    bounds = wx.Rect(dialog.ClientToScreen((0, 0)), dialog.GetClientSize())
                    for identity in (wx.ID_OK, wx.ID_CANCEL):
                        button = dialog.FindWindow(identity)
                        rect = button.GetScreenRect()
                        assert button.IsShownOnScreen()
                        assert rect.top >= bounds.top and rect.bottom <= bounds.bottom
                        assert rect.left >= bounds.left and rect.right <= bounds.right
                    if args.output:
                        capture(dialog, args.output / 'git-settings.png')
                except Exception:
                    traceback.print_exc()
                    failures.append('Git settings buttons are clipped')
                finally:
                    dialog.EndModal(wx.ID_CANCEL)
            wx.CallLater(100, inspect_dialog)
            assert dialog.ShowModal() == wx.ID_CANCEL
        if failures:
            raise SystemExit('; '.join(failures))
        panel.set_source(fixture.local)
        phase, started = 0, time.monotonic()

        def check():
            nonlocal phase
            try:
                assert time.monotonic() - started < 20, 'Source controls timed out'
                if panel._busy:
                    wx.CallLater(100, check)
                    return
                if phase == 0:
                    assert panel.status.behind == 1
                    assert panel.pull_button.IsEnabled()
                    assert fixture.git(fixture.local, 'rev-parse', 'HEAD') == initial
                    assert not updated
                    event = wx.CommandEvent(wx.EVT_BUTTON.typeId, panel.pull_button.GetId())
                    panel.pull_button.GetEventHandler().ProcessEvent(event)
                    phase = 1
                else:
                    assert updated == [True]
                    assert 'Pulled successfully' in panel.message.GetLabel()
                    assert panel.status.behind == 0
                    assert not panel.pull_button.IsEnabled()
                    assert (fixture.local / 'template.txt').read_text() == 'remote update'
                    frame.Close()
                    return
                wx.CallLater(100, check)
            except Exception:
                traceback.print_exc()
                failures.append('Background source controls failed')
                frame.Destroy()

        wx.CallLater(100, check)
        faulthandler.dump_traceback_later(25, exit=True)
        app.MainLoop()
        faulthandler.cancel_dump_traceback_later()
        if failures:
            raise SystemExit('; '.join(failures))
    finally:
        fixture.doCleanups()
    print('Automatic background check, unchanged checkout and explicit fast-forward Pull passed')


if __name__ == '__main__':
    main()
