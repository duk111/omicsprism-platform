from __future__ import annotations


def test_phase_zero_agent_modules_are_importable() -> None:
    from backend.app.agent import approvals  # noqa: F401
    from backend.app.agent import audit  # noqa: F401
    from backend.app.agent import graph  # noqa: F401
    from backend.app.agent import grounding  # noqa: F401
    from backend.app.agent import model  # noqa: F401
    from backend.app.agent import schemas  # noqa: F401
    from backend.app.agent import store  # noqa: F401
    from backend.app.agent import tools  # noqa: F401
    from backend.app.agent import verifier  # noqa: F401
