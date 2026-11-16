"""Native project setup form with optional preview and guarded application."""
from __future__ import annotations

from pathlib import Path
from dataclasses import replace
from typing import Callable
import logging
import re
import wx
import wx.lib.scrolledpanel

from .setup import METADATA, STATUSES, Plan, Snapshot, apply, prepare, suggested_kicad_major
from .yaml_fields import SetupError
from .diff_ui import show_diff
from .release_ui import ReleasePreparationPanel


class PreviewDialog(wx.Dialog):
    def __init__(self, parent, plan: Plan, title: str = 'Review Project Changes', intro: str | None = None):
        super().__init__(parent, title=title, size=(850, 620),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        layout = wx.BoxSizer(wx.VERTICAL)
        label = wx.StaticText(self, label=intro or 'Apply these changes to the active project? Original files will be backed up locally.')
        label.Wrap(800)
        layout.Add(label, 0, wx.ALL, 14)
        self.diff = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL | wx.TE_RICH2)
        self.diff.SetFont(wx.Font(wx.FontInfo(10).Family(wx.FONTFAMILY_TELETYPE)))
        show_diff(self.diff, plan.diff())
        layout.Add(self.diff, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 14)
        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        self.FindWindow(wx.ID_OK).SetLabel('Apply Changes')
        layout.Add(buttons, 0, wx.EXPAND | wx.ALL, 14)
        self.SetSizer(layout)
        self.SetMinSize((640, 420))
        self.CentreOnParent()


class ProjectSetupPanel(wx.Panel):
    def __init__(self, parent, on_applied: Callable[[Path | None], None], storage: Path | None = None,
                 before_apply: Callable[[], None] | None = None):
        super().__init__(parent)
        self._on_applied = on_applied
        self.storage = storage
        self.before_apply = before_apply
        self.snapshot: Snapshot | None = None
        self._baseline = None
        self._author_display = ''
        self._author_value = ''
        self._reviewer_display = ''
        self._reviewer_value = ''
        self._loaded_values = {}
        self._feedback_text = ''
        self._feedback_root: Path | None = None
        outer = wx.BoxSizer(wx.VERTICAL)
        self.body = wx.lib.scrolledpanel.ScrolledPanel(self, size=(1000, 700))
        body = self.body
        layout = wx.BoxSizer(wx.VERTICAL)
        self.message = wx.StaticText(body, label='Connect to a saved template project to edit setup.')
        self.message.Wrap(740)
        layout.Add(self.message, 0, wx.EXPAND | wx.ALL, 14)
        grid = wx.FlexGridSizer(cols=4, vgap=9, hgap=12)
        grid.AddGrowableCol(1)
        grid.AddGrowableCol(3)
        self.fields: dict[str, wx.TextCtrl] = {}
        for key, label in [('PROJECT_NAME', 'Project name'), ('BOARD_NAME', 'Board name'),
                           ('COMPANY', 'Company'), ('DESIGNER', 'Authors'),
                           ('REVIEWER', 'Reviewers'),
                           ('GIT_URL', 'Git web URL'), ('LOGO', 'Logo path')]:
            grid.Add(wx.StaticText(body, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            field = wx.TextCtrl(body)
            field.SetMinSize((1, -1))
            field.SetToolTip('Path relative to the project, for example Logos/logo.png' if key == 'LOGO' else label.replace('\n', ' '))
            if key in ('DESIGNER', 'REVIEWER'):
                field.SetToolTip('Separate multiple names with commas or semicolons. Existing saved values are preserved until you edit them.')
            self.fields[key] = field
            grid.Add(field, 1, wx.EXPAND)
        grid.Add(wx.StaticText(body, label='Project status'), 0, wx.ALIGN_CENTER_VERTICAL)
        self.status_choice = wx.Choice(body)
        self._fill_statuses(None)
        grid.Add(self.status_choice, 1, wx.EXPAND)
        grid.Add(wx.StaticText(body, label='KiCad version for CI'), 0, wx.ALIGN_CENTER_VERTICAL)
        self.kicad_choice = wx.Choice(body)
        for major in (9, 10):
            self.kicad_choice.Append(f'KiCad {major}', major)
        self.kicad_choice.SetToolTip('Version used to generate outputs on GitHub. Select KiCad 10 for files saved with KiCad 10.')
        grid.Add(self.kicad_choice, 1, wx.EXPAND)
        grid.AddSpacer(0)
        grid.AddSpacer(0)
        layout.Add(grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 14)
        note = wx.StaticText(body, label='KiBot updates text variables on the next run. Release: CHECKED on dev → CI → merge into main → tag main.')
        note.Wrap(740)
        layout.Add(note, 0, wx.EXPAND | wx.ALL, 14)
        self.release_panel = ReleasePreparationPanel(body, self._edited)
        layout.Add(self.release_panel, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)
        self.release_panel.Hide()
        body.SetSizer(layout)
        body.SetupScrolling(scroll_x=False)
        outer.Add(body, 1, wx.EXPAND)
        self.feedback = wx.StaticText(self, label='', style=wx.ST_NO_AUTORESIZE)
        self.feedback.SetMinSize((1, 0))
        outer.Add(self.feedback, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.AddStretchSpacer()
        self.preview_button = wx.Button(self, label='Preview Changes…')
        self.preview_button.Bind(wx.EVT_BUTTON, self._preview)
        row.Add(self.preview_button)
        self.apply_button = wx.Button(self, label='Apply')
        self.apply_button.SetToolTip('Save changes directly. Original files are backed up locally.')
        self.apply_button.Bind(wx.EVT_BUTTON, self._apply)
        row.Add(self.apply_button, 0, wx.LEFT, 8)
        outer.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)
        self.SetSizer(outer)
        for field in self.fields.values():
            field.Bind(wx.EVT_TEXT, self._edited)
        for choice in (self.status_choice, self.kicad_choice):
            choice.Bind(wx.EVT_CHOICE, self._edited)
        self.Bind(wx.EVT_SIZE, self._resize_feedback)
        self.clear('Reading the active project…')

    def FitInside(self):
        self.body.Layout()
        self.body.FitInside()

    def ScrollChildIntoView(self, child):
        parent = child.GetParent()
        while parent and parent is not self.body:
            parent = parent.GetParent()
        if parent is self.body:
            self.body.ScrollChildIntoView(child)

    def _show_feedback(self, text: str, *, error: bool = False, root: Path | None = None):
        self._feedback_text = text
        self._feedback_root = root
        self.feedback.SetForegroundColour(wx.Colour(220, 70, 70) if error else wx.Colour(46, 170, 80))
        self.feedback.SetToolTip(text)
        self._layout_feedback()

    def _layout_feedback(self):
        if not self:
            return
        self.feedback.SetLabel(self._feedback_text)
        self.feedback.Wrap(max(180, self.GetClientSize().width - 28))
        self.feedback.SetMinSize((1, self.feedback.GetBestSize().height if self._feedback_text else 0))
        self.Layout()
        self.FitInside()

    def _resize_feedback(self, event):
        event.Skip()
        wx.CallAfter(self._layout_feedback)

    def _edited(self, event):
        self._show_release()
        if self._feedback_text:
            self._show_feedback('')
        event.Skip()

    def _state(self):
        return (tuple(self.fields[key].GetValue() for key in METADATA), self.selected_status(),
                self.selected_kicad_major(),
                self.release_panel.state() if self.selected_status() == 'CHECKED' else None)

    def _show_release(self):
        self.release_panel.Show(self.snapshot is not None and self.selected_status() == 'CHECKED')
        self.Layout()
        self.FitInside()

    @property
    def dirty(self) -> bool:
        return self.snapshot is not None and self._baseline is not None and self._state() != self._baseline

    def clear(self, message: str):
        self._show_feedback('')
        self.snapshot = None
        self._baseline = None
        for field in self.fields.values():
            field.ChangeValue('')
            field.Disable()
        self._fill_statuses(None)
        self.release_panel.Hide()
        self.status_choice.Disable()
        self.select_kicad_major(None)
        self.kicad_choice.Disable()
        self.preview_button.Disable()
        self.apply_button.Disable()
        self.message.SetLabel(message)
        self.message.Wrap(max(400, self.GetClientSize().width - 28))
        self.Layout()

    def load(self, snapshot: Snapshot, values: dict[str, str], editor_major: int | None = None,
             schematic: Path | None = None):
        if self._feedback_root != snapshot.root:
            self._show_feedback('')
        self.snapshot = snapshot
        self._loaded_values = dict(values)
        self._author_value = values['DESIGNER']
        self._author_display = ', '.join(part.strip() for part in re.split(r'[;\n]', values['DESIGNER']) if part.strip())
        self._reviewer_value = values['REVIEWER']
        self._reviewer_display = ', '.join(part.strip() for part in re.split(r'[;\n]', values['REVIEWER']) if part.strip())
        for key, field in self.fields.items():
            field.ChangeValue(self._author_display if key == 'DESIGNER' else
                              self._reviewer_display if key == 'REVIEWER' else values[key])
            field.Enable()
        self._fill_statuses(snapshot.status)
        self.select_kicad_major(snapshot.kicad_major)
        self.preview_button.Enable()
        self.apply_button.Enable()
        self._update_status_enabled()
        self.release_panel.load(snapshot.root, schematic)
        self._show_release()
        self.message.SetLabel('Review the suggested values, then preview or apply your changes.' if values != snapshot.metadata
                              else 'Edit project information below, then preview or apply your changes.')
        suggested = suggested_kicad_major(snapshot, editor_major)
        if suggested != snapshot.kicad_major:
            self.select_kicad_major(suggested)
            self.message.SetLabel(f'CI currently uses KiCad {snapshot.kicad_major}. KiCad {suggested} is suggested for this editor.\n'
                                  'Apply to save your selection.')
        # Suggested defaults need Apply, like suggested metadata. Merely opening
        # the panel should not create a discard-changes prompt.
        self._baseline = self._state()
        self.Layout()
        self.FitInside()

    def metadata(self) -> dict[str, str]:
        result = {key: field.GetValue() if field.GetValue() == self._loaded_values.get(key)
                  else field.GetValue().strip() for key, field in self.fields.items()}
        authors = self.fields['DESIGNER'].GetValue()
        result['DESIGNER'] = self._author_value if authors == self._author_display else '; '.join(
            name.strip() for name in re.split(r'[,;\n]', authors) if name.strip())
        reviewers = self.fields['REVIEWER'].GetValue()
        result['REVIEWER'] = self._reviewer_value if reviewers == self._reviewer_display else '; '.join(
            name.strip() for name in re.split(r'[,;\n]', reviewers) if name.strip())
        return result

    def _fill_statuses(self, current: str | None):
        descriptions = {
            'DRAFT': 'DRAFT — schematic PDF',
            'PRELIMINARY': 'PRELIMINARY — documentation',
            'CHECKED': 'CHECKED — ERC/DRC',
        }
        codes = list(STATUSES)
        if current and current not in codes:
            codes.append(current)
        self.status_choice.Clear()
        for code in codes:
            label = descriptions.get(code, f'{code} — kept unless you choose DRAFT, PRELIMINARY, or CHECKED')
            self.status_choice.Append(label, code)
        self.select_status(current)

    def select_status(self, code: str | None):
        if not code:
            self.status_choice.SetSelection(wx.NOT_FOUND)
            return
        for index in range(self.status_choice.GetCount()):
            if self.status_choice.GetClientData(index) == code:
                self.status_choice.SetSelection(index)
                return

    def selected_status(self) -> str:
        index = self.status_choice.GetSelection()
        if index == wx.NOT_FOUND:
            return ''
        return self.status_choice.GetClientData(index)

    def select_kicad_major(self, major: int | None):
        self.kicad_choice.SetSelection((9, 10).index(major) if major in (9, 10) else wx.NOT_FOUND)

    def selected_kicad_major(self) -> int | None:
        index = self.kicad_choice.GetSelection()
        return self.kicad_choice.GetClientData(index) if index != wx.NOT_FOUND else None

    def _update_status_enabled(self):
        ready = self.snapshot is not None and self.snapshot.workflow is not None
        self.status_choice.Enable(ready)
        self.kicad_choice.Enable(ready)
        self.status_choice.SetToolTip('' if ready else 'Use template review on the Overview tab to add the missing workflow before changing project status.')

    def make_plan(self) -> Plan:
        if self.snapshot is None:
            raise SetupError('Refresh the project before preparing changes.')
        plan = prepare(self.snapshot, self.metadata(), self.selected_status(),
                       kicad_major=self.selected_kicad_major())
        if self.selected_status() == 'CHECKED':
            changes, checks = self.release_panel.changes()
            plan = replace(plan, changes=plan.changes + changes, checks=checks)
        return plan

    def apply_plan(self, plan: Plan):
        if self.before_apply:
            self.before_apply()
        backup = apply(plan, self.storage)
        self._baseline = self._state()
        self._on_applied(backup)

    def _preview(self, event):
        self._submit(preview=True)

    def _apply(self, event):
        self._submit(preview=False)

    def _submit(self, *, preview: bool):
        root = self.snapshot.root if self.snapshot is not None else None
        self.preview_button.Disable()
        self.apply_button.Disable()
        try:
            plan = self.make_plan()
            if not plan.changes:
                # The selected version/formatting may already be represented in
                # the files. Accept the form state even when no write is needed.
                self._baseline = self._state()
                self._show_feedback('No changes to save.', root=root)
                return
            if preview:
                with PreviewDialog(self, plan) as dialog:
                    if dialog.ShowModal() != wx.ID_OK:
                        return
            self.apply_plan(plan)
            # The callback starts a refresh and clears the form. Set the outcome
            # afterwards so it survives loading this same project's saved data.
            self._show_feedback('Changes saved successfully.', root=root)
        except (SetupError, OSError) as exc:
            self._show_feedback(f'Could not save changes: {exc}', error=True, root=root)
            wx.MessageBox(str(exc), 'Project Setup', wx.OK | wx.ICON_ERROR, self)
        except Exception as exc:
            logging.getLogger(__name__).exception('Could not save project setup')
            self._show_feedback(f'Could not save changes: {exc}', error=True, root=root)
            wx.MessageBox(str(exc), 'Project Setup', wx.OK | wx.ICON_ERROR, self)
        finally:
            self.preview_button.Enable(self.snapshot is not None)
            self.apply_button.Enable(self.snapshot is not None)
