import unittest
import xml.dom.minidom
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from importlib import import_module


ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests" / "fixtures"
TORZNAB_NS = "{http://torznab.com/schemas/2015/feed}"

# 纯解析模块也必须以生产模块身份加载，避免同一源码出现第二个模块实例。
MODULE = import_module("app.plugins.jackettextend._torznab")


def fixture_fields(name):
    item = ElementTree.parse(FIXTURES / name).find(".//item")
    attrs = {
        node.get("name"): node.get("value")
        for node in item.findall(f"{TORZNAB_NS}attr")
    }
    return {
        "enclosure": item.find("enclosure").get("url"),
        "link": item.findtext("link"),
        "guid": item.findtext("guid"),
        "comments": item.findtext("comments"),
        "magnet_url": attrs.get("magneturl"),
        "files": attrs.get("files"),
    }


class TorznabEnclosureTest(unittest.TestCase):
    def test_private_indexer_direct_download_is_preserved(self):
        fields = fixture_fields("jackettextend_private_http.xml")

        self.assertEqual(
            MODULE.select_torznab_enclosure(
                enclosure=fields["enclosure"],
                link=fields["link"],
                guid=fields["guid"],
            ),
            fields["enclosure"],
        )

    def test_public_indexer_magnet_only_result_stays_magnet(self):
        fields = fixture_fields("jackettextend_public_magnet.xml")

        selected = MODULE.select_torznab_enclosure(
            enclosure=fields["enclosure"],
            link=fields["link"],
            magnet_url=fields["magnet_url"],
            guid=fields["guid"],
        )

        self.assertEqual(selected, fields["enclosure"])
        self.assertTrue(selected.startswith("magnet:"))
        self.assertEqual(fields["files"], "23")
        self.assertNotIn("files=", selected)
        self.assertNotIn(".torrent", selected)

    def test_http_link_wins_when_enclosure_is_magnet(self):
        direct = "https://jackett.invalid/dl/indexer/?path=protected"
        self.assertEqual(
            MODULE.select_torznab_enclosure(
                enclosure="magnet:?xt=urn:btih:fixture",
                link=direct,
                magnet_url="magnet:?xt=urn:btih:fixture",
            ),
            direct,
        )

    def test_direct_torrent_url_is_preserved_exactly(self):
        direct = "https://jackett.invalid/download/release.torrent?ticket=opaque"
        self.assertEqual(
            MODULE.select_torznab_enclosure(enclosure=direct),
            direct,
        )

    def test_detail_and_infohash_cannot_be_promoted_to_download_url(self):
        fields = fixture_fields("jackettextend_public_magnet.xml")

        selected = MODULE.select_torznab_enclosure(
            enclosure="",
            link="",
            magnet_url="",
            guid=fields["comments"],
        )

        self.assertEqual(selected, "")

    def test_unauthorized_json_and_empty_responses_are_rejected(self):
        cases = (
            (401, "application/json", '{"error":"unauthorized"}'),
            (403, "text/html", "<html>forbidden</html>"),
            (200, "application/json", '{"error":"bad request"}'),
            (200, "application/xml", ""),
        )
        for status, content_type, body in cases:
            with self.subTest(status=status, content_type=content_type):
                self.assertFalse(
                    MODULE.is_usable_torznab_response(status, content_type, body)
                )

    def test_log_url_redacts_credentials(self):
        redacted = MODULE.redact_url(
            "http://jackett.invalid/results?q=private%20title&apikey=secret&cat=3000&token=hidden&password=pw"
        )
        self.assertNotIn("private", redacted)
        self.assertNotIn("title", redacted)
        self.assertNotIn("secret", redacted)
        self.assertNotIn("hidden", redacted)
        self.assertNotIn("pw", redacted)
        self.assertIn("q=", redacted)
        self.assertIn("apikey=", redacted)
        self.assertIn("token=", redacted)
        self.assertIn("password=", redacted)
        self.assertIn("cat=3000", redacted)


class TorznabPureParsingTest(unittest.TestCase):
    def test_safe_numeric_helpers_keep_parser_fallbacks(self):
        self.assertEqual(MODULE.safe_int("-3"), -3)
        self.assertEqual(MODULE.safe_int("bad"), 0)
        self.assertEqual(MODULE.safe_float("1.5"), 1.5)
        self.assertEqual(MODULE.safe_float("-1"), 0.0)
        self.assertEqual(MODULE.safe_float("nan"), 0.0)
        self.assertEqual(MODULE.safe_count("4"), 4)
        self.assertEqual(MODULE.safe_count("-4"), 0)
        self.assertEqual(MODULE.safe_float_none("0"), 0.0)
        self.assertIsNone(MODULE.safe_float_none("inf"))
        self.assertIsNone(MODULE.safe_float_none("invalid"))

    def test_normalize_imdb_id_rejects_non_identity_values(self):
        self.assertEqual(MODULE.normalize_imdbid(" TT1234567 "), "tt1234567")
        self.assertEqual(MODULE.normalize_imdbid("tt12345678901234567890"),
                         "tt12345678901234567890")
        self.assertEqual(MODULE.normalize_imdbid("tt0000000"), "")
        self.assertEqual(MODULE.normalize_imdbid("nm1234567"), "")

    def test_extract_item_fields_and_torznab_attrs_without_host_imports(self):
        item = xml.dom.minidom.parseString("""
            <item>
              <title>Fixture title</title>
              <enclosure url="https://jackett.invalid/dl.torrent" />
              <link>https://jackett.invalid/detail</link>
              <guid>guid-value</guid>
              <description>description</description>
              <size>123.5</size>
              <comments>https://jackett.invalid/comments</comments>
              <pubDate>Wed, 01 Jan 2025 00:00:00 GMT</pubDate>
              <torznab:attr xmlns:torznab="http://torznab.com/schemas/2015/feed"
                            name="seeders" value="12" />
              <torznab:attr xmlns:torznab="http://torznab.com/schemas/2015/feed"
                            name="peers" value="3" />
              <torznab:attr xmlns:torznab="http://torznab.com/schemas/2015/feed"
                            name="grabs" value="7" />
              <torznab:attr xmlns:torznab="http://torznab.com/schemas/2015/feed"
                            name="imdbid" value=" TT1234567 " />
              <torznab:attr xmlns:torznab="http://torznab.com/schemas/2015/feed"
                            name="infohash" value=" ABC " />
              <torznab:attr xmlns:torznab="http://torznab.com/schemas/2015/feed"
                            name="magneturl" value="magnet:?xt=urn:btih:abc" />
              <torznab:attr xmlns:torznab="http://torznab.com/schemas/2015/feed"
                            name="uploadvolumefactor" value="2.5" />
              <torznab:attr xmlns:torznab="http://torznab.com/schemas/2015/feed"
                            name="downloadvolumefactor" value="0" />
              <torznab:attr xmlns:torznab="http://torznab.com/schemas/2015/feed"
                            name="hit_and_run" value="YES" />
              <torznab:attr xmlns:torznab="http://torznab.com/schemas/2015/feed"
                            name="label" value="trusted" />
              <torznab:attr xmlns:torznab="http://torznab.com/schemas/2015/feed"
                            name="tag" value="trusted" />
              <torznab:attr xmlns:torznab="http://torznab.com/schemas/2015/feed"
                            name="tag" value="scene" />
            </item>
        """).documentElement

        fields = MODULE.extract_torznab_item(item)

        self.assertEqual(fields["title"], "Fixture title")
        self.assertEqual(fields["enclosure"], "https://jackett.invalid/dl.torrent")
        self.assertEqual(fields["link"], "https://jackett.invalid/detail")
        self.assertEqual(fields["guid"], "guid-value")
        self.assertEqual(fields["description"], "description")
        self.assertEqual(fields["size"], "123.5")
        self.assertEqual(fields["page_url"], "https://jackett.invalid/comments")
        self.assertEqual(fields["pubdate"], "Wed, 01 Jan 2025 00:00:00 GMT")
        self.assertEqual(fields["seeders"], "12")
        self.assertEqual(fields["peers"], "3")
        self.assertEqual(fields["grabs"], "7")
        self.assertEqual(fields["imdbid"], "tt1234567")
        self.assertEqual(fields["infohash"], "ABC")
        self.assertEqual(fields["magnet_url"], "magnet:?xt=urn:btih:abc")
        self.assertEqual(fields["uploadvolumefactor"], "2.5")
        self.assertEqual(fields["downloadvolumefactor"], "0")
        self.assertTrue(fields["hit_and_run"])
        self.assertEqual(fields["labels"], ["trusted", "scene"])

    def test_extract_item_uses_first_child_data_for_cdata_fields(self):
        item = xml.dom.minidom.parseString("""
            <item>
              <title><![CDATA[first title]]>ignored trailing text</title>
              <description><![CDATA[first description]]>ignored trailing text</description>
            </item>
        """).documentElement

        fields = MODULE.extract_torznab_item(item)

        self.assertEqual(fields["title"], "first title")
        self.assertEqual(fields["description"], "first description")

    def test_extract_item_keeps_valid_item_when_optional_tag_is_empty(self):
        item = xml.dom.minidom.parseString("""
            <item>
              <title>valid title</title>
              <enclosure url="https://jackett.invalid/valid.torrent" />
              <description />
              <comments />
            </item>
        """).documentElement

        fields = MODULE.extract_torznab_item(item)

        self.assertEqual(fields["title"], "valid title")
        self.assertEqual(fields["enclosure"], "https://jackett.invalid/valid.torrent")
        self.assertEqual(fields["description"], "")
        self.assertEqual(fields["page_url"], "")

    def test_primary_identity_order_and_http_duplicate_preference(self):
        self.assertEqual(
            MODULE.select_torznab_identity("Hash", "Guid", "Page", "URL"),
            ("infohash", "hash"),
        )
        self.assertEqual(
            MODULE.select_torznab_identity("", "Guid", "Page", "URL"),
            ("guid", "guid"),
        )
        self.assertEqual(
            MODULE.select_torznab_identity("", "", "Page", "URL"),
            ("page_url", "page"),
        )
        self.assertEqual(
            MODULE.select_torznab_identity("", "", "", "URL"),
            ("enclosure", "url"),
        )
        magnet = "magnet:?xt=urn:btih:fixture"
        direct = "https://jackett.invalid/dl/fixture.torrent"
        self.assertTrue(MODULE.is_http_torznab_url(direct))
        self.assertFalse(MODULE.is_http_torznab_url(magnet))
        self.assertTrue(MODULE.should_replace_torznab_duplicate(magnet, direct))
        self.assertFalse(MODULE.should_replace_torznab_duplicate(direct, magnet))
        self.assertFalse(MODULE.should_replace_torznab_duplicate(magnet, magnet))

    def test_shared_page_url_is_ambiguous_only_for_distinct_downloads(self):
        shared = "https://tracker.invalid/"
        self.assertEqual(
            MODULE.find_ambiguous_torznab_page_urls([
                (shared, "https://tracker.invalid/a.torrent"),
                (shared, "https://tracker.invalid/b.torrent"),
                ("https://tracker.invalid/detail/c", "https://tracker.invalid/c.torrent"),
            ]),
            {shared},
        )
        self.assertEqual(
            MODULE.find_ambiguous_torznab_page_urls([
                (shared, "https://tracker.invalid/a.torrent"),
                (shared, "https://tracker.invalid/a.torrent"),
            ]),
            set(),
        )


if __name__ == "__main__":
    unittest.main()
