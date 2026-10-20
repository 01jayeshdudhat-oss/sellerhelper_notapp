import re
from pathlib import Path

def should_skip(path: Path):
    parts = [p.lower() for p in path.parts]
    if 'migrations' in parts: return True
    if '.venv' in parts or 'venv' in parts or 'env' in parts: return True
    if '.git' in parts: return True
    return False

root = Path('.').resolve()
updated = []
for p in root.rglob('*.py'):
    if should_skip(p):
        continue
    try:
        text = p.read_text(encoding='utf-8')
    except Exception:
        continue
    lines = text.splitlines()
    new_lines = []
    for i, line in enumerate(lines):
        if i == 0 and (line.startswith('#!') or 'coding' in line):
            new_lines.append(line)
            continue
        if re.match(r"^\s*#", line):
            continue
        new_lines.append(line)
    out_lines = []
    prev_blank = False
    for ln in new_lines:
        if ln.strip() == '':
            if prev_blank:
                continue
            prev_blank = True
            out_lines.append('')
        else:
            prev_blank = False
            out_lines.append(ln)
    new_text = '\n'.join(out_lines) + ("\n" if text.endswith('\n') else '')
    if new_text != text:
        p.write_text(new_text, encoding='utf-8')
        updated.append(str(p))

print('Updated files:', len(updated))
for f in updated:
    print(f)
