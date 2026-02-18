from modules.config import HiveConfig


def test_autonomous_governance_alias_maps_to_failsafe():
    cfg = HiveConfig(governance_mode="autonomous")
    assert cfg.governance_mode == "failsafe"
