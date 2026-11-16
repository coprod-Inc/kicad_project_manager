"""Shared native-control behaviour for the project-manager window."""
from __future__ import annotations

import weakref
import wx


class DropdownWheelFilter(wx.EventFilter):
    """Scroll the surrounding form instead of changing a dropdown by accident."""
    def __init__(self, owner):
        super().__init__()
        self.owner = weakref.ref(owner)

    def FilterEvent(self, event):
        if event.GetEventType() != wx.EVT_MOUSEWHEEL.typeId:
            return self.Event_Skip
        owner = self.owner()
        target = event.GetEventObject()
        if not owner or not isinstance(target, (wx.Choice, wx.ComboBox)):
            return self.Event_Skip
        ancestor = target
        while ancestor and ancestor is not owner:
            ancestor = ancestor.GetParent()
        if ancestor is not owner:
            return self.Event_Skip
        parent = target.GetParent()
        while parent and not isinstance(parent, wx.ScrolledWindow):
            parent = parent.GetParent()
        if parent:
            forwarded = event.Clone()
            forwarded.SetEventObject(parent)
            wx.PostEvent(parent, forwarded)
        return self.Event_Processed
