"""Broad reference scans screen item form, bundles, and asking-price conflicts."""

import unittest

from marketplace_reference_candidates import approved_pricing_comparables, screen_listing
from marketplace_price import seller_asking_price


def listing(title, price, description=""):
    return {"listing_id": "123", "title": title, "asking_price": price,
            "description": description, "description_status": "visible",
            "radius_check": {"status": "included"}, "location": "Bel Air, MD"}


class ReferenceCandidateTests(unittest.TestCase):
    def test_single_furniture_price_is_eligible_but_sets_and_repairs_are_not(self):
        recliner = screen_listing(listing("Recliner", 75,
                                          "Fabric recliner, fair condition."), "now")
        self.assertEqual(recliner["reference_type"], "recliner_chair")
        self.assertEqual(recliner["review_status"], "candidate")
        set_listing = screen_listing(listing("Dresser and nightstand", 120,
                                             "Both included."), "now")
        self.assertIn("bundle_or_multiple_items", set_listing["review_reasons"])
        broken = screen_listing(listing("Recliner", 30,
                                        "It currently does not recline."), "now")
        self.assertIn("repair_or_parts_condition", broken["review_reasons"])

    def test_only_clear_candidates_enter_provisional_price_pool(self):
        clear = screen_listing(listing("Electric scooter", 100, "Works well."), "now")
        flagged = screen_listing(listing("Electric scooter", 0,
                                         "Asking $180."), "now")
        pool = approved_pricing_comparables({
            "requested_zip": "21009", "observed_at": "2026-10-26T12:00:00Z",
            "radius_miles": 60, "listings": [clear, flagged]})
        self.assertEqual(len(pool), 1)
        self.assertEqual(pool[0]["asking_price"], 100)
        self.assertEqual(pool[0]["reference_type"], "electric_scooter")
        self.assertEqual(pool[0]["price_basis"], "seller_asking_price_not_completed_sale")
        self.assertEqual(approved_pricing_comparables({
            "requested_zip": "21009", "observed_at": "2026-10-26T12:00:00Z",
            "radius_miles": 60, "listings": [clear]}, {"123"}), [])

    def test_or_best_offer_is_explicit_seller_ask(self):
        self.assertEqual(seller_asking_price("$400 or best offer"), 400)
        row = screen_listing(listing("Samsung electric dryer", 250,
                                     "Dryer for sale, $400 or best offer."), "now")
        self.assertIn("card_and_description_price_conflict", row["review_reasons"])

    def test_specific_item_and_clear_price_can_be_reviewed(self):
        row = screen_listing(listing("Riding+Mower", 250, "Runs well"), "2026-10-26T12:00:00Z")
        self.assertEqual(row["reference_type"], "riding_mower")
        self.assertEqual(row["review_status"], "candidate")
        self.assertEqual(row["effective_ask"], 250)

    def test_bundle_and_card_price_conflict_need_review(self):
        bundle = screen_listing(listing("Dining table with 6 chairs", 100), "now")
        self.assertIn("bundle_or_multiple_items", bundle["review_reasons"])
        conflict = screen_listing(listing("Electric scooter", 100, "Asking $180."), "now")
        self.assertIn("card_and_description_price_conflict", conflict["review_reasons"])

    def test_free_card_with_seller_ask_cannot_auto_enter_pool(self):
        row = screen_listing(listing("Electric scooter", 0, "Asking $180."), "now")
        self.assertEqual(row["effective_ask"], 180)
        self.assertIn("zero_card_seller_asks", row["review_reasons"])

    def test_service_or_accessory_needs_review(self):
        service = screen_listing(listing("Lawn mower repair service", 40), "now")
        self.assertIn("service_or_promotion", service["review_reasons"])
        accessory = screen_listing(listing("Electric scooter charger only", 10), "now")
        self.assertIn("accessory_or_parts_listing", accessory["review_reasons"])
        trade_in = screen_listing(listing("Toro self propelled mower", 120,
                                          "Bring your non running mower for 20.00 off."),
                                  "now")
        self.assertIn("service_or_promotion", trade_in["review_reasons"])

    def test_bundle_store_and_placeholder_prices_stay_out_of_clear_pool(self):
        bundle = screen_listing(listing("Refrigerator", 250,
                                        "14 cu ft fridge $150 for 1, $250 for both"), "now")
        self.assertIn("bundle_or_multiple_items", bundle["review_reasons"])
        store = screen_listing(listing("Samsung dryer", 199,
                                       "Finance available and 4 months warranty"), "now")
        self.assertIn("dealer_or_warranty_listing", store["review_reasons"])
        placeholder = screen_listing(listing("Old stove", 1234,
                                             "Functions. Make offer."), "now")
        self.assertIn("likely_placeholder_price", placeholder["review_reasons"])
        plural = screen_listing(listing("Kitchen cabinets", 120,
                                        "Shelves and doors included."), "now")
        self.assertIn("multiple_items_in_title", plural["review_reasons"])
        table = screen_listing(listing("Dining table", 20,
                                       "The table comes with 2 chairs."), "now")
        self.assertIn("bundle_or_multiple_items", table["review_reasons"])
        mower = screen_listing(listing("Lawn mower w/accessories", 600,
                                       "Runs well."), "now")
        self.assertIn("bundle_or_multiple_items", mower["review_reasons"])
        broken = screen_listing(listing("Riding mower", 200,
                                        "Engine runs but it needs a new belt."), "now")
        self.assertIn("mechanical_issue_needs_review", broken["review_reasons"])
        incomplete = screen_listing(listing("Electric scooter", 60,
                                            "I lost the charger."), "now")
        self.assertIn("mechanical_issue_needs_review", incomplete["review_reasons"])
        broken_scooter = screen_listing(listing("Electric scooter", 90,
                                                "Does not turn on."), "now")
        self.assertIn("mechanical_issue_needs_review", broken_scooter["review_reasons"])
        belt = screen_listing(listing("Riding mower", 425,
                                    "It might need a new drive belt."), "now")
        self.assertIn("mechanical_issue_needs_review", belt["review_reasons"])
        heat = screen_listing(listing("LG dryer", 200,
                                    "Heating issue, will not heat."), "now")
        self.assertIn("mechanical_issue_needs_review", heat["review_reasons"])
        self.assertIn("repair_or_parts_condition", heat["review_reasons"])
        handle = screen_listing(listing("Honda self-propelled mower", 125,
                                      "The handle is broke."), "now")
        self.assertIn("mechanical_issue_needs_review", handle["review_reasons"])
        mismatch = screen_listing(listing("Murray self-propelled mower", 250,
                                        "Lawn Mower Type\nPush Mower"), "now")
        self.assertIn("seller_type_conflicts_with_title", mismatch["review_reasons"])
        cover = screen_listing(listing("Commercial lawn mower", 350,
                                     "Only thing I don't have is the belt and pulley cover."),
                               "now")
        self.assertIn("mechanical_issue_needs_review", cover["review_reasons"])

    def test_mower_mechanical_defects_need_review(self):
        descriptions = (
            "Front of deck starting to rust out.",
            "The carburetor needs cleaned after sitting for two years.",
            "Needs some TLC and new blades.",
            "Handle broke and needs carb.",
            "Runs but a little funny to start.",
            "Sometimes runs; may just need maintenance.",
            "Needs drive/gearbox repair; does not self-propel.",
            "I do not have batteries or a charger.",
            "Good mower that needs attention. Deck wheel rusted off.",
            "I belive it runs, haven’t ran it since last year.",
            "It ran when I parked it.",
            "The carb needs to be tuned and will need a new rear tire.",
            "Used+push+mower+deck+starting+to+rust+out.",
            "Runs but needs some work.",
            "Will not stay running; needs a carburetor.",
            "Runs but will stall out and not rev correctly.",
            "It needs some love; you need a jump pack to start it.",
            "The muffler fell off a while ago.",
            "Needs to be jump started and the deck needs an area welded.",
            "It starts and then shuts down.",
            "Battery is dead and the belt is loose.",
            "Mower deck spindle bearing seized.",
            "I don't know if it starts after two seasons.",
            "Needs a tune up and currently not starting.",
            "Needs new deck belt.",
            "The plastic is missing and the deck is rusted.",
            "Could not get it started this year.",
            "The upper handle likes to not stay in place; rig the handle.",
        )
        for description in descriptions:
            with self.subTest(description=description):
                row = screen_listing(listing("Self propelled mower", 100,
                                             description), "now")
                self.assertIn("mechanical_issue_needs_review",
                              row["review_reasons"])

    def test_seller_form_refines_generic_lawn_mower_title(self):
        row = screen_listing(listing("Toro Recycler Lawn Mower", 120,
                                     "Lawn Mower Type\nSelf Propelled Mower\nRuns well."),
                             "now")
        self.assertEqual(row["reference_type"], "self_propelled_mower")
        self.assertEqual(row["review_status"], "candidate")
        zero_turn = screen_listing(listing("TimeCutter SS 4225 Riding Mower", 1200,
                                           "Toro TimeCutter zero-turn riding mower. Runs well."),
                                   "now")
        self.assertEqual(zero_turn["reference_type"], "zero_turn_mower")

    def test_other_seller_amount_or_bagger_bundle_needs_review(self):
        amount = screen_listing(listing("Zero turn mower", 400,
                                        "New solenoid$500 or make an offer."), "now")
        self.assertIn("other_amount_in_seller_description", amount["review_reasons"])
        obo = screen_listing(listing("Riding mower", 150,
                                     "Will take 250 obo."), "now")
        self.assertIn("other_amount_in_seller_description", obo["review_reasons"])
        bagger = screen_listing(listing("Riding mower with bagger", 600,
                                        "Comes with 2 place bagger."), "now")
        self.assertIn("bundle_or_multiple_items", bagger["review_reasons"])

    def test_push_title_with_self_propelled_description_needs_review(self):
        row = screen_listing(listing("John Deere Push Mower", 50,
                                     "Runs but stalls out. Has self propelled 3 gears."),
                             "now")
        self.assertIn("seller_type_conflicts_with_title", row["review_reasons"])


if __name__ == "__main__":
    unittest.main()
