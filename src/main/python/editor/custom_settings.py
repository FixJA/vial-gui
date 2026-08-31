# SPDX-License-Identifier: GPL-2.0-or-later
"""
Editor for the VIA "custom channel" settings declared in the keyboard
definition's `menus` section (VIA v3 schema).

Wire format (same command ids as the legacy lighting commands):
    GET  [0x08, channel, value_id]          -> [0x08, channel, value_id, value...]
    SET  [0x07, channel, value_id, value...]
    SAVE [0x09, channel]

Supported item types: toggle, range, dropdown and color (hue/sat pair).
"offset" on a range is a vial-gui extension: the displayed value is the wire
value plus the offset (e.g. sleep timeout stored as minutes-1).
"""
from PyQt5 import QtCore
from PyQt5.QtCore import pyqtSignal, QObject
from PyQt5.QtWidgets import QVBoxLayout, QCheckBox, QGridLayout, QLabel, QWidget, QSizePolicy, QTabWidget, QSpinBox, \
    QComboBox, QHBoxLayout, QPushButton

from editor.basic_editor import BasicEditor
from util import tr
from vial_device import VialKeyboard


class CustomOption(QObject):

    changed = pyqtSignal()

    def __init__(self, item, container):
        super().__init__()

        self.row = container.rowCount()
        self.item = item
        # content = [name, channel, value_id, extra...]
        self.name = item["content"][0]
        self.channel = item["content"][1]
        self.value_id = item["content"][2]
        self.width = item.get("bytes", 1)
        self.container = container

        self.lbl = QLabel(item["label"])
        self.container.addWidget(self.lbl, self.row, 0)

        self.value = b""

    def wire_value(self):
        """ Current control state serialized as bytes to send to the keyboard """
        raise NotImplementedError

    def reload(self, keyboard):
        self.value = keyboard.custom_get_value(self.channel, self.value_id)[:self.width]

    def sync(self):
        self.value = self.wire_value()

    def dirty(self):
        return self.wire_value() != self.value

    def delete(self):
        self.lbl.hide()
        self.lbl.deleteLater()

    def on_change(self):
        self.changed.emit()


class ToggleOption(CustomOption):

    def __init__(self, item, container):
        super().__init__(item, container)

        self.checkbox = QCheckBox()
        self.checkbox.stateChanged.connect(self.on_change)
        self.container.addWidget(self.checkbox, self.row, 1)

    def wire_value(self):
        return bytes([int(self.checkbox.isChecked())])

    def reload(self, keyboard):
        super().reload(keyboard)
        self.checkbox.blockSignals(True)
        self.checkbox.setChecked(bool(self.value and self.value[0]))
        self.checkbox.blockSignals(False)

    def delete(self):
        super().delete()
        self.checkbox.hide()
        self.checkbox.deleteLater()


class RangeOption(CustomOption):

    def __init__(self, item, container):
        super().__init__(item, container)

        self.offset = item.get("offset", 0)
        lo, hi = item.get("options", [0, 255])

        self.spinbox = QSpinBox()
        self.spinbox.setMinimum(lo + self.offset)
        self.spinbox.setMaximum(hi + self.offset)
        self.spinbox.valueChanged.connect(self.on_change)
        self.container.addWidget(self.spinbox, self.row, 1)

    def wire_value(self):
        v = self.spinbox.value() - self.offset
        return v.to_bytes(self.width, byteorder="big")

    def reload(self, keyboard):
        super().reload(keyboard)
        v = int.from_bytes(self.value.ljust(self.width, b"\x00"), byteorder="big")
        self.spinbox.blockSignals(True)
        self.spinbox.setValue(v + self.offset)
        self.spinbox.blockSignals(False)

    def delete(self):
        super().delete()
        self.spinbox.hide()
        self.spinbox.deleteLater()


class DropdownOption(CustomOption):

    def __init__(self, item, container):
        super().__init__(item, container)

        # options are either ["Label", ...] (value = index) or
        # [["Label", value, ...], ...] (explicit wire bytes)
        self.choices = []
        self.combobox = QComboBox()
        for opt in item.get("options", []):
            if isinstance(opt, str):
                self.choices.append(bytes([len(self.choices)]))
                self.combobox.addItem(opt)
            else:
                self.choices.append(bytes(opt[1:]))
                self.combobox.addItem(opt[0])
        self.combobox.currentIndexChanged.connect(self.on_change)
        self.container.addWidget(self.combobox, self.row, 1)

    def wire_value(self):
        return self.choices[self.combobox.currentIndex()]

    def reload(self, keyboard):
        super().reload(keyboard)
        self.combobox.blockSignals(True)
        index = 0
        if self.value in self.choices:
            index = self.choices.index(self.value)
        self.combobox.setCurrentIndex(index)
        self.combobox.blockSignals(False)

    def delete(self):
        super().delete()
        self.combobox.hide()
        self.combobox.deleteLater()


class ColorOption(CustomOption):
    """ hue/sat byte pair rendered as two spinboxes """

    def __init__(self, item, container):
        super().__init__(item, container)

        w = QWidget()
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        self.hue = QSpinBox()
        self.hue.setRange(0, 255)
        self.hue.valueChanged.connect(self.on_change)
        self.sat = QSpinBox()
        self.sat.setRange(0, 255)
        self.sat.valueChanged.connect(self.on_change)
        layout.addWidget(self.hue)
        layout.addWidget(self.sat)
        w.setLayout(layout)
        self.container.addWidget(w, self.row, 1)

    def wire_value(self):
        return bytes([self.hue.value(), self.sat.value()])

    def reload(self, keyboard):
        super().reload(keyboard)
        hue = self.value[0] if len(self.value) > 0 else 0
        sat = self.value[1] if len(self.value) > 1 else 0
        self.hue.blockSignals(True)
        self.sat.blockSignals(True)
        self.hue.setValue(hue)
        self.sat.setValue(sat)
        self.hue.blockSignals(False)
        self.sat.blockSignals(False)

    def delete(self):
        super().delete()
        self.hue.hide()
        self.hue.deleteLater()
        self.sat.hide()
        self.sat.deleteLater()


OPTION_TYPES = {
    "toggle": ToggleOption,
    "range": RangeOption,
    "dropdown": DropdownOption,
    "color": ColorOption,
}


class CustomSettings(BasicEditor):

    def __init__(self):
        super().__init__()
        self.keyboard = None

        self.tabs_widget = QTabWidget()
        self.addWidget(self.tabs_widget)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.btn_save = QPushButton(tr("CustomSettings", "Save"))
        self.btn_save.clicked.connect(self.save_settings)
        buttons.addWidget(self.btn_save)
        self.btn_undo = QPushButton(tr("CustomSettings", "Undo"))
        self.btn_undo.clicked.connect(self.reload_settings)
        buttons.addWidget(self.btn_undo)
        self.addLayout(buttons)

        self.tabs = []
        self.misc_widgets = []

    @staticmethod
    def flatten_items(content):
        """ Flatten one optional level of item slices ({content: [...]}) """
        items = []
        for entry in content:
            if "type" in entry:
                items.append(entry)
            elif "content" in entry:
                items += [x for x in entry["content"] if "type" in x]
        return items

    def populate_tab(self, submenu, container):
        options = []
        for item in self.flatten_items(submenu["content"]):
            if item["type"] not in OPTION_TYPES:
                continue
            opt = OPTION_TYPES[item["type"]](item, container)
            options.append(opt)
            opt.changed.connect(self.on_change)
        return options

    def recreate_gui(self):
        # delete old GUI
        for tab in self.tabs:
            for field in tab:
                field.delete()
        self.tabs.clear()
        for w in self.misc_widgets:
            w.hide()
            w.deleteLater()
        self.misc_widgets.clear()
        while self.tabs_widget.count() > 0:
            self.tabs_widget.removeTab(0)

        # create new GUI: one tab per submenu of every menu
        for menu in self.keyboard.menus:
            for submenu in menu["content"]:
                items = self.flatten_items(submenu["content"])
                if not any(item["type"] in OPTION_TYPES for item in items):
                    continue

                w = QWidget()
                w.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Maximum)
                container = QGridLayout()
                w.setLayout(container)
                l = QVBoxLayout()
                l.addWidget(w)
                l.setAlignment(w, QtCore.Qt.AlignHCenter)
                w2 = QWidget()
                w2.setLayout(l)
                self.misc_widgets += [w, w2]
                self.tabs_widget.addTab(w2, submenu.get("label", menu.get("label", "")))
                self.tabs.append(self.populate_tab(submenu, container))

    def reload_settings(self):
        self.recreate_gui()

        for tab in self.tabs:
            for field in tab:
                field.reload(self.keyboard)

        self.on_change()

    def on_change(self):
        for x, tab in enumerate(self.tabs):
            title = self.tabs_widget.tabText(x).rstrip("*")
            if any(opt.dirty() for opt in tab):
                title += "*"
            self.tabs_widget.setTabText(x, title)

        changed = any(opt.dirty() for tab in self.tabs for opt in tab)
        self.btn_save.setEnabled(changed)

    def rebuild(self, device):
        super().rebuild(device)
        if self.valid():
            self.keyboard = device.keyboard
            self.reload_settings()

    def save_settings(self):
        channels = set()
        for tab in self.tabs:
            for opt in tab:
                if opt.dirty():
                    self.keyboard.custom_set_value(opt.channel, opt.value_id, opt.wire_value())
                    opt.sync()
                    channels.add(opt.channel)
        for channel in channels:
            self.keyboard.custom_save(channel)
        self.on_change()

    def valid(self):
        return isinstance(self.device, VialKeyboard) and \
               (self.device.keyboard and self.device.keyboard.menus)
