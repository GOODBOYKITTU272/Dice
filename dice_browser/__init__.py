"""Phase 4B: persistent Dice browser foundation.

Foundation only — no resume upload, no question answering, no Next/Review/
Submit flow, no submission verification. Those are later, separate phases
(see STATE.md). This package proves the browser layer can be launched
persistently, reused across restarts, and can inspect a known job page's
safety signals (auth state, security challenges, already-applied, Easy
Apply presence) without ever initiating an application.
"""
