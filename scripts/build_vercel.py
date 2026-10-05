"""Build public website assets; source data and releases stay behind the API."""

import os
import subprocess

from api.store import ROOT
from scripts.check_launch import public_identity_errors


def main():
    if os.getenv("VERCEL_ENV") == "production":
        errors = public_identity_errors(os.environ, (ROOT / "DATA-LICENSE.md").read_text())
        if errors:
            raise SystemExit("Public launch blocked:\n" + "\n".join(errors))
    subprocess.run(["npm", "--prefix", "web", "ci"], cwd=ROOT, check=True)
    subprocess.run(["npm", "--prefix", "web", "run", "build"], cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
