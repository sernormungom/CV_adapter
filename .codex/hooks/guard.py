import sys, json, re

cmd = json.load(sys.stdin).get('tool_input', {}).get('command', '')
bad = [r'rm .*docker\.sock', r'Docker Desktop', r'git init',  r'terraform (apply|plan)']
for p in bad:
    if re.search(p, cmd, re.I):
        print(f'Blocked by guard: {p}. Ask the user first.', file=sys.stderr)
        sys.exit(2)
