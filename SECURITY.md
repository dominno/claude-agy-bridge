# Security

agy-bridge validates staged inputs and constrains how they reach Antigravity. The implementation worker also uses
Linux Bubblewrap to isolate the writable workspace and the local check. [Threat model and tests](docs/THREAT_MODEL.md).

## Reporting a problem
Report a suspected rule bypass privately through
[GitHub's private vulnerability reporting](https://github.com/dominno/claude-agy-bridge/security/advisories/new). Do not
post a working bypass in a public issue. Please include the command, the files involved (fake ones are fine) and the
exit code you saw.

## Known limits
- AGY can send selected files and task text to Google for processing. Encoded, split, or novel secrets can pass the scan.
- Read-only delegation gives AGY the current `HOME` so it can find its sign-in. File and network access through AGY's
  tools are only partly measured. [Recorded AGY behavior](docs/COMPAT.md).
- Implementation gives AGY a private candidate and a dedicated profile with network access for the model. It runs
  the approved check offline on a separate copy. The host controls resource limits. [Implementation requirements](docs/IMPLEMENT.md).
- The bridge cannot stop a model from returning harmful text. Verify every answer.
- Untested on Windows.
- Claude Code pre-approves the listed read-only bridge subcommands. `/agy:implement` uses normal Claude Code permissions.
- For the grading commands, `--unsafe-no-sandbox` runs model-written code with your privileges and network access.
