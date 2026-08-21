"""DASH Network social-content pipeline.

DASH data → social content object → caption generation → asset →
approval queue → publish → history.

This package is deliberately generic across DASH products (Moonshot/MLB
today, Tuddy/NFL and whatever comes after). Nothing in here is MLB-specific
— sport-specific logic lives in a per-product builder (see
bots/social/night_recap.py for the first one) that hands this package a
plain content object built from ALREADY-PUBLISHED data files. This package
never fetches player stats, odds or grades itself and never invents a
number Claude wasn't given.

Read docs/SOCIAL.md before touching this package.
"""
