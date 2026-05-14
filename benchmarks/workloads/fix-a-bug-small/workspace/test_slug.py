"""Tests for slug.slugify. Two of these currently fail because of the
documented leading/trailing-hyphen bug."""

from slug import slugify


def test_basic():
    assert slugify("Hello World") == "hello-world"


def test_strips_punctuation():
    # Currently FAILS: returns '-hello-world-'.
    assert slugify("  Hello, World!  ") == "hello-world"


def test_strips_repeated_separators():
    # Currently FAILS: returns '-weird-input-'.
    assert slugify("---weird-input---") == "weird-input"


def test_empty_input_yields_empty_slug():
    assert slugify("!!!") == ""
