"""Guarded operator workflow. Only the explicitly authorized run action infers."""
from northstar_codereview.real_review_cli import main

if __name__ == '__main__':
    raise SystemExit(main())
