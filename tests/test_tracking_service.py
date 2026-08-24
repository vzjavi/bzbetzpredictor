import unittest
from unittest.mock import Mock, patch

from tracking_service import (
    _aggregate,
    _fetch_completed_games,
    _grade_pick,
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

    @patch("tracking_service.requests.get")
    def test_completed_scoreboard_parsing(self, mock_get):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "events": [
                {
                    "status": {"type": {"completed": True, "state": "post"}},
                    "competitions": [
                        {
                            "competitors": [
                                {
                                    "homeAway": "away",
                                    "score": "4",
                                    "team": {"displayName": "Texas Rangers"},
                                },
                                {
                                    "homeAway": "home",
                                    "score": "6",
                                    "team": {"displayName": "Houston Astros"},
                                },
                            ]
                        }
                    ],
                }
            ]
        }
        mock_get.return_value = response

        games = _fetch_completed_games("MLB", "2026-08-24")
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["away_score"], 4.0)
        self.assertEqual(games[0]["home_score"], 6.0)
        self.assertEqual(games[0]["actual_total"], 10.0)

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
