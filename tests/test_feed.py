import unittest

from feed import QuoteCache, fill_key, match_trade, row_from_activity, take_frames


WALLET = "0x" + "ab" * 20


def payload(**overrides):
    result = {
        "proxyWallet": WALLET,
        "transactionHash": "0xabc",
        "conditionId": "0xcondition",
        "asset": "12345",
        "timestamp": 1_700_000_000,
        "side": "BUY",
        "size": 5,
        "price": 0.15,
        "slug": "btc-updown-5m-1700000000",
        "outcome": "Up",
    }
    result.update(overrides)
    return result


class FeedTests(unittest.TestCase):
    def test_take_frames_reads_a_complete_text_frame_and_keeps_a_partial(self):
        body = b'{"ok":true}'
        whole = bytes([0x81, len(body)]) + body
        frames, rest = take_frames(whole)
        self.assertEqual(frames, [(True, 1, body)])
        self.assertEqual(rest, b"")
        partial = whole[:5]
        frames, rest = take_frames(partial)
        self.assertEqual(frames, [])
        self.assertEqual(rest, partial)
        frames, rest = take_frames(partial + whole[5:])
        self.assertEqual(frames, [(True, 1, body)])
        self.assertEqual(rest, b"")

    def test_match_trade_keeps_his_side_and_market_only(self):
        row = match_trade(payload(), WALLET.upper())
        self.assertEqual(row["side"], "BUY")
        self.assertEqual(row["outcome"], "Up")
        self.assertEqual(row["slug"], "btc-updown-5m-1700000000")
        self.assertEqual(row["token_id"], "12345")
        self.assertEqual(row["condition_id"], "0xcondition")
        self.assertEqual(row["timestamp"], 1_700_000_000)
        self.assertIsNone(match_trade(payload(), "0x" + "cd" * 20))
        self.assertIsNone(match_trade({"proxyWallet": WALLET}, WALLET))

    def test_quote_cache_returns_a_young_book_for_the_same_market(self):
        cache = QuoteCache()
        market = {"slug": "btc-updown-5m-100"}
        book = {"timestamp": 1}
        cache.store("token", market, book, 10.0)
        fresh = cache.take("token", "btc-updown-5m-100", 10.4, 1.0)
        self.assertEqual(fresh[0], market)
        self.assertEqual(fresh[1], book)
        self.assertAlmostEqual(fresh[2], 0.4)
        self.assertIsNone(cache.take("token", "btc-updown-5m-100", 11.2, 1.0))
        self.assertIsNone(cache.take("token", "btc-updown-15m-100", 10.4, 1.0))

    def test_fill_key_matches_the_stream_trade_and_the_activity_row(self):
        streamed = row_from_activity(payload())
        activity = dict(streamed, transaction_hash="0xABC", price="0.15", size="5.0")
        self.assertEqual(fill_key(streamed), fill_key(activity))


if __name__ == "__main__":
    unittest.main()
