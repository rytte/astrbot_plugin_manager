"""Keep marketplace publisher matching distinct from full repository authorship."""

import pytest
from astrbot_plugin_manager.identity import authors_match


@pytest.mark.parametrize(
    "local, remote, market, expected",
    [
        ("Soulter,FateTrial", "Soulter", True, True),
        ("Soulter, FateTrial", "FateTrial", True, True),
        ("Soulter,FateTrial", "FateTrial, Soulter", True, True),
        ("Soulter,FateTrial", "Soulter", False, False),
        ("Soulter,FateTrial", "FateTrial, Soulter", False, True),
        ("Soulter", "Soulter,FateTrial", True, False),
        ("Soulter,FateTrial", "Soulter,Impostor", True, False),
        ("Soulter,FateTrial", "Soul", True, False),
        ("Soulter,FateTrial", "soulter", True, False),
        ("Soulter,FateTrial", "Impostor", True, False),
        ("Soulter,FateTrial", "", True, False),
        ("Soulter,FateTrial", None, True, False),
        ("Soulter,FateTrial", ["Soulter"], True, False),
        ("Soulter,", "Soulter", True, False),
        ("Soulter,FateTrial", "Soulter,", True, False),
        ("", "", False, False),
    ],
)
def test_author_identity(local, remote, market, expected):
    assert authors_match(local, remote, market=market) is expected
