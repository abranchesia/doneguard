# A real Codex CLI session with doneguard. Record from the repo root: vhs demo/codex.tape
# Needs Codex logged in and Docker running. Codex runs project hooks only once you approve them (/hooks);
# for the recording, the alias approves them for this one run.
Output demo/codex.gif
Require npx
Require docker
Source demo/style.tape
Set FontSize 15
Set Height 860
Set Framerate 15

Hide
Type "source demo/setup.sh store >/dev/null 2>&1 && doneguard init codex >/dev/null && git add -A && git -c user.name=demo -c user.email=demo@example.com commit -qm doneguard && alias codex='npx -y @openai/codex@latest --dangerously-bypass-hook-trust' && clear"
Enter
Wait@5m
Show

Type `codex exec -s workspace-write 'Quick one, just edit src/cart.ts: make total() return the amount as a formatted string like "$20.00". Only edit files.'`
Enter
Wait@8m
Sleep 4s
Type "doneguard stats"
Enter
Sleep 6s
