import subprocess, sys
import json, os, random, re, time, logging
from datetime import datetime
from typing import Optional
import requests
from bs4 import BeautifulSoup
MODO_PRUEBA = os.environ.get('MODO_PRUEBA', 'si').lower() in ('si', 'true', '1')
GUARDAR_EN_SUPABASE = True
CADENA_ID = 'superselectos_sv'
BASE_URL = 'https://www.superselectos.com'
DEPARTAMENTOS = {'01': 'Frescos', '02': 'Alimentos Congelados', '03': 'Abarrotes', '04': 'Cervezas, Vinos y Licores', '05': 'Jugos y Bebidas', '06': 'Mascota', '07': 'Limpieza', '08': 'Higiene y Belleza', '09': 'Bebes y Niños'}
LOTE_SUPABASE = 150
PAUSA_MINIMA_SEG = 2.0
PAUSA_MAXIMA_SEG = 4.0
MAX_REINTENTOS = 3
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36', 'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8', 'Accept-Language': 'es-SV,es;q=0.9,en;q=0.8'}
logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(message)s', datefmt='%H:%M:%S', force=True)
log = logging.getLogger('job')
logging.getLogger('httpx').setLevel(logging.WARNING)

class Descargador:

    def __init__(self):
        self.sesion = requests.Session()
        self.sesion.headers.update(HEADERS)
        self.navegador = None

    @staticmethod
    def bloqueado(html: str) -> bool:
        return 'item-producto' not in html and 'Página' not in html and ('cf-chl' in html or 'challenge-platform' in html or 'Just a moment' in html)

    def _con_navegador(self, url: str) -> Optional[str]:
        if self.navegador is None:
            log.info('  El sitio bloqueó las peticiones simples → se usará un navegador (Playwright)')
            subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'playwright', '-q'])
            subprocess.check_call([sys.executable, '-m', 'playwright', 'install', '--with-deps', 'chromium'], stdout=subprocess.DEVNULL)
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True, args=['--no-sandbox'])
            self.navegador = self._browser.new_page(user_agent=HEADERS['User-Agent'], locale='es-SV')
        self.navegador.goto(url, wait_until='domcontentloaded', timeout=60000)
        try:
            self.navegador.wait_for_selector('li.item-producto', timeout=20000)
        except Exception:
            pass
        return self.navegador.content()

    def html(self, url: str) -> Optional[str]:
        if self.navegador is not None:
            return self._con_navegador(url)
        for intento in range(MAX_REINTENTOS):
            try:
                r = self.sesion.get(url, timeout=30)
                if r.status_code == 200 and (not self.bloqueado(r.text)):
                    return r.text
                if r.status_code in (403, 503) or self.bloqueado(r.text):
                    return self._con_navegador(url)
                if r.status_code == 429:
                    time.sleep(45 * (intento + 1))
                    continue
                if r.status_code >= 500:
                    time.sleep(10 * (intento + 1))
                    continue
                log.error(f'    HTTP {r.status_code} — {url}')
                return None
            except requests.exceptions.RequestException as e:
                log.warning(f'    Error de red ({e.__class__.__name__}) — reintento en 15s')
                time.sleep(15)
        return None

    def cerrar(self):
        if self.navegador is not None:
            self._browser.close()
            self._pw.stop()

def pausa():
    time.sleep(random.uniform(PAUSA_MINIMA_SEG, PAUSA_MAXIMA_SEG))

def precio(texto: str) -> Optional[float]:
    m = re.search('\\d+(?:\\.\\d{1,2})?', (texto or '').replace(',', ''))
    return float(m.group()) if m else None

def total_paginas(soup: BeautifulSoup) -> int:
    lbl = soup.select_one('label.justify-content-start')
    m = re.search('de\\s+(\\d+)', lbl.get_text() if lbl else '')
    return int(m.group(1)) if m else 1

def leer_productos(soup: BeautifulSoup, departamento: str) -> list:
    items = []
    for li in soup.select('li.item-producto'):
        a = li.select_one("a.clickeable[href*='productId']")
        nombre_el = li.select_one('h5.prod-nombre a') or li.select_one('h5.prod-nombre')
        if not a or not nombre_el:
            continue
        m = re.search('productId=(\\d+)', a.get('href', ''))
        if not m:
            continue
        sku = m.group(1)
        nombre = re.sub('\\s+', ' ', nombre_el.get_text()).strip().strip('"')
        venta = precio(li.select_one('strong.precio').get_text() if li.select_one('strong.precio') else '')
        antes = precio(li.select_one('span.antes').get_text() if li.select_one('span.antes') else '')
        img = li.select_one('div.prod-images img')
        sub = li.select_one('div.cat a')
        items.append({'sku': sku, 'ref_externa': None, 'ean': None, 'nombre': nombre, 'marca': None, 'categoria': departamento, 'subcategoria': sub.get_text().strip() if sub else None, 'url': f'{BASE_URL}/products?productId={sku}', 'imagen_url': img.get('src') if img else None, 'precio': venta, 'precio_regular': antes if antes and venta and (antes > venta) else None, 'disponible': True})
    return items

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
    log.info(f'Scraper Súper Selectos v1.0  |  {datetime.now():%Y-%m-%d %H:%M}  |  MODO_PRUEBA={MODO_PRUEBA}')
    base = Base() if GUARDAR_EN_SUPABASE else None
    corrida = base.rpc('iniciar_corrida', {'p_cadena': CADENA_ID}) if base else None
    web = Descargador()
    vistos, pendientes, todos, total = (set(), [], [], {})
    completa = True

    def enviar_lote(lote: list):
        for intento in range(2):
            try:
                limpios = [{k: v for k, v in i.items() if k != 'subcategoria'} for i in lote]
                sumar(total, base.rpc('cargar_listados', {'p_cadena': CADENA_ID, 'p_corrida': corrida, 'p_items': limpios}))
                return
            except Exception as e:
                msg = str(e)
                if ('timeout' in msg.lower() or '57014' in msg) and len(lote) > 10:
                    log.info(f'    Lote de {len(lote)} tardó demasiado — se envía en dos partes')
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
    deptos = list(DEPARTAMENTOS.items())[:2] if MODO_PRUEBA else list(DEPARTAMENTOS.items())
    try:
        for n, (codigo, nombre_depto) in enumerate(deptos, 1):
            pagina, paginas, antes = (1, 1, len(vistos))
            while pagina <= paginas:
                html = web.html(f'{BASE_URL}/products?category={codigo}&page={pagina}')
                pausa()
                if html is None:
                    completa = False
                    break
                soup = BeautifulSoup(html, 'html.parser')
                if pagina == 1:
                    paginas = total_paginas(soup)
                    if MODO_PRUEBA:
                        paginas = min(paginas, 2)
                for it in leer_productos(soup, nombre_depto):
                    if it['sku'] not in vistos and it['precio']:
                        vistos.add(it['sku'])
                        pendientes.append(it)
                        todos.append(it)
                if len(pendientes) >= LOTE_SUPABASE:
                    enviar()
                if pagina % 10 == 0 or pagina == paginas:
                    log.info(f'  [{n}/{len(deptos)}] {nombre_depto}: página {pagina}/{paginas} | {len(vistos) - antes} productos del departamento')
                pagina += 1
        enviar()
    finally:
        web.cerrar()
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
