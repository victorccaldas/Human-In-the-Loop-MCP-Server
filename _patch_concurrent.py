"""
Patch human_loop_server.py:
  1. Prompt area height 5 → 8 (50% increase)
  2. Persistent GUI root + threading.Event for concurrent dialogs
"""
import re, sys, os

path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'human_loop_server.py')
with open(path, 'r', encoding='utf-8') as f:
    src = f.read()

# ── 1. Prompt area height 5 → 8 ─────────────────────────────────────────────
# This is inside MultilineInputDialog prompt_container section
old1 = (
    '        prompt_text = tk.Text(\n'
    '            prompt_inner,\n'
    '            height=5,\n'
)
new1 = (
    '        prompt_text = tk.Text(\n'
    '            prompt_inner,\n'
    '            height=8,\n'
)
assert old1 in src, 'BLOCK 1 (prompt height) NOT FOUND'
src = src.replace(old1, new1, 1)
print('Block 1 (prompt height) done')

# ── 2. Add persistent root globals after _gui_lock ───────────────────────────
old2 = '# Global variable to ensure GUI is initialized properly\n_gui_initialized = False\n_gui_lock = threading.Lock()\n'
new2 = (
    '# Global variable to ensure GUI is initialized properly\n'
    '_gui_initialized = False\n'
    '_gui_lock = threading.Lock()\n'
    '\n'
    '# Persistent GUI thread and root for concurrent dialog support\n'
    '_persistent_root = None\n'
    '_persistent_gui_thread = None\n'
)
assert old2 in src, 'BLOCK 2 (globals) NOT FOUND'
src = src.replace(old2, new2, 1)
print('Block 2 (globals) done')

# ── 3. Add _ensure_persistent_root() after ensure_gui_initialized() ──────────
# Insert before configure_window_for_platform
old3 = 'def configure_window_for_platform(window):\n    """Apply platform-specific window configurations"""'
new3 = (
    'def _ensure_persistent_root():\n'
    '    """Ensure a single Tk root runs permanently on a dedicated GUI thread.\n'
    '\n'
    '    Using a persistent root with mainloop() allows multiple Toplevel dialogs\n'
    '    to be open simultaneously — each waits on its own threading.Event instead\n'
    '    of calling wait_window(), so the GUI thread never blocks.\n'
    '    """\n'
    '    global _persistent_root, _persistent_gui_thread\n'
    '    with _gui_lock:\n'
    '        if _persistent_gui_thread is not None and _persistent_gui_thread.is_alive():\n'
    '            return _persistent_root\n'
    '        ready = threading.Event()\n'
    '\n'
    '        def _gui_loop():\n'
    '            global _persistent_root\n'
    '            _persistent_root = tk.Tk()\n'
    '            _persistent_root.withdraw()  # hidden root\n'
    '            ready.set()\n'
    '            _persistent_root.mainloop()\n'
    '\n'
    '        _persistent_gui_thread = threading.Thread(target=_gui_loop, daemon=True, name="tkinter-gui-thread")\n'
    '        _persistent_gui_thread.start()\n'
    '        ready.wait(timeout=5)\n'
    '    return _persistent_root\n'
    '\n'
    '\n'
    'def configure_window_for_platform(window):\n'
    '    """Apply platform-specific window configurations"""'
)
assert old3 in src, 'BLOCK 3 (_ensure_persistent_root) NOT FOUND'
src = src.replace(old3, new3, 1)
print('Block 3 (_ensure_persistent_root) done')

# ── 4. Modify MultilineInputDialog.__init__ signature to accept done_event ───
old4 = '    def __init__(self, parent, title, prompt, default_value=""):'
new4 = '    def __init__(self, parent, title, prompt, default_value="", done_event=None):'
assert old4 in src, 'BLOCK 4 (init signature) NOT FOUND'
src = src.replace(old4, new4, 1)
print('Block 4 (init signature) done')

# ── 5. Store done_event early in __init__ (after self.result = None) ──────────
# Find a reliable anchor: the first line in __init__ body
old5 = '        self.result = None\n        \n        # Get theme colors\n        self.theme_colors = get_theme_colors()'
new5 = (
    '        self.result = None\n'
    '        self._done_event = done_event  # set by ok/cancel to unblock the caller\n'
    '        \n'
    '        # Get theme colors\n'
    '        self.theme_colors = get_theme_colors()'
)
assert old5 in src, 'BLOCK 5 (store done_event) NOT FOUND'
src = src.replace(old5, new5, 1)
print('Block 5 (store done_event) done')

# ── 6. Replace wait_window() at end of __init__ with a comment ───────────────
old6 = (
    '        # Wait for the dialog to complete\n'
    '        self.dialog.wait_window()'
)
new6 = (
    '        # No wait_window() here — the caller blocks on self._done_event instead,\n'
    '        # allowing multiple dialogs to be open simultaneously.'
)
assert old6 in src, 'BLOCK 6 (wait_window) NOT FOUND'
src = src.replace(old6, new6, 1)
print('Block 6 (remove wait_window) done')

# ── 7. ok_clicked: set done_event after destroy ───────────────────────────────
old7 = (
    '        self.result = base\n'
    '        self.dialog.destroy()\n'
    '\n'
    '    def cancel_clicked(self):'
)
new7 = (
    '        self.result = base\n'
    '        self.dialog.destroy()\n'
    '        if self._done_event is not None:\n'
    '            self._done_event.set()\n'
    '\n'
    '    def cancel_clicked(self):'
)
assert old7 in src, 'BLOCK 7 (ok done_event.set) NOT FOUND'
src = src.replace(old7, new7, 1)
print('Block 7 (ok done_event.set) done')

# ── 8. cancel_clicked: set done_event after destroy ──────────────────────────
old8 = (
    '        self.result = None\n'
    '        self.dialog.destroy()\n'
    '\n'
    '# MCP Tools'
)
new8 = (
    '        self.result = None\n'
    '        self.dialog.destroy()\n'
    '        if self._done_event is not None:\n'
    '            self._done_event.set()\n'
    '\n'
    '# MCP Tools'
)
assert old8 in src, 'BLOCK 8 (cancel done_event.set) NOT FOUND'
src = src.replace(old8, new8, 1)
print('Block 8 (cancel done_event.set) done')

# ── 9. Rewrite create_multiline_input_dialog ─────────────────────────────────
old9 = (
    'def create_multiline_input_dialog(title: str, prompt: str, default_value: str = ""):\n'
    '    """Create a multi-line text input dialog"""\n'
    '    try:\n'
    '        root = tk.Tk()\n'
    '        root.withdraw()\n'
    '        dialog = MultilineInputDialog(root, title, prompt, default_value)\n'
    '        result = dialog.result\n'
    '        root.destroy()\n'
    '        return result\n'
    '    except Exception as e:\n'
    '        print(f"Error in multiline dialog: {e}")\n'
    '        return None'
)
new9 = (
    'def create_multiline_input_dialog(title: str, prompt: str, default_value: str = ""):\n'
    '    """Create a multi-line text input dialog.\n'
    '\n'
    '    Uses a persistent Tk root on a dedicated GUI thread so multiple\n'
    '    calls can be active simultaneously without blocking each other.\n'
    '    """\n'
    '    try:\n'
    '        root = _ensure_persistent_root()\n'
    '        if root is None:\n'
    '            return None\n'
    '        done = threading.Event()\n'
    '        dialog_holder = [None]\n'
    '\n'
    '        def _create_on_gui_thread():\n'
    '            try:\n'
    '                dialog_holder[0] = MultilineInputDialog(\n'
    '                    root, title, prompt, default_value, done_event=done\n'
    '                )\n'
    '            except Exception as e:\n'
    '                print(f"Error creating dialog on GUI thread: {e}")\n'
    '                done.set()  # unblock caller even on error\n'
    '\n'
    '        root.after_idle(_create_on_gui_thread)\n'
    '        timeout = _get_tool_timeout()\n'
    '        done.wait(timeout=timeout)\n'
    '\n'
    '        if dialog_holder[0] is not None:\n'
    '            return dialog_holder[0].result\n'
    '        return None\n'
    '    except Exception as e:\n'
    '        print(f"Error in multiline dialog: {e}")\n'
    '        return None'
)
assert old9 in src, 'BLOCK 9 (create_multiline_input_dialog) NOT FOUND'
src = src.replace(old9, new9, 1)
print('Block 9 (create_multiline_input_dialog) done')

with open(path, 'w', encoding='utf-8') as f:
    f.write(src)
print('All patches applied.')
