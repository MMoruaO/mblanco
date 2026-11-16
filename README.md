# mblanco

Chat y transferencia de archivos para dos personas que trabajan en el mismo proyecto. Cada quien conserva su propia carpeta; solo se comparte `compartido/`. No requiere Git, commits, base de datos ni un proceso de sincronización separado.

## Instalar

Instala mblanco una vez por computadora. Clona el repositorio en una carpeta de herramientas, no dentro del proyecto que compartirás:

```sh
git clone https://github.com/MMoruaO/mblanco.git ~/tools/mblanco
cd ~/tools/mblanco
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

### Dependencias

Requisitos del sistema:

- Python 3.10 o posterior.
- `venv` para crear el entorno virtual.
- Git para clonar el repositorio.
- Tailscale solo si se conectarán desde computadoras distintas.

En Debian/Ubuntu, instala Python, `venv` y Git con:

```sh
sudo apt update
sudo apt install python3 python3-venv git
```

En otras distribuciones, instala los paquetes equivalentes de Python 3, `venv` y Git con el gestor de paquetes correspondiente (por ejemplo, `dnf`, `pacman` o `zypper`). Los nombres exactos pueden variar según la distribución.

Dependencias de Python, instaladas automáticamente por `python -m pip install -e .`:

- FastAPI `>=0.110` y Uvicorn `>=0.27` para el servidor de sala.
- `websockets >=12` para el chat.
- Textual `>=0.50` para la interfaz de terminal.
- Setuptools `>=68` para construir e instalar el paquete.

No hace falta instalarlas una por una. En cada terminal nueva, activa el entorno con `source ~/tools/mblanco/.venv/bin/activate` antes de usar el comando. La interfaz abre una pantalla TUI con el chat, entrada abajo y atajos F2/F3/F10.

## Inicio guiado

Ve a la carpeta local del proyecto y ejecuta `mblanco`:

```sh
cd ~/ruta/local/al/proyecto
mblanco
```

La primera vez, mblanco confirma la carpeta actual y aclara que solo accederá a `compartido/`. Después pregunta si crear una sala o unirse a una. La elección se guarda en la configuración local de tu usuario, fuera del proyecto; las siguientes veces basta con ejecutar `mblanco` en esa carpeta. Usa `mblanco setup` para cambiar de sala o de rol.

Si Tailscale está conectado y mblanco puede detectar su IP, al crear una sala escucha en esa IP privada, verifica que el servidor responda y muestra el comando completo para tu colega. También corrige una configuración guiada que hubiera quedado en `127.0.0.1`. Esa verificación confirma el servidor local; tu colega confirma la ruta entre computadoras al ejecutar el comando de conexión. Sin Tailscale, el inicio guiado solo habilita conexiones locales. Para cambiar manualmente el puerto o la interfaz de escucha, usa los comandos `mblanco host` y `mblanco join` de abajo.

## Probar en una computadora, con dos terminales

Abre **dos terminales en dos carpetas distintas** y activa el entorno de mblanco en ambas. La anfitriona elige crear la sala y la otra persona elige unirse. También puedes usar los comandos explícitos siguientes.

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

Escribe mensajes en cualquiera de las terminales. Para compartir un archivo después de conectarse, cópialo a la carpeta `compartido/` de cualquiera de las dos. La otra instancia lo detecta y descarga automáticamente. `F2` abre la vista de archivos, `F3` revisa pendientes y `F10` sale.

Para comprobar pendientes con la app cerrada, termina ambas instancias, copia archivos a uno o ambos `compartido/`, inicia el host y vuelve a hacer `join`. La app muestra `↑ ... por subir · ↓ ... por recibir`, enumera nombres y pide confirmar antes de transferir. Si el host no responde, informa que no pudo revisar los archivos remotos.

## Probar entre dos computadoras por Tailscale

Instala y conecta Tailscale en las dos computadoras. En ambas, instala mblanco una vez y activa su entorno en la terminal. Después abre la carpeta local del proyecto correspondiente.

En la computadora anfitriona:

```sh
cd ~/ruta/local/al/proyecto
mblanco
```

Elige `1` para crear la sala. Si el asistente detecta Tailscale, mostrará la dirección que debe usar tu colega.

En la computadora de tu colega:

```sh
cd ~/otra/ruta/local/al/proyecto
mblanco
```

Elige `2` y pega la dirección que te compartió la anfitriona. La anfitriona debe mantener mblanco abierto; si se apaga, la sala deja de responder. Los archivos ya descargados permanecen en cada carpeta. No se configura VPN ni se busca automáticamente a la otra computadora: Tailscale da la conectividad y ustedes intercambian la dirección de la sala.

## Alcance de esta versión

Solo recorre y transfiere archivos regulares dentro de `compartido/`, conservando rutas relativas. Las subidas y descargas se escriben de forma atómica. Si una ruta existe con contenido distinto en ambos lados, se informa el conflicto y no se sobrescribe. No se propagan borrados ni se fusiona contenido. El escaneo periódico solo observa archivos nuevos y exige una lectura estable antes de anunciar una adición detectada localmente.
