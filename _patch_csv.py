"""Patch human_loop_server.py for CSV-based custom_prompts with active flag."""
import re, sys, os

path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'human_loop_server.py')
with open(path, 'r', encoding='utf-8') as f:
    src = f.read()

# ── 1. Replace _get_multiline_input_custom_prompts ──────────────────────────
# Find function boundaries via regex
fn_match = re.search(r'def _get_multiline_input_custom_prompts\(\)[^\n]*\n', src)
assert fn_match, 'Function def not found'
idx_start = fn_match.start()
# Find next top-level def/class after the function body
rest_match = re.search(r'\ndef [a-zA-Z_]|\nclass [a-zA-Z_]', src[idx_start + 1:])
idx_end = (idx_start + 1 + rest_match.start() + 1) if rest_match else len(src)

old_fn = src[idx_start:idx_end]
print('=== OLD FUNCTION (first 200 chars) ===')
print(old_fn[:200])

new_fn = '''\
def _get_multiline_input_custom_prompts() -> list:
    """Return list of (active: bool, text: str) tuples for dialog checkboxes.

    Reads from custom_prompts.csv next to this script (format: active,prompt).
    Falls back to --multiline_input_custom_prompts= CLI args (all active=True).
    The CSV is re-read on every dialog open, so edits take effect immediately.
    """
    import csv as _csv
    # Try CSV file first
    try:
        csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "custom_prompts.csv")
        if os.path.isfile(csv_path):
            prompts = []
            with open(csv_path, encoding="utf-8", newline="") as fh:
                reader = _csv.DictReader(fh)
                for row in reader:
                    text = (row.get("prompt") or "").strip()
                    if text:
                        active = str(row.get("active", "1")).strip() not in ("0", "false", "False", "no")
                        prompts.append((active, text))
            if prompts:
                return prompts
    except Exception:
        pass

    # Fallback: CLI args (all pre-checked)
    prompts = []
    try:
        argv = sys.argv[1:]
        for i, a in enumerate(argv):
            if a.startswith("--multiline_input_custom_prompts=") or a.startswith("multiline_input_custom_prompts="):
                prompts.append((True, a.split("=", 1)[1]))
            elif a in ("--multiline_input_custom_prompts", "multiline_input_custom_prompts"):
                if i + 1 < len(argv):
                    prompts.append((True, argv[i + 1]))
    except Exception:
        pass
    return prompts
'''

src = src[:idx_start] + new_fn + src[idx_end:]
print('Block 1 (function) done')

# ── 2. Update checkbox creation in __init__ (iterate tuples) ────────────────
old2 = (
    '        # Custom prompt checkboxes (one per --multiline_input_custom_prompts arg)\n'
    '        self.prompt_vars = []\n'
    '        custom_prompts = _get_multiline_input_custom_prompts()\n'
    '        if custom_prompts:\n'
    '            checkbox_frame = tk.Frame(main_frame, bg=self.theme_colors["bg_secondary"])\n'
    '            checkbox_frame.grid(row=3, column=0, sticky="ew", pady=(0, 8))\n'
    '            for sentence in custom_prompts:\n'
    '                var = tk.BooleanVar(value=True)\n'
    '                cb = tk.Checkbutton(\n'
    '                    checkbox_frame,\n'
    '                    text=sentence,\n'
    '                    variable=var,\n'
    '                    bg=self.theme_colors["bg_secondary"],\n'
    '                    fg=self.theme_colors["fg_secondary"],\n'
    '                    selectcolor=self.theme_colors["bg_primary"],\n'
    '                    activebackground=self.theme_colors["bg_secondary"],\n'
    '                    font=get_system_font(),\n'
    '                    anchor="w",\n'
    '                    wraplength=520,\n'
    '                    justify="left",\n'
    '                )\n'
    '                cb.pack(fill="x", padx=8, pady=2)\n'
    '                self.prompt_vars.append(var)'
)

new2 = (
    '        # Custom prompt checkboxes loaded from custom_prompts.csv\n'
    '        self.prompt_vars = []\n'
    '        custom_prompts = _get_multiline_input_custom_prompts()\n'
    '        if custom_prompts:\n'
    '            checkbox_frame = tk.Frame(main_frame, bg=self.theme_colors["bg_secondary"])\n'
    '            checkbox_frame.grid(row=3, column=0, sticky="ew", pady=(0, 8))\n'
    '            for active, sentence in custom_prompts:\n'
    '                var = tk.BooleanVar(value=active)\n'
    '                cb = tk.Checkbutton(\n'
    '                    checkbox_frame,\n'
    '                    text=sentence,\n'
    '                    variable=var,\n'
    '                    bg=self.theme_colors["bg_secondary"],\n'
    '                    fg=self.theme_colors["fg_secondary"],\n'
    '                    selectcolor=self.theme_colors["bg_primary"],\n'
    '                    activebackground=self.theme_colors["bg_secondary"],\n'
    '                    font=get_system_font(),\n'
    '                    anchor="w",\n'
    '                    wraplength=520,\n'
    '                    justify="left",\n'
    '                )\n'
    '                cb.pack(fill="x", padx=8, pady=2)\n'
    '                self.prompt_vars.append(var)'
)

if old2 not in src:
    print('BLOCK 2 NOT FOUND')
    idx = src.find('Custom prompt checkboxes')
    print(repr(src[max(0,idx-30):idx+600]))
    sys.exit(1)
src = src.replace(old2, new2, 1)
print('Block 2 (checkbox init) done')

# ── 3. Update ok_clicked to use zip(custom_prompts, prompt_vars) ─────────────
old3 = (
    '        # Append checked custom prompts to the answer\n'
    '        custom_prompts = _get_multiline_input_custom_prompts()\n'
    '        checked = [custom_prompts[i] for i, var in enumerate(self.prompt_vars) if var.get()]\n'
    '        if checked:\n'
    '            separator = "\\n\\n" if base else ""\n'
    '            base = base + separator + "\\n\\n".join(checked)'
)

new3 = (
    '        # Append checked custom prompts to the answer\n'
    '        custom_prompts = _get_multiline_input_custom_prompts()\n'
    '        checked = [text for (_active, text), var in zip(custom_prompts, self.prompt_vars) if var.get()]\n'
    '        if checked:\n'
    '            separator = "\\n\\n" if base else ""\n'
    '            base = base + separator + "\\n\\n".join(checked)'
)

if old3 not in src:
    print('BLOCK 3 NOT FOUND')
    idx = src.find('Append checked custom prompts')
    print(repr(src[max(0,idx-30):idx+400]))
    sys.exit(1)
src = src.replace(old3, new3, 1)
print('Block 3 (ok_clicked) done')

with open(path, 'w', encoding='utf-8') as f:
    f.write(src)
print('All patches applied.')
