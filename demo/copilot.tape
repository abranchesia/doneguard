# A real GitHub Copilot CLI session with doneguard. Record from the repo root: vhs demo/copilot.tape
# Needs Copilot CLI logged in and Docker running. Copilot loads a folder's hooks once you trust the folder;
# for the recording, the alias trusts it for this one run.
Output demo/copilot.gif
Require npx
Require docker
Source demo/style.tape
Set FontSize 15
Set Height 820
Set Framerate 15

Hide
Type "source demo/setup.sh store >/dev/null 2>&1 && doneguard init copilot >/dev/null && git add -A && git -c user.name=demo -c user.email=demo@example.com commit -qm doneguard && alias copilot='npx -y @github/copilot@latest --trust-folder' && clear"
Enter
Wait@5m
Show

Type `copilot --allow-tool write -p 'Quick one, just edit src/cart.ts: make total() return the amount as a formatted string like "$20.00". Only edit files.'`
Enter
Wait@8m
Sleep 4s
Type "doneguard stats"
Enter
Sleep 6s
