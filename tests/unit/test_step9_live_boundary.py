"""
Regression test for Step 9 Live Broker Boundary Verification:
Verifies that LiveBrokerAdapter._execute_live_dispatch() fails safely with
REAL_BROKER_NETWORK_TRANSPORT_UNINITIALIZED when live dispatch is attempted,
confirming zero real broker network transport or order execution.
"""

import unittest

from gateway.broker_adapter import LiveBrokerAdapter
from gateway.broker_connectivity import BrokerConnectivityManager
from gateway.broker_credentials import BrokerCredentialsConfig
from gateway.execution import ExecutionStateManager
from gateway.order_instruction import InstructionState, OrderInstruction, OrderInstructionManager
from gateway.recovery import TradingGuard
from gateway.security import Tier1AuditLogger


class TestStep9LiveBrokerBoundary(unittest.TestCase):
    """Step 9: Live broker boundary verification suite."""

    def setUp(self) -> None:
        self.guard = TradingGuard()
        self.audit = Tier1AuditLogger(log_file_path=None)
        self.conn_mgr = BrokerConnectivityManager(
            trading_guard=self.guard,
            audit_logger=self.audit,
            risk_gate_available=True,
            active_broker="TEST_LIVE_VENUE",
        )
        self.creds = BrokerCredentialsConfig(
            venue_name="TEST_LIVE_VENUE",
            client_id="OP_CLIENT_STEP9",
            access_token="TEST_TOKEN_XYZ",
            account_id="ACC_STEP9",
        )
        self.conn_mgr.set_credentials(self.creds, "op_step9")
        self.conn_mgr.connect("op_step9")

        # Unmocked authoritative LiveBrokerAdapter
        self.live_adapter = LiveBrokerAdapter(
            credentials=self.creds,
            connectivity_manager=self.conn_mgr,
            venue_name="TEST_LIVE_VENUE",
        )

        self.instruction = OrderInstruction(
            instruction_id="INS-STEP9-001",
            intent_id="INT-STEP9-001",
            symbol="NSE:INFY",
            side="BUY",
            quantity=10,
            order_type="LIMIT",
            limit_price=1500.0,
            risk_evaluation_reference="RISK_APPROVED_REF_01",
            approval_reference="OP_APPROVED_REF_01",
            state=InstructionState.VALIDATED,
        )

    def test_live_dispatch_uninitialized_transport_returns_failed(self) -> None:
        """Attempting live dispatch through uninitialized transport fails safely with REAL_BROKER_NETWORK_TRANSPORT_UNINITIALIZED."""
        # Direct adapter dispatch test
        result = self.live_adapter.dispatch(self.instruction)

        self.assertFalse(result.success)
        self.assertEqual(result.outcome, "FAILED")
        self.assertEqual(result.failure_reason, "REAL_BROKER_NETWORK_TRANSPORT_UNINITIALIZED")
        self.assertIn("Live network transport is uninitialized", result.raw_payload.get("error", ""))
        self.assertEqual(result.raw_payload.get("venue"), "TEST_LIVE_VENUE")

        # End-to-end OrderInstructionManager dispatch test
        exec_mgr = ExecutionStateManager(
            trading_guard=self.guard,
            audit_logger=self.audit,
            connectivity_manager=self.conn_mgr,
        )
        instruction_mgr = OrderInstructionManager(
            trading_guard=self.guard,
            audit_logger=self.audit,
            broker_adapter=self.live_adapter,
            execution_manager=exec_mgr,
            connectivity_manager=self.conn_mgr,
        )
        instruction_2 = OrderInstruction(
            instruction_id="INS-STEP9-002",
            intent_id="INT-STEP9-002",
            symbol="NSE:TCS",
            side="BUY",
            quantity=5,
            order_type="LIMIT",
            limit_price=3500.0,
            risk_evaluation_reference="RISK_APPROVED_REF_02",
            approval_reference="OP_APPROVED_REF_02",
            state=InstructionState.VALIDATED,
        )
        instruction_mgr._instructions[instruction_2.instruction_id] = instruction_2

        dispatched = instruction_mgr.dispatch_instruction(instruction_2.instruction_id, "op_step9")
        self.assertEqual(dispatched.state, InstructionState.FAILED)
        self.assertEqual(dispatched.failure_reason, "REAL_BROKER_NETWORK_TRANSPORT_UNINITIALIZED")


if __name__ == "__main__":
    unittest.main()
