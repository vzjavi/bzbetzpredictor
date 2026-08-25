import unittest
from unittest.mock import Mock, patch

from tracking_service import (
    _aggregate,
    _canonical_game_key,
    _clv_value,
    _fetch_completed_games,
    _freeze_prediction_from_row,
    _grade_pick,
    _mark_missed_pregame,
    _snapshot_id,
    _teams_match,
)


class TrackingServiceTests(unittest.TestCase):
    def test_grade_over_under_and_push(self):
        self.assertEqual(_grade_pick("OVER", 8.5, 10.0), "WIN")
        self.assertEqual(_grade_pick("OVER", 8.5, 7.0), "LOSS")
        self.assertEqual(_grade_pick("UNDER", 8.5, 7.0), "WIN")
        self.assertEqual(_grade_pick("UNDER", 8.5, 10.0), "LOSS")
        self.assertEqual(_grade_pick("UNDER", 8.0, 8.0), "PUSH")
        self.assertEqual(_grade_pick("PASS", 8.0, 11.0), "PASS")

    def test_team_matching_handles_common_provider_variants(self):
        self.assertTrue(_teams_match("Oakland Athletics", "Athletics"))
        self.assertTrue(_teams_match("Texas Rangers", "Texas Rangers"))
        self.assertFalse(_teams_match("Texas Rangers", "Houston Astros"))

    def test_canonical_key_does_not_depend_on_provider_event_id(self):
        prediction_a = {
            "sport": "MLB",
            "team1": "Astros",
            "team2": "Rangers",
            "away_team_raw": "Houston Astros",
            "home_team_raw": "Texas Rangers",
            "odds_event_id": "provider-id-1",
        }
        prediction_b = {**prediction_a, "odds_event_id": "provider-id-2"}
        self.assertEqual(_snapshot_id(prediction_a), _snapshot_id(prediction_b))
        self.assertEqual(
            _canonical_game_key("MLB", "2026-08-24", "Houston Astros", "Texas Rangers"),
            "MLB:2026-08-24:houston astros:texas rangers",
        )

    def test_line_value_is_positive_when_market_moves_with_pick(self):
        self.assertEqual(_clv_value("OVER", 8.0, 9.0), 1.0)
        self.assertEqual(_clv_value("UNDER", 9.0, 8.0), 1.0)
        self.assertEqual(_clv_value("OVER", 9.0, 8.5), -0.5)
        self.assertEqual(_clv_value("UNDER", 8.0, 8.5), -0.5)
        self.assertIsNone(_clv_value("PASS", 8.0, 9.0))

    @patch("tracking_service.requests.get")
    def test_completed_mlb_statsapi_parsing(self, mock_get):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "dates": [
                {
                    "games": [
                        {
                            "status": {
                                "abstractGameState": "Final",
                                "detailedState": "Final",
                                "codedGameState": "F",
                            },
                            "teams": {
                                "away": {
                                    "score": 4,
                                    "team": {"name": "Texas Rangers"},
                                },
                                "home": {
                                    "score": 6,
                                    "team": {"name": "Houston Astros"},
                                },
                            },
                        }
                    ]
                }
            ]
        }
        mock_get.return_value = response

        games = _fetch_completed_games("MLB", "2026-08-24")
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["away_score"], 4.0)
        self.assertEqual(games[0]["home_score"], 6.0)
        self.assertEqual(games[0]["actual_total"], 10.0)

    def test_started_game_is_frozen_to_original_snapshot(self):
        prediction = {
            "predicted_total": 12.0,
            "market_total": 13.5,
            "edge": -1.5,
            "abs_edge": 1.5,
            "pick": "UNDER",
            "edge_tier": "Strong Edge",
            "best_book": "Live Book",
            "best_line": 14.5,
            "best_price": -110,
            "best_abs_edge": 2.5,
        }
        tracked = {
            "bz_total": "9.2",
            "market_total": "8.5",
            "edge": "0.7",
            "pick": "OVER",
            "edge_tier": "Lean",
            "bookmaker_count": "6",
            "model_version": "mlb_form_pitching_v2",
        }
        result = _freeze_prediction_from_row(prediction, tracked)
        self.assertEqual(result["predicted_total"], 9.2)
        self.assertEqual(result["market_total"], 8.5)
        self.assertEqual(result["pick"], "OVER")
        self.assertEqual(result["edge"], 0.7)
        self.assertIsNone(result["abs_edge"])
        self.assertIsNone(result["best_line"])
        self.assertTrue(result["pick_locked"])

    def test_missed_pregame_never_displays_live_pick(self):
        prediction = {
            "market_total": 11.5,
            "edge": 2.0,
            "pick": "OVER",
            "best_line": 11.0,
        }
        result = _mark_missed_pregame(prediction)
        self.assertEqual(result["pick"], "NO BET")
        self.assertIsNone(result["market_total"])
        self.assertIsNone(result["best_line"])
        self.assertEqual(result["tracking_status"], "MISSED_PREGAME")

    def test_aggregate_uses_standard_minus_110_flat_units(self):
        rows = [
            {"result": "WIN"},
            {"result": "WIN"},
            {"result": "LOSS"},
            {"result": "PUSH"},
        ]
        summary = _aggregate(rows)
        self.assertEqual(summary["wins"], 2)
        self.assertEqual(summary["losses"], 1)
        self.assertEqual(summary["pushes"], 1)
        self.assertEqual(summary["hit_rate"], 66.7)
        self.assertEqual(summary["flat_units"], 0.82)


if __name__ == "__main__":
    unittest.main()
