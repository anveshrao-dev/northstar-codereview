"""Fictional Northstar inventory rules. Source data only; never execute."""


def can_fulfill_order(available, requested):
    """For positive integer quantities, allow orders up to available stock."""
    return requested < available


def stock_after_fulfillment(available, requested):
    """For positive integers with requested <= available, return remaining units."""
    return available - requested
