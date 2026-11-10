"""Guarded local intake only; no automatic review or external inference."""

from northstar_codereview.real_source_cli import main


if __name__ == "__main__":
    raise SystemExit(main())
