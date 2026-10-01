"""Faithfulness guard tests (rule 4).

The fixup may only correct mis-heard words. Genuine throat-mic corrections (dropped
consonants, a letter or two off, re-spaced words) must pass; anything that adds,
drops, or swaps in a word must be rejected so the raw transcript is kept.
"""
import pytest

import faithful

GENUINE = [
    ("lit thee files please", "list the files please"),
    ("un thee et again", "run the tests again"),          # most words mis-heard
    ("open thee pull equest", "open the pull request"),
    ("ave the current ile", "save the current file"),     # leading s/f dropped
    ("chec the atus of the ild", "check the status of the build"),
    ("how the lat commit", "show the last commit"),
    ("git tatu please", "git status please"),
    ("look in to the logs", "look into the logs"),        # two heard words → one
    ("check the setup", "check the set up"),              # one heard word → two
    ("run the tests", "Run the tests."),                  # case + punctuation only
    ("run the tests", "run the tests"),                   # unchanged
]

INVENTED = [
    ("run the tests", "delete all files now"),            # same-ish length, all new
    ("run the tests", "delete the tests"),                # same length, one verb swapped
    ("list the files", "delete the files"),
    ("run the tests", "rm the tests"),
    ("open the pull request", "close the pull request"),
    ("show the last commit", "revert the last commit"),
    ("deploy to staging now", "deploy to production now"),
    ("run the tests", "run all tests"),
    ("run the tests", "okay start execution"),
    ("un thee et again", "run the deletes again"),        # a "fix" that isn't close
    ("run the tests", "run the tests and push"),          # modest expansion
    ("run the tests", "run the the tests"),               # any added word
    ("do not push the branch", "do push the branch"),     # a dropped word (negation)
    ("run the tests", "run tests"),
    ("run the tests", ""),
    ("run the tests", "..."),
]


@pytest.mark.parametrize("raw,reply", GENUINE)
def test_genuine_mishearing_corrections_pass(raw, reply):
    assert faithful.is_faithful(raw, reply)


@pytest.mark.parametrize("raw,reply", INVENTED)
def test_invented_replies_are_rejected(raw, reply):
    assert not faithful.is_faithful(raw, reply)


def test_close_accepts_restored_consonants_but_not_unrelated_words():
    assert faithful.close("et", "tests")
    assert faithful.close("thee", "the")
    assert not faithful.close("et", "deletes")
    assert not faithful.close("run", "delete")
    assert not faithful.close("a", "anything")             # one letter proves nothing
