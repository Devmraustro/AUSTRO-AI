import ast
with open('app/knowledge/repositories.py', 'r') as f:
    content = f.read()
try:
    ast.parse(content)
    print('AST parsing successful')
except SyntaxError as e:
    print(f'SyntaxError at line {e.lineno}, offset {e.offset}: {e.msg}')
    lines = content.split('\n')
    if e.lineno <= len(lines):
        print(f'Line {e.lineno}: {repr(lines[e.lineno-1])}')