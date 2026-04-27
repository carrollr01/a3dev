#!/usr/bin/env python3
"""Offline self-tests for platform detection.

Run:  python test_detect.py
Exits non-zero on failure.
"""

from __future__ import annotations

import sys

from discover import detect_platforms

SAMPLES: dict[str, tuple[str, set[str], bool]] = {
    "tock_widget": (
        """
        <html><body>
          <a href="https://www.exploretock.com/cosima/reserve">Book a table</a>
          <script src="https://www.exploretock.com/tock/widget.js"></script>
        </body></html>
        """,
        {"Tock"},
        True,  # expect at least one extracted booking URL
    ),
    "sevenrooms_widget": (
        """
        <html><body>
          <iframe src="https://www.sevenrooms.com/reservations/lacolombe"></iframe>
          <div data-sr-venue="lacolombe"></div>
        </body></html>
        """,
        {"SevenRooms"},
        True,
    ),
    "resos_widget": (
        """
        <div class="resos-widget"></div>
        <script src="https://app.resos.com/widget/booking.js"></script>
        """,
        {"resOS"},
        True,
    ),
    "toast_order": (
        """
        <a href="https://order.toasttab.com/online/sample-restaurant">
          Order online
        </a>
        <script src="https://cdn.toasttab.com/static/embed.js"></script>
        """,
        {"Toast"},
        True,
    ),
    "square_appointments": (
        """
        <a href="https://squareup.com/appointments/book/abc123/joes-cafe">
          Book
        </a>
        <iframe src="https://joes-cafe.square.site/menu"></iframe>
        """,
        {"Square"},
        True,
    ),
    "multi_platform": (
        """
        <a href="https://www.exploretock.com/foo">Reserve</a>
        <a href="https://order.toasttab.com/online/foo">Order</a>
        """,
        {"Tock", "Toast"},
        True,
    ),
    "no_platform": (
        """
        <html><body>
          <a href="/contact">Contact us</a>
          <script src="https://www.googletagmanager.com/gtag/js"></script>
        </body></html>
        """,
        set(),
        False,
    ),
    "false_positive_guard_squarespace": (
        # Squarespace mentions "square" in places but should NOT trigger Square.
        """
        <meta name="generator" content="Squarespace">
        <link href="https://static1.squarespace.com/static/a.css">
        """,
        set(),
        False,
    ),
}


def main() -> int:
    failures: list[str] = []
    for name, (html, expected, expect_url) in SAMPLES.items():
        platforms, urls = detect_platforms(html)
        got = set(platforms)
        if got != expected:
            failures.append(
                f"{name}: expected {expected!r}, got {got!r}"
            )
            continue
        if expect_url and not urls and expected:
            failures.append(
                f"{name}: expected at least one booking URL, got none"
            )
            continue
        print(f"PASS  {name:<40}  -> {sorted(got) or '(none)'}")
    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print(f"\nAll {len(SAMPLES)} cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
