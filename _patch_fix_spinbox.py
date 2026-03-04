"""
Fix mis-placed spinbox: remove from ModernInputDialog, insert into MultilineInputDialog.
"""
import re, os

path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'human_loop_server.py')
with open(path, 'r', encoding='utf-8') as f:
    src = f.read()

# Build class boundary map
classes = [(m.start(), m.group()) for m in re.finditer(r'^class \w+', src, re.MULTILINE)]
def class_body(name):
    start = next(s for s, n in classes if name in n)
    nexts = [s for s, n in classes if s > start]
    end = nexts[0] if nexts else len(src)
    return start, end

mi_s, mi_e = class_body('ModernInputDialog')
ml_s, ml_e = class_body('MultilineInputDialog')

print(f'ModernInputDialog: L{src[:mi_s].count(chr(10))+1} pos {mi_s}-{mi_e}')
print(f'MultilineInputDialog: L{src[:ml_s].count(chr(10))+1} pos {ml_s}-{ml_e}')

# ── 1. Remove mis-inserted height block from ModernInputDialog ────────────────
mi_body = src[mi_s:mi_e]
height_block = (
    '\n'
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
)

if height_block in mi_body:
    new_mi_body = mi_body.replace(height_block, '', 1)
    src = src[:mi_s] + new_mi_body + src[mi_e:]
    print('Block 1 (removed from ModernInputDialog) done')
else:
    print('Block 1: height block NOT found in ModernInputDialog — checking globally')
    if height_block in src[:ml_s]:
        first_pos = src.find(height_block)
        src = src[:first_pos] + src[first_pos+len(height_block):]
        print('  Removed from before MultilineInputDialog')
    else:
        print('  WARNING: height block not found anywhere before MultilineInputDialog')

# Recalculate MultilineInputDialog boundaries after the removal
ml_s = src.find('class MultilineInputDialog')
cl2 = [(m.start(), m.group()) for m in re.finditer(r'^class \w+', src, re.MULTILINE)]
nexts2 = [s for s, n in cl2 if s > ml_s]
ml_e = nexts2[0] if nexts2 else len(src)
ml_body = src[ml_s:ml_e]
print(f'MultilineInputDialog after removal: pos {ml_s}-{ml_e}, len={len(ml_body)}')

# ── 2. Insert height block correctly in MultilineInputDialog ─────────────────
# Find the unique button frame section within MultilineInputDialog ONLY
idx = ml_body.find('button_frame.grid(row=4, column=0')
assert idx != -1, 'button_frame row=4 not found in MultilineInputDialog'
# Find the start of '# Create modern buttons' after that point
idx2 = ml_body.find('# Create modern buttons\n', idx)
assert idx2 != -1, '# Create modern buttons not found after button_frame'
# The insertion point: just before '        # Create modern buttons'
insert_at = ml_s + idx2

insert_text = height_block[1:]  # remove leading \n (already have newline before)

src = src[:insert_at] + insert_text + src[insert_at:]
print('Block 2 (inserted into MultilineInputDialog) done')

# ── 3. Also remove mis-placed _on_height_change from ModernInputDialog if present ──
# Recalculate ModernInputDialog boundaries
mi_s2, mi_e2 = class_body('ModernInputDialog') if False else (0, 0)
# Actually do it by checking current src
cl3 = [(m.start(), m.group()) for m in re.finditer(r'^class \w+', src, re.MULTILINE)]
mi_s3 = next(s for s, n in cl3 if 'ModernInputDialog' in n and 'Dialog' in n and 'Confirmation' not in n and 'Info' not in n)
nexts3 = [s for s, n in cl3 if s > mi_s3]
mi_e3 = nexts3[0] if nexts3 else len(src)
mi_body3 = src[mi_s3:mi_e3]

on_height = (
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
)

if on_height in mi_body3:
    src = src[:mi_s3] + mi_body3.replace(on_height, '', 1) + src[mi_e3:]
    print('Block 3 (_on_height_change removed from ModernInputDialog) done')
else:
    print('Block 3: _on_height_change not in ModernInputDialog (OK)')

# Verify it's in MultilineInputDialog
ml_s_final = src.find('class MultilineInputDialog')
cl4 = [(m.start(), m.group()) for m in re.finditer(r'^class \w+', src, re.MULTILINE)]
nexts4 = [s for s, n in cl4 if s > ml_s_final]
ml_e_final = nexts4[0] if nexts4 else len(src)
ml_body_final = src[ml_s_final:ml_e_final]
print('_on_height_change in MultilineInputDialog:', '_on_height_change' in ml_body_final)
print('height_spin in MultilineInputDialog:', 'height_spin' in ml_body_final)

with open(path, 'w', encoding='utf-8') as f:
    f.write(src)
print('Done.')
