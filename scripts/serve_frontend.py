#!/usr/bin/env python3
"""Thin wrapper so zip-root launchers can run the SPA proxy without -m."""

from opencode_manager.dashboard.frontend_proxy import main

if __name__ == "__main__":
    raise SystemExit(main())
