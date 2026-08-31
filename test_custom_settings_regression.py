# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless regression test for the Custom Settings tab (halo light fixes).

Covers:
  1. saving a color-variant change keeps the Palette/Static row below stable
     (shared-command value collection + save propagation)
  2. "color" items use a 2-byte hue/sat value: never perpetually dirty,
     saving does not wipe saturation
  3. "range" items render as a slider with the right wire values
  4. the color swatch dialog callback maps QColor -> hue/sat bytes

Run:  venv/Scripts/python.exe test_custom_settings_regression.py
"""
import json
import os
import sys
import traceback
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src", "main", "python"))

from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QColor

app = QApplication([])

from editor.custom_settings import CustomSettings, RangeOption, ColorOption  # noqa: E402
from vial_device import VialKeyboard  # noqa: E402

VIAL_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                         "vial-qmk", "keyboards", "nuphy", "halo65v2", "ansi",
                         "keymaps", "vial", "vial.json")


class FakeKeyboard:

    def __init__(self, menus):
        self.menus = menus
        self.store = {
            (0, 10): b"\x00",             # side mode: Wave
            (0, 11): b"\x00",             # speed
            (0, 12): b"\x00",             # side color: Palette
            (0, 13): b"\x03",             # brightness
            (0, 14): bytes([85, 255]),    # static color hue/sat
            (0, 22): b"\x04",             # palette color
            (0, 31): b"\x64",             # battery indicator brightness
        }
        self.saved_channels = []

    def custom_get_value(self, channel, value_id):
        return self.store.get((channel, value_id), b"\x00")

    def custom_set_value(self, channel, value_id, data):
        self.store[(channel, value_id)] = bytes(data)

    def custom_save(self, channel):
        self.saved_channels.append(channel)


def make_cs():
    with open(VIAL_JSON, encoding="utf-8") as f:
        menus = json.load(f)["menus"]
    kb = FakeKeyboard(menus)
    device = VialKeyboard.__new__(VialKeyboard)
    device.keyboard = kb
    cs = CustomSettings()
    cs.rebuild(device)
    return cs, kb


def find(cs, label, visible=None):
    out = [opt for tab in cs.tabs for opt in tab if opt.item["label"] == label]
    if visible is not None:
        out = [o for o in out if o.visible == visible]
    assert out, "no option labeled %r" % label
    return out[0]


def test_variant_save_keeps_rows_stable():
    cs, kb = make_cs()
    palette, static = find(cs, "Palette Color"), find(cs, "Static Color")
    assert palette.visible and not static.visible

    variant = find(cs, "Color Variant", visible=True)
    variant.combobox.setCurrentIndex(1)  # Custom Color
    assert static.visible and not palette.visible
    assert cs.btn_save.isEnabled()

    cs.save_settings()
    assert kb.store[(0, 12)] == b"\x01"
    assert kb.store[(0, 14)] == bytes([85, 255]), "save must not touch the static color"
    assert static.visible, "Static Color row disappeared after save"
    assert not palette.visible
    assert not static.dirty(), "color item must not be perpetually dirty (2-byte width)"
    assert not cs.btn_save.isEnabled()

    # switching back to Palette Color must equally survive the save
    variant.combobox.setCurrentIndex(0)
    assert palette.visible and not static.visible
    cs.save_settings()
    assert palette.visible, "Palette Color row disappeared after save"
    assert not static.visible

    # switching to Mix mode shows the Dual Color dropdown in the saved state
    find(cs, "Side Mode").combobox.setCurrentIndex(2)
    dual = find(cs, "Dual Color", visible=True)
    assert not dual.dirty(), "shared-command dropdown must follow the saved value"
    assert dual.combobox.currentIndex() == 0  # last save was b"\x00" -> Cyan + Blue


def test_range_renders_as_slider():
    cs, kb = make_cs()
    bright = find(cs, "Brightness")
    assert isinstance(bright, RangeOption)
    assert (bright.slider.minimum(), bright.slider.maximum()) == (0, 5)
    assert bright.slider.value() == 3 and bright.value_label.text() == "3"
    assert not bright.dirty()

    bright.slider.setValue(4)
    assert bright.dirty() and bright.wire_value() == b"\x04"
    cs.save_settings()
    assert kb.store[(0, 13)] == b"\x04"
    assert not bright.dirty() and bright.value_label.text() == "4"

    bat = find(cs, "Battery indicator brightness")
    assert bat.slider.maximum() == 100
    bat.slider.setValue(77)
    cs.save_settings()
    assert kb.store[(0, 31)] == b"\x4d"


def test_color_picker_mapping():
    cs, kb = make_cs()
    find(cs, "Color Variant", visible=True).combobox.setCurrentIndex(1)
    static = find(cs, "Static Color")
    assert static.visible and isinstance(static, ColorOption)
    assert static.width == 2
    assert static.value == bytes([85, 255]) and not static.dirty()

    static.dlg_color = types.SimpleNamespace(
        selectedColor=lambda: QColor.fromHsvF(0.5, 1.0, 1.0))
    static.on_color_picked()
    assert static.wire_value() == bytes([128, 255])
    assert static.dirty()
    cs.save_settings()
    assert kb.store[(0, 14)] == bytes([128, 255])

    # achromatic pick keeps the stored hue
    static.dlg_color = types.SimpleNamespace(
        selectedColor=lambda: QColor.fromHsvF(0.9, 0.0, 1.0))
    static.on_color_picked()
    assert static.wire_value() == bytes([128, 0])


def main():
    tests = [test_variant_save_keeps_rows_stable,
             test_range_renders_as_slider,
             test_color_picker_mapping]
    failed = 0
    for t in tests:
        try:
            t()
            print("PASS %s" % t.__name__)
        except Exception:
            failed += 1
            print("FAIL %s" % t.__name__)
            traceback.print_exc()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
