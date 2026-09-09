"""Phase 0 canary: proves the pytest harness itself runs in CI before any real tests exist."""


def test_canary() -> None:
    assert True
