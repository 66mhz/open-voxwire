"""Echo — the reference Voxwire integration.

The smallest possible plugin: it proves the transcript → route → handle → reply
path end to end. To build your own integration, copy this file, rename it, and
change `wake_words` + `handle`. Dropping a module into `voxwire/integrations/`
and calling `register(...)` is all it takes to make it live — nothing is wired
into the gateway.
"""
from __future__ import annotations

from .base import Command, Integration, Result, register


class EchoIntegration(Integration):
    name = "echo"
    wake_words = ("echo", "repeat")

    def handle(self, command: Command) -> Result:
        said = command.text
        for w in self.wake_words:
            said = said.replace(w, "", 1)
        return Result(ok=True, reply=said.strip() or "(nothing to echo)")


register(EchoIntegration())
