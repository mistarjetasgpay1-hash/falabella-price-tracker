"""Pruebas del scraper de Falabella con datos reales de __NEXT_DATA__ (recortados)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tracker.http_client import BlockedError
from tracker.scraper import FalabellaScraper, extract_next_data

SCRAPER_CFG = {
    "seller_facets": ["FALABELLA", "TOTTUS"],
    "allowed_seller_ids": ["FALABELLA_PERU", "TOTTUS_PERU"],
    "fixed_pages": 2,
    "rotating_pages": 1,
    "max_page": 6,
    "sources": [{"key": "Electrohogar", "url": "/falabella-pe/category/cat40584/Electrohogar", "group": "General"}],
}

# Recortes reales (24/10/2026)
REFRI = {
    "productId": "19590824", "skuId": "19590824", "displayName": "Refrigerador Side By Side 517L No Frost ERSA53K2HVB",
    "brand": "Electrolux", "sellerName": "Falabella", "sellerId": "FALABELLA_PERU",
    "url": "https://www.falabella.com.pe/falabella-pe/product/19590824/refrigerador-side-by-side-517l-no-frost-ersa53k2hvb",
    "mediaUrls": ["https://media.falabella.com.pe/falabellaPE/19590824_01/public"],
    "prices": [{"type": "cmrPrice", "price": ["1,699"], "crossed": False},
               {"type": "internetPrice", "price": ["1,799"], "crossed": False},
               {"type": "normalPrice", "price": ["4,199"], "crossed": True}],
}
ARROZ_TOTTUS = {
    "productId": "113707060", "displayName": "Arroz Faraón Extra Bolsa 5 Kg", "brand": "Faraón",
    "sellerName": "Tottus", "sellerId": "TOTTUS_PERU",
    "url": "https://www.falabella.com.pe/falabella-pe/product/113707060/arroz-faraon-extra-bolsa-5-kg",
    "prices": [{"type": "internetPrice", "price": ["22.50"], "crossed": False},
               {"type": "normalPrice", "price": ["26.90"], "crossed": True}],
}
AIRE_MARKETPLACE = {
    "productId": "157723584", "displayName": "Aire Acondicionado Split 12000 BTU", "brand": "Alfano",
    "sellerName": "Alfano Comercial", "sellerId": "SC976DE",
    "prices": [{"type": "eventPrice", "price": ["1,599"], "crossed": False},
               {"type": "normalPrice", "price": ["1,899"], "crossed": True}],
}


def _html(results, count=480, per_page=48):
    nd = {"props": {"pageProps": {"results": results, "pagination": {"count": count, "perPage": per_page}}},
          "page": "/category/[[...slug]]"}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script></html>'


class FakeHttp:
    def __init__(self, respuestas):
        self.respuestas = respuestas
        self.urls = []

    def get(self, url, *, expect="text", **_):
        self.urls.append(url)
        r = self.respuestas(url) if callable(self.respuestas) else self.respuestas
        if isinstance(r, Exception):
            raise r
        return r

    def close(self):
        pass


def _scraper(make_cfg, http, meta=None, **extra):
    cfg = make_cfg(copy.deepcopy(SCRAPER_CFG) | extra, min_discount_pct=49.5)
    return FalabellaScraper(cfg, meta if meta is not None else {}, http=http)


def test_extract_next_data():
    datos = extract_next_data(_html([REFRI]))
    assert datos["results"][0]["productId"] == "19590824" and datos["pagination"]["count"] == 480
    assert extract_next_data("<html>Access Denied</html>") is None


def test_price_policy_without_card(make_cfg):
    s = _scraper(make_cfg, FakeHttp(""))
    o = s.parse_result(REFRI, s.sources[0])
    assert o.price == 1799 and o.list_price == 4199 and o.card_price == 1699
    assert o.discount == pytest.approx(57.16, abs=0.01)      # no 60 % (eso era con CMR)
    assert o.image_url.endswith("/public")


def test_price_policy_with_card(make_cfg):
    s = _scraper(make_cfg, FakeHttp(""), use_card_price_for_discount=True)
    o = s.parse_result(REFRI, s.sources[0])
    assert o.price == 1699 and o.card_price is None and "CMR" in o.note


def test_official_sellers_only(make_cfg):
    s = _scraper(make_cfg, FakeHttp(""))
    assert s.parse_result(ARROZ_TOTTUS, s.sources[0]).seller_id == "TOTTUS_PERU"
    assert s.parse_result(AIRE_MARKETPLACE, s.sources[0]) is None


def test_iter_uses_seller_filter_and_rotating_pages(make_cfg):
    http = FakeHttp(_html([REFRI, ARROZ_TOTTUS, AIRE_MARKETPLACE], count=480))   # 10 páginas
    meta = {}
    s = _scraper(make_cfg, http, meta)
    offers = list(s.iter_offers())
    assert "f.derived.variant.sellerId=FALABELLA%3A%3ATOTTUS" in http.urls[0]
    paginas = [u.split("page=")[-1] if "page=" in u else "1" for u in http.urls]
    assert paginas == ["1", "2", "3"]                     # 2 fijas + 1 rotativa
    assert meta["cursors"]["Electrohogar"] == 4 and s.meta_changed
    assert len(offers) == 6                               # 2 oficiales × 3 páginas

    http.urls.clear()
    list(_scraper(make_cfg, http, meta).iter_offers())
    assert [u.split("page=")[-1] for u in http.urls[1:]] == ["2", "4"]   # la ventana avanzó


def test_rotation_wraps_around(make_cfg):
    s = _scraper(make_cfg, FakeHttp(""), {"cursors": {"Electrohogar": 6}})
    assert s.pages_for(s.sources[0], total_pages=10) == [2, 6]
    assert s.pages_for(s.sources[0], total_pages=10) == [2, 3]          # max_page=6 → vuelve al inicio


def test_blocked_http_switches_to_browser(make_cfg):
    class FakeBrowser:
        def __init__(self):
            self.urls = []

        def fetch_listing(self, url):
            self.urls.append(url)
            return {"results": [REFRI], "pagination": {"count": 48, "perPage": 48}}

        def close(self):
            pass

    navegador = FakeBrowser()
    cfg = make_cfg(copy.deepcopy(SCRAPER_CFG), min_discount_pct=49.5)
    s = FalabellaScraper(cfg, {}, http=FakeHttp(BlockedError("HTTP 403", 403)), browser_factory=lambda: navegador)
    offers = list(s.iter_offers())
    assert s.stats.mode == "browser" and len(offers) == 1 and len(navegador.urls) == 1


def test_html_without_data_counts_as_block(make_cfg):
    cfg = make_cfg(copy.deepcopy(SCRAPER_CFG) | {"browser_fallback": False}, min_discount_pct=49.5)
    s = FalabellaScraper(cfg, {}, http=FakeHttp("<html>Access Denied</html>"))
    assert list(s.iter_offers()) == [] and s.stats.total_failure


def test_discover_collections(make_cfg):
    portada = ('<a href="/falabella-pe/collection/descuentos-cmr">x</a>'
               '<a href="/falabella-pe/collection/liquidacionhombre">y</a>'
               '<a href="/falabella-pe/collection/sybilla-closet-white">z</a>')
    extra = {"discover_collections": {"enabled": True, "pattern": "liquid|descuent", "max": 4, "group": "Promos"},
             "sources": SCRAPER_CFG["sources"] + [{"key": "CMR", "url": "/falabella-pe/collection/descuentos-cmr"}]}
    s = _scraper(make_cfg, FakeHttp(portada), **extra)
    nuevas = s.discover_sources()
    assert [n.url for n in nuevas] == ["/falabella-pe/collection/liquidacionhombre"]
    assert nuevas[0].group == "Promos" and not nuevas[0].rotate


def test_repo_config_is_valid():
    from tracker.config import load_config

    cfg = load_config(Path(__file__).parents[1] / "config.yaml", require_secrets=False)
    s = FalabellaScraper(cfg, {}, http=FakeHttp(""))
    assert s.allowed_seller_ids == {"FALABELLA_PERU", "TOTTUS_PERU"} and s.seller_filter == "FALABELLA::TOTTUS"
    assert not s.use_card_price and len(s.sources) >= 20
    for src in s.sources:
        assert src.group in cfg.telegram.groups, src.key
