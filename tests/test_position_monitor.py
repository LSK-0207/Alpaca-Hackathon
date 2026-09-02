from datetime import date
from src.execution.position_monitor import should_close_position


def test_position_monitor_exits():
    today = date(2026, 9, 10)

    # 1. Target hit (+50% or higher gain)
    # Entry cost $2.00, current mid $3.00 (+50%)
    assert should_close_position(
        entry_cost=2.00,
        current_mid=3.00,
        expiry_date=date(2026, 9, 20),
        current_date=today,
    ) == "target_hit"

    # 2. Stop hit (-50% or worse loss)
    # Entry cost $2.00, current mid $1.00 (-50%)
    assert should_close_position(
        entry_cost=2.00,
        current_mid=1.00,
        expiry_date=date(2026, 9, 20),
        current_date=today,
    ) == "stop_hit"

    # 3. Expiry exit (<= 1 day left to expiry)
    assert should_close_position(
        entry_cost=2.00,
        current_mid=2.10,
        expiry_date=date(2026, 9, 11),
        current_date=today,
    ) == "expiry"

    # 4. Stay open (P&L between -50% and +50%, > 1 day to expiry)
    assert should_close_position(
        entry_cost=2.00,
        current_mid=2.20,
        expiry_date=date(2026, 9, 20),
        current_date=today,
    ) is None


import pytest
from unittest.mock import AsyncMock, MagicMock
from src.execution.position_monitor import monitor_open_positions

@pytest.mark.asyncio
async def test_monitor_open_positions_handles_failures():
    db_mock = MagicMock()
    db_mock.is_connected.return_value = True
    # One open position
    db_mock.get_open_positions.return_value = [
        {
            "position_id": "pos-1",
            "occ_symbol": "AAPL260920C00150000",
            "entry_cost": 2.00,
            "qty": 1,
        }
    ]

    mcp_mock = AsyncMock()

    # Scenario 1: Quote is None (failed quote)
    mcp_mock.get_option_quote.return_value = None
    
    closed = await monitor_open_positions(
        mcp_client=mcp_mock, db_client=db_mock, current_date=date(2026, 9, 10)
    )
    
    assert len(closed) == 0
    db_mock.close_position.assert_not_called()

    # Scenario 2: Quote succeeds and triggers stop-loss, but sell order fails
    # entry_cost = 2.00, mid = 1.00 (-50%) -> Stop loss
    mcp_mock.get_option_quote.return_value = {"bid": 1.00, "ask": 1.00, "mid_price": 1.00}
    mcp_mock.place_option_order.side_effect = Exception("Network error")
    
    closed = await monitor_open_positions(
        mcp_client=mcp_mock, db_client=db_mock, current_date=date(2026, 9, 10)
    )
    
    assert len(closed) == 0
    mcp_mock.place_option_order.assert_called_once()
    db_mock.close_position.assert_not_called()
