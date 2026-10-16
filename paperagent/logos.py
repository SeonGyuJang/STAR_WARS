"""Company logos for TECH posts: when a project or piece of news comes from a
known company (Meta, Apple, Google, NVIDIA, ...), its official mark is shown on
the cards and the dashboard.

The marks are the single-colour brand icons of Simple Icons (CC0), bundled in
static/logos/brands.json. They only identify who made a project; the
trademarks belong to their owners. Companies whose marks Simple Icons does not
carry (e.g. OpenAI, Microsoft) are shown by name, never with a stand-in logo.

Who made it comes first from the source itself (the GitHub organisation, the
Hugging Face namespace), then from the writer's `maker` field, which is only
filled when the material says so.
"""
import json
import re
from functools import lru_cache
from pathlib import Path

from markupsafe import Markup

BRANDS_FILE = Path(__file__).resolve().parent / "static" / "logos" / "brands.json"

# GitHub organisations / Hugging Face namespaces -> company
ORG_MAKERS = {
    "facebookresearch": "Meta", "facebook": "Meta", "meta-llama": "Meta", "metaresearch": "Meta", "instagram": "Instagram",
    "google": "Google", "google-research": "Google", "google-deepmind": "Google DeepMind", "googleapis": "Google",
    "google-gemini": "Google", "googlecloudplatform": "Google Cloud", "tensorflow": "Google",
    "apple": "Apple", "ml-explore": "Apple",
    "nvidia": "NVIDIA", "nvlabs": "NVIDIA", "nvidia-ai-blueprints": "NVIDIA",
    "anthropics": "Anthropic", "openai": "OpenAI", "microsoft": "Microsoft", "azure": "Microsoft", "azure-samples": "Microsoft",
    "huggingface": "Hugging Face", "mistralai": "Mistral AI", "deepseek-ai": "DeepSeek", "qwenlm": "Alibaba Cloud",
    "qwen": "Alibaba Cloud", "alibaba": "Alibaba Cloud", "bytedance": "ByteDance", "bytedance-seed": "ByteDance",
    "tencent": "Tencent", "baidu": "Baidu", "paddlepaddle": "Baidu", "naver": "NAVER", "naver-ai": "NAVER", "kakao": "Kakao",
    "kakaobrain": "Kakao", "samsung": "Samsung", "intel": "Intel", "amd": "AMD", "rocm": "AMD", "ibm": "IBM",
    "ibm-granite": "IBM", "cloudflare": "Cloudflare", "vercel": "Vercel", "docker": "Docker", "stripe": "Stripe",
    "netflix": "Netflix", "spotify": "Spotify", "uber": "Uber", "airbnb": "Airbnb", "shopify": "Shopify",
    "databricks": "Databricks", "snowflakedb": "Snowflake", "supabase": "Supabase", "ollama": "Ollama",
    "langchain-ai": "LangChain", "pytorch": "PyTorch", "jetbrains": "JetBrains", "github": "GitHub", "mozilla": "Mozilla",
    "elevenlabs": "ElevenLabs", "perplexityai": "Perplexity", "x-ai": "xAI", "xai-org": "xAI", "tesla": "Tesla",
}

# names people use for a company -> the Simple Icons slug
NAME_ALIASES = {
    "google deepmind": "google", "deepmind": "google", "google research": "google", "google cloud": "googlecloud",
    "gemini": "googlegemini", "meta ai": "meta", "facebook ai": "meta", "fair": "meta", "llama": "meta",
    "alibaba": "alibabacloud", "alibaba cloud": "alibabacloud", "qwen": "alibabacloud", "x": "x", "twitter": "x",
    "kakao brain": "kakao", "naver cloud": "naver", "aws": None, "amazon": None,
}


@lru_cache(maxsize=1)
def _brands():
    try:
        return json.loads(BRANDS_FILE.read_text(encoding="utf-8"))["icons"]
    except (OSError, ValueError, KeyError):
        return {}


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower().replace("+", "plus").replace(".", "dot").replace("&", "and"))


@lru_cache(maxsize=512)
def find(name: str):
    """{slug, title, hex, path} for a company name, or None when there is no official mark."""
    if not name:
        return None
    low = name.strip().lower()
    if low in NAME_ALIASES:
        slug = NAME_ALIASES[low]
        return {"slug": slug, **_brands()[slug]} if slug and slug in _brands() else None
    brands = _brands()
    k = _key(name)
    if k in brands:
        return {"slug": k, **brands[k]}
    for slug, b in brands.items():
        if _key(b["title"]) == k or k in (_key(a) for a in b.get("aliases", [])):
            return {"slug": slug, **b}
    return None


def maker_for(item: dict, review: dict | None = None) -> dict | None:
    """{name, logo} for whoever made a TECH item, or None if unknown.
    logo is None for companies without an official mark in the set."""
    name = None
    src, title = item.get("source"), item.get("title") or ""
    if src in ("github", "hf") and "/" in title:
        name = ORG_MAKERS.get(title.split("/")[0].lower())
    if not name:  # news that points at a repository or model (GeekNews, Hacker News)
        for link in (item.get("url") or "", (item.get("meta") or {}).get("repo") or ""):
            m = re.match(r"^https?://(?:www\.)?(?:github\.com|huggingface\.co)/(?:spaces/)?([\w.-]+)/", link)
            name = ORG_MAKERS.get(m.group(1).lower()) if m else None
            if name:
                break
    if not name:
        name = ((review or {}).get("maker") or "").strip() or None
    if not name:
        return None
    return {"name": name, "logo": find(name)}


def _ink(hex_color: str) -> str:
    """The brand colour, or near-black when it would be too light on white (e.g. Snapchat yellow)."""
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (0, 2, 4))
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return f"#{hex_color}" if lum < 0.72 else "#111111"


def svg(logo: dict, size: int = 40, color: str | None = None) -> Markup:
    if not logo:
        return Markup("")
    fill = color or _ink(logo["hex"])
    return Markup(f'<svg class="logo-mark" width="{size}" height="{size}" viewBox="0 0 24 24" role="img" aria-label="{logo["title"]}">'
                  f'<path fill="{fill}" d="{logo["path"]}"/></svg>')
