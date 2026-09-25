"""MCP extension: visual lookup of EPLAN electrical symbols.

Drops into any EPLAN_MCP_EXTENSIONS directory. Registers
`eplan_symbol_search`: given an image (path or base64), embeds it with CLIP
ViT-B/32 locally and queries the `eplan-symbols` Vectorize index for the
nearest known EPLAN symbols, returning their short_name / description /
variant so the agent can use the real identifier in EPLAN API calls.

Runtime env:
  CF_ACCOUNT_ID, CF_API_TOKEN, VECTORIZE_INDEX (default "eplan-symbols")

Deps (lazy): pip install sentence-transformers pillow requests
"""

import base64
import io
import json
import os

import requests

__all__ = ["symbol_search"]
TOOL_PREFIX = "eplan_"

_ACCOUNT = os.environ.get("CF_ACCOUNT_ID", "")
_TOKEN = os.environ.get("CF_API_TOKEN", "")
_INDEX = os.environ.get("VECTORIZE_INDEX", "eplan-symbols")

_model = None


def _embedder():
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise RuntimeError(
                "pip install sentence-transformers pillow is required for "
                "symbol_search")
        _model = SentenceTransformer("clip-ViT-B-32")
    return _model


def _load_image(image_path: str = "", image_base64: str = ""):
    from PIL import Image
    if image_path:
        return Image.open(image_path).convert("RGB")
    if image_base64:
        return Image.open(io.BytesIO(base64.b64decode(image_base64))).convert("RGB")
    raise ValueError("pass image_path or image_base64")


def symbol_search(image_path: str = "", image_base64: str = "",
                  top_k: int = 5) -> str:
    """Identify an EPLAN electrical symbol from an image.

    Pass a cropped image of the symbol (local file path or base64 PNG/JPEG).
    Returns the closest known EPLAN symbols with their short_name, catalog
    number, description and similarity score — use the top short_name as the
    symbol identifier in EPLAN API calls (e.g. SymbolIntermingle /
    Function creation with that symbol name).

    Args:
        image_path: local filesystem path to the symbol image.
        image_base64: alternatively, base64-encoded image content.
        top_k: how many candidate symbols to return (default 5).
    """
    if not _ACCOUNT or not _TOKEN:
        return ("symbol_search is not configured: set CF_ACCOUNT_ID and "
                "CF_API_TOKEN in the MCP server environment.")
    img = _load_image(image_path, image_base64)
    vec = _embedder().encode(img, normalize_embeddings=True).tolist()

    r = requests.post(
        f"https://api.cloudflare.com/client/v4/accounts/{_ACCOUNT}"
        f"/vectorize/v2/indexes/{_INDEX}/query",
        headers={"Authorization": f"Bearer {_TOKEN}",
                 "Content-Type": "application/json"},
        json={"vector": vec, "topK": int(top_k),
              "returnValues": False, "returnMetadata": "all"},
        timeout=30)
    if not r.ok:
        return f"Vectorize query failed: HTTP {r.status_code} {r.text[:200]}"
    matches = (r.json().get("result") or {}).get("matches") or []
    if not matches:
        return "No similar symbols found in the index."
    lines = [f"{i+1}. `{m['metadata'].get('short_name','?')}` "
             f"(no. {m['metadata'].get('number','?')}, "
             f"variant {m['metadata'].get('variant_id','?')}) — "
             f"{m['metadata'].get('description','')} "
             f"[score {m.get('score', 0):.3f}]"
             for i, m in enumerate(matches)]
    return ("Closest EPLAN symbols for the given image:\n" + "\n".join(lines)
            + "\n\nUse the top `short_name` as the symbol identifier. "
              "If the top score is low (<0.85), the crop may be noisy — "
              "re-crop tighter or check the candidates visually.")
