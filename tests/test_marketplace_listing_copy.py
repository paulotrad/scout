"""Card titles and generated copy stay grounded in saved listing evidence."""

import json
from pathlib import Path
import tempfile
import unittest
from marketplace_listing_copy import display_title, mixed_item_lot, run


class ListingCopyTests(unittest.TestCase):
    def test_zero_price_titles_show_the_item(self):
        self.assertEqual(display_title("free couch!", 0, "couch"), "Couch")
        self.assertEqual(display_title("Free chair", 0, "office chair"), "Chair")
        self.assertEqual(display_title("FREE", 0, "moving box"), "Moving box")
        self.assertEqual(display_title("Free couch!", 25, "couch"), "Couch")
        self.assertEqual(display_title("✨ FREE BEAUTIFUL VANITY WITH MIRROR! ✨", 0,
                                       "console_table"), "Vanity with mirror")
        self.assertEqual(display_title("⭐ FREE TV STAND ⭐", 0, "tv_stand"),
                         "TV stand")
        self.assertEqual(display_title("Free People dress", 0, "dress"),
                         "Free People dress")
        self.assertEqual(display_title("✨ RYOBI ONE+ 18V MOWER ✨", 75,
                                       "lawn_mower"), "Ryobi ONE+ 18V mower")

    def test_saved_card_titles_drop_price_and_pickup_promotions(self):
        self.assertEqual(display_title("Freeee", 0, "mop"), "Mop")
        self.assertEqual(display_title("FREE", 0), "Unidentified item")
        self.assertEqual(display_title("2 cabinet free to go", 0,
                                       "display cabinet"), "2 cabinets")
        self.assertEqual(display_title("Used couch for free", 0, "couch"),
                         "Used couch")
        self.assertEqual(display_title("Pick up free Couch and Recliner", 0,
                                       "sofa"), "Couch and Recliner")
        self.assertEqual(display_title("Free @ the curb 170 Springvale Road", 0,
                                       "cardboard box"), "Curbside items")
        self.assertEqual(display_title("Misc for FREE list in description", 0,
                                       "ceramic dishes"), "Assorted items")
        self.assertEqual(display_title("$5", 5, "subwoofer"), "Subwoofer")
        self.assertEqual(display_title("$100 It works perfectly.", 100,
                                       "snow plow blade"), "Snow plow blade")
        self.assertEqual(display_title("Brio water dispenser $100", 100,
                                       "water dispenser"), "Brio water dispenser")
        self.assertEqual(display_title(
            "Free dishes/ kitchen items porch pick up only~ havre de grace must take all",
            0, "dishes"), "Dishes/ kitchen items")
        self.assertEqual(display_title("FREE � Lamp", 0, "lamp"), "Lamp")
        self.assertEqual(display_title("“FREE” full/queen bed. Mattress included", 0,
                                       "bed"), "Full/queen bed. Mattress included")
        self.assertEqual(display_title("Free-standing bookshelf", 0,
                                       "bookshelf"), "Free-standing bookshelf")
        self.assertEqual(display_title("Free weights", 0, "weights"),
                         "Free weights")

    def test_mixed_lot_title_names_offered_items_without_address(self):
        title = "Free stuff 3100 block Grindon Ave"
        description = ("Free stuff on 3100 block Grindon Ave. Fan needs repair, "
                       "shoe rack, scooter, dog bed, and harness.")
        self.assertTrue(mixed_item_lot(title, description))
        self.assertEqual(display_title(title, 0, "box", description),
                         "Fan, shoe rack, scooter and more")
        self.assertFalse(mixed_item_lot("Free couch", "A couch and pillows."))
        self.assertEqual(display_title("FREE Vintage NordicTrack Achiever Ski "
                                       "Machine � Parts/Repair", 0),
                         "NordicTrack Achiever ski machine (parts/repair)")

    def test_long_seller_pitch_becomes_short_accurate_oil_title(self):
        seller_title = ("Winter is coming! 32 gal. Of waste oil from automotive "
                        "oil changes Free Free Free all in gal. Jugs")
        self.assertEqual(display_title(seller_title, 0, "waste oil",
                                       "Free come get it ."),
                         "Used automotive oil in jugs")
        self.assertEqual(display_title("Used automotive oil in a barrel", 0,
                                       "waste oil"), "Used automotive oil")
        self.assertEqual(display_title("Olive oil in bottles", 5, "olive oil"),
                         "Olive oil in bottles")

    def test_mixed_lot_description_does_not_assign_one_items_damage_to_all(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.json"
            output = Path(directory) / "output.json"
            source.write_text(json.dumps({"listings": [{
                "listing_id": "lot", "title": "Free stuff 3100 block Grindon Ave",
                "asking_price": 0,
                "description": "Fan needs repair, shoe rack, scooter, dog bed.",
                "vision": {"observations": {"listing_kind": "product",
                                            "item_type": "box"}},
            }]}), encoding="utf-8")
            listing = run(source, output)["listings"][0]
        self.assertTrue(listing["mixed_lot"])
        self.assertEqual(listing["display_title"],
                         "Fan, shoe rack, scooter and more")
        self.assertIn("Conditions may differ", listing["generated_description"])

    def test_generated_copy_keeps_original_title_and_only_photo_supported_detail(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.json"
            output = Path(directory) / "output.json"
            source.write_text(json.dumps({"listings": [{
                "listing_id": "1", "title": "free couch!", "asking_price": 0,
                "description": "Only the couch is included.",
                "vision": {"observations": {
                    "listing_kind": "product", "item_type": "couch",
                    "classification_evidence": [
                        "Photo: The main object in the photo is a brown fabric couch, "
                        "which is the item being offered."],
                    "wear_or_damage": ["couch shows signs of wear"],
                }},
            }]},), encoding="utf-8")
            result = run(source, output)
            couch = result["listings"][0]
            self.assertEqual(couch["title"], "free couch!")
            self.assertEqual(couch["display_title"], "Couch")
            self.assertEqual(couch["generated_description_status"], "generated")
            self.assertEqual(couch["generated_description"],
                             "The photo shows a brown fabric couch. "
                             "Seller says only the couch is included.")


if __name__ == "__main__":
    unittest.main()
