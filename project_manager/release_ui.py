"""Small release-preparation section embedded in Project Setup."""
from __future__ import annotations

from pathlib import Path
import wx

from .release import CHANGELOG, VARIABLES, STATE, history_status, prepare_release, read_release
from .yaml_fields import SetupError


class ReleasePreparationPanel(wx.Panel):
    def __init__(self, parent, on_edit):
        super().__init__(parent)
        self.SetMinSize((1, -1))
        self.snapshot = None
        self.schematic = None
        self.on_edit = on_edit
        layout = wx.StaticBoxSizer(wx.VERTICAL, self, 'Prepare a board release')
        box = layout.GetStaticBox()
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(box, label='Next release'), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        self.version = wx.TextCtrl(box, size=(135, -1))
        self.version.SetHint('Unreleased')
        self.version.SetToolTip('Leave blank for Unreleased, or enter a version such as 1.0.0. Apply remembers this choice; existing history entries are kept.')
        row.Add(self.version, 0, wx.RIGHT, 10)
        self.inspect = wx.Button(box, label='Check schematic history')
        self.inspect.Bind(wx.EVT_BUTTON, self._inspect)
        row.Add(self.inspect)
        self.history_result = wx.StaticText(box, label='Not checked', style=wx.ST_ELLIPSIZE_END)
        self.history_result.SetMinSize((1, -1))
        row.Add(self.history_result, 1, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, 12)
        layout.Add(row, 0, wx.EXPAND | wx.ALL, 10)
        layout.Add(wx.StaticText(box, label='Changes below [Unreleased] in CHANGELOG.md'), 0, wx.LEFT | wx.RIGHT, 10)
        self.notes = wx.TextCtrl(box, style=wx.TE_MULTILINE | wx.HSCROLL, size=(-1, 150))
        self.notes.SetMinSize((1, 120))
        self.notes.SetFont(wx.Font(wx.FontInfo(10).Family(wx.FONTFAMILY_TELETYPE)))
        layout.Add(self.notes, 1, wx.EXPAND | wx.ALL, 10)
        self.message = wx.StaticText(box, label='', style=wx.ST_NO_AUTORESIZE)
        self.message.SetMinSize((1, -1))
        layout.Add(self.message, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        self.SetSizer(layout)
        self.version.Bind(wx.EVT_TEXT, self._edited)
        self.version.Bind(wx.EVT_KILL_FOCUS, self._version_left)
        self.notes.Bind(wx.EVT_TEXT, self._edited)
        self.Bind(wx.EVT_SIZE, self._resize)
        self._message = ''

    def state(self):
        return self.version.GetValue(), self.notes.GetValue()

    def load(self, root: Path, schematic: Path | None = None):
        self.schematic = schematic
        self._reset_history()

        try:
            self.snapshot = read_release(root)
            self.version.ChangeValue(self.snapshot.pending_version)
            self.notes.ChangeValue(self.snapshot.notes)
            self.notes.Enable()
            ready = self.snapshot.variables is not None
            self.version.Enable(ready)
            self.inspect.Enable(ready and schematic is not None)
            self._show('Apply saves Unreleased notes and enables this version’s title/body commands. Release CI creates the dated changelog section.' if ready else
                       f'You can edit the changelog. Add {VARIABLES} through template review to enable release variables.')
        except (SetupError, OSError, UnicodeError) as exc:
            self.snapshot = None
            self.version.ChangeValue('')
            self.notes.ChangeValue('')
            for control in (self.notes, self.version, self.inspect):
                control.Disable()
            self._show(str(exc))
        self._baseline = self.state()
        if self.snapshot is not None and self.version.GetValue() and schematic is not None:
            self._inspect(None)

    def _reset_history(self):
        self.history_result.SetLabel('Not checked')
        self.history_result.SetToolTip('')
        self.history_result.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT))

    @property
    def dirty(self):
        return self.snapshot is not None and self.state() != self._baseline

    def changes(self):
        if self.snapshot is None or not self.dirty:
            return (), ()
        changes = prepare_release(self.snapshot, self.version.GetValue(), self.notes.GetValue())
        checks = ((CHANGELOG, self.snapshot.changelog), (VARIABLES, self.snapshot.variables), (STATE, self.snapshot.state))
        return changes, checks

    def _show(self, message):
        self._message = message
        self.message.SetLabel(message)
        self.message.Wrap(max(240, self.GetClientSize().width - 30))
        height = self.message.GetBestSize().height
        previous = self.message.GetMinSize().height
        self.message.SetMinSize((1, height))
        self.Layout()
        if height != previous:
            self.GetParent().Layout()
            self.GetParent().FitInside()

    def _resize(self, event):
        event.Skip()
        if self._message:
            wx.CallAfter(self._show, self._message)

    def _edited(self, event):
        if event.GetEventObject() is self.version:
            self._reset_history()
        self.on_edit(event)

    def _version_left(self, event):
        event.Skip()
        if self.version.GetValue().strip():
            self._inspect(None)

    def _inspect(self, event):
        try:
            if self.snapshot is None or self.schematic is None:
                return
            details = history_status(self.snapshot, self.version.GetValue(), self.schematic)
            lines = details.splitlines()
            missing = [kind for kind, line in zip(('title', 'body'), lines) if 'missing from saved schematic text' in line]
            self.history_result.SetLabel('Missing ' + ' and '.join(missing) + ' text' if missing else 'Title and body found')
            self.history_result.SetForegroundColour(wx.Colour(205, 145, 40) if missing else wx.Colour(46, 155, 80))
            self.history_result.SetToolTip(details)
        except (SetupError, OSError, UnicodeError) as exc:
            self.history_result.SetLabel('Could not check saved schematic')
            self.history_result.SetForegroundColour(wx.Colour(200, 55, 55))
            self.history_result.SetToolTip(str(exc))
        self.GetParent().Layout()
        self.GetParent().FitInside()
