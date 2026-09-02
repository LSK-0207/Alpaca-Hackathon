import os
import logging
from typing import Any, Dict, List, Optional
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)


class AlpacaMCPClient:
    """Session management, tool discovery, and typed wrappers for Alpaca MCP server."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        paper: bool = True,
    ):
        self.api_key = api_key or os.getenv("ALPACA_API_KEY", "")
        self.secret_key = secret_key or os.getenv("ALPACA_SECRET_KEY", "")
        self.paper = paper
        self.session: Optional[ClientSession] = None
        self._tool_catalog: Dict[str, Any] = {}
        self._cm = None

    async def connect(self) -> None:
        """Initializes stdio connection to alpaca-mcp-server and discovers available tools."""
        server_params = StdioServerParameters(
            command="uvx",
            args=["alpaca-mcp-server"],
            env={
                "ALPACA_API_KEY": self.api_key,
                "ALPACA_SECRET_KEY": self.secret_key,
                "ALPACA_PAPER_TRADE": "true" if self.paper else "false",
            },
        )
        self._cm = stdio_client(server_params)
        read, write = await self._cm.__aenter__()
        self.session = ClientSession(read, write)
        await self.session.__aenter__()
        await self.session.initialize()

        tools_result = await self.session.list_tools()
        self._tool_catalog = {t.name: t for t in tools_result.tools}
        logger.info(
            f"Connected to Alpaca MCP Server. Discovered tools: {list(self._tool_catalog.keys())}"
        )

    async def disconnect(self) -> None:
        """Gracefully closes the MCP session and stdio process."""
        if self.session:
            await self.session.__aexit__(None, None, None)
            self.session = None
        if self._cm:
            await self._cm.__aexit__(None, None, None)
            self._cm = None
        logger.info("Disconnected from Alpaca MCP Server.")

    async def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        """Calls a tool dynamically on the MCP session."""
        if not self.session:
            raise RuntimeError("MCP Client is not connected. Call connect() first.")
        if name not in self._tool_catalog:
            raise ValueError(
                f"Tool '{name}' not found in MCP tool catalog: {list(self._tool_catalog.keys())}"
            )
        return await self.session.call_tool(name, arguments=arguments or {})

    def _find_tool(self, *keywords: str, fallback: str = "") -> str:
        """Finds a tool name in the catalog by matching any of the given keywords."""
        for tool_name in self._tool_catalog:
            if all(kw in tool_name for kw in keywords):
                return tool_name
        # Partial match — any keyword
        for kw in keywords:
            for tool_name in self._tool_catalog:
                if kw in tool_name:
                    return tool_name
        return fallback

    # ------------------------------------------------------------------ #
    # Typed wrappers                                                       #
    # ------------------------------------------------------------------ #

    async def get_clock(self) -> Dict[str, Any]:
        """Returns the market clock / is_open status."""
        tool_name = self._find_tool("clock", fallback="get_clock")
        result = await self.call_tool(tool_name)
        return getattr(result, "content", result)

    async def market_is_open(self) -> bool:
        """Returns True if the US market is currently open."""
        try:
            clock = await self.get_clock()
            # MCP result may be a list of TextContent objects or a dict
            if isinstance(clock, list):
                # Parse text content for is_open field
                for item in clock:
                    text = getattr(item, "text", str(item))
                    if "is_open" in text:
                        import json
                        try:
                            data = json.loads(text)
                            return bool(data.get("is_open", False))
                        except Exception:
                            return "is_open\": true" in text
            if isinstance(clock, dict):
                return bool(clock.get("is_open", False))
            return False
        except Exception as e:
            logger.warning(f"Could not determine market open status: {e}. Assuming open.")
            return True

    async def get_account(self) -> Dict[str, Any]:
        """Returns the Alpaca account information."""
        tool_name = (
            "get_account_info"
            if "get_account_info" in self._tool_catalog
            else self._find_tool("account", fallback="get_account")
        )
        result = await self.call_tool(tool_name)
        return getattr(result, "content", result)

    async def get_account_info(self) -> Dict[str, Any]:
        """Alias for get_account(); returns parsed account dict."""
        raw = await self.get_account()
        if isinstance(raw, list):
            import json
            for item in raw:
                text = getattr(item, "text", str(item))
                try:
                    return json.loads(text)
                except Exception:
                    continue
        if isinstance(raw, dict):
            return raw
        return {}

    async def get_historical_bars(
        self, symbol: str, timeframe: str = "1Day", limit: int = 60
    ) -> List[float]:
        """
        Retrieves historical bars for technical analysis.
        Returns a list of closing prices (floats), oldest first.
        """
        tool_name = self._find_tool("bar", fallback="get_bars")
        result = await self.call_tool(
            tool_name, {"symbol": symbol, "timeframe": timeframe, "limit": limit}
        )
        raw = getattr(result, "content", result)

        # Parse MCP result into a list of close prices
        import json

        def extract_closes(data: Any) -> List[float]:
            closes: List[float] = []
            if isinstance(data, list):
                for item in data:
                    text = getattr(item, "text", None)
                    if text:
                        try:
                            parsed = json.loads(text)
                            return extract_closes(parsed)
                        except Exception:
                            pass
                    elif isinstance(item, dict):
                        c = item.get("c") or item.get("close") or item.get("Close")
                        if c is not None:
                            closes.append(float(c))
            elif isinstance(data, dict):
                # May be {"bars": [...]} or {"AAPL": [...]}
                bars = data.get("bars") or data.get(symbol) or []
                if isinstance(bars, list):
                    for bar in bars:
                        c = bar.get("c") or bar.get("close") or bar.get("Close")
                        if c is not None:
                            closes.append(float(c))
            return closes

        closes = extract_closes(raw)
        if not closes:
            logger.warning(f"No bar data returned for {symbol}. Raw: {raw}")
        return closes

    async def get_option_chain_or_snapshots(
        self, underlying: str
    ) -> List[Dict[str, Any]]:
        """Retrieves option snapshots / quotes for an underlying symbol."""
        tool_name = self._find_tool("option", "snapshot", fallback="")
        if not tool_name:
            tool_name = self._find_tool("option", "chain", fallback="")
        if not tool_name:
            tool_name = self._find_tool("option", "quote", fallback="get_option_snapshots")

        result = await self.call_tool(tool_name, {"underlying_symbol": underlying})
        raw = getattr(result, "content", result)

        import json

        def extract_chain(data: Any) -> List[Dict[str, Any]]:
            contracts: List[Dict[str, Any]] = []
            if isinstance(data, list):
                for item in data:
                    text = getattr(item, "text", None)
                    if text:
                        try:
                            parsed = json.loads(text)
                            return extract_chain(parsed)
                        except Exception:
                            pass
                    elif isinstance(item, dict):
                        contracts.append(item)
            elif isinstance(data, dict):
                # {symbol: snapshot_dict} or {"snapshots": [...]}
                for key, val in data.items():
                    if isinstance(val, dict):
                        val["occ_symbol"] = key
                        # Flatten greeks
                        greeks = val.get("greeks", {})
                        if greeks:
                            val["delta"] = greeks.get("delta", 0.0)
                            val["gamma"] = greeks.get("gamma", 0.0)
                            val["theta"] = greeks.get("theta", 0.0)
                            val["vega"] = greeks.get("vega", 0.0)
                        # Flatten latest quote
                        quote = val.get("latestQuote") or val.get("latest_quote", {})
                        if quote:
                            val["bid"] = quote.get("bp", quote.get("bid", 0.0))
                            val["ask"] = quote.get("ap", quote.get("ask", 0.0))
                        contracts.append(val)
            return contracts

        return extract_chain(raw)

    async def get_option_quote(self, occ_symbol: str) -> Dict[str, Any]:
        """
        Gets the latest quote (bid/ask/mid) for a specific option contract.
        Returns dict with at minimum: bid, ask, mid_price keys.
        """
        try:
            tool_name = self._find_tool("option", "snapshot", fallback="")
            if not tool_name:
                tool_name = self._find_tool("option", "quote", fallback="get_option_snapshots")

            result = await self.call_tool(tool_name, {"symbols": [occ_symbol]})
            raw = getattr(result, "content", result)

            import json

            def parse_quote(data: Any) -> Dict[str, Any]:
                if isinstance(data, list):
                    for item in data:
                        text = getattr(item, "text", None)
                        if text:
                            try:
                                return parse_quote(json.loads(text))
                            except Exception:
                                pass
                        elif isinstance(item, dict):
                            return item
                elif isinstance(data, dict):
                    snap = data.get(occ_symbol, data)
                    if isinstance(snap, dict):
                        greeks = snap.get("greeks", {})
                        quote = snap.get("latestQuote") or snap.get("latest_quote", {})
                        bid = float(quote.get("bp", quote.get("bid", 0.0)) or 0.0)
                        ask = float(quote.get("ap", quote.get("ask", 0.0)) or 0.0)
                        mid = (bid + ask) / 2.0 if (bid + ask) > 0 else 0.0
                        return {
                            "bid": bid,
                            "ask": ask,
                            "mid_price": mid,
                            "delta": float(greeks.get("delta", 0.0)) if greeks else 0.0,
                        }
                return {"bid": 0.0, "ask": 0.0, "mid_price": 0.0}

            return parse_quote(raw)
        except Exception as e:
            logger.error(f"Failed to fetch option quote for {occ_symbol}: {e}")
            return {"bid": 0.0, "ask": 0.0, "mid_price": 0.0}

    async def get_order_status(self, order_id: str) -> Dict[str, Any]:
        """Polls the status of a placed order by order_id."""
        try:
            tool_name = self._find_tool("order", "status", fallback="")
            if not tool_name:
                tool_name = self._find_tool("get", "order", fallback="get_order")
            result = await self.call_tool(tool_name, {"order_id": order_id})
            raw = getattr(result, "content", result)
            if isinstance(raw, list):
                import json
                for item in raw:
                    text = getattr(item, "text", None)
                    if text:
                        try:
                            return json.loads(text)
                        except Exception:
                            pass
            if isinstance(raw, dict):
                return raw
            return {"status": "unknown"}
        except Exception as e:
            logger.warning(f"Could not fetch order status for {order_id}: {e}")
            return {"status": "unknown"}

    async def place_option_order(
        self, occ_symbol: str, qty: int, side: str = "buy"
    ) -> Dict[str, Any]:
        """
        Places a single-leg market order using the OCC option symbol.
        Order shape exactly per spec §6:
        {
          "symbol": occ_symbol,
          "qty": str(qty),
          "side": side,
          "type": "market",
          "time_in_force": "day"
        }
        Never uses a 'legs' array — this build only places single-leg orders.
        """
        tool_name = (
            "place_option_market_order"
            if "place_option_market_order" in self._tool_catalog
            else self._find_tool("place", "option", fallback="place_order")
        )
        args = {
            "symbol": occ_symbol,
            "qty": str(qty),
            "side": side,
            "type": "market",
            "time_in_force": "day",
        }
        result = await self.call_tool(tool_name, args)
        return getattr(result, "content", result)


_mcp_instance: Optional[AlpacaMCPClient] = None


def get_mcp_client() -> AlpacaMCPClient:
    global _mcp_instance
    if _mcp_instance is None:
        _mcp_instance = AlpacaMCPClient()
    return _mcp_instance
