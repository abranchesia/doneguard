# The same checks without any agent: by hand, in CI or in a git hook. Record from the repo root: vhs demo/check.tape
Output demo/check.gif
Require npm
Source demo/style.tape
Set Height 900

Hide
Type "source demo/setup.sh shop && doneguard init claude >/dev/null && cp $DEMO/cart.bug.ts src/cart.ts && clear"
Enter
Wait
Show

Type "git --no-pager diff --stat"
Enter
Sleep 1.5s
Type `doneguard check; echo "exit $?"`
Enter
Wait
Sleep 5s

Hide
Type "cp $DEMO/cart.ok.ts src/cart.ts && clear"
Enter
Wait
Show

Type "# after the fix:"
Enter
Type `doneguard check; echo "exit $?"`
Enter
Wait
Sleep 4s
