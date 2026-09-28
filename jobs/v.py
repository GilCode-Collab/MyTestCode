import subprocess, sys
import json, os, random, re, time, logging
from urllib.parse import urlencode, quote
from datetime import datetime
from typing import Optional
import requests
MODO_PRUEBA = os.environ.get('MODO_PRUEBA', 'si').lower() in ('si', 'true', '1')
GUARDAR_EN_SUPABASE = True
CADENAS = [{'id': 'walmart_sv', 'nombre': 'Walmart', 'base_url': 'https://www.walmart.com.sv'}, {'id': 'maxi_despensa_sv', 'nombre': 'Maxi Despensa', 'base_url': 'https://www.maxidespensa.com.sv'}, {'id': 'despensa_don_juan_sv', 'nombre': 'La Despensa de Don Juan', 'base_url': 'https://www.ladespensadedonjuan.com.sv'}]
SOLO_CADENAS = [c.strip() for c in os.environ.get('SOLO_CADENAS', '').split(',') if c.strip()]
EXCLUIR_DEPARTAMENTOS = ['electronica', 'tecnologia', 'celulares', 'computacion', 'videojuegos', 'linea blanca', 'electrodomesticos', 'hogar y decoracion', 'muebles', 'ropa', 'zapatos', 'calzado', 'juguetes', 'deportes', 'ferreteria', 'automotriz', 'herramientas', 'jardin', 'libreria', 'papeleria', 'oficina', 'temporada', 'navidad', 'autos', 'articulos para el hogar']
PRODUCTOS_POR_PAGINA = 50
LIMITE_VTEX = 2500
RANGOS_PRECIO = [(0, 0.99), (1, 1.99), (2, 2.99), (3, 3.99), (4, 5.99), (6, 9.99), (10, 14.99), (15, 24.99), (25, 49.99), (50, 99.99), (100, 99999)]
LOTE_SUPABASE = 150
PAUSA_MINIMA_SEG = 4.0
PAUSA_MAXIMA_SEG = 8.0
ESPERA_429_SEG = 45
MAX_REINTENTOS = 3
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36', 'Accept': 'application/json, text/plain, */*', 'Accept-Language': 'es-SV,es;q=0.9,en;q=0.8'}
logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(message)s', datefmt='%H:%M:%S', force=True)
log = logging.getLogger('job')
logging.getLogger('httpx').setLevel(logging.WARNING)

def leer_secreto(nombre: str) -> Optional[str]:
    try:
        from google.colab import userdata
        return userdata.get(nombre)
    except Exception:
        return os.environ.get(nombre)

def pausa():
    time.sleep(random.uniform(PAUSA_MINIMA_SEG, PAUSA_MAXIMA_SEG))

def get_json(url: str, params, referer: str):
    consulta = urlencode(params, quote_via=quote, safe=':/[]') if params else ''
    url_final = f'{url}?{consulta}' if consulta else url
    for intento in range(MAX_REINTENTOS):
        try:
            r = requests.get(url_final, headers={**HEADERS, 'Referer': referer}, timeout=30)
            if r.status_code in (200, 206):
                return r.json()
            if r.status_code == 429:
                espera = ESPERA_429_SEG * (intento + 1)
                log.warning(f'    HTTP 429 — esperando {espera}s ({intento + 1}/{MAX_REINTENTOS})')
                time.sleep(espera)
                continue
            if r.status_code >= 500 and intento < MAX_REINTENTOS - 1:
                espera = 10 * (intento + 1)
                log.info(f'    HTTP {r.status_code} pasajero — reintento en {espera}s')
                time.sleep(espera)
                continue
            log.error(f'    HTTP {r.status_code} — {url} {params}')
            return None
        except requests.exceptions.RequestException as e:
            log.warning(f'    Error de red ({e.__class__.__name__}) — reintento en 15s')
            time.sleep(15)
    log.error(f'    Sin respuesta tras {MAX_REINTENTOS} intentos — {params}')
    return None

def sin_tildes(t: str) -> str:
    t = (t or '').lower()
    for a, b in (('á', 'a'), ('é', 'e'), ('í', 'i'), ('ó', 'o'), ('ú', 'u'), ('ñ', 'n'), ('ü', 'u')):
        t = t.replace(a, b)
    return t

def departamentos(cadena: dict) -> list:
    arbol = get_json(f"{cadena['base_url']}/api/catalog_system/pub/category/tree/1", {}, cadena['base_url'])
    if not arbol:
        return []
    incluidos, excluidos = ([], [])
    for d in arbol:
        if any((x in sin_tildes(d['name']) for x in EXCLUIR_DEPARTAMENTOS)):
            excluidos.append(d['name'])
        else:
            incluidos.append({'id': d['id'], 'nombre': d['name']})
    log.info(f"  Departamentos incluidos ({len(incluidos)}): {', '.join((x['nombre'] for x in incluidos))}")
    if excluidos:
        log.info(f"  Departamentos excluidos ({len(excluidos)}): {', '.join(excluidos)}")
    return incluidos
EAN_EN_IMAGEN = re.compile('-(\\d{8,14})\\.(?:webp|jpe?g|png)', re.I)

def normalizar(p: dict, departamento: str) -> Optional[dict]:
    try:
        item = p['items'][0]
    except (KeyError, IndexError, TypeError):
        return None
    nombre = (p.get('productName') or '').strip()
    sku = str(item.get('itemId') or '').strip()
    if not nombre or not sku:
        return None
    oferta = {}
    try:
        oferta = item['sellers'][0]['commertialOffer']
    except (KeyError, IndexError, TypeError):
        pass
    precio = oferta.get('Price') or None
    regular = oferta.get('ListPrice') or oferta.get('PriceWithoutDiscount') or None
    disponible = bool(oferta.get('AvailableQuantity', 0) > 0 or oferta.get('IsAvailable'))
    imagenes = item.get('images') or [{}]
    imagen = imagenes[0].get('imageUrl') or None
    ean = str(item.get('ean') or '').strip()
    if not re.fullmatch('\\d{8,14}', ean):
        m = EAN_EN_IMAGEN.search(imagen or '')
        ean = m.group(1) if m else None
    link = p.get('linkText')
    return {'sku': sku, 'ref_externa': str(p.get('productId') or '') or None, 'ean': ean, 'nombre': nombre, 'marca': (p.get('brand') or '').strip() or None, 'categoria': departamento, 'url': p.get('link') or None, 'imagen_url': imagen, 'precio': float(precio) if precio else None, 'precio_regular': float(regular) if regular else None, 'disponible': disponible, '_link': link}

class Base:

    def __init__(self):
        from supabase import create_client
        url, key = (leer_secreto('SUPABASE_URL'), leer_secreto('SUPABASE_SECRET_KEY'))
        if not url or not key:
            raise SystemExit('Faltan los secretos SUPABASE_URL y/o SUPABASE_SECRET_KEY (ver instrucciones arriba).')
        url, key = (url.strip(), key.strip())
        m = re.match('^(https://[a-z0-9-]+\\.supabase\\.co)', url)
        if not m:
            raise SystemExit(f'SUPABASE_URL no tiene la forma https://xxxx.supabase.co — valor recibido: {url[:60]}')
        if m.group(1) != url.rstrip('/'):
            log.info(f"  (SUPABASE_URL ajustada: se quitó '{url[len(m.group(1)):]}' del final)")
        url = m.group(1)
        if key.startswith('sb_publishable') or '.anon.' in key:
            raise SystemExit('Esa es la llave pública. Los scrapers necesitan la llave SECRETA (service_role).')
        self.sb = create_client(url, key)

    def rpc(self, fn: str, args: dict):
        return self.sb.rpc(fn, args).execute().data

    def iniciar(self, cadena: str) -> int:
        return self.rpc('iniciar_corrida', {'p_cadena': cadena})

    def cargar(self, cadena: str, corrida: int, items: list) -> dict:
        limpios = [{k: v for k, v in i.items() if not k.startswith('_')} for i in items]
        return self.rpc('cargar_listados', {'p_cadena': cadena, 'p_corrida': corrida, 'p_items': limpios})

    def cerrar(self, corrida: int, completa: bool, notas: str) -> dict:
        return self.rpc('cerrar_corrida', {'p_corrida': corrida, 'p_completa': completa, 'p_notas': notas})

def sumar(total: dict, r: dict):
    for k, v in (r or {}).items():
        if isinstance(v, (int, float)):
            total[k] = total.get(k, 0) + v
        elif isinstance(v, list) and v:
            total.setdefault('muestra_errores', []).extend(v[:5 - len(total.get('muestra_errores', []))])

def procesar_cadena(cadena: dict, base: Optional[Base]) -> dict:
    log.info('=' * 60)
    log.info(f"  🏪 {cadena['nombre']}  ({cadena['base_url']})")
    log.info('=' * 60)
    deptos = departamentos(cadena)
    if not deptos:
        log.error('  No se pudo leer el árbol de categorías — se omite la cadena.')
        return {'cadena': cadena['id'], 'error': 'sin categorías'}
    if MODO_PRUEBA:
        deptos = deptos[:2]
        log.info('  MODO_PRUEBA: 2 departamentos × 2 rangos de precio × 1 página')
    corrida = base.iniciar(cadena['id']) if base else None
    vistos, pendientes, todos, total = (set(), [], [], {})
    estado = {'completa': True}
    url = f"{cadena['base_url']}/api/catalog_system/pub/products/search"

    def enviar_lote(lote: list):
        for intento in range(2):
            try:
                sumar(total, base.cargar(cadena['id'], corrida, lote))
                return
            except Exception as e:
                msg = str(e)
                lento = 'timeout' in msg.lower() or '57014' in msg
                if lento and len(lote) > 10:
                    log.info(f'    Lote de {len(lote)} tardó demasiado — se envía en dos partes')
                    mitad = len(lote) // 2
                    enviar_lote(lote[:mitad])
                    enviar_lote(lote[mitad:])
                    return
                if intento == 0:
                    time.sleep(10)
                    continue
                log.error(f'    Error enviando {len(lote)} productos a Supabase: {msg[:200]}')
                total['no_enviados'] = total.get('no_enviados', 0) + len(lote)
                with open(f"pendientes_{cadena['id']}.json", 'a', encoding='utf-8') as f:
                    for i in lote:
                        f.write(json.dumps({k: v for k, v in i.items() if not k.startswith('_')}, ensure_ascii=False) + '\n')

    def enviar():
        if base and pendientes:
            enviar_lote(list(pendientes))
        pendientes.clear()

    def recorrer_rango(depto: dict, lo: float, hi: float) -> int:
        desde, recibidos = (0, 0)
        while desde < LIMITE_VTEX:
            params = [('fq', f"C:/{depto['id']}/"), ('fq', f'P:[{lo:.2f} TO {hi:.2f}]'), ('_from', desde), ('_to', desde + PRODUCTOS_POR_PAGINA - 1)]
            datos = get_json(url, params, cadena['base_url'])
            pausa()
            if datos is None:
                estado['completa'] = False
                return recibidos
            for p in datos:
                it = normalizar(p, depto['nombre'])
                if it and it['sku'] not in vistos:
                    vistos.add(it['sku'])
                    if not it['url'] and it['_link']:
                        it['url'] = f"{cadena['base_url']}/{it['_link']}/p"
                    pendientes.append(it)
                    todos.append(it)
            recibidos += len(datos)
            if len(pendientes) >= LOTE_SUPABASE:
                enviar()
            if len(datos) < PRODUCTOS_POR_PAGINA or MODO_PRUEBA:
                return recibidos
            desde += PRODUCTOS_POR_PAGINA
        if hi - lo >= 0.02:
            medio = round((lo + hi) / 2, 2)
            log.info(f'    rango ${lo:.2f}–${hi:.2f} tiene más de {LIMITE_VTEX}: se divide')
            return recibidos + recorrer_rango(depto, lo, medio) + recorrer_rango(depto, round(medio + 0.01, 2), hi)
        log.warning(f'    ⚠ rango ${lo:.2f}–${hi:.2f} sigue lleno; puede faltar producto')
        return recibidos
    rangos = RANGOS_PRECIO[1:3] if MODO_PRUEBA else RANGOS_PRECIO
    for n, depto in enumerate(deptos, 1):
        antes = len(vistos)
        for lo, hi in rangos:
            n_rango = recorrer_rango(depto, lo, hi)
            log.info(f"      {depto['nombre']} ${lo:.2f}–${hi:.2f}: {n_rango} productos")
        log.info(f"  [{n}/{len(deptos)}] {depto['nombre']}: {len(vistos) - antes} productos | {len(vistos)} únicos acumulados")
    enviar()
    completa = estado['completa']
    notas = f"{len(vistos)} productos únicos; {('completa' if completa and (not MODO_PRUEBA) else 'parcial')}"
    if base:
        cierre = base.cerrar(corrida, completa and (not MODO_PRUEBA), notas)
        total['marcados_no_disponibles'] = (cierre or {}).get('marcados_no_disponibles', 0)
    archivo = f"{cadena['id']}_{datetime.now():%Y%m%d_%H%M}.json"
    with open(archivo, 'w', encoding='utf-8') as f:
        json.dump([{k: v for k, v in i.items() if not k.startswith('_')} for i in todos], f, ensure_ascii=False, indent=1)
    total.update({'cadena': cadena['id'], 'unicos': len(vistos), 'archivo': archivo})
    return total

def main():
    log.info(f'Scraper VTEX v2.4  |  {datetime.now():%Y-%m-%d %H:%M}  |  MODO_PRUEBA={MODO_PRUEBA}')
    base = Base() if GUARDAR_EN_SUPABASE else None
    resumen = []
    for cadena in CADENAS:
        if SOLO_CADENAS and cadena['id'] not in SOLO_CADENAS:
            continue
        resumen.append(procesar_cadena(cadena, base))
        time.sleep(random.uniform(20, 35))
    log.info('=' * 60)
    log.info('  RESUMEN')
    log.info('=' * 60)
    for r in resumen:
        log.info(f"  {r.get('cadena')}: {r.get('unicos', 0)} productos → nuevos {r.get('productos_nuevos', 0)}, por código {r.get('match_ean', 0)}, por ref VTEX {r.get('match_ref_vtex', 0)}, por descripción {r.get('match_descripcion', 0)}, ya conocidos {r.get('ya_conocidos', 0)} | precios {r.get('precios', 0)} (cambiaron {r.get('precios_cambiados', 0)}) | errores {r.get('errores', 0)} | no enviados {r.get('no_enviados', 0)}")
        if r.get('muestra_errores'):
            log.info(f"    Ejemplos de error: {r['muestra_errores']}")
    return resumen
if __name__ == '__main__' or True:
    resultado = main()
