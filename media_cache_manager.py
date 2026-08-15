import logging
import os
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)


class MediaCacheManager:
    def __init__(self, cache_dir: str = "media_cache", http_client: Optional[httpx.AsyncClient] = None, base_url: Optional[str] = None):
        self.cache_dir = Path(cache_dir)
        # base_url is used to resolve relative attachment_url values
        # (e.g. /campaign-attachments/file.png) coming from the orchestrator.
        self.base_url = (base_url or os.getenv("SEAWEEDFS_FILER_URL") or "http://localhost:8888").rstrip("/") + "/"
        self.http_client = http_client or httpx.AsyncClient(timeout=60.0, follow_redirects=True)

    def _resolve_url(self, attachment_url: str) -> str:
        """Resolve a possibly-relative URL against the configured base URL.

        Examples:
          _resolve_url("http://x:8888/foo.png")  -> "http://x:8888/foo.png"
          _resolve_url("/foo.png")                -> "<base>/foo.png"
          _resolve_url("foo.png")                 -> "<base>/foo.png"
        """
        parsed = urlparse(attachment_url)
        if parsed.scheme in ("http", "https"):
            return attachment_url
        # Relative path: join with base, preserving any leading slash.
        rel = attachment_url if attachment_url.startswith("/") else "/" + attachment_url
        return urljoin(self.base_url, rel)

    async def ensure_campaign_media(
        self,
        campaign_id: str,
        attachment_url: Optional[str],
        attachment_name: Optional[str] = None,
    ) -> Optional[str]:
        if not attachment_url:
            return None

        self.cache_dir.mkdir(parents=True, exist_ok=True)

        ext = ""
        if attachment_name:
            ext = Path(attachment_name).suffix
        if not ext:
            ext = Path(urlparse(attachment_url).path).suffix
        if not ext:
            ext = ".bin"

        local_path = self.cache_dir / f"{campaign_id}{ext}"
        if local_path.exists():
            logger.info(f"[media-cache] hit cache: {local_path}")
            return str(local_path)

        tmp_path = self.cache_dir / f".{campaign_id}{ext}.part"
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass

        resolved_url = self._resolve_url(attachment_url)
        # If the URL points to the SeaweedFS S3 API port (8333) but our base
        # is the filer (8888), the S3 endpoint may reject anonymous GETs.
        # In that case, fall back to the filer on the same host (different port).
        fallback_urls = []
        if "localhost:8333" in resolved_url or ":8333/" in resolved_url:
            fallback_urls.append(resolved_url.replace(":8333", ":8888").replace("/campaign-attachments/", "/campaign-attachments/"))
        logger.info(
            f"[media-cache] downloading raw_url={attachment_url!r} resolved_url={resolved_url!r} -> {local_path} fallbacks={fallback_urls}"
        )
        urls_to_try = [resolved_url] + fallback_urls
        last_exc = None
        for attempt_idx, url in enumerate(urls_to_try):
            try:
                async with self.http_client.stream("GET", url) as resp:
                    if resp.status_code >= 400:
                        body_preview = ""
                        try:
                            body_preview = (await resp.aread())[:512].decode("utf-8", errors="replace")
                        except Exception:
                            pass
                        logger.error(
                            f"[media-cache] download failed (attempt {attempt_idx + 1}/{len(urls_to_try)}): status={resp.status_code} url={url} body={body_preview!r}"
                        )
                        if attempt_idx < len(urls_to_try) - 1:
                            # Try the fallback URL next.
                            continue
                        resp.raise_for_status()
                    with open(tmp_path, "wb") as f:
                        async for chunk in resp.aiter_bytes():
                            if chunk:
                                f.write(chunk)
                os.replace(tmp_path, local_path)
                logger.info(f"[media-cache] successfully cached media to {local_path} (from {url})")
                return str(local_path)
            except httpx.HTTPError as e:
                logger.exception(f"[media-cache] HTTP error downloading {url}: {e}")
                last_exc = e
                if attempt_idx < len(urls_to_try) - 1:
                    continue
                raise
        # Should not reach here, but just in case:
        if last_exc:
            raise last_exc
        return None
