# The same checks without any agent: by hand, in CI or in a git hook. Record from the repo root: vhs demo/check.tape
# Needs Docker running: the tests get a throwaway Postgres.
Output demo/check.gif
Require npm
Require docker
Source demo/style.tape
Set Height 940

Hide
Type `source demo/setup.sh store >/dev/null 2>&1 && doneguard init claude >/dev/null && sed -i '' 's/ [*] qty;/;/' src/cart.ts && clear`
Enter
Wait@5m
Show

Type "git --no-pager diff --stat"
Enter
Sleep 1.5s
Type `doneguard check; echo "exit $?"`
Enter
Wait@3m
Sleep 6s

Hide
Type "git checkout -q src/cart.ts && clear"
Enter
Wait
Show

Type "# after the fix:"
Enter
Type `doneguard check src/cart.ts; echo "exit $?"`
Enter
Wait@3m
Sleep 4s
