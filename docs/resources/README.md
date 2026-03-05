# docs/resources

## Demo recordings

The demo shown in the project README is built from split asciicast v3 files:

- **`demo-common.cast`** — shared prefix: shell prompt → `hatchery new` → description entry → sandbox box → Docker build → "Image built."
- **`endings/claude.cast`** — Claude Code startup screen (logo, "Welcome to Opus 4.6", "Pouncing..." spinner)
- **`endings/codex.cast`** — Codex CLI startup screen (placeholder — replace with a real recording when available)

### How the common part was captured

1. Resize terminal to 120x36: `printf '\e[8;36;120t'`
2. Simplify prompt: `precmd_functions=() && ZSH_THEME="robbyrussell" && source $ZSH/oh-my-zsh.sh`
3. Record: `asciinema rec demo.cast`
4. Run `hatchery new add-oauth-login --no-editor`, type the description, let it launch, Ctrl-C out
5. Ctrl-D to stop recording
6. Edit `demo.cast` (plain JSON lines) to clean up:
   - Remove theme-switching commands from the beginning
   - Fix typing typos and smooth keystroke timing
   - Rename directory references
   - Split at "Image built." — everything before goes in `demo-common.cast`, everything after in `endings/claude.cast`

### How to regenerate the SVGs

```bash
# Render all agents
./docs/resources/render-svg.sh

# Render a specific agent
./docs/resources/render-svg.sh --agent claude
./docs/resources/render-svg.sh --agent codex
```

This concatenates `demo-common.cast` + `endings/<agent>.cast`, converts v3 → v2 (required by svg-term-cli), renders the SVG, and cleans up. The SVGs are embedded in the project README — commit them after regenerating.

### Adding a new agent

1. Create `endings/<agent>.cast` with the agent's startup screen events (v3 format, relative timestamps, no header)
2. Run `./docs/resources/render-svg.sh --agent <agent>`
3. Add the new SVG to the side-by-side table in `README.md`
