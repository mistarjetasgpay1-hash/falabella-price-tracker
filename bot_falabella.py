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
UMBRAL_DESCUENTO = 45.0
PAGINAS_A_ESCANEAR = 4
MAX_ALERTAS_POR_EJECUCION = 30

CATEGORIAS = [
    {
        "nombre": "Tecnología",
        "url": "https://www.falabella.com.pe/falabella-pe/category/cat760706/Tecnologia",
    },
    {
        "nombre": "Celulares",
        "url": "https://www.falabella.com.pe/falabella-pe/category/cat70057/Celulares-y-Telefonos",
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
    """Construye todas las variantes válidas del CDN de imágenes de Falabella Perú."""
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
    """Descarga la imagen del CDN y la convierte a JPEG RGB real en memoria."""
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

    # 1. Descargar y convertir imagen a JPEG estándar para subirla a Telegram
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

    # 2. Respaldo en texto plano solo si el producto no tiene foto
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


def extraer_productos_de_html(html_texto: str) -> list:
    if not html_texto:
        return []
    match = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html_texto,
        re.DOTALL,
    )
    if match:
        try:
            next_data = json.loads(match.group(1))
            return buscar_productos_recursivo(next_data)
        except json.JSONDecodeError:
            pass
    return []


class NavegadorRespaldo:
    """Abre Google Chrome real en caso de que Akamai devuelva HTTP 403."""

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
        co.set_argument("--window-size=1366,768")
        co.set_argument("--lang=es-PE")

        self.page = ChromiumPage(co)
        return self.page

    def obtener_productos(self, url_completa: str) -> list:
        try:
            page = self.iniciar()
            page.get(url_completa, timeout=20)

            # Esperar hasta 6 segundos a que cargue __NEXT_DATA__
            for _ in range(12):
                js_data = page.run_js(
                    "return window.__NEXT_DATA__ ? JSON.stringify(window.__NEXT_DATA__) : '';"
                )
                if js_data:
                    datos = json.loads(js_data)
                    prods = buscar_productos_recursivo(datos)
                    if prods:
                        return prods
                html_actual = page.html
                prods = extraer_productos_de_html(html_actual)
                if prods:
                    return prods
                time.sleep(0.5)
        except Exception as e:
            print(f"[WARN] Error en Chrome de respaldo: {e}")
        return []

    def cerrar(self) -> None:
        if self.page is not None:
            try:
                self.page.quit()
            except Exception:
                pass
            self.page = None


def obtener_productos_pagina(
    req_sess: requests.Session,
    cffi_sess: cffi_requests.Session,
    navegador: NavegadorRespaldo,
    cat: dict,
    pagina: int,
) -> list:
    url_con_orden = f"{cat['url']}?page={pagina}&sortBy=derived.price.search.discount,desc"
    url_simple = f"{cat['url']}?page={pagina}"

    # Nivel 1: Petición directa con requests (sin tocar rutas 404 que bloquean la sesión)
    for url_prueba in (url_con_orden, url_simple):
        try:
            resp = req_sess.get(url_prueba, timeout=12)
            if resp.status_code == 200:
                prods = extraer_productos_de_html(resp.text)
                if prods:
                    return prods
        except requests.RequestException:
            pass

    # Nivel 2: Petición con curl_cffi (huella TLS de navegador sin sobrescribir cabeceras)
    for url_prueba in (url_con_orden, url_simple):
        try:
            resp_cffi = cffi_sess.get(url_prueba, timeout=12)
            if resp_cffi.status_code == 200:
                prods = extraer_productos_de_html(resp_cffi.text)
                if prods:
                    return prods
        except Exception:
            pass

    # Nivel 3: Respaldo con Google Chrome real (salta el bloqueo HTTP 403 de Akamai)
    for url_prueba in (url_con_orden, url_simple):
        prods = navegador.obtener_productos(url_prueba)
        if prods:
            return prods

    return []


def es_vendedor_oficial(prod: dict) -> bool:
    """
    Verifica estrictamente que el producto sea vendido por Falabella Oficial:
    1. Descarta cualquier producto con flag de Marketplace o ID de 9 dígitos que empiece con '1' (3P Seller Center).
    2. Revisa todos los campos posibles de vendedor ('sellerName', 'sellerId', 'offeredBy', 'seller').
    """
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
        valor_raw = valor_raw[0] if valor_raw else ""
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
    navegador = NavegadorRespaldo()

    try:
        with (
            requests.Session() as req_sess,
            cffi_requests.Session(impersonate="safari17_0") as cffi_sess,
        ):
            req_sess.headers.update(
                {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "es-PE,es;q=0.9,en;q=0.8",
                }
            )

            for cat in CATEGORIAS:
                if alertas_enviadas >= MAX_ALERTAS_POR_EJECUCION:
                    break

                for pagina in range(1, PAGINAS_A_ESCANEAR + 1):
                    if alertas_enviadas >= MAX_ALERTAS_POR_EJECUCION:
                        break

                    resultados = obtener_productos_pagina(
                        req_sess, cffi_sess, navegador, cat, pagina
                    )
                    oficiales = [p for p in resultados if es_vendedor_oficial(p)]

                    print(
                        f"[INFO] {cat['nombre']} (pág {pagina}): "
                        f"{len(resultados)} totales | {len(oficiales)} oficiales de Falabella."
                    )

                    if not resultados:
                        break

                    for prod in oficiales:
                        if alertas_enviadas >= MAX_ALERTAS_POR_EJECUCION:
                            break

                        prod_id = str(prod.get("productId") or prod.get("skuId") or "").strip()
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
