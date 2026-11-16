"""Shared source-folder chooser for templates and standards."""
from __future__ import annotations

from pathlib import Path
import wx


class SourceFolderDialog(wx.Dialog):
    def __init__(self, parent, title, description, folder, browse_start, on_select):
        super().__init__(parent, title=title,
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.browse_start = browse_start
        self.on_select = on_select
        self.selection = None
        layout = wx.BoxSizer(wx.VERTICAL)
        note = wx.StaticText(self, label=description)
        note.Wrap(self.FromDIP(650))
        layout.Add(note, 0, wx.ALL, 14)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.folder = wx.TextCtrl(self, value=str(folder or ''))
        self.folder.SetMinSize((self.FromDIP(480), -1))
        row.Add(self.folder, 1, wx.EXPAND | wx.RIGHT, 10)
        browse = wx.Button(self, label='Browse…')
        browse.Bind(wx.EVT_BUTTON, self._browse)
        row.Add(browse)
        layout.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)
        hint = wx.StaticText(self, label='Use a local folder or an existing Git checkout. Optional Git settings are on Overview.')
        hint.Wrap(self.FromDIP(650))
        layout.Add(hint, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)
        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        self.FindWindow(wx.ID_OK).SetLabel('Use folder')
        self.Bind(wx.EVT_BUTTON, self._save, id=wx.ID_OK)
        layout.Add(buttons, 0, wx.EXPAND | wx.ALL, 14)
        self.SetSizerAndFit(layout)
        self.SetMinSize(self.GetSize())
        self.CentreOnParent()

    def _browse(self, event):
        start = Path(self.folder.GetValue().strip() or self.browse_start).expanduser()
        while not start.exists() and start != start.parent:
            start = start.parent
        dialog = wx.DirDialog(self, f'Choose a folder — {self.GetTitle()}', str(start),
                              style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST)
        try:
            if dialog.ShowModal() == wx.ID_OK:
                self.folder.ChangeValue(dialog.GetPath())
        finally:
            dialog.Destroy()

    def _save(self, event):
        try:
            self.selection = self.on_select(self.folder.GetValue().strip())
        except (OSError, ValueError) as exc:
            wx.MessageBox(str(exc), self.GetTitle(), wx.OK | wx.ICON_ERROR, self)
            return
        self.EndModal(wx.ID_OK)
