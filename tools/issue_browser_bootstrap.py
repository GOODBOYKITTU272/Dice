"""Phase F2B (revised): operator-only issuance of a browser bootstrap
code. Not reachable over HTTP -- run directly by someone who has already
confirmed the candidate's identity out-of-band, the same trust moment
that already precedes db.dice_auth_state_repository.save_auth_state().

Usage:
    python -m tools.issue_browser_bootstrap <candidate_id>
"""
from __future__ import annotations

import sys

from db.browser_bootstrap import issue_bootstrap_code


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python -m tools.issue_browser_bootstrap <candidate_id>", file=sys.stderr)
        raise SystemExit(2)

    candidate_id = sys.argv[1]
    raw_code, expires_at = issue_bootstrap_code(candidate_id)

    print(f"Bootstrap code: {raw_code}")
    print(f"Expires: {expires_at}")


if __name__ == "__main__":
    main()
