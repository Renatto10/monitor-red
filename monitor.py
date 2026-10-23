import argparse
import csv
import json
import logging
import platform
import re
import socket
import subprocess
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ARCHIVO_EQUIPOS = "equipos.json"
CARPETA_REPORTES = Path("reportes")
ARCHIVO_HISTORIAL = CARPETA_REPORTES / "historial.csv"
ARCHIVO_ALERTAS = CARPETA_REPORTES / "alertas.log"
COLUMNAS_HISTORIAL = ["fecha_hora", "equipo", "ip", "estado", "tiempo_ms", "puertos"]
LIMITE_LENTO_MS = 100
MAX_HILOS = 10
INTERVALO_MINIMO = 5

logger = logging.getLogger("monitor")

# Windows en espanol puede mostrar "tiempo<1m", por eso la "s" es opcional
PATRON_TIEMPO = re.compile(
    r"(?:tiempo|time)\s*([=<])\s*([\d.,]+)\s*ms?", re.IGNORECASE
)


def cargar_equipos(ruta=ARCHIVO_EQUIPOS):
    with open(ruta, encoding="utf-8") as archivo:
        return json.load(archivo)


def hacer_ping(destino, espera_ms=1000):
    """Devuelve (activo, tiempo_ms). tiempo_ms = 0.0 significa menos de 1 ms."""
    if platform.system() == "Windows":
        comando = ["ping", "-n", "1", "-w", str(espera_ms), destino]
    else:
        comando = ["ping", "-c", "1", "-W", str(max(1, espera_ms // 1000)), destino]

    try:
        resultado = subprocess.run(
            comando, capture_output=True, text=True, timeout=5
        )
    except subprocess.TimeoutExpired:
        return False, None

    salida = resultado.stdout
    # Se exige TTL en la respuesta: en Windows, un "host inaccesible" puede
    # devolver codigo 0 aunque el equipo no haya respondido.
    activo = resultado.returncode == 0 and "ttl=" in salida.lower()

    tiempo = None
    if activo:
        coincidencia = PATRON_TIEMPO.search(salida)
        if coincidencia:
            simbolo, valor = coincidencia.groups()
            tiempo = 0.0 if simbolo == "<" else float(valor.replace(",", "."))
    return activo, tiempo


def puerto_abierto(destino, puerto, tiempo_limite=2):
    try:
        with socket.create_connection((destino, puerto), timeout=tiempo_limite):
            return True
    except OSError:
        return False


def determinar_estado(activo, tiempo, puertos):
    if not activo:
        return "CAIDO"
    if any(not abierto for abierto in puertos.values()):
        return "SERVICIO CAIDO"
    if tiempo is not None and tiempo > LIMITE_LENTO_MS:
        return "LENTO"
    return "ACTIVO"


def revisar_equipo(equipo):
    activo, tiempo = hacer_ping(equipo["ip"])

    # Solo se revisan puertos si el equipo respondio al ping
    puertos = {}
    if activo:
        for puerto in equipo.get("puertos", []):
            puertos[puerto] = puerto_abierto(equipo["ip"], puerto)

    return {
        "nombre": equipo["nombre"],
        "ip": equipo["ip"],
        "activo": activo,
        "tiempo_ms": tiempo,
        "puertos": puertos,
        "estado": determinar_estado(activo, tiempo, puertos),
    }


def revisar_equipos(equipos):
    # pool.map mantiene el mismo orden que la lista original
    with ThreadPoolExecutor(max_workers=MAX_HILOS) as pool:
        return list(pool.map(revisar_equipo, equipos))


def texto_tiempo(tiempo):
    if tiempo is None:
        return "-"
    if tiempo < 1:
        return "<1 ms"
    return f"{tiempo:.0f} ms"


def texto_puertos(puertos):
    if not puertos:
        return "-"
    return "  ".join(
        f"{p}:{'abierto' if abierto else 'cerrado'}" for p, abierto in puertos.items()
    )


def texto_resumen(resultados):
    contador = Counter(r["estado"] for r in resultados)
    return ", ".join(f"{n} {estado.lower()}" for estado, n in contador.items())


def mostrar_tabla(resultados):
    print(f"{'Equipo':<22}{'IP':<18}{'Estado':<16}{'Tiempo':<10}{'Puertos'}")
    print("-" * 80)
    for r in resultados:
        print(
            f"{r['nombre']:<22}{r['ip']:<18}{r['estado']:<16}"
            f"{texto_tiempo(r['tiempo_ms']):<10}{texto_puertos(r['puertos'])}"
        )
    print(f"\nResumen: {texto_resumen(resultados)}")


def guardar_historial(resultados, ruta=ARCHIVO_HISTORIAL):
    ruta.parent.mkdir(exist_ok=True)
    archivo_nuevo = not ruta.exists()
    fecha_hora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # utf-8-sig permite que Excel abra el CSV sin problemas de caracteres
    with open(ruta, "a", newline="", encoding="utf-8-sig") as archivo:
        escritor = csv.writer(archivo)
        if archivo_nuevo:
            escritor.writerow(COLUMNAS_HISTORIAL)
        for r in resultados:
            escritor.writerow(
                [
                    fecha_hora,
                    r["nombre"],
                    r["ip"],
                    r["estado"],
                    "" if r["tiempo_ms"] is None else r["tiempo_ms"],
                    texto_puertos(r["puertos"]),
                ]
            )


def mostrar_disponibilidad(ruta=ARCHIVO_HISTORIAL):
    if not ruta.exists():
        print("Aun no hay historial. Ejecuta primero el monitor.")
        return

    totales = {}
    with open(ruta, newline="", encoding="utf-8-sig") as archivo:
        for fila in csv.DictReader(archivo):
            datos = totales.setdefault(fila["equipo"], {"revisiones": 0, "caidas": 0})
            datos["revisiones"] += 1
            if fila["estado"] in ("CAIDO", "SERVICIO CAIDO"):
                datos["caidas"] += 1

    print(f"{'Equipo':<22}{'Revisiones':<12}{'Caidas':<8}{'Disponibilidad'}")
    print("-" * 56)
    for nombre, datos in totales.items():
        disponibilidad = (datos["revisiones"] - datos["caidas"]) * 100 / datos["revisiones"]
        print(
            f"{nombre:<22}{datos['revisiones']:<12}{datos['caidas']:<8}"
            f"{disponibilidad:.1f}%"
        )


def configurar_logger():
    CARPETA_REPORTES.mkdir(exist_ok=True)
    logger.setLevel(logging.INFO)
    formato = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    manejador_archivo = logging.FileHandler(ARCHIVO_ALERTAS, encoding="utf-8")
    manejador_archivo.setFormatter(formato)
    manejador_consola = logging.StreamHandler()
    manejador_consola.setFormatter(formato)

    logger.addHandler(manejador_archivo)
    logger.addHandler(manejador_consola)


def detectar_cambios(resultados, estados_previos):
    """Compara con el ciclo anterior y registra alertas o recuperaciones."""
    for r in resultados:
        anterior = estados_previos.get(r["nombre"])
        actual = r["estado"]

        if anterior is not None and anterior != actual:
            if actual == "ACTIVO":
                logger.info(
                    "RECUPERADO: %s (%s) volvio a ACTIVO (antes: %s)",
                    r["nombre"], r["ip"], anterior,
                )
            else:
                logger.warning(
                    "ALERTA: %s (%s) cambio de %s a %s",
                    r["nombre"], r["ip"], anterior, actual,
                )
        estados_previos[r["nombre"]] = actual


def monitorear(equipos, intervalo):
    configurar_logger()
    estados_previos = {}
    print(
        f"Monitoreo continuo de {len(equipos)} equipos cada {intervalo} s. "
        "Presiona Ctrl+C para detener.\n"
    )

    try:
        while True:
            resultados = revisar_equipos(equipos)
            guardar_historial(resultados)

            if not estados_previos:
                # Primer ciclo: tabla completa como punto de partida
                mostrar_tabla(resultados)
                print()
            else:
                hora = datetime.now().strftime("%H:%M:%S")
                print(f"[{hora}] {texto_resumen(resultados)}")

            detectar_cambios(resultados, estados_previos)
            time.sleep(intervalo)
    except KeyboardInterrupt:
        print("\nMonitoreo detenido.")


def main():
    parser = argparse.ArgumentParser(description="Monitor de red simple")
    parser.add_argument(
        "--historial",
        action="store_true",
        help="muestra la disponibilidad acumulada de cada equipo",
    )
    parser.add_argument(
        "--cada",
        type=int,
        metavar="SEGUNDOS",
        help="repite la revision cada N segundos hasta presionar Ctrl+C",
    )
    args = parser.parse_args()

    if args.historial:
        mostrar_disponibilidad()
        return

    equipos = cargar_equipos()

    if args.cada is not None:
        if args.cada < INTERVALO_MINIMO:
            parser.error(f"el intervalo minimo es {INTERVALO_MINIMO} segundos")
        monitorear(equipos, args.cada)
        return

    print(f"Revisando {len(equipos)} equipos...\n")
    inicio = time.perf_counter()
    resultados = revisar_equipos(equipos)
    duracion = time.perf_counter() - inicio

    mostrar_tabla(resultados)
    guardar_historial(resultados)
    print(f"Revision completada en {duracion:.1f} s. Historial: {ARCHIVO_HISTORIAL}")


if __name__ == "__main__":
    main()