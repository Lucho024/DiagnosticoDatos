# ⛏ GitHub Word Miner

Dashboard en tiempo real que mina las palabras más usadas en nombres de **funciones y métodos** de Python y Java, extrayéndolas de los repositorios más populares de GitHub.

## Arquitectura

```
┌─────────────┐   pub/sub + sorted set   ┌──────────────┐   SSE   ┌─────────┐
│    Miner    │ ───────────────────────► │  Visualizer  │ ──────► │Browser  │
│ (Producer)  │          │              │  (Consumer)  │         │Dashboard│
└─────────────┘          ▼              └──────────────┘         └─────────┘
                     ┌───────┐
                     │ Redis │
                     └───────┘
```

| Contenedor   | Rol        | Tecnología           |
|--------------|------------|----------------------|
| `redis`      | Broker     | Redis 7              |
| `miner`      | Producer   | Python 3.12          |
| `visualizer` | Consumer   | Python 3.12 + Flask  |



## Ejecución (un solo comando)

```bash
docker compose up --build
```

Luego abre **http://localhost:5000** en tu navegador.

El dashboard comienza a mostrar datos en ~30-60 segundos (tiempo que tarda el miner en procesar el primer repositorio).

## Detener

```bash
docker compose down
```

Para borrar también los conteos acumulados en Redis:

```bash
docker compose down -v
```

## Configuración avanzada

Todas las variables se configuran en `docker-compose.yml`:

| Variable             | Default | Descripción                              |
|----------------------|---------|------------------------------------------|
| `GITHUB_TOKEN`       | —       | Token de GitHub (requerido)              |
| `REPOS_PER_PAGE`     | `10`    | Repos por página de búsqueda             |
| `MAX_FILES_PER_REPO` | `20`    | Máximo de archivos procesados por repo   |
| `SLEEP_BETWEEN_REPOS`| `2`     | Segundos entre repos (respetar rate limit)|
| `DEFAULT_TOP_N`      | `20`    | Palabras mostradas por defecto           |
| `PORT`               | `5000`  | Puerto del visualizer                    |

## Decisiones de diseño

### ¿Por qué Redis?
- **pub/sub** permite fan-out instantáneo a todos los clientes conectados
- **sorted set** (`ZINCRBY` / `ZREVRANGE`) actúa como leaderboard persistente en O(log N)
- Sin esquema SQL, sin migraciones, cero configuración adicional

### ¿Por qué SSE y no WebSockets?
El flujo de datos es estrictamente servidor → cliente (los conteos solo suben). SSE es más simple, funciona sobre HTTP/1.1 puro y reconecta automáticamente.

### División de palabras

| Entrada          | Resultado                        |
|------------------|----------------------------------|
| `make_response`  | `make`, `response`               |
| `retainAll`      | `retain`, `all`                  |
| `getHTTPSUrl`    | `get`, `https`, `url`            |
| `dispatch_request` | `dispatch`, `request`          |

Algoritmo: split en `_` → split en bordes camelCase → lowercase → descartar tokens de 1 carácter.

### Minería continua
El miner intercala Python y Java en un loop infinito, procesando repos de mayor a menor stars. Al agotar las páginas disponibles, reinicia desde la página 1 para garantizar operación continua hasta ser detenido.

## Estructura del repositorio

```
.
├── .gitignore
├── docker-compose.yml
├── README.md
├── miner/
│   ├── Dockerfile
│   ├── miner.py
│   └── requirements.txt
└── visualizer/
    ├── Dockerfile
    ├── visualizer.py
    └── requirements.txt
```
