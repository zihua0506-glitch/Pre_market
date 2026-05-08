# Documentation

Maps the docs at this directory by audience and intent.

| File | Audience | When to read |
|---|---|---|
| **[STATUS.md](STATUS.md)** | **anyone, every day** | **Single-page snapshot — current wallet, services, sizing, what's still open. Read this first when you come back to the project.** |
| [overview.md](overview.md) | strategy / research | Background on prediction markets, alpha taxonomy, where this project sits in that map. Long-form. |
| [weather_strategies.md](weather_strategies.md) | strategy / research | Detail of weather-specific strategies by time scale and market type. Reference, not a tutorial. |
| [operations.md](operations.md) | operator / on-call | Daily checklist: what to run, when, what failure looks like, wallet ops, troubleshooting. |
| [deployment.md](deployment.md) | operator / SRE | The two production servers (singapore + valkyrie), how they're wired together, sync pipeline, runbooks for unscheduled archive / strategy stop / artifact recovery / wallet rotation. **Read this before changing anything on a server.** |

Project-level pointers (root of the repo):
- [`README.md`](../README.md) — one-page repo intro, version table, common commands.
- [`CLAUDE.md`](../CLAUDE.md) — guidance for Claude Code agents working in this repo (architecture, conventions, version semantics, performance numbers).
