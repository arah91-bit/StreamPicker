"""Regenerate the committed .env.reference from the live config catalog.

    python -m tools.gen_env_reference        # run from the project root

Keeps the hand-editable reference in lockstep with what the code reads. A test
(tests/test_settings_dashboard.py) fails if the committed file is stale.
"""

import pathlib

from app import envref

OUT = pathlib.Path(__file__).resolve().parent.parent / ".env.reference"


def main() -> None:
    OUT.write_text(envref.reference_dotenv())
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
