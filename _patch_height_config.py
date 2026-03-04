"""Add persisted window height configurator to MultilineInputDialog."""
import re, os

path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'human_loop_server.py')
with open(path, 'r', encoding='utf-8') as f:
    src = f.read()

# ── 1. Add CONFIG_FILE + helper fns right after _dialog_request_queue global ──
old1 = ('_dialog_request_queue = None  # thread-safe queue; worker threads post callables here\n')
new1 = (
    '_dialog_request_queue = None  # thread-safe queue; worker threads post callables here\n'
    '\n'
    '# Persistent config file (same directory as this script)\n'
    '_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dialog_config.json")\n'
    '\n'
    'def _get_persisted_dialog_height() -> int:\n'
    '    """Return the saved dialog height, or the platform default (first run)."""\n'
    '    default = 530 if IS_WINDOWS else (510 if IS_MACOS else 480)\n'
    '    try:\n'
    '        if os.path.isfile(_CONFIG_FILE):\n'
    '            data = json.loads(open(_CONFIG_FILE, encoding="utf-8").read())\n'
    '            return int(data.get("dialog_height", default))\n'
    '    except Exception:\n'
    '        pass\n'
    '    return default\n'
    '\n'
    'def _save_persisted_dialog_height(height: int):\n'
    '    """Persist the dialog height to dialog_config.json."""\n'
    '    try:\n'
    '        data = {}\n'
    '        if os.path.isfile(_CONFIG_FILE):\n'
    '            try:\n'
    '                data = json.loads(open(_CONFIG_FILE, encoding="utf-8").read())\n'
    '            except Exception:\n'
    '                pass\n'
    '        data["dialog_height"] = height\n'
    '        with open(_CONFIG_FILE, "w", encoding="utf-8") as _f:\n'
    '            json.dump(data, _f, indent=2)\n'
    '    except Exception:\n'
    '        pass\n'
    '\n'
)
assert old1 in src, 'BLOCK 1 not found'
src = src.replace(old1, new1, 1)
print('Block 1 done')

# ── 2. Replace the three platform-specific geometry calls with persisted height ─
# The current code in MultilineInputDialog.__init__:
#   if IS_MACOS:
#       self.dialog.geometry("580x510")
#   elif IS_WINDOWS:
#       self.dialog.geometry("600x530")
#   else:
#       self.dialog.geometry("550x480")
old2 = (
    '        # Set size based on platform\n'
    '        if IS_MACOS:\n'
    '            self.dialog.geometry("580x510")\n'
    '        elif IS_WINDOWS:\n'
    '            self.dialog.geometry("600x530")\n'
    '        else:\n'
    '            self.dialog.geometry("550x480")\n'
    '        '
)
new2 = (
    '        # Width is platform-specific; height uses the persisted value\n'
    '        _dlg_width = 580 if IS_MACOS else (600 if IS_WINDOWS else 550)\n'
    '        _dlg_height = _get_persisted_dialog_height()\n'
    '        self.dialog.geometry(f"{_dlg_width}x{_dlg_height}")'
)
assert old2 in src, 'BLOCK 2 not found'
src = src.replace(old2, new2, 1)
print('Block 2 done')

# ── 3. Add height spinbox to button_frame (left side) ────────────────────────
old3 = (
    '        \n'
    '        # Create modern buttons\n'
    '        self.ok_button = create_modern_button(\n'
    '            button_frame, "OK", self.ok_clicked, "primary", self.theme_colors\n'
    '        )\n'
    '        self.ok_button.pack(side=tk.RIGHT, padx=(8, 0))'
)
new3 = (
    '        \n'
    '        # Height configurator (left side of button row)\n'
    '        height_ctrl_frame = tk.Frame(button_frame, bg=self.theme_colors["bg_primary"])\n'
    '        height_ctrl_frame.pack(side=tk.LEFT, padx=(0, 0))\n'
    '        tk.Label(\n'
    '            height_ctrl_frame,\n'
    '            text="Window height:",\n'
    '            bg=self.theme_colors["bg_primary"],\n'
    '            fg=self.theme_colors["fg_secondary"],\n'
    '            font=get_system_font(),\n'
    '        ).pack(side=tk.LEFT)\n'
    '        self._height_var = tk.StringVar(value=str(_dlg_height))\n'
    '        height_spin = tk.Spinbox(\n'
    '            height_ctrl_frame,\n'
    '            from_=300, to=1500, increment=10,\n'
    '            textvariable=self._height_var,\n'
    '            width=6,\n'
    '            bg=self.theme_colors["bg_secondary"],\n'
    '            fg=self.theme_colors["fg_primary"],\n'
    '            font=get_system_font(),\n'
    '            relief="flat",\n'
    '            buttonbackground=self.theme_colors["bg_secondary"],\n'
    '        )\n'
    '        height_spin.pack(side=tk.LEFT, padx=(6, 0))\n'
    '        height_spin.bind("<Return>", self._on_height_change)\n'
    '        height_spin.bind("<FocusOut>", self._on_height_change)\n'
    '\n'
    '        # Create modern buttons\n'
    '        self.ok_button = create_modern_button(\n'
    '            button_frame, "OK", self.ok_clicked, "primary", self.theme_colors\n'
    '        )\n'
    '        self.ok_button.pack(side=tk.RIGHT, padx=(8, 0))'
)
assert old3 in src, 'BLOCK 3 not found'
src = src.replace(old3, new3, 1)
print('Block 3 done')

# ── 4. Add _on_height_change method before center_window ─────────────────────
old4 = '    def center_window(self):\n        """Center the dialog window on screen"""'
new4 = (
    '    def _on_height_change(self, event=None):\n'
    '        """Resize the dialog to the new height and persist it."""\n'
    '        try:\n'
    '            h = int(self._height_var.get())\n'
    '            h = max(300, min(1500, h))\n'
    '            self._height_var.set(str(h))\n'
    '            geo = self.dialog.geometry()  # e.g. "600x530+200+100"\n'
    '            w = int(geo.split("x")[0])\n'
    '            self.dialog.geometry(f"{w}x{h}")\n'
    '            _save_persisted_dialog_height(h)\n'
    '        except Exception:\n'
    '            pass\n'
    '\n'
    '    def center_window(self):\n'
    '        """Center the dialog window on screen"""'
)
assert old4 in src, 'BLOCK 4 not found'
src = src.replace(old4, new4, 1)
print('Block 4 done')

with open(path, 'w', encoding='utf-8') as f:
    f.write(src)
print('All patches applied.')
