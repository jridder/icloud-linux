import datetime
import os
import tempfile
import unittest
from unittest.mock import patch

from photos import PhotoIndex, allocate_names, build_tree, classify_process


def asset(asset_id, filename, taken, live_name=None, item_type="image"):
    return {
        "id": asset_id,
        "filename": filename,
        "item_type": item_type,
        "taken": taken,
        "size": 10,
        "live_name": live_name,
        "live_size": 5 if live_name else None,
    }


MAY = datetime.datetime(2024, 5, 3, 12).timestamp()
JUNE = datetime.datetime(2024, 6, 1, 9).timestamp()


class NamingTests(unittest.TestCase):
    def test_duplicates_get_numbered_and_live_pairs_share_a_stem(self):
        names = allocate_names([
            asset("a", "IMG_0001.HEIC", MAY, live_name="IMG_0001.MOV"),
            asset("b", "IMG_0001.HEIC", MAY),
            asset("c", "img_0001.mov", MAY, item_type="movie"),
        ])
        self.assertEqual(names, [
            ("IMG_0001.HEIC", "a", "original"),
            ("IMG_0001.MOV", "a", "live"),
            ("IMG_0001 (2).HEIC", "b", "original"),
            ("img_0001 (2).mov", "c", "original"),
        ])

    def test_unsafe_characters_are_replaced(self):
        self.assertEqual(allocate_names([asset("a", "a/b.jpg", MAY)])[0][0], "a∕b.jpg")
        self.assertEqual(allocate_names([asset("a", "..", MAY)])[0][0], "_")


class TreeTests(unittest.TestCase):
    def test_library_by_month_and_nested_albums(self):
        assets = {
            "a": asset("a", "A.JPG", MAY),
            "b": asset("b", "B.JPG", JUNE),
            "v": asset("v", "V.MOV", JUNE, item_type="movie"),
        }
        tree = build_tree(assets, [("Trips/Iceland", ["b", "missing"]), ("Videos", ["v"])])
        self.assertEqual(tree["/"][1], ["Library", "Albums"])
        self.assertEqual(tree["/Library"][1], ["2024"])
        self.assertEqual(tree["/Library/2024"][1], ["2024-05", "2024-06"])
        self.assertEqual(tree["/Library/2024/2024-06"][1], ["B.JPG", "V.MOV"])
        self.assertEqual(tree["/Albums"][1], ["Trips", "Videos"])
        self.assertEqual(tree["/Albums/Trips/Iceland"][1], ["B.JPG"])
        self.assertEqual(tree["/Albums/Trips/Iceland/B.JPG"], ("file", "b", "original"))

    def test_albums_with_the_same_name_stay_separate(self):
        assets = {"a": asset("a", "A.JPG", MAY), "b": asset("b", "B.JPG", MAY)}
        tree = build_tree(assets, [("Pets", ["a"]), ("Pets", ["b"])])
        self.assertEqual(tree["/Albums"][1], ["Pets", "Pets (2)"])
        self.assertEqual(tree["/Albums/Pets (2)"][1], ["B.JPG"])


class IndexTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as root:
            index = PhotoIndex(os.path.join(root, "index.sqlite3"))
            assets = {"a": asset("a", "A.HEIC", MAY, live_name="A.MOV")}
            index.replace(assets, [("Favorites", ["a"])], "token-1")
            self.assertEqual(index.load(), (assets, [("Favorites", ["a"])]))
            self.assertEqual(index.get_meta("sync_token"), "token-1")


class CallerTests(unittest.TestCase):
    def fake_proc(self, table):
        def fake_open(path, *args, **kwargs):
            _, _, pid, field = path.split("/")
            comm, ppid = table[int(pid)]
            text = comm if field == "comm" else f"{pid} ({comm}) S {ppid} 0 0"
            return unittest.mock.mock_open(read_data=text)()
        return patch("builtins.open", side_effect=fake_open)

    def test_thumbnailer_found_through_sandbox_parents(self):
        table = {30: ("glycin-heif", 20), 20: ("bwrap", 10), 10: ("glycin-thumbnai", 5), 5: ("nautilus", 1)}
        with self.fake_proc(table):
            self.assertEqual(classify_process(30), "thumbnailer")

    def test_image_viewer_loader_is_a_user(self):
        table = {30: ("glycin-heif", 20), 20: ("bwrap", 10), 10: ("loupe", 1)}
        with self.fake_proc(table):
            self.assertEqual(classify_process(30), "user")

    def test_backup_tool_launched_by_deja_dup(self):
        table = {40: ("restic", 30), 30: ("deja-dup", 1)}
        with self.fake_proc(table):
            self.assertEqual(classify_process(40), "backup")

    def test_indexer(self):
        with self.fake_proc({7: ("localsearch-ext", 1)}):
            self.assertEqual(classify_process(7), "indexer")


if __name__ == "__main__":
    unittest.main()
