"""Module entry point used by installed and detached CLI invocations."""
from .cli import main


if __name__ == '__main__':
    raise SystemExit(main())
