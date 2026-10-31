"""Broad asking-price estimates keep distinct appliances and equipment apart."""

import unittest

from marketplace_value import (estimate_value, general_reference_type,
                               infer_condition, scooter_form)


class GeneralValueTests(unittest.TestCase):
    def test_pet_rehoming_post_has_no_product_price_estimate(self):
        target = {"title": "Dog to Good Home", "vision": {
            "review_flags": ["pet_listing"],
            "observations": {"listing_kind": "product", "item_type": "dog"}}}
        self.assertEqual(estimate_value(target, [])['reason'], "pet_listing")

    def test_known_cleat_type_without_price_group_is_not_unidentified(self):
        target = {"title": "Nike baseball cleats", "identity": {"id": None},
                  "vision": {"observations": {"listing_kind": "product",
                                              "item_type": "baseball_cleats",
                                              "category_confidence": "high"}}}
        self.assertEqual(estimate_value(target, [])["reason"],
                         "price_group_unavailable")

    def test_mower_that_cannot_start_is_not_unknown_condition(self):
        self.assertEqual(infer_condition("I can’t get it to start")["grade"], "parts")
        self.assertEqual(infer_condition("Needs an engine repair; other parts good")["grade"],
                         "parts")
        self.assertEqual(infer_condition("Electric scooter does not turn on")["grade"],
                         "parts")
        self.assertEqual(infer_condition("Scooter is stuck in fix mode")["grade"],
                         "parts")
        self.assertEqual(infer_condition("Recliner stopped working")["grade"],
                         "parts")
        self.assertEqual(infer_condition("It currently does not recline")["grade"],
                         "parts")
        self.assertNotEqual(infer_condition("Used, has a brand new carburetor")["grade"],
                            "new")
        self.assertNotEqual(infer_condition("Used Honda mower. Brand new rear wheels.")["grade"],
                            "new")

    def test_product_form_is_specific(self):
        self.assertEqual(general_reference_type("Riding lawn mower"), "riding_mower")
        self.assertEqual(general_reference_type("Electric scooter"), "electric_scooter")
        self.assertIsNone(general_reference_type("Washer and dryer set"))

    def test_median_uses_only_same_form_nearby_asking_prices(self):
        target = {"listing_id": "target", "title": "Riding lawn mower",
                  "search_zip": "21009", "condition": {"grade": "unknown"},
                  "identity": {"id": None}}
        comps = [{"source": "facebook_marketplace", "listing_id": str(index),
                  "title": "Riding mower", "reference_type": "riding_mower",
                  "search_zip": "21009", "reference_distance_miles": 10,
                  "asking_price": price, "condition": "good"}
                 for index, price in enumerate((200, 250, 300), 1)]
        comps.append({"source": "facebook_marketplace", "listing_id": "push",
                      "title": "Push mower", "reference_type": "push_mower",
                      "search_zip": "21009", "reference_distance_miles": 5,
                      "asking_price": 25, "condition": "good"})
        result = estimate_value(target, comps)
        self.assertEqual(result["status"], "provisional")
        self.assertEqual(result["midpoint"], 250)
        self.assertEqual(result["comparable_count"], 3)
        self.assertEqual(result["asking_price_sample"], [200.0, 250.0, 300.0])
        self.assertEqual([row["asking_price"] for row in result["comparable_evidence"]],
                         [200.0, 250.0, 300.0])
        self.assertNotIn("Push mower", [row["title"] for row in
                                         result["comparable_evidence"]])

    def test_too_few_same_form_comps_does_not_estimate(self):
        target = {"title": "Electric scooter", "search_zip": "21009",
                  "identity": {"id": None}}
        comps = [{"source": "facebook_marketplace", "listing_id": "1",
                  "title": "Electric scooter", "reference_type": "electric_scooter",
                  "search_zip": "21009", "asking_price": 75}]
        self.assertEqual(estimate_value(target, comps)["status"], "unvalued")

    def test_seated_scooter_does_not_borrow_standing_scooter_prices(self):
        self.assertEqual(scooter_form("Razor E300S Seated Electric Scooter"), "seated")
        target = {"listing_id": "target", "title": "Razor E300S Seated Electric Scooter",
                  "search_zip": "21009", "condition": {"grade": "unknown"},
                  "identity": {"id": None}}
        comps = [{"source": "facebook_marketplace", "listing_id": str(index),
                  "title": "Gotrax Electric Scooter", "reference_type": "electric_scooter",
                  "search_zip": "21009", "asking_price": price,
                  "condition": "good"}
                 for index, price in enumerate((60, 100, 125), 1)]
        result = estimate_value(target, comps)
        self.assertEqual(result["status"], "unvalued")
        self.assertEqual(result["reason"], "insufficient_same_variant_comparables")
        self.assertEqual(result["comparable_count"], 0)
        self.assertEqual(result["price_cohort"]["key"], "seated_electric_scooter")

        comps += [{"source": "facebook_marketplace", "listing_id": str(index),
                   "title": "Seated electric scooter", "reference_type": "electric_scooter",
                   "search_zip": "21009", "asking_price": price,
                   "condition": "good"}
                  for index, price in enumerate((80, 90, 100), 4)]
        result = estimate_value(target, comps)
        self.assertEqual(result["status"], "unvalued")
        self.assertEqual(result["comparable_count"], 0)
        comps += [{"source": "facebook_marketplace", "listing_id": str(index),
                   "title": "Razor E300S Seated Electric Scooter",
                   "reference_type": "electric_scooter", "search_zip": "21009",
                   "asking_price": price, "condition": "good"}
                  for index, price in enumerate((80, 90, 100), 7)]
        result = estimate_value(target, comps)
        self.assertEqual(result["midpoint"], 90)
        self.assertEqual([row["asking_price"] for row in result["comparable_evidence"]],
                         [80, 90, 100])

    def test_photo_identified_office_chair_does_not_use_generic_chairs(self):
        target = {"listing_id": "target", "title": "Free chair",
                  "search_zip": "21009", "identity": {"id": None},
                  "vision": {"observations": {"listing_kind": "product",
                                              "category": "furniture",
                                              "category_confidence": "high",
                                              "item_type": "office chair"}}}
        comps = [{"source": "facebook_marketplace", "listing_id": str(index),
                  "title": title, "search_zip": "21009", "asking_price": price,
                  "condition": "good"}
                 for index, (title, price) in enumerate((
                     ("Pink chair", 15), ("Oversized chair", 30),
                     ("Purple chair", 50)), 1)]
        result = estimate_value(target, comps)
        self.assertEqual(result["status"], "unvalued")
        self.assertEqual(result["comparable_count"], 0)

        comps += [{"source": "facebook_marketplace", "listing_id": str(index),
                   "title": "Office chair", "search_zip": "21009",
                   "asking_price": price, "condition": "good"}
                  for index, price in enumerate((40, 60, 80), 4)]
        result = estimate_value(target, comps)
        self.assertEqual(result["product_type"], "office_chair")
        self.assertEqual(result["midpoint"], 60)
        self.assertEqual([row["asking_price"] for row in result["comparable_evidence"]],
                         [40, 60, 80])

    def test_mower_without_power_source_cannot_show_a_median(self):
        target = {"listing_id": "target", "title": "Push mower", "search_zip": "21009",
                  "condition": {"grade": "needs_repair"}, "identity": {"id": None}}
        comps = [{"source": "facebook_marketplace", "listing_id": str(index),
                  "title": "Push mower", "reference_type": "push_mower",
                  "search_zip": "21009", "asking_price": price,
                  "condition": grade}
                 for index, (price, grade) in enumerate(
                     ((40, "fair"), (100, "good"), (115, "new")), 1)]
        result = estimate_value(target, comps)
        self.assertEqual(result["status"], "unvalued")
        self.assertEqual(result["reason"], "mower_power_source_unclear")
        self.assertNotIn("asking_context", result)

    def test_generic_walk_behind_mower_is_not_compared_to_mixed_forms(self):
        target = {"listing_id": "target", "title": "Lawn mower", "search_zip": "21009",
                  "condition": {"grade": "needs_repair"}, "identity": {"id": None}}
        comps = [{"source": "facebook_marketplace", "listing_id": str(index),
                  "title": title, "reference_type": kind,
                  "search_zip": "21009", "asking_price": price,
                  "condition": "good"}
                 for index, (title, kind, price) in enumerate((
                     ("Lawn mower", "lawn_mower", 100),
                     ("Push mower", "push_mower", 40),
                     ("Self propelled mower", "self_propelled_mower", 320)), 1)]
        result = estimate_value(target, comps)
        self.assertEqual(result["status"], "unvalued")
        self.assertEqual(result["reason"], "mower_power_source_unclear")
        self.assertNotIn("asking_context", result)

    def test_unknown_scooter_model_does_not_borrow_cheap_form_median(self):
        target = {"listing_id": "target", "title": "Electric scooter",
                  "search_zip": "21009", "condition": {"grade": "unknown"},
                  "identity": {"id": "brand-model", "confidence": 0.9,
                               "category": "personal_transport"}}
        comps = [{"source": "facebook_marketplace", "listing_id": str(index),
                  "title": "Electric scooter", "reference_type": "electric_scooter",
                  "search_zip": "21009", "asking_price": price,
                  "condition": "good"}
                 for index, price in enumerate((80, 100, 200), 1)]
        result = estimate_value(target, comps)
        self.assertEqual(result["status"], "unvalued")
        self.assertEqual(result["reason"], "scooter_variant_details_missing")


    def test_handmade_cherry_cabinet_does_not_use_generic_cabinets(self):
        target = {"listing_id": "target", "title": "Cherry Cabinet handmade by a cabinetmaker",
                  "search_zip": "21009", "identity": {"id": None},
                  "condition": {"grade": "good"}}
        generic = [{"source": "facebook_marketplace", "listing_id": str(index),
                    "title": title, "asking_price": price,
                    "condition": "good", "search_zip": "21009"}
                   for index, (title, price) in enumerate((
                       ("Small cabinet with child lock", 30),
                       ("White storage cabinet", 50),
                       ("Vintage solid wood display cabinet", 200)), 1)]
        result = estimate_value(target, generic)
        self.assertEqual(result["status"], "unvalued")
        self.assertEqual(result["comparable_count"], 0)
        self.assertEqual(result["feature_clues_required"],
                         ["handcrafted", "named_cherry"])

        matching = [{**row, "title": "Handmade cherry cabinet"}
                    for row in generic]
        result = estimate_value(target, matching)
        self.assertEqual(result["status"], "provisional")
        self.assertEqual(result["comparable_count"], 3)

    def test_chaise_sofa_does_not_use_plain_sofa_prices(self):
        target = {"listing_id": "target", "title": "Couch with chaise",
                  "search_zip": "21009", "identity": {"id": None},
                  "condition": {"grade": "good"}}
        generic = [{"source": "facebook_marketplace", "listing_id": str(index),
                    "title": "Couch", "asking_price": price,
                    "condition": "good", "search_zip": "21009"}
                   for index, price in enumerate((80, 100, 120), 1)]
        result = estimate_value(target, generic)
        self.assertEqual(result["status"], "unvalued")
        self.assertEqual(result["comparable_count"], 0)
        self.assertEqual(result["feature_clues_required"], ["chaise"])


if __name__ == "__main__":
    unittest.main()
