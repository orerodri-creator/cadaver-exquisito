# Cadáver Exquisito

Juego para escribir un poema a muchas manos. Cada persona ve solo las últimas
palabras del verso anterior. El poema completo se revela al llegar al último verso.
Nadie necesita cuenta: cada jugador abre el enlace y escribe su nombre.

## Probarlo en tu computadora

Necesitas Python 3 (en Mac ya viene instalado).

```bash
python3 server.py
```

Abre http://localhost:8000. Las partidas se guardan en `data.json`.

## Publicarlo gratis en internet

Vas a usar tres servicios gratuitos:

- **GitHub**, para guardar el código.
- **Neon**, para la base de datos donde quedan las partidas.
- **Render**, para que la app tenga una dirección pública.

Hace falta la base de datos porque el plan gratuito de Render borra los
archivos cada vez que el servidor se reinicia, y eso pasa a menudo.

### 1. Sube el código a GitHub

1. Crea una cuenta en https://github.com si no tienes una.
2. Crea un repositorio nuevo (por ejemplo `cadaver-exquisito`).
3. En la página del repositorio elige **uploading an existing file** y arrastra
   todo el contenido de esta carpeta: `server.py`, `requirements.txt`,
   `render.yaml`, `README.md`, `.gitignore` y la carpeta `public`.
   No subas `data.json`.

### 2. Crea la base de datos en Neon

1. Crea una cuenta en https://neon.tech (plan Free).
2. Crea un proyecto. Elige la región más cercana a tus jugadores.
3. En **Connection string** copia la dirección que empieza con `postgresql://`.
   Guárdala, la vas a necesitar en el paso siguiente. Es una clave: no la compartas.

### 3. Publica en Render

1. Crea una cuenta en https://render.com y conéctala con tu GitHub.
2. Elige **New → Blueprint** y selecciona tu repositorio. Render lee
   `render.yaml` y prepara el servicio solo.
3. Cuando te pida `DATABASE_URL`, pega la dirección de Neon.
4. Pulsa **Apply**. En unos minutos tendrás una dirección como
   `https://cadaver-exquisito.onrender.com`.

Esa es la dirección que compartes con los jugadores.

## Cosas que conviene saber

- **Primera visita lenta.** En el plan gratuito de Render el servidor se
  duerme tras 15 minutos sin visitas. La siguiente persona que entra espera
  unos 30 a 60 segundos mientras despierta. Las partidas no se pierden.
- **Turnos.** Quien toma el turno tiene 4 minutos. Mientras tanto nadie más
  puede escribir en esa partida.
- **Borrar y revelar.** Solo puede hacerlo quien abrió la partida, desde el
  mismo navegador con que la abrió.
- **Privacidad.** El servidor nunca envía los versos ocultos al navegador.
  Cualquiera que tenga la dirección de la app puede ver la lista de partidas
  y jugar.
- **Límites.** Hasta 300 partidas guardadas y 160 caracteres por verso.
  Se pueden cambiar al principio de `server.py`.
