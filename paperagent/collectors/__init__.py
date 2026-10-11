"""Shared HTTP helper and keyword matching for the collectors."""
import re
import time

import requests

from .. import config

_session = requests.Session()
_session.headers["User-Agent"] = config.HTTP_USER_AGENT


def http_get(url, params=None, retries=3, timeout=60, **kwargs):
    """GET with simple exponential backoff on network errors, 429 and 5xx."""
    last_exc = None
    for attempt in range(retries):
        try:
            resp = _session.get(url, params=params, timeout=timeout, **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"{resp.status_code} from {url}", response=resp)
            resp.raise_for_status()
            return resp
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            last_exc = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status and 400 <= status < 500 and status != 429:
                raise
            time.sleep(3 * (2 ** attempt))
    raise last_exc


def http_post(url, json=None, params=None, timeout=60, **kwargs):
    resp = _session.post(url, json=json, params=params, timeout=timeout, **kwargs)
    resp.raise_for_status()
    return resp


def _keyword_pattern(term: str):
    words = [re.escape(w) for w in re.split(r"[\s\-]+", term.strip().lower()) if w]
    # allow plural/suffix variants ("agent" -> "agents", "model" -> "models")
    # and hyphen/space variants ("retrieval-augmented" vs "retrieval augmented")
    return re.compile(r"\b" + r"[\s\-]+".join(w + r"\w{0,3}" for w in words) + r"\b", re.IGNORECASE)


def match_keywords(text: str, keywords):
    text = text or ""
    return [kw for kw in keywords if _keyword_pattern(kw).search(text)]
