"""`python -m ainews` - the same entry point as the `ai-news` console script."""

from ainews.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
