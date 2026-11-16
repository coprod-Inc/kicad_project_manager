"""Native human-review checklist with persistent local answers."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import wx

from . import checklist
from .yaml_fields import SetupError


class ChecklistPanel(wx.Panel):
    def __init__(self, parent, storage=None, before_save=None):
        super().__init__(parent)
        self.storage = storage
        self.before_save = before_save
        self.snapshot = None
        self.state = checklist.empty_state()
        self.context = None
        self.current = None
        self._loading = False
        self._timer = None
        self._changed = False
        layout = wx.BoxSizer(wx.VERTICAL)
        self.message = wx.StaticText(self, label='Open a saved project or board to use the checklist.',
                                     style=wx.ST_NO_AUTORESIZE)
        layout.Add(self.message, 0, wx.EXPAND | wx.ALL, 14)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, label='Your reviewer name'), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        self.reviewer = wx.TextCtrl(self)
        row.Add(self.reviewer, 1)
        self.save_status = wx.StaticText(self, label='Saved automatically')
        self.save_status.SetToolTip(f'Checks save immediately; comments save after a short pause. File: {checklist.STATE}')
        row.Add(self.save_status, 0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, 10)
        layout.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)
        self.items = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.items.EnableCheckBoxes()
        for heading, width in [('Check', 490), ('Stage', 105), ('Reviewer', 105), ('Date', 85), ('Comment', 180)]:
            self.items.InsertColumn(self.items.GetColumnCount(), heading, width=width)
        for identity, group, title in checklist.ITEMS:
            index = self.items.InsertItem(self.items.GetItemCount(), title)
            self.items.SetItem(index, 1, group)
        layout.Add(self.items, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 14)
        self.comment_label = wx.StaticText(self, label='Select a check to add a comment or explain why it is not applicable.')
        layout.Add(self.comment_label, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 14)
        self.comment = wx.TextCtrl(self, style=wx.TE_MULTILINE, size=(-1, 85))
        self.comment.Disable()
        layout.Add(self.comment, 0, wx.EXPAND | wx.ALL, 14)
        footer = wx.BoxSizer(wx.HORIZONTAL)
        self.count = wx.StaticText(self, label='')
        footer.Add(self.count, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.tick_all_button = wx.Button(self, label='Tick all')
        self.untick_all_button = wx.Button(self, label='Untick all')
        self.tick_all_button.Bind(wx.EVT_BUTTON, lambda event: self._bulk(True))
        self.untick_all_button.Bind(wx.EVT_BUTTON, lambda event: self._bulk(False))
        footer.Add(self.tick_all_button, 0, wx.RIGHT, 8)
        footer.Add(self.untick_all_button, 0, wx.RIGHT, 8)
        self.complete_button = wx.Button(self, label='Record completed review')
        self.complete_button.Bind(wx.EVT_BUTTON, self._complete)
        self.complete_button.SetToolTip('Record the saved design after all checks are complete. A reviewer name is required.')
        footer.Add(self.complete_button)
        layout.Add(footer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)
        self.SetSizer(layout)
        self.reviewer.Bind(wx.EVT_TEXT, self._edited)
        self.comment.Bind(wx.EVT_TEXT, self._edited)
        self.items.Bind(wx.EVT_LIST_ITEM_SELECTED, self._selected)
        self.items.Bind(wx.EVT_LIST_ITEM_CHECKED, self._checked)
        self.items.Bind(wx.EVT_LIST_ITEM_UNCHECKED, self._checked)
        self.Bind(wx.EVT_SIZE, self._resize)
        self.set_context(None)

    @property
    def dirty(self):
        if self.snapshot is None:
            return False
        self._collect()
        return self.state != self.snapshot.state

    def _collect(self):
        if self._loading or self.snapshot is None:
            return
        self.state['reviewer'] = self.reviewer.GetValue().strip()
        if self.current is not None:
            identity = checklist.ITEMS[self.current][0]
            value = dict(checklist.item_value(self.state, identity))
            if value['comment'] != self.comment.GetValue():
                value['comment'] = self.comment.GetValue()
                self.state['items'][identity] = value
                self.items.SetItem(self.current, 4, ' / '.join(value['comment'].splitlines()))

    def set_context(self, context):
        selected = self.current if context == self.context else None
        if self._timer:
            self._timer.Stop()
            self._timer = None
        self.context = context
        self.snapshot = None
        self.current = None
        self._changed = False
        self._loading = True
        self.comment.ChangeValue('')
        self.comment.Disable()
        self.comment_label.SetLabel('Select a check to add a comment or explain why it is not applicable.')
        try:
            if context is None or not context.board_saved:
                raise SetupError('Open and save a board before using the checklist.')
            root = Path(context.project_file).parent if context.project_file else Path(context.board_file).parent
            self.snapshot = checklist.load(root)
            self.state = deepcopy(self.snapshot.state)
            self.reviewer.ChangeValue(self.state['reviewer'])
            if self.state.get('completed_review'):
                self._changed = checklist.changed_since_review(self.state, self._fingerprint())
            for index, (identity, _, _) in enumerate(checklist.ITEMS):
                self._paint_item(index)
                self.items.Select(index, index == selected)
            if selected is not None:
                self.current = selected
                self.comment.ChangeValue(checklist.item_value(self.state, checklist.ITEMS[selected][0])['comment'])
                self.comment.Enable()
                self.comment_label.SetLabel('Comment — ' + checklist.ITEMS[selected][2])
                self.comment_label.Wrap(max(240, self.GetClientSize().width - 28))
            self._summary()
            self._save_feedback('Saved automatically' if self.snapshot.original else 'Answers save automatically')
        except (SetupError, OSError, UnicodeError) as exc:
            self._show(str(exc))
        finally:
            self._loading = False
            ready = self.snapshot is not None
            for control in (self.reviewer, self.items, self.tick_all_button, self.untick_all_button):
                control.Enable(ready)
            self.complete_button.Enable(ready)

    def _fingerprint(self):
        project = Path(self.context.project_file) if self.context.project_file else None
        schematic = project.with_suffix('.kicad_sch') if project is not None else None
        return checklist.design_fingerprint(self.snapshot.root, Path(self.context.board_file), project, schematic)

    def refresh_review_status(self):
        if self.snapshot is not None and self.state.get('completed_review'):
            try:
                self._changed = checklist.changed_since_review(self.state, self._fingerprint())
                self._summary()
            except (SetupError, OSError) as exc:
                self._show(f'Could not compare saved design files: {exc}', error=True)

    def _show(self, text, error=False):
        self.message.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT)
                                         if not error else wx.Colour(200, 55, 55))
        self.message.SetLabel(text)
        self.message.Wrap(max(240, self.GetClientSize().width - 28))
        self.message.SetMinSize((1, self.message.GetBestSize().height))
        self.Layout()

    def _summary(self):
        done = sum(checklist.item_value(self.state, identity)['checked'] for identity, _, _ in checklist.ITEMS)
        self.count.SetLabel(f'{done} / {len(checklist.ITEMS)} checked')
        completed = self.state.get('completed_review')
        if completed:
            message = f'Completed review recorded by {completed["reviewer"]} on {completed["date"][:10]}.'
            if self._changed:
                message += ' Saved design files have changed since that review. Revisit the affected checks; your ticks are kept.'
        else:
            message = 'Review in progress. Checks stay checked while you edit the design. Answers and comments save locally.'
        self._show(message)
        self.complete_button.Enable(self.snapshot is not None)

    def _save_feedback(self, text, error=False):
        self.save_status.SetLabel(text)
        self.save_status.SetForegroundColour(wx.Colour(200, 55, 55) if error else wx.Colour(46, 155, 80))
        self.Layout()

    def _all_checked(self):
        return all(checklist.item_value(self.state, identity)['checked'] for identity, _, _ in checklist.ITEMS)

    def _paint_item(self, index):
        value = checklist.item_value(self.state, checklist.ITEMS[index][0])
        self.items.CheckItem(index, value['checked'])
        self.items.SetItem(index, 2, value['reviewer'])
        self.items.SetItem(index, 3, value['checked_at'][:10])
        self.items.SetItem(index, 4, ' / '.join(value['comment'].splitlines()))

    def _selected(self, event):
        if self._loading or self.snapshot is None:
            return
        self._collect()
        self.current = event.GetIndex()
        self.comment.ChangeValue(checklist.item_value(self.state, checklist.ITEMS[self.current][0])['comment'])
        self.comment.Enable()
        self.comment_label.SetLabel('Comment — ' + checklist.ITEMS[self.current][2])
        self.comment_label.Wrap(max(240, self.GetClientSize().width - 28))
        self.Layout()

    def _checked(self, event):
        if self._loading or self.snapshot is None:
            return
        index = event.GetIndex()
        identity = checklist.ITEMS[index][0]
        self._collect()
        try:
            self.state = checklist.update_item(self.state, identity, checked=self.items.IsItemChecked(index),
                                               reviewer=self.reviewer.GetValue(),
                                               comment=checklist.item_value(self.state, identity)['comment'])
            self.flush()
        except SetupError as exc:
            self._show(str(exc), error=True)
            self.reviewer.SetFocus()
        finally:
            self._loading = True
            self._paint_item(index)
            self._loading = False

    def _edited(self, event):
        if self._loading or self.snapshot is None:
            return
        if self._timer:
            self._timer.Stop()
        self.save_status.SetLabel('Saving…')
        self._timer = wx.CallLater(600, self.flush)
        event.Skip()

    def flush(self) -> bool:
        if self._timer:
            self._timer.Stop()
            self._timer = None
        if self.snapshot is None:
            return True
        self._collect()
        try:
            if self.before_save:
                self.before_save()
            self.snapshot = checklist.save(self.snapshot, self.state, self.storage)
            if self.current is not None:
                self.items.SetItem(self.current, 4, ' / '.join(self.comment.GetValue().splitlines()))
            self._summary()
            self._save_feedback('Saved automatically')
            return True
        except (SetupError, OSError) as exc:
            self._save_feedback('Not saved', error=True)
            self._show(f'Could not save checklist: {exc}', error=True)
            return False

    def _complete(self, event):
        if not self.flush():
            return
        try:
            self.state = checklist.complete(self.state, self._fingerprint(), self.reviewer.GetValue())
            self._changed = False
            if self.flush():
                self._save_feedback('Review recorded successfully')
        except (SetupError, OSError) as exc:
            self._show(str(exc), error=True)

    def _bulk(self, checked):
        if self.snapshot is None:
            return
        self._collect()
        try:
            self.state = checklist.update_all(self.state, checked=checked, reviewer=self.reviewer.GetValue())
            self._loading = True
            for index in range(len(checklist.ITEMS)):
                self._paint_item(index)
            self._loading = False
            self._changed = False if not checked else self._changed
            if self.flush():
                self._save_feedback('All items checked' if checked else 'All items unchecked; comments kept')
        except (SetupError, OSError) as exc:
            self._loading = False
            self._show(str(exc), error=True)

    def _resize(self, event):
        event.Skip()
        wx.CallAfter(self._size_columns)

    def _size_columns(self):
        if self.items:
            other = sum(self.items.GetColumnWidth(i) for i in range(1, 5))
            self.items.SetColumnWidth(0, max(260, self.items.GetClientSize().width - other - 24))
