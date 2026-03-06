#!/usr/bin/env bash
# Render per-agent demo SVGs from the split cast files.
#
# Usage:
#   ./render-svg.sh                  # render all agents
#   ./render-svg.sh --agent codex    # render only Codex
#
# Each agent's SVG is produced by concatenating demo-common.cast (the shared
# prefix) with endings/<agent>.cast, converting v3→v2, and rendering via
# svg-term-cli. Output: demo-<agent>.svg in this directory.
set -euo pipefail
cd "$(dirname "$0")"

AGENTS=(codex)

# Parse --agent flag
if [[ "${1:-}" == "--agent" ]]; then
    AGENTS=("${2:?missing agent name after --agent}")
    shift 2
fi

for agent in "${AGENTS[@]}"; do
    ending="endings/${agent}.cast"
    if [[ ! -f "$ending" ]]; then
        echo "Error: $ending not found" >&2
        exit 1
    fi

    # Concatenate common prefix + agent-specific ending
    combined="demo-${agent}-combined.cast"
    cat demo-common.cast "$ending" > "$combined"

    # Convert v3 → v2 (svg-term-cli only supports v2)
    python3 -c "
import json, sys

with open('$combined') as f:
    lines = f.readlines()

v3 = json.loads(lines[0])
v2 = {
    'version': 2,
    'width': v3['term']['cols'],
    'height': v3['term']['rows'],
    'timestamp': v3.get('timestamp'),
    'env': {'SHELL': v3.get('env', {}).get('SHELL', '/bin/bash'),
            'TERM': v3['term'].get('type', 'xterm-256color')},
    'theme': v3['term'].get('theme', {}),
}
out = [json.dumps(v2)]
t = 0.0
for line in lines[1:]:
    ev = json.loads(line)
    t += ev[0]
    ev[0] = round(t, 6)
    out.append(json.dumps(ev))
print('\n'.join(out))
" > "demo-${agent}-v2.cast"

    npx svg-term-cli --in "demo-${agent}-v2.cast" --out "demo-${agent}.svg" --window
    rm "demo-${agent}-combined.cast" "demo-${agent}-v2.cast"

    echo "Generated demo-${agent}.svg"
done
