"""
Deal Scorer & Verdict Generator
Computes a 0-10 deal score and generates plain-English buy/wait recommendations.
"""

from typing import Optional


class DealScorer:

    def compute_deal_score(
        self,
        valid_prices: dict,
        rolling_min: Optional[float],
    ) -> int:
        if not valid_prices:
            return 0

        cheapest = min(valid_prices.values())
        score = 0

        score += min(len(valid_prices), 3)  # coverage: max 3 pts

        if rolling_min and rolling_min > 0:
            pct_above_min = (cheapest - rolling_min) / rolling_min * 100
            if pct_above_min <= 2:
                score += 5
            elif pct_above_min <= 8:
                score += 4
            elif pct_above_min <= 15:
                score += 3
            elif pct_above_min <= 25:
                score += 2
            elif pct_above_min <= 40:
                score += 1
            else:
                score += 0
        else:
            score += 2  # no history — neutral

        if len(valid_prices) >= 2:
            max_price = max(valid_prices.values())
            if max_price > 0:
                spread_pct = (max_price - cheapest) / max_price * 100
                if spread_pct >= 15:
                    score += 2
                elif spread_pct >= 8:
                    score += 1

        return min(int(score), 10)

    def generate_verdict(
        self,
        valid_prices: dict,
        cheapest_retailer: Optional[str],
        cheapest_price: Optional[float],
        price_spread_pct: Optional[float],
        deal_score: int,
        rolling_min: Optional[float],
    ) -> str:
        if not valid_prices or cheapest_retailer is None:
            return (
                "Could not retrieve reliable pricing data across retailers. "
                "Try searching with a more specific product name or model number."
            )

        parts = []
        retailer_cap = cheapest_retailer.capitalize()

        if len(valid_prices) == 1:
            parts.append(
                f"{retailer_cap} is the only retailer with a confirmed price match "
                f"at ${cheapest_price:,.2f}."
            )
        else:
            parts.append(f"{retailer_cap} is cheapest at ${cheapest_price:,.2f}.")

        if price_spread_pct and price_spread_pct >= 5:
            max_retailer = max(valid_prices, key=valid_prices.get)
            max_price    = valid_prices[max_retailer]
            parts.append(
                f"That's {price_spread_pct}% cheaper than {max_retailer.capitalize()} "
                f"(${max_price:,.2f})."
            )

        if rolling_min and cheapest_price:
            pct_above = (cheapest_price - rolling_min) / rolling_min * 100
            if pct_above <= 2:
                parts.append(
                    f"The current price is at or near the lowest we have observed "
                    f"(observed low: ${rolling_min:,.2f}). This is a strong buying signal."
                )
            elif pct_above <= 10:
                parts.append(
                    f"Price is close to the observed low of ${rolling_min:,.2f} "
                    f"({pct_above:.1f}% above). Good value."
                )
            elif pct_above <= 25:
                parts.append(
                    f"Price is {pct_above:.1f}% above the observed low of ${rolling_min:,.2f}. "
                    f"Consider waiting for a sale or price drop."
                )
            else:
                parts.append(
                    f"Price is {pct_above:.1f}% above the observed low of ${rolling_min:,.2f}. "
                    f"Historically, better prices have been available."
                )
        else:
            parts.append(
                "No historical price data available yet for this product "
                "(deal score improves over time as price history builds)."
            )

        if deal_score >= 8:
            recommendation = "✅ Buy now — this is a strong deal."
        elif deal_score >= 6:
            recommendation = "👍 Good time to buy."
        elif deal_score >= 4:
            recommendation = "⏳ Decent price — buying is reasonable, but a better deal may come."
        elif deal_score >= 2:
            recommendation = "🔍 Price is elevated — consider waiting for a promotion."
        else:
            recommendation = "❌ Poor value right now — wait for a better price."

        parts.append(f"Deal Score: {deal_score}/10. {recommendation}")

        return " ".join(parts)

    def generate_price_context(
        self,
        valid_prices: dict,
        retailer_results: dict,
        rolling_min: Optional[float],
        cheapest_price: Optional[float],
    ) -> str:
        """
        Returns a one-sentence market context note when it adds real value:
        - Flags when the cheapest result is refurbished vs new prices elsewhere
        - Notes when prices are at or well above the historical low
        Empty string if nothing notable to add.
        """
        if not valid_prices or not cheapest_price:
            return ""

        notes = []

        conditions = {
            retailer: result.get("condition", "new")
            for retailer, result in retailer_results.items()
            if result and result.get("price") is not None
        }
        has_renewed = any(c in ("renewed", "used") for c in conditions.values())
        all_new     = all(c == "new" for c in conditions.values())

        if has_renewed and not all_new:
            renewed_retailers = [r for r, c in conditions.items() if c in ("renewed", "used")]
            new_retailers     = [r for r, c in conditions.items() if c == "new"]
            if renewed_retailers and new_retailers:
                renewed_prices = [valid_prices[r] for r in renewed_retailers if r in valid_prices]
                new_prices     = [valid_prices[r] for r in new_retailers     if r in valid_prices]
                if renewed_prices and new_prices:
                    notes.append(
                        f"Note: {renewed_retailers[0].capitalize()} is showing a renewed/refurbished listing "
                        f"at ${min(renewed_prices):,.2f} vs new at ${min(new_prices):,.2f} "
                        f"at {new_retailers[0].capitalize()}."
                    )
            elif renewed_retailers:
                notes.append(
                    "Note: The listed price is for a renewed/refurbished unit — "
                    "new units may be priced higher or unavailable at this retailer."
                )

        if rolling_min and cheapest_price:
            pct_above = (cheapest_price - rolling_min) / rolling_min * 100
            if pct_above <= 2:
                notes.append(
                    "Prices are at or near the lowest observed — "
                    "this may reflect a recent sale or product cycle discount."
                )
            elif pct_above > 40:
                notes.append(
                    f"Prices are elevated vs the historical low of ${rolling_min:,.2f} — "
                    "check back during major sale events (Prime Day, Black Friday)."
                )

        return " ".join(notes)
