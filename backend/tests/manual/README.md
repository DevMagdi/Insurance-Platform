# Manual verification scripts

These are not collected by pytest. They run against a **live, migrated**
database and are meant to be executed by hand during development.

## `intake_e2e_check.py`

End-to-end proof that an inbound channel message becomes a request:

```bash
./.venv/bin/python backend/tests/manual/intake_e2e_check.py
```

It resets the channel-sourced rows first, then asserts that a WhatsApp message
with no quoted ticket number creates a request, that a follow-up from the same
mobile joins it instead of duplicating, that a replayed webhook is a no-op,
that e-mail intake works, that a message with no policy/card reference is still
accepted, and that attachment bytes are encrypted at rest.
