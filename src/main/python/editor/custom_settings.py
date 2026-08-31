# SPDX-License-Identifier: GPL-2.0-or-later
"""
Editor for the VIA "custom channel" settings declared in the keyboard
definition's `menus` section (VIA v3 schema).

Wire format (same command ids as the legacy lighting commands):
    GET  [0x08, channel, value_id]          -> [0x08, channel, value_id, value...]
    SET  [0x07, channel, value_id, value...]
    SAVE [0x09, channel]

Supported item types: toggle, range, dropdown and color (hue/sat pair).
Items may carry a "showIf" expression (VIA syntax: {id_name} references to
other commands' current values, ==/!=/&&/|| and parentheses) controlling
their visibility; hidden items are skipped when saving.
"""
import ast
import re

from PyQt5 import QtCore
from PyQt5.QtCore import pyqtSignal, QObject
from PyQt5.QtWidgets import QVBoxLayout, QCheckBox, QGridLayout, QLabel, QWidget, QSizePolicy, QTabWidget, QSpinBox, \
    QComboBox, QHBoxLayout, QPushButton

from editor.basic_editor import BasicEditor
from util import tr
from vial_device import VialKeyboard

_SHOWIF_REF = re.compile(r"\{(\w+)\}")


def _safe_eval_bool(expr):
    """Evaluate an integer/boolean expression over ==/!=/</<=/>/>=/and/or/not/parens only"""
    tree = ast.parse(expr, mode="eval")

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.BoolOp):
            values = [ev(v) for v in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not ev(node.operand)
        if isinstance(node, ast.Compare):
            left = ev(node.left)
            for op, comp in zip(node.ops, node.comparators):
                right = ev(comp)
                if isinstance(op, ast.Eq):
                    result = left == right
                elif isinstance(op, ast.NotEq):
                    result = left != right
                elif isinstance(op, ast.Lt):
                    result = left < right
                elif isinstance(op, ast.LtE):
                    result = left <= right
                elif isinstance(op, ast.Gt):
                    result = left > right
                elif isinstance(op, ast.GtE):
                    result = left >= right
                else:
                    raise ValueError("unsupported operator")
                if not result:
                    return False
                left = right
            return True
        if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
            return node.value
        raise ValueError("unsupported showIf expression: %s" % expr)

    return ev(tree)


def eval_showif(expr, values):
    """Evaluate a VIA showIf expression such as '{id_a} == 0 || {id_b} == 2'.

    values maps command names (content[0]) to their current integer wire value;
    unknown references read as 0. Syntax errors fail open (returns True).
    """
    if not expr:
        return True

    def repl(m):
        return str(values.get(m.group(1), 0))

    py = _SHOWIF_REF.sub(repl, expr.replace("||", " or ").replace("&&", " and "))
    try:
        return bool(_safe_eval_bool(py))
    except (ValueError, SyntaxError):
        return True


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

        self.showif = item.get("showIf")
        self.visible = True
        self._widgets = [self.lbl]

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

    def set_visible(self, visible):
        self.visible = visible
        for w in self._widgets:
            w.setVisible(visible)

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
        self._widgets.append(self.checkbox)

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
        self._widgets.append(self.spinbox)

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
        self._widgets.append(self.combobox)
        # set when the loaded value has no entry in this dropdown's choices
        # (possible for one of several controls sharing a command while
        # another variant is active); suppress dirty until the user picks
        self._unresolved = False

    def wire_value(self):
        return self.choices[self.combobox.currentIndex()]

    def reload(self, keyboard):
        super().reload(keyboard)
        self.combobox.blockSignals(True)
        index = 0
        self._unresolved = self.value not in self.choices
        if not self._unresolved:
            index = self.choices.index(self.value)
        self.combobox.setCurrentIndex(index)
        self.combobox.blockSignals(False)

    def on_change(self):
        self._unresolved = False
        super().on_change()

    def dirty(self):
        return not self._unresolved and super().dirty()

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
        self._widgets.append(w)

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

    def apply_visibility(self):
        """ Re-evaluate showIf for every item against current command values.

        Values come from the widget state for visible+dirty items and from the
        last value read from the keyboard otherwise, so stale hidden widgets
        (e.g. a variant dropdown whose choices don't contain the active wire
        value) never feed the expressions.
        """
        values = {}
        for tab in self.tabs:
            for opt in tab:
                v = opt.wire_value() if (opt.visible and opt.dirty()) else opt.value
                v = int.from_bytes(v or b"\x00", byteorder="big")
                # several controls may share one command (e.g. the color
                # variant dropdowns); the visible+dirty one reflects the
                # user's edit and must not be overwritten by hidden widgets
                if opt.name not in values or (opt.visible and opt.dirty()):
                    values[opt.name] = v
        for tab in self.tabs:
            for opt in tab:
                if opt.showif:
                    opt.set_visible(eval_showif(opt.showif, values))

    def on_change(self):
        self.apply_visibility()
        for x, tab in enumerate(self.tabs):
            title = self.tabs_widget.tabText(x).rstrip("*")
            if any(opt.dirty() and opt.visible for opt in tab):
                title += "*"
            self.tabs_widget.setTabText(x, title)

        changed = any(opt.dirty() and opt.visible for tab in self.tabs for opt in tab)
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
                if opt.dirty() and opt.visible:
                    self.keyboard.custom_set_value(opt.channel, opt.value_id, opt.wire_value())
                    opt.sync()
                    channels.add(opt.channel)
        for channel in channels:
            self.keyboard.custom_save(channel)
        self.on_change()

    def valid(self):
        return isinstance(self.device, VialKeyboard) and \
               (self.device.keyboard and self.device.keyboard.menus)
