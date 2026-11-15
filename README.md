# mblanco

Chat y transferencia de archivos para dos personas que trabajan en el mismo proyecto. Cada quien conserva su propia carpeta; solo se comparte `compartido/`. No requiere Git, commits, base de datos ni un proceso de sincronización separado.

## Instalar

Desde la carpeta de este proyecto:

```sh
python -m pip install -e .
```

Python 3.10 o posterior. La interfaz inicial es una consola interactiva; `textual` queda como extra opcional mientras se prioriza el flujo de red.

## Probar en una computadora, con dos terminales

Abre **dos terminales en dos carpetas distintas**. En ambas, instala `mblanco` con el comando anterior (o instala una sola vez en el mismo entorno Python).

Terminal 1, desde la carpeta local de la anfitriona:

```sh
mkdir -p ~/prueba-mblanco/anfitriona/compartido
cd ~/prueba-mblanco/anfitriona
mblanco host
```

Terminal 2, desde otra carpeta local:

```sh
mkdir -p ~/prueba-mblanco/colega/compartido
cd ~/prueba-mblanco/colega
mblanco join http://127.0.0.1:8765 --name colega
```

Escribe mensajes en cualquiera de las terminales. Para compartir un archivo después de conectarse, cópialo a la carpeta `compartido/` de cualquiera de las dos. La otra instancia lo detecta y descarga automáticamente. Los comandos `F2` o `/files`, `F3` o `/sync` y `F10` o `/quit` muestran archivos, revisan pendientes y salen. Las teclas de función se pueden escribir como comandos; la consola sencilla no captura pulsaciones especiales.

Para comprobar pendientes con la app cerrada, termina ambas instancias, copia archivos a uno o ambos `compartido/`, inicia el host y vuelve a hacer `join`. La app muestra `↑ ... por subir · ↓ ... por recibir`, enumera nombres y pide confirmar antes de transferir. Si el host no responde, informa que no pudo revisar los archivos remotos.

## Probar entre dos computadoras por Tailscale

Instala y conecta Tailscale en las dos computadoras. En ambas, instala `mblanco` en un entorno Python y abre la carpeta local del proyecto correspondiente.

En la computadora anfitriona, permite conexiones por su interfaz de red:

```sh
cd ~/ruta/local/al/proyecto
mblanco host --listen 0.0.0.0 --port 8765
```

En la otra computadora, sustituye `100.x.y.z` por la IP Tailscale de la anfitriona:

```sh
cd ~/otra/ruta/local/al/proyecto
mblanco join http://100.x.y.z:8765 --name colega
```

No se configura VPN ni descubrimiento: Tailscale proporciona la red privada. La anfitriona debe mantener `mblanco host` abierto; si se apaga, la sala deja de responder. Los archivos ya descargados permanecen en cada carpeta. Si el firewall del equipo anfitrión bloquea conexiones entrantes, permite TCP en el puerto elegido dentro de la interfaz Tailscale.

## Alcance de esta versión

Solo recorre y transfiere archivos regulares dentro de `compartido/`, conservando rutas relativas. Las subidas y descargas se escriben de forma atómica. Si una ruta existe con contenido distinto en ambos lados, se informa el conflicto y no se sobrescribe. No se propagan borrados ni se fusiona contenido. El escaneo periódico solo observa archivos nuevos y exige una lectura estable antes de anunciar una adición detectada localmente.
