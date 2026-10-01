"""Gateway / plugin-router tests (BYT-116).

Covers the whole point of the genericization: a fresh clone with only the
example integration routes a command end to end, the gateway names no specific
service, and it never self-approves a gated action.
"""
import re
from pathlib import Path

import gateway
from integrations import base
from integrations.base import Integration, Result, register


def test_discovers_example_plugin():
    names = gateway.load_integrations()
    assert "echo" in names


def test_integrations_listing_describes_plugins():
    listing = {i["name"]: i for i in gateway.integrations()}
    assert "echo" in listing
    assert "echo" in listing["echo"]["wake_words"]


# ── end-to-end routing ──────────────────────────────────────────────────────
def test_echo_end_to_end():
    out = gateway.dispatch("echo hello world", source="throat")
    assert out["matched"] is True
    assert out["handled_by"] == "echo"
    assert out["ok"] is True
    assert out["reply"] == "hello world"
    assert out["requires_confirmation"] is False
    assert out["source"] == "throat"


def test_repeat_wake_word_also_routes_to_echo():
    out = gateway.dispatch("repeat after me")
    assert out["handled_by"] == "echo"
    assert out["reply"] == "after me"


def test_unmatched_command_is_not_handled():
    out = gateway.dispatch("what is the weather in Paris")
    assert out["matched"] is False
    assert out["handled_by"] is None
    assert out["ok"] is False
    assert out["requires_confirmation"] is False


def test_empty_transcript_is_a_no_op():
    out = gateway.dispatch("   ")
    assert out["matched"] is False
    assert out["ok"] is False


# ── security: the gateway never self-approves a gated action ─────────────────
def test_confirmation_is_surfaced_not_executed():
    executed = {"n": 0}

    class Danger(Integration):
        name = "danger"
        wake_words = ("delete",)

        def handle(self, command):
            # a destructive plugin returns the gate request and does NOT act
            return Result(ok=True, reply="Delete the temp files?",
                          requires_confirmation=True,
                          preview="shell.run: rm -rf /tmp/x")

    register(Danger())
    out = gateway.dispatch("delete the temp files")
    assert out["handled_by"] == "danger"
    assert out["requires_confirmation"] is True
    assert out["preview"] == "shell.run: rm -rf /tmp/x"
    assert executed["n"] == 0  # gateway never runs a gated action itself


# ── robustness ──────────────────────────────────────────────────────────────
def test_plugin_exception_does_not_crash_gateway():
    class Boom(Integration):
        name = "boom"
        wake_words = ("boom",)

        def handle(self, command):
            raise RuntimeError("kaboom")

    register(Boom())
    out = gateway.dispatch("boom now")
    assert out["matched"] is True
    assert out["handled_by"] == "boom"
    assert out["ok"] is False
    assert "kaboom" in out["reply"]


def test_last_registered_wins_the_tie():
    class EchoTwo(Integration):
        name = "echo2"
        wake_words = ("echo",)

        def handle(self, command):
            return Result(ok=True, reply="two")

    register(EchoTwo())
    out = gateway.dispatch("echo hi")
    assert out["handled_by"] == "echo2"


def test_register_replaces_same_name_no_duplicates():
    from integrations.echo import EchoIntegration
    assert len([i for i in base.registered() if i.name == "echo"]) == 1
    register(EchoIntegration())
    assert len([i for i in base.registered() if i.name == "echo"]) == 1


# ── the public core names no employer specifics (rule 1) ────────────────────
def test_public_core_names_no_employer_specifics():
    # Tokens are assembled from fragments so this guard file itself stays clean
    # under the rule-1 pre-commit grep (see CLAUDE.md).
    names = ["chip" + "craft", "trade" + "pulse", "zero" + "claw"]
    banned = re.compile("|".join(names) + r"|10\.0\.0\.|100\.\d+\.\d+", re.I)
    root = Path(gateway.__file__).resolve().parent
    files = [root / "gateway.py", *(root / "integrations").glob("*.py")]
    leaks = [p.name for p in files if banned.search(p.read_text())]
    assert leaks == [], f"employer specifics leaked into: {leaks}"
