from io import BytesIO
import json
import os
import re
import shutil
import time
from pathlib import Path
import requests
from curl_cffi import requests as cffi_requests
from PIL import Image

# 1. Credenciales del Bot de Falabella
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN2", "").strip()
CHAT_ID = os.getenv("CHAT_ID2", "").strip()

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise ValueError(
        "Faltan las variables TELEGRAM_TOKEN2 o CHAT_ID2 en los Secrets de GitHub."
    )

ARCHIVO_ESTADO = Path("notificados_falabella.json")
UMBRAL_DESCUENTO = 40.0  # Umbral en 40% para captar más ofertas reales de Falabella Oficial
PAGINAS_A_ESCANEAR = 3
MAX_ALERTAS_POR_EJECUCION = 30

# URLs verificadas de Falabella Perú
CATEGORIAS = [
    {
        "nombre": "Tecnología",
        "url": "https://www.falabella.com.pe/falabella-pe/category/cat760706/Tecnologia",
    },
    {
        "nombre": "Celulares",
        "url": "https://www.falabella.com.pe/falabella-pe/category/cat760706/Celulares-y-Telefonos",
        "url_alt": "https://www.falabella.com.pe/falabella-pe/category/cat70057/Celulares-y-Telefonos",
    },
    {
        "nombre": "Electrohogar",
        "url": "https://www.falabella.com.pe/falabella-pe/category/cat40512/Electrohogar",
    },
    {
        "nombre": "Cómputo y Laptops",
        "url": "https://www.falabella.com.pe/falabella-pe/category/cat40496/Computo",
    },
    {
        "nombre": "Televisores",
        "url": "https://www.falabella.com.pe/falabella-pe/category/cat40554/Televisores",
    },
]


def cargar_notificados() -> set:
    if not ARCHIVO_ESTADO.exists():
        return set()
    try:
        with ARCHIVO_ESTADO.open("r", encoding="utf-8") as f:
            datos = json.load(f)
            return set(datos) if isinstance(datos, list) else set()
    except (json.JSONDecodeError, OSError):
        return set()


def guardar_notificados(notificados: set) -> None:
    with ARCHIVO_ESTADO.open("w", encoding="utf-8") as f:
        json.dump(sorted(notificados), f, indent=2, ensure_ascii=False)


def construir_urls_imagen(prod: dict) -> list[str]:
    """Construye las URLs del CDN de imágenes de Falabella Perú forzando salida JPEG."""
    urls = []
    media_urls = prod.get("mediaUrls") or []
    if isinstance(media_urls, str):
        media_urls = [media_urls]

    if isinstance(media_urls, list):
        for item in media_urls[:3]:
            limpia = str(item).strip().lstrip("/")
            if not limpia:
                continue
            if limpia.startswith("http"):
                urls.append(limpia)
            elif limpia.startswith("falabellaPE/"):
                urls.append(f"https://media.falabella.com.pe/{limpia}/w=800,h=800,f=jpeg")
                urls.append(f"https://media.falabella.com.pe/{limpia}/public")
            else:
                urls.append(f"https://media.falabella.com.pe/falabellaPE/{limpia}/w=800,h=800,f=jpeg")
                urls.append(f"https://media.falabella.com.pe/falabellaPE/{limpia}/public")

    for key in ("skuId", "productId"):
        ident = str(prod.get(key) or "").strip()
        if ident:
            urls.append(f"https://media.falabella.com.pe/falabellaPE/{ident}_1/w=800,h=800,f=jpeg")
            urls.append(f"https://media.falabella.com.pe/falabellaPE/{ident}_1/public")
            urls.append(f"https://media.falabella.com.pe/falabellaPE/{ident}_01/public")
            urls.append(
                f"https://falabella.scene7.com/is/image/FalabellaPE/{ident}_1?wid=800&hei=800&qlt=85&fmt=jpeg"
            )

    vistas = set()
    resultado = []
    for u in urls:
        if u not in vistas:
            vistas.add(u)
            resultado.append(u)
    return resultado


def descargar_y_convertir_jpeg(
    cffi_sess: cffi_requests.Session, urls_imagen: list[str]
) -> bytes | None:
    """Descarga la imagen del producto y la convierte a JPEG RGB real con Pillow."""
    headers_img = {
        "Accept": "image/jpeg,image/png,image/webp,image/*,*/*;q=0.8",
        "Referer": "https://www.falabella.com.pe/",
    }
    for url in urls_imagen:
        try:
            resp = cffi_sess.get(url, headers=headers_img, timeout=10)
            if resp.status_code == 200 and resp.content and len(resp.content) > 800:
                with Image.open(BytesIO(resp.content)) as img:
                    img_rgb = img.convert("RGB")
                    buffer = BytesIO()
                    img_rgb.save(buffer, format="JPEG", quality=88)
                    return buffer.getvalue()
        except Exception:
            continue
    return None


def enviar_alerta_telegram(
    req_sess: requests.Session,
    cffi_sess: cffi_requests.Session,
    mensaje: str,
    urls_imagen: list[str],
) -> bool:
    base_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

    img_bytes = descargar_y_convertir_jpeg(cffi_sess, urls_imagen)
    if img_bytes:
        for _ in range(2):
            try:
                resp = req_sess.post(
                    f"{base_url}/sendPhoto",
                    data={"chat_id": CHAT_ID, "caption": mensaje[:1024]},
                    files={"photo": ("oferta.jpg", img_bytes, "image/jpeg")},
                    timeout=15,
                )
                if resp.ok:
                    return True
                if resp.status_code == 429:
                    espera = min(
                        resp.json().get("parameters", {}).get("retry_after", 5), 15
                    )
                    time.sleep(espera + 1)
                    continue
                print(f"[WARN] sendPhoto devolvió {resp.status_code}: {resp.text}")
                break
            except requests.RequestException as e:
                print(f"[WARN] Error de red en sendPhoto: {e}")
                break

    payload_texto = {
        "chat_id": CHAT_ID,
        "text": mensaje,
        "disable_web_page_preview": False,
    }
    for _ in range(2):
        try:
            resp = req_sess.post(
                f"{base_url}/sendMessage", json=payload_texto, timeout=10
            )
            if resp.ok:
                return True
            if resp.status_code == 429:
                espera = min(
                    resp.json().get("parameters", {}).get("retry_after", 5), 15
                )
                time.sleep(espera + 1)
                continue
            return False
        except requests.RequestException:
            return False
    return False


def buscar_productos_recursivo(nodo) -> list:
    if isinstance(nodo, list):
        productos_validos = [
            item
            for item in nodo
            if isinstance(item, dict)
            and ("productId" in item or "skuId" in item)
            and "prices" in item
        ]
        if productos_validos:
            return productos_validos
        for elemento in nodo:
            hallado = buscar_productos_recursivo(elemento)
            if hallado:
                return hallado
    elif isinstance(nodo, dict):
        for llave in ("results", "products", "data", "pageProps", "searchData"):
            if llave in nodo:
                hallado = buscar_productos_recursivo(nodo[llave])
                if hallado:
                    return hallado
        for valor in nodo.values():
            hallado = buscar_productos_recursivo(valor)
            if hallado:
                return hallado
    return []


class NavegadorChrome:
    """Gestiona Google Chrome real con reinicio rápido por categoría para evitar bloqueos."""

    def __init__(self) -> None:
        self.page = None

    def iniciar(self):
        if self.page is not None:
            return self.page
        from DrissionPage import ChromiumPage, ChromiumOptions

        co = ChromiumOptions().auto_port()
        ruta_chrome = (
            shutil.which("google-chrome")
            or shutil.which("google-chrome-stable")
            or shutil.which("chromium-browser")
            or shutil.which("chromium")
        )
        if ruta_chrome:
            co.set_browser_path(ruta_chrome)
        co.set_argument("--no-sandbox")
        co.set_argument("--disable-dev-shm-usage")
        co.set_argument("--disable-gpu")
        co.set_argument("--disable-blink-features=AutomationControlled")
        co.set_argument("--blink-settings=imagesEnabled=false")  # Carga 3x más rápido sin descargar fotos en el navegador
        co.set_argument("--window-size=1366,768")
        co.set_argument("--lang=es-PE")

        self.page = ChromiumPage(co)
        return self.page

    def reiniciar(self) -> None:
        self.cerrar()
        time.sleep(0.4)
        self.iniciar()

    def extraer_json_actual(self, page) -> list:
        for _ in range(10):
            try:
                js_data = page.run_js(
                    "return window.__NEXT_DATA__ ? JSON.stringify(window.__NEXT_DATA__) : '';"
                )
                if js_data:
                    datos = json.loads(js_data)
                    prods = buscar_productos_recursivo(datos)
                    if prods:
                        return prods
            except Exception:
                pass
            time.sleep(0.4)
        return []

    def obtener_productos_categoria(self, cat: dict, pagina: int) -> list:
        urls_base = [cat["url"]]
        if "url_alt" in cat:
            urls_base.append(cat["url_alt"])

        for base in urls_base:
            # 1° Intentar con filtro nativo de Falabella Oficial + Mayor Descuento
            variantes_query = [
                f"{base}?page={pagina}&sortBy=derived.price.search.discount,desc&f.derived.variant.sellerId=FALABELLA&facetSelected=true",
                f"{base}?page={pagina}&sortBy=derived.price.search.discount,desc",
            ]
            for url_completa in variantes_query:
                try:
                    page = self.iniciar()
                    page.get(url_completa, timeout=15)
                    prods = self.extraer_json_actual(page)
                    if prods:
                        return prods
                except Exception as e:
                    print(f"[WARN] Reintentando navegador en {cat['nombre']}: {e}")
                    self.reiniciar()
        return []

    def cerrar(self) -> None:
        if self.page is not None:
            try:
                self.page.quit()
            except Exception:
                pass
            self.page = None


def es_vendedor_oficial(prod: dict) -> bool:
    """Verifica estrictamente que el producto sea vendido por Falabella Oficial."""
    if prod.get("isMarketplace") is True or prod.get("marketplace") is True:
        return False

    for key in ("productId", "skuId"):
        val = str(prod.get(key) or "").strip()
        if val.isdigit() and len(val) >= 9 and val.startswith("1"):
            return False

    campos_vendedor = []
    for k in ("sellerName", "sellerId", "offeredBy", "seller", "vendor"):
        val = prod.get(k)
        if isinstance(val, str) and val.strip():
            campos_vendedor.append(val.strip().upper())

    for texto_vend in campos_vendedor:
        if texto_vend.startswith("SC") or "MARKETPLACE" in texto_vend:
            return False
        if "FALABELLA" not in texto_vend:
            return False

    nombre = str(prod.get("displayName") or "").upper()
    if "REACONDICIONADO" in nombre or "REFURBISHED" in nombre:
        return False

    return True


def limpiar_precio(valor_raw) -> float:
    if isinstance(valor_raw, list):
        for v in valor_raw:
            monto = limpiar_precio(v)
            if monto > 0:
                return monto
        return 0.0
    if not valor_raw:
        return 0.0
    limpio = re.sub(r"[^\d.]", "", str(valor_raw).replace(",", ""))
    try:
        return float(limpio)
    except ValueError:
        return 0.0


def extraer_datos_falabella(prod: dict) -> tuple[float, float, str, list[str]]:
    precios = prod.get("prices", [])
    precios_detectados = []

    for p in precios:
        if not isinstance(p, dict):
            continue
        monto = limpiar_precio(p.get("price"))
        if monto > 0:
            precios_detectados.append(monto)

    if len(precios_detectados) >= 2:
        precio_lista = max(precios_detectados)
        precio_oferta = min(precios_detectados)
    else:
        precio_lista, precio_oferta = 0.0, 0.0

    url_producto = str(prod.get("url") or "").strip()
    if url_producto and not url_producto.startswith("http"):
        url_producto = f"https://www.falabella.com.pe{url_producto}"
    elif not url_producto:
        prod_id = prod.get("productId", "")
        url_producto = f"https://www.falabella.com.pe/falabella-pe/product/{prod_id}"

    urls_imagen = construir_urls_imagen(prod)

    return precio_lista, precio_oferta, url_producto, urls_imagen


def monitorear_ofertas() -> None:
    inicio = time.monotonic()
    notificados = cargar_notificados()
    nuevos_registros = False
    alertas_enviadas = 0
    navegador = NavegadorChrome()

    try:
        with (
            requests.Session() as req_sess,
            cffi_requests.Session(impersonate="chrome120") as cffi_sess,
        ):
            for idx, cat in enumerate(CATEGORIAS):
                if alertas_enviadas >= MAX_ALERTAS_POR_EJECUCION:
                    break

                # Reiniciar sesión limpia de Chrome en cada categoría para evitar bloqueo Akamai tras 4 páginas
                if idx > 0:
                    navegador.reiniciar()

                for pagina in range(1, PAGINAS_A_ESCANEAR + 1):
                    if alertas_enviadas >= MAX_ALERTAS_POR_EJECUCION:
                        break

                    resultados = navegador.obtener_productos_categoria(cat, pagina)
                    oficiales = [p for p in resultados if es_vendedor_oficial(p)]

                    max_desc_pagina = 0.0
                    for p_of in oficiales:
                        pl, po, _, _ = extraer_datos_falabella(p_of)
                        if pl > 0 and 0 < po < pl:
                            d = ((pl - po) / pl) * 100.0
                            if d > max_desc_pagina:
                                max_desc_pagina = d

                    print(
                        f"[INFO] {cat['nombre']} (pág {pagina}): "
                        f"{len(resultados)} totales | {len(oficiales)} oficiales "
                        f"(Máx desc: {max_desc_pagina:.1f}%)."
                    )

                    if not resultados:
                        break

                    for prod in oficiales:
                        if alertas_enviadas >= MAX_ALERTAS_POR_EJECUCION:
                            break

                        prod_id = str(
                            prod.get("productId") or prod.get("skuId") or ""
                        ).strip()
                        if not prod_id:
                            continue

                        precio_lista, precio_oferta, url_prod, urls_img = (
                            extraer_datos_falabella(prod)
                        )

                        if (
                            precio_lista <= 0
                            or precio_oferta <= 0
                            or precio_oferta >= precio_lista
                        ):
                            continue

                        clave_unica = f"{prod_id}|{precio_oferta:.2f}"
                        if clave_unica in notificados:
                            continue

                        descuento = (
                            (precio_lista - precio_oferta) / precio_lista
                        ) * 100.0

                        if descuento >= UMBRAL_DESCUENTO:
                            nombre = prod.get(
                                "displayName", "Producto sin nombre"
                            ).strip()
                            marca = prod.get("brand", "Genérico").strip()

                            mensaje = (
                                f"🟢 OFERTA FALABELLA OFICIAL (-{descuento:.1f}%)\n\n"
                                f"Producto: {nombre}\n"
                                f"Marca: {marca}\n"
                                f"Vendedor: Falabella Oficial\n"
                                f"Precio Normal: S/ {precio_lista:,.2f}\n"
                                f"Precio Oferta: S/ {precio_oferta:,.2f}\n\n"
                                f"Enlace: {url_prod}"
                            )

                            if enviar_alerta_telegram(
                                req_sess, cffi_sess, mensaje, urls_img
                            ):
                                notificados.add(clave_unica)
                                nuevos_registros = True
                                alertas_enviadas += 1
                                print(
                                    f"[OK] ({alertas_enviadas}/{MAX_ALERTAS_POR_EJECUCION}) "
                                    f"{nombre} (-{descuento:.1f}%)"
                                )
                                time.sleep(0.5)
    finally:
        navegador.cerrar()
        if nuevos_registros:
            guardar_notificados(notificados)
            print(
                f"[INFO] Se guardaron {alertas_enviadas} ofertas nuevas en {ARCHIVO_ESTADO.name}."
            )
        else:
            print("[INFO] Sin ofertas nuevas que superen el umbral.")

        duracion = time.monotonic() - inicio
        print(f"[FIN] Ejecución completada en {duracion:.2f} s.")


if __name__ == "__main__":
    monitorear_ofertas()
