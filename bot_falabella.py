from io import BytesIO
import json
import os
import re
import time
from pathlib import Path
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
PAGINAS_A_ESCANEAR = 5
MAX_ALERTAS_POR_EJECUCION = 30

# Categorías de Falabella Perú
CATEGORIAS = [
    {
        "nombre": "Tecnología",
        "cat_id": "cat760706",
        "name_slug": "Tecnologia",
        "url": "https://www.falabella.com.pe/falabella-pe/category/cat760706/Tecnologia",
    },
    {
        "nombre": "Celulares",
        "cat_id": "cat70057",
        "name_slug": "Celulares-y-Telefonos",
        "url": "https://www.falabella.com.pe/falabella-pe/category/cat70057/Celulares-y-Telefonos",
    },
    {
        "nombre": "Electrohogar",
        "cat_id": "cat40512",
        "name_slug": "Electrohogar",
        "url": "https://www.falabella.com.pe/falabella-pe/category/cat40512/Electrohogar",
    },
    {
        "nombre": "Cómputo y Laptops",
        "cat_id": "cat40496",
        "name_slug": "Computo",
        "url": "https://www.falabella.com.pe/falabella-pe/category/cat40496/Computo",
    },
    {
        "nombre": "Televisores",
        "cat_id": "cat40554",
        "name_slug": "Televisores",
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
    """Obtiene todas las URLs posibles de la imagen del producto en el CDN de Falabella."""
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
                urls.append(f"https://media.falabella.com.pe/{limpia}/public")
            else:
                urls.append(f"https://media.falabella.com.pe/falabellaPE/{limpia}/public")

    for key in ("skuId", "productId"):
        ident = str(prod.get(key) or "").strip()
        if ident:
            urls.append(f"https://media.falabella.com.pe/falabellaPE/{ident}_1/public")
            urls.append(f"https://media.falabella.com.pe/falabellaPE/{ident}_01/public")
            urls.append(f"https://media.falabella.com.pe/falabellaPE/{ident}/public")

    vistas = set()
    resultado = []
    for u in urls:
        if u not in vistas:
            vistas.add(u)
            resultado.append(u)
    return resultado


def descargar_y_convertir_jpeg(
    session: cffi_requests.Session, urls_imagen: list[str]
) -> bytes | None:
    """
    Descarga la imagen usando la sesión Chrome (curl_cffi) y la convierte en memoria
    a un archivo JPEG RGB estándar con Pillow para que Telegram nunca la rechace.
    """
    headers_img = {
        "Accept": "image/jpeg,image/png,image/webp,image/*,*/*;q=0.8",
        "Referer": "https://www.falabella.com.pe/",
    }
    for url in urls_imagen:
        try:
            resp = session.get(url, headers=headers_img, timeout=10)
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
    session: cffi_requests.Session, mensaje: str, urls_imagen: list[str]
) -> bool:
    base_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

    # 1. Subir imagen convertida a JPEG real
    img_bytes = descargar_y_convertir_jpeg(session, urls_imagen)
    if img_bytes:
        for _ in range(2):
            try:
                from curl_cffi import CurlMime
                mp = CurlMime()
                mp.addpart(
                    name="chat_id",
                    data=CHAT_ID.encode("utf-8"),
                )
                mp.addpart(
                    name="caption",
                    data=mensaje[:1024].encode("utf-8"),
                )
                mp.addpart(
                    name="photo",
                    content_type="image/jpeg",
                    filename="oferta.jpg",
                    data=img_bytes,
                )
                resp = session.post(
                    f"{base_url}/sendPhoto",
                    multipart=mp,
                    timeout=15,
                )
                if resp.status_code == 200:
                    return True
                if resp.status_code == 429:
                    espera = min(
                        resp.json().get("parameters", {}).get("retry_after", 5), 15
                    )
                    time.sleep(espera + 1)
                    continue
                print(f"[WARN] Error en sendPhoto ({resp.status_code}): {resp.text}")
                break
            except Exception as e:
                print(f"[WARN] Excepción enviando foto: {e}")
                break

    # 2. Respaldo en texto plano solo si el producto no tiene imagen válida
    payload_texto = {
        "chat_id": CHAT_ID,
        "text": mensaje,
        "disable_web_page_preview": False,
    }
    for _ in range(2):
        try:
            resp = session.post(
                f"{base_url}/sendMessage", json=payload_texto, timeout=10
            )
            if resp.status_code == 200:
                return True
            if resp.status_code == 429:
                espera = min(
                    resp.json().get("parameters", {}).get("retry_after", 5), 15
                )
                time.sleep(espera + 1)
                continue
            return False
        except Exception:
            return False
    return False


def buscar_productos_recursivo(nodo) -> list:
    """Extrae la lista de productos de cualquier estructura JSON de Falabella."""
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


def obtener_productos_pagina(
    session: cffi_requests.Session, cat: dict, pagina: int
) -> list:
    """
    Obtiene los productos usando curl_cffi (huella Chrome real) probando primero
    la API JSON de Falabella y luego el HTML (__NEXT_DATA__) sin parámetros que vacíen la respuesta.
    """
    api_url = "https://www.falabella.com.pe/s/browse/v1/listing/pe"
    params_api = {
        "categoryId": cat["cat_id"],
        "categoryName": cat["name_slug"],
        "page": str(pagina),
    }
    headers_api = {
        "Accept": "application/json, text/plain, */*",
        "Referer": cat["url"],
    }

    # 1. Intento por API interna de listado
    try:
        resp = session.get(api_url, params=params_api, headers=headers_api, timeout=15)
        if resp.status_code == 200:
            datos = resp.json()
            productos = buscar_productos_recursivo(datos)
            if productos:
                return productos
        else:
            print(f"[DEBUG] API {cat['nombre']} pág {pagina} devolvió HTTP {resp.status_code}")
    except Exception as e:
        print(f"[DEBUG] Excepción API {cat['nombre']} pág {pagina}: {e}")

    # 2. Respaldo por HTML de categoría (__NEXT_DATA__)
    try:
        resp_html = session.get(
            cat["url"],
            params={"page": str(pagina)},
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
            timeout=15,
        )
        if resp_html.status_code == 200:
            match = re.search(
                r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                resp_html.text,
                re.DOTALL,
            )
            if match:
                next_data = json.loads(match.group(1))
                productos = buscar_productos_recursivo(next_data)
                if productos:
                    return productos
            else:
                print(f"[DEBUG] HTML {cat['nombre']} pág {pagina} no contiene __NEXT_DATA__")
        else:
            print(f"[DEBUG] HTML {cat['nombre']} pág {pagina} devolvió HTTP {resp_html.status_code}")
    except Exception as e:
        print(f"[WARN] Error HTML {cat['nombre']} (pág {pagina}): {e}")

    return []


def es_vendedor_oficial(prod: dict) -> bool:
    """
    Filtra estrictamente productos vendidos por Falabella Oficial:
    1. En Falabella Perú, todos los productos de terceros (Marketplace / Seller Center)
       tienen un productId o skuId de 9 dígitos que empieza con '1' (ej. 11xxxxxxx, 12xxxxxxx).
       Los productos propios de Falabella tienen IDs de 7 u 8 dígitos.
    2. Verifica cualquier campo de vendedor (sellerId, sellerName, variants) para descartar terceros.
    """
    for key in ("productId", "skuId"):
        val = str(prod.get(key) or "").strip()
        if val.isdigit() and len(val) >= 9 and val.startswith("1"):
            return False

    posibles_vendedores = []
    for campo in ("sellerName", "sellerId", "seller"):
        valor = prod.get(campo)
        if isinstance(valor, str) and valor.strip():
            posibles_vendedores.append(valor.strip().upper())

    variantes = prod.get("variants")
    if isinstance(variantes, list):
        for v in variantes:
            if isinstance(v, dict):
                for campo in ("sellerName", "sellerId"):
                    valor = v.get(campo)
                    if isinstance(valor, str) and valor.strip():
                        posibles_vendedores.append(valor.strip().upper())

    for vend in posibles_vendedores:
        if vend.startswith("SC") or "MARKETPLACE" in vend:
            return False
        if "FALABELLA" not in vend:
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

    try:
        # Sesión con impersonación TLS de Chrome real para pasar el firewall de Falabella
        with cffi_requests.Session(impersonate="chrome120") as session:
            session.headers.update(
                {
                    "Accept-Language": "es-PE,es;q=0.9,en;q=0.8",
                }
            )

            # Visita inicial rápida para obtener cookies de sesión válidas de Falabella Perú
            try:
                session.get("https://www.falabella.com.pe/falabella-pe", timeout=10)
            except Exception:
                pass

            for cat in CATEGORIAS:
                if alertas_enviadas >= MAX_ALERTAS_POR_EJECUCION:
                    break

                for pagina in range(1, PAGINAS_A_ESCANEAR + 1):
                    if alertas_enviadas >= MAX_ALERTAS_POR_EJECUCION:
                        break

                    resultados = obtener_productos_pagina(session, cat, pagina)
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

                        prod_id = str(prod.get("productId", "")).strip()
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

                            if enviar_alerta_telegram(session, mensaje, urls_img):
                                notificados.add(clave_unica)
                                nuevos_registros = True
                                alertas_enviadas += 1
                                print(
                                    f"[OK] ({alertas_enviadas}/{MAX_ALERTAS_POR_EJECUCION}) "
                                    f"{nombre} (-{descuento:.1f}%)"
                                )
                                time.sleep(0.6)
    finally:
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
