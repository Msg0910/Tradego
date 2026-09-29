"""
Regression tests for M-01 forensic finding remediation.
Guarantees that BrokerConnectivityManager strictly validates state transitions against
VALID_TRANSITIONS and rejects illegal transitions without mutating state.
"""

import unittest

from gateway.broker_connectivity import BrokerConnectivityManager, BrokerConnectivityState
from gateway.recovery import GuardState, TradingGuard
from gateway.security import Tier1AuditLogger


class TestM01Remediation(unittest.TestCase):
    """
    Authoritative regression tests for M-01 remediation:
    1. test_valid_connectivity_transitions
    2. test_invalid_transition_rejected
    3. test_illegal_transition_does_not_mutate_state
    """

    def setUp(self) -> None:
        self.guard = TradingGuard()
        self.guard._state = GuardState.NORMAL
        self.audit_logger = Tier1AuditLogger(log_file_path=None)
        self.conn_mgr = BrokerConnectivityManager(
            trading_guard=self.guard,
            audit_logger=self.audit_logger,
        )

    def tearDown(self) -> None:
        self.audit_logger.close()

    def test_valid_connectivity_transitions(self) -> None:
        """1. Every authorized transition in the VALID_TRANSITIONS graph succeeds."""
        # Initial state is DISCONNECTED
        self.assertEqual(self.conn_mgr.connectivity_state, BrokerConnectivityState.DISCONNECTED)

        # DISCONNECTED -> CONNECTING
        self.conn_mgr.set_connectivity_state(BrokerConnectivityState.CONNECTING)
        self.assertEqual(self.conn_mgr.connectivity_state, BrokerConnectivityState.CONNECTING)

        # CONNECTING -> CONNECTED
        self.conn_mgr.set_connectivity_state(BrokerConnectivityState.CONNECTED)
        self.assertEqual(self.conn_mgr.connectivity_state, BrokerConnectivityState.CONNECTED)

        # CONNECTED -> DEGRADED
        self.conn_mgr.set_connectivity_state(BrokerConnectivityState.DEGRADED)
        self.assertEqual(self.conn_mgr.connectivity_state, BrokerConnectivityState.DEGRADED)

        # DEGRADED -> RECONNECTING
        self.conn_mgr.set_connectivity_state(BrokerConnectivityState.RECONNECTING)
        self.assertEqual(self.conn_mgr.connectivity_state, BrokerConnectivityState.RECONNECTING)

        # RECONNECTING -> CONNECTED
        self.conn_mgr.set_connectivity_state(BrokerConnectivityState.CONNECTED)
        self.assertEqual(self.conn_mgr.connectivity_state, BrokerConnectivityState.CONNECTED)

        # CONNECTED -> DISCONNECTED
        self.conn_mgr.set_connectivity_state(BrokerConnectivityState.DISCONNECTED)
        self.assertEqual(self.conn_mgr.connectivity_state, BrokerConnectivityState.DISCONNECTED)

        # DISCONNECTED -> UNKNOWN
        self.conn_mgr.set_connectivity_state(BrokerConnectivityState.UNKNOWN)
        self.assertEqual(self.conn_mgr.connectivity_state, BrokerConnectivityState.UNKNOWN)

        # UNKNOWN -> DISCONNECTED
        self.conn_mgr.set_connectivity_state(BrokerConnectivityState.DISCONNECTED)
        self.assertEqual(self.conn_mgr.connectivity_state, BrokerConnectivityState.DISCONNECTED)

        # DISCONNECTED -> CONNECTING -> AUTH_FAILED
        self.conn_mgr.set_connectivity_state(BrokerConnectivityState.CONNECTING)
        self.conn_mgr.set_connectivity_state(BrokerConnectivityState.AUTH_FAILED)
        self.assertEqual(self.conn_mgr.connectivity_state, BrokerConnectivityState.AUTH_FAILED)

        # AUTH_FAILED -> DISCONNECTED
        self.conn_mgr.set_connectivity_state(BrokerConnectivityState.DISCONNECTED)
        self.assertEqual(self.conn_mgr.connectivity_state, BrokerConnectivityState.DISCONNECTED)

    def test_invalid_transition_rejected(self) -> None:
        """2. State transitions not in VALID_TRANSITIONS are strictly rejected with ValueError."""
        # Current state: DISCONNECTED
        self.assertEqual(self.conn_mgr.connectivity_state, BrokerConnectivityState.DISCONNECTED)

        # DISCONNECTED -> CONNECTED directly is illegal (must be CONNECTING or UNKNOWN)
        with self.assertRaises(ValueError) as ctx:
            self.conn_mgr.set_connectivity_state(BrokerConnectivityState.CONNECTED)
        self.assertIn("ILLEGAL_CONNECTIVITY_TRANSITION", str(ctx.exception))
        self.assertIn("DISCONNECTED to CONNECTED", str(ctx.exception))

        # DISCONNECTED -> DEGRADED directly is illegal
        with self.assertRaises(ValueError) as ctx:
            self.conn_mgr.set_connectivity_state(BrokerConnectivityState.DEGRADED)
        self.assertIn("ILLEGAL_CONNECTIVITY_TRANSITION", str(ctx.exception))

        # DISCONNECTED -> RECONNECTING directly is illegal
        with self.assertRaises(ValueError) as ctx:
            self.conn_mgr.set_connectivity_state(BrokerConnectivityState.RECONNECTING)
        self.assertIn("ILLEGAL_CONNECTIVITY_TRANSITION", str(ctx.exception))

        # DISCONNECTED -> AUTH_FAILED directly is illegal
        with self.assertRaises(ValueError) as ctx:
            self.conn_mgr.set_connectivity_state(BrokerConnectivityState.AUTH_FAILED)
        self.assertIn("ILLEGAL_CONNECTIVITY_TRANSITION", str(ctx.exception))

    def test_illegal_transition_does_not_mutate_state(self) -> None:
        """3. When an illegal transition is rejected, the current state remains completely unmutated."""
        # Advance cleanly to CONNECTED
        self.conn_mgr.set_connectivity_state(BrokerConnectivityState.CONNECTING)
        self.conn_mgr.set_connectivity_state(BrokerConnectivityState.CONNECTED)
        self.assertEqual(self.conn_mgr.connectivity_state, BrokerConnectivityState.CONNECTED)

        # Attempt illegal transition: CONNECTED -> AUTH_FAILED
        with self.assertRaises(ValueError):
            self.conn_mgr.set_connectivity_state(BrokerConnectivityState.AUTH_FAILED)

        # State must remain CONNECTED (not AUTH_FAILED or corrupted)
        self.assertEqual(self.conn_mgr.connectivity_state, BrokerConnectivityState.CONNECTED)

        # Attempt another illegal transition: CONNECTED -> CONNECTING
        with self.assertRaises(ValueError):
            self.conn_mgr.set_connectivity_state(BrokerConnectivityState.CONNECTING)

        # State must still remain CONNECTED
        self.assertEqual(self.conn_mgr.connectivity_state, BrokerConnectivityState.CONNECTED)


if __name__ == "__main__":
    unittest.main()
