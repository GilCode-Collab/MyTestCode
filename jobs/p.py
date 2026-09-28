import subprocess, sys
import json, os, random, re, time, logging
from datetime import datetime
from typing import Optional
import requests
MODO_PRUEBA = os.environ.get('MODO_PRUEBA', 'si').lower() in ('si', 'true', '1')
GUARDAR_EN_SUPABASE = True
INCLUIR_LICORES = False
CADENA_ID = 'pricesmart_sv'
CATEGORIAS = [('G10D03', 'Alimentos')]
if INCLUIR_LICORES:
    CATEGORIAS.append(('G10D08014', 'Cervezas, Vinos y Licores'))
FILAS_POR_LLAMADA = 200
LOTE_SUPABASE = 150
PAUSA_SEG = (2.0, 4.0)
BR = {'account_id': '7024', 'auth_key': 'ev7libhybjg5h1d1', 'domain_key': 'pricesmart_bloomreach_io_es', 'view_id': 'SV'}
CAMPOS = 'pid,title,brand,slug,thumb_image,price_SV,fractionDigits,availability_SV,inventory_SV,saving_amount_SV,original_price_without_saving_SV,sold_by_weight_SV,price_per_uom_SV,uom_description_SV'
API_DIRECTA = 'https://core.dxpapi.com/api/v1/core/'
API_SITIO = 'https://www.pricesmart.com/api/br_discovery/getProductsByKeyword'
URL_SITIO = 'https://www.pricesmart.com/es-sv'
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36', 'Accept': 'application/json, text/plain, */*', 'Accept-Language': 'es-SV,es;q=0.9'}
logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(message)s', datefmt='%H:%M:%S', force=True)
log = logging.getLogger('job')
logging.getLogger('httpx').setLevel(logging.WARNING)

def consultar(categoria: str, inicio: int, filas: int) -> Optional[dict]:
    params = {**BR, 'request_type': 'search', 'search_type': 'category', 'q': categoria, 'rows': filas, 'start': inicio, 'fl': CAMPOS, 'url': URL_SITIO, 'ref_url': URL_SITIO, '_br_uid_2': f'uid={random.randint(10 ** 12, 10 ** 13 - 1)}'}
    for intento in range(3):
        try:
            r = requests.get(API_DIRECTA, params=params, headers=HEADERS, timeout=30)
            if r.status_code == 200 and 'response' in r.json():
                return r.json()
            log.info(f'    API directa respondió {r.status_code}; se intenta por el sitio de PriceSmart')
        except (requests.exceptions.RequestException, ValueError) as e:
            log.info(f'    API directa falló ({e.__class__.__name__}); se intenta por el sitio')
        try:
            cuerpo = [{**BR, 'url': f'{URL_SITIO}/categoria/x/{categoria}', 'ref_url': URL_SITIO, 'start': inicio, 'rows': filas, 'q': categoria, 'fq': [], 'search_type': 'category', 'request_id': int(time.time() * 1000), 'fl': CAMPOS}]
            r = requests.post(API_SITIO, json=cuerpo, headers={**HEADERS, 'Content-Type': 'application/json'}, timeout=30)
            if r.status_code == 200 and 'response' in r.json():
                return r.json()
            log.warning(f'    Sitio respondió {r.status_code}')
        except (requests.exceptions.RequestException, ValueError) as e:
            log.warning(f'    Error de red ({e.__class__.__name__})')
        time.sleep(15 * (intento + 1))
    return None

def a_dolares(valor, decimales) -> Optional[float]:
    try:
        v = float(valor)
        return round(v / 10 ** int(decimales or 2), 2) if v > 0 else None
    except (TypeError, ValueError):
        return None

def normalizar(d: dict, categoria: str) -> Optional[dict]:
    pid, titulo = (str(d.get('pid') or '').strip(), (d.get('title') or '').strip())
    if not pid or not titulo:
        return None
    dec = d.get('fractionDigits', 2)
    precio = a_dolares(d.get('price_SV'), dec)
    regular = a_dolares(d.get('original_price_without_saving_SV'), dec)
    slug = d.get('slug') or ''
    return {'sku': pid, 'ref_externa': None, 'ean': None, 'nombre': re.sub('\\s+', ' ', titulo), 'marca': (d.get('brand') or '').strip() or None, 'categoria': categoria, 'url': f'{URL_SITIO}/producto/{slug}/{pid}' if slug else URL_SITIO, 'imagen_url': d.get('thumb_image') or None, 'precio': precio, 'precio_regular': regular if regular and precio and (regular > precio) else None, 'disponible': str(d.get('availability_SV', 'true')).lower() == 'true'}

def leer_secreto(nombre: str) -> Optional[str]:
    try:
        from google.colab import userdata
        return userdata.get(nombre)
    except Exception:
        return os.environ.get(nombre)

class Base:

    def __init__(self):
        from supabase import create_client
        url, key = (leer_secreto('SUPABASE_URL'), leer_secreto('SUPABASE_SECRET_KEY'))
        if not url or not key:
            raise SystemExit('Faltan los secretos SUPABASE_URL y/o SUPABASE_SECRET_KEY.')
        url, key = (url.strip(), key.strip())
        m = re.match('^(https://[a-z0-9-]+\\.supabase\\.co)', url)
        if not m:
            raise SystemExit(f'SUPABASE_URL no tiene la forma https://xxxx.supabase.co — valor: {url[:60]}')
        if key.startswith('sb_publishable') or '.anon.' in key:
            raise SystemExit('Esa es la llave pública. Los scrapers necesitan la llave SECRETA (service_role).')
        self.sb = create_client(m.group(1), key)

    def rpc(self, fn: str, args: dict):
        return self.sb.rpc(fn, args).execute().data

def sumar(total: dict, r: dict):
    for k, v in (r or {}).items():
        if isinstance(v, (int, float)):
            total[k] = total.get(k, 0) + v
        elif isinstance(v, list) and v:
            total.setdefault('muestra_errores', []).extend(v[:5 - len(total.get('muestra_errores', []))])

def main():
    log.info(f'Scraper PriceSmart v1.0  |  {datetime.now():%Y-%m-%d %H:%M}  |  MODO_PRUEBA={MODO_PRUEBA}')
    base = Base() if GUARDAR_EN_SUPABASE else None
    corrida = base.rpc('iniciar_corrida', {'p_cadena': CADENA_ID}) if base else None
    vistos, pendientes, todos, total = (set(), [], [], {})
    completa = True

    def enviar_lote(lote: list):
        for intento in range(2):
            try:
                sumar(total, base.rpc('cargar_listados', {'p_cadena': CADENA_ID, 'p_corrida': corrida, 'p_items': lote}))
                return
            except Exception as e:
                msg = str(e)
                if ('timeout' in msg.lower() or '57014' in msg) and len(lote) > 10:
                    mitad = len(lote) // 2
                    enviar_lote(lote[:mitad])
                    enviar_lote(lote[mitad:])
                    return
                if intento == 0:
                    time.sleep(10)
                    continue
                log.error(f'    Error enviando {len(lote)} productos: {msg[:200]}')
                total['no_enviados'] = total.get('no_enviados', 0) + len(lote)
                with open(f'pendientes_{CADENA_ID}.json', 'a', encoding='utf-8') as f:
                    for i in lote:
                        f.write(json.dumps(i, ensure_ascii=False) + '\n')

    def enviar():
        if base and pendientes:
            enviar_lote(list(pendientes))
        pendientes.clear()
    for codigo, nombre_cat in CATEGORIAS:
        inicio, encontrados = (0, None)
        while encontrados is None or inicio < encontrados:
            filas = 50 if MODO_PRUEBA else FILAS_POR_LLAMADA
            datos = consultar(codigo, inicio, filas)
            time.sleep(random.uniform(*PAUSA_SEG))
            if datos is None:
                completa = False
                break
            resp = datos['response']
            encontrados = resp.get('numFound', 0)
            docs = resp.get('docs') or []
            for d in docs:
                it = normalizar(d, nombre_cat)
                if it and it['sku'] not in vistos and it['precio']:
                    vistos.add(it['sku'])
                    pendientes.append(it)
                    todos.append(it)
            if len(pendientes) >= LOTE_SUPABASE:
                enviar()
            log.info(f'  {nombre_cat}: {min(inicio + len(docs), encontrados)}/{encontrados} leídos')
            if not docs:
                break
            inicio += len(docs)
            if MODO_PRUEBA and inicio >= 100:
                break
    enviar()
    if base:
        cierre = base.rpc('cerrar_corrida', {'p_corrida': corrida, 'p_completa': completa and (not MODO_PRUEBA), 'p_notas': f'{len(vistos)} productos únicos'})
        total['marcados_no_disponibles'] = (cierre or {}).get('marcados_no_disponibles', 0)
    archivo = f'{CADENA_ID}_{datetime.now():%Y%m%d_%H%M}.json'
    with open(archivo, 'w', encoding='utf-8') as f:
        json.dump(todos, f, ensure_ascii=False, indent=1)
    log.info('=' * 60)
    log.info('  RESUMEN')
    log.info('=' * 60)
    log.info(f"  {CADENA_ID}: {len(vistos)} productos → nuevos {total.get('productos_nuevos', 0)}, emparejados por descripción {total.get('match_descripcion', 0)}, sugerencias para revisar {total.get('sugerencias_revision', 0)}, ya conocidos {total.get('ya_conocidos', 0)} | precios {total.get('precios', 0)} (cambiaron {total.get('precios_cambiados', 0)}) | errores {total.get('errores', 0)} | no enviados {total.get('no_enviados', 0)}")
    if total.get('muestra_errores'):
        log.info(f"    Ejemplos de error: {total['muestra_errores']}")
    log.info(f'  Archivo: {archivo}')
    return total
if __name__ == '__main__' or True:
    resultado = main()
