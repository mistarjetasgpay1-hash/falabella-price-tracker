# falabella-price-tracker

Monitor de ofertas de **Falabella Perú (Falabella + Tottus)** con alertas por Telegram. Corre en GitHub Actions, sin servidor.

## Qué hace

1. Recorre las categorías configuradas en `config.yaml`.
2. **Filtro estricto de tienda oficial:** en el servidor, `f.derived.variant.sellerId=FALABELLA::TOTTUS`; en el código, solo se aceptan `FALABELLA_PERU` y `TOTTUS_PERU`. El marketplace (`SC…`) queda fuera.
3. Descarta lo que no llega al umbral de descuento, no tiene stock o contiene palabras excluidas.
4. Consulta el historial (TTL) y envía a Telegram solo lo nuevo o lo que **bajó aún más de precio**.
5. Guarda el historial en la rama `state` (fuera de `main`) y publica un resumen en la pestaña *Actions*.

Fuentes: tecnología, celulares, electrohogar, moda (mujer, hombre, infantil), zapatillas, accesorios, deportes, juguetería, mundo bebé, hogar (dormitorio, decoración, muebles, cocina), belleza (perfumes, maquillaje, higiene), supermercado (Tottus) y colecciones de promoción. Además, en cada corrida se **detectan automáticamente** las colecciones de campaña enlazadas en la portada (Cyber, liquidaciones…).

- **Precios:** el % se calcula con el precio sin tarjeta (internet/evento) contra el precio normal. El precio CMR se muestra aparte (`use_card_price_for_discount: false`).
- **Paginado:** Falabella no permite ordenar por descuento. Por eso se leen las `fixed_pages` primeras páginas de cada fuente más `rotating_pages` que avanzan en cada corrida, hasta cubrir el catálogo.
- **Extracción:** primero HTTP con `curl_cffi` (huella de Chrome, ~1–2 s por página). Si la tienda bloquea, pasa automáticamente a Chrome real (DrissionPage) y valida la sesión una sola vez.

## Estructura

```
├── .github/workflows/
│   ├── monitor.yml      # cron + ejecución + historial en la rama `state`
│   └── ci.yml           # pytest en cada push
├── config.yaml          # umbrales, TTL, fuentes, grupos ← lo único que normalmente editas
├── tracker/
│   ├── __main__.py      # CLI: python -m tracker [--dry-run] [--max-alerts N] [-v]
│   ├── config.py        # carga/valida YAML + secretos
│   ├── models.py        # Offer (modelo normalizado)
│   ├── http_client.py   # reintentos, backoff con jitter, pausas corteses
│   ├── scraper.py       # extracción específica de la tienda
│   ├── browser.py       # Chrome de respaldo si la tienda bloquea HTTP
│   ├── base.py          # interfaz común de scrapers
│   ├── pricing.py       # parseo de precios y % de descuento
│   ├── filters.py       # tienda oficial, stock, umbral, palabras excluidas
│   ├── state.py         # historial con TTL + regla de bajada de precio
│   ├── telegram.py      # envío con throttle y manejo de 429
│   ├── formatter.py     # plantillas HTML de los mensajes
│   └── pipeline.py      # orquestador + resumen de la corrida
└── tests/               # pytest (sin red)
```

## Historial con TTL

Cada producto notificado se guarda con su precio y la fecha del aviso (`state/<tienda>.json`, rama `state`).

| Situación | ¿Se notifica? |
|---|---|
| Producto nuevo | ✅ |
| Su precio bajó ≥ `renotify_drop_pct` (3 %) desde el último aviso | ✅ `📉 ¡Bajó más!` |
| Mismo precio o subió | ❌ |
| Pasaron `ttl_days` (3) desde el último aviso y sigue en oferta | ✅ (el registro vence y se purga solo) |

El TTL es por producto, así que no hay una avalancha de re-avisos el mismo día. Para empezar de cero, borra la rama `state`.

## Telegram

- Secrets (Settings → Secrets and variables → Actions): `TELEGRAM_TOKEN2` y `CHAT_ID2`.
- Cada mensaje lleva hashtags por grupo y categoría (ej. `#Falabella_Supermercado #AlimentosBebidas`). Puedes buscar o filtrar por ellos dentro del chat.
- Máximo `max_individual_alerts` alertas con foto por corrida (las de mayor descuento). El resto va en **un** mensaje resumen; lo que no entra se envía en la siguiente corrida.
- **Temas (opcional):** si conviertes el chat en un grupo con Temas, pon el `thread_id` de cada tema en `telegram.groups` y las alertas se separan solas.
- Si 3 corridas seguidas no obtienen datos (bloqueo o cambio de la web), llega un aviso ⚠️ silencioso.

## Uso

```bash
pip install -r requirements-dev.txt
python -m pytest -q                       # tests
python -m tracker --dry-run -v            # extrae y filtra sin enviar ni guardar
```

En GitHub: *Actions → Monitor → Run workflow* (marca `dry_run` para una simulación).

## Personalizar

- **Umbral:** `offers.min_discount_pct` (global) o `min_discount` por categoría.
- **Categorías:** agrega o comenta líneas en `scraper` dentro de `config.yaml`.
- **Frecuencia:** `cron` en `.github/workflows/monitor.yml` (hora UTC; Lima = UTC-5). El cron de GitHub es de "mejor esfuerzo" y puede retrasarse algunos minutos.
