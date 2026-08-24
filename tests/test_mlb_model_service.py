import unittest
from unittest.mock import patch

from mlb_model_service import (
    MODEL_VERSION,
    _build_recent_form,
    _recent_projection,
    _starter_adjustment,
    enrich_mlb_predictions,
)


class MLBModelServiceTests(unittest.TestCase):
    def test_recent_projection_combines_recent_offense_and_defense(self):
        away = {"games": 10, "runs_for_pg": 5.0, "runs_against_pg": 4.0}
        home = {"games": 10, "runs_for_pg": 4.0, "runs_against_pg": 5.0}
        self.assertEqual(_recent_projection(away, home), 9.0)

    def test_recent_projection_requires_reasonable_sample(self):
        away = {"games": 4, "runs_for_pg": 6.0, "runs_against_pg": 3.0}
        home = {"games": 10, "runs_for_pg": 4.0, "runs_against_pg": 4.0}
        self.assertIsNone(_recent_projection(away, home))

    def test_recent_form_uses_only_final_games(self):
        games = [
            {
                "gameDate": "2026-08-22T00:00:00Z",
                "status": {"abstractGameState": "Final"},
                "teams": {
                    "away": {"team": {"name": "Texas Rangers"}, "score": 6},
                    "home": {"team": {"name": "Houston Astros"}, "score": 4},
                },
            },
            {
                "gameDate": "2026-08-23T00:00:00Z",
                "status": {"abstractGameState": "Live"},
                "teams": {
                    "away": {"team": {"name": "Texas Rangers"}, "score": 99},
                    "home": {"team": {"name": "Houston Astros"}, "score": 99},
                },
            },
        ]
        form = _build_recent_form(games)
        self.assertEqual(form["texas rangers"]["games"], 1)
        self.assertEqual(form["texas rangers"]["runs_for_pg"], 6.0)
        self.assertEqual(form["houston astros"]["runs_against_pg"], 6.0)

    def test_starter_adjustment_rewards_low_era_and_penalizes_high_era(self):
        low = _starter_adjustment({"era": 2.20}, {"era": 3.20})
        high = _starter_adjustment({"era": 5.20}, {"era": 6.20})
        self.assertLess(low, 0)
        self.assertGreater(high, 0)

    @patch("mlb_model_service._starter_with_stats")
    @patch("mlb_model_service._build_recent_form")
    @patch("mlb_model_service._recent_games_payload")
    @patch("mlb_model_service._schedule_games_for_date")
    def test_enrichment_blends_baseline_recent_form_and_starters(
        self, mock_schedule, mock_recent_payload, mock_build_form, mock_starter
    ):
        mock_schedule.return_value = [
            {
                "teams": {
                    "away": {
                        "team": {"name": "Texas Rangers"},
                        "probablePitcher": {"id": 1, "fullName": "Away Starter"},
                    },
                    "home": {
                        "team": {"name": "Houston Astros"},
                        "probablePitcher": {"id": 2, "fullName": "Home Starter"},
                    },
                }
            }
        ]
        mock_recent_payload.return_value = []
        mock_build_form.return_value = {
            "texas rangers": {"games": 10, "runs_for_pg": 5.0, "runs_against_pg": 4.0},
            "houston astros": {"games": 10, "runs_for_pg": 4.0, "runs_against_pg": 5.0},
        }
        mock_starter.side_effect = [
            {"id": 1, "name": "Away Starter", "era": 5.20, "whip": 1.40},
            {"id": 2, "name": "Home Starter", "era": 4.20, "whip": 1.25},
        ]

        predictions = [
            {
                "sport": "MLB",
                "team1": "Rangers",
                "team2": "Astros",
                "away_team_raw": "Texas Rangers",
                "home_team_raw": "Houston Astros",
                "predicted_total": 8.0,
            }
        ]
        enriched = enrich_mlb_predictions(predictions)[0]

        self.assertEqual(enriched["model_version"], MODEL_VERSION)
        self.assertEqual(enriched["baseline_total"], 8.0)
        self.assertEqual(enriched["recent_total"], 9.0)
        self.assertEqual(enriched["starter_adjustment"], 0.24)
        self.assertEqual(enriched["predicted_total"], 8.5)
        self.assertEqual(enriched["away_starter"]["name"], "Away Starter")
        self.assertTrue(enriched["model_notes"])

    @patch("mlb_model_service._schedule_games_for_date", return_value=[])
    @patch("mlb_model_service._recent_games_payload", return_value=[])
    def test_missing_statsapi_inputs_fall_back_to_baseline(self, _recent, _schedule):
        prediction = {
            "sport": "MLB",
            "team1": "Rangers",
            "team2": "Astros",
            "away_team_raw": "Texas Rangers",
            "home_team_raw": "Houston Astros",
            "predicted_total": 8.4,
        }
        result = enrich_mlb_predictions([prediction])[0]
        self.assertEqual(result["predicted_total"], 8.4)
        self.assertEqual(result["model_version"], MODEL_VERSION)


if __name__ == "__main__":
    unittest.main()
