from ..ui.onboarding import ensure_onboarded_blocking


def ensure_setup() -> bool:
    """Backstop the eager onboarding gate from the coordinator's run loop.

    Returns True once the machine is onboarded (current EULA accepted + API key
    present). Runs on a coordinator background thread; see
    ``ensure_onboarded_blocking`` for why it waits out an in-progress modal
    rather than returning early.
    """
    return ensure_onboarded_blocking()
