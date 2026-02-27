"""
Fix concurrent dialogs: replace after(0, fn) with thread-safe queue + polling.
"""
path = r'C:\Users\Victor\Desktop\Projetos\Human-In-the-Loop-MCP-Server\human_loop_server.py'
with open(path, 'r', encoding='utf-8') as f:
    src = f.read()

# ── 1. Add _dialog_request_queue global next to _persistent_root ─────────────
old1 = (
    '# Persistent GUI thread and root for concurrent dialog support\n'
    '_persistent_root = None\n'
    '_persistent_gui_thread = None\n'
)
new1 = (
    '# Persistent GUI thread, root, and request queue for concurrent dialog support\n'
    '_persistent_root = None\n'
    '_persistent_gui_thread = None\n'
    '_dialog_request_queue = None  # thread-safe queue; worker threads post callables here\n'
)
assert old1 in src, 'BLOCK 1 not found'
src = src.replace(old1, new1, 1)
print('Block 1 done')

# ── 2. Rewrite _ensure_persistent_root to add queue + polling loop ─────────────
import re
fn_match = re.search(r'def _ensure_persistent_root\(\):', src)
assert fn_match, '_ensure_persistent_root not found'
start = fn_match.start()
# Find next top-level def/class
rest_match = re.search(r'\ndef [a-zA-Z_]|\nclass [a-zA-Z_]', src[start+1:])
end = start + 1 + rest_match.start() + 1  # include the leading \n
old2 = src[start:end]
print('OLD _ensure_persistent_root:')
print(old2[:300])

new2 = '''\
def _ensure_persistent_root():
    """Ensure a single Tk root + polling loop run on a dedicated GUI thread.

    Worker threads post callables to _dialog_request_queue; the GUI thread's
    polling loop dequeues and executes them, making it safe to create/destroy
    Toplevel widgets from multiple concurrent worker threads.
    """
    global _persistent_root, _persistent_gui_thread, _dialog_request_queue
    with _gui_lock:
        if _persistent_gui_thread is not None and _persistent_gui_thread.is_alive():
            return _persistent_root
        import queue as _queue_module
        _dialog_request_queue = _queue_module.Queue()
        ready = threading.Event()

        def _poll():
            """Drain the request queue and re-schedule (runs on GUI thread)."""
            try:
                while True:
                    fn = _dialog_request_queue.get_nowait()
                    try:
                        fn()
                    except Exception as exc:
                        print(f"Error in dialog request: {exc}")
            except Exception:
                pass  # queue.Empty — nothing to do
            if _persistent_root and _persistent_root.winfo_exists():
                _persistent_root.after(30, _poll)

        def _gui_loop():
            global _persistent_root
            _persistent_root = tk.Tk()
            _persistent_root.withdraw()  # hidden root; all dialogs are Toplevels
            ready.set()
            _persistent_root.after(30, _poll)  # start the polling loop
            _persistent_root.mainloop()

        _persistent_gui_thread = threading.Thread(
            target=_gui_loop, daemon=True, name="tkinter-gui-thread"
        )
        _persistent_gui_thread.start()
        ready.wait(timeout=5)
    return _persistent_root

'''

src = src[:start] + new2 + src[end:]
print('Block 2 done')

# ── 3. Update create_multiline_input_dialog to use the queue ─────────────────
old3 = (
    '        # after(0) fires at the next mainloop iteration (not "when idle"),\n'
    '        # ensuring the dialog is created immediately even if another dialog is active.\n'
    '        root.after(0, _create_on_gui_thread)'
)
new3 = (
    '        # Post to the GUI thread via the thread-safe queue.\n'
    '        # The polling loop (_poll) picks this up within ~30ms.\n'
    '        _dialog_request_queue.put(_create_on_gui_thread)'
)
assert old3 in src, 'BLOCK 3 not found'
src = src.replace(old3, new3, 1)
print('Block 3 done')

with open(path, 'w', encoding='utf-8') as f:
    f.write(src)
print('All patches applied.')
