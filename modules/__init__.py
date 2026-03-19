"""
Modules package for cl-hive

This package contains the core modules for the Hive swarm intelligence layer:
- config: Configuration dataclass and snapshot pattern
- database: SQLite persistence with thread-local connections
- protocol: BOLT 8 custom message types and serialization
- handshake: PKI-based handshake protocol
- state_manager: HiveMap distributed state
- gossip: Threshold gossiping and anti-entropy sync
- intent_manager: Intent Lock conflict resolution
- bridge: cl-revenue-ops integration
- membership: Two-tier membership system
- contribution: Contribution ratio tracking
- planner: Topology optimization
- quality_scorer: Peer quality scoring
- governance: Decision engine modes
"""

__version__ = "2.2.6"
