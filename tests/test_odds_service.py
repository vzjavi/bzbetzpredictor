import unittest
from unittest.mock import patch

from odds_service import (
    _extract_consensus_total,
    _select_best_offer,
    enrich_predictions_with_odds,
)


class OddsServiceTests(unittest.TestCase):
    def setUp(self):
        self.event = {
            "id": "event-1",
            "home_team": "Houston Astros",
            "away_team": "Texas Rangers",
            "bookmakers": [
                {
                    "key": "draftkings",
                    "title": "DraftKings",
                    "markets": [
                        {
                            "key": "totals",
                            "outcomes": [
                                {"name": "Over", "price": -110, "point": 8.5},
                                {"name": "Under", "price": -110, "point": 8.5},
                            ],
                        }
                    ],
                },
                {
                    "key": "fanduel",
                    "title": "FanDuel",
                    "markets": [
                        {
                            "key": "totals",
                            "outcomes": [
                                {"name": "Over", "price": -105, "point": 8.0},
                                {"name": "Under", "price": -115, "point": 8.0},
                            ],
                        }
                    ],
                },
                {
                    "key": "betmgm",
                    "title": "BetMGM",
                    "markets": [
                        {
                            "key": "totals",
                            "outcomes": [
                                {"name": "Over", "price": 100, "point": 8.5},
                                {"name": "Under", "price": -120, "point": 9.0},
                            ],
                        }
                    ],
                },
            ],
        }

    def test_extracts_consensus_and_book_prices(self):
        parsed = _extract_consensus_total(self.event)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["market_total"], 8.5)
        self.assertEqual(parsed["bookmaker_count"], 3)
        self.assertEqual(len(parsed["offers"]), 6)

        dk = next(row for row in parsed["bookmaker_lines"] if row["bookmaker"] == "DraftKings")
        self.assertEqual(dk["over_price"], -110)
        self.assertEqual(dk["under_price"], -110)

    def test_best_over_prefers_lower_total_before_price(self):
        parsed = _extract_consensus_total(self.event)
        best = _select_best_offer(parsed["offers"], "OVER")
        self.assertEqual(best["bookmaker"], "FanDuel")
        self.assertEqual(best["total"], 8.0)
        self.assertEqual(best["price"], -105)

    def test_best_under_prefers_higher_total(self):
        parsed = _extract_consensus_total(self.event)
        best = _select_best_offer(parsed["offers"], "UNDER")
        self.assertEqual(best["bookmaker"], "BetMGM")
        self.assertEqual(best["total"], 9.0)
        self.assertEqual(best["price"], -120)

    def test_same_line_uses_better_american_price(self):
        offers = [
            {"bookmaker": "Book A", "direction": "OVER", "total": 8.5, "price": -110},
            {"bookmaker": "Book B", "direction": "OVER", "total": 8.5, "price": -105},
            {"bookmaker": "Book C", "direction": "OVER", "total": 8.5, "price": 100},
        ]
        best = _select_best_offer(offers, "OVER")
        self.assertEqual(best["bookmaker"], "Book C")
        self.assertEqual(best["price"], 100)

    @patch("odds_service.fetch_totals_market")
    def test_enrichment_exposes_best_book_and_best_edge(self, mock_fetch):
        parsed = _extract_consensus_total(self.event)
        mock_fetch.return_value = [
            {
                "event_id": "event-1",
                "home_team": "Houston Astros",
                "away_team": "Texas Rangers",
                "commence_time": None,
                **parsed,
            }
        ]
        predictions = [
            {
                "sport": "MLB",
                "team1": "Rangers",
                "team2": "Astros",
                "away_team_raw": "Texas Rangers",
                "home_team_raw": "Houston Astros",
                "predicted_total": 10.0,
                "game_time": None,
            }
        ]

        enriched = enrich_predictions_with_odds(predictions, "MLB")[0]
        self.assertEqual(enriched["pick"], "OVER")
        self.assertEqual(enriched["market_total"], 8.5)
        self.assertEqual(enriched["best_book"], "FanDuel")
        self.assertEqual(enriched["best_line"], 8.0)
        self.assertEqual(enriched["best_price"], -105)
        self.assertEqual(enriched["edge"], 1.5)
        self.assertEqual(enriched["best_edge"], 2.0)
        self.assertEqual(enriched["line_improvement"], 0.5)


if __name__ == "__main__":
    unittest.main()
