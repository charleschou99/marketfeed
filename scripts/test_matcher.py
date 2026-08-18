#!/usr/bin/env python3
"""Regression suite for the watchlist matcher (run from repo root)."""
import sys; sys.path.insert(0, 'scripts')
from collect import _find_watchlist_hits, _load_watchlist, FILLER_RE

wl = _load_watchlist()
def hits(t): return _find_watchlist_hits(t, wl)

assert not hits('Bank of Japan holds rates steady')            # C1 no false pos
assert '03988.HK' in hits('Bank of China reports NIM pressure')
assert 'BAC' in hits('Bank of America raises dividend')
assert hits('Tencent profit beats') == ['00700.HK']
assert '01810.HK' in hits("Xiaomi's profits drop 43% on memory crunch")
assert '09988.HK' in hits('Alibaba Cloud revenue accelerates')
assert '00700.HK' in hits('Tencent Music reports earnings')
assert hits('Samsung Electronics flags memory recovery') == ['005930.KS']
assert hits("Japan's cat cafes delight") == []                 # m0
assert not FILLER_RE.match('Best Buy raises guidance after strong quarter')  # m1
assert FILLER_RE.match('Best Stocks Under $10')

# Alias table: Bloomberg short names resolve to the headline names too.
assert '09888.HK' in hits('Baidu unveils new Ernie model')
assert '09999.HK' in hits('NetEase games revenue climbs')
assert '01698.HK' in hits('Tencent Music reports earnings')

print('test_matcher.py: all asserts passed')
