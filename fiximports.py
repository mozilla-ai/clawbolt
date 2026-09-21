import ast, pathlib, re, subprocess, collections

HELPERS = {'fetch_all','count_rows','iso','iso_or_none','get_or_404_async','get_or_404'}
out = subprocess.run(['uv','run','ruff','check','backend/','--output-format','concise'],
                     capture_output=True, text=True).stdout
need = collections.defaultdict(set)
for line in out.splitlines():
    m = re.match(r'(\S+?):\d+:\d+: F821 Undefined name `(\w+)`', line)
    if m and m.group(2) in HELPERS:
        need[m.group(1)].add(m.group(2))

def add_import(path, names):
    p = pathlib.Path(path); src = p.read_text(); tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == 'backend.app.query_helpers':
            want = sorted({a.name for a in node.names} | set(names))
            lines = src.splitlines(keepends=True)
            del lines[node.lineno-1:node.end_lineno]
            lines.insert(node.lineno-1, f"from backend.app.query_helpers import {', '.join(want)}\n")
            p.write_text(''.join(lines)); return 'merged'
    last = [n for n in tree.body if isinstance(n,(ast.Import,ast.ImportFrom))]
    if not last: return 'skip'
    lines = src.splitlines(keepends=True)
    lines.insert(last[-1].end_lineno, f"from backend.app.query_helpers import {', '.join(sorted(names))}\n")
    p.write_text(''.join(lines)); return 'inserted'

for f, n in sorted(need.items()):
    print(f"{add_import(f, n):9} {f}  {sorted(n)}")
