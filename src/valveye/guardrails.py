from __future__ import annotations

import re


class ResponseValidator:
    """Validates that LLM responses don't contradict tool-returned data."""

    def validate_price_response(self, response: str, tool_outputs: list[str]) -> list[str]:
        """Check that prices mentioned in response match tool output."""
        warnings: list[str] = []

        for output in tool_outputs:
            # Extract price from tool output (e.g., "当前价 298.00 CNY")
            price_match = re.search(r"当前价\s*([\d.]+)\s*(\w+)", output)
            if not price_match:
                continue

            tool_price = price_match.group(1)
            tool_currency = price_match.group(2)

            # Find all price-like patterns in the response
            # Match patterns like "298.00 CNY", "¥298", "$29.99"
            response_prices = re.findall(
                r"([\d,.]+)\s*(?:USD|CNY|EUR|JPY|GBP|RUB|KRW|¥|\$|€|£)",
                response,
            )

            if response_prices:
                # Normalize: remove commas from numbers
                normalized = [p.replace(",", "") for p in response_prices]
                # Check if any response price matches the tool price
                # Allow small floating point differences
                try:
                    tool_val = float(tool_price)
                    for resp_price in normalized:
                        resp_val = float(resp_price)
                        if abs(tool_val - resp_val) < 0.01:
                            break
                    else:
                        # None of the response prices match
                        if tool_val > 0:
                            warnings.append(
                                f"Response prices {normalized} may not match tool price {tool_price} {tool_currency}"
                            )
                except ValueError:
                    pass

        return warnings

    def validate_game_names(self, response: str, tool_outputs: list[str]) -> list[str]:
        """Check that game names in response come from tool data, not hallucinated."""
        warnings: list[str] = []

        # Extract game names from tool outputs
        known_names: set[str] = set()
        for output in tool_outputs:
            # Tool outputs typically start with the game title
            title_match = re.match(r"^([^|]+)\s*\|", output)
            if title_match:
                known_names.add(title_match.group(1).strip())

            # Also look for game names in "找到游戏：XXX" patterns
            found_match = re.search(r"找到游戏[：:]\s*(.+?)(?:\s*[,，]|$)", output)
            if found_match:
                known_names.add(found_match.group(1).strip())

        if not known_names:
            return warnings

        # Look for game-like names in the response that aren't in known_names
        # This is a heuristic check — we look for quoted names or names after "游戏"
        quoted_names = re.findall(r"[「「]([^」」]+)[」」]", response)
        for name in quoted_names:
            if len(name) > 2 and name not in known_names:
                # Check if it's a known game (partial match)
                matched = any(name in known or known in name for known in known_names)
                if not matched:
                    warnings.append(f"Game name '{name}' not found in tool outputs")

        return warnings

    def validate(
        self,
        response: str,
        tool_outputs: list[str],
        agent_type: str,
    ) -> list[str]:
        """Run all applicable validations for the given agent type."""
        warnings: list[str] = []

        if agent_type in ("price",) or "价" in response or "price" in response.lower():
            warnings.extend(self.validate_price_response(response, tool_outputs))

        warnings.extend(self.validate_game_names(response, tool_outputs))

        return warnings
