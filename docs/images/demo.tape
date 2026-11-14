# The animation at the top of the READMEs, recorded with VHS (https://github.com/charmbracelet/vhs).
# Start akko-sandbox (https://github.com/AKKO-p/akko-sandbox) first, then, from this folder:
#   SANDBOX=/path/to/akko-sandbox vhs demo.tape
# demo.sh asks the sandbox's MCP server the same question as Alice and as Carol, live.

Output demo.gif
Output demo.mp4

Require docker

Set Shell "bash"
Set FontFamily "JetBrains Mono"
Set FontSize 20
Set Width 1000
Set Height 760
Set Padding 28
Set Margin 0
Set WindowBar ""
Set Framerate 12
Set TypingSpeed 45ms
Set CursorBlink false
Set Theme { "name": "akko-night", "background": "#0b1628", "foreground": "#dbe4f0", "cursor": "#7aa2f7", "selection": "#1e3355", "black": "#0b1628", "red": "#f7768e", "green": "#7ee0a1", "yellow": "#f5c26b", "blue": "#7aa2f7", "magenta": "#bb9af7", "cyan": "#6fd3e8", "white": "#dbe4f0", "brightBlack": "#5b6b85", "brightRed": "#ff8fa3", "brightGreen": "#9ff0bd", "brightYellow": "#ffd68a", "brightBlue": "#9ab8ff", "brightMagenta": "#cdb2ff", "brightCyan": "#9be6f2", "brightWhite": "#ffffff" }

# Off camera: a first run warms Trino up, so the recorded one answers at once.
Hide
Type "export PS1='$ ' && ./demo.sh > /dev/null && echo warm''ed"
Enter
Wait+Screen@120s /warmed/
Type "clear"
Enter
Sleep 300ms
Show

Sleep 400ms
Type "./demo.sh"
Sleep 300ms
Enter
Wait+Screen@30s /each user's rules/
Sleep 7s
