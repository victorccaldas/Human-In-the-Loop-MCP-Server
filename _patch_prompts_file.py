"""Patch _get_multiline_input_custom_prompts to read from custom_prompts.txt."""
import sys, os

path = r'C:\Users\Victor\Desktop\Projetos\Human-In-the-Loop-MCP-Server\human_loop_server.py'
with open(path, 'r', encoding='utf-8') as f:
    src = f.read()

# Find exact end of function by scanning from the def line
start_marker = 'def _get_multiline_input_custom_prompts() -> list:\n'
idx_start = src.find(start_marker)
assert idx_start != -1, 'Function not found'

# Find the end: next top-level def/class or EOF
import re
match = re.search(r'\ndef [a-zA-Z_]|\nclass [a-zA-Z_]', src[idx_start + len(start_marker):])
if match:
    idx_end = idx_start + len(start_marker) + match.start() + 1  # +1 to include the \n before 'def'
else:
    idx_end = len(src)

old_fn = src[idx_start:idx_end]
print(f'Replacing function from char {idx_start} to {idx_end}')
print('OLD:')
print(old_fn[:300])

new_fn = (
    'def _get_multiline_input_custom_prompts() -> list:\n'
    '    """Return list of custom prompt sentences.\n'
    '\n'
    '    Reads from custom_prompts.txt (one prompt per non-empty line) in the\n'
    '    same directory as this script. Falls back to --multiline_input_custom_prompts=\n'
    '    CLI args if the file does not exist or is empty.\n'
    '    """\n'
    '    # Try txt file first (allows editing without server restart)\n'
    '    try:\n'
    '        txt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "custom_prompts.txt")\n'
    '        if os.path.isfile(txt_path):\n'
    '            lines = [l.rstrip("\\n\\r") for l in open(txt_path, encoding="utf-8").readlines()]\n'
    '            prompts = [l for l in lines if l.strip()]\n'
    '            if prompts:\n'
    '                return prompts\n'
    '    except Exception:\n'
    '        pass\n'
    '\n'
    '    # Fallback: CLI args\n'
    '    prompts = []\n'
    '    try:\n'
    '        argv = sys.argv[1:]\n'
    '        for i, a in enumerate(argv):\n'
    '            if a.startswith("--multiline_input_custom_prompts=") or a.startswith("multiline_input_custom_prompts="):\n'
    '                prompts.append(a.split("=", 1)[1])\n'
    '            elif a in ("--multiline_input_custom_prompts", "multiline_input_custom_prompts"):\n'
    '                if i + 1 < len(argv):\n'
    '                    prompts.append(argv[i + 1])\n'
    '    except Exception:\n'
    '        pass\n'
    '    return prompts\n'
)

src = src[:idx_start] + new_fn + src[idx_end:]

with open(path, 'w', encoding='utf-8') as f:
    f.write(src)
print('Done. New function written.')
