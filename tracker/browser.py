"""Navegador de respaldo (Chrome real vía DrissionPage) para cuando la tienda bloquea HTTP.

Solo se importa si hace falta. Navega una vez para validar la sesión (cookies
anti-bot) y después pide cada listado con un XHR desde dentro de la página, lo que
evita renderizar cada página completa (de ~20 s a ~2 s por página).
"""

from __future__ import annotations

import json
import logging
import shutil
import time

from .scraper import parse_listing

log = logging.getLogger(__name__)

# Devuelve solo {results, pagination} para no transferir 2 MB de HTML por CDP.
JS_FETCH = r"""
const x = new XMLHttpRequest();
x.open('GET', arguments[0], false);
x.send();
if (x.status !== 200) { return 'HTTP ' + x.status; }
const m = x.responseText.match(/<script id="__NEXT_DATA__"[^>]*>([\s\S]*?)<\/script>/);
if (!m) { return 'SIN_DATOS'; }
const pp = JSON.parse(m[1]).props.pageProps || {};
return JSON.stringify({results: pp.results || [], pagination: pp.pagination || {}});
"""

JS_READ = r"""
const nd = window.__NEXT_DATA__;
if (!nd || !nd.props || !nd.props.pageProps) { return ''; }
const pp = nd.props.pageProps;
return JSON.stringify({results: pp.results || [], pagination: pp.pagination || {}});
"""


class BrowserFetcher:
    def __init__(self, *, timeout_s: float = 30.0, headless: bool = False) -> None:
        from DrissionPage import ChromiumOptions, ChromiumPage

        co = ChromiumOptions().auto_port()
        ruta = (shutil.which("google-chrome") or shutil.which("google-chrome-stable")
                or shutil.which("chromium-browser") or shutil.which("chromium"))
        if ruta:
            co.set_browser_path(ruta)
        for arg in ("--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                    "--disable-blink-features=AutomationControlled", "--window-size=1366,768", "--lang=es-PE"):
            co.set_argument(arg)
        co.no_imgs(True)            # no descarga imágenes: más rápido
        co.set_load_mode("eager")   # no espera recursos secundarios
        if headless:
            co.headless(True)
        self.timeout_s = timeout_s
        self.page = ChromiumPage(co)
        self._session_ok = False
        log.info("Navegador de respaldo iniciado (%s).", ruta or "Chrome por defecto")

    def _navigate_and_read(self, url: str) -> dict | None:
        self.page.get(url, timeout=self.timeout_s)
        for _ in range(int(self.timeout_s * 2)):
            crudo = self.page.run_js(JS_READ)
            if crudo:
                datos = parse_listing(json.loads(crudo))
                if datos is not None:
                    return datos
            time.sleep(0.5)
        return None

    def fetch_listing(self, url: str) -> dict | None:
        if self._session_ok:
            try:
                crudo = self.page.run_js(JS_FETCH, url)
                if crudo and crudo.startswith("{"):
                    return parse_listing(json.loads(crudo))
                log.info("XHR en navegador devolvió %s; se navega directamente.", (crudo or "vacío")[:20])
            except Exception as exc:
                log.info("XHR en navegador falló (%s); se navega directamente.", exc)
        try:
            datos = self._navigate_and_read(url)
        except Exception as exc:
            raise RuntimeError(f"Chrome: {exc}") from exc
        if datos is not None:
            self._session_ok = True
        return datos

    def close(self) -> None:
        try:
            self.page.quit()
        except Exception:
            pass
