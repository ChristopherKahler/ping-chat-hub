"""cx-spawn-hidden <codename> — start the cx-chat pipe window as a REAL but
hidden console.

`Start-Process -WindowStyle Hidden` on a .cmd yields a windowless (headless)
console: FindWindow returns 0, so the tray can never show it and title-based
tooling can't see it. CREATE_NEW_CONSOLE + STARTUPINFO SW_HIDE creates an
actual conhost window that is merely hidden — titled, FindWindow-able, and
ShowWindow-able from the cx tray.

Run under pythonw (returns immediately, no flash).
"""
import os
import subprocess
import sys

# paths derive rather than being spelled out -- see cxpaths.py, which ships
# beside this file. Vendored into ping-chat-hub 2026-08-19.
import cxpaths

# the .cmd that opens a chat pipe window. Installer-written; derived
# from the account otherwise.
CX_CHAT_CMD = (os.environ.get("PING_HUB_CX_CHAT_CMD")
               or str(cxpaths.home() / "bin" / "cx-chat.cmd"))

if len(sys.argv) > 1:
    si = subprocess.STARTUPINFO()
    si.dwFlags = subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    subprocess.Popen(
        ["cmd.exe", "/c", CX_CHAT_CMD, sys.argv[1]],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        startupinfo=si, close_fds=True)
