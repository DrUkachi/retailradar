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
        data_points: int = 0,
    ) -> int:
        if not valid_prices:
            return 0

        cheapest = min(valid_prices.values())
        score = 0

        # Coverage: up to 3 points for how many retailers have the product
        score += min(len(valid_prices), 3)

        # Price vs rolling minimum (up to 5 points)
        # Only trust the historical signal when we have enough data points.
        # With 1-2 observations the "observed low" IS the current price — misleading.
        if rolling_min and rolling_min > 0 and data_points >= 3:
            pct_above_min = (cheapest - rolling_min) / rolling_min * 100
            if pct_above_min <= 2:
                score += 5   # at or near all-time observed low
            elif pct_above_min <= 8:
                score += 4   # very close to low
            elif pct_above_min <= 15:
                score += 3   # reasonably close
            elif pct_above_min <= 25:
                score += 2   # somewhat elevated
            elif pct_above_min <= 40:
                score += 1   # elevated
            else:
                score += 0   # well above historical low
        else:
            # Thin or no history — neutral signal, don't reward or penalise
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
        data_points: int = 0,
    ) -> str:
        """Generate a plain-English buy/wait recommendation."""

        if not valid_prices or cheapest_retailer is None:
            return (
                "Could not retrieve reliable pricing data across retailers. "
                "Try searching with a more specific product name or model number."
            )

        parts = []
        # Convert internal retailer key to display name
        _display = {"best_buy": "Best Buy", "amazon": "Amazon",
                    "walmart": "Walmart", "target": "Target"}
        retailer_cap = _display.get(cheapest_retailer.lower(), cheapest_retailer.replace("_", " ").title())

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
                f"That's {price_spread_pct}% cheaper than {_display.get(max_retailer.lower(), max_retailer.replace('_', ' ').title())} "
                f"(${max_price:,.2f})."
            )

        # Part 3: Historical context
        if rolling_min and cheapest_price and data_points >= 3:
            # Enough history to make a meaningful comparison
            pct_above = (cheapest_price - rolling_min) / rolling_min * 100
            if pct_above <= 2:
                parts.append(
                    f"The current price matches the lowest we've ever observed "
                    f"across {data_points} price checks (low: ${rolling_min:,.2f}). Strong buying signal."
                )
            elif pct_above <= 10:
                parts.append(
                    f"Price is close to the observed low of ${rolling_min:,.2f} "
                    f"({pct_above:.1f}% above) across {data_points} observations. Good value."
                )
            elif pct_above <= 25:
                parts.append(
                    f"Price is {pct_above:.1f}% above the observed low of ${rolling_min:,.2f}. "
                    f"Consider waiting for a sale or price drop."
                )
            else:
                parts.append(
                    f"Price is {pct_above:.1f}% above the observed low of ${rolling_min:,.2f}. "
                    f"Historically, better prices have been available — consider waiting."
                )
        elif rolling_min and cheapest_price and data_points > 0:
            # 1-2 observations — too thin to draw real conclusions
            parts.append(
                f"🆕 Limited history ({data_points} observation{'s' if data_points > 1 else ''}) — "
                f"not enough data to confirm whether this is a good price yet. "
                f"Price score will improve as more observations are collected."
            )
        else:
            parts.append(
                "🆕 First time we've tracked this product — no historical data to compare against yet. "
                "Check back after a few queries to get a more accurate deal score."
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

    def generate_price_context(
        self,
        valid_prices: dict,
        retailer_results: dict,
        rolling_min: float | None,
        cheapest_price: float | None,
    ) -> str:
        """
        Generate a one-sentence market context note explaining WHY the price
        is what it is — e.g. condition mix, price trend, retailer spread.
        This is what ChatGPT provides that raw price comparisons miss.
        """
        if not valid_prices or not cheapest_price:
            return ""

        notes = []

        # Check if any result is refurbished/renewed
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
                    f"Note: The listed price is for a renewed/refurbished unit — "
                    f"new units may be priced higher or unavailable at this retailer."
                )

        # Price trend context from rolling min
        if rolling_min and cheapest_price:
            pct_above = (cheapest_price - rolling_min) / rolling_min * 100
            if pct_above <= 2:
                notes.append("Prices are at or near the lowest observed — this may reflect a recent sale or product cycle discount.")
            elif pct_above > 40:
                notes.append(f"Prices are elevated vs the historical low of ${rolling_min:,.2f} — check back during major sale events (Prime Day, Black Friday).")

        return " ".join(notes)