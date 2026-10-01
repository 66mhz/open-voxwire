<!-- Thanks for contributing to Voxwire! Keep PRs focused and well-described. -->

## What & why

<!-- What does this change, and why? Link any related issue (e.g. Closes #123). -->

## How it was tested

<!-- Commands run and their results. Never claim green if red. -->

## Checklist

- [ ] `pytest` passes (`voxwire/.venv/bin/python -m pytest`)
- [ ] `ruff check .` is clean
- [ ] `./scripts/leakcheck.sh` is clean — no secrets, keys, private hosts, or IPs
- [ ] New behavior has real tests
- [ ] No service is hardcoded into `gateway.py`; integrations/STT stay drop-in plugins
- [ ] Model IDs (if any) live only in `voxwire/stt/models.py`
- [ ] The security/confirmation gate is not weakened (see `SECURITY.md`)
- [ ] `docs/DESIGN.md` updated if the architecture changed
