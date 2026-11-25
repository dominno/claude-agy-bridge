"""Handler for doctor: checks the flag combination, then runs the app.doctor health check or sandbox probe."""
from __future__ import annotations

from ..core import config
from .output import fail


def cmd_doctor(args) -> int:
    from ..app import doctor

    if args.implementation:
        if not args.profile:
            fail('--implementation requires --profile')
            return config.EXIT_USAGE
        return doctor.implementation(args.repo, args.profile)
    if args.profile:
        fail('--profile requires --implementation')
        return config.EXIT_USAGE
    return doctor.health(args.repo, args.offline)
