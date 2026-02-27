"""Patch human_loop_server.py with the multiline_input_custom_prompts feature."""
import sys

path = r'C:\Users\Victor\Desktop\Projetos\Human-In-the-Loop-MCP-Server\human_loop_server.py'
with open(path, 'r', encoding='utf-8') as f:
    src = f.read()

# ── 1. Replace INJECTION_ENV_KEYS + _get_multiline_input_injection ──────────
old1 = (
    '# Keys for environment-based injection support. Supports uppercase or lowercase name.\n'
    'INJECTION_ENV_KEYS = ("POST_MULTILINE_INPUT_INJECTION", "post_multiline_input_injection")\n'
    '\n'
    'def _get_multiline_input_injection() -> str:\n'
    '    """Return the configured injection sentence read only from CLI args.\n'
    '\n'
    '    Expected CLI forms:\n'
    '      --post_multiline_input_injection=...\n'
    '      --post-multiline-input-injection=...\n'
    '      post_multiline_input_injection=...\n'
    '    If not present, returns empty string.\n'
    '    """\n'
    '    try:\n'
    '        argv = sys.argv[1:]\n'
    '        for i, a in enumerate(argv):\n'
    '            if a.startswith("--post_multiline_input_injection=") or a.startswith("post_multiline_input_injection=") or a.startswith("--post-multiline-input-injection=") or a.startswith("post-multiline-input-injection="):\n'
    '                return a.split("=", 1)[1]\n'
    '            if a in ("--post_multiline_input_injection", "--post-multiline-input-injection", "post_multiline_input_injection", "post-multiline-input-injection"):\n'
    '                if i + 1 < len(argv):\n'
    '                    return argv[i + 1]\n'
    '    except Exception:\n'
    '        pass\n'
    '\n'
    '    return ""'
)

new1 = (
    'def _get_multiline_input_custom_prompts() -> list:\n'
    '    """Return list of custom prompt sentences from CLI args.\n'
    '\n'
    '    Each --multiline_input_custom_prompts=<sentence> arg adds one prompt.\n'
    '    """\n'
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
    '    return prompts'
)

if old1 not in src:
    print("BLOCK 1 NOT FOUND — printing nearby lines for diagnosis")
    idx = src.find('INJECTION_ENV_KEYS')
    print(repr(src[max(0,idx-100):idx+500]))
    sys.exit(1)
src = src.replace(old1, new1, 1)
print('Block 1 done')

# ── 2. Insert checkboxes frame between text_container and button_frame ───────
old2 = (
    '        # Set default value with better formatting\n'
    '        if default_value:\n'
    '            self.text_widget.insert("1.0", default_value)\n'
    '        \n'
    '        # Modern button frame\n'
    '        button_frame = tk.Frame(main_frame, bg=self.theme_colors["bg_primary"])\n'
    '        button_frame.grid(row=3, column=0, sticky="ew")'
)

new2 = (
    '        # Set default value with better formatting\n'
    '        if default_value:\n'
    '            self.text_widget.insert("1.0", default_value)\n'
    '\n'
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
    '                self.prompt_vars.append(var)\n'
    '\n'
    '        # Modern button frame\n'
    '        button_frame = tk.Frame(main_frame, bg=self.theme_colors["bg_primary"])\n'
    '        button_frame.grid(row=4, column=0, sticky="ew")'
)

if old2 not in src:
    print("BLOCK 2 NOT FOUND — printing nearby lines for diagnosis")
    idx = src.find('# Set default value with better formatting')
    print(repr(src[max(0,idx-50):idx+500]))
    sys.exit(1)
src = src.replace(old2, new2, 1)
print('Block 2 done')

# ── 3. Update ok_clicked to append checked prompts ───────────────────────────
old3 = (
    '\n    def ok_clicked(self):\n'
    '        # Cancel the periodic reminder before closing\n'
    '        try:\n'
    '            if self._reminder_id is not None:\n'
    '                self.dialog.after_cancel(self._reminder_id)\n'
    '        except Exception:\n'
    '            pass\n'
    '        self.result = self.text_widget.get("1.0", tk.END).strip()\n'
    '        self.dialog.destroy()'
)

new3 = (
    '\n    def ok_clicked(self):\n'
    '        # Cancel the periodic reminder before closing\n'
    '        try:\n'
    '            if self._reminder_id is not None:\n'
    '                self.dialog.after_cancel(self._reminder_id)\n'
    '        except Exception:\n'
    '            pass\n'
    '        base = self.text_widget.get("1.0", tk.END).strip()\n'
    '        # Append checked custom prompts to the answer\n'
    '        custom_prompts = _get_multiline_input_custom_prompts()\n'
    '        checked = [custom_prompts[i] for i, var in enumerate(self.prompt_vars) if var.get()]\n'
    '        if checked:\n'
    '            separator = "\\n\\n" if base else ""\n'
    '            base = base + separator + "\\n\\n".join(checked)\n'
    '        self.result = base\n'
    '        self.dialog.destroy()'
)

if old3 not in src:
    print("BLOCK 3 NOT FOUND — printing nearby lines for diagnosis")
    idx = src.find('def ok_clicked(self):')
    print(repr(src[max(0,idx-50):idx+500]))
    sys.exit(1)
src = src.replace(old3, new3, 1)
print('Block 3 done')

# ── 4. Remove server-side injection block from get_multiline_input tool ──────
# Build the exact string from known content
old4 = (
    '        if result is not None:\n'
    '            # Append configured injection sentence to the user\'s submitted answer (if any).\n'
    '            injection = _get_multiline_input_injection()\n'
    '            final_value = result\n'
    '            if injection:\n'
    '                if final_value:\n'
    '                    final_value = final_value + "\\n\\n" + injection\n'
    '                else:\n'
    '                    final_value = injection\n'
    '\n'
    '            if ctx:\n'
    '                await ctx.info(f"User provided multiline input ({len(final_value)} characters)")\n'
    '            return {\n'
    '                "success": True,\n'
    '                "user_input": final_value,\n'
    '                "character_count": len(final_value),\n'
    '                "line_count": len(final_value.split(\'\\n\')),'
)

new4 = (
    '        if result is not None:\n'
    '            if ctx:\n'
    '                await ctx.info(f"User provided multiline input ({len(result)} characters)")\n'
    '            return {\n'
    '                "success": True,\n'
    '                "user_input": result,\n'
    '                "character_count": len(result),\n'
    '                "line_count": len(result.split(\'\\n\')),'
)

if old4 not in src:
    print("BLOCK 4 NOT FOUND — printing nearby lines for diagnosis")
    idx = src.find('_get_multiline_input_injection()')
    if idx == -1:
        print("  _get_multiline_input_injection() not found at all — may already be removed or different text")
        # Try a broader search
        idx2 = src.find('Append configured injection')
        print(f"  'Append configured injection' at index {idx2}")
        if idx2 != -1:
            print(repr(src[max(0,idx2-100):idx2+600]))
    else:
        print(repr(src[max(0,idx-200):idx+600]))
    sys.exit(1)
src = src.replace(old4, new4, 1)
print('Block 4 done')

with open(path, 'w', encoding='utf-8') as f:
    f.write(src)
print('All patches applied successfully.')
