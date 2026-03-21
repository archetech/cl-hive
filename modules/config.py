"""
Configuration module for cl-hive

Contains the HiveConfig dataclass that holds all tunable parameters
for the Hive swarm intelligence layer.

Implements the ConfigSnapshot pattern from cl-revenue-ops for
thread-safe configuration access during background operations.
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .database import HiveDatabase


# Type mapping for config fields (for validation)
CONFIG_FIELD_TYPES: Dict[str, type] = {
    'membership_enabled': bool,
    'auto_join_enabled': bool,
    'member_fee_ppm': int,
    'max_members': int,
    'market_share_cap_pct': float,
    'intent_hold_seconds': int,
    'intent_expire_seconds': int,
    'gossip_threshold_pct': float,
    'heartbeat_interval': int,
    'planner_interval': int,
    'planner_min_channel_sats': int,
    'planner_max_channel_sats': int,
    'planner_default_channel_sats': int,
    'planner_max_active_channels': int,
    'daily_expansion_budget_sats': int,
    'budget_reserve_pct': float,
    'budget_max_per_channel_pct': float,
    # Feerate gate
    'max_expansion_feerate_perkb': int,
}

# Range constraints for numeric fields
CONFIG_FIELD_RANGES: Dict[str, tuple] = {
    'member_fee_ppm': (0, 100000),
    'max_members': (2, 100),
    'market_share_cap_pct': (0.01, 1.0),
    'intent_hold_seconds': (10, 600),
    'intent_expire_seconds': (60, 3600),
    'gossip_threshold_pct': (0.01, 0.5),
    'heartbeat_interval': (60, 3600),
    'planner_interval': (300, 86400),  # Min 5 minutes, max 24 hours
    'planner_min_channel_sats': (100_000, 100_000_000),  # 100k to 100M sats
    'planner_max_channel_sats': (1_000_000, 1_000_000_000),  # 1M to 1B sats (10 BTC)
    'planner_default_channel_sats': (100_000, 500_000_000),  # 100k to 500M sats (5 BTC)
    'planner_max_active_channels': (10, 500),  # Max channels before auto-expansion is gated
    'daily_expansion_budget_sats': (100_000, 100_000_000),  # 100k to 100M sats
    'budget_reserve_pct': (0.05, 0.50),  # 5% to 50% reserve
    'budget_max_per_channel_pct': (0.10, 1.0),  # 10% to 100% of daily budget per channel
    # Feerate gate for expansions
    'max_expansion_feerate_perkb': (1000, 100000),  # 1-100 sat/vB (perkb = 4x perkw)
}


@dataclass
class HiveConfig:
    """
    Configuration container for the Hive plugin.
    
    All values can be set via plugin options at startup.
    """
    
    # Database path
    db_path: str = '~/.lightning/cl_hive.db'

    # Membership
    membership_enabled: bool = True
    auto_join_enabled: bool = False       # Auto-send HELLO on peer_connected (disabled: CLN crash bug)

    # Membership Economics
    member_fee_ppm: int = 0                    # 0-fee for members
    
    # Ecological Limits
    max_members: int = 50                      # Dunbar cap for gossip efficiency
    market_share_cap_pct: float = 0.20         # 20% max per target (anti-monopoly)
    
    # Intent Lock Protocol
    intent_hold_seconds: int = 60              # Wait before committing Intent
    intent_expire_seconds: int = 300           # Lock TTL (5 minutes)
    
    # Gossip Protocol
    gossip_threshold_pct: float = 0.10         # 10% capacity change triggers gossip
    heartbeat_interval: int = 300              # 5 minutes between heartbeats

    # Planner (Phase 6)
    planner_interval: int = 3600               # 1 hour between planner cycles
    planner_min_channel_sats: int = 1_000_000  # 1M sats minimum channel size
    planner_max_channel_sats: int = 50_000_000  # 50M sats maximum channel size
    planner_default_channel_sats: int = 5_000_000  # 5M sats default channel size
    planner_max_active_channels: int = 50      # Gate expansion auto-approve above this channel count

    # Budget controls
    daily_expansion_budget_sats: int = 10_000_000  # 10M sats daily expansion budget
    budget_reserve_pct: float = 0.20               # Reserve 20% of onchain for future expansion
    budget_max_per_channel_pct: float = 0.50       # Max 50% of daily budget per single channel

    # Feerate gate for expansions (sat/kB, where 1 sat/vB = 4 sat/kB approx)
    # Default 5000 sat/kB = ~1.25 sat/vB - conservative low-fee threshold
    max_expansion_feerate_perkb: int = 5000

    # Internal version tracking
    _version: int = field(default=0, repr=False, compare=False)

    def __post_init__(self):
        """Normalize fields on construction."""
        pass

    def snapshot(self) -> 'HiveConfigSnapshot':
        """
        Create an immutable snapshot for cycle execution.
        
        All worker cycles MUST capture a snapshot at cycle start and use
        only that snapshot for the duration of the cycle. This prevents
        torn reads when config is updated mid-cycle.
        """
        return HiveConfigSnapshot.from_config(self)
    
    def validate(self) -> Optional[str]:
        """
        Validate configuration values.

        Returns:
            Error message if invalid, None if valid
        """
        # Type validation
        for key, expected_type in CONFIG_FIELD_TYPES.items():
            value = getattr(self, key, None)
            if value is not None and not isinstance(value, expected_type):
                # Allow int where float is expected
                if expected_type is float and isinstance(value, int):
                    continue
                return f"Config {key} must be {expected_type.__name__}, got {type(value).__name__}"

        for key, (min_val, max_val) in CONFIG_FIELD_RANGES.items():
            if key == 'max_expansion_feerate_perkb':
                value = getattr(self, key, None)
                if value is not None and value != 0 and not (min_val <= value <= max_val):
                    return f"max_expansion_feerate_perkb must be 0 (disabled) or between {min_val} and {max_val}"
                continue
            value = getattr(self, key, None)
            if value is not None and not (min_val <= value <= max_val):
                return f"Config {key}={value} out of range [{min_val}, {max_val}]"

        # Cross-field constraints
        if self.planner_min_channel_sats > self.planner_max_channel_sats:
            return (f"planner_min_channel_sats ({self.planner_min_channel_sats}) > "
                    f"planner_max_channel_sats ({self.planner_max_channel_sats})")
        if (self.planner_default_channel_sats < self.planner_min_channel_sats or
                self.planner_default_channel_sats > self.planner_max_channel_sats):
            return (f"planner_default_channel_sats ({self.planner_default_channel_sats}) "
                    f"outside [{self.planner_min_channel_sats}, {self.planner_max_channel_sats}]")

        return None


@dataclass(frozen=True)
class HiveConfigSnapshot:
    """
    Immutable configuration snapshot for thread-safe cycle execution.
    
    This frozen dataclass prevents accidental mutation and ensures
    consistency when a background loop captures config at cycle start.
    """
    
    # Core settings (immutable snapshot)
    db_path: str
    membership_enabled: bool
    auto_join_enabled: bool
    member_fee_ppm: int
    max_members: int
    market_share_cap_pct: float
    intent_hold_seconds: int
    intent_expire_seconds: int
    gossip_threshold_pct: float
    heartbeat_interval: int
    planner_interval: int
    planner_min_channel_sats: int
    planner_max_channel_sats: int
    planner_default_channel_sats: int
    planner_max_active_channels: int
    daily_expansion_budget_sats: int
    budget_reserve_pct: float
    budget_max_per_channel_pct: float
    max_expansion_feerate_perkb: int
    version: int

    @classmethod
    def from_config(cls, config: HiveConfig) -> 'HiveConfigSnapshot':
        """Create a frozen snapshot from mutable config."""
        return cls(
            db_path=config.db_path,
            membership_enabled=config.membership_enabled,
            auto_join_enabled=config.auto_join_enabled,
            member_fee_ppm=config.member_fee_ppm,
            max_members=config.max_members,
            market_share_cap_pct=config.market_share_cap_pct,
            intent_hold_seconds=config.intent_hold_seconds,
            intent_expire_seconds=config.intent_expire_seconds,
            gossip_threshold_pct=config.gossip_threshold_pct,
            heartbeat_interval=config.heartbeat_interval,
            planner_interval=config.planner_interval,
            planner_min_channel_sats=config.planner_min_channel_sats,
            planner_max_channel_sats=config.planner_max_channel_sats,
            planner_default_channel_sats=config.planner_default_channel_sats,
            planner_max_active_channels=config.planner_max_active_channels,
            daily_expansion_budget_sats=config.daily_expansion_budget_sats,
            budget_reserve_pct=config.budget_reserve_pct,
            budget_max_per_channel_pct=config.budget_max_per_channel_pct,
            max_expansion_feerate_perkb=config.max_expansion_feerate_perkb,
            version=config._version,
        )
