"""Unchanged listings reuse their saved 8B analysis across scans."""

import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from marketplace_condition import PROMPT_VERSION as CONDITION_PROMPT, run as run_condition
from marketplace_db import get_cached_analysis, store_result
from marketplace_vision import PROMPT_VERSION as VISION_PROMPT, run as run_vision


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/V7sAAAAASUVORK5CYII="
)
MODEL = "qwen3-vl:8b-instruct"


def input_hash(photo: bytes, fields: dict) -> str:
    return hashlib.sha256(
        photo + json.dumps(fields, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


class MarketplaceAnalysisCacheTests(unittest.TestCase):
    def test_out_of_range_listing_skips_model_and_preserves_saved_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "marketplace.sqlite"
            image_dir = root / "images"
            image_dir.mkdir()
            (image_dir / "1.png").write_bytes(PNG)
            listing = {"listing_id": "1", "title": "Electric scooter",
                       "description": "Runs well", "asking_price": 60,
                       "image_file": "images/1.png",
                       "vision": {"status": "observed", "model": MODEL},
                       "condition_assessment": {"status": "estimated",
                                                "model": MODEL}}
            store_result({"listings": [listing]}, root, db)
            input_file = root / "input.json"
            input_file.write_text(json.dumps({"listings": [{
                key: value for key, value in listing.items()
                if key not in {"vision", "condition_assessment"}
            }]}), encoding="utf-8")
            with patch("marketplace_vision.describe_image",
                       side_effect=AssertionError("model should not run")):
                vision = run_vision(input_file, root / "vision.json", MODEL, 1,
                                    10, db_path=db, min_price=0, max_price=30)
            self.assertEqual(vision["listings"][0]["vision"]["status"],
                             "skipped_price_range")
            with patch("marketplace_condition.classify_condition",
                       side_effect=AssertionError("model should not run")):
                condition = run_condition(root / "vision.json",
                                          root / "condition.json", MODEL, 1, 10,
                                          db_path=db)
            self.assertEqual(condition["listings"][0]["condition_assessment"]["status"],
                             "skipped_price_range")
            cached = get_cached_analysis(db, "facebook_marketplace", "1")
            self.assertEqual(cached["vision"]["status"], "observed")
            self.assertEqual(cached["condition_assessment"]["status"], "estimated")

    def test_vision_reuses_saved_analysis_for_unchanged_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "marketplace.sqlite"
            image_dir = root / "images"
            image_dir.mkdir()
            (image_dir / "1.png").write_bytes(PNG)
            listing = {
                "listing_id": "1", "title": "Free desk", "description": "Wood desk",
                "image_file": "images/1.png", "asking_price": 0,
            }
            observations = {
                "primary_object": "desk", "other_objects": [],
                "visible_brand": None, "visible_model": None,
                "visible_text": [], "material_clues": ["wood-like surface"],
                "wear_or_damage": [], "uncertainties": [],
                "listing_kind": "product", "category": "furniture",
                "item_type": "desk", "category_confidence": "high",
                "candidate_identity": None, "identity_basis": "none",
                "identity_confidence": "low", "classification_evidence": ["Photo: desk"],
            }
            listing["vision"] = {
                "status": "observed", "model": MODEL,
                "prompt_version": VISION_PROMPT,
                "input_hash": input_hash(PNG, {
                    "title": listing["title"], "description": listing["description"]}),
                "observations": observations,
            }
            store_result({"listings": [listing]}, root, db)
            self.assertEqual(
                get_cached_analysis(db, "facebook_marketplace", "1")["vision"]["status"],
                "observed")
            input_file = root / "input.json"
            input_file.write_text(json.dumps({"listings": [{
                key: value for key, value in listing.items() if key != "vision"
            }]}), encoding="utf-8")
            with patch("marketplace_vision.describe_image",
                       side_effect=AssertionError("model should not run")):
                result = run_vision(input_file, root / "output.json", MODEL, 1,
                                    10, db_path=db)
            self.assertEqual(result["vision_processed"], 1)
            self.assertEqual(result["listings"][0]["vision"]["observations"]["item_type"],
                             "desk")
            changed = {key: value for key, value in listing.items() if key != "vision"}
            changed["description"] = "Different seller description"
            input_file.write_text(json.dumps({"listings": [changed]}), encoding="utf-8")
            with patch("marketplace_vision.describe_image", return_value=observations) as model:
                run_vision(input_file, root / "changed.json", MODEL, 1, 10,
                           db_path=db)
            model.assert_called_once()

    def test_condition_reuses_saved_assessment_for_unchanged_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "marketplace.sqlite"
            image_dir = root / "images"
            image_dir.mkdir()
            (image_dir / "1.png").write_bytes(PNG)
            listing = {
                "listing_id": "1", "title": "Desk", "description": "Used desk",
                "image_file": "images/1.png", "asking_price": 0,
                "vision": {"observations": {
                    "listing_kind": "product", "wear_or_damage": [],
                    "uncertainties": []}, "review_flags": []},
            }
            listing["condition_assessment"] = {
                "status": "estimated", "model": MODEL,
                "prompt_version": CONDITION_PROMPT,
                "input_hash": input_hash(PNG, {
                    "title": "Desk", "description": "Used desk",
                    "photo_wear": [], "photo_uncertainties": []}),
                "result": {"usage_state": "used", "condition_grade": "unknown",
                           "functional_status": "unknown", "confidence": "low",
                           "evidence": [], "uncertainties": []},
            }
            store_result({"listings": [listing]}, root, db)
            input_file = root / "input.json"
            input_file.write_text(json.dumps({"listings": [{
                key: value for key, value in listing.items()
                if key != "condition_assessment"
            }]}), encoding="utf-8")
            with patch("marketplace_condition.classify_condition",
                       side_effect=AssertionError("model should not run")):
                result = run_condition(input_file, root / "output.json", MODEL,
                                       1, 10, db_path=db)
            self.assertEqual(result["condition_processed"], 1)
            self.assertEqual(result["listings"][0]["condition_assessment"]["status"],
                             "estimated")


if __name__ == "__main__":
    unittest.main()
