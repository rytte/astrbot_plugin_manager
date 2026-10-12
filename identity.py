"""Compare declared authors without rewriting metadata or plugin data identifiers."""

from __future__ import annotations


def authors_match(local_author: object, source_author: object, *, market: bool) -> bool:
    """Match comma-separated authors, allowing market entries to omit coauthors.

    Args:
        local_author: Full author declaration from local or package metadata.
        source_author: Author declaration supplied by the source.
        market: Whether the source is a market entry rather than repository metadata.

    Returns:
        Whether both declarations are valid and the source authors are a subset
        for markets, or the complete author sets are equal for repositories.
    """
    if not isinstance(local_author, str) or not isinstance(source_author, str):
        return False
    local = {part.strip() for part in local_author.split(",")}
    source = {part.strip() for part in source_author.split(",")}
    if "" in local or "" in source:
        return False
    return source <= local if market else source == local
