"""The credential broker — where secrets come from, and where they may not.

A personal access token once leaked because it was typed as a literal
``--gh-token ghp_…`` on a command line. That single argument put the secret into
the shell's history file, into every process listing on the machine for as long
as the command ran, and into the terminal scrollback that later got pasted into
an issue. None of those three copies is revocable by the person who typed it.

So this module does two things, and deliberately nothing else:

1. **Refuses a literal secret on the command line**, before the argument reaches
   any script. The refusal names the flag and never the value.
2. **Fetches a token from a source that already holds it** — ``gh auth token``
   or the login Keychain — and hands it to a child process through its
   environment, which is the narrowest channel available here.

There is deliberately no code path in this module, or in anything that calls it,
that prints a token, writes one to a file, includes one in an error message, or
puts one in a report. A broker that can echo its secret is not a broker. The
value returned by :func:`token` is passed straight into a child's environment
and is never bound anywhere it could outlive the call.

The environment is not a perfect channel — a child can read it back out and a
sibling process owned by the same user can read ``/proc``. It is chosen because
every alternative available to a CLI that shells out (an argument, a temp file,
a pipe the script must know to read) is worse or needs every delegate rewritten.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable, Mapping

# Flags that carry a secret as their value. Refused wherever they appear in an
# fm command line, whichever verb they were meant for.
SECRET_FLAGS = frozenset(
    {
        "--gh-token",
        "--github-token",
        "--token",
        "--access-token",
        "--api-key",
        "--apikey",
        "--pat",
        "--password",
        "--secret",
    }
)

# Environment assignments typed inline (``GH_TOKEN=ghp_… fm flash``) reach argv
# in some wrappers and carry exactly the same exposure.
SECRET_ASSIGNMENT_PREFIXES = (
    "GH_TOKEN=",
    "GITHUB_TOKEN=",
    "HF_TOKEN=",
    "--gh-token=",
    "--github-token=",
    "--token=",
    "--api-key=",
    "--password=",
)

# Shapes GitHub's own credentials take. A bare value of this shape anywhere in an
# argument list is a leaked token whatever flag it followed — including no flag
# at all, which is how they reach a positional argument.
SECRET_VALUE_PREFIXES = ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_")

# The credential names a manifest command may declare, and the environment
# variables each one populates in the child. Two names for the GitHub token
# because the CLI and the Actions ecosystem disagree about which to read.
CREDENTIALS: dict[str, tuple[str, ...]] = {
    "github": ("GH_TOKEN", "GITHUB_TOKEN"),
}

# The Keychain item a developer can store the token in when `gh` is not
# installed: `security add-generic-password -s fm-github -a <user> -w`.
KEYCHAIN_SERVICE = "fm-github"


class TokenUnavailable(Exception):
    """No configured source could supply a credential.

    Carries the name of the credential and the sources that were tried — never
    a partial value, never the output of the source that failed.
    """


def _flag_of(argument: str) -> str | None:
    """The flag an argument is, or begins with, when it is one this module refuses."""
    if argument in SECRET_FLAGS:
        return argument
    for prefix in SECRET_ASSIGNMENT_PREFIXES:
        if argument.startswith(prefix):
            return prefix.rstrip("=")
    return None


def refuse_literal_secrets(argv: Iterable[str]) -> str | None:
    """Return a refusal message when ``argv`` carries a literal secret, else ``None``.

    The message names the flag and never its value: a refusal that quotes the
    secret back at the user writes it to the terminal a second time, which is
    one of the exposures being refused.
    """
    for argument in argv:
        flag = _flag_of(argument)
        if flag is not None:
            return (
                f"refusing {flag} on the command line — a secret typed as an argument is "
                "in your shell history and in this machine's process list, and neither copy "
                "can be revoked. Remove it: fm reads the token from `gh auth token` or the "
                "login Keychain."
            )
        if argument.startswith(SECRET_VALUE_PREFIXES):
            return (
                "refusing an argument that looks like a GitHub token — it is now in your "
                "shell history and in this machine's process list. Revoke it, then let fm "
                "read the token from `gh auth token` or the login Keychain."
            )
    return None


def _from_gh() -> str | None:
    """The token ``gh`` already holds, or ``None`` when it holds none.

    ``gh auth token`` is tried first because it is the source a developer has
    already authenticated, refreshed, and scoped — asking it is the difference
    between one login and one login per tool.
    """
    try:
        done = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError):
        return None
    if done.returncode != 0:
        return None
    value = done.stdout.strip()
    return value or None


def _from_keychain() -> str | None:
    """The token stored in the login Keychain, or ``None``.

    The fallback for a machine with no ``gh`` — an appliance, or a CI runner
    where the developer stored the credential once by hand. ``security`` writes
    the value to stdout and nowhere else; the process's own stderr is discarded
    because a failure message from it can quote the item it was looking for.
    """
    try:
        done = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError):
        return None
    if done.returncode != 0:
        return None
    value = done.stdout.strip()
    return value or None


def token(credential: str = "github") -> str:
    """Fetch one credential from the first source that has it.

    Raises :class:`TokenUnavailable` when no source does. The exception names
    the credential and the sources tried, and carries no fragment of any value.
    """
    if credential not in CREDENTIALS:
        raise TokenUnavailable(f"unknown credential {credential!r}")
    for source in (_from_gh, _from_keychain):
        value = source()
        if value:
            return value
    raise TokenUnavailable(
        f"no source holds the {credential} credential — run `gh auth login`, or store it "
        f"with `security add-generic-password -s {KEYCHAIN_SERVICE} -a $USER -w`"
    )


def environment(credentials: Iterable[str], base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment a delegate runs in, with its declared credentials filled in.

    Only what the command declared is fetched: a verb that needs no credential
    must not cause a Keychain prompt or a ``gh`` round trip, and a token that was
    never fetched cannot leak. Raises :class:`TokenUnavailable` if a declared
    credential cannot be supplied, which the caller reports as a precondition
    failure — running the delegate without it would fail later and less clearly.
    """
    env = dict(os.environ if base is None else base)
    for credential in credentials:
        value = token(credential)
        for variable in CREDENTIALS[credential]:
            env[variable] = value
    return env
