"""Fixed-fixture live evaluation; no requests without explicit --allow-live."""

from northstar_codereview.live_quality import main


if __name__ == "__main__":
    main()
