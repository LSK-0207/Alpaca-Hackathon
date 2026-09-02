import pytest
from unittest.mock import AsyncMock, MagicMock
from src.execution.executor import execute_order


@pytest.mark.asyncio
async def test_execute_order_idempotency_guard():
    db_mock = MagicMock()
    db_mock.is_connected.return_value = True
    # Simulate that this decision ID already has an open position
    db_mock.check_existing_position_for_decision.return_value = True

    mcp_mock = AsyncMock()

    result = await execute_order(
        occ_symbol="AAPL260920C00150000",
        qty=1,
        decision_id="dec-123",
        mcp_client=mcp_mock,
        db_client=db_mock,
    )

    assert result["status"] == "skipped_duplicate"
    mcp_mock.place_option_order.assert_not_called()


@pytest.mark.asyncio
async def test_execute_order_success():
    db_mock = MagicMock()
    db_mock.is_connected.return_value = True
    db_mock.check_existing_position_for_decision.return_value = False

    mcp_mock = AsyncMock()
    mcp_mock.place_option_order.return_value = {"id": "order-123", "status": "accepted"}
    mcp_mock.get_order_status.return_value = {"status": "filled"}

    result = await execute_order(
        occ_symbol="AAPL260920C00150000",
        qty=2,
        decision_id="dec-123",
        mcp_client=mcp_mock,
        db_client=db_mock,
    )

    mcp_mock.place_option_order.assert_called_once_with(
        occ_symbol="AAPL260920C00150000", qty=2, side="buy"
    )
    assert result["order_id"] == "order-123"
    assert result["status"] == "filled"
