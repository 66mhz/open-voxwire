# Security Policy

Voxwire is a voice-driven agent: an always-on microphone that, once you enable its
executor tools, can run shell commands, edit files, and drive your desktop. That
makes security a first-class concern, not an afterthought. This document explains
the threat model, what protection exists today versus what is still in build-out,
and how to report a vulnerability.

> **Status: early software.** Treat Voxwire as experimental. Do not point it at
> production systems or grant broad tool access on a machine you can't afford to
> have misused.

## Threat model

The core assumption: **anything that reaches the microphone, or the transcript, is
untrusted input that may be trying to command your machine.**

- **Voice injection (acoustic).** Anyone who can make sound near the mic — a person
  in the room, a nearby speaker, a video, a phone — can issue commands. A throat
  mic narrows this (it reads your larynx, not the air), but does not eliminate it.
- **Transcript / prompt injection.** Text that flows to the agent (a transcript, a
  tool result, a file the agent reads, an integration's output) can contain
  instructions that try to redirect it — e.g. "ignore that and delete …".
- **Self-approval.** The most dangerous failure would be an agent that can approve
  its own destructive action. Voxwire is designed so this is structurally
  impossible, not merely discouraged by a prompt.
- **Local exposure.** The server listens on `127.0.0.1:8123` (loopback only). Do
  not expose it to a network without putting your own authentication in front.
- **Other web pages.** Any page open in your browser can send requests to
  `127.0.0.1`, and can reach it under its own name through DNS rebinding.

## How Voxwire defends against it

The security model is **structural, not prompt-based** — enforcement lives in code
paths the agent cannot talk its way around. See `docs/DESIGN.md §3` for the design.

**Shipping today:**

- **The gateway never self-approves.** A destructive integration returns a
  confirmation *request* (`requires_confirmation`, with a preview); the router
  surfaces it and stops — it never executes a gated action itself. This is covered
  by tests (`voxwire/tests/test_gateway.py`).
- **Confirmation is owned by the executor host.** The native confirmation dialog
  lives in the menubar process, not in the agent. It does not yet reject
  synthetic input (see *In build-out* below).
- **Only this machine, and only Voxwire's own page, reach the server.** Every
  HTTP request and WebSocket must come from a loopback peer, name a loopback host
  (`127.0.0.1`, `localhost`, `::1`), and, if a browser sent it, carry Voxwire's own
  page as its Origin. Anything else is refused before a route runs, so a web page
  can't arm the mic, read recordings, write your clipboard or route a command,
  whether it tries a cross-site request or DNS rebinding
  (`voxwire/tests/test_local_only.py`).
- **A kill switch** in the menubar releases the microphone and turns dictation
  off, calling the server directly rather than going through the agent. It does
  not stop the server or lock its API: any local process can re-arm the mic. Treat
  it as a mic cut-off, not a lockdown.
- **Deny-by-default executor.** No shell/file/desktop executor is enabled on any OS
  yet: the confirm gate reports "not ready" everywhere until a per-OS dialog that
  provably rejects synthetic input is built and human-reviewed. Voxwire is
  dictation-first by design.

**In build-out (do not rely on these yet):**

- Rejecting synthetic input at the confirm dialog (so the agent cannot inject its
  own "Approve" click).
- Capability scoping — effective permission computed as `scope ∩ host tier`,
  deny-by-default, in one place.
- An append-only audit log of every attempt (ask / deny / do).
- The full persona / executor tool contract.

## Hardening guidance for operators

- **Start with dictation only.** Grant shell/file/desktop tools deliberately, one
  host at a time, and only on machines you control.
- **Keep the server on loopback.** If you must reach it remotely, tunnel it (e.g.
  SSH/Tailscale) and add authentication — Voxwire ships none. An SSH port forward
  arrives as a loopback request and works as is. A reverse proxy is refused unless
  it rewrites the Host and Origin headers (Voxwire ignores `X-Forwarded-For`, so
  the peer it checks is the proxy's own connection), and doing that switches these
  checks off for everything behind it, so put your authentication in that proxy.
- **Mind the room.** A mic others can reach is a command surface. Use push-to-talk,
  and prefer the throat mic where acoustic injection is a concern.
- **Review integrations before installing them.** A plugin runs in your process
  with your privileges. Treat a third-party integration like any dependency.

## Reporting a vulnerability

**Please do not open a public issue for a security vulnerability.**

Report it privately through GitHub's **"Report a vulnerability"** flow (the
repository's *Security → Advisories* tab), which opens a private security advisory
visible only to the maintainers. Include:

- what the issue is and the impact you see,
- steps to reproduce (a minimal proof-of-concept helps),
- affected version / commit, and any suggested fix.

We aim to acknowledge a report within a few days and to keep you updated as we work
on a fix. Please give us reasonable time to release a fix before any public
disclosure. Thank you for helping keep Voxwire users safe.
