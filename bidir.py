"""Track which (token, exchange-pair) combinations exhibit bidirectional flow.

A pair is bidirectional if it has fired in BOTH directions within the lookback window.
Bidirectional pairs = self-rebalancing inventory = lower risk.
"""
import time
from collections import defaultdict


class BidirectionalTracker:
    def __init__(self, lookback_sec=6 * 3600, min_each_direction=1):
        self.lookback_sec = lookback_sec
        self.min_each = min_each_direction
        # (token, ex_a, ex_b) -> [timestamps]   (always sorted ex_a < ex_b)
        self.events_a_to_b: dict = defaultdict(list)
        self.events_b_to_a: dict = defaultdict(list)

    @staticmethod
    def _key(token, ex1, ex2):
        a, b = sorted([ex1, ex2])
        return (token, a, b), (ex1 == a)  # second value: True if direction is a→b

    def record(self, token, buy_ex, sell_ex):
        key, is_a_to_b = self._key(token, buy_ex, sell_ex)
        ts = time.time()
        bucket = self.events_a_to_b if is_a_to_b else self.events_b_to_a
        bucket[key].append(ts)
        # prune
        cutoff = ts - self.lookback_sec
        bucket[key] = [t for t in bucket[key] if t >= cutoff]

    def is_bidirectional(self, token, buy_ex, sell_ex) -> bool:
        key, _ = self._key(token, buy_ex, sell_ex)
        cutoff = time.time() - self.lookback_sec
        a_to_b = sum(1 for t in self.events_a_to_b.get(key, []) if t >= cutoff)
        b_to_a = sum(1 for t in self.events_b_to_a.get(key, []) if t >= cutoff)
        return a_to_b >= self.min_each and b_to_a >= self.min_each

    def stats(self) -> dict:
        cutoff = time.time() - self.lookback_sec
        bidir = 0
        all_keys = set(self.events_a_to_b) | set(self.events_b_to_a)
        for key in all_keys:
            a = sum(1 for t in self.events_a_to_b.get(key, []) if t >= cutoff)
            b = sum(1 for t in self.events_b_to_a.get(key, []) if t >= cutoff)
            if a >= self.min_each and b >= self.min_each:
                bidir += 1
        return {"total_pairs": len(all_keys), "bidirectional": bidir}
