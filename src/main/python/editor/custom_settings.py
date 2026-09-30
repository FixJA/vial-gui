# SPDX-License-Identifier: GPL-2.0-or-later
"""
Editor for the VIA "custom channel" settings declared in the keyboard
definition's `menus` section (VIA v3 schema).

Wire format (same command ids as the legacy lighting commands):
    GET  [0x08, channel, value_id]          -> [0x08, channel, value_id, value...]
    SET  [0x07, channel, value_id, value...]
    SAVE [0x09, channel]

Supported item types: toggle, range (rendered as a slider), dropdown and
color (hue/sat pair rendered as a swatch opening a color dialog).
Items may carry a "showIf" expression (VIA syntax: {id_name} references to
other commands' current values, ==/!=/&&/|| and parentheses) controlling
their visibility; hidden items are skipped when saving.
"""
import ast
import re
import sys

from PyQt5 import QtCore
from PyQt5.QtCore import pyqtSignal, QObject
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QVBoxLayout, QCheckBox, QGridLayout, QLabel, QWidget, QSizePolicy, QTabWidget, \
    QComboBox, QHBoxLayout, QPushButton, QSlider, QColorDialog

from editor.basic_editor import BasicEditor
from util import tr
from vial_device import VialKeyboard
from widgets.clickable_label import ClickableLabel

_SHOWIF_REF = re.compile(r"\{(\w+)\}")
_SHOWIF_WARNED = set()


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
    unknown references read as 0 (warned once, they usually mean a typo in the
    keyboard definition). Syntax errors fail open (returns True).
    """
    if not expr:
        return True

    unknown = []

    def repl(m):
        if m.group(1) not in values:
            unknown.append(m.group(1))
        return str(values.get(m.group(1), 0))

    py = _SHOWIF_REF.sub(repl, expr.replace("||", " or ").replace("&&", " and "))
    if unknown and expr not in _SHOWIF_WARNED:
        _SHOWIF_WARNED.add(expr)
        print("warning: showIf references unknown commands (%s) in %r" % (", ".join(sorted(set(unknown))), expr),
              file=sys.stderr)
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

    def set_value(self, data):
        """ Update the cached value and the widget state without HID traffic """
        self.value = data

    def reload(self, keyboard):
        # pad short device answers to the declared width so dirty comparison
        # stays width-consistent (a 1-byte answer to a 2-byte control would
        # otherwise read as permanently dirty)
        data = keyboard.custom_get_value(self.channel, self.value_id)[:self.width]
        self.set_value(data.ljust(self.width, b"\x00"))

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

    def set_value(self, data):
        super().set_value(data)
        self.checkbox.blockSignals(True)
        self.checkbox.setChecked(bool(data and data[0]))
        self.checkbox.blockSignals(False)

    def delete(self):
        super().delete()
        self.checkbox.hide()
        self.checkbox.deleteLater()


class RangeOption(CustomOption):

    def __init__(self, item, container):
        super().__init__(item, container)

        self.offset = item.get("offset", 0)
        self.unit = item.get("unit", "")
        lo, hi = item.get("options", [0, 255])

        w = QWidget()
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        self.slider = QSlider(QtCore.Qt.Horizontal)
        self.slider.setMinimum(lo + self.offset)
        self.slider.setMaximum(hi + self.offset)
        self.slider.setMinimumWidth(160)
        if hi - lo <= 10:
            # short ranges (e.g. the 0-5 halo brightness) get one tick per step
            self.slider.setTickPosition(QSlider.TicksBelow)
            self.slider.setTickInterval(1)
            self.slider.setPageStep(1)
        self.value_label = QLabel()
        self.value_label.setMinimumWidth(24)
        self.slider.valueChanged.connect(self.on_slider_changed)
        self.slider.valueChanged.connect(self.on_change)
        layout.addWidget(self.slider)
        layout.addWidget(self.value_label)
        w.setLayout(layout)
        self.container.addWidget(w, self.row, 1)
        self._widgets.append(w)
        self._display(self.slider.value())

    def on_slider_changed(self, value):
        self._display(value)

    def _display(self, value):
        self.value_label.setText(str(value) + self.unit)

    def wire_value(self):
        v = self.slider.value() - self.offset
        return v.to_bytes(self.width, byteorder="big")

    def set_value(self, data):
        super().set_value(data)
        v = int.from_bytes(data.ljust(self.width, b"\x00"), byteorder="big")
        self.slider.blockSignals(True)
        self.slider.setValue(v + self.offset)
        self.slider.blockSignals(False)
        self._display(v + self.offset)

    def delete(self):
        super().delete()
        self.slider.hide()
        self.slider.deleteLater()
        self.value_label.hide()
        self.value_label.deleteLater()


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
        # another variant is active); dirty is suppressed until the user picks
        self._unresolved = False
        # while unresolved an inert "(current: N)" item is parked at index 0
        # so no real choice is displayed wrongly; removed on first user pick
        self._placeholder = False

    def wire_value(self):
        return self.choices[self.combobox.currentIndex()]

    def _show_placeholder(self, data):
        text = tr("CustomSettings", "(current: %d)") % int.from_bytes(data, byteorder="big")
        if self._placeholder:
            self.combobox.setItemText(0, text)
        else:
            self.combobox.insertItem(0, text)
            self._placeholder = True
        self.combobox.setCurrentIndex(0)

    def _remove_placeholder(self):
        if self._placeholder:
            self.combobox.removeItem(0)
            self._placeholder = False

    def set_value(self, data):
        super().set_value(data)
        self.combobox.blockSignals(True)
        self._unresolved = data not in self.choices
        if self._unresolved:
            self._show_placeholder(data)
        else:
            self._remove_placeholder()
            self.combobox.setCurrentIndex(self.choices.index(data))
        self.combobox.blockSignals(False)

    def on_change(self):
        if self._placeholder:
            # dropping the placeholder shifts real options back by one so
            # currentIndex maps onto choices again
            self.combobox.blockSignals(True)
            index = max(0, self.combobox.currentIndex() - 1)
            self.combobox.removeItem(0)
            self.combobox.setCurrentIndex(index)
            self.combobox.blockSignals(False)
            self._placeholder = False
        self._unresolved = False
        super().on_change()

    def dirty(self):
        return not self._unresolved and super().dirty()

    def delete(self):
        super().delete()
        self.combobox.hide()
        self.combobox.deleteLater()


class ColorOption(CustomOption):
    """ hue/sat byte pair rendered as a swatch that opens a color dialog """

    def __init__(self, item, container):
        super().__init__(item, container)
        self.width = item.get("bytes", 2)
        if self.width != 2:
            raise ValueError("color controls carry a 2-byte hue/sat pair, got bytes=%d" % self.width)

        self.hue8 = 0
        self.sat8 = 0
        self.swatch = ClickableLabel()
        self.swatch.setMinimumSize(48, 22)
        self.swatch.clicked.connect(self.on_pick_color)
        self.container.addWidget(self.swatch, self.row, 1)
        self._widgets.append(self.swatch)
        self._update_swatch()

    def current_color(self):
        # value comes from the side-light brightness, not stored in the pair;
        # show the hue/sat at full value so dark settings stay previewable
        return QColor.fromHsvF(self.hue8 / 255, self.sat8 / 255, 1.0)

    def _update_swatch(self):
        self.swatch.setStyleSheet(
            "QLabel { background-color: %s; border: 1px solid #888; }" % self.current_color().name())

    def on_pick_color(self):
        self.dlg_color = QColorDialog()
        self.dlg_color.setModal(True)
        self.dlg_color.finished.connect(self.on_color_picked)
        self.dlg_color.setCurrentColor(self.current_color())
        self.dlg_color.show()

    def on_color_picked(self):
        color = self.dlg_color.selectedColor()
        if not color.isValid():
            return
        h, s, v, a = color.getHsvF()
        if s > 0:
            self.hue8 = int(h * 255 + 0.5) % 256
        else:
            # achromatic: Qt reports an arbitrary hue, keep the stored one
            pass
        self.sat8 = max(0, min(255, int(s * 255 + 0.5)))
        self._update_swatch()
        self.on_change()

    def wire_value(self):
        return bytes([self.hue8, self.sat8])

    def set_value(self, data):
        super().set_value(data)
        self.hue8 = data[0] if len(data) > 0 else 0
        self.sat8 = data[1] if len(data) > 1 else 0
        self._update_swatch()

    def delete(self):
        super().delete()
        self.swatch.hide()
        self.swatch.deleteLater()


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

        Values come from the widget state for visible items and from the last
        value read from the keyboard otherwise, so stale hidden widgets (e.g.
        a variant dropdown whose choices don't contain the active wire value)
        never feed the expressions.
        """
        values = {}
        # several controls may share one command (e.g. the color variant
        # dropdowns); the state of the visible control must always win over
        # cached values of hidden ones, whether or not it is dirty
        for tab in self.tabs:
            for opt in tab:
                if not opt.visible or opt.name in values:
                    continue
                v = opt.wire_value() if opt.dirty() else opt.value
                values[opt.name] = int.from_bytes(v or b"\x00", byteorder="big")
        for tab in self.tabs:
            for opt in tab:
                if opt.name not in values:
                    values[opt.name] = int.from_bytes(opt.value or b"\x00", byteorder="big")
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

    @staticmethod
    def _flash_btn(btn, text):
        """Show `text` on the button for a moment, then restore its label"""
        btn.setText(text)
        QtCore.QTimer.singleShot(1500, lambda: btn.setText(tr("CustomSettings", "Save")))

    def save_settings(self):
        try:
            channels = set()
            saved = {}
            for tab in self.tabs:
                for opt in tab:
                    if opt.dirty() and opt.visible:
                        data = opt.wire_value()
                        self.keyboard.custom_set_value(opt.channel, opt.value_id, data)
                        saved[opt.name] = data
                        channels.add(opt.channel)
            # every control sharing a saved command name (e.g. the variant
            # dropdowns) must follow the saved value instead of its stale cache
            if saved:
                for tab in self.tabs:
                    for opt in tab:
                        if opt.name in saved:
                            opt.set_value(saved[opt.name])
            for channel in channels:
                self.keyboard.custom_save(channel)
            # re-read everything the firmware actually kept: the device is the
            # source of truth and may clamp values below what was sent
            if channels:
                for tab in self.tabs:
                    for opt in tab:
                        opt.reload(self.keyboard)
        except Exception as e:
            print("custom settings save failed: %s" % e, file=sys.stderr)
            self._flash_btn(self.btn_save, tr("CustomSettings", "Save failed"))
            return
        if channels:
            self._flash_btn(self.btn_save, tr("CustomSettings", "Saved ✓"))
        self.on_change()

    def valid(self):
        return isinstance(self.device, VialKeyboard) and \
               (self.device.keyboard and self.device.keyboard.menus)
