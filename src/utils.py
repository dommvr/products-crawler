"""
utils.py — shared helpers: logging setup, domain utilities, skip-list.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import colorlog


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    handler = colorlog.StreamHandler()
    handler.setFormatter(colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s [%(levelname)s]%(reset)s %(message)s",
        datefmt="%H:%M:%S",
        log_colors={
            "DEBUG": "cyan",
            "INFO": "green",
            "WARNING": "yellow",
            "ERROR": "red",
            "CRITICAL": "bold_red",
        },
    ))
    logger = logging.getLogger("crawler")
    logger.setLevel(level)
    logger.handlers.clear()
    logger.addHandler(handler)
    return logger


logger = setup_logging()


def get_root_domain(url: str) -> str:
    """Return registrable domain from a URL, e.g. 'https://sklep.jysk.pl/x' → 'jysk.pl'."""
    try:
        host = urlparse(url).netloc or url
        host = host.lower().lstrip("www.")
        # Strip port
        host = host.split(":")[0]
        # Take last two parts (handles .co.uk etc. only roughly — sufficient here)
        parts = host.split(".")
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return host
    except Exception:
        return url


def get_full_domain(url: str) -> str:
    """Return full hostname, e.g. 'https://sklep.jysk.pl/x' → 'sklep.jysk.pl'."""
    try:
        return urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return ""


def is_skippable(url: str, skip_domains: list[str]) -> bool:
    """Return True if the URL belongs to a domain in the skip list."""
    root = get_root_domain(url)
    full = get_full_domain(url)
    for blocked in skip_domains:
        blocked = blocked.lower()
        if root == blocked or full == blocked or full.endswith("." + blocked):
            return True
    return False


def looks_like_url(text: str) -> bool:
    """True if text looks like a bare domain or full URL (not a company name)."""
    text = text.strip()
    if text.startswith(("http://", "https://")):
        return True
    # bare domain like jysk.pl or www.jysk.pl
    domain_re = re.compile(
        r"^(www\.)?[a-zA-Z0-9-]+\.[a-zA-Z]{2,}(/.*)?$"
    )
    return bool(domain_re.match(text))


def normalise_url(text: str) -> str:
    """Ensure URL has a scheme."""
    text = text.strip()
    if text.startswith(("http://", "https://")):
        return text
    return "https://" + text


def same_site(url: str, base_domain: str, follow_subdomains: bool) -> bool:
    """
    Return True if url belongs to the same site as base_domain.
    base_domain should be the root domain (e.g. 'jysk.pl').
    If follow_subdomains=True, sklep.jysk.pl counts as same site.
    """
    root = get_root_domain(url)
    if follow_subdomains:
        return root == base_domain
    return get_full_domain(url) == base_domain


def read_names_file(path: str) -> list[str]:
    """Read names.txt, skip blank lines and comments."""
    entries: list[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                entries.append(line)
    return entries
