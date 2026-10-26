"""Checks for reliable local vision output and image-path boundaries."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from marketplace_vision import (apply_category_taxonomy, apply_listing_scope,
                                describe_image, image_for_listing,
                                normalize_observations, review_flags)


class MarketplaceVisionTests(unittest.TestCase):
    def test_normalizes_unreadable_model_without_confirming_it(self):
        response = {
            "primary_object": "television", "other_objects": [],
            "visible_brand": "unknown", "visible_model": "null",
            "visible_text": [], "material_clues": [],
            "wear_or_damage": [], "uncertainties": ["small thumbnail"],
            "category": "electronics", "item_type": "television",
            "listing_kind": "product", "identity_basis": "none",
            "category_confidence": "high", "candidate_identity": None,
            "identity_confidence": "low", "classification_evidence": ["Photo: screen"],
        }
        result = normalize_observations(response)
        self.assertIsNone(result["visible_brand"])
        self.assertIsNone(result["visible_model"])
        self.assertEqual(result["primary_object"], "television")
        response["visible_model"] = "Accord"
        self.assertIsNone(normalize_observations(response)["visible_model"])
        response["wear_or_damage"] = "scratched"
        with self.assertRaises(ValueError):
            normalize_observations(response)

    def test_sends_title_description_and_photo_together(self):
        observations = {
            "primary_object": "desk", "other_objects": [],
            "visible_brand": None, "visible_model": None,
            "visible_text": [], "material_clues": ["wood-like surface"],
            "wear_or_damage": [], "uncertainties": ["Small photo"],
            "listing_kind": "product", "category": "furniture", "item_type": "desk",
            "category_confidence": "high",
            "candidate_identity": "Jasper Office Furniture desk",
            "identity_basis": "seller_named_maker_or_model",
            "identity_confidence": "medium",
            "classification_evidence": ["Photo: desk", "Seller description: Jasper"],
        }
        photo_response = Mock()
        photo_response.json.return_value = {"message": {"content": json.dumps({
            key: observations[key] for key in (
                "primary_object", "other_objects", "visible_brand", "visible_model",
                "visible_text", "material_clues", "wear_or_damage", "uncertainties")
        })}}
        classification_response = Mock()
        classification_response.json.return_value = {"message": {"content": json.dumps({
            key: observations[key] for key in (
                "listing_kind", "category", "item_type", "category_confidence",
                "candidate_identity", "identity_basis", "identity_confidence",
                "classification_evidence")
        })}}
        session = Mock()
        session.post.side_effect = [photo_response, classification_response]
        actual = describe_image(session, b"photo", "Free",
                                "Jasper Office Furniture desk", "gemma3:12b", 30)
        photo_payload = session.post.call_args_list[0].kwargs["json"]
        payload = session.post.call_args_list[1].kwargs["json"]
        self.assertNotIn("Jasper", photo_payload["messages"][0]["content"])
        self.assertIn('"title": "Free"', payload["messages"][0]["content"])
        self.assertIn('"description": "Jasper Office Furniture desk"',
                      payload["messages"][0]["content"])
        self.assertEqual(payload["messages"][0]["images"], ["cGhvdG8="])
        self.assertEqual(photo_payload["messages"][0]["images"], ["cGhvdG8="])
        self.assertEqual(actual["item_type"], "desk")

    def test_image_must_be_inside_input_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            result = base / "results.json"
            image = base / "images" / "item.jpg"
            image.parent.mkdir()
            image.write_bytes(b"test")
            self.assertEqual(image_for_listing(result, {"image_file": "images/item.jpg"}),
                             image.resolve())
            self.assertIsNone(image_for_listing(result, {"image_file": "../private.jpg"}))

    def test_flags_piano_and_bench_disagreement(self):
        flags = review_flags(
            {"title": "Piano", "description": "Very nice condition"},
            {"listing_kind": "product", "other_objects": ["piano"],
             "item_type": "piano bench", "category": "furniture"})
        self.assertIn("seller_title_names_other_visible_object", flags)

    def test_flags_zero_price_card_with_seller_price(self):
        flags = review_flags(
            {"title": "Runners (NOT FREE THROW A OFFER)",
             "description": "", "asking_price": 0},
            {"listing_kind": "product", "other_objects": [],
             "item_type": "shoes", "category": "footwear"})
        self.assertIn("zero_price_card_may_not_be_free", flags)

    def test_previous_price_is_not_a_free_price_conflict(self):
        flags = review_flags(
            {"title": "Brio Water Dispenser", "original_card_title": "$30",
             "previous_price": 30, "description": "FREE Brio water dispenser. FREE.",
             "asking_price": 0},
            {"listing_kind": "product", "other_objects": [],
             "item_type": "water cooler", "category": "home_appliance"})
        self.assertNotIn("zero_price_card_may_not_be_free", flags)

    def test_taxonomy_fixes_vacuum_category_without_claiming_high_confidence(self):
        observations = {"item_type": "vacuum cleaner", "category": "furniture",
                        "category_confidence": "high", "classification_evidence": []}
        self.assertEqual(apply_category_taxonomy(observations), "furniture")
        self.assertEqual(observations["category"], "home_appliance")
        self.assertEqual(observations["category_confidence"], "medium")

    def test_taxonomy_places_water_cooler_with_appliances(self):
        observations = {"item_type": "water cooler", "category": "furniture",
                        "category_confidence": "high", "classification_evidence": []}
        self.assertEqual(apply_category_taxonomy(observations), "furniture")
        self.assertEqual(observations["category"], "home_appliance")

    def test_flags_cabinet_title_when_model_suggests_curtain(self):
        flags = review_flags(
            {"title": "2 cabinet free to go", "description": "Moving house"},
            {"listing_kind": "product", "other_objects": ["curtain"],
             "item_type": "curtain", "category": "furniture"})
        self.assertIn("seller_title_model_type_disagree", flags)

    def test_mower_listing_overrides_background_object_error(self):
        observations = {
            "primary_object": "skid steer loader", "listing_kind": "product",
            "category": "musical_instrument", "item_type": "piano",
            "category_confidence": "low", "candidate_identity": None,
            "identity_basis": "none", "identity_confidence": "low",
            "classification_evidence": [],
        }
        flags = apply_listing_scope({
            "title": "FREE LAWN MOWER BURNT",
            "description": "FREE riding lawn mower. Good for scrap only",
        }, observations)
        self.assertEqual(observations["item_type"], "riding lawn mower")
        self.assertEqual(observations["category"], "garden_outdoor_equipment")
        self.assertIn("photo_primary_object_may_be_background", flags)

    def test_storewide_promotion_is_not_one_shelf(self):
        observations = {
            "primary_object": "shelf", "listing_kind": "product",
            "category": "furniture", "item_type": "shelf",
            "category_confidence": "high", "candidate_identity": None,
            "identity_basis": "none", "identity_confidence": "low",
            "classification_evidence": [],
        }
        flags = apply_listing_scope({
            "title": "20% DISCOUNT at The Weekend Store",
            "description": "Our booth has discounts on all of our items",
        }, observations)
        self.assertEqual(observations["listing_kind"], "promotion")
        self.assertIsNone(observations["item_type"])
        self.assertIn("store_promotion_not_single_item", flags)

    def test_pickup_service_is_not_scrap_item(self):
        observations = {
            "primary_object": "truck", "listing_kind": "product",
            "category": "automotive_part", "item_type": "scrap metal",
            "category_confidence": "high", "candidate_identity": None,
            "identity_basis": "none", "identity_confidence": "low",
            "classification_evidence": [],
        }
        flags = apply_listing_scope({
            "title": "Free pick up of unwanted scrap metal and appliances",
            "description": "We collect scrap metal for free",
        }, observations)
        self.assertEqual(observations["listing_kind"], "service")
        self.assertIsNone(observations["item_type"])
        self.assertIn("service_ad_not_item", flags)


if __name__ == "__main__":
    unittest.main()
