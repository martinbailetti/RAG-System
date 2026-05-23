"""Test de reordenamiento de título en la ingesta HTML."""
from pathlib import Path
import tempfile, os
from ingesta.html_processing import process_html_for_chunks

# HTML que simula la estructura real: contenido técnico PRIMERO en el DOM,
# título/header/intro DESPUÉS (orden DOM inverso al visual)
html = """<!DOCTYPE html>
<html>
<head><title>COMO CONFIGURAR UN CAJERO COMO ESCLAVO DE OTRO MASTER</title></head>
<body>
<div class="content" style="order:2">
  <p>Verificar que ese es el Servidor correcto en esta pantalla</p>
  <p>Se puede ver que la IP debe ser la del cajero MASTER</p>
  <p>NOTAS A TENER EN CUENTA:</p>
  <p>Si un servidor hace de MASTER y se apaga, los esclavos ya no podran validar tickets</p>
  <p>3) Realizar la configuración de derivar el TicketServer actual (normalmente 127.0.0.1)
     en los cajeros ESCLAVOS por la IP del cajero MASTER.</p>
  <p>Ir a Configuraciones</p>
  <p>4) Opcionalmente puede ser que haya que variar password, puertos, modo conexión.</p>
  <p>5) Una vez reiniciado, se puede validar que el sistema ya trabaja con el
     TicketServer del cajero MASTER.</p>
</div>
<div class="header" style="order:1">
  <h1>COMO CONFIGURAR UN CAJERO COMO ESCLAVO DE OTRO MASTER</h1>
  <p>Nota Técnica de Software</p>
  <p>SMI2000 S.L.</p>
  <p>Para configurar un cajero como esclavo de otro cajero en relación al uso del
     TicketServer hay que realizar estos pasos:</p>
  <p>1) Conectar en la misma red los dos o más cajeros. Preferentemente en la red 2
     de conexiones externas.</p>
  <p>2) Averiguar que dirección IP se publica el cajero MASTER.</p>
</div>
</body>
</html>"""

tmp = Path(tempfile.mktemp(suffix=".html"))
tmp.write_text(html, encoding="utf-8")

chunks = process_html_for_chunks(str(tmp))
full_text = chunks[0].page_content if chunks else ""

# Verificar orden
pos_title = full_text.find("COMO CONFIGURAR UN CAJERO")
pos_intro = full_text.find("hay que realizar estos pasos")
pos_step1 = full_text.find("1) Conectar")
pos_step3 = full_text.find("3) Realizar la configuración")
pos_notas = full_text.find("NOTAS A TENER EN CUENTA")

print("=== POSICIONES EN TEXTO ===")
print(f"  Título:    pos {pos_title}")
print(f"  Intro:     pos {pos_intro}")
print(f"  Paso 1:    pos {pos_step1}")
print(f"  Paso 3:    pos {pos_step3}")
print(f"  NOTAS:     pos {pos_notas}")
print()

if pos_title >= 0 and pos_title < 100:
    print("OK: Título al inicio del texto")
else:
    print(f"FAIL: Título en posición {pos_title} (debería ser < 100)")

if pos_intro >= 0 and pos_intro < pos_step3:
    print("OK: Intro antes de paso 3")
else:
    print(f"FAIL: Intro ({pos_intro}) no está antes de paso 3 ({pos_step3})")

if pos_step1 >= 0 and pos_step1 < pos_step3:
    print("OK: Paso 1 antes de paso 3")
else:
    print(f"FAIL: Paso 1 ({pos_step1}) no está antes de paso 3 ({pos_step3})")

print(f"\n=== TEXTO COMPLETO ({len(full_text)} chars) ===")
print(full_text)

os.unlink(tmp)
