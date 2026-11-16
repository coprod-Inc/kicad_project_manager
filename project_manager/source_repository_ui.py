"""Optional source-repository controls; network operations stay off the UI thread."""
from __future__ import annotations

from pathlib import Path
import threading

import wx

from .source_repository import (check_repository, current_branch, load_repository, local_origin,
                                pull_repository, save_repository)
from .yaml_fields import SetupError


class RepositoryDialog(wx.Dialog):
    def __init__(self, parent, root, settings):
        super().__init__(parent, title='Source Git repository',
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        layout = wx.BoxSizer(wx.VERTICAL)
        intro = wx.StaticText(self, label='Optional: check this local source for remote updates. Pull only when you click.')
        intro.Wrap(self.FromDIP(610))
        layout.Add(intro, 0, wx.ALL, 12)
        grid = wx.FlexGridSizer(cols=2, vgap=10, hgap=12)
        grid.AddGrowableCol(1)
        self.url = wx.TextCtrl(self, value=settings.get('url') or local_origin(root))
        self.branch = wx.TextCtrl(self, value=settings.get('branch') or current_branch(root) or 'main')
        for label, control in (('Git URL', self.url), ('Branch', self.branch)):
            grid.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(control, 1, wx.EXPAND)
        layout.Add(grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)
        self.automatic = wx.CheckBox(self, label='Check automatically on open and every minute')
        self.automatic.SetValue(settings.get('automatic', True))
        layout.Add(self.automatic, 0, wx.ALL, 12)
        note = wx.StaticText(self, label='Leave the URL empty to use local files only. Git credentials use your existing Git configuration.')
        note.Wrap(self.FromDIP(610))
        layout.Add(note, 0, wx.LEFT | wx.RIGHT, 12)
        layout.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 12)
        self.SetSizerAndFit(layout)
        self.SetMinSize(self.GetSize())
        self.CentreOnParent()


class SourceRepositoryPanel(wx.Panel):
    def __init__(self, parent, storage, on_updated):
        super().__init__(parent, size=(650, 36))
        self.storage, self.on_updated = storage, on_updated
        self.root = None
        self.settings = {}
        self.status = None
        self._busy = False
        self._generation = 0
        self._alive = True
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.configure_button = wx.Button(self, label='Git…')
        self.check_button = wx.Button(self, label='Check remote')
        self.pull_button = wx.Button(self, label='Pull')
        self.message = wx.StaticText(self, label='Local files only', style=wx.ST_ELLIPSIZE_END)
        self.message.SetMinSize((1, -1))
        for button in (self.configure_button, self.check_button, self.pull_button):
            row.Add(button, 0, wx.RIGHT, 6)
        row.Add(self.message, 1, wx.ALIGN_CENTER_VERTICAL)
        self.SetSizer(row)
        self.configure_button.Bind(wx.EVT_BUTTON, self._configure)
        self.check_button.Bind(wx.EVT_BUTTON, lambda event: self.check())
        self.pull_button.Bind(wx.EVT_BUTTON, lambda event: self.check(pull=True))
        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._automatic, self._timer)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._destroy)
        self._enable()

    def _destroy(self, event):
        if event.GetEventObject() is self:
            self._alive = False
            self._timer.Stop()
        event.Skip()

    def set_source(self, root: Path | None):
        root = root.resolve() if root else None
        if root == self.root:
            return
        self._generation += 1
        self.root, self.status, self._busy = root, None, False
        self.settings = load_repository(root, self.storage) if root else {}
        self._timer.Stop()
        self._show('Local files only')
        self._enable()
        if self.settings.get('url') and self.settings.get('automatic'):
            self._timer.Start(60_000)
            self.check()

    def _show(self, message, colour=None):
        self.message.SetLabel(message)
        self.message.SetToolTip(message)
        self.message.SetForegroundColour(colour or wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT))

    def _enable(self):
        self.configure_button.Enable(bool(self.root) and not self._busy)
        self.check_button.Enable(bool(self.settings.get('url')) and not self._busy)
        self.pull_button.Enable(bool(self.status and self.status.can_pull) and not self._busy)

    def _configure(self, event):
        with RepositoryDialog(self, self.root, self.settings) as dialog:
            while dialog.ShowModal() == wx.ID_OK:
                try:
                    save_repository(self.root, dialog.url.GetValue(), dialog.branch.GetValue(),
                                    dialog.automatic.GetValue(), self.storage)
                    break
                except (OSError, SetupError) as exc:
                    wx.MessageBox(str(exc), 'Source repository', wx.OK | wx.ICON_ERROR, dialog)
            else:
                return
        root = self.root
        self.root = None
        self.set_source(root)
        if self.settings.get('url') and not self.settings.get('automatic'):
            self.check()

    def _automatic(self, event):
        if self.settings.get('automatic'):
            self.check()

    def check(self, pull=False):
        if self._busy or not self.root or not self.settings.get('url'):
            return
        self._busy = True
        self._show('Pulling…' if pull else 'Checking remote…')
        self._enable()
        root, settings, generation = self.root, dict(self.settings), self._generation

        def worker():
            status, error = None, None
            try:
                operation = pull_repository if pull else check_repository
                status = operation(root, settings['url'], settings.get('branch', 'main'))
            except (SetupError, OSError) as exc:
                error = str(exc)
            wx.CallAfter(self._finish, generation, status, error, pull)

        threading.Thread(target=worker, daemon=True, name='source-repository').start()

    def _finish(self, generation, status, error, pulled):
        if not self._alive or generation != self._generation:
            return
        self._busy, self.status = False, status
        self._show(error or ('Pulled successfully. ' if pulled else '') + status.message,
                   wx.Colour(185, 115, 25) if error or (status and (status.conflicts or status.behind))
                   else wx.Colour(35, 140, 65))
        self._enable()
        if pulled and not error:
            self.on_updated()
