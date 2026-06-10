import ast

path = r'C:\Users\Administrator\Documents\zapretzen\src\zapret_zen\services\backend_worker.py'
with open(path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Remove trailing whitespace/newlines
lines = [l.rstrip('\n\r') for l in lines]

# Helper to get stripped content at a line
def stripped(i):
    return lines[i].strip() if 0 <= i < len(lines) else ''

# Fix 1: _sync_telegram_component_from_services - remove orphaned enabled.discard/autostart.discard for "zapret"
# Lines 109-110: orphaned from removed if block
for i in range(len(lines) - 1):
    if stripped(i) == 'enabled.discard("zapret")' and stripped(i+1) == 'autostart.discard("zapret")':
        # Check if this is inside _sync_telegram (after line 108)
        if i >= 108 and i <= 112:
            lines[i] = ''
            lines[i+1] = ''
        break

# Fix 2: _runtime_running_states - fix return type (tuple of 3 not 4)
for i in range(len(lines)):
    if stripped(i).startswith('def _runtime_running_states'):
        # Fix return type annotation
        lines[i] = lines[i].replace('tuple[dict[str, Any], bool, bool, bool]', 'tuple[dict[str, Any], bool, bool]')
        break

# Fix 3: Remove dead code lines 129-132 (after _runtime_running_states return)
for i in range(len(lines)):
    if stripped(i) == 'zapret_running = bool(states.get("zapret") and states["zapret"].status == "running")':
        # Check next lines for dead code
        for j in range(i+1, min(i+5, len(lines))):
            if stripped(j) == 'return False':
                lines[j] = ''
            elif stripped(j).startswith('return bool(getattr(state, "status", "") == "running")'):
                lines[j] = ''
        break

# Fix 4: _prepare_general_autotest_runtime - add _runtime_running_states call
for i in range(len(lines)):
    if stripped(i) == 'def _prepare_general_autotest_runtime(context) -> dict[str, Any]:':
        lines[i+1] = '    settings = context.settings.get()'
        lines.insert(i+2, '    _states, _any_running, zapret_running = _runtime_running_states(context)')
        lines[i+3] = '    restore = {'
        lines[i+4] = '        "was_running": bool(zapret_running),'
        break

# Fix 5: _restore_general_autotest_runtime - remove dead return True line
for i in range(len(lines)):
    if stripped(i) == 'def _restore_general_autotest_runtime(context, restore: dict[str, Any]) -> bool:':
        for j in range(i, min(i+20, len(lines))):
            if stripped(j) == 'return True' and stripped(j-1) == '' and stripped(j-2) == '_finish_zapret_reconfiguration(context, restart=True)':
                lines[j] = ''
        break

# Fix 6: _set_zapret_enabled_from_components - add _runtime_running_states call
for i in range(len(lines)):
    if stripped(i) == 'def _set_zapret_enabled_from_components(context, enabled_target: bool) -> dict[str, Any]:':
        lines.insert(i+3, '    _states, any_running, zapret_running = _runtime_running_states(context)')
        break

# Fix 7: toggle_master_runtime - fix indentation
in_block = False
for i in range(len(lines)):
    s = stripped(i)
    if s == 'if action == "toggle_master_runtime":':
        in_block = True
    if in_block and i > 0:
        # Find "else:" line and fix indentation after it
        if s == 'else:' and stripped(i-1) == 'mode = "disconnect"':
            # Fix the next lines
            pass

# Actually, let me take a completely different approach - write the whole file cleanly
# Read the source, keep non-VPN lines, fix damaged sections

# Actually the most reliable approach is to write the whole file from scratch based on the
# original, clean version. Let me read the whole thing first, identify ALL the issues,
# and fix them.

print(f"Total lines: {len(lines)}")
print("Fix script loaded - read the file successfully")
