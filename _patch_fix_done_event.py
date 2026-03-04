"""Fix: move _done_event assignment from ChoiceDialog to MultilineInputDialog, +30px height."""
import re, os

path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'human_loop_server.py')
with open(path, 'r', encoding='utf-8') as f:
    src = f.read()

# ── 1. Remove wrongly-inserted _done_event line from ChoiceDialog ────────────
# Identify exact ChoiceDialog body start
cd_match = re.search(r'class ChoiceDialog:', src)
assert cd_match, 'ChoiceDialog not found'
cd_start = cd_match.start()

ml_match = re.search(r'class MultilineInputDialog:', src)
assert ml_match, 'MultilineInputDialog not found'
ml_start = ml_match.start()

# Find _done_event in ChoiceDialog region only
cd_src = src[cd_start:ml_start]
if 'self._done_event = done_event' in cd_src:
    # Remove that one line from the ChoiceDialog region
    # The wrongly-added block was: 'self.result = None\n        self._done_event = done_event  # ....\n        \n        # Get theme colors'
    # We just need to remove the one inserted line
    wrong = '\n        self._done_event = done_event  # set by ok/cancel to unblock the caller'
    assert wrong in cd_src, 'wrong done_event line not found in ChoiceDialog region'
    new_cd_src = cd_src.replace(wrong, '', 1)
    src = src[:cd_start] + new_cd_src + src[ml_start:]
    print('Block 1 (remove wrong ChoiceDialog assignment) done')
else:
    print('Block 1: already absent from ChoiceDialog, skipping')

# ── 2. Insert _done_event assignment in MultilineInputDialog.__init__ ─────────
# Find new ml_start after possible src change
ml_match2 = re.search(r'class MultilineInputDialog:', src)
ml_start2 = ml_match2.start()
ml_src = src[ml_start2:]
# Find self.result = None in MultilineInputDialog (first occurrence)
rn_idx = ml_src.find('self.result = None\n')
assert rn_idx != -1, 'self.result = None not found in MultilineInputDialog'
# Insert after that line, before the blank/Get-theme-colors line
abs_pos = ml_start2 + rn_idx + len('self.result = None\n')
insert_line = '        self._done_event = done_event  # set by ok/cancel to unblock the caller\n'
if 'self._done_event = done_event' not in ml_src:
    src = src[:abs_pos] + insert_line + src[abs_pos:]
    print('Block 2 (insert _done_event in MultilineInputDialog) done')
else:
    print('Block 2: already present in MultilineInputDialog, skipping')

# ── 3. Increase window height by 30px ────────────────────────────────────────
# 600x500 -> 600x530, 580x480 -> 580x510, 550x450 -> 550x480
replacements = [
    ('self.dialog.geometry("580x480")', 'self.dialog.geometry("580x510")'),
    ('self.dialog.geometry("600x500")', 'self.dialog.geometry("600x530")'),
    ('self.dialog.geometry("550x450")', 'self.dialog.geometry("550x480")'),
]
for old, new in replacements:
    # Only replace within MultilineInputDialog
    ml_match3 = re.search(r'class MultilineInputDialog:', src)
    ml_start3 = ml_match3.start()
    ml_src3 = src[ml_start3:]
    if old in ml_src3:
        new_ml = ml_src3.replace(old, new, 1)
        src = src[:ml_start3] + new_ml
        print(f'Block 3: {old} -> {new}')
    else:
        print(f'Block 3: {old!r} not found in MultilineInputDialog, checking anywhere...')
        if old in src:
            src = src.replace(old, new, 1)
            print(f'  replaced globally')

with open(path, 'w', encoding='utf-8') as f:
    f.write(src)
print('Done.')
