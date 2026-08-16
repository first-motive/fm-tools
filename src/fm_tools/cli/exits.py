"""The one exit-code contract every ``fm`` verb answers to.

Before this module the same class of error left with a different number
depending on which verb hit it: an unknown repo exited 2 from ``fm install`` and
1 from everywhere else, a missing script exited 1, a failed delegate exited 1,
and three different message prefixes (``fm:``, ``fm install:``, ``error:``)
described them. Nothing calling ``fm`` from a script could branch on the result,
which is the only reason an exit code exists.

The table, and what belongs in each row:

=====  ==========================================================================
Code   Meaning
=====  ==========================================================================
0      Success.
1      Reported unhealthy state. The command ran correctly and the answer is
       bad: ``fm doctor`` had a failing check. Nothing is wrong with the
       invocation, so this is not a usage error.
2      Usage error. The command line cannot be honoured as typed — unknown verb,
       unknown repo, unknown flag, missing argument. Matches argparse's own
       exit code, so hand-parsed verbs and argparse agree.
3      Precondition failure. The command is valid but this machine cannot run it
       yet: the workspace root is unresolvable, the repo is not cloned, a
       declared script is missing or not executable, a required credential is
       unavailable, or the machine card is one this build refuses to read.
4      Delegate failure. A repo's own script ran and reported failure while
       ``fm`` was aggregating several of them (``fm update``, ``fm setup``).
       The delegate's own exit code is in the report; the summary cannot
       preserve several of them at once, so it reports the class instead.
130    Interrupted — SIGINT reached the child (128 + 2, the shell's convention).
=====  ==========================================================================

**Passthrough is the deliberate exception.** ``fm <manifest verb>``,
``fm install``, ``fm reset``, ``fm uninstall``, and ``fm run`` return the exit
code of the single process they ran, unchanged. Those verbs promise that going
through ``fm`` is indistinguishable from running the script directly, and
rewriting a script's exit code to 4 would break every caller that reads it — a
launcher that exits 3 for "no robot connected" means its own 3, not this table's.
Codes 3 and 4 apply to failures ``fm`` itself detects on either side of that run.

Error messages share one prefix for the same reason they share one table: ``fm:``,
never ``fm install:`` or ``error:``. A caller grepping stderr for failures should
not have to know which verb produced the line.
"""

from __future__ import annotations

import sys

OK = 0
UNHEALTHY = 1
USAGE = 2
PRECONDITION = 3
DELEGATE = 4
INTERRUPTED = 130

# The one prefix every fm-authored error line carries.
PREFIX = "fm:"


def fail(message: str) -> None:
    """Print one error line to stderr under the shared prefix."""
    print(f"{PREFIX} {message}", file=sys.stderr)


def from_returncode(code: int) -> int:
    """Turn a :mod:`subprocess` return code into the code a shell would report.

    ``subprocess`` reports a child killed by a signal as the *negative* signal
    number, while every shell reports ``128 + n``. Handing the negative value
    back is worse than either convention: CPython takes an exit status modulo
    256, so a script killed by SIGTERM would leave with 241 — a number that
    means nothing to anyone. Passthrough only holds if what comes out is what a
    caller would have seen running the script directly.
    """
    return 128 - code if code < 0 else code
