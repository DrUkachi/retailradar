"""
Deal Scorer & Verdict Generator
Computes a 0-10 deal score and generates plain-English buy/wait recommendations.
"""

from typing import Optional


class DealScorer:
    """
    Deal score logic:
      - Base: how many retailers have this product (coverage score)
      - Price signal: how far current cheapest price is from rolling minimum
      - Spread bonus: larger spread = more value in finding the cheapest
    """

    def compute_deal_score(
        self,
        valid_prices: dict[str, float],
        rolling_min: Optional[float],
    ) -> int:
        if not valid_prices:
            return 0

        cheapest = min(valid_prices.values())
        score = 0

        # Coverage: 1 point per retailer found, max 3
        score += min(len(valid_prices), 3)

        # Price vs rolling minimum (up to 6 points)
        if rolling_min and rolling_min > 0:
            pct_above_min = (cheapest - rolling_min) / rolling_min * 100
            if pct_above_min <= 2:
                score += 6   # at or near all-time observed low
            elif pct_above_min <= 8:
                score += 5   # very close to low
            elif pct_above_min <= 15:
                score += 3   # reasonably close
            elif pct_above_min <= 25:
                score += 2   # somewhat elevated
            elif pct_above_min <= 40:
                score += 1   # elevated
            else:
                score += 0   # well above historical low
        else:
            # No historical data — neutral middle score
            score += 2

        # Spread bonus: finding cheapest is more valuable when spread is high
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
        valid_prices: dict[str, float],
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

        # Part 1: Cheapest retailer finding
        if len(valid_prices) == 1:
            parts.append(
                f"{retailer_cap} is the only retailer with a confirmed price match "
                f"at ${cheapest_price:,.2f}."
            )
        else:
            parts.append(f"{retailer_cap} is cheapest at ${cheapest_price:,.2f}.")

        # Part 2: Spread context
        if price_spread_pct and price_spread_pct >= 5:
            max_retailer = max(valid_prices, key=valid_prices.get)
            max_price    = valid_prices[max_retailer]
            parts.append(
                f"That's {price_spread_pct}% cheaper than {max_retailer.capitalize()} "
                f"(${max_price:,.2f})."
            )

        # Part 3: Historical context
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

        # Part 4: Deal score summary
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