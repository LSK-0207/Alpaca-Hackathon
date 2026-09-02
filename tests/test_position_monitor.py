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
