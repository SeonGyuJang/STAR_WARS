"""Shared HTTP helper and keyword matching for the collectors."""
import re
import time
from urllib.parse import urlparse

import requests

from .. import config

_session = requests.Session()
_session.headers["User-Agent"] = config.HTTP_USER_AGENT


def _request(method, url, retries=3, timeout=60, **kwargs):
    """Request with simple exponential backoff on network errors, 429 and 5xx."""
    last_exc = None
    for attempt in range(retries):
        try:
            resp = _session.request(method, url, timeout=timeout, **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"{resp.status_code} from {url}", response=resp)
            resp.raise_for_status()
            return resp
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            last_exc = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status and 400 <= status < 500 and status != 429:
                raise
            if attempt + 1 < retries:
                time.sleep(3 * (2 ** attempt))
    raise last_exc


def short_error(exc) -> str:
    """A network failure in a few words for the run log ('HTTP 429 (export.arxiv.org)',
    'could not connect (api.openalex.org)'), instead of a URL-long exception."""
    resp = getattr(exc, "response", None)
    req = getattr(exc, "request", None)
    url = getattr(resp, "url", None) or getattr(req, "url", None) or ""
    host = urlparse(url).netloc if isinstance(url, str) else ""
    where = f" ({host})" if host else ""
    status = getattr(resp, "status_code", None)
    if isinstance(status, int):
        return f"HTTP {status}{where}"
    if isinstance(exc, requests.Timeout):
        return f"timed out{where}"
    if isinstance(exc, requests.ConnectionError):
        return f"could not connect{where}"
    text = re.sub(r"https?://\S+", lambda m: urlparse(m.group(0)).netloc, str(exc))
    return text[:200]


def http_get(url, params=None, retries=3, timeout=60, **kwargs):
    return _request("GET", url, retries=retries, timeout=timeout, params=params, **kwargs)


def http_post(url, json=None, params=None, retries=3, timeout=60, **kwargs):
    return _request("POST", url, retries=retries, timeout=timeout, json=json, params=params, **kwargs)


def _keyword_pattern(term: str):
    words = [re.escape(w) for w in re.split(r"[\s\-]+", term.strip().lower()) if w]
    # allow plural/suffix variants ("agent" -> "agents", "model" -> "models")
    # and hyphen/space variants ("retrieval-augmented" vs "retrieval augmented")
    return re.compile(r"\b" + r"[\s\-]+".join(w + r"\w{0,3}" for w in words) + r"\b", re.IGNORECASE)


def match_keywords(text: str, keywords):
    text = text or ""
    return [kw for kw in keywords if _keyword_pattern(kw).search(text)]
