"""Glasshouse — the transparent control room for AI agents.

A local, dependency-free event store and dashboard. Everything an agent does in
a project can be recorded here: who it is, which model, which task, every tool
call, every file edit, and when it stopped. The log is hash-chained so a later
reader can detect an accidental edit or a deleted tail.

Projects are folders. Each folder keeps its own history under
``~/.local/share/glasshouse``; nothing is written into the folder itself, so a
project can be committed or published without leaking a local history.
"""

__version__ = "0.2.0"
