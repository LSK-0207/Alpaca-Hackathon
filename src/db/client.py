import os
from typing import Any, Dict, List, Optional
from supabase import create_client, Client


class DatabaseClient:
    """Wrapper client for Supabase database operations."""

    def __init__(self, url: Optional[str] = None, key: Optional[str] = None):
        self.url = url or os.getenv("SUPABASE_URL")
        self.key = key or os.getenv("SUPABASE_SERVICE_KEY")
        if not self.url or not self.key:
            self.client: Optional[Client] = None
        else:
            self.client = create_client(self.url, self.key)

    def is_connected(self) -> bool:
        return self.client is not None

    def insert_decision(self, decision_data: Dict[str, Any]) -> Optional[str]:
        """Inserts a decision row and returns the decision_id."""
        if not self.client:
            raise RuntimeError(
                "Database client not initialized. Check SUPABASE_URL and SUPABASE_SERVICE_KEY."
            )
        res = self.client.table("decisions").insert(decision_data).execute()
        if res.data and len(res.data) > 0:
            return res.data[0].get("decision_id")
        return None

    def update_decision_order_id(self, decision_id: str, order_id: str) -> None:
        """Updates the alpaca_order_id on a decision record."""
        if not self.client:
            raise RuntimeError("Database client not initialized.")
        (
            self.client.table("decisions")
            .update({"alpaca_order_id": order_id})
            .eq("decision_id", decision_id)
            .execute()
        )

    def insert_position(self, position_data: Dict[str, Any]) -> Optional[str]:
        """Inserts a new open position record."""
        if not self.client:
            raise RuntimeError("Database client not initialized.")
        res = self.client.table("positions").insert(position_data).execute()
        if res.data and len(res.data) > 0:
            return res.data[0].get("position_id")
        return None

    def get_open_positions(self) -> List[Dict[str, Any]]:
        """Retrieves all open positions."""
        if not self.client:
            raise RuntimeError("Database client not initialized.")
        res = (
            self.client.table("positions")
            .select("*")
            .eq("status", "open")
            .execute()
        )
        return res.data or []

    def check_existing_position_for_decision(self, decision_id: str) -> bool:
        """
        Returns True if a positions row already exists for this decision_id.
        Used by executor.py for idempotency — guards against double-firing on retried cycles.
        """
        if not self.client:
            return False
        res = (
            self.client.table("positions")
            .select("position_id")
            .eq("decision_id", decision_id)
            .limit(1)
            .execute()
        )
        return bool(res.data and len(res.data) > 0)

    def close_position(
        self,
        position_id: str,
        exit_value: float,
        realized_pnl: float,
        close_reason: str,
        closed_at: str,
    ) -> None:
        """Updates position status to closed with exit details."""
        if not self.client:
            raise RuntimeError("Database client not initialized.")
        payload = {
            "exit_value": exit_value,
            "realized_pnl": realized_pnl,
            "close_reason": close_reason,
            "closed_at": closed_at,
            "status": "closed",
        }
        (
            self.client.table("positions")
            .update(payload)
            .eq("position_id", position_id)
            .execute()
        )

    def get_recent_trade_outcomes(self, limit: int = 10) -> List[str]:
        """Returns recent trade outcomes as a list of strings: 'win' or 'loss'."""
        if not self.client:
            return []
        res = (
            self.client.table("positions")
            .select("realized_pnl")
            .eq("status", "closed")
            .order("closed_at", desc=True)
            .limit(limit)
            .execute()
        )
        if not res.data:
            return []
        return [
            "win" if (row.get("realized_pnl") or 0) > 0 else "loss"
            for row in res.data
        ]


_db_instance: Optional[DatabaseClient] = None


def get_db_client() -> DatabaseClient:
    global _db_instance
    if _db_instance is None:
        _db_instance = DatabaseClient()
    return _db_instance
