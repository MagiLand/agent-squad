"""Agent Squad test suite.

In-process CLI calls keep the caller's HOME and XDG_STATE_HOME, so the suite
turns the user-level event store off (docs/events.md). The fixtures direct
their subprocesses to a temporary store, and the event tests choose their
own.
"""

import os

os.environ["AGENT_SQUAD_EVENTS"] = "off"
