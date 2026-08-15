"""A scoped health run must not write a partial observation as if it were a total one.

run_source_health has two lanes: a daily FULL run that probes everything, and a 6h fast lane that
probes only the non-CDP sources. Any per-source state a scoped run writes wholesale destroys what
it could not see.

This has now happened twice in the same function. `last_status` was fixed (the fast lane merges so
the CDP entries the daily run owns survive). `degraded` was left behind, and the consequence was not
a stale number but a recurring lie: every fast-lane run dropped the CDP sources out of the degraded
set, the next daily run re-added them, they registered as NEWLY degraded, and the 源降级 alert
re-fired twice a day forever. An alarm that cries wolf on a schedule is worse than no alarm, because
it trains its reader to ignore the real one.

So the guard is not on `degraded`. It is on the RULE: every state key this function writes must have
declared how it handles scope. A third key added later fails here until its author says which it is.
"""
import ast
import inspect
import re
import textwrap
import unittest

from omniseek.core import infra_jobs


# Every `state[...]` key run_source_health writes, and the scope discipline each one has DECLARED.
# Adding a key to the function without adding it here is the failure this file exists to catch.
DECLARED = {
    "fails":       "per-source, scope-aware: _health_track mutates in place, only for probed sources",
    "_alerts":     "per-source cooldowns, mutated in place, never rebuilt",
    "last_run":    "scalar, not per-source",
    "last_status": "per-source, scope-aware: rebuilt only on a full run, merged on the fast lane",
    "degraded":    "per-source, scope-aware: rebuilt only on a full run, merged on the fast lane",
}

# The two that are REBUILT from this run's observations, so each must be gated on `full`.
MUST_BE_FULL_GATED = ("last_status", "degraded")


class HealthStateScopeTests(unittest.TestCase):
    def setUp(self):
        self.src = inspect.getsource(infra_jobs.run_source_health)

    def _assigned_keys(self):
        # textwrap.dedent, NOT inspect.cleandoc: cleandoc re-indents relative to the second line and
        # flattens the function body, which parses as an empty def.
        tree = ast.parse(textwrap.dedent(self.src))
        keys = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for tgt in node.targets:
                if (isinstance(tgt, ast.Subscript)
                        and isinstance(tgt.value, ast.Name) and tgt.value.id == "state"
                        and isinstance(tgt.slice, ast.Constant)
                        and isinstance(tgt.slice.value, str)):
                    keys.add(tgt.slice.value)
        return keys

    def test_every_state_key_this_function_writes_has_declared_its_scope_discipline(self):
        """The generic guard. A new per-source key written wholesale is the next instance of this
        bug, and it would ship silently; here it fails until someone states which lane owns it."""
        found = self._assigned_keys()
        undeclared = sorted(found - set(DECLARED))
        stale = sorted(set(DECLARED) - found)
        self.assertFalse(undeclared,
                         f"run_source_health writes state keys nobody declared a scope rule for: "
                         f"{undeclared}. Say how each behaves on the fast (noncdp) lane.")
        self.assertFalse(stale,
                         f"declared keys no longer written (this file is now describing a world "
                         f"that does not exist): {stale}")

    def test_the_rebuilt_sets_are_gated_on_a_full_run(self):
        """Both keys that are rebuilt from this run's results must consult `full`. Without it the
        fast lane speaks for sources it never looked at."""
        for key in MUST_BE_FULL_GATED:
            m = re.search(rf'state\["{key}"\]\s*=(.{{0,400}})', self.src, re.S)
            self.assertIsNotNone(m, f"no assignment found for state[{key!r}]")
            expr = m.group(1)
            # either the assignment itself branches on `full`, or it assigns a name built one or two
            # lines above from a `full`-gated expression (the last_status/snap idiom).
            window = self.src[max(0, m.start() - 400):m.end()]
            self.assertIn("full", window,
                          f"state[{key!r}] is written without consulting `full`: a fast-lane run "
                          f"would overwrite what it never probed")

    def test_degraded_keeps_the_unprobed(self):
        """The specific regression: the fast lane must carry forward degraded sources it did not
        probe, instead of silently clearing them and re-alerting on the next full run."""
        m = re.search(r'state\["degraded"\]\s*=(.{0,300})', self.src, re.S)
        self.assertIsNotNone(m)
        expr = m.group(1)
        self.assertIn("prev_degraded", expr,
                      "the fast lane discards the previous degraded set instead of merging it")
        self.assertIn("probed", expr,
                      "the merge does not subtract what WAS probed, so a recovered source would "
                      "never leave the degraded set")


if __name__ == "__main__":
    unittest.main(verbosity=2)
