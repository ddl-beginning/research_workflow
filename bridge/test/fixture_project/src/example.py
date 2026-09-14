"""Small fixture implementation selected for a bounded review."""


def remove_low_confidence(mask, radius=2):
    """Erode uncertain pixels before extracting a boundary."""
    return binary_erosion(mask, radius=radius)


def extract_boundary(mask):
    """Return the visible boundary of the cleaned mask."""
    cleaned = remove_low_confidence(mask)
    return find_contour(cleaned)
