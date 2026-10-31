# Security

agy-bridge is meant to be safe to run on a machine that holds production API keys. Its safety comes from rules enforced
by the script (`docs/THREAT_MODEL.md` lists each rule and the test that proves it), not from instructions to a model.

## Reporting a problem
Report a suspected rule bypass privately through
[GitHub's private vulnerability reporting](https://github.com/dominno/claude-agy-bridge/security/advisories/new). Do not
post a working bypass in a public issue. Please include the command, the files involved (fake ones are fine) and the
exit code you saw.

## Known limits
- Files that pass the checks are sent to Google. Encoded, split or novel secrets are not detected.
- `agy` runs with your real `HOME`, so it can find its own sign-in. Whether `agy` itself can read other files under
  `HOME`, or reach the network from its tools, is only partly measured (`docs/COMPAT.md`). The bridge cannot sandbox it.
- The bridge cannot stop a model from returning harmful text. Verify every answer.
- Untested on Windows.
- `allowed-tools` in the command files pre-approves the script with any arguments; it does not limit them.
- `--unsafe-no-sandbox` runs model-written code with your privileges and network access.
