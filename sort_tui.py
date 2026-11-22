#!/usr/bin/env python3
"""Sorting algorithms, animated — the bars you know from the videos, in the terminal.

Thirty-seven sorts, from bubble sort to bitonic networks and bogosort: watch the comparisons light
up red and the writes green, then the green sweep that checks the result. Race nine of them on the
same input side by side, let demo mode play one after another, or step through one move at a
time. Counts of comparisons, swaps and writes run along the bottom.

Every algorithm is a generator over a counting array: it yields after each comparison or write,
so one "step" is one operation and a race between them is fair.
"""

import argparse
import colorsys
import curses
import locale
import math
import random
import time
from collections import namedtuple

TICK_MS = 30
MIN_W, MIN_H = 60, 14
SIZES = (8, 16, 32, 48, 64, 100, 128, 160, 200, 256, 320)
SPEEDS = ("auto", 1 / 8, 1 / 4, 1 / 2, 1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 4096)   # steps a frame
AUTO_SECONDS = 9               # how long auto speed aims to make a sort take
DEMO_PAUSE = 1.6               # seconds between sorts in demo mode
SHAPES = ("random", "reversed", "nearly sorted", "few unique", "sorted", "pipe organ", "sawtooth")
BLOCKS = " ▁▂▃▄▅▆▇█"
MARKS = {"cmp": 1, "write": 2, "pivot": 3, "read": 6}      # what an index was touched for -> colour pair

rng = random.Random()


class Arr:
    """The array being sorted. Every look and every change is counted and remembered, so the
    screen can light up what the last steps touched."""

    def __init__(self, values):
        self.v = list(values)
        self.cmps = self.swaps = self.writes = self.reads = 0
        self.hot = {}

    def __len__(self):
        return len(self.v)

    def gt(self, i, j):
        self.cmps += 1
        self.hot[i] = self.hot[j] = "cmp"
        return self.v[i] > self.v[j]

    def lt(self, i, j):
        self.cmps += 1
        self.hot[i] = self.hot[j] = "cmp"
        return self.v[i] < self.v[j]

    def cmp_v(self, x, i):
        """-1, 0 or 1 as a value held aside compares to a[i]."""
        self.cmps += 1
        self.hot[i] = "cmp"
        y = self.v[i]
        return (x > y) - (x < y)

    def get(self, i):
        self.reads += 1
        self.hot.setdefault(i, "read")
        return self.v[i]

    def set(self, i, x):
        self.writes += 1
        self.hot[i] = "write"
        self.v[i] = x

    def swap(self, i, j):
        self.swaps += 1
        self.writes += 2
        self.hot[i] = self.hot[j] = "write"
        self.v[i], self.v[j] = self.v[j], self.v[i]

    def mark(self, i, kind="pivot"):
        self.hot[i] = kind


# -- the algorithms: generators that yield after each operation --

def bubble(a):
    n = len(a)
    for i in range(n - 1):
        swapped = False
        for j in range(n - 1 - i):
            if a.gt(j, j + 1):
                a.swap(j, j + 1)
                swapped = True
            yield
        if not swapped:
            return


def cocktail(a):
    lo, hi = 0, len(a) - 1
    while lo < hi:
        last = lo
        for j in range(lo, hi):
            if a.gt(j, j + 1):
                a.swap(j, j + 1)
                last = j
            yield
        hi = last
        for j in range(hi, lo, -1):
            if a.gt(j - 1, j):
                a.swap(j - 1, j)
                last = j
            yield
        lo = last


def odd_even(a):
    n, done = len(a), False
    while not done:
        done = True
        for start in (1, 0):
            for j in range(start, n - 1, 2):
                if a.gt(j, j + 1):
                    a.swap(j, j + 1)
                    done = False
                yield


def exchange(a):
    n = len(a)
    for i in range(n - 1):
        for j in range(i + 1, n):
            if a.gt(i, j):
                a.swap(i, j)
            yield


def selection(a):
    n = len(a)
    for i in range(n - 1):
        m = i
        for j in range(i + 1, n):
            a.mark(m)
            if a.lt(j, m):
                m = j
            yield
        if m != i:
            a.swap(i, m)
            yield


def double_selection(a):
    lo, hi = 0, len(a) - 1
    while lo < hi:
        mn = mx = lo
        for j in range(lo + 1, hi + 1):
            if a.lt(j, mn):
                mn = j
            elif a.gt(j, mx):
                mx = j
            yield
        a.swap(lo, mn)
        yield
        if mx == lo:
            mx = mn
        a.swap(hi, mx)
        yield
        lo, hi = lo + 1, hi - 1


def insertion(a):
    for i in range(1, len(a)):
        j = i
        while j > 0 and a.gt(j - 1, j):
            a.swap(j - 1, j)
            j -= 1
            yield
        yield


def insertion_range(a, lo, hi):
    for i in range(lo + 1, hi):
        j = i
        while j > lo and a.gt(j - 1, j):
            a.swap(j - 1, j)
            j -= 1
            yield
        yield


def binary_insertion(a):
    for i in range(1, len(a)):
        x = a.get(i)
        lo, hi = 0, i
        while lo < hi:
            mid = (lo + hi) // 2
            if a.cmp_v(x, mid) < 0:
                hi = mid
            else:
                lo = mid + 1
            yield
        for j in range(i, lo, -1):
            a.set(j, a.get(j - 1))
            yield
        a.set(lo, x)
        yield


def gnome(a):
    i, n = 1, len(a)
    while i < n:
        if i == 0 or not a.gt(i - 1, i):
            i += 1
        else:
            a.swap(i - 1, i)
            i -= 1
        yield


def comb(a):
    n, gap, done = len(a), len(a), False
    while gap > 1 or not done:
        gap = max(1, int(gap / 1.3))
        done = True
        for j in range(n - gap):
            if a.gt(j, j + gap):
                a.swap(j, j + gap)
                done = False
            yield


CIURA = (1, 4, 10, 23, 57, 132, 301, 701, 1750)


def shell(a):
    n = len(a)
    for gap in [g for g in CIURA if g < n][::-1] or [1]:
        for i in range(gap, n):
            j = i
            while j >= gap and a.gt(j - gap, j):
                a.swap(j - gap, j)
                j -= gap
                yield
            yield


def merge(a, lo, mid, hi):
    left = [a.get(k) for k in range(lo, mid)]
    i, j, k = 0, mid, lo
    while i < len(left) and j < hi:
        if a.cmp_v(left[i], j) <= 0:
            a.set(k, left[i])
            i += 1
        else:
            a.set(k, a.get(j))
            j += 1
        k += 1
        yield
    while i < len(left):
        a.set(k, left[i])
        i, k = i + 1, k + 1
        yield


def merge_sort(a):
    def rec(lo, hi):
        if hi - lo < 2:
            return
        mid = (lo + hi) // 2
        yield from rec(lo, mid)
        yield from rec(mid, hi)
        yield from merge(a, lo, mid, hi)
    yield from rec(0, len(a))


def merge_in_place(a):
    """Merges without a second array: a value from the right half is carried left by shifting
    everything between over by one."""
    def merge_(lo, mid, hi):
        while lo < mid < hi:
            if not a.gt(lo, mid):
                lo += 1
                yield
                continue
            x = a.get(mid)
            for k in range(mid, lo, -1):
                a.set(k, a.get(k - 1))
                yield
            a.set(lo, x)
            lo, mid = lo + 1, mid + 1
            yield

    def rec(lo, hi):
        if hi - lo < 2:
            return
        mid = (lo + hi) // 2
        yield from rec(lo, mid)
        yield from rec(mid, hi)
        yield from merge_(lo, mid, hi)
    yield from rec(0, len(a))


def merge_bottom_up(a):
    n, w = len(a), 1
    while w < n:
        for lo in range(0, n - w, 2 * w):
            yield from merge(a, lo, lo + w, min(lo + 2 * w, n))
        w *= 2


def tim(a):
    n = len(a)
    run = max(4, min(32, n // 8))
    for lo in range(0, n, run):
        yield from insertion_range(a, lo, min(lo + run, n))
    w = run
    while w < n:
        for lo in range(0, n - w, 2 * w):
            yield from merge(a, lo, lo + w, min(lo + 2 * w, n))
        w *= 2


def quick_lomuto(a):
    stack = [(0, len(a) - 1)]
    while stack:
        lo, hi = stack.pop()
        if lo >= hi:
            continue
        i = lo
        for j in range(lo, hi):
            a.mark(hi)
            if a.lt(j, hi):
                a.swap(i, j)
                i += 1
            yield
        a.swap(i, hi)
        yield
        stack += [(i + 1, hi), (lo, i - 1)]


def quick_hoare(a):
    stack = [(0, len(a) - 1)]
    while stack:
        lo, hi = stack.pop()
        if lo >= hi:
            continue
        p = (lo + hi) // 2
        pv = a.get(p)
        i, j = lo, hi
        while i <= j:
            while a.cmp_v(pv, i) > 0:
                i += 1
                yield
            while a.cmp_v(pv, j) < 0:
                j -= 1
                yield
            if i <= j:
                a.swap(i, j)
                i, j = i + 1, j - 1
                yield
        stack += [(i, hi), (lo, j)]


def quick_three_way(a):
    stack = [(0, len(a) - 1)]
    while stack:
        lo, hi = stack.pop()
        if lo >= hi:
            continue
        pv = a.get(lo)
        lt, i, gt = lo, lo + 1, hi
        while i <= gt:
            c = a.cmp_v(pv, i)
            if c > 0:                           # a[i] < pivot
                a.swap(lt, i)
                lt, i = lt + 1, i + 1
            elif c < 0:
                a.swap(i, gt)
                gt -= 1
            else:
                i += 1
            yield
        stack += [(gt + 1, hi), (lo, lt - 1)]


def quick_dual(a):
    stack = [(0, len(a) - 1)]
    while stack:
        lo, hi = stack.pop()
        if lo >= hi:
            continue
        if a.gt(lo, hi):
            a.swap(lo, hi)
        yield
        lt, gt, k = lo + 1, hi - 1, lo + 1
        while k <= gt:
            a.mark(lo)
            a.mark(hi)
            if a.lt(k, lo):
                a.swap(k, lt)
                lt += 1
            elif a.gt(k, hi):
                while k < gt and a.gt(gt, hi):
                    gt -= 1
                    yield
                a.swap(k, gt)
                gt -= 1
                if a.lt(k, lo):
                    a.swap(k, lt)
                    lt += 1
            k += 1
            yield
        lt, gt = lt - 1, gt + 1
        a.swap(lo, lt)
        a.swap(hi, gt)
        yield
        stack += [(gt + 1, hi), (lt + 1, gt - 1), (lo, lt - 1)]


def sift(a, i, n, base=0):
    while True:
        c = 2 * i + 1
        if c >= n:
            return
        if c + 1 < n and a.lt(base + c, base + c + 1):
            c += 1
        if not a.lt(base + i, base + c):
            yield
            return
        a.swap(base + i, base + c)
        i = c
        yield


def heap_range(a, lo, hi):
    n = hi - lo
    for s in range(n // 2 - 1, -1, -1):
        yield from sift(a, s, n, lo)
    for end in range(n - 1, 0, -1):
        a.swap(lo, lo + end)
        yield
        yield from sift(a, 0, end, lo)


def heap(a):
    yield from heap_range(a, 0, len(a))


def intro(a):
    def rec(lo, hi, depth):
        while hi - lo > 16:
            if depth == 0:
                yield from heap_range(a, lo, hi)
                return
            depth -= 1
            mid = (lo + hi - 1) // 2               # median of three goes to hi - 1
            if a.lt(mid, lo):
                a.swap(mid, lo)
            if a.lt(hi - 1, lo):
                a.swap(hi - 1, lo)
            if a.lt(mid, hi - 1):
                a.swap(mid, hi - 1)
            yield
            p, i = hi - 1, lo
            for j in range(lo, hi - 1):
                a.mark(p)
                if a.lt(j, p):
                    a.swap(i, j)
                    i += 1
                yield
            a.swap(i, p)
            yield
            yield from rec(i + 1, hi, depth)
            hi = i
        yield from insertion_range(a, lo, hi)
    yield from rec(0, len(a), 2 * max(1, int(math.log2(max(2, len(a))))))


def counting(a):
    n = len(a)
    vals = []
    for i in range(n):
        vals.append(a.get(i))
        yield
    lo, hi = min(vals), max(vals)
    counts = [0] * (hi - lo + 1)
    for x in vals:
        counts[x - lo] += 1
    k = 0
    for v, c in enumerate(counts):
        for _ in range(c):
            a.set(k, v + lo)
            k += 1
            yield


def pigeonhole(a):
    n = len(a)
    holes = {}
    for i in range(n):
        x = a.get(i)
        holes.setdefault(x, []).append(x)
        yield
    k = 0
    for x in sorted(holes):
        for y in holes[x]:
            a.set(k, y)
            k += 1
            yield


def bucket(a):
    n = len(a)
    vals = []
    for i in range(n):
        vals.append(a.get(i))
        yield
    hi = max(vals) + 1
    nb = max(1, int(math.sqrt(n)))
    buckets = [[] for _ in range(nb)]
    for x in vals:
        buckets[x * nb // hi].append(x)
    k, bounds = 0, []
    for b in buckets:
        start = k
        for x in b:
            a.set(k, x)
            k += 1
            yield
        bounds.append((start, k))
    for lo, hi_ in bounds:
        yield from insertion_range(a, lo, hi_)


def radix_lsd(a, base):
    n = len(a)
    mx = 0
    for i in range(n):
        mx = max(mx, a.get(i))
        yield
    exp = 1
    while mx // exp > 0:
        buckets = [[] for _ in range(base)]
        for i in range(n):
            x = a.get(i)
            buckets[(x // exp) % base].append(x)
            yield
        k = 0
        for b in buckets:
            for x in b:
                a.set(k, x)
                k += 1
                yield
        exp *= base


def radix_msd(a, base=4):
    n = len(a)
    mx = 0
    for i in range(n):
        mx = max(mx, a.get(i))
        yield
    exp = 1
    while exp * base <= mx:
        exp *= base

    def rec(lo, hi, exp):
        if hi - lo < 2 or exp == 0:
            return
        buckets = [[] for _ in range(base)]
        for i in range(lo, hi):
            x = a.get(i)
            buckets[(x // exp) % base].append(x)
            yield
        k, bounds = lo, []
        for b in buckets:
            start = k
            for x in b:
                a.set(k, x)
                k += 1
                yield
            bounds.append((start, k))
        for s, e in bounds:
            yield from rec(s, e, exp // base)
    yield from rec(0, n, exp)


def gravity(a):
    """Bead sort: the beads fall, and each rod's height is the count of values at least that tall."""
    n = len(a)
    vals = []
    for i in range(n):
        vals.append(a.get(i))
        yield
    mx = max(vals)
    at_least = [0] * (mx + 2)
    for x in vals:
        at_least[x] += 1
    for h in range(mx - 1, 0, -1):
        at_least[h] += at_least[h + 1]
    for j in range(n):                         # the j-th rod from the right holds beads where enough fell
        height = sum(1 for h in range(1, mx + 1) if at_least[h] >= n - j)
        a.set(j, height)
        yield


def cycle(a):
    n = len(a)
    for start in range(n - 1):
        item = a.get(start)
        pos = start
        for i in range(start + 1, n):
            if a.cmp_v(item, i) > 0:
                pos += 1
            yield
        if pos == start:
            continue
        while a.cmp_v(item, pos) == 0:
            pos += 1
            yield
        old = a.get(pos)
        a.set(pos, item)
        item = old
        yield
        while pos != start:
            pos = start
            for i in range(start + 1, n):
                if a.cmp_v(item, i) > 0:
                    pos += 1
                yield
            while a.cmp_v(item, pos) == 0:
                pos += 1
                yield
            old = a.get(pos)
            a.set(pos, item)
            item = old
            yield


def flip(a, k):
    i = 0
    while i < k:
        a.swap(i, k)
        i, k = i + 1, k - 1
        yield


def pancake(a):
    for size in range(len(a), 1, -1):
        m = 0
        for i in range(1, size):
            if a.gt(i, m):
                m = i
            yield
        if m != size - 1:
            if m:
                yield from flip(a, m)
            yield from flip(a, size - 1)


def circle(a):
    def rec(lo, hi):
        if lo >= hi:
            return False
        swapped = False
        i, j = lo, hi
        while i < j:
            if a.gt(i, j):
                a.swap(i, j)
                swapped = True
            i, j = i + 1, j - 1
            yield
        if i == j and a.gt(i, i + 1):
            a.swap(i, i + 1)
            swapped = True
        mid = lo + (hi - lo) // 2
        left = yield from rec(lo, mid)
        right = yield from rec(mid + 1, hi)
        return swapped or left or right
    while (yield from rec(0, len(a) - 1)):
        pass


def bitonic(a):
    """Batcher's bitonic network, in the form that works for any length, not just powers of two."""
    def sort(lo, n, up):
        if n > 1:
            m = n // 2
            yield from sort(lo, m, not up)
            yield from sort(lo + m, n - m, up)
            yield from merge_(lo, n, up)

    def merge_(lo, n, up):
        if n > 1:
            m = 1 << (n - 1).bit_length() - 1
            for i in range(lo, lo + n - m):
                if a.gt(i, i + m) == up:
                    a.swap(i, i + m)
                yield
            yield from merge_(lo, m, up)
            yield from merge_(lo + m, n - m, up)
    yield from sort(0, len(a), True)


def batcher(a):
    """Batcher's odd–even merge sort network, for any length."""
    n, p = len(a), 1
    while p < n:
        k = p
        while k >= 1:
            for j in range(k % p, n - k, 2 * k):
                for i in range(min(k, n - j - k)):
                    if (i + j) // (2 * p) == (i + j + k) // (2 * p):
                        if a.gt(i + j, i + j + k):
                            a.swap(i + j, i + j + k)
                        yield
            k //= 2
        p *= 2


def stooge(a):
    def rec(i, j):
        if a.gt(i, j):
            a.swap(i, j)
        yield
        if j - i + 1 > 2:
            t = (j - i + 1) // 3
            yield from rec(i, j - t)
            yield from rec(i + t, j)
            yield from rec(i, j - t)
    if len(a) > 1:
        yield from rec(0, len(a) - 1)


def slow(a):
    """Multiply and surrender: the deliberately worst sort that still finishes."""
    def rec(i, j):
        if i >= j:
            return
        m = (i + j) // 2
        yield from rec(i, m)
        yield from rec(m + 1, j)
        if a.gt(m, j):
            a.swap(m, j)
        yield
        yield from rec(i, j - 1)
    yield from rec(0, len(a) - 1)


def bogo(a):
    n = len(a)
    while True:
        for i in range(n - 1):
            yield
            if a.gt(i, i + 1):
                break
        else:
            return
        for i in range(n - 1, 0, -1):
            a.swap(i, rng.randint(0, i))
            yield


def bozo(a):
    n = len(a)
    while True:
        for i in range(n - 1):
            yield
            if a.gt(i, i + 1):
                break
        else:
            return
        a.swap(rng.randrange(n), rng.randrange(n))
        yield


Algo = namedtuple("Algo", "name fn kind best avg worst stable limit desc")
ALGOS = [
    Algo("Bubble sort", bubble, "exchange", "n", "n²", "n²", True, None,
         "Swaps neighbours that are out of order; each pass carries the largest left to the end."),
    Algo("Cocktail shaker sort", cocktail, "exchange", "n", "n²", "n²", True, None,
         "Bubble sort both ways: up the array, then back down, so small values travel fast too."),
    Algo("Odd–even sort", odd_even, "exchange", "n", "n²", "n²", True, None,
         "Compares all odd pairs, then all even pairs — every comparison in a pass could happen at once."),
    Algo("Exchange sort", exchange, "exchange", "n²", "n²", "n²", False, None,
         "Compares the first place with every later one and swaps when smaller; then the second place…"),
    Algo("Gnome sort", gnome, "exchange", "n", "n²", "n²", True, None,
         "A garden gnome sorting pots: step forward if in order, else swap and step back."),
    Algo("Comb sort", comb, "exchange", "n log n", "n²/2ᵖ", "n²", False, None,
         "Bubble sort over a gap that shrinks by 1.3 each pass, so small values far right ('turtles') move early."),
    Algo("Selection sort", selection, "selection", "n²", "n²", "n²", False, None,
         "Finds the smallest of what's left and swaps it to the front."),
    Algo("Double selection sort", double_selection, "selection", "n²", "n²", "n²", False, None,
         "Finds the smallest and the largest at once, and puts them at both ends."),
    Algo("Cycle sort", cycle, "selection", "n²", "n²", "n²", False, None,
         "Counts how many are smaller to find each value's place: the fewest writes possible."),
    Algo("Pancake sort", pancake, "selection", "n", "n²", "n²", False, None,
         "Only flips the top of the stack: flip the largest to the top, then flip it down into place."),
    Algo("Heap sort", heap, "selection", "n log n", "n log n", "n log n", False, None,
         "Builds a max-heap, then keeps moving the top to the end and sifting the heap back into shape."),
    Algo("Insertion sort", insertion, "insertion", "n", "n²", "n²", True, None,
         "Takes each value and slides it left until it fits among the sorted ones."),
    Algo("Binary insertion sort", binary_insertion, "insertion", "n log n", "n²", "n²", True, None,
         "Insertion sort that finds the place by binary search, then shifts everything over."),
    Algo("Shell sort", shell, "insertion", "n log n", "n^1.3", "n^1.5", False, None,
         "Insertion sort over shrinking gaps (Ciura's 701, 301, 132, 57, 23, 10, 4, 1)."),
    Algo("Merge sort", merge_sort, "merge", "n log n", "n log n", "n log n", True, None,
         "Splits in halves until single values, then merges sorted halves back together."),
    Algo("Bottom-up merge sort", merge_bottom_up, "merge", "n log n", "n log n", "n log n", True, None,
         "Merges pairs, then fours, then eights… no recursion at all."),
    Algo("In-place merge sort", merge_in_place, "merge", "n log n", "n²", "n²", True, None,
         "Merge sort with no second array: values from the right half are shifted in one by one."),
    Algo("Timsort (simplified)", tim, "merge", "n", "n log n", "n log n", True, None,
         "Python's own idea: insertion-sort small runs, then merge the runs (without the galloping)."),
    Algo("Quicksort (Lomuto)", quick_lomuto, "partition", "n log n", "n log n", "n²", False, None,
         "The last value is the pivot; smaller ones are swapped to the front, then each side is sorted."),
    Algo("Quicksort (Hoare)", quick_hoare, "partition", "n log n", "n log n", "n²", False, None,
         "Two pointers walk in from both ends with the middle value as pivot, swapping what's misplaced."),
    Algo("Quicksort (3-way)", quick_three_way, "partition", "n", "n log n", "n²", False, None,
         "Splits into smaller, equal and larger: the equal ones are done at once. Try few unique."),
    Algo("Dual-pivot quicksort", quick_dual, "partition", "n log n", "n log n", "n²", False, None,
         "Two pivots, three parts (Yaroslavskiy's — Java sorts numbers this way)."),
    Algo("Introsort", intro, "partition", "n log n", "n log n", "n log n", False, None,
         "Quicksort that falls back to heap sort if it recurses too deep, and insertion sort when small."),
    Algo("Counting sort", counting, "distribution", "n+k", "n+k", "n+k", True, None,
         "Never compares: counts how often each value occurs, then writes them out in order."),
    Algo("Pigeonhole sort", pigeonhole, "distribution", "n+k", "n+k", "n+k", True, None,
         "One hole per value; drop each in its hole, then empty the holes in order."),
    Algo("Bucket sort", bucket, "distribution", "n+k", "n+k", "n²", True, None,
         "Spreads the values over √n buckets by size, then insertion-sorts within each bucket."),
    Algo("Radix sort (LSD, base 4)", lambda a: radix_lsd(a, 4), "distribution", "nk", "nk", "nk", True, None,
         "Sorts by the last base-4 digit, then the one before… stable passes, no comparisons."),
    Algo("Radix sort (LSD, base 10)", lambda a: radix_lsd(a, 10), "distribution", "nk", "nk", "nk", True, None,
         "The same, by decimal digits: three passes for numbers under a thousand."),
    Algo("Radix sort (MSD, base 4)", radix_msd, "distribution", "nk", "nk", "nk", True, None,
         "Splits by the first digit into buckets, then sorts each bucket by the next digit."),
    Algo("Gravity (bead) sort", gravity, "distribution", "n", "S", "S", False, None,
         "Each value is a row of beads on rods; let them fall and count the beads on each rod."),
    Algo("Circle sort", circle, "network", "n log n", "n log n · log n", "n log n · log n", False, None,
         "Compares the ends of each half inward, like circles, recursively, until a pass swaps nothing."),
    Algo("Bitonic sort", bitonic, "network", "n log² n", "n log² n", "n log² n", False, None,
         "A sorting network: builds up-and-down sequences and merges them. Fixed comparisons, any input."),
    Algo("Batcher odd–even merge sort", batcher, "network", "n log² n", "n log² n", "n log² n", False, None,
         "Batcher's other network: merges odd and even halves recursively, with a fixed set of comparisons."),
    Algo("Stooge sort", stooge, "joke", "n^2.7", "n^2.7", "n^2.7", False, 48,
         "Sort the first two-thirds, the last two-thirds, then the first two-thirds again."),
    Algo("Slowsort", slow, "joke", "n^(log n)", "n^(log n)", "n^(log n)", False, 32,
         "'Multiply and surrender': find the largest the slowest possible way, put it last, repeat."),
    Algo("Bogosort", bogo, "joke", "n", "n·n!", "∞", False, 7,
         "Shuffle until sorted. At 7 bars that's about 5,000 shuffles; at 16, longer than you have."),
    Algo("Bozosort", bozo, "joke", "n", "n·n!", "∞", False, 6,
         "Bogosort's lazy cousin: swap two random values and check again."),
]
BY_NAME = {x.name: x for x in ALGOS}
RACES = (
    ("the n log n ones", ("Merge sort", "Quicksort (Hoare)", "Heap sort", "Shell sort", "Introsort",
                          "Timsort (simplified)", "Dual-pivot quicksort", "Bottom-up merge sort", "Comb sort")),
    ("the n² ones", ("Bubble sort", "Cocktail shaker sort", "Odd–even sort", "Selection sort",
                     "Double selection sort", "Insertion sort", "Binary insertion sort", "Gnome sort",
                     "Cycle sort")),
    ("no comparisons", ("Counting sort", "Pigeonhole sort", "Bucket sort", "Radix sort (LSD, base 4)",
                        "Radix sort (LSD, base 10)", "Radix sort (MSD, base 4)", "Gravity (bead) sort",
                        "Merge sort", "Quicksort (Hoare)")),
    ("quicksorts", ("Quicksort (Lomuto)", "Quicksort (Hoare)", "Quicksort (3-way)", "Dual-pivot quicksort",
                    "Introsort", "Merge sort")),
    ("networks", ("Bitonic sort", "Batcher odd–even merge sort", "Odd–even sort", "Circle sort")),
)


def estimate(algo, n):
    """Roughly how many steps a sort of n takes, so auto speed can pace it."""
    lg = math.log2(max(2, n))
    cost = {"n²": n * n / 3, "n log n": 1.6 * n * lg, "n^1.3": 1.6 * n * lg, "n+k": 2.2 * n, "nk": 2 * n * 4,
            "S": 2 * n, "n log n · log n": n * lg * lg / 2, "n log² n": n * lg * lg / 2, "n²/2ᵖ": 2 * n * lg,
            "n^2.7": n ** 2.7 / 2, "n^(log n)": n ** 3, "n·n!": n * math.factorial(min(n, 10))}
    return cost.get(algo.avg, n * n / 3)


def make_input(shape, n):
    vals = list(range(1, n + 1))
    if shape == "random":
        rng.shuffle(vals)
    elif shape == "reversed":
        vals.reverse()
    elif shape == "nearly sorted":
        for _ in range(max(1, n // 12)):
            i = rng.randrange(n)
            j = min(n - 1, i + rng.randint(1, 4))
            vals[i], vals[j] = vals[j], vals[i]
    elif shape == "few unique":
        k = 5
        vals = [(rng.randrange(k) + 1) * n // k for _ in range(n)]
    elif shape == "pipe organ":
        vals = [v for v in vals if v % 2] + [v for v in reversed(vals) if not v % 2]
    elif shape == "sawtooth":
        rng.shuffle(vals)
        q = max(1, n // 4)
        vals = [v for k in range(0, n, q) for v in sorted(vals[k:k + q])]
    return vals


class Runner:
    """One sort in progress."""

    def __init__(self, algo, values):
        self.algo = algo
        self.a = Arr(values)
        self.gen = algo.fn(self.a)
        self.steps = 0
        self.done = False
        self.hot = {}
        self.sweep = None                   # how far the green check has got, once done
        self.ok = None                      # whether it really came out sorted
        self.place = None                   # finishing position in a race
        self.started, self.took = time.monotonic(), None
        self.credit = 0.0                   # fractions of a step owed, for slow speeds

    def advance(self, k):
        hot = {}
        for _ in range(k):
            if self.done:
                break
            self.a.hot = {}
            try:
                next(self.gen)
                self.steps += 1
            except StopIteration:
                self.done = True
                self.took = time.monotonic() - self.started
                v = self.a.v
                self.ok = all(v[i] <= v[i + 1] for i in range(len(v) - 1))
                self.sweep = 0
                break
            for i, kind in self.a.hot.items():
                if hot.get(i) != "cmp":
                    hot[i] = kind
        self.hot = hot


# -- drawing --

def rainbow_pairs():
    """Colour pairs 16.. along the hue circle, when the terminal has 256 colours."""
    if curses.COLORS < 256:
        return [curses.COLOR_RED, curses.COLOR_YELLOW, curses.COLOR_GREEN, curses.COLOR_CYAN,
                curses.COLOR_BLUE, curses.COLOR_MAGENTA]
    out = []
    for k in range(36):
        r, g, b = colorsys.hsv_to_rgb(k / 36 * 0.85, 0.85, 1.0)
        out.append(16 + 36 * round(r * 5) + 6 * round(g * 5) + round(b * 5))
    return out


class App:
    def __init__(self, stdscr, args):
        self.s = stdscr
        self.ai = next((i for i, x in enumerate(ALGOS) if x.name == "Quicksort (Hoare)"), 0)
        if args.algorithm:
            want = args.algorithm.lower()
            self.ai = next((i for i, x in enumerate(ALGOS) if want in x.name.lower()), self.ai)
        self.shape = args.shape
        self.speed_i = 0
        self.view = "bars"
        self.colour = "plain"
        self.demo = args.demo
        self.race = None                    # index into RACES while racing
        self.paused = False
        self.done_at = None
        self.message, self.message_at = "", 0.0
        h, w = stdscr.getmaxyx()
        self.n = args.size or max(s for s in SIZES if s <= max(8, w - 2))
        self.n = min(self.n, max(8, w - 2))
        self.runners = []
        self.ramp = []
        self.restart()

    @property
    def algo(self):
        return ALGOS[self.ai]

    def say(self, text):
        self.message, self.message_at = text, time.monotonic()

    def put(self, y, x, text, attr=0):
        h, w = self.s.getmaxyx()
        if y < 0 or y >= h or x >= w or x < 0:
            return
        text = text[:max(0, w - x - (1 if y == h - 1 else 0))]
        try:
            self.s.addstr(y, x, text, attr)
        except curses.error:
            pass

    # -- setting up a sort --

    def race_n(self):
        h, w = self.s.getmaxyx()
        names = RACES[self.race][1]
        cols = 3 if len(names) > 4 else 2
        return max(8, min(self.n, w // cols - 3))

    def restart(self):
        if self.race is None:
            values = make_input(self.shape, self.n)
            self.runners = [Runner(self.algo, values)]
            if self.algo.limit and self.n > self.algo.limit:
                self.say(f"{self.algo.name} with {self.n} bars may never finish — try - for fewer")
        else:
            values = make_input(self.shape, self.race_n())
            self.runners = [Runner(BY_NAME[name], values) for name in RACES[self.race][1]]
        self.done_at = None
        self.places = 0

    def per_frame(self, runner):
        """Steps for this frame: auto aims at AUTO_SECONDS a sort; slow speeds step every few frames."""
        sp = SPEEDS[self.speed_i]
        if sp == "auto":
            n = len(runner.a)
            sp = max(1, estimate(runner.algo, n) / (AUTO_SECONDS * 1000 / TICK_MS))
            if self.race is not None:
                sp = max(1, estimate(BY_NAME[RACES[self.race][1][0]], n) / (AUTO_SECONDS * 1000 / TICK_MS))
        runner.credit += sp
        k = int(runner.credit)
        runner.credit -= k
        return k

    def speed_label(self):
        sp = SPEEDS[self.speed_i]
        if sp == "auto":
            return "auto"
        return f"{sp:g} steps/frame" if sp >= 1 else f"1 step every {round(1 / sp)} frames"

    # -- the clock --

    def tick(self):
        if not self.paused:
            for r in self.runners:
                if not r.done:
                    r.advance(self.per_frame(r))
                    if r.done:
                        self.places += 1
                        r.place = self.places
                elif r.sweep is not None and r.sweep < len(r.a):
                    r.sweep = min(len(r.a), r.sweep + max(1, len(r.a) // 40))
                    r.hot = {}
        if all(r.done and (r.sweep is None or r.sweep >= len(r.a)) for r in self.runners):
            if self.done_at is None:
                self.done_at = time.monotonic()
            elif self.demo and time.monotonic() - self.done_at > DEMO_PAUSE:
                if self.race is None:
                    self.ai = (self.ai + 1) % len(ALGOS)
                    while self.algo.limit and self.n > self.algo.limit:
                        self.ai = (self.ai + 1) % len(ALGOS)
                else:
                    self.race = (self.race + 1) % len(RACES)
                self.restart()

    # -- drawing --

    def draw(self):
        s = self.s
        s.erase()
        h, w = s.getmaxyx()
        if w < MIN_W or h < MIN_H:
            self.put(0, 0, f"window too small — needs {MIN_W}×{MIN_H}", curses.color_pair(1))
            s.refresh()
            return
        if self.race is None:
            self.draw_single(h, w)
        else:
            self.draw_race(h, w)
        self.draw_footer(h - 1, w)
        s.refresh()

    def bar_attr(self, r, i, v, top):
        """The colour of bar i: lit by what touched it, green once checked, or by its value."""
        if r.sweep is not None and i < r.sweep:
            return curses.color_pair(2 if r.ok else 1) | curses.A_BOLD
        kind = r.hot.get(i)
        if kind:
            return curses.color_pair(MARKS[kind]) | curses.A_BOLD
        if self.colour == "rainbow":
            return curses.color_pair(16 + min(len(self.ramp) - 1, int((v - 1) / max(1, top) * len(self.ramp))))
        return curses.color_pair(7)

    def draw_bars(self, r, y0, x0, height, width):
        vals = r.a.v
        n = len(vals)
        if not n or height < 1:
            return
        top = max(vals) or 1
        bw = max(1, width // n)
        gap = 1 if bw >= 3 else 0
        x0 += (width - bw * n) // 2
        cell = "█" * (bw - gap)
        for i, v in enumerate(vals):
            attr = self.bar_attr(r, i, v, top)
            x = x0 + i * bw
            if self.view == "dots":
                level = round(v / top * (height * 2 - 1))
                row, half = divmod(level, 2)
                self.put(y0 + height - 1 - row, x, ("▀" if half else "▄") * (bw - gap), attr)
                continue
            eighths = max(1, round(v / top * height * 8))
            full, part = divmod(eighths, 8)
            for k in range(full):
                self.put(y0 + height - 1 - k, x, cell, attr)
            if part and full < height:
                self.put(y0 + height - 1 - full, x, BLOCKS[part] * (bw - gap), attr)

    def draw_single(self, h, w):
        r = self.runners[0]
        al = r.algo
        a = r.a
        self.put(0, 1, al.name, curses.color_pair(6) | curses.A_BOLD)
        info = (f"   {al.kind} · best {al.best} · average {al.avg} · worst {al.worst}"
                f"{' · stable' if al.stable else ''}")
        self.put(0, 2 + len(al.name), info, curses.color_pair(8))
        right = f"{self.ai + 1}/{len(ALGOS)}"
        if 3 + len(al.name) + len(info) + len(right) < w:
            self.put(0, w - len(right) - 1, right, curses.color_pair(8))
        self.put(1, 1, al.desc, 0)
        self.draw_bars(r, 3, 1, h - 7, w - 2)
        stats = (f"{a.cmps:,} comparisons   {a.swaps:,} swaps   {a.writes:,} writes   {a.reads:,} reads   "
                 f"{r.steps:,} steps")
        self.put(h - 3, 1, stats, curses.color_pair(7))
        if r.done:
            sweep_done = r.sweep is not None and r.sweep >= len(a)
            if r.ok:
                state = (f"sorted ✓ in {r.steps:,} steps" if sweep_done else "checking…")
                attr = curses.color_pair(2) | curses.A_BOLD
            else:
                state, attr = "NOT sorted — that's a bug", curses.color_pair(1) | curses.A_BOLD
        elif self.paused:
            state, attr = "paused — space goes on, . steps once", curses.color_pair(3) | curses.A_BOLD
        else:
            state, attr = "sorting…", curses.color_pair(3)
        tail = f"{len(a)} bars · {self.shape} · {self.speed_label()}" + (" · demo" if self.demo else "")
        self.put(h - 2, 1, state, attr)
        self.put(h - 2, w - len(tail) - 1, tail, curses.color_pair(8))

    def draw_race(self, h, w):
        title, names = RACES[self.race]
        self.put(0, 1, f"Race: {title}", curses.color_pair(6) | curses.A_BOLD)
        tail = f"{len(self.runners[0].a)} bars · {self.shape} · {self.speed_label()} · g next race"
        self.put(0, w - len(tail) - 1, tail, curses.color_pair(8))
        cols = 3 if len(names) > 4 else 2
        rows = math.ceil(len(names) / cols)
        tw, th = (w - 1) // cols, (h - 3) // rows
        medals = {1: "1st", 2: "2nd", 3: "3rd"}
        for k, r in enumerate(self.runners):
            ty, tx = 1 + (k // cols) * th, 1 + (k % cols) * tw
            place = r.place
            label = f"{medals.get(place, f'{place}th') + ' · ' if place else ''}{r.algo.name}"
            attr = curses.A_BOLD | (curses.color_pair(2) if place == 1 else curses.color_pair(3) if place else 0)
            self.put(ty, tx, label[:tw - 2], attr)
            count = f"{r.steps:,}"
            self.put(ty, tx + tw - len(count) - 2, count, curses.color_pair(8))
            self.draw_bars(r, ty + 1, tx, th - 2, tw - 2)
        done = sum(r.done for r in self.runners)
        state = f"{done}/{len(self.runners)} finished" if done < len(self.runners) else \
            f"{self.runners[min(range(len(self.runners)), key=lambda i: self.runners[i].place)].algo.name} wins"
        self.put(h - 2, 1, state + ("   paused" if self.paused else ""), curses.color_pair(3) | curses.A_BOLD)

    def draw_footer(self, y, w):
        if self.message and time.monotonic() - self.message_at < 3:
            self.put(y, 1, self.message, curses.color_pair(3))
            return
        keys = ["␣ pause", "⏎ again", "←→ algorithm", "a list", "↑↓ speed", "+- bars", "i input",
                "v view", "c colour", "r race", "d demo", "? help", "q quit"]
        if self.race is not None:
            keys[2:4] = ["g next race"]
            keys[keys.index("r race")] = "r leave race"
        while len(keys) > 1 and len("  ".join(keys)) > w - 2:
            keys.pop(-2)
        self.put(y, 1, "  ".join(keys), curses.color_pair(8))

    # -- screens --

    def menu(self):
        h, w = self.s.getmaxyx()
        sel = self.ai
        top = 0
        self.s.timeout(-1)
        try:
            while True:
                self.draw()
                bh = min(h - 2, len(ALGOS) + 2)
                bw = min(w - 4, 78)
                y0, x0 = (h - bh) // 2, (w - bw) // 2
                body = bh - 2
                top = max(0, min(top, len(ALGOS) - body))
                if sel < top:
                    top = sel
                elif sel >= top + body:
                    top = sel - body + 1
                for y in range(y0, y0 + bh):
                    self.put(y, x0, " " * bw, curses.A_REVERSE)
                self.put(y0, x0 + 2, " choose a sort — Enter, or Esc ", curses.A_REVERSE | curses.A_BOLD)
                for n, al in enumerate(ALGOS[top:top + body]):
                    i = top + n
                    line = f" {al.name:<30} {al.kind:<13} {al.avg}"
                    attr = curses.A_BOLD if i == sel else curses.A_REVERSE
                    self.put(y0 + 1 + n, x0 + 1, line.ljust(bw - 2)[:bw - 2], attr)
                self.s.refresh()
                ch = self.s.getch()
                if ch in (curses.KEY_UP, ord("k")):
                    sel = (sel - 1) % len(ALGOS)
                elif ch in (curses.KEY_DOWN, ord("j")):
                    sel = (sel + 1) % len(ALGOS)
                elif ch == curses.KEY_PPAGE:
                    sel = max(0, sel - body)
                elif ch == curses.KEY_NPAGE:
                    sel = min(len(ALGOS) - 1, sel + body)
                elif ch in (10, 13, curses.KEY_ENTER):
                    self.ai, self.race = sel, None
                    self.restart()
                    return
                elif ch in (27, ord("q"), ord("a")):
                    return
        finally:
            self.s.timeout(TICK_MS)

    def help(self):
        lines = [
            "Sorting algorithms, animated",
            "",
            "  space      pause and go on          .   one step at a time while paused",
            "  Enter      shuffle and sort again   ←→  the previous / next algorithm (also [ ])",
            "  a          the list of all of them  ↑↓  faster / slower (auto paces a sort at ~9 s)",
            "  + -        more or fewer bars       i   the input: random, reversed, nearly sorted,",
            "                                          few unique, sorted, pipe organ, sawtooth",
            "  v          bars or dots             c   plain or rainbow colours",
            "  r          race a group side by side, g the next group; r again to leave",
            "  d          demo: one sort after another, on its own",
            "",
            "  red: compared   green: written or swapped   yellow: the pivot   blue: read",
            "  A step is one comparison or one write, so steps are a fair measure between sorts.",
            "  When a sort is done a green sweep checks that it really is in order.",
            "",
            "  any key to close",
        ]
        self.s.timeout(-1)
        try:
            self.s.erase()
            for n, line in enumerate(lines):
                self.put(1 + n, 2, line, curses.A_BOLD if n == 0 else 0)
            self.s.refresh()
            self.s.getch()
        finally:
            self.s.timeout(TICK_MS)

    # -- keys --

    def handle(self, ch):
        if ch in (ord("q"), ord("Q")):
            return False
        if ch == curses.KEY_RESIZE:
            h, w = self.s.getmaxyx()
            if self.n > w - 2:                  # the bars must fit: fewer of them, and start over
                self.n = max([x for x in SIZES if x <= w - 2] or [SIZES[0]])
                self.restart()
            elif self.race is not None and len(self.runners[0].a) > self.race_n():
                self.restart()
            return True
        if ch == ord(" "):
            self.paused = not self.paused
        elif ch == ord("."):
            self.paused = True
            for r in self.runners:
                if not r.done:
                    r.advance(1)
                    if r.done:
                        self.places += 1
                        r.place = self.places
        elif ch in (10, 13, curses.KEY_ENTER, ord("n")):
            self.restart()
            self.paused = False
        elif ch in (curses.KEY_RIGHT, ord("]"), ord("l")) and self.race is None:
            self.ai = (self.ai + 1) % len(ALGOS)
            self.restart()
        elif ch in (curses.KEY_LEFT, ord("["), ord("h")) and self.race is None:
            self.ai = (self.ai - 1) % len(ALGOS)
            self.restart()
        elif ch == ord("a"):
            self.menu()
        elif ch in (curses.KEY_UP, ord("k")):
            self.speed_i = min(len(SPEEDS) - 1, self.speed_i + 1) if self.speed_i else SPEEDS.index(16)
            self.say(f"speed: {self.speed_label()}")
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.speed_i = max(0, self.speed_i - 1) if self.speed_i != 1 else 1
            if SPEEDS[self.speed_i] == "auto":
                self.speed_i = 1
            self.say(f"speed: {self.speed_label()}")
        elif ch == ord("0"):
            self.speed_i = 0
            self.say("speed: auto")
        elif ch in (ord("+"), ord("=")):
            h, w = self.s.getmaxyx()
            bigger = [x for x in SIZES if x > self.n and x <= w - 2]
            if bigger:
                self.n = bigger[0]
                self.restart()
        elif ch in (ord("-"), ord("_")):
            smaller = [x for x in SIZES if x < self.n]
            if smaller:
                self.n = smaller[-1]
                self.restart()
        elif ch == ord("i"):
            self.shape = SHAPES[(SHAPES.index(self.shape) + 1) % len(SHAPES)]
            self.say(f"input: {self.shape}")
            self.restart()
        elif ch == ord("v"):
            self.view = "dots" if self.view == "bars" else "bars"
        elif ch == ord("c"):
            self.colour = "rainbow" if self.colour == "plain" else "plain"
        elif ch == ord("d"):
            self.demo = not self.demo
            self.say("demo: one sort after another" if self.demo else "demo off")
        elif ch == ord("r"):
            self.race = None if self.race is not None else 0
            self.restart()
        elif ch == ord("g") and self.race is not None:
            self.race = (self.race + 1) % len(RACES)
            self.restart()
        elif ch == ord("?"):
            self.help()
        return True


def run(stdscr, args):
    curses.curs_set(0)
    curses.use_default_colors()
    for n, colour in enumerate((curses.COLOR_RED, curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_BLUE,
                                curses.COLOR_MAGENTA, curses.COLOR_CYAN, curses.COLOR_WHITE), 1):
        curses.init_pair(n, colour, -1)
    curses.init_pair(8, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1)
    ramp = rainbow_pairs()
    for k, colour in enumerate(ramp):
        curses.init_pair(16 + k, colour, -1)
    stdscr.keypad(True)
    stdscr.timeout(TICK_MS)
    app = App(stdscr, args)
    app.ramp = ramp
    while True:
        started = time.monotonic()
        app.tick()
        app.draw()
        left = TICK_MS - int((time.monotonic() - started) * 1000)
        stdscr.timeout(max(1, left))
        ch = stdscr.getch()
        if ch != -1 and not app.handle(ch):
            return


def main():
    parser = argparse.ArgumentParser(description="Sorting algorithms animated as bars, like the videos.")
    parser.add_argument("algorithm", nargs="?", help="start with the first sort whose name has this in it")
    parser.add_argument("-n", "--size", type=int, choices=SIZES, help="how many bars")
    parser.add_argument("-i", "--input", dest="shape", choices=SHAPES, default="random", help="the starting order")
    parser.add_argument("-d", "--demo", action="store_true", help="play one sort after another")
    parser.add_argument("--list", action="store_true", help="list the algorithms and exit")
    args = parser.parse_args()
    if args.list:
        for al in ALGOS:
            print(f"{al.name:<30} {al.kind:<13} best {al.best:<10} average {al.avg:<16} worst {al.worst}")
        return
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(run, args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
