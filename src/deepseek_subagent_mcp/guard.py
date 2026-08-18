"""Deterministic classification of a child's proposed tool call.

This is the load-bearing half of supervised execution. The supervisor model
only ever sees what this module cannot decide, which keeps the dangerous cases
independent of any model's judgement and keeps latency and cost off the common
path.

Classification runs on **parsed argv**, never on raw command text. A substring
matcher produces false positives on quoted strings, comments and documentation,
and false negatives on anything obfuscated.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

ALLOW = "allow"
DENY = "deny"
ESCALATE = "escalate"

# Read-only commands. Safe to run anywhere: they inspect, they do not mutate.
READ_ONLY = frozenset({
    "ls", "cat", "head", "tail", "wc", "file", "stat", "pwd", "echo", "true",
    "grep", "egrep", "fgrep", "rg", "ag", "find", "which", "type", "basename",
    "dirname", "realpath", "readlink", "date", "env", "printenv", "uname",
    "sort", "uniq", "cut", "tr", "diff", "cmp", "md5", "shasum", "sha256sum",
    "tree", "du", "df", "ps", "id", "whoami", "man", "help", "jq", "column",
})

# Test and build runners: mutating in principle, routine in practice, and the
# entire point of delegating coding work.
TEST_RUNNERS = frozenset({"pytest", "tox", "nose2", "unittest", "make", "cargo", "go"})

# git subcommands that only read.
GIT_READ_ONLY = frozenset({
    "status", "diff", "log", "show", "branch", "remote", "rev-parse", "describe",
    "blame", "shortlog", "ls-files", "config", "stash",
})

# Paths a delegated coding agent has no business touching, relative to $HOME.
SENSITIVE_HOME = (
    ".ssh", ".aws", ".gnupg", ".kube", ".docker/config.json", ".netrc",
    ".config/gh", ".claude.json", ".claude/settings.json", ".omlx",
)
# Basenames that carry secrets wherever they appear, including in a workspace.
SENSITIVE_NAMES = frozenset({
    ".env", ".env.local", ".env.production", ".netrc", ".npmrc", ".pypirc",
    "id_rsa", "id_ed25519", "credentials", ".credentials.yaml",
})

# Commands that are never routine for a delegated coding agent.
NEVER = frozenset({
    "sudo", "doas", "su", "shutdown", "reboot", "halt", "mkfs", "fdisk",
    "diskutil", "launchctl", "systemctl", "crontab", "at", "kextload",
    "csrutil", "spctl", "defaults", "scutil", "networksetup", "dscl",
})

# Interpreters. Running the workspace's own code is the job; running inline
# source or a script from outside the workspace is not.
INTERPRETERS = frozenset({"python", "python3", "node", "ruby", "perl", "deno", "bun", "tsx"})
INLINE_CODE_FLAGS = frozenset({"-c", "-e", "--eval", "--eval-file", "-p"})

# Commands whose whole purpose is to put bytes somewhere. For these the
# destination is the decision, so a destination outside the workspace -- or one
# that cannot be resolved at all -- is refused outright rather than escalated.
# Measured: given `cp note.txt $TMPDIR/copy.txt` and facts correctly reporting
# the target as unresolved, a model reviewer reasoned that $TMPDIR "is the same
# OS temp root the workspace lives under" and allowed it. The write escaped. A
# reviewer is a judgement; the workspace boundary needs to be a rule.
WRITE_COMMANDS = frozenset({
    "cp", "mv", "tee", "ln", "install", "dd", "truncate", "chmod", "chown", "touch",
})

# Commands that reach the network and can execute what they fetch.
NETWORK_FETCH = frozenset({"curl", "wget", "nc", "ncat", "telnet", "ssh", "scp", "sftp", "rsync"})

# Shell metacharacters that make one command line several. Their presence means
# argv parsing is not the whole story, so the call escalates rather than being
# waved through on its first token.
COMPOUND = re.compile(r"(?<!\\)(\|\||&&|[;|`]|\$\(|<\()")


@dataclass(frozen=True)
class Verdict:
    action: str
    reason: str
    # Structured facts the supervisor is allowed to see. Deliberately excludes
    # any prose written by the child.
    facts: dict[str, object] = field(default_factory=dict)


def _home() -> Path:
    return Path.home()


def is_sensitive(path: Path) -> str | None:
    """Return why a path is off-limits, or None."""
    if path.name in SENSITIVE_NAMES:
        return f"{path.name} carries credentials"
    try:
        rel = path.resolve().relative_to(_home().resolve())
    except (ValueError, OSError):
        return None
    posix = PurePosixPath(*rel.parts).as_posix()
    for entry in SENSITIVE_HOME:
        if posix == entry or posix.startswith(entry + "/"):
            return f"~/{entry} is off-limits to a delegated agent"
    return None


def inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def classify_path_write(path_str: str, workspace: Path) -> Verdict:
    """A file-tool write or edit."""
    path = Path(path_str).expanduser()
    if not path.is_absolute():
        path = workspace / path
    facts = {"path": str(path), "inside_workspace": inside(path, workspace)}
    sensitive = is_sensitive(path)
    if sensitive:
        return Verdict(DENY, f"refused: {sensitive}", facts)
    if inside(path, workspace):
        return Verdict(ALLOW, "write inside the workspace", facts)
    return Verdict(ESCALATE, "write outside the workspace", facts)


def classify_bash(command: str, workspace: Path) -> Verdict:
    """A bash command line."""
    facts: dict[str, object] = {"command_length": len(command)}
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return Verdict(ESCALATE, f"command does not parse as shell words: {exc}", facts)
    if not argv:
        return Verdict(ALLOW, "empty command", facts)

    # A read command is not automatically safe: `cat ~/.ssh/id_rsa` is a
    # read-only tool applied to a secret. Sensitive paths are refused whatever
    # the verb, before the per-command rules get a say.
    leaked = _sensitive_argument(argv, workspace)
    if leaked is not None:
        path, why = leaked
        facts["sensitive_path"] = str(path)
        return Verdict(DENY, f"refused: {why}", facts)

    compound = bool(COMPOUND.search(command))
    facts["compound"] = compound

    # Populate the path facts BEFORE any early return. A compound line escalates
    # on the next few lines, and an escalation that carries only "compound: true"
    # asks the supervisor to judge blind -- which a careful one answers by
    # denying. Paths are structure, not the child's prose, so they are safe to
    # show and they are exactly what the decision turns on.
    _path_facts(argv, command, workspace, facts)

    # Every executable named anywhere in the line, so a pipeline cannot hide a
    # dangerous tail behind a harmless head.
    heads = _command_heads(command)
    facts["programs"] = heads

    for head in heads:
        base = PurePosixPath(head).name
        if base in NEVER:
            return Verdict(DENY, f"refused: `{base}` is not available to a delegated agent", facts)
        if base in {"rm", "rmdir", "shred", "unlink"}:
            return _classify_delete(command, argv, workspace, facts)
        if base in NETWORK_FETCH:
            if compound:
                return Verdict(
                    DENY,
                    f"refused: `{base}` piped or chained into another command "
                    "(fetch-and-execute)",
                    facts,
                )
            return Verdict(ESCALATE, f"`{base}` reaches the network", facts)
        if base == "git":
            sub = _git_subcommand(command, base)
            if sub and sub not in GIT_READ_ONLY:
                return Verdict(ESCALATE, f"`git {sub}` changes repository state", facts)

    if facts.get("redirect_targets_outside"):
        return Verdict(
            DENY,
            "refused: redirects output outside the workspace "
            f"({', '.join(str(t) for t in facts['redirect_targets_outside'])})",
            facts,
        )

    escaping = _escaping_write(heads, facts)
    if escaping is not None:
        return Verdict(DENY, f"refused: {escaping}", facts)

    if compound:
        return _classify_segments(command, workspace, facts)

    base = PurePosixPath(argv[0]).name
    if base in READ_ONLY:
        return Verdict(ALLOW, f"`{base}` is read-only", facts)
    if base == "git":
        return Verdict(ALLOW, "read-only git", facts)
    if base in TEST_RUNNERS:
        return Verdict(ALLOW, "test or build runner", facts)
    if base in INTERPRETERS:
        return _classify_interpreter(base, argv, workspace, facts)
    return Verdict(ESCALATE, f"`{base}` is not on the read-only list", facts)


def _classify_interpreter(base: str, argv: list[str], workspace: Path, facts: dict) -> Verdict:
    """`python foo.py` where foo.py is the agent's own work is routine.

    `python -c '...'` is arbitrary code with no artifact to inspect, so it
    escalates however harmless it looks.
    """
    rest = argv[1:]
    for flag in rest:
        if flag in INLINE_CODE_FLAGS:
            return Verdict(ESCALATE, f"`{base}` running inline source", facts)
    if rest[:1] == ["-m"]:
        module = rest[1] if len(rest) > 1 else ""
        facts["module"] = module
        if module.split(".")[0] in TEST_RUNNERS:
            return Verdict(ALLOW, f"`{base} -m {module}`", facts)
        return Verdict(ESCALATE, f"`{base} -m {module}`", facts)
    scripts = [a for a in rest if not a.startswith("-")]
    if not scripts:
        return Verdict(ESCALATE, f"`{base}` with no script argument", facts)
    outside = []
    for raw in scripts:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = workspace / path
        if path.suffix and not inside(path, workspace):
            outside.append(str(path))
    if outside:
        facts["scripts_outside_workspace"] = outside
        return Verdict(ESCALATE, f"`{base}` running a script outside the workspace", facts)
    facts["scripts"] = scripts
    return Verdict(ALLOW, f"`{base}` running workspace code", facts)


# A word carrying an unexpanded shell variable or a command substitution cannot
# be resolved statically, and resolving it anyway produces a confident lie --
# `$TMPDIR/x` looks like a relative path and would be reported as inside the
# workspace. Say "unresolved" instead; that is the fact the supervisor needs.
UNEXPANDED = re.compile(r"[$`]")


def _looks_like_a_path(word: str) -> bool:
    # A bare `sample.txt` counts: telling the supervisor the file sits inside the
    # workspace is what turns a blind ruling into an informed one.
    return "/" in word or word.startswith(".") or "." in word


def _describe_path(word: str, workspace: Path) -> dict[str, object]:
    if UNEXPANDED.search(word):
        return {"path": word, "resolved": False}
    path = Path(word).expanduser()
    if not path.is_absolute():
        path = workspace / path
    return {"path": str(path), "inside_workspace": inside(path, workspace)}


def _path_facts(argv: list[str], command: str, workspace: Path, facts: dict) -> None:
    """Every path this command names, resolved where that is honest.

    Runs for every bash call, including the compound ones that escalate, so the
    supervisor is never asked to rule on a write without being told where.
    """
    named = [
        _describe_path(word, workspace)
        for word in argv[1:]
        if not word.startswith("-") and _looks_like_a_path(word)
    ]
    if named:
        facts["paths"] = named[:12]
    _redirects_outside(command, workspace, facts)


def _escaping_write(heads: list[str], facts: dict) -> str | None:
    """A write command aimed outside the workspace, or at a path nobody can resolve."""
    writers = [PurePosixPath(h).name for h in heads]
    if not any(w in WRITE_COMMANDS for w in writers):
        return None
    for described in facts.get("paths", []):
        if not described.get("resolved", True):
            return (
                f"`{next(w for w in writers if w in WRITE_COMMANDS)}` targets "
                f"{described['path']}, which cannot be resolved, so it cannot be "
                "confirmed inside the workspace"
            )
        if not described.get("inside_workspace", True):
            return (
                f"`{next(w for w in writers if w in WRITE_COMMANDS)}` targets "
                f"{described['path']}, outside the workspace"
            )
    return None


def _classify_segments(command: str, workspace: Path, facts: dict) -> Verdict:
    """Judge a compound line one segment at a time.

    Escalating every compound line wholesale is the safe answer and the wrong
    one: it sent `pwd && ls` to a model, which cost eleven seconds and a model
    call to be told what the read-only list already knew. Measured, in a trace.

    The cross-segment dangers do not depend on this -- `curl … | sh`, a `sudo`
    in the tail, an `rm` anywhere -- because `_command_heads` inspects every
    segment before this runs. What is left is the question of whether each
    segment, on its own, is something the policy already allows.
    """
    segments = [s.strip() for s in SEGMENTS.split(command) if s.strip()]
    facts["segments"] = len(segments)
    # A line whose danger is punctuation rather than a separator -- `$(…)`, a
    # backtick -- does not split, and recursing on it would never terminate.
    if len(segments) <= 1:
        return Verdict(ESCALATE, "compound command line", facts)
    for segment in segments:
        verdict = classify_bash(segment, workspace)
        if verdict.action != ALLOW:
            merged = {**facts, **verdict.facts, "segments": len(segments)}
            return Verdict(
                verdict.action,
                f"segment `{_clip_command(segment)}`: {verdict.reason}",
                merged,
            )
    return Verdict(ALLOW, f"{len(segments)} segments, each allowed by policy", facts)


def _sensitive_argument(argv: list[str], workspace: Path) -> tuple[Path, str] | None:
    """The first argument naming an off-limits path, or None.

    Only words that look like paths are considered -- containing a separator or
    starting with a dot. A bare word like `credentials` is far more often an
    argument than a filename, and denying it would be the substring-matching
    mistake this module exists to avoid.
    """
    for word in argv[1:]:
        if word.startswith("-"):
            continue
        if "/" not in word and not word.startswith("."):
            continue
        path = Path(word).expanduser()
        if not path.is_absolute():
            path = workspace / path
        why = is_sensitive(path)
        if why:
            return path, why
    return None


def _command_heads(command: str) -> list[str]:
    """First word of each segment, so a pipeline's tail is not invisible."""
    heads: list[str] = []
    for segment in re.split(r"\|\||&&|[;|]|\$\(|`", command):
        try:
            words = shlex.split(segment)
        except ValueError:
            continue
        for word in words:
            if "=" in word and not word.startswith("/") and not word.startswith("-"):
                continue  # VAR=value prefix
            heads.append(word)
            break
    return heads


def _git_subcommand(command: str, head: str) -> str | None:
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    for i, word in enumerate(words):
        if PurePosixPath(word).name == head:
            for candidate in words[i + 1:]:
                if not candidate.startswith("-"):
                    return candidate
            return None
    return None


def _classify_delete(command: str, argv: list[str], workspace: Path, facts: dict) -> Verdict:
    targets = [a for a in argv[1:] if not a.startswith("-")]
    facts["delete_targets"] = targets
    if not targets:
        return Verdict(ESCALATE, "delete with no visible target", facts)
    for raw in targets:
        if any(ch in raw for ch in "*?["):
            return Verdict(ESCALATE, f"delete with a glob: {raw}", facts)
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = workspace / path
        if is_sensitive(path):
            return Verdict(DENY, f"refused: delete targets {path}", facts)
        if not inside(path, workspace):
            return Verdict(DENY, f"refused: delete targets {path}, outside the workspace", facts)
    return Verdict(ESCALATE, "delete inside the workspace", facts)


def _redirects_outside(command: str, workspace: Path, facts: dict) -> bool:
    targets = re.findall(r"(?<!\d)>{1,2}\s*([^\s;|&]+)", command)
    outside = []
    for raw in targets:
        word = raw.strip("\"'")
        if word in ("/dev/null", "/dev/stdout", "/dev/stderr"):
            continue
        described = _describe_path(word, workspace)
        # Unresolvable counts as outside: a destination nobody can determine is
        # not a destination anyone should have approved.
        if not described.get("inside_workspace", False):
            outside.append(described["path"])
    if outside:
        facts["redirect_targets_outside"] = outside
        return True
    return False


# Tool-name aliases the dsh bridge may present.
BASH_TOOLS = frozenset({"bash", "shell", "run_command", "Bash"})
WRITE_TOOLS = frozenset({"write", "edit", "str_replace_editor", "Write", "Edit", "create"})
READ_TOOLS = frozenset({"read", "read_image", "Read", "list", "glob", "grep", "Grep", "Glob"})

PATH_KEYS = ("path", "file_path", "filePath", "target", "filename", "file")
COMMAND_KEYS = ("command", "cmd", "script", "input")


def classify(tool_name: str, tool_input: dict, workspace: Path) -> Verdict:
    """Classify one proposed tool call. Unknown shapes escalate, never allow."""
    name = (tool_name or "").strip()
    if name in READ_TOOLS:
        return Verdict(ALLOW, "read-only tool", {"tool": name})
    if name in BASH_TOOLS:
        command = _first(tool_input, COMMAND_KEYS)
        if command is None:
            return Verdict(ESCALATE, "shell call with no readable command", {"tool": name})
        verdict = classify_bash(str(command), workspace)
        return Verdict(verdict.action, verdict.reason, {"tool": name, **verdict.facts})
    if name in WRITE_TOOLS:
        path = _first(tool_input, PATH_KEYS)
        if path is None:
            return Verdict(ESCALATE, "write call with no readable path", {"tool": name})
        verdict = classify_path_write(str(path), workspace)
        return Verdict(verdict.action, verdict.reason, {"tool": name, **verdict.facts})
    return Verdict(ESCALATE, f"unrecognized tool `{name}`", {"tool": name})


def _first(payload: dict, keys: tuple[str, ...]):
    if not isinstance(payload, dict):
        return None
    for key in keys:
        if payload.get(key) is not None:
            return payload[key]
    return None


# Segment separators for a caller-supplied acceptance command. Splitting on the
# raw string cannot see quoting, so a quoted `&&` produces a bogus segment --
# which escalates and therefore blocks. That is the safe direction to be wrong in.
SEGMENTS = re.compile(r"\|\||&&|[;|]")


def classify_verification(command: str, workspace: Path) -> Verdict:
    """Classify a caller's acceptance command.

    This is deliberately the same policy the child's own calls get, rather than
    a second one that drifts. The caller is another agent and can be prompt
    injected, so "the caller asked for it" is not authorization; and an
    acceptance command is shaped exactly like ordinary dev work, which the
    policy already knows how to read.
    """
    if not command.strip():
        return Verdict(ESCALATE, "empty verification command", {"command": command})
    return classify_bash(command, workspace)


def _clip_command(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
