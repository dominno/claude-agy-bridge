"""R1: the path denylist (plan section 5). The rules are data; the logic is at the bottom.

Every rule is a glob applied to each path component, case-insensitively, so a
denylisted name is caught at any depth (src/deep/.env is denied like .env).
Each rule carries a positive and a negative example; the tests check both.
"""
from __future__ import annotations

import fnmatch
import unicodedata
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence


@dataclass(frozen=True)
class PathRule:
    pattern: str
    positive: str  # a relative path this rule must deny
    negative: str  # a relative path that no rule may deny


PATH_RULES: Sequence[PathRule] = (
    PathRule(".env", "config/.env", "src/environment.py"),
    PathRule(".env.*", "app/.env.local", "docs/envelope.md"),
    PathRule("*.env", "deploy/prod.env", "src/envy.py"),
    PathRule("*.pem", "certs/server.pem", "docs/pem.md"),
    PathRule("*.key", "certs/server.key", "src/keyboard.py"),
    PathRule("id_rsa*", "home/id_rsa.pub", "src/id_card.py"),
    PathRule("*.p12", "certs/a.p12", "src/p12.py"),
    PathRule("*.pfx", "certs/a.pfx", "src/pfx.py"),
    PathRule(".npmrc", "web/.npmrc", "web/npmrc.md"),
    PathRule(".netrc", "home/.netrc", "home/netrc.md"),
    PathRule(".pypirc", "home/.pypirc", "home/pypirc.md"),
    PathRule("*credentials*", "cfg/aws_credentials.json", "src/credential.py"),
    PathRule(".git-credentials", "home/.git-credentials", "src/git_helper.py"),
    PathRule("id_ed25519*", "home/id_ed25519", "src/id_numbers.py"),
    PathRule("id_ecdsa*", "home/id_ecdsa.pub", "src/id_check.py"),
    PathRule(".pgpass", "home/.pgpass", "src/pgpass.md"),
    PathRule(".kube", "home/.kube/config", "src/kube.py"),
    PathRule(".docker", "home/.docker/config.json", "src/docker.py"),
    PathRule("gcloud", "home/.config/gcloud/properties", "src/gcloud_helper.py"),
    PathRule("*.jks", "keys/a.jks", "src/jks.md"),
    PathRule("*.keystore", "keys/a.keystore", "src/keystore.md"),
    PathRule(".htpasswd", "web/.htpasswd", "web/htpasswd.md"),
    PathRule("secrets*", "cfg/secrets.yaml", "src/secret.py"),
    PathRule(".agy-bridge", "x/.agy-bridge/events.jsonl", "src/bridge.py"),
    PathRule(".git", "sub/.git/config", ".github/workflows/ci.yml"),
    # Generated artifacts can contain captured inputs, credentials or private logs.
    PathRule("artifacts", "out/artifacts/a.txt", "src/artifact.py"),
    PathRule("node_modules", "web/node_modules/x/index.js", "src/modules.py"),
    PathRule(".venv", "py/.venv/bin/python", "src/venv.py"),
    PathRule(".codex", "home/.codex/auth.json", "src/codex.py"),
    PathRule(".claude", "home/.claude/settings.json", ".claude-plugin/plugin.json"),
    PathRule(".gemini", "home/.gemini/settings.json", "src/gemini.py"),
    PathRule(".ssh", "home/.ssh/config", "src/ssh.py"),
    PathRule(".aws", "home/.aws/config", "src/aws.py"),
    PathRule(".gnupg", "home/.gnupg/pubring.kbx", "src/gnupg.py"),
    PathRule("*.sqlite", "data/a.sqlite", "src/sqlite.py"),
    PathRule("*.db", "data/a.db", "src/dbutil.py"),
    PathRule("*.mp4", "media/a.mp4", "src/mp4.md"),
    PathRule("*.wav", "media/a.wav", "src/wav.md"),
    PathRule("*.mp3", "media/a.mp3", "src/mp3.md"),
    PathRule("*.png", "media/a.png", "src/png.md"),
    PathRule("*.jpg", "media/a.jpg", "src/jpg.md"),
)


def _normalise(name: str) -> str:
    """Fold case and compatibility forms; drop invisible format characters."""
    folded = unicodedata.normalize("NFKC", name).casefold()
    folded = "".join(ch for ch in folded if unicodedata.category(ch) != "Cf")
    return folded.rstrip(" .") or folded


def _components(rel: str) -> List[str]:
    return [_normalise(part) for part in rel.replace("\\", "/").split("/") if part not in ("", ".")]


def matching_rules(rel_path: str, extra: Iterable[str] = ()) -> List[str]:
    """Every pattern that denies rel_path (built-in rules first, then repo-local ones).

    `extra` holds repo-local globs (they can only add). A glob containing "/"
    is matched against every leading prefix of the path; others per component.
    """
    parts = _components(rel_path)
    hits = []
    for pattern in [rule.pattern for rule in PATH_RULES] + [p for p in extra]:
        pat = _normalise(pattern)
        if "/" in pat:
            matched = any(fnmatch.fnmatchcase("/".join(parts[:end]), pat.strip("/"))
                          for end in range(1, len(parts) + 1))
        else:
            matched = any(fnmatch.fnmatchcase(part, pat) for part in parts)
        if matched:
            hits.append(pattern)
    return hits


def is_denied(rel_path: str, extra: Iterable[str] = ()) -> Optional[str]:
    hits = matching_rules(rel_path, extra)
    return hits[0] if hits else None
