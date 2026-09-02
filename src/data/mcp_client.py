import os
import json
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
        if self.session is not None:
            logger.warning("MCP Client already connected. Skipping duplicate connect().")
            return

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
            try:
                await self.session.__aexit__(None, None, None)
            except Exception as e:
                logger.warning(f"Error closing MCP session: {e}")
            finally:
                self.session = None
        if self._cm:
            try:
                await self._cm.__aexit__(None, None, None)
            except Exception as e:
                logger.warning(f"Error closing MCP stdio client: {e}")
            finally:
                self._cm = None
        self._tool_catalog = {}
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
        """Finds a tool name in the catalog by matching ALL given keywords first, then ANY."""
        # Try all-keywords match first (most specific)
        for tool_name in self._tool_catalog:
            if all(kw in tool_name for kw in keywords):
                return tool_name
        # Partial match — any single keyword
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
                for item in clock:
                    text = getattr(item, "text", str(item))
                    if "is_open" in text:
                        try:
                            data = json.loads(text)
                            return bool(data.get("is_open", False))
                        except Exception:
                            return '"is_open": true' in text or "'is_open': True" in text
            if isinstance(clock, dict):
                return bool(clock.get("is_open", False))
            return False
        except Exception as e:
            logger.warning(f"Could not determine market open status: {e}. Assuming CLOSED (fail-safe).")
            return False

    async def get_account(self) -> Dict[str, Any]:
        """Returns the Alpaca account information as raw MCP content."""
        tool_name = (
            "get_account_info"
            if "get_account_info" in self._tool_catalog
            else self._find_tool("account", fallback="get_account")
        )
        result = await self.call_tool(tool_name)
        return getattr(result, "content", result)

    async def get_account_info(self) -> Dict[str, Any]:
        """Returns parsed account dict from Alpaca MCP. Falls back to empty dict on parse error."""
        raw = await self.get_account()
        if isinstance(raw, list):
            for item in raw:
                text = getattr(item, "text", str(item))
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    continue
        if isinstance(raw, dict):
            return raw
        logger.warning(f"Could not parse account data into dict. Raw type: {type(raw)}")
        return {}

    async def get_historical_bars(
        self, symbol: str, timeframe: str = "1Day", limit: int = 60
    ) -> List[float]:
        """
        Retrieves historical bars for technical analysis.
        Returns a list of closing prices (floats), oldest first.
        Returns empty list on failure (caller must handle the short-list case).
        """
        # Try multiple parameter conventions: 'symbols' (list/string) vs 'symbol'
        tool_name = self._find_tool("bar", fallback="get_bars")
        from datetime import datetime, timedelta
        start_str = (datetime.utcnow() - timedelta(days=100)).strftime('%Y-%m-%dT%H:%M:%SZ')

        raw = None
        for args in (
            {"symbols": symbol, "timeframe": timeframe, "limit": limit, "start": start_str},
            {"symbol": symbol, "timeframe": timeframe, "limit": limit, "start": start_str},
            {"symbols": symbol, "timeframe": timeframe, "limit": limit},
        ):
            try:
                result = await self.call_tool(tool_name, args)
                raw = getattr(result, "content", result)
                break
            except Exception as e:
                logger.debug(f"get_historical_bars call with args {args} failed: {e}")
                continue
        
        if raw is None:
            logger.warning(f"All get_historical_bars attempts failed for {symbol}.")
            return []

        def extract_closes(data: Any) -> List[float]:
            closes: List[float] = []
            if isinstance(data, list):
                for item in data:
                    text = getattr(item, "text", None)
                    if text:
                        try:
                            parsed = json.loads(text)
                            extracted = extract_closes(parsed)
                            if extracted:
                                return extracted
                        except Exception as e:
                            logger.error(f"extract_closes list parsing error: {e}")
                    elif isinstance(item, dict):
                        c = item.get("c") or item.get("close") or item.get("Close")
                        if c is not None:
                            try:
                                closes.append(float(c))
                            except (TypeError, ValueError):
                                pass
            elif isinstance(data, dict):
                # Unwrap FastMCP 'data' envelope if present
                if "data" in data and isinstance(data["data"], dict):
                    data = data["data"]
                
                # May be {"bars": [...]} or {"AAPL": [...]} or {"bars": {"AAPL": [...]}}
                bars = (
                    data.get("bars")
                    or data.get(symbol)
                    or data.get(symbol.lower())
                    or []
                )
                logger.warning(f"bars after data.get: type={type(bars)}, value={str(bars)[:200]}")
                # bars can itself be a dict like {"AAPL": [...]}
                if isinstance(bars, dict):
                    bars = bars.get(symbol) or bars.get(symbol.lower()) or []
                logger.warning(f"bars after symbol unwrap: type={type(bars)}, len={len(bars) if isinstance(bars, list) else 0}")
                if isinstance(bars, list):
                    for bar in bars:
                        if isinstance(bar, dict):
                            c = bar.get("c") or bar.get("close") or bar.get("Close")
                            if c is not None:
                                try:
                                    closes.append(float(c))
                                except (TypeError, ValueError):
                                    pass
            return closes

        closes = extract_closes(raw)
        if not closes:
            logger.warning(f"No bar close prices extracted for {symbol}. Raw type: {type(raw)}")
            logger.warning(f"RAW DATA: {raw}")
        return closes

    async def get_option_chain_or_snapshots(
        self, underlying: str
    ) -> List[Dict[str, Any]]:
        """
        Retrieves option snapshots / quotes for an underlying symbol.
        Tries multiple parameter key names for compatibility with different MCP server versions.
        """
        tool_name = self._find_tool("option", "snapshot", fallback="")
        if not tool_name:
            tool_name = self._find_tool("option", "chain", fallback="")
        if not tool_name:
            tool_name = self._find_tool("option", "quote", fallback="get_option_snapshots")

        # The Alpaca options/snapshots endpoint requires 'symbols' (plural, comma-separated).
        # Try all known parameter naming conventions in order.
        raw = None
        for args in (
            {"symbols": underlying},
            {"underlying_symbol": underlying},
            {"symbol": underlying},
            {"ticker": underlying},
        ):
            try:
                result = await self.call_tool(tool_name, args)
                raw = getattr(result, "content", result)
                # Check if it's actually a 400/error response wrapped in content
                if raw is not None:
                    break
            except Exception as e:
                logger.debug(f"Option chain call with args {args} failed: {e}")
                continue

        if raw is None:
            logger.warning(f"All option chain call attempts failed for {underlying}.")
            return []

        def extract_chain(data: Any) -> List[Dict[str, Any]]:
            contracts: List[Dict[str, Any]] = []
            if isinstance(data, list):
                for item in data:
                    text = getattr(item, "text", None)
                    if text:
                        try:
                            parsed = json.loads(text)
                            extracted = extract_chain(parsed)
                            if extracted:
                                return extracted
                        except Exception:
                            pass
                    elif isinstance(item, dict):
                        contracts.append(item)
            elif isinstance(data, dict):
                # {occ_symbol: snapshot_dict, ...} format
                for key, val in data.items():
                    if isinstance(val, dict):
                        val = dict(val)  # copy to avoid mutating original
                        val["occ_symbol"] = key
                        # Flatten greeks
                        greeks = val.get("greeks", {}) or {}
                        val["delta"] = float(greeks.get("delta", 0.0) or 0.0)
                        val["gamma"] = float(greeks.get("gamma", 0.0) or 0.0)
                        val["theta"] = float(greeks.get("theta", 0.0) or 0.0)
                        val["vega"] = float(greeks.get("vega", 0.0) or 0.0)
                        # Flatten latest quote
                        quote = val.get("latestQuote") or val.get("latest_quote") or {}
                        if quote:
                            val["bid"] = float(quote.get("bp", quote.get("bid", 0.0)) or 0.0)
                            val["ask"] = float(quote.get("ap", quote.get("ask", 0.0)) or 0.0)
                        # Parse expiry from OCC symbol if not present
                        if "expiry" not in val and len(key) >= 15:
                            # OCC format: {ticker}{YYMMDD}{C|P}{strike}
                            try:
                                import re
                                from datetime import datetime
                                m = re.match(r"^[A-Z]+(\d{6})[CP]", key)
                                if m:
                                    val["expiry"] = datetime.strptime(m.group(1), "%y%m%d").date().isoformat()
                            except Exception:
                                pass
                        # Parse option_type if not present
                        if "option_type" not in val and len(key) >= 15:
                            try:
                                import re
                                m = re.match(r"^[A-Z]+\d{6}([CP])", key)
                                if m:
                                    val["option_type"] = "call" if m.group(1) == "C" else "put"
                            except Exception:
                                pass
                        # Parse strike if not present
                        if "strike" not in val and len(key) >= 15:
                            try:
                                import re
                                m = re.match(r"^[A-Z]+\d{6}[CP](\d{8})$", key)
                                if m:
                                    val["strike"] = int(m.group(1)) / 1000.0
                            except Exception:
                                pass
                        contracts.append(val)
            return contracts

        return extract_chain(raw)

    async def get_option_quote(self, occ_symbol: str) -> Optional[Dict[str, Any]]:
        """
        Gets the latest quote (bid/ask/mid) for a specific option contract.
        Returns dict with: bid, ask, mid_price, delta.
        Returns None on any failure.
        """
        try:
            tool_name = self._find_tool("option", "snapshot", fallback="")
            if not tool_name:
                tool_name = self._find_tool("option", "quote", fallback="get_option_snapshots")

            # Try both single-symbol and list parameter forms
            raw = None
            for args in ({"symbols": [occ_symbol]}, {"symbol": occ_symbol}):
                try:
                    result = await self.call_tool(tool_name, args)
                    raw = getattr(result, "content", result)
                    break
                except Exception:
                    continue

            if raw is None:
                return None

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
                    snap = data.get(occ_symbol) or data
                    if isinstance(snap, dict):
                        greeks = snap.get("greeks") or {}
                        quote = snap.get("latestQuote") or snap.get("latest_quote") or {}
                        bid = float(quote.get("bp", quote.get("bid", 0.0)) or 0.0)
                        ask = float(quote.get("ap", quote.get("ask", 0.0)) or 0.0)
                        mid = (bid + ask) / 2.0 if (bid + ask) > 0 else 0.0
                        return {
                            "bid": bid,
                            "ask": ask,
                            "mid_price": mid,
                            "delta": float(greeks.get("delta", 0.0) or 0.0),
                        }
                return None

            return parse_quote(raw)
        except Exception as e:
            logger.error(f"Failed to fetch option quote for {occ_symbol}: {e}")
            return None

    async def get_order_status(self, order_id: str) -> Dict[str, Any]:
        """Polls the status of a placed order by order_id."""
        try:
            tool_name = self._find_tool("order", "status", fallback="")
            if not tool_name:
                tool_name = self._find_tool("get", "order", fallback="get_order")
            result = await self.call_tool(tool_name, {"order_id": order_id})
            raw = getattr(result, "content", result)
            if isinstance(raw, list):
                for item in raw:
                    text = getattr(item, "text", None)
                    if text:
                        try:
                            parsed = json.loads(text)
                            if isinstance(parsed, dict):
                                return parsed
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
    ) -> Any:
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


def get_mcp_client() -> AlpacaMCPClient:
    """Creates a NEW AlpacaMCPClient instance. Do NOT cache this — one per cycle."""
    return AlpacaMCPClient()
