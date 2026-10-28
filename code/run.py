"""Run the calendar extension directly without installing a package."""

# Enable bounded startup traces only for managed CI launches or an explicit
# diagnostic request. The normal CLI and UI never print diagnostic banners.
import faulthandler
import os

if os.environ.get("PERSONAL_AGENT_STARTUP_DIAGNOSTICS") == "1":
    faulthandler.dump_traceback_later(10, repeat=True)

from personal_agent.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
