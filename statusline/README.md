# Claudemon status-line bridge

Claude Code's status line receives session JSON on stdin, and for claude.ai
Pro and Max subscribers that JSON can include `rate_limits` (your official
5-hour and 7-day usage windows). Claudemon has no network access and no API
key, so it can't see these numbers on its own — this small script is the
only way they reach it.

`claudemon_statusline.py` reads that JSON, and if a valid `five_hour` or
`seven_day` window is present, writes it to `~/.claude/claudemon/limits.json`
so the Claudemon app (macOS, or the Windows/Linux Python port) can display
it. It also prints a line for the status line itself.

- **Nothing is sent anywhere.** The script makes no network calls. It only
  reads stdin and writes one local JSON file.
- **The data only appears for Pro and Max subscribers, and only after the
  first API response.** Until then `rate_limits` is simply absent from the
  input, and the script writes nothing.
- **Stale data is ignored automatically.** Claudemon checks each window's
  reset time before showing it, so a `limits.json` left over from an older
  session is simply skipped once its window has passed, rather than shown
  as if it were current.

## Setup

Add this to your `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 /path/to/claudemon/statusline/claudemon_statusline.py"
  }
}
```

Replace `/path/to/claudemon` with wherever you cloned this repository.

### Windows

Claude Code runs status-line commands through Git Bash when it's installed,
or through PowerShell if it isn't. Either way:

- Use forward slashes in the path, even on Windows (Git Bash and Python
  both accept them; backslashes need doubling or quoting and are easy to
  get wrong in JSON).
- The `python3` command often doesn't exist on Windows. Use `py -3`
  (the standard Python launcher) if it's available, otherwise `python`.

```json
{
  "statusLine": {
    "type": "command",
    "command": "py -3 C:/Users/you/claudemon/statusline/claudemon_statusline.py"
  }
}
```

### Keeping your existing status line

If you already have a status-line command, pass it as arguments instead of
replacing it. The bridge script runs it with the same stdin it received and
prints its output unchanged, so your status line looks exactly as it did
before:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 /path/to/claudemon/statusline/claudemon_statusline.py /your/existing/statusline.sh --any --args"
  }
}
```

If that command fails for any reason, the bridge falls back to its own
short default line instead of leaving the status line blank.

## Output with no arguments

With no arguments, the script prints a short default line, for example:

```
Opus · 5h 23% · 7d 41%
```

Only the parts that are present are shown (the model name alone if there
are no usage windows yet).
