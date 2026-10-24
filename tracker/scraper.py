"""Scraper de Falabella Perú (Next.js · datos en `__NEXT_DATA__`).

Estrategia de extracción:
  1. HTTP con `curl_cffi` imitando a Chrome (rápido, ~1–2 s por página).
  2. Si la tienda bloquea (403 o HTML sin datos), cambia automáticamente a un
     Chrome real (DrissionPage) que valida la sesión una vez y luego pide las
     demás páginas desde dentro del navegador.

Filtro de tienda oficial en dos capas:
  1. Servidor: `f.derived.variant.sellerId=FALABELLA::TOTTUS`.
  2. Cliente: `sellerId` debe estar en `allowed_seller_ids` (FALABELLA_PERU, TOTTUS_PERU).

Precios: el % se calcula con el precio sin tarjeta (internet/evento) contra el
precio normal tachado. El precio CMR se muestra aparte, salvo que
`use_card_price_for_discount: true`.

La web no permite ordenar por descuento, así que en cada corrida se leen las
primeras `fixed_pages` de cada fuente más `rotating_pages` que van avanzando
corrida a corrida (cursor guardado en el historial) hasta cubrir el catálogo.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Iterator
from urllib.parse import urlencode

from .base import BaseScraper
from .config import AppConfig, ConfigError
from .http_client import BlockedError, HttpClient, HttpError
from .models import Offer
from .pricing import parse_price

log = logging.getLogger(__name__)

NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)
COLLECTION_RE = re.compile(r"/falabella-pe/collection/([A-Za-z0-9\-]+)")
CARD_TYPES = {"cmrPrice"}


@dataclass(frozen=True)
class Source:
    key: str
    url: str
    group: str
    min_discount: float | None = None
    rotate: bool = True


def _find_results(nodo: Any, profundidad: int = 0) -> list[dict]:
    """Búsqueda tolerante de la lista de productos por si cambia la estructura."""
    if profundidad > 8:
        return []
    if isinstance(nodo, list):
        validos = [x for x in nodo if isinstance(x, dict) and "prices" in x and ("productId" in x or "skuId" in x)]
        if validos:
            return validos
        for elemento in nodo:
            hallado = _find_results(elemento, profundidad + 1)
            if hallado:
                return hallado
    elif isinstance(nodo, dict):
        for llave in ("results", "products", "pageProps", "props", "data"):
            if llave in nodo:
                hallado = _find_results(nodo[llave], profundidad + 1)
                if hallado:
                    return hallado
    return []


def parse_listing(next_data: dict) -> dict | None:
    """De `__NEXT_DATA__` (o del subconjunto que devuelve el navegador) a {results, pagination}."""
    pp = (next_data.get("props") or {}).get("pageProps") if "props" in next_data else next_data
    if not isinstance(pp, dict):
        return None
    resultados = pp.get("results")
    if not isinstance(resultados, list):
        resultados = _find_results(next_data)
    if resultados is None:
        return None
    return {"results": resultados, "pagination": pp.get("pagination") or {}}


def extract_next_data(html: str) -> dict | None:
    if not html:
        return None
    match = NEXT_DATA_RE.search(html)
    if not match:
        return None
    try:
        return parse_listing(json.loads(match.group(1)))
    except (ValueError, AttributeError):
        return None


class FalabellaScraper(BaseScraper):
    store_id = "falabella"

    def __init__(self, cfg: AppConfig, meta: dict[str, Any], http: HttpClient | None = None,
                 browser_factory=None) -> None:
        super().__init__(cfg, meta)
        sc = cfg.scraper
        try:
            self.seller_filter = "::".join(str(s) for s in sc["seller_facets"])
            self.allowed_seller_ids = frozenset(str(s).upper() for s in sc["allowed_seller_ids"])
            fuentes = sc["sources"]
        except (KeyError, TypeError) as exc:
            raise ConfigError(f"scraper: falta la clave {exc}") from exc
        self.site_url = str(sc.get("site_url", "https://www.falabella.com.pe")).rstrip("/")
        self.use_card_price = bool(sc.get("use_card_price_for_discount", False))
        self.fixed_pages = max(1, int(sc.get("fixed_pages", 2)))
        self.rotating_pages = max(0, int(sc.get("rotating_pages", 1)))
        self.max_page = int(sc.get("max_page", 30))
        self.browser_fallback = bool(sc.get("browser_fallback", True))
        self.browser_timeout = float(sc.get("browser_timeout_s", 30))
        self.discover = dict(sc.get("discover_collections") or {})
        self.sources = [
            Source(
                key=str(s["key"]),
                url="/" + str(s["url"]).lstrip("/"),
                group=str(s.get("group") or "General"),
                min_discount=float(s["min_discount"]) if s.get("min_discount") is not None else None,
                rotate=bool(s.get("rotate", True)),
            )
            for s in fuentes
        ]
        self.http = http or HttpClient(cfg.http, backend="curl_cffi", impersonate=str(sc.get("impersonate", "chrome")))
        self._browser_factory = browser_factory
        self._browser = None
        self.stats.mode = "http"

    # ------------------------------------------------------------- obtención
    def _page_url(self, path: str, page: int) -> str:
        params = {"facetSelected": "true", "f.derived.variant.sellerId": self.seller_filter}
        if page > 1:
            params["page"] = str(page)
        return f"{self.site_url}{path}?{urlencode(params)}"

    def _get_browser(self):
        if self._browser is None:
            if self._browser_factory is not None:
                self._browser = self._browser_factory()
            else:
                from .browser import BrowserFetcher

                self._browser = BrowserFetcher(timeout_s=self.browser_timeout)
        return self._browser

    def fetch_listing(self, url: str) -> dict | None:
        if self.stats.mode == "http":
            try:
                datos = extract_next_data(self.http.get(url, expect="text"))
                if datos is not None:
                    return datos
                raise BlockedError("HTML sin __NEXT_DATA__ (posible página de bloqueo)")
            except BlockedError as exc:
                if not self.browser_fallback:
                    raise
                log.warning("HTTP bloqueado (%s). Cambiando a navegador real para el resto de la corrida.", exc)
                self.stats.mode = "browser"
        return self._get_browser().fetch_listing(url)

    def fetch_image(self, offer: Offer) -> bytes | None:
        """Descarga la imagen (con la sesión de la tienda) y la entrega en JPEG."""
        candidatas = [offer.image_url]
        if offer.image_url.endswith("/public"):
            candidatas.insert(0, offer.image_url[: -len("/public")] + "/w=800,h=800,f=jpeg")
        for url in candidatas:
            try:
                contenido = self.http.get(
                    url, expect="bytes", polite=False,
                    headers={"Accept": "image/jpeg,image/png,image/*;q=0.8", "Referer": f"{self.site_url}/"},
                )
            except HttpError:
                continue
            if not contenido or len(contenido) < 800:
                continue
            if contenido[:3] == b"\xff\xd8\xff" or contenido[:8] == b"\x89PNG\r\n\x1a\n":
                return contenido
            try:  # WebP/AVIF → JPEG
                from PIL import Image

                with Image.open(BytesIO(contenido)) as img:
                    buffer = BytesIO()
                    img.convert("RGB").save(buffer, format="JPEG", quality=88)
                    return buffer.getvalue()
            except Exception:
                continue
        return None

    # -------------------------------------------------------------- parseo
    def parse_result(self, r: dict, src: Source) -> Offer | None:
        seller_id = str(r.get("sellerId") or "").upper()
        if seller_id not in self.allowed_seller_ids:
            return None
        normal = oferta = tarjeta = 0.0
        for p in r.get("prices") or []:
            if not isinstance(p, dict):
                continue
            valor = parse_price(p.get("price"))
            if valor <= 0:
                continue
            tipo = str(p.get("type") or "")
            if p.get("crossed") or tipo == "normalPrice":
                normal = max(normal, valor)
            elif tipo in CARD_TYPES:
                tarjeta = valor if not tarjeta else min(tarjeta, valor)
            else:  # internetPrice, eventPrice, etc.
                oferta = valor if not oferta else min(oferta, valor)
        if not oferta:
            oferta = normal  # solo hay precio normal (o normal + CMR): sin descuento sin tarjeta
        if not oferta:
            return None
        lista = normal if normal > oferta else oferta

        precio, precio_tarjeta, nota = oferta, (tarjeta or None), ""
        if self.use_card_price and tarjeta and tarjeta < oferta:
            precio, precio_tarjeta, nota = tarjeta, None, "Precio exclusivo con tarjeta CMR"

        url = str(r.get("url") or "").split("?")[0]
        product_id = str(r.get("productId") or r.get("skuId") or "").strip()
        if not product_id:
            return None
        if not url:
            url = f"/falabella-pe/product/{product_id}"
        if not url.startswith("http"):
            url = f"{self.site_url}{url}"

        imagenes = r.get("mediaUrls") or []
        if isinstance(imagenes, str):
            imagenes = [imagenes]
        imagen = str(imagenes[0]).strip() if imagenes else ""
        if imagen and not imagen.startswith("http"):
            imagen = f"https://media.falabella.com.pe/falabellaPE/{imagen.lstrip('/')}/public"

        return Offer(
            store=self.store_id,
            product_id=product_id,
            name=str(r.get("displayName") or "Producto sin nombre").strip(),
            brand=str(r.get("brand") or "").strip(),
            seller_id=seller_id,
            seller_name=str(r.get("sellerName") or seller_id).strip(),
            list_price=lista,
            price=precio,
            url=url,
            image_url=imagen,
            category=src.key,
            group=src.group,
            card_price=precio_tarjeta,
            min_discount=src.min_discount,
            note=nota,
        )

    # ------------------------------------------------------------- paginado
    def pages_for(self, src: Source, total_pages: int) -> list[int]:
        """Páginas a leer tras la 1: fijas + ventana rotativa persistida en meta['cursors']."""
        fijas = list(range(2, min(self.fixed_pages, total_pages) + 1))
        tope = min(total_pages, self.max_page)
        if not src.rotate or self.rotating_pages == 0 or tope <= self.fixed_pages:
            return fijas
        cursores = self.meta.setdefault("cursors", {})
        inicio = self.fixed_pages + 1
        pagina = int(cursores.get(src.key, inicio))
        rotativas: list[int] = []
        for _ in range(self.rotating_pages):
            if pagina > tope or pagina < inicio:
                pagina = inicio
            if pagina not in rotativas:
                rotativas.append(pagina)
            pagina += 1
        cursores[src.key] = pagina
        self.meta_changed = True
        return fijas + rotativas

    def discover_sources(self) -> list[Source]:
        """Detecta colecciones de promoción enlazadas desde la portada (Cyber, liquidaciones…)."""
        if not self.discover.get("enabled"):
            return []
        try:
            html = self.http.get(f"{self.site_url}/falabella-pe", expect="text")
        except HttpError as exc:
            log.info("No se pudo leer la portada para detectar colecciones: %s", exc)
            return []
        patron = re.compile(str(self.discover.get("pattern") or "liquid|descuent|cyber|black|flash|oferta"), re.I)
        ya_configuradas = {s.url.rstrip("/").rsplit("/", 1)[-1] for s in self.sources}
        nuevas: list[Source] = []
        for slug in dict.fromkeys(COLLECTION_RE.findall(html)):
            if slug in ya_configuradas or not patron.search(slug):
                continue
            nuevas.append(Source(key=slug.replace("-", " ").title().replace(" ", ""),
                                 url=f"/falabella-pe/collection/{slug}",
                                 group=str(self.discover.get("group") or "Promos"), rotate=False))
            if len(nuevas) >= int(self.discover.get("max", 4)):
                break
        if nuevas:
            log.info("Colecciones detectadas: %s", ", ".join(s.key for s in nuevas))
        return nuevas

    def _scrape_page(self, src: Source, page: int) -> tuple[list[dict], dict] | None:
        url = self._page_url(src.url, page)
        try:
            datos = self.fetch_listing(url)
        except (HttpError, RuntimeError) as exc:
            self.stats.fail(f"{src.key} pág {page}: {exc}")
            log.warning("[%s] pág %s: %s", src.key, page, exc)
            return None
        if datos is None:
            self.stats.fail(f"{src.key} pág {page}: sin datos")
            return None
        self.stats.pages_ok += 1
        return datos.get("results") or [], datos.get("pagination") or {}

    # ------------------------------------------------------------------ API
    def iter_offers(self) -> Iterator[Offer]:
        fuentes = self.sources + (self.discover_sources() if self.stats.mode == "http" else [])
        for src in fuentes:
            primera = self._scrape_page(src, 1)
            if primera is None:
                continue
            resultados, paginacion = primera
            por_pagina = int(paginacion.get("perPage") or 48) or 48
            total_paginas = max(1, math.ceil(int(paginacion.get("count") or 0) / por_pagina))
            paginas = [(1, resultados)]
            for numero in self.pages_for(src, total_paginas):
                pagina = self._scrape_page(src, numero)
                if pagina is not None:
                    paginas.append((numero, pagina[0]))
            for numero, lista in paginas:
                oficiales = 0
                for r in lista:
                    self.stats.products_seen += 1
                    offer = self.parse_result(r, src)
                    if offer is not None:
                        oficiales += 1
                        yield offer
                log.info("[%s] pág %s/%s: %s productos | %s oficiales", src.key, numero, total_paginas,
                         len(lista), oficiales)

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        self.http.close()


def build_scraper(cfg: AppConfig, meta: dict[str, Any]) -> BaseScraper:
    return FalabellaScraper(cfg, meta)
